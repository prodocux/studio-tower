from typing import Literal
from pydantic import BaseModel, ConfigDict
from app.models.space import MembershipRole


class InvitePreviewResponse(BaseModel):
    """Public preview model for an active space invitation.

    Explicitly omits space_id and raw inviter identity to prevent tenant
    enumeration, while providing the user with space name, role, and masked email.
    """
    model_config = ConfigDict(extra="forbid")

    space_name: str
    role: MembershipRole
    target_email_masked: str | None = None
    status: Literal["active"] = "active"
