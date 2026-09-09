import hashlib
from datetime import datetime

from app.core.auth import get_current_user
from app.core.config import settings
from app.models.invite_preview import InvitePreviewResponse
from app.models.message import Message
from app.models.space import (
    Invite,
    MemberInfo,
    MembershipRole,
    ProjectTag,
    Space,
    SpaceContext,
)
from app.models.user import User
from app.services.lineage_service import LineageService
from app.services.message_service import MessageService
from app.services.space_service import SpaceService
from app.services.storage import store
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field

router = APIRouter(prefix="/v1", tags=["spaces"])


# Request Schemas
class CreateSpaceRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    tags: list[ProjectTag] | None = None


class CreateTagRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    slug: str = Field(..., min_length=1, max_length=50)
    color: str = Field(default="#3B82F6")
    description: str = Field(default="")


class UpdateTagRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=50)
    color: str | None = None
    description: str | None = None


class ArchiveTagRequest(BaseModel):
    archived: bool = True


class CreateInviteRequest(BaseModel):
    role: MembershipRole = MembershipRole.MEMBER
    target_email: str | None = None
    max_uses: int | None = None
    expires_at: datetime | None = None


class UpdateMemberRoleRequest(BaseModel):
    role: MembershipRole


class DirectAddMemberRequest(BaseModel):
    email_or_uid: str = Field(..., min_length=1)
    role: MembershipRole = MembershipRole.MEMBER


class TransferOwnershipRequest(BaseModel):
    new_owner_uid: str = Field(..., min_length=1)


class PostMessageRequest(BaseModel):
    content: str = Field(..., min_length=1)
    project_tag: str | None = "general"
    attachment_file_ids: list[str] | None = None


# Endpoints
@router.get("/spaces", response_model=list[Space])
def list_spaces(current_user: User = Depends(get_current_user)):
    """List all Spaces accessible to the authenticated user."""
    return SpaceService.list_accessible_spaces(current_user)


@router.post("/spaces", response_model=Space)
def create_space(
    payload: CreateSpaceRequest,
    current_user: User = Depends(get_current_user),
):
    """Create a new Shared Space."""
    return SpaceService.create_shared_space(payload.name, current_user, payload.tags)


@router.get("/spaces/{space_id}", response_model=Space)
def get_space(
    space_id: str,
    current_user: User = Depends(get_current_user),
):
    """Get single Space details (requires membership)."""
    return SpaceService.get_space_with_auth(space_id, current_user)


@router.get("/spaces/{space_id}/context", response_model=SpaceContext)
def get_space_context(
    space_id: str,
    current_user: User = Depends(get_current_user),
):
    """Get Space runtime context, active role, member count, and dynamic capabilities."""
    return SpaceService.get_space_context(space_id, current_user)


@router.get("/spaces/{space_id}/members", response_model=list[MemberInfo])
def list_space_members(
    space_id: str,
    current_user: User = Depends(get_current_user),
):
    """List all members and their assigned roles within a Space."""
    return SpaceService.list_space_members(space_id, current_user)


@router.post("/spaces/{space_id}/members/direct-add", response_model=MemberInfo)
def direct_add_space_member(
    space_id: str,
    payload: DirectAddMemberRequest,
    current_user: User = Depends(get_current_user),
):
    """Directly add an existing site user to a Space by email or UID."""
    return SpaceService.direct_add_member(
        space_id=space_id,
        email_or_uid=payload.email_or_uid,
        role=payload.role,
        actor_user=current_user,
    )


@router.patch("/spaces/{space_id}/members/{uid}/role")
def update_member_role(
    space_id: str,
    uid: str,
    payload: UpdateMemberRoleRequest,
    current_user: User = Depends(get_current_user),
):
    """Update role of an existing Space member."""
    return SpaceService.update_member_role(space_id, uid, payload.role, current_user)


@router.delete("/spaces/{space_id}/members/{uid}")
def remove_member(
    space_id: str,
    uid: str,
    current_user: User = Depends(get_current_user),
):
    """Remove a member from a Space."""
    return SpaceService.remove_space_member(space_id, uid, current_user)


@router.post("/spaces/{space_id}/transfer-ownership")
def transfer_ownership(
    space_id: str,
    payload: TransferOwnershipRequest,
    current_user: User = Depends(get_current_user),
):
    """Transfer Space ownership to another existing member."""
    return SpaceService.transfer_ownership(space_id, payload.new_owner_uid, current_user)


@router.post("/spaces/{space_id}/tags", response_model=Space)
def add_project_tag(
    space_id: str,
    payload: CreateTagRequest,
    current_user: User = Depends(get_current_user),
):
    """Add or update a project tag within a Space."""
    tag = ProjectTag(
        name=payload.name,
        slug=payload.slug,
        color=payload.color,
        description=payload.description,
    )
    return SpaceService.add_tag_to_space(space_id, tag, current_user)


@router.put("/spaces/{space_id}/tags/{tag_id}", response_model=Space)
def update_project_tag(
    space_id: str,
    tag_id: str,
    payload: UpdateTagRequest,
    current_user: User = Depends(get_current_user),
):
    """Update name, color, or description of an existing project tag (slug is immutable)."""
    return SpaceService.update_tag_in_space(
        space_id=space_id,
        tag_id_or_slug=tag_id,
        name=payload.name,
        color=payload.color,
        description=payload.description,
        user=current_user,
    )


@router.post("/spaces/{space_id}/tags/{tag_id}/archive", response_model=Space)
def archive_project_tag(
    space_id: str,
    tag_id: str,
    payload: ArchiveTagRequest,
    current_user: User = Depends(get_current_user),
):
    """Archive or unarchive a project tag (soft-delete while preserving historical runs/files/citations)."""
    return SpaceService.archive_tag_in_space(
        space_id=space_id,
        tag_id_or_slug=tag_id,
        archived=payload.archived,
        user=current_user,
    )


@router.get("/spaces/{space_id}/invites", response_model=list[Invite])
def list_invites(
    space_id: str,
    current_user: User = Depends(get_current_user),
):
    """List active invitation links for a Space."""
    return SpaceService.list_invites_for_space(space_id, current_user)


@router.post("/spaces/{space_id}/invites", response_model=Invite)
def create_invite(
    space_id: str,
    payload: CreateInviteRequest,
    current_user: User = Depends(get_current_user),
):
    """Generate an invitation link/token to join a shared Space."""
    return SpaceService.create_invite(
        space_id=space_id,
        user=current_user,
        role=payload.role,
        target_email=payload.target_email,
        max_uses=payload.max_uses,
        expires_at=payload.expires_at,
    )


def _extract_client_ip(request: Request) -> str:
    """Extract verified client IP.

    If TRUST_PROXY_HEADERS is False, direct caller host is strictly returned (request.client.host).
    Only when TRUST_PROXY_HEADERS is explicitly True (Cloud Run / Load Balancer ingress),
    the rightmost IP in X-Forwarded-For (appended by Google Front End) is parsed and validated.
    """
    import ipaddress

    if not settings.TRUST_PROXY_HEADERS:
        if request.client and request.client.host:
            return request.client.host
        return "127.0.0.1"

    xff = request.headers.get("x-forwarded-for")
    if xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            candidate = parts[-1]
            try:
                ipaddress.ip_address(candidate)
                return candidate
            except ValueError:
                pass
    if request.client and request.client.host:
        return request.client.host
    return "unknown_ip"


@router.get("/invites/{token}/preview", response_model=InvitePreviewResponse)
def get_invite_preview(
    token: str,
    request: Request,
    response: Response,
):
    """Public preview endpoint for an invitation.

    Returns space name, role, and masked target email for active invites.
    Does NOT require authentication, enabling unauthenticated invite landing pages.
    Sets 'Cache-Control: no-store, private' header to prevent caching.
    Protected by dual sliding-window rate limiters:
    1. IP-only bucket: 30 req/min per client IP (prevents token spray attacks).
    2. Token-only bucket: 10 req/min per token (prevents single-token enumeration).
    """
    response.headers["Cache-Control"] = "no-store, private"
    client_ip = _extract_client_ip(request)
    ip_hash = hashlib.sha256(client_ip.encode()).hexdigest()[:16]
    tok_hash = hashlib.sha256(token.encode()).hexdigest()[:16]

    # Bucket 1: IP-only rate limit (30 req/min)
    ip_rate_key = f"invite_prev_ip_{ip_hash}"
    allowed_ip, retry_after_ip = store.check_and_record_rate_limit(ip_rate_key, limit=30, window_seconds=60.0)
    if not allowed_ip:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "RATE_LIMIT_EXCEEDED", "message": "Too many preview requests from this network. Please wait."},
            headers={"Retry-After": str(retry_after_ip)},
        )

    # Bucket 2: Token-only rate limit (10 req/min)
    tok_rate_key = f"invite_prev_tok_{tok_hash}"
    allowed_tok, retry_after_tok = store.check_and_record_rate_limit(tok_rate_key, limit=10, window_seconds=60.0)
    if not allowed_tok:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "RATE_LIMIT_EXCEEDED", "message": "Too many preview requests for this invitation. Please try again later."},
            headers={"Retry-After": str(retry_after_tok)},
        )

    return SpaceService.get_invite_preview(token)


@router.post("/invites/{token}/accept", response_model=Space)
def accept_invite(
    token: str,
    current_user: User = Depends(get_current_user),
):
    """Accept an invite and join the corresponding Space."""
    return SpaceService.accept_invite(token, current_user)


@router.post("/invites/{token}/revoke", response_model=Invite)
def revoke_invite(
    token: str,
    current_user: User = Depends(get_current_user),
):
    """Revoke an active invite link."""
    return SpaceService.revoke_invite(token, current_user)


@router.post("/spaces/{space_id}/leave")
def leave_space(
    space_id: str,
    current_user: User = Depends(get_current_user),
):
    """Leave a shared Space."""
    success = SpaceService.leave_space(space_id, current_user)
    return {"status": "left", "space_id": space_id, "success": success}


@router.get("/spaces/{space_id}/lineage")
def get_space_lineage(
    space_id: str,
    tag: str | None = Query(default=None, description="Optional project tag filter"),
    current_user: User = Depends(get_current_user),
):
    """Get dynamic Artifact Lineage DAG for a Space."""
    return LineageService.get_space_lineage(space_id, tag, current_user)


@router.get("/spaces/{space_id}/messages", response_model=list[Message])
def list_messages(
    space_id: str,
    tag: str | None = Query(default=None, description="Optional project tag filter"),
    current_user: User = Depends(get_current_user),
):
    """List messages in a Space, optionally filtered by project_tag."""
    return MessageService.list_messages(space_id, current_user, project_tag=tag)


@router.get("/spaces/{space_id}/messages/page")
def list_messages_page(
    space_id: str,
    tag: str | None = Query(default=None, description="Optional project tag filter"),
    limit: int = Query(default=30, ge=1, le=100, description="Page size limit"),
    cursor: str | None = Query(default=None, description="Signed before_cursor token"),
    current_user: User = Depends(get_current_user),
):
    """List messages in a Space with signed reverse-cursor pagination."""
    return MessageService.list_messages_page(
        space_id=space_id,
        user=current_user,
        project_tag=tag,
        limit=limit,
        before_cursor=cursor,
    )


@router.post("/spaces/{space_id}/messages", response_model=Message)
def post_message(
    space_id: str,
    payload: PostMessageRequest,
    current_user: User = Depends(get_current_user),
):
    """Post a new chat message into a Space with active project tag."""
    return MessageService.post_message(
        space_id=space_id,
        content=payload.content,
        user=current_user,
        project_tag=payload.project_tag,
        attachment_file_ids=payload.attachment_file_ids,
    )
