import uuid
from datetime import UTC, datetime
from typing import List, Optional

from app.models.activity import ActivityEventType
from app.models.invite_preview import InvitePreviewResponse
from app.models.message import Message, MessageRole
from app.models.space import (
    Capabilities,
    Invite,
    MemberInfo,
    MembershipRole,
    ProjectTag,
    Space,
    SpaceContext,
    SpaceKind,
)
from app.models.user import User
from app.services.activity_service import ActivityService
from app.services.storage import store
from fastapi import HTTPException, status


class SpaceService:
    @staticmethod
    def ensure_agent_dm(user: User) -> Space:
        """
        Ensure user has an Agent DM (1:1 private workspace).
        Auto-provisions one if missing.
        """
        existing = store.get_agent_dm_for_user(user.uid)
        if existing:
            return existing

        dm_space = Space(
            name="StudioTower Agent",
            kind=SpaceKind.AGENT_DM,
            created_by=user.uid,
            tags=[
                ProjectTag(name="General", slug="general", color="#64748B", description="Private agent workspace")
            ],
        )
        store.create_space(dm_space, user.uid)

        # Initial welcome message from Agent
        welcome_msg = Message(
            space_id=dm_space.space_id,
            sender_uid="agent_studiotower",
            sender_name="StudioTower Agent",
            role=MessageRole.AGENT,
            content=f"Hello {user.display_name or 'there'}, I am your StudioTower film production prep assistant. Upload your treatment or script, and we can begin scene breakdowns and conflict analysis.",
            project_tag="general",
        )
        store.add_message(welcome_msg)
        return dm_space

    @staticmethod
    def list_accessible_spaces(user: User) -> List[Space]:
        # Always ensure Agent DM is present
        SpaceService.ensure_agent_dm(user)
        return store.list_spaces_for_user(user.uid)

    @staticmethod
    def get_space_with_auth(space_id: str, user: User) -> Space:
        if not store.is_member(space_id, user.uid):
            target_space = store.get_space(space_id)
            if target_space and (target_space.created_by == user.uid or target_space.kind == SpaceKind.AGENT_DM):
                store.add_member(space_id, user.uid, MembershipRole.OWNER)
            else:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: you are not a member of this Space",
                )
        space = store.get_space(space_id)
        if not space:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Space not found",
            )
        return space

    @staticmethod
    def get_user_role(space_id: str, user: User) -> Optional[MembershipRole]:
        SpaceService.get_space_with_auth(space_id, user)
        return store.get_member_role(space_id, user.uid)

    @staticmethod
    def create_shared_space(name: str, user: User, initial_tags: Optional[List[ProjectTag]] = None) -> Space:
        tags = [ProjectTag(name="General", slug="general", color="#64748B")]
        if initial_tags:
            tags.extend(initial_tags)

        new_space = Space(
            name=name,
            kind=SpaceKind.SHARED_SPACE,
            created_by=user.uid,
            tags=tags,
        )
        return store.create_space(new_space, user.uid)

    @staticmethod
    def add_tag_to_space(space_id: str, tag: ProjectTag, user: User) -> Space:
        user_role = SpaceService.get_user_role(space_id, user)
        if user_role not in (MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.COORDINATOR):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: only Space owners, administrators, or coordinators can manage project tags",
            )

        # Validate slug format
        clean_slug = tag.slug.strip().lower()
        if not clean_slug:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Tag slug cannot be empty")
        if clean_slug == "all":
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="'all' is a reserved slug for overview filter")

        import re
        if not re.match(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", clean_slug):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Tag slug must contain only lowercase letters, numbers, and hyphens",
            )

        tag.slug = clean_slug
        updated_space = store.add_tag_to_space(space_id, tag, actor_uid=user.uid)
        if not updated_space:
            raise HTTPException(status_code=404, detail="Space not found")
        return updated_space

    @staticmethod
    def update_tag_in_space(
        space_id: str,
        tag_id_or_slug: str,
        name: Optional[str],
        color: Optional[str],
        description: Optional[str],
        user: User,
    ) -> Space:
        user_role = SpaceService.get_user_role(space_id, user)
        if user_role not in (MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.COORDINATOR):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: only Space owners, administrators, or coordinators can manage project tags",
            )

        updated_space = store.update_tag_in_space(
            space_id=space_id,
            tag_id_or_slug=tag_id_or_slug,
            name=name,
            color=color,
            description=description,
            actor_uid=user.uid,
        )
        if not updated_space:
            raise HTTPException(status_code=404, detail="Space or Tag not found")
        return updated_space

    @staticmethod
    def archive_tag_in_space(
        space_id: str,
        tag_id_or_slug: str,
        archived: bool,
        user: User,
    ) -> Space:
        user_role = SpaceService.get_user_role(space_id, user)
        if user_role not in (MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.COORDINATOR):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: only Space owners, administrators, or coordinators can manage project tags",
            )

        # Check protection on 'general' tag
        if tag_id_or_slug.lower().strip() in ("general", "tag_general"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Cannot archive or delete the default 'general' tag",
            )

        updated_space = store.archive_tag_in_space(space_id, tag_id_or_slug, archived=archived, actor_uid=user.uid)
        if not updated_space:
            raise HTTPException(status_code=404, detail="Space or Tag not found")
        return updated_space

    @staticmethod
    def create_invite(
        space_id: str,
        user: User,
        role: MembershipRole = MembershipRole.MEMBER,
        target_email: Optional[str] = None,
        max_uses: Optional[int] = None,
        expires_at: Optional[datetime] = None,
    ) -> Invite:
        space = SpaceService.get_space_with_auth(space_id, user)
        if space.kind == SpaceKind.AGENT_DM:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot create invite for private Agent DM",
            )

        caller_role = SpaceService.get_user_role(space_id, user)
        if caller_role not in (MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.COORDINATOR):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: standard members cannot generate space invitations",
            )

        # Strict hierarchy check:
        # 1. Only OWNER can invite another OWNER
        if role == MembershipRole.OWNER:
            if caller_role != MembershipRole.OWNER:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: only Space owners can invite another owner",
                )
        # 2. Only OWNER or ADMIN can invite ADMIN or COORDINATOR
        elif role in (MembershipRole.ADMIN, MembershipRole.COORDINATOR):
            if caller_role not in (MembershipRole.OWNER, MembershipRole.ADMIN):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Access denied: you cannot invite users with '{role.value}' role",
                )

        # Hard security invariant for high-privileged invites
        if role in (MembershipRole.OWNER, MembershipRole.ADMIN):
            if not target_email or not target_email.strip():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Target email is required when creating a privileged '{role.value}' invite",
                )
            if max_uses is not None and max_uses != 1:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Privileged role '{role.value}' invites must be single-use (max_uses=1)",
                )

        # Privileged roles default to single-use (max_uses=1)
        actual_max_uses = 1 if role in (MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.COORDINATOR) else (max_uses or 100)

        invite = Invite(
            space_id=space_id,
            created_by=user.uid,
            role=role,
            target_email=target_email.lower().strip() if target_email else None,
            max_uses=actual_max_uses,
            expires_at=expires_at,
        )
        return store.create_invite(invite)

    @staticmethod
    def get_invite_preview(token: str) -> InvitePreviewResponse:
        """Public preview contract for an invitation token.

        Returns 200 with space name, role, and masked target email for active invites.
        Returns 410 with machine-readable error codes (INVITE_REVOKED, INVITE_EXHAUSTED,
        INVITE_EXPIRED) for known invalid invites.
        Returns 404 for unknown or malformed invites.
        Strictly omits space_id and raw inviter name to prevent tenant enumeration.
        """
        invite = store.get_invite(token)
        if not invite:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "INVITE_NOT_FOUND", "message": "Invitation link was not found or is malformed."},
            )

        if invite.revoked_at is not None:
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail={"code": "INVITE_REVOKED", "message": "This invitation has been revoked."},
            )

        if invite.used_count >= invite.max_uses:
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail={"code": "INVITE_EXHAUSTED", "message": "This invitation has reached its maximum usage limit."},
            )

        if invite.expires_at and invite.expires_at < datetime.now(UTC):
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail={"code": "INVITE_EXPIRED", "message": "This invitation has expired."},
            )

        space = store.get_space(invite.space_id)
        if not space:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "SPACE_NOT_FOUND", "message": "The workspace for this invitation no longer exists."},
            )

        target_email_masked = None
        if invite.target_email:
            parts = invite.target_email.split("@", 1)
            if len(parts) == 2:
                local, domain = parts
                masked_local = (local[0] + "***") if len(local) > 0 else "***"
                target_email_masked = f"{masked_local}@{domain}"
            else:
                target_email_masked = "***"

        return InvitePreviewResponse(
            space_name=space.name,
            role=invite.role,
            target_email_masked=target_email_masked,
            status="active",
        )

    @staticmethod
    def accept_invite(token: str, user: User) -> Space:
        # Atomic validation, single-step usage consumption, and transactional membership assignment
        try:
            return store.accept_invite_and_join(token, user)
        except ValueError as e:
            err = str(e)
            if err == "INVALID_INVITE":
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid invite link")
            elif err == "REVOKED_OR_EXHAUSTED":
                raise HTTPException(status_code=status.HTTP_410_GONE, detail="This invitation has been revoked or reached its maximum usage limit")
            elif err == "EXPIRED":
                raise HTTPException(status_code=status.HTTP_410_GONE, detail="This invitation has expired")
            elif err == "EMAIL_MISMATCH":
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied: this invite is bound to a different email address")
            elif err == "SPACE_NOT_FOUND":
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Space no longer exists")
            else:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invite consumption failed")
        except Exception as e:
            from app.services.storage import StorageConflictError, StorageUnavailableError
            if isinstance(e, StorageConflictError):
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Concurrent invitation update conflict. Please retry.")
            elif isinstance(e, StorageUnavailableError):
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database backend is temporarily busy. Please retry shortly.")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Invitation processing encountered an internal error.")

    @staticmethod
    def revoke_invite(token: str, user: User) -> Invite:
        invite = store.get_invite(token)
        if not invite:
            raise HTTPException(status_code=404, detail="Invite not found")

        caller_role = SpaceService.get_user_role(invite.space_id, user)
        if caller_role not in (MembershipRole.OWNER, MembershipRole.ADMIN) and invite.created_by != user.uid:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: only Space administrators or invite creator can revoke invitations",
            )

        revoked = store.revoke_invite(token)
        return revoked

    @staticmethod
    def leave_space(space_id: str, user: User) -> bool:
        space = SpaceService.get_space_with_auth(space_id, user)
        if space.kind == SpaceKind.AGENT_DM:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot leave private Agent DM",
            )

        # Check if user is sole OWNER while other members exist across backend
        user_role = SpaceService.get_user_role(space_id, user)
        role_counts = store.count_space_members_by_role(space_id)
        total_members = sum(role_counts.values())
        owner_count = role_counts.get(MembershipRole.OWNER, 0)

        if user_role == MembershipRole.OWNER and total_members > 1 and owner_count <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Sole Space owner cannot leave without transferring ownership to another member",
            )

        return store.remove_member(space_id, user.uid)

    @staticmethod
    def get_space_context(space_id: str, user: User) -> SpaceContext:
        space = SpaceService.get_space_with_auth(space_id, user)
        role = SpaceService.get_user_role(space_id, user) or MembershipRole.MEMBER
        members = store.list_members_in_space(space_id)
        member_count = len(members)

        # Capabilities matrix:
        caps = Capabilities(
            can_invite=role in (MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.COORDINATOR),
            can_manage_members=role in (MembershipRole.OWNER, MembershipRole.ADMIN),
            can_change_role=role in (MembershipRole.OWNER, MembershipRole.ADMIN),
            can_remove_member=role in (MembershipRole.OWNER, MembershipRole.ADMIN),
            can_transfer_ownership=role == MembershipRole.OWNER,
            can_approve_runs=role in (MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.COORDINATOR),
            can_manage_tags=role in (MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.COORDINATOR),
            can_leave_space=not (role == MembershipRole.OWNER and member_count > 1 and store.count_space_members_by_role(space_id).get(MembershipRole.OWNER, 0) <= 1),
            can_view_telemetry=role in (MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.COORDINATOR, MembershipRole.MEMBER) and getattr(user, "can_view_telemetry", True),
            can_diagnose_runs=role in (MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.COORDINATOR, MembershipRole.MEMBER) and getattr(user, "can_diagnose_runs", True),
        )

        return SpaceContext(
            space=space,
            current_user_role=role,
            member_count=member_count,
            capabilities=caps,
        )

    @staticmethod
    def list_space_members(space_id: str, user: User) -> List[MemberInfo]:
        SpaceService.get_space_with_auth(space_id, user)
        members = store.list_members_in_space(space_id)
        result = []
        for uid, role in members:
            user_rec = store.get_user(uid)
            if user_rec:
                disp_name = user_rec.display_name or user_rec.email or uid
                email = user_rec.email
            else:
                disp_name = uid
                email = ""
            result.append(MemberInfo(
                uid=uid,
                display_name=disp_name,
                email=email,
                role=role,
            ))
        return result

    @staticmethod
    def direct_add_member(space_id: str, email_or_uid: str, role: MembershipRole, actor_user: User) -> MemberInfo:
        SpaceService.get_space_with_auth(space_id, actor_user)
        actor_role = SpaceService.get_user_role(space_id, actor_user)
        if actor_role not in (MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.COORDINATOR):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: standard members cannot directly add new space members",
            )

        if actor_role != MembershipRole.OWNER and role == MembershipRole.OWNER:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: only Space Owner can grant Owner role",
            )

        target_user = store.get_user(email_or_uid) or store.get_user_by_email(email_or_uid)
        if not target_user:
            clean_str = email_or_uid.strip().lower()
            if "@" in clean_str:
                target_user = User(
                    uid=f"usr_{uuid.uuid4().hex[:12]}",
                    email=clean_str,
                    display_name=clean_str.split("@")[0].capitalize(),
                )
                store.save_user(target_user)
            else:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"User '{email_or_uid}' not found on site",
                )

        store.add_member(space_id, target_user.uid, role)
        return MemberInfo(
            uid=target_user.uid,
            display_name=target_user.display_name or target_user.email,
            email=target_user.email,
            role=role,
        )

    @staticmethod
    def update_member_role(space_id: str, target_uid: str, new_role: MembershipRole, actor_user: User) -> dict:
        SpaceService.get_space_with_auth(space_id, actor_user)
        actor_role = SpaceService.get_user_role(space_id, actor_user)
        if actor_role not in (MembershipRole.OWNER, MembershipRole.ADMIN):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied: only Owner or Admin can modify member roles")

        target_role = store.get_member_role(space_id, target_uid)
        if not target_role:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target member not found in this space")

        if target_role == MembershipRole.OWNER:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot change Owner role directly; use ownership transfer")

        if actor_role == MembershipRole.ADMIN and (target_role == MembershipRole.ADMIN or new_role in (MembershipRole.OWNER, MembershipRole.ADMIN)):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admins cannot promote to Admin/Owner or demote other Admins")

        store.add_member(space_id, target_uid, new_role)
        return {"status": "updated", "space_id": space_id, "uid": target_uid, "new_role": new_role.value}

    @staticmethod
    def remove_space_member(space_id: str, target_uid: str, actor_user: User) -> dict:
        SpaceService.get_space_with_auth(space_id, actor_user)
        actor_role = SpaceService.get_user_role(space_id, actor_user)
        if actor_role not in (MembershipRole.OWNER, MembershipRole.ADMIN):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied: only Owner or Admin can remove members")

        target_role = store.get_member_role(space_id, target_uid)
        if not target_role:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target member not found in this space")

        if target_role == MembershipRole.OWNER:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot remove Space Owner without ownership transfer")

        if actor_role == MembershipRole.ADMIN and target_role == MembershipRole.ADMIN:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admins cannot remove other Admins")

        store.remove_member(space_id, target_uid)
        return {"status": "removed", "space_id": space_id, "uid": target_uid}

    @staticmethod
    def transfer_ownership(space_id: str, new_owner_uid: str, actor_user: User) -> dict:
        SpaceService.get_space_with_auth(space_id, actor_user)
        actor_role = SpaceService.get_user_role(space_id, actor_user)
        if actor_role != MembershipRole.OWNER:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied: only Space Owner can transfer ownership")

        if new_owner_uid == actor_user.uid:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You are already the owner of this space")

        try:
            store.transfer_ownership_atomic(space_id, actor_user.uid, new_owner_uid)
        except ValueError as e:
            err = str(e)
            if err == "CALLER_NOT_CURRENT_OWNER":
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Actor is not the current Space Owner")
            elif err == "NEW_OWNER_NOT_MEMBER":
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="New owner must already be a member of this space")
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Ownership transfer failed")

        return {
            "status": "transferred",
            "space_id": space_id,
            "previous_owner_uid": actor_user.uid,
            "new_owner_uid": new_owner_uid,
            "previous_owner_new_role": MembershipRole.ADMIN.value,
        }

    @staticmethod
    def list_invites_for_space(space_id: str, actor_user: User) -> List[Invite]:
        SpaceService.get_space_with_auth(space_id, actor_user)
        actor_role = SpaceService.get_user_role(space_id, actor_user)
        if actor_role not in (MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.COORDINATOR):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied: members cannot view invitation links")
        return store.list_invites_for_space(space_id)

    @staticmethod
    def check_can_diagnose_runs(space_id: str, actor_user: User) -> None:
        """
        Verify caller has permission to invoke AI failure diagnosis on workflow runs.
        Separated from general telemetry viewing permissions.
        """
        SpaceService.get_space_with_auth(space_id, actor_user)
        role = SpaceService.get_user_role(space_id, actor_user)
        if not role or getattr(actor_user, "can_diagnose_runs", True) is False:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: caller does not have permission to diagnose runs in this Space (can_diagnose_runs capability required)",
            )

    @staticmethod
    def check_can_view_telemetry(space_id: str, actor_user: User) -> None:
        """
        Verify caller has permission to view telemetry traces and metrics in this Space.
        """
        SpaceService.get_space_with_auth(space_id, actor_user)
        role = SpaceService.get_user_role(space_id, actor_user)
        if not role or getattr(actor_user, "can_view_telemetry", True) is False:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: caller does not have permission to view telemetry in this Space (can_view_telemetry capability required)",
            )
