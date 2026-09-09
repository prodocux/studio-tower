import uuid
from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field


class MessageRole(str, Enum):
    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"


class Citation(BaseModel):
    citation_id: str = Field(default_factory=lambda: f"cit_{uuid.uuid4().hex[:10]}", description="Unique citation identifier")
    index: int = Field(description="1-based citation index corresponding to [1], [2] in markdown")
    file_id: str
    filename: str
    generation: int = Field(description="Fixed chunk generation at retrieval time")
    content_hash: str
    chunk_id: str
    source_locator: str = Field(description="e.g. 'page:3'")
    page_number: int | None = None
    char_start: int = 0
    char_end: int = 0
    snippet: str = ""
    score: float = 0.0


from app.models.action_proposal import ActionProposal


class Message(BaseModel):
    message_id: str = Field(default_factory=lambda: f"msg_{uuid.uuid4().hex[:12]}")
    space_id: str
    sender_uid: str
    sender_name: str = ""
    role: MessageRole = MessageRole.USER
    content: str
    project_tag: str | None = Field(default="general", description="Associated project tag slug")
    attachment_file_ids: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list, description="Authoritative backend-verified citations")
    client_message_id: str | None = Field(default=None, description="Client-side idempotency tracking key")
    proposed_action: ActionProposal | None = Field(default=None, description="Structured action proposal requiring user confirmation")
    run_id: str | None = Field(default=None, description="Associated execution run ID if triggered")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
