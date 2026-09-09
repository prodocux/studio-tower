import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator


class InvalidCursorError(ValueError):
    """Raised when an activity or message cursor is malformed, tampered, expired, or mismatched."""
    pass


class ActivityEventType(str, Enum):
    # Files
    FILE_UPLOADED = "file.uploaded"
    FILE_INGESTION_READY = "file.ingestion_ready"
    FILE_INGESTION_PARTIAL = "file.ingestion_partial_ocr"
    FILE_NEEDS_OCR = "file.needs_ocr"
    FILE_INGESTION_FAILED = "file.ingestion_failed"
    FILE_REINDEX_TRIGGERED = "file.reindex_triggered"
    # Tags
    TAG_CREATED = "tag.created"
    TAG_UPDATED = "tag.updated"
    TAG_ARCHIVED = "tag.archived"
    TAG_UNARCHIVED = "tag.unarchived"
    # Messages
    MESSAGE_CREATED = "message.created"
    # Runs
    RUN_STARTED = "run.started"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    # Approval Gates
    GATE_REQUESTED = "gate.requested"
    GATE_APPROVED = "gate.approved"
    GATE_REJECTED = "gate.rejected"


class ActivityEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: f"evt_{uuid.uuid4().hex[:12]}")
    event_type: ActivityEventType
    space_id: str
    project_tag: Optional[str] = "general"
    project_tags: List[str] = Field(default_factory=lambda: ["general"])
    resource_type: str = "file"  # "file", "tag", "message", "run", "gate"
    resource_id: str
    summary: str
    details: Dict[str, Any] = Field(default_factory=dict)
    actor_uid: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="before")
    @classmethod
    def normalize_tags(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        raw_tags = data.get("project_tags")
        raw_tag = data.get("project_tag")

        tags: List[str] = []
        if raw_tags and isinstance(raw_tags, list):
            tags = [str(t).strip() for t in raw_tags if str(t).strip() and str(t).strip() != "all"]
        elif raw_tag and isinstance(raw_tag, str) and raw_tag.strip() and raw_tag.strip() != "all":
            tags = [raw_tag.strip()]

        if not tags:
            tags = ["general"]

        data["project_tags"] = tags
        data["project_tag"] = tags[0]
        return data


class OutboxStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    PUBLISHED = "published"
    FAILED = "failed"


class ActivityOutboxItem(BaseModel):
    outbox_id: str = Field(default_factory=lambda: f"outbox_{uuid.uuid4().hex[:16]}")
    event_id: str
    space_id: str
    event: ActivityEvent
    status: OutboxStatus = OutboxStatus.PENDING
    attempts: int = 0
    max_attempts: int = 5
    last_error: Optional[str] = None
    lease_owner: Optional[str] = None
    lease_token: Optional[str] = None
    lease_until: Optional[datetime] = None
    next_retry_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
