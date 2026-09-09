import uuid
from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field


class FileSourceType(str, Enum):
    USER_UPLOAD = "user_upload"
    PRODOCUX_EXTRACT = "prodocux_extract"
    PDX_ARTIFACT = "pdx_artifact"
    MANIFEST = "manifest"


class IngestionStatus(str, Enum):
    PENDING = "pending"
    EXTRACTING = "extracting"
    INDEXING = "indexing"
    READY = "ready"
    READY_PARTIAL = "ready_partial"
    NEEDS_OCR = "needs_ocr"
    FAILED = "failed"


class DocumentChunk(BaseModel):
    chunk_id: str
    file_id: str
    space_id: str
    ingestion_version: int
    ordinal: int  # 0-indexed sequence for reading order
    page_number: int | None = None
    section_heading: str | None = None
    source_locator: str  # e.g. "page:3", "scene:12:EXT. OCEAN", "table:1:rows:1-5"
    raw_text: str
    normalized_text: str
    char_start: int  # relative to concatenated normalized document stream
    char_end: int
    token_count: int
    content_hash: str  # sha256 of normalized_text
    contains_formula_like_content: bool = False
    extraction_method: str
    extractor_version: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class DocumentChunkSummary(BaseModel):
    chunk_id: str
    file_id: str
    space_id: str
    ingestion_version: int
    ordinal: int
    page_number: int | None = None
    section_heading: str | None = None
    source_locator: str
    normalized_text: str
    char_start: int
    char_end: int
    token_count: int
    content_hash: str
    contains_formula_like_content: bool = False
    extraction_method: str
    extractor_version: str


class DocumentChunksResponse(BaseModel):
    items: list[DocumentChunkSummary]
    cursor: int
    limit: int
    total: int
    has_more: bool
    next_cursor: int | None = None
    active_generation: int


class FileIngestionStatusResponse(BaseModel):
    file_id: str
    space_id: str
    ingestion_status: IngestionStatus
    active_generation: int
    ingestion_job_id: str | None = None
    ingestion_version: int = 1
    chunk_count: int = 0
    extracted_pages: int = 0
    ocr_gap_pages: list[int] = Field(default_factory=list)
    has_ocr_gaps: bool = False
    error_code: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class FileRecord(BaseModel):
    file_id: str = Field(default_factory=lambda: f"file_{uuid.uuid4().hex[:12]}")
    space_id: str
    filename: str
    content_type: str = "application/octet-stream"
    size_bytes: int = 0
    sha256: str = ""
    storage_path: str = ""
    project_tags: list[str] = Field(default_factory=lambda: ["general"])
    uploaded_by: str
    source_type: FileSourceType = FileSourceType.USER_UPLOAD
    run_id: str | None = None
    copied_from_space_id: str | None = None
    copied_from_file_id: str | None = None
    upload_status: str = "committed"  # "pending_upload", "committed", "failed"
    publication_status: str = "published"  # "published", "pending_approval", "rejected"
    upload_fencing_token: str | None = None
    upload_lease_until: datetime | None = None
    cleanup_pending: bool = False
    cleanup_status: str = "pending"  # "pending", "in_progress", "deleting", "failed"
    cleanup_retries: int = 0
    max_cleanup_retries: int = 5
    cleanup_next_retry_at: datetime | None = None
    cleanup_lease_until: datetime | None = None
    lease_token: str | None = None
    lease_version: int = 0
    cleanup_last_error: str | None = None
    # Ingestion Lifecycle & Generation Fencing fields
    ingestion_status: IngestionStatus = IngestionStatus.PENDING
    active_generation: int = 0
    max_allocated_generation: int = 0
    ingestion_job_id: str | None = None
    ingestion_version: int = 1
    ingestion_lease_owner: str | None = None
    ingestion_lease_until: datetime | None = None
    extractor_version: str = "v1.0"
    chunk_count: int = 0
    extracted_pages: int = 0
    ocr_gap_pages: list[int] = Field(default_factory=list)
    has_ocr_gaps: bool = False
    ingestion_error_code: str | None = None
    ingestion_error_message: str | None = None
    cleaning_generations: list[int] = Field(default_factory=list)
    deleted_generations: list[int] = Field(default_factory=list)
    committed_generations: list[int] = Field(default_factory=list)
    ingestion_started_at: datetime | None = None
    ingestion_completed_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
