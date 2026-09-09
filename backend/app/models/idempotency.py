import hashlib
from datetime import UTC, datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class ChatIdempotencyStatus(str, Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class ChatIdempotencyRecord(BaseModel):
    key: str
    space_id: str
    sender_uid: str
    client_message_id: str
    payload_hash: str
    status: ChatIdempotencyStatus = ChatIdempotencyStatus.IN_PROGRESS
    user_message_id: Optional[str] = None
    agent_message_id: Optional[str] = None
    run_id: Optional[str] = None
    error_status_code: Optional[int] = None
    error_detail: Optional[str] = None
    version: int = 1
    lease_owner: Optional[str] = None
    lease_until: Optional[datetime] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @staticmethod
    def compute_key(space_id: str, sender_uid: str, client_message_id: str) -> str:
        import json

        canonical_obj = {
            "client_message_id": client_message_id,
            "sender_uid": sender_uid,
            "space_id": space_id,
        }
        raw_bytes = json.dumps(canonical_obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw_bytes).hexdigest()

    @staticmethod
    def compute_payload_hash(
        content: str,
        project_tag: Optional[str],
        attachment_file_ids: Optional[List[str]],
        intent: Optional[str] = None,
        context_run_id: Optional[str] = None,
    ) -> str:
        import json

        canonical_obj = {
            "attachment_file_ids": sorted(attachment_file_ids or []),
            "content": content,
            "context_run_id": context_run_id or None,
            "intent": intent or None,
            "project_tag": project_tag or "general",
        }
        raw_bytes = json.dumps(canonical_obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw_bytes).hexdigest()

    @staticmethod
    def compute_deterministic_run_id(idemp_key: str) -> str:
        digest = hashlib.sha256((idemp_key + ":run").encode("utf-8")).hexdigest()[:12]
        return f"run_{digest}"

    @staticmethod
    def compute_deterministic_message_id(idemp_key: str, role: str) -> str:
        digest = hashlib.sha256((idemp_key + ":" + role).encode("utf-8")).hexdigest()[:12]
        return f"msg_{digest}"

