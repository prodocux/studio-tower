
from app.models.message import Message, MessageRole
from app.models.user import User
from app.services.space_service import SpaceService
from app.services.storage import store
from fastapi import HTTPException, status


class MessageService:
    @staticmethod
    def post_message(
        space_id: str,
        content: str,
        user: User,
        project_tag: str | None = "general",
        attachment_file_ids: list[str] | None = None,
    ) -> Message:
        # Strict membership check
        SpaceService.get_space_with_auth(space_id, user)

        if not content.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Message content cannot be empty",
            )

        # Validate attachments belong to the same space
        if attachment_file_ids:
            for fid in attachment_file_ids:
                file_rec = store.get_file(fid)
                if not file_rec or file_rec.space_id != space_id:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Attachment '{fid}' does not belong to this Space",
                    )

        # Strict security: sender_uid is ALWAYS derived from authenticated user token
        msg = Message(
            space_id=space_id,
            sender_uid=user.uid,
            sender_name=user.display_name or user.email.split("@")[0],
            role=MessageRole.USER,
            content=content,
            project_tag=project_tag or "general",
            attachment_file_ids=attachment_file_ids or [],
        )
        return store.add_message(msg)

    @staticmethod
    def list_messages(
        space_id: str,
        user: User,
        project_tag: str | None = None,
    ) -> list[Message]:
        # Strict membership check
        SpaceService.get_space_with_auth(space_id, user)
        return store.list_messages(space_id, project_tag=project_tag)

    @staticmethod
    def list_messages_page(
        space_id: str,
        user: User,
        project_tag: str | None = None,
        limit: int = 30,
        before_cursor: str | None = None,
    ) -> dict:
        # Strict membership check
        SpaceService.get_space_with_auth(space_id, user)
        try:
            items, next_cursor, has_more = store.list_messages_page(
                space_id=space_id,
                project_tag=project_tag,
                limit=limit,
                before_cursor=before_cursor,
            )
            return {
                "items": items,
                "next_cursor": next_cursor,
                "has_more": has_more,
            }
        except Exception as e:
            if "InvalidCursorError" in type(e).__name__ or "CURSOR" in str(e):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid pagination cursor: {e}",
                )
            raise
