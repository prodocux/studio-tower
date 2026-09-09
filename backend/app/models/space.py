import uuid
from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field


class SpaceKind(str, Enum):
    AGENT_DM = "agent_dm"
    SHARED_SPACE = "space"


class MembershipRole(str, Enum):
    OWNER = "owner"
    ADMIN = "admin"
    COORDINATOR = "coordinator"
    MEMBER = "member"


class ProjectTag(BaseModel):
    id: str = Field(default_factory=lambda: f"tag_{uuid.uuid4().hex[:8]}")
    name: str = Field(..., description="Human readable tag name, e.g. 'Block A - Disaster Unit'")
    slug: str = Field(..., description="Normalized slug/tag label, e.g. 'block-a'")
    color: str = Field(default="#3B82F6", description="Hex color code for UI badge")
    description: str = Field(default="", description="Optional description of this project track")
    archived: bool = Field(default=False, description="Whether this tag is archived")
    revision: int = Field(default=1, description="Monotonic revision counter")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Space(BaseModel):
    space_id: str = Field(default_factory=lambda: f"space_{uuid.uuid4().hex[:12]}")
    name: str = Field(..., description="Name of the space")
    kind: SpaceKind = Field(default=SpaceKind.SHARED_SPACE)
    created_by: str = Field(..., description="UID of creator")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    tags: list[ProjectTag] = Field(
        default_factory=lambda: [
            ProjectTag(id="tag_general", name="General", slug="general", color="#64748B", description="Default general stream")
        ]
    )
    # Server-managed sandbox lifecycle metadata (excluded from public API serialization)
    is_sandbox: bool = Field(default=False, exclude=True)
    sandbox_expires_at: datetime | None = Field(default=None, exclude=True)
    cleanup_status: str | None = Field(default=None, exclude=True)

    def to_storage_dict(self) -> dict:
        """Serializes space for internal storage persistence including server metadata."""
        d = self.model_dump(mode="json")
        d["is_sandbox"] = self.is_sandbox
        d["sandbox_expires_at"] = self.sandbox_expires_at.isoformat() if self.sandbox_expires_at else None
        d["cleanup_status"] = self.cleanup_status
        return d


class Capabilities(BaseModel):
    can_invite: bool
    can_manage_members: bool
    can_change_role: bool
    can_remove_member: bool
    can_transfer_ownership: bool
    can_approve_runs: bool
    can_manage_tags: bool
    can_leave_space: bool
    can_view_telemetry: bool = True
    can_diagnose_runs: bool = True


class SpaceContext(BaseModel):
    space: Space
    current_user_role: MembershipRole
    member_count: int
    capabilities: Capabilities


class MemberInfo(BaseModel):
    uid: str
    display_name: str = ""
    email: str = ""
    role: MembershipRole
    joined_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Membership(BaseModel):
    space_id: str
    uid: str
    role: MembershipRole = Field(default=MembershipRole.MEMBER)
    joined_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Invite(BaseModel):
    token: str = Field(default_factory=lambda: uuid.uuid4().hex)
    space_id: str
    created_by: str
    role: MembershipRole = Field(default=MembershipRole.MEMBER)
    target_email: str | None = Field(default=None, description="Optional restricted recipient email")
    max_uses: int = Field(default=1, ge=1, description="Maximum number of times this invite can be accepted")
    used_count: int = Field(default=0, ge=0, description="Number of times this invite has been accepted")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
