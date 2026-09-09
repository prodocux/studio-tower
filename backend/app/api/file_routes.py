
import json
import logging
import uuid

from app.core.auth import get_current_user
from app.core.config import settings
from app.models.activity import ActivityEventType
from app.models.file_record import (
    DocumentChunksResponse,
    DocumentChunkSummary,
    FileIngestionStatusResponse,
    FileRecord,
    FileSourceType,
    IngestionStatus,
)
from app.models.space import MembershipRole
from app.models.user import User
from app.services.activity_service import ActivityService
from app.services.file_service import FileService
from app.services.space_service import SpaceService
from app.services.ingestion_runner import execute_ingestion_job, get_ingestion_runner, load_file_binary
from app.services.storage import store
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel

logger = logging.getLogger("studiotower.file_routes")
router = APIRouter(prefix="/v1", tags=["files"])


class ShareFileRequest(BaseModel):
    target_space_id: str
    target_project_tags: list[str] | None = None


@router.post("/spaces/{space_id}/files", response_model=FileRecord)
async def upload_space_file(
    space_id: str,
    file: UploadFile = File(...),
    project_tag: str | None = Form(default="general"),
    current_user: User = Depends(get_current_user),
):
    """Upload a file into a Space with active project tag and size limit validation."""
    max_size = settings.MAX_UPLOAD_SIZE_BYTES
    content = await file.read(max_size + 1)

    if len(content) > max_size:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File size exceeds maximum allowed limit of {max_size // (1024 * 1024)}MB",
        )

    tags = [project_tag] if project_tag else ["general"]

    uploaded_rec = FileService.upload_file(
        space_id=space_id,
        filename=file.filename or "unnamed_file",
        content=content,
        content_type=file.content_type or "application/octet-stream",
        user=current_user,
        project_tags=tags,
        source_type=FileSourceType.USER_UPLOAD,
    )
    ActivityService.record_file_event(
        space_id=space_id,
        event_type=ActivityEventType.FILE_UPLOADED,
        file_record=uploaded_rec,
        actor_uid=current_user.uid,
    )
    return uploaded_rec


@router.get("/spaces/{space_id}/files", response_model=list[FileRecord])
def list_space_files(
    space_id: str,
    tag: str | None = Query(default=None, description="Filter by project tag"),
    current_user: User = Depends(get_current_user),
):
    """List all files in a Space accessible to the user."""
    return FileService.list_files(space_id, current_user, project_tag=tag)


@router.get("/spaces/{space_id}/files/{file_id}", response_model=FileRecord)
def get_file_details(
    space_id: str,
    file_id: str,
    current_user: User = Depends(get_current_user),
):
    """Get metadata for a specific file in a Space."""
    return FileService.get_file_record(space_id, file_id, current_user)


@router.delete("/spaces/{space_id}/files/{file_id}")
def delete_space_file(
    space_id: str,
    file_id: str,
    current_user: User = Depends(get_current_user),
):
    """Delete a file permanently from a Space."""
    file_rec = FileService.get_file_record(space_id, file_id, current_user)
    user_role = SpaceService.get_user_role(space_id, current_user)
    is_admin = user_role in [
        MembershipRole.OWNER,
        MembershipRole.ADMIN,
        MembershipRole.COORDINATOR,
    ]
    is_uploader = file_rec.uploaded_by == current_user.uid
    if not (is_admin or is_uploader):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: only file uploader or space administrators can delete files",
        )

    FileService.delete_file_permanently(file_rec)
    return {"status": "ok", "deleted_file_id": file_id}


@router.get("/spaces/{space_id}/files/{file_id}/download")
def download_file(
    space_id: str,
    file_id: str,
    current_user: User = Depends(get_current_user),
):
    """Download binary content of a file with Space membership check."""
    import re
    import urllib.parse

    content, record = FileService.read_file_content(space_id, file_id, current_user)
    ascii_filename = re.sub(r"[^\x20-\x7E]", "_", record.filename) or "file"
    encoded_filename = urllib.parse.quote(record.filename)
    return Response(
        content=content,
        media_type=record.content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{ascii_filename}"; filename*=UTF-8\'\'{encoded_filename}',
            "X-SHA256-Checksum": record.sha256,
        },
    )


@router.post("/spaces/{source_space_id}/files/{file_id}/share", response_model=FileRecord)
def share_file_to_space(
    source_space_id: str,
    file_id: str,
    payload: ShareFileRequest,
    current_user: User = Depends(get_current_user),
):
    """
    Copy a file to another Space.
    Caller must be an active member of both source and target Spaces.
    """
    return FileService.copy_file_to_space(
        source_space_id=source_space_id,
        file_id=file_id,
        target_space_id=payload.target_space_id,
        user=current_user,
        target_project_tags=payload.target_project_tags,
    )


@router.get("/files/search", response_model=list[FileRecord])
def search_files(
    q: str = Query(..., min_length=1, description="Search keyword"),
    current_user: User = Depends(get_current_user),
):
    """Search files only across Spaces the caller belongs to."""
    return FileService.search_accessible_files(q, current_user)


@router.post("/maintenance/cleanup-pending-files")
def run_pending_file_cleanups(
    x_maintenance_key: str | None = Header(default=None, alias="X-Maintenance-Key"),
):
    """
    Background maintenance job to scan and permanently clean up files marked cleanup_pending.
    Strictly protected by internal maintenance secret key with constant-time comparison.
    """
    import secrets

    is_valid = False
    if x_maintenance_key and settings.STUDIO_TOWER_MAINTENANCE_SECRET:
        is_valid = secrets.compare_digest(x_maintenance_key, settings.STUDIO_TOWER_MAINTENANCE_SECRET)

    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: valid X-Maintenance-Key is required for system maintenance operations",
        )

    try:
        return FileService.retry_pending_cleanups()
    except Exception as e:
        from app.services.storage import StorageUnavailableError
        if isinstance(e, StorageUnavailableError):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Database backend is temporarily unavailable during maintenance scan. Please retry shortly.",
            )
        raise


# -----------------------------------------------------------------------------
# Document Ingestion & Chunking API Endpoints
# -----------------------------------------------------------------------------


@router.get("/spaces/{space_id}/files/{file_id}/status", response_model=FileIngestionStatusResponse)
def get_file_ingestion_status(
    space_id: str,
    file_id: str,
    current_user: User = Depends(get_current_user),
):
    """
    Returns the current ingestion lifecycle stage, active generation, OCR gap pages, and chunk metrics.
    Protected by Space tenancy isolation (404 if file does not belong to Space).
    """
    file_rec = FileService.get_file_record(space_id, file_id, current_user)
    return FileIngestionStatusResponse(
        file_id=file_rec.file_id,
        space_id=file_rec.space_id,
        ingestion_status=file_rec.ingestion_status,
        active_generation=file_rec.active_generation,
        ingestion_job_id=file_rec.ingestion_job_id,
        ingestion_version=file_rec.ingestion_version,
        chunk_count=file_rec.chunk_count,
        extracted_pages=file_rec.extracted_pages,
        ocr_gap_pages=file_rec.ocr_gap_pages,
        has_ocr_gaps=file_rec.has_ocr_gaps,
        error_code=file_rec.ingestion_error_code,
        started_at=file_rec.ingestion_started_at,
        completed_at=file_rec.ingestion_completed_at,
    )


@router.get("/spaces/{space_id}/files/{file_id}/chunks", response_model=DocumentChunksResponse)
def get_file_document_chunks(
    space_id: str,
    file_id: str,
    generation: int | None = Query(default=None, description="Optional target generation"),
    cursor: int = Query(default=0, ge=0, description="Pagination cursor offset"),
    limit: int = Query(default=20, ge=1, le=100, description="Page size limit (max 100)"),
    page: int | None = Query(default=None, description="Optional page number filter"),
    section: str | None = Query(default=None, description="Optional section filter"),
    current_user: User = Depends(get_current_user),
):
    """
    Returns paginated DocumentChunkSummary records with cursor envelope for the specified or active generation.
    Strictly isolated by Space tenancy.
    """
    # Enforces tenancy & permission checks
    file_rec = FileService.get_file_record(space_id, file_id, current_user)

    target_gen = generation if generation is not None else file_rec.active_generation
    if target_gen is not None and target_gen > 0:
        committed_set = set(file_rec.committed_generations or [])
        if file_rec.active_generation and file_rec.active_generation >= 1:
            committed_set.add(file_rec.active_generation)
        if target_gen not in committed_set or target_gen in (file_rec.deleted_generations or []):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="TARGET_GENERATION_UNAVAILABLE",
            )
        all_chunks = store.get_document_chunks(space_id, file_id, generation=target_gen)
    else:
        all_chunks = store.get_document_chunks(space_id, file_id)

    # Optional filtering
    filtered = all_chunks
    if page is not None:
        filtered = [c for c in filtered if c.page_number == page]
    if section is not None:
        sec_lower = section.lower()
        filtered = [
            c
            for c in filtered
            if (c.section_heading and sec_lower in c.section_heading.lower())
            or (c.source_locator and sec_lower in c.source_locator.lower())
        ]

    # Paginate
    total = len(filtered)
    paginated = filtered[cursor : cursor + limit]
    items = [DocumentChunkSummary.model_validate(c.model_dump()) for c in paginated]
    has_more = (cursor + limit) < total
    next_cursor = (cursor + limit) if has_more else None

    return DocumentChunksResponse(
        items=items,
        cursor=cursor,
        limit=limit,
        total=total,
        has_more=has_more,
        next_cursor=next_cursor,
        active_generation=target_gen or file_rec.active_generation or 0,
    )


@router.get("/spaces/{space_id}/files/{file_id}/citations/{chunk_id}")
def get_citation_chunk(
    space_id: str,
    file_id: str,
    chunk_id: str,
    generation: int = Query(..., description="Target generation of the citation"),
    current_user: User = Depends(get_current_user),
):
    """
    Resolves the exact document chunk referenced by a Citation for the specified generation.
    Strictly fenced: if the specified generation is no longer available in storage (cleaned/tombstoned),
    returns 404 with detail 'SOURCE_GENERATION_UNAVAILABLE' rather than silently switching versions.
    """
    file_rec = FileService.get_file_record(space_id, file_id, current_user)

    # Validate that the requested generation was legitimately committed/published
    committed_set = set(file_rec.committed_generations or [])
    if file_rec.active_generation and file_rec.active_generation >= 1:
        committed_set.add(file_rec.active_generation)

    if generation not in committed_set:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SOURCE_GENERATION_UNAVAILABLE",
        )

    # Disallow tombstoned or cleaning generations
    if generation in (file_rec.deleted_generations or []) or generation in (file_rec.cleaning_generations or []):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SOURCE_GENERATION_UNAVAILABLE",
        )

    chunks = store.get_document_chunks(space_id, file_id, generation=generation)
    matched = next((c for c in chunks if c.chunk_id == chunk_id), None)
    if not matched:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SOURCE_GENERATION_UNAVAILABLE",
        )

    return matched


@router.post("/spaces/{space_id}/files/{file_id}/reindex")
def trigger_file_reindex(
    space_id: str,
    file_id: str,
    current_user: User = Depends(get_current_user),
):
    """
    Triggers an asynchronous re-indexing / OCR generation swap for a committed file.
    Restricted to space governance roles (OWNER, ADMIN, COORDINATOR).
    Idempotently returns existing job ID if already in progress with active lease.
    """
    file_rec = FileService.get_file_record(space_id, file_id, current_user)

    # Governance check: Caller must be OWNER, ADMIN, or COORDINATOR in space
    role = store.get_member_role(space_id, current_user.uid)
    if not role or role not in (MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.COORDINATOR):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only space owners, admins, or coordinators can trigger document re-indexing.",
        )

    # If already running with valid lease, return active job idempotently
    from datetime import UTC, datetime
    now = datetime.now(UTC)
    if (
        file_rec.ingestion_status in (IngestionStatus.EXTRACTING, IngestionStatus.INDEXING)
        and file_rec.ingestion_lease_until
        and file_rec.ingestion_lease_until > now
    ):
        return Response(
            content=json.dumps({
                "status": "in_progress",
                "job_id": file_rec.ingestion_job_id,
                "file_id": file_id,
                "message": "Ingestion job is already actively running.",
            }),
            status_code=status.HTTP_202_ACCEPTED,
            media_type="application/json",
        )

    # Read content
    content, _ = FileService.read_file_content(space_id, file_id, current_user)

    # Dispatch ingestion job
    runner = get_ingestion_runner()
    try:
        job_id = runner.submit_ingestion(
            space_id=space_id,
            file_id=file_id,
            content_bytes=content,
            filename=file_rec.filename,
            store=store,
        )
        ActivityService.record_file_event(
            space_id=space_id,
            event_type=ActivityEventType.FILE_REINDEX_TRIGGERED,
            file_record=file_rec,
            actor_uid=current_user.uid,
        )
    except Exception as dispatch_err:
        logger.exception(f"Failed to dispatch reindex job for file {file_id}: {dispatch_err}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to dispatch ingestion task: {dispatch_err}",
        )

    return Response(
        content=json.dumps({
            "status": "accepted",
            "job_id": job_id,
            "file_id": file_id,
            "message": "Re-indexing generation job successfully dispatched.",
        }),
        status_code=status.HTTP_202_ACCEPTED,
        media_type="application/json",
    )


class IngestionTaskPayload(BaseModel):
    space_id: str
    file_id: str
    filename: str
    target_generation: int
    job_id: str


@router.post("/internal/tasks/ingest-document")
def execute_cloud_tasks_ingestion_callback(
    payload: IngestionTaskPayload,
    x_task_secret: str | None = Header(default=None, alias="X-Task-Secret"),
):
    """
    Cloud Tasks worker execution callback.
    Protected by constant-time secret comparison against STUDIO_TOWER_TASK_SECRET.
    """
    import secrets

    is_valid = False
    if x_task_secret and settings.STUDIO_TOWER_TASK_SECRET:
        is_valid = secrets.compare_digest(x_task_secret, settings.STUDIO_TOWER_TASK_SECRET)
    if not is_valid and x_task_secret and settings.STUDIO_TOWER_TASK_SECRET_PREVIOUS:
        is_valid = secrets.compare_digest(x_task_secret, settings.STUDIO_TOWER_TASK_SECRET_PREVIOUS)

    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: valid X-Task-Secret is required for Cloud Tasks execution",
        )

    f_rec = store.get_file(payload.file_id)
    if not f_rec:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File record not found")

    content = load_file_binary(store, f_rec)
    if content is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Storage read error: binary content unavailable for file {payload.file_id}",
        )

    worker_id = f"worker_cloud_task_{uuid.uuid4().hex[:8]}"
    success = execute_ingestion_job(
        space_id=payload.space_id,
        file_id=payload.file_id,
        content_bytes=content,
        filename=payload.filename,
        store=store,
        worker_id=worker_id,
        target_generation=payload.target_generation,
        expected_job_id=payload.job_id,
    )
    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Ingestion processing failed",
        )

    return {"status": "success", "file_id": payload.file_id, "generation": payload.target_generation}


@router.post("/maintenance/reclaim-ingestion-leases")
def reclaim_expired_ingestion_leases(
    x_maintenance_key: str | None = Header(default=None, alias="X-Maintenance-Key"),
):
    """
    Durable maintenance cron endpoint (triggered by Cloud Scheduler) to scan and reclaim expired ingestion leases
    and automatically re-dispatch stalled ingestion tasks.
    """
    import secrets

    is_valid = False
    if x_maintenance_key and settings.STUDIO_TOWER_MAINTENANCE_SECRET:
        is_valid = secrets.compare_digest(x_maintenance_key, settings.STUDIO_TOWER_MAINTENANCE_SECRET)

    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: valid X-Maintenance-Key is required for lease recovery",
        )

    runner = get_ingestion_runner()
    reclaimed = runner.recover_expired_leases(store)
    return {
        "status": "success",
        "reclaimed_count": len(reclaimed),
        "reclaimed_file_ids": [f.file_id for f in reclaimed],
        "redispatched": len(reclaimed) > 0,
        "redispatched_count": len(reclaimed),
    }

