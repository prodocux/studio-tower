import hashlib
import logging
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Optional

from app.core.config import settings
from app.models.file_record import FileRecord, FileSourceType
from app.models.space import SpaceKind
from app.models.user import User
from app.services.storage import store
from fastapi import HTTPException, status

logger = logging.getLogger(__name__)


class HeartbeatTracker:
    def __init__(self, file_id: str):
        self.file_id = file_id
        self.last_heartbeat_at: Optional[datetime] = None
        self.last_error: Optional[str] = None
        self.is_healthy: bool = True
        self.failure_count: int = 0

    def record_success(self):
        self.last_heartbeat_at = datetime.now(UTC)
        self.failure_count = 0
        self.last_error = None
        self.is_healthy = True

    def record_failure(self, error_msg: str, is_fatal: bool = False):
        self.failure_count += 1
        self.last_error = error_msg
        if is_fatal or self.failure_count >= 1:
            self.is_healthy = False


@contextmanager
def upload_heartbeat(
    file_id: str,
    upload_fencing_token: str,
    interval_seconds: float = 5.0,
    extension_seconds: int = 30,
):
    """
    Background heartbeat worker to continuously extend upload grace period
    while data streaming / physical storage writes are actively in progress.
    Guarantees thread is completely joined and stopped before exiting context.
    """
    tracker = HeartbeatTracker(file_id)
    stop_event = threading.Event()

    def _worker():
        while not stop_event.wait(timeout=interval_seconds):
            try:
                renewed = store.renew_upload_lease(
                    file_id=file_id,
                    upload_fencing_token=upload_fencing_token,
                    extension_seconds=extension_seconds,
                )
                if renewed:
                    tracker.record_success()
                else:
                    tracker.record_failure(
                        "Upload lease renewal rejected: token mismatch or claimed by cleanup.",
                        is_fatal=True,
                    )
                    break
            except Exception as e:
                tracker.record_failure(f"Storage error during heartbeat: {e}", is_fatal=True)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    try:
        yield tracker
    finally:
        stop_event.set()
        t.join(timeout=2.0)
        if t.is_alive():
            tracker.record_failure("Heartbeat thread join timeout", is_fatal=True)


class FileService:
    @staticmethod
    def _sanitize_filename(filename: str) -> str:
        base = os.path.basename(filename.replace("\\", "/"))
        clean = "".join(c for c in base if c.isalnum() or c in (".", "-", "_")).strip()
        while ".." in clean:
            clean = clean.replace("..", ".")
        return clean if clean else "unnamed_file"

    @staticmethod
    def _compensate_failed_intent(
        record: FileRecord,
        upload_fencing_token: str,
        trigger_err: Exception,
    ) -> None:
        """
        Unified, CAS-protected compensating rollback for failed uploads/copies.
        1. Tries deleting physical blob from Local FS / GCS.
        2. If physical deletion succeeds: removes metadata intent ONLY if still owned (0 orphan blobs).
        3. If physical deletion fails: marks upload_status="failed", cleanup_pending=True using CAS
           (refusing to overwrite active cleanup worker lease if already reclaimed).
        """
        try:
            if settings.STUDIO_TOWER_ARTIFACT_BACKEND == "local":
                data_dir_abs = os.path.abspath(settings.STUDIO_TOWER_DATA_DIR)
                full_path = os.path.abspath(os.path.join(data_dir_abs, record.storage_path))
                if os.path.exists(full_path):
                    os.remove(full_path)
            elif settings.STUDIO_TOWER_ARTIFACT_BACKEND == "gcs":
                from google.cloud import storage as gcs
                client = gcs.Client()
                bucket = client.bucket(settings.STUDIO_TOWER_GCS_BUCKET)
                blob = bucket.blob(record.storage_path)
                if blob.exists():
                    blob.delete()

            # Physical deletion succeeded: conditionally delete metadata intent if still owned
            store.delete_upload_intent_if_owned(record.file_id, upload_fencing_token)
        except Exception as comp_err:
            logger.error(
                "Physical compensation failed for %s: %s (Trigger: %s)",
                record.file_id,
                comp_err,
                trigger_err,
            )
            # Physical deletion failed: mark as failed using CAS to avoid stomping concurrent cleanup worker
            err_msg = f"COMPENSATION_FAILED: {type(comp_err).__name__} (Orig: {type(trigger_err).__name__})"
            store.mark_upload_failed_if_owned(record.file_id, upload_fencing_token, err_msg)

    @staticmethod
    def upload_file(
        space_id: str,
        filename: str,
        content: bytes,
        content_type: str,
        user: User,
        project_tags: Optional[list[str]] = None,
        source_type: FileSourceType = FileSourceType.USER_UPLOAD,
        run_id: Optional[str] = None,
        publication_status: str = "published",
        file_id: Optional[str] = None,
    ) -> FileRecord:
        space = store.get_space(space_id)
        if not space:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Space not found")

        # Tenancy authorization
        if not store.is_member(space_id, user.uid):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied to space file library")

        # 1. Compute SHA-256 Checksum and unique object storage path
        sha256_hash = hashlib.sha256(content).hexdigest()
        size_bytes = len(content)

        if file_id:
            existing = store.get_file(file_id)
            if existing:
                if (
                    existing.sha256 == sha256_hash
                    and existing.space_id == space_id
                    and (run_id is None or existing.run_id == run_id)
                ):
                    return existing  # Idempotent duplicate
                else:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"DETERMINISTIC_ARTIFACT_CONTENT_MISMATCH: File '{file_id}' exists with different content or metadata",
                    )
        else:
            file_id = f"file_{uuid.uuid4().hex[:12]}"

        clean_name = FileService._sanitize_filename(filename)
        storage_rel_path = f"{space_id}/files/{file_id}_{clean_name}"
        upload_fencing_token = uuid.uuid4().hex
        now = datetime.now(UTC)

        tags = project_tags if (project_tags and len(project_tags) > 0) else ["general"]
        record = FileRecord(
            file_id=file_id,
            space_id=space_id,
            filename=clean_name,
            content_type=content_type or "application/octet-stream",
            size_bytes=size_bytes,
            sha256=sha256_hash,
            storage_path=storage_rel_path,
            project_tags=tags,
            uploaded_by=user.uid,
            source_type=source_type,
            run_id=run_id,
            upload_status="pending_upload",
            publication_status=publication_status,
            upload_fencing_token=upload_fencing_token,
            upload_lease_until=now + timedelta(seconds=60),  # Initial grace period with dynamic heartbeat renewal
            cleanup_pending=False,
            cleanup_status="pending",
        )

        # 2-Phase Commit Phase 1: Durable Intent Persistence
        store.save_file(record)

        # 2-Phase Commit Phase 2: Physical Blob Write with Heartbeat Lease Renewal
        try:
            with upload_heartbeat(record.file_id, upload_fencing_token, interval_seconds=5.0, extension_seconds=30) as hb_tracker:
                if settings.STUDIO_TOWER_ARTIFACT_BACKEND == "local":
                    data_dir_abs = os.path.abspath(settings.STUDIO_TOWER_DATA_DIR)
                    full_path = os.path.abspath(os.path.join(data_dir_abs, storage_rel_path))

                    # Strict boundary check: full_path MUST stay within data_dir_abs
                    if not full_path.startswith(data_dir_abs):
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Invalid storage path traversal detected",
                        )

                    os.makedirs(os.path.dirname(full_path), exist_ok=True)
                    with open(full_path, "wb") as f:
                        f.write(content)
                elif settings.STUDIO_TOWER_ARTIFACT_BACKEND == "gcs":
                    from google.cloud import storage as gcs
                    client = gcs.Client()
                    bucket_name = settings.STUDIO_TOWER_GCS_BUCKET
                    if not bucket_name or bucket_name == "agentic-cinema-demo-2026-artifacts":
                        bucket_name = "agentic-cinema-demo-2026-studiotower-artifacts"
                    bucket = client.bucket(bucket_name)
                    blob = bucket.blob(storage_rel_path)
                    blob.upload_from_string(content, content_type=content_type or "application/octet-stream")

            # Post-Heartbeat Stop Check (verified after thread completely joined)
            if not hb_tracker.is_healthy:
                logger.warning("Upload heartbeat failed for file %s: %s", record.file_id, hb_tracker.last_error)
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Upload lease could not be maintained. Upload aborted for data safety. Please retry.",
                )
        except HTTPException as http_err:
            FileService._compensate_failed_intent(record, upload_fencing_token, http_err)
            raise
        except Exception as upload_err:
            FileService._compensate_failed_intent(record, upload_fencing_token, upload_err)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Cloud storage upload failed. Request logged for diagnosis.",
            )

        # 2-Phase Commit Phase 3: Atomic Final Commit
        committed_rec = store.commit_uploaded_file(
            file_id=record.file_id,
            upload_fencing_token=upload_fencing_token,
            size_bytes=size_bytes,
            sha256=sha256_hash,
        )

        if not committed_rec:
            FileService._compensate_failed_intent(record, upload_fencing_token, RuntimeError("COMMIT_CAS_REJECTED"))
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Upload lease expired or claimed by cleanup before commit. Storage rolled back.",
            )

        store.save_file_blob(committed_rec.file_id, content)

        # Trigger background document ingestion & indexing pipeline
        try:
            from app.services.ingestion_runner import get_ingestion_runner
            runner = get_ingestion_runner()
            runner.submit_ingestion(
                space_id=committed_rec.space_id,
                file_id=committed_rec.file_id,
                content_bytes=content,
                filename=committed_rec.filename,
                store=store,
            )
            committed_rec = store.get_file(committed_rec.file_id) or committed_rec
        except Exception as ingest_dispatch_err:
            logger.warning(
                f"Failed to dispatch background ingestion for {committed_rec.file_id}: {ingest_dispatch_err}"
            )

        return committed_rec

    @staticmethod
    def get_file_record(space_id: str, file_id: str, user: User) -> FileRecord:
        space = store.get_space(space_id)
        if not space:
            raise HTTPException(status_code=404, detail="Space not found")

        if not store.is_member(space_id, user.uid):
            raise HTTPException(status_code=403, detail="Access denied to space files")

        file_rec = store.get_file(file_id)
        if not file_rec:
            # Resilient fallback: Check if file_id corresponds to an ArtifactDescriptor
            art = store.get_artifact(space_id, file_id)
            if not art and file_id.startswith("file_art_"):
                art = store.get_artifact(space_id, f"art_{file_id[9:]}")
            elif not art and file_id.startswith("file_"):
                art = store.get_artifact(space_id, file_id[5:])
            elif not art and not file_id.startswith("art_"):
                art = store.get_artifact(space_id, f"art_{file_id}")
            if art:
                file_rec = FileRecord(
                    file_id=file_id,
                    space_id=space_id,
                    filename=art.filename,
                    content_type=art.media_type,
                    size_bytes=art.size_bytes,
                    sha256=art.sha256,
                    storage_path=art.storage_path,
                    project_tags=[],
                    uploaded_by=getattr(space, "created_by", None) or user.uid,
                    source_type=FileSourceType.PDX_ARTIFACT,
                    run_id=art.run_id,
                    upload_status="committed",
                    publication_status=art.visibility,
                    ingestion_status=IngestionStatus.READY,
                    active_generation=1,
                )

        if not file_rec or file_rec.space_id != space_id or file_rec.cleanup_pending or file_rec.upload_status != "committed":
            raise HTTPException(status_code=404, detail="File not found")

        # Staging / Approval Gate Visibility Enforcement
        pub_status = getattr(file_rec, "publication_status", "published")
        if pub_status == "pending_approval":
            from app.services.space_service import SpaceService
            context = SpaceService.get_space_context(space_id, user)
            if not context.capabilities.can_approve_runs:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="ARTIFACT_AWAITING_APPROVAL: Deliverable file is pending coordinator approval.",
                )
        elif pub_status == "rejected":
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail="ARTIFACT_REJECTED: Deliverable file was rejected during approval review.",
            )

        return file_rec

    @staticmethod
    def list_files(space_id: str, user: User, project_tag: Optional[str] = None) -> list[FileRecord]:
        space = store.get_space(space_id)
        if not space:
            raise HTTPException(status_code=404, detail="Space not found")

        if not store.is_member(space_id, user.uid):
            raise HTTPException(status_code=403, detail="Access denied to space files")

        files = store.list_files_in_space(space_id, project_tag=project_tag)
        from app.services.space_service import SpaceService
        context = SpaceService.get_space_context(space_id, user)
        can_approve = context.capabilities.can_approve_runs

        if can_approve:
            # Coordinators and above can see staging pending_approval files, but hide rejected
            return [f for f in files if getattr(f, "publication_status", "published") != "rejected"]
        else:
            # General space members can ONLY see published files
            return [f for f in files if getattr(f, "publication_status", "published") == "published"]

    @staticmethod
    def read_file_content(space_id: str, file_id: str, user: User) -> tuple[bytes, FileRecord]:
        record = FileService.get_file_record(space_id, file_id, user)

        # 1. Try memory cache
        cached = store.get_file_blob(file_id)
        if not cached:
            art_id = file_id[9:] if file_id.startswith("file_art_") else (file_id[5:] if file_id.startswith("file_") else file_id)
            cached = store.get_artifact_blob(space_id, f"art_{art_id}" if not art_id.startswith("art_") else art_id, record.filename)
        if cached:
            return cached, record

        # 2. Try disk storage
        if settings.STUDIO_TOWER_ARTIFACT_BACKEND == "local":
            data_dir_abs = os.path.abspath(settings.STUDIO_TOWER_DATA_DIR)
            full_path = os.path.abspath(os.path.join(data_dir_abs, record.storage_path))
            if not full_path.startswith(data_dir_abs):
                raise HTTPException(status_code=400, detail="Invalid storage path")
            if not os.path.exists(full_path):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Binary file '{record.filename}' not found or unlinked from storage.",
                )
            try:
                with open(full_path, "rb") as f:
                    content = f.read()
                    store.save_file_blob(file_id, content)
                    return content, record
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to read local file: {e}",
                )

        # 3. Try GCS cloud storage
        elif settings.STUDIO_TOWER_ARTIFACT_BACKEND == "gcs":
            try:
                from google.cloud import storage as gcs
                client = gcs.Client()
                bucket_name = settings.STUDIO_TOWER_GCS_BUCKET
                if not bucket_name or bucket_name == "agentic-cinema-demo-2026-artifacts":
                    bucket_name = "agentic-cinema-demo-2026-studiotower-artifacts"
                bucket = client.bucket(bucket_name)
                blob = bucket.blob(record.storage_path)

                if not blob.exists():
                    clean_name = FileService._sanitize_filename(record.filename)
                    art_suffix = file_id[9:] if file_id.startswith("file_art_") else (file_id[4:] if file_id.startswith("art_") else file_id)
                    candidates = [
                        f"{space_id}/files/{file_id}_{clean_name}",
                        f"{space_id}/files/{file_id}_{record.filename}",
                        f"{space_id}/files/file_art_{art_suffix}_{clean_name}",
                        f"{space_id}/files/file_{file_id}_{clean_name}",
                        f"{space_id}/artifacts/art_{art_suffix}/{clean_name}",
                        f"{space_id}/artifacts/art_{art_suffix}/{record.filename}",
                        f"{space_id}/artifacts/{file_id}/{clean_name}",
                        record.storage_path.lstrip("/"),
                    ]
                    for candidate in candidates:
                        cand_blob = bucket.blob(candidate)
                        if cand_blob.exists():
                            blob = cand_blob
                            break

                if not blob.exists():
                    # Prefix list search fallback
                    art_suffix = file_id[9:] if file_id.startswith("file_art_") else (file_id[4:] if file_id.startswith("art_") else file_id)
                    blobs = bucket.list_blobs(prefix=f"{space_id}/")
                    for b in blobs:
                        if file_id in b.name or art_suffix in b.name:
                            blob = b
                            break

                if blob.exists():
                    content = blob.download_as_bytes()
                    store.save_file_blob(file_id, content)
                    return content, record
            except Exception as gcs_read_err:
                logger.warning("GCS read failed for file %s (%s). Attempting local fallback.", record.file_id, gcs_read_err)

        # Local disk fallback if GCS fails or file was stored locally
        data_dir_abs = os.path.abspath(settings.STUDIO_TOWER_DATA_DIR)
        full_path = os.path.abspath(os.path.join(data_dir_abs, record.storage_path))
        if full_path.startswith(data_dir_abs) and os.path.exists(full_path):
            with open(full_path, "rb") as f:
                content = f.read()
                store.save_file_blob(file_id, content)
                return content, record

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Binary file '{record.filename}' not found in storage.",
        )

    @staticmethod
    def copy_file_to_space(
        source_space_id: str,
        file_id: str,
        target_space_id: str,
        user: User,
        target_project_tags: Optional[list[str]] = None,
    ) -> FileRecord:
        # 1. Authorization: User must be member of BOTH source and target
        source_rec = FileService.get_file_record(source_space_id, file_id, user)
        target_space = store.get_space(target_space_id)
        if not target_space:
            raise HTTPException(status_code=404, detail="Target space not found")

        if not store.is_member(target_space_id, user.uid):
            raise HTTPException(status_code=403, detail="Access denied: you must be a member of the target space")

        if target_space.kind == SpaceKind.AGENT_DM and target_space.created_by != user.uid:
            raise HTTPException(status_code=403, detail="Cannot share into private agent DM of another user")

        # 2. Read source binary content
        content, _ = FileService.read_file_content(source_space_id, file_id, user)

        # 3. Create target FileRecord with distinct physical object path
        new_file_id = f"file_{uuid.uuid4().hex[:12]}"
        clean_name = FileService._sanitize_filename(source_rec.filename)
        target_storage_rel_path = f"{target_space_id}/files/{new_file_id}_{clean_name}"
        upload_fencing_token = uuid.uuid4().hex
        now = datetime.now(UTC)

        tags = target_project_tags or source_rec.project_tags
        target_rec = FileRecord(
            file_id=new_file_id,
            space_id=target_space_id,
            filename=clean_name,
            content_type=source_rec.content_type,
            size_bytes=source_rec.size_bytes,
            sha256=source_rec.sha256,
            storage_path=target_storage_rel_path,
            project_tags=tags,
            uploaded_by=user.uid,
            source_type=source_rec.source_type,
            copied_from_space_id=source_space_id,
            copied_from_file_id=source_rec.file_id,
            upload_status="pending_upload",
            upload_fencing_token=upload_fencing_token,
            upload_lease_until=now + timedelta(seconds=60),
            cleanup_pending=False,
            cleanup_status="pending",
        )

        # 2PC Phase 1: Intent Persistence
        store.save_file(target_rec)

        # 2PC Phase 2: Binary Copy with Heartbeat Lease Renewal
        try:
            with upload_heartbeat(target_rec.file_id, upload_fencing_token, interval_seconds=5.0, extension_seconds=30) as hb_tracker:
                if settings.STUDIO_TOWER_ARTIFACT_BACKEND == "local":
                    data_dir_abs = os.path.abspath(settings.STUDIO_TOWER_DATA_DIR)
                    target_full_path = os.path.abspath(os.path.join(data_dir_abs, target_storage_rel_path))
                    if not target_full_path.startswith(data_dir_abs):
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Invalid copy target storage path",
                        )
                    os.makedirs(os.path.dirname(target_full_path), exist_ok=True)
                    with open(target_full_path, "wb") as f:
                        f.write(content)
                elif settings.STUDIO_TOWER_ARTIFACT_BACKEND == "gcs":
                    from google.cloud import storage as gcs
                    client = gcs.Client()
                    bucket = client.bucket(settings.STUDIO_TOWER_GCS_BUCKET)
                    blob = bucket.blob(target_storage_rel_path)
                    blob.upload_from_string(content, content_type=source_rec.content_type or "application/octet-stream")

            # Post-Heartbeat Stop Check (verified after thread completely joined)
            if not hb_tracker.is_healthy:
                logger.warning("Copy heartbeat failed for file %s: %s", target_rec.file_id, hb_tracker.last_error)
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Copy lease could not be maintained. Copy aborted for data safety. Please retry.",
                )
        except HTTPException as http_err:
            FileService._compensate_failed_intent(target_rec, upload_fencing_token, http_err)
            raise
        except Exception as copy_err:
            FileService._compensate_failed_intent(target_rec, upload_fencing_token, copy_err)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Cloud storage copy failed. Request logged for diagnosis.",
            )

        # 2PC Phase 3: Atomic Final Commit
        committed_rec = store.commit_uploaded_file(
            file_id=target_rec.file_id,
            upload_fencing_token=upload_fencing_token,
            size_bytes=source_rec.size_bytes,
            sha256=source_rec.sha256,
        )

        if not committed_rec:
            FileService._compensate_failed_intent(target_rec, upload_fencing_token, RuntimeError("COMMIT_CAS_REJECTED"))
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Copy lease expired or claimed by cleanup before commit. Storage rolled back.",
            )

        store.save_file_blob(committed_rec.file_id, content)
        return committed_rec

    @staticmethod
    def search_accessible_files(query: str, user: User) -> list[FileRecord]:
        accessible_spaces = store.list_spaces_for_user(user.uid)
        accessible_space_ids = [s.space_id for s in accessible_spaces]
        return store.search_accessible_files(accessible_space_ids, query)

    @staticmethod
    def delete_file_permanently(file_rec: FileRecord) -> None:
        """
        Permanently remove a file across disk/GCS and persistent database storage.
        If physical deletion fails, metadata is preserved with cleanup_pending=True to prevent orphan blobs.
        """
        # 1. Local disk cleanup
        if settings.STUDIO_TOWER_ARTIFACT_BACKEND == "local":
            try:
                data_dir_abs = os.path.abspath(settings.STUDIO_TOWER_DATA_DIR)
                full_path = os.path.abspath(os.path.join(data_dir_abs, file_rec.storage_path))
                if os.path.exists(full_path):
                    os.remove(full_path)
            except Exception as e:
                file_rec.cleanup_pending = True
                file_rec.cleanup_last_error = f"LOCAL_FS_IO_ERROR: {type(e).__name__}"
                store.save_file(file_rec)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Local storage deletion failed. File has been queued for background cleanup retry.",
                )
        # 2. GCS bucket cleanup
        elif settings.STUDIO_TOWER_ARTIFACT_BACKEND == "gcs":
            try:
                from google.cloud import storage as gcs
                client = gcs.Client()
                bucket = client.bucket(settings.STUDIO_TOWER_GCS_BUCKET)
                blob = bucket.blob(file_rec.storage_path)
                if blob.exists():
                    blob.delete()
            except Exception as e:
                file_rec.cleanup_pending = True
                file_rec.cleanup_last_error = f"GCS_NETWORK_ERROR: {type(e).__name__}"
                store.save_file(file_rec)
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Cloud storage deletion failed. File has been queued for background cleanup retry.",
                )

        # 3. Persistent database and cache cleanup only after physical deletion confirmed
        store.delete_file(file_rec.file_id)

    @staticmethod
    def retry_pending_cleanups() -> dict:
        """
        Background maintenance job to scan and permanently remove pending orphaned files
        with atomic lease claiming, pre-deletion fencing state, retry ceilings, and exponential backoff.
        """
        all_pending = store.list_cleanup_pending_files()
        claimed_files: list[FileRecord] = []

        # Attempt atomic claim for each pending candidate
        for p in all_pending:
            claimed = store.claim_cleanup_file(p.file_id, lease_duration_seconds=60)
            if claimed:
                claimed_files.append(claimed)

        success_count = 0
        failed_count = 0

        for f in claimed_files:
            # Step 1: Pre-deletion fencing state transition BEFORE irreversible physical deletion
            marked = store.mark_cleanup_file_deleting(f.file_id, f.lease_token or "")
            if not marked:
                # Lease expired or reclaimed by another worker; abort without touching storage!
                failed_count += 1
                continue

            try:
                # Step 2: Direct physical deletion attempt (idempotent if already deleted)
                if settings.STUDIO_TOWER_ARTIFACT_BACKEND == "local":
                    data_dir_abs = os.path.abspath(settings.STUDIO_TOWER_DATA_DIR)
                    full_path = os.path.abspath(os.path.join(data_dir_abs, f.storage_path))
                    if os.path.exists(full_path):
                        os.remove(full_path)
                elif settings.STUDIO_TOWER_ARTIFACT_BACKEND == "gcs":
                    from google.cloud import storage as gcs
                    client = gcs.Client()
                    bucket = client.bucket(settings.STUDIO_TOWER_GCS_BUCKET)
                    blob = bucket.blob(f.storage_path)
                    if blob.exists():
                        blob.delete()

                # Step 3: Atomically delete metadata checking active lease token
                deleted = store.delete_cleanup_file_with_lease(f.file_id, f.lease_token or "")
                if deleted:
                    success_count += 1
                else:
                    # Lease expired during physical delete
                    failed_count += 1
            except Exception as e:
                is_terminal = (f.cleanup_retries + 1) >= f.max_cleanup_retries
                backoff_sec = 2 ** (f.cleanup_retries + 1) * 5
                next_retry = datetime.now(UTC) + timedelta(seconds=backoff_sec)
                last_err = f"RETRY_FAILED: {type(e).__name__}"

                store.release_cleanup_file_with_lease(
                    file_id=f.file_id,
                    lease_token=f.lease_token or "",
                    next_retry_at=next_retry,
                    last_error=last_err,
                    is_terminal=is_terminal,
                )
                failed_count += 1

        # Also sweep any pending generation chunk cleanups
        gen_sweep = {"scanned": 0, "claimed": 0, "cleaned": 0}
        if hasattr(store, "sweep_pending_generation_cleanups"):
            try:
                gen_sweep = store.sweep_pending_generation_cleanups(limit=20)
            except Exception as e:
                logger.error(f"sweep_pending_generation_cleanups failed: {e}")

        return {
            "scanned": len(all_pending),
            "claimed_and_processed": len(claimed_files),
            "cleaned": success_count,
            "still_pending": failed_count,
            "generation_cleanups": gen_sweep,
        }


def check_artifact_storage_readiness() -> dict:
    from app.core.config import settings
    backend = settings.STUDIO_TOWER_ARTIFACT_BACKEND
    try:
        if backend == "gcs":
            bucket_name = settings.STUDIO_TOWER_GCS_BUCKET
            if not bucket_name:
                return {
                    "status": "degraded",
                    "backend": "gcs",
                    "error": "STUDIO_TOWER_GCS_BUCKET is not configured",
                }
            if bucket_name == "agentic-cinema-demo-2026-artifacts":
                bucket_name = "agentic-cinema-demo-2026-studiotower-artifacts"
            try:
                import importlib
                import sys
                gcs_storage = sys.modules.get("google.cloud.storage")
                if gcs_storage is None:
                    gcs_storage = importlib.import_module("google.cloud.storage")
                client = gcs_storage.Client(project=settings.STUDIO_TOWER_FIREBASE_PROJECT_ID or None)
                bucket = client.bucket(bucket_name)

                # Active object write/read/delete probe to verify full GCS permissions
                probe_blob_name = f"_healthz_probes/probe_{uuid.uuid4().hex[:8]}.tmp"
                blob = bucket.blob(probe_blob_name)
                delete_succeeded = False
                try:
                    blob.upload_from_string(b"probe_ok", content_type="text/plain", timeout=2.0)
                    downloaded = blob.download_as_bytes(timeout=2.0)
                    if downloaded != b"probe_ok":
                        return {
                            "status": "degraded",
                            "backend": "gcs",
                            "bucket": bucket_name,
                            "error": "GCS object probe content mismatch",
                        }
                    blob.delete(timeout=2.0)
                    delete_succeeded = True
                except Exception as op_err:
                    logger.error(f"GCS probe operation failed on '{probe_blob_name}': {op_err}")
                    return {
                        "status": "degraded",
                        "backend": "gcs",
                        "bucket": bucket_name,
                        "error": "GCS object write/read/delete permission denied or unreachable",
                    }
                finally:
                    if not delete_succeeded:
                        try:
                            blob.delete(timeout=2.0)
                        except Exception as cleanup_err:
                            logger.warning(f"GCS orphan probe object cleanup failed for '{probe_blob_name}': {cleanup_err}")
                            try:
                                from app.services.storage import store
                                orphan_rec = FileRecord(
                                    file_id=f"gcs_probe_orphan_{uuid.uuid4().hex[:8]}",
                                    space_id="_system",
                                    filename=os.path.basename(probe_blob_name),
                                    storage_path=probe_blob_name,
                                    uploaded_by="system_healthz",
                                    source_type="pdx_artifact",
                                    size_bytes=8,
                                    content_type="text/plain",
                                    upload_status="failed",
                                    cleanup_pending=True,
                                    cleanup_status="pending",
                                    cleanup_retries=0,
                                    cleanup_next_retry_at=datetime.now(UTC),
                                )
                                store.save_file(orphan_rec)
                            except Exception as track_err:
                                logger.error(f"Failed to persist GCS orphan tracking record: {track_err}")

                return {
                    "status": "ok",
                    "backend": "gcs",
                    "bucket": bucket_name,
                }
            except Exception as e:
                logger.error("GCS bucket readiness check failed: %s", e)
                return {
                    "status": "degraded",
                    "backend": "gcs",
                    "bucket": bucket_name,
                    "error": "GCS object write/read/delete permission denied or unreachable",
                }
        else:
            # Local filesystem probe: actually write, read, and delete a small probe file to confirm RW
            data_dir = settings.STUDIO_TOWER_DATA_DIR
            os.makedirs(data_dir, exist_ok=True)
            probe_path = os.path.join(data_dir, f".healthz_probe_{uuid.uuid4().hex[:8]}.tmp")
            try:
                with open(probe_path, "w", encoding="utf-8") as f:
                    f.write("probe_ok")
                with open(probe_path, "r", encoding="utf-8") as f:
                    content = f.read()
                if content != "probe_ok":
                    return {"status": "degraded", "backend": "local", "error": "Local storage read mismatch"}
            finally:
                if os.path.exists(probe_path):
                    try:
                        os.remove(probe_path)
                    except Exception:
                        pass
            return {"status": "ok", "backend": "local", "storage_dir": data_dir}
    except Exception as e:
        logger.error("Artifact storage readiness check failed: %s", e)
        return {"status": "degraded", "backend": backend, "error": "Artifact storage inaccessible"}

