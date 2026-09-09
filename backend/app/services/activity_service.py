import logging
from typing import Any, Dict, List, Optional, Tuple

from app.models.activity import ActivityEvent, ActivityEventType
from app.models.file_record import FileRecord, IngestionStatus
from app.models.message import Message
from app.models.run import Run
from app.models.space import ProjectTag
from app.models.user import User
from app.services.storage import store

logger = logging.getLogger("studiotower.activity")


def _source_file_correlation(run: Run) -> Dict[str, Any]:
    """Attach source file_id and basename so Tempo spans can name the asset."""
    file_id = getattr(run, "source_file_id", None)
    if not file_id:
        return {}
    details: Dict[str, Any] = {"file_id": file_id}
    try:
        rec = store.get_file(file_id)
    except Exception:
        rec = None
    if rec and rec.filename:
        details["filename"] = rec.filename
    return details


class ActivityService:
    @staticmethod
    def record_file_event(
        space_id: str,
        event_type: ActivityEventType,
        file_record: FileRecord,
        actor_uid: Optional[str] = None,
        summary: Optional[str] = None,
        extra_details: Optional[Dict[str, Any]] = None,
    ) -> ActivityEvent:
        details: Dict[str, Any] = {
            "filename": file_record.filename,
            "file_id": file_record.file_id,
            "generation": file_record.active_generation,
            "ingestion_status": file_record.ingestion_status.value if isinstance(file_record.ingestion_status, IngestionStatus) else str(file_record.ingestion_status),
        }
        if extra_details:
            details.update(extra_details)

        gen = file_record.active_generation or 0
        if not summary:
            if event_type == ActivityEventType.FILE_UPLOADED:
                summary = f"File '{file_record.filename}' uploaded"
            elif event_type == ActivityEventType.FILE_INGESTION_READY:
                summary = f"File '{file_record.filename}' indexing completed (generation #{gen})"
            elif event_type == ActivityEventType.FILE_INGESTION_PARTIAL:
                summary = f"File '{file_record.filename}' indexed with partial text extraction"
            elif event_type == ActivityEventType.FILE_NEEDS_OCR:
                summary = f"File '{file_record.filename}' requires OCR processing"
            elif event_type == ActivityEventType.FILE_INGESTION_FAILED:
                summary = f"File '{file_record.filename}' indexing failed"
            elif event_type == ActivityEventType.FILE_REINDEX_TRIGGERED:
                summary = f"File '{file_record.filename}' reindexing initiated"
            else:
                summary = f"File '{file_record.filename}' status updated"

        # Deterministic Event ID
        if event_type == ActivityEventType.FILE_UPLOADED:
            event_id = f"file.uploaded:{file_record.file_id}"
        else:
            event_id = f"{event_type.value}:{file_record.file_id}:g{gen}"

        tags = file_record.project_tags if (file_record.project_tags and len(file_record.project_tags) > 0) else ["general"]
        ev = ActivityEvent(
            event_id=event_id,
            event_type=event_type,
            space_id=space_id,
            project_tags=tags,
            resource_type="file",
            resource_id=file_record.file_id,
            summary=summary,
            details=details,
            actor_uid=actor_uid,
        )
        return store.record_activity_event(ev)

    @staticmethod
    def record_tag_event(
        space_id: str,
        event_type: ActivityEventType,
        tag: ProjectTag,
        actor_uid: Optional[str] = None,
        summary: Optional[str] = None,
    ) -> ActivityEvent:
        rev = getattr(tag, "revision", 1) or 1
        if not summary:
            if event_type == ActivityEventType.TAG_CREATED:
                summary = f"Tag '{tag.name}' created"
            elif event_type == ActivityEventType.TAG_UPDATED:
                summary = f"Tag '{tag.name}' updated"
            elif event_type == ActivityEventType.TAG_ARCHIVED:
                summary = f"Tag '{tag.name}' archived"
            elif event_type == ActivityEventType.TAG_UNARCHIVED:
                summary = f"Tag '{tag.name}' restored"
            else:
                summary = f"Tag '{tag.name}' modified"

        details: Dict[str, Any] = {
            "tag_id": tag.id,
            "slug": tag.slug,
            "name": tag.name,
            "color": tag.color,
            "revision": rev,
            "archived": tag.archived,
        }
        event_id = f"{event_type.value}:{tag.id}:r{rev}"
        ev = ActivityEvent(
            event_id=event_id,
            event_type=event_type,
            space_id=space_id,
            project_tags=[tag.slug],
            resource_type="tag",
            resource_id=tag.id,
            summary=summary,
            details=details,
            actor_uid=actor_uid,
        )
        return store.record_activity_event(ev)

    @staticmethod
    def record_message_event(
        space_id: str,
        message: Message,
        actor_uid: Optional[str] = None,
        summary: Optional[str] = None,
    ) -> ActivityEvent:
        tag = message.project_tag or "general"
        sender = message.sender_name or message.sender_uid or "User"
        preview = message.content[:60] + "..." if len(message.content) > 60 else message.content
        if not summary:
            summary = f"Message sent by {sender}: {preview}"

        attachments = list(getattr(message, "attachment_file_ids", []) or [])
        details: Dict[str, Any] = {
            "message_id": message.message_id,
            "sender_uid": message.sender_uid,
            "sender_name": message.sender_name,
            "role": message.role.value if hasattr(message.role, "value") else str(message.role),
            "content_preview": preview,
            "attachment_count": len(attachments),
        }
        if attachments:
            details["file_id"] = attachments[0]
        event_id = f"message.created:{message.message_id}"
        ev = ActivityEvent(
            event_id=event_id,
            event_type=ActivityEventType.MESSAGE_CREATED,
            space_id=space_id,
            project_tags=[tag],
            resource_type="message",
            resource_id=message.message_id,
            summary=summary,
            details=details,
            actor_uid=actor_uid or message.sender_uid,
        )
        return store.record_activity_event(ev)

    @staticmethod
    def record_run_event(
        space_id: str,
        run: Run,
        event_type: ActivityEventType,
        actor_uid: Optional[str] = None,
        summary: Optional[str] = None,
    ) -> ActivityEvent:
        tag = run.project_tag or "general"
        ver = getattr(run, "state_version", 1) or 1
        if not summary:
            if event_type == ActivityEventType.RUN_STARTED:
                summary = f"Execution run started (Trace: {run.trace_id})"
            elif event_type == ActivityEventType.RUN_COMPLETED:
                summary = f"Execution run completed successfully (Trace: {run.trace_id})"
            elif event_type == ActivityEventType.RUN_FAILED:
                summary = f"Execution run failed (Trace: {run.trace_id}): {run.error_summary or 'Internal error'}"
            else:
                summary = f"Execution run status updated (Trace: {run.trace_id})"

        details: Dict[str, Any] = {
            "run_id": run.run_id,
            "trace_id": run.trace_id,
            "status": run.status.value if hasattr(run.status, "value") else str(run.status),
            "state_version": ver,
            "is_retryable": getattr(run, "is_retryable", False),
            "failure_code": getattr(run, "failure_code", None),
        }
        details.update(_source_file_correlation(run))
        event_id = f"{event_type.value}:{run.run_id}:r{ver}"
        ev = ActivityEvent(
            event_id=event_id,
            event_type=event_type,
            space_id=space_id,
            project_tags=[tag],
            resource_type="run",
            resource_id=run.run_id,
            summary=summary,
            details=details,
            actor_uid=actor_uid,
        )
        return store.record_activity_event(ev)

    @staticmethod
    def record_gate_event(
        space_id: str,
        run: Run,
        approved: bool,
        actor_uid: Optional[str] = None,
        summary: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> ActivityEvent:
        tag = run.project_tag or "general"
        ver = getattr(run, "state_version", 1) or 1
        event_type = ActivityEventType.GATE_APPROVED if approved else ActivityEventType.GATE_REJECTED
        gate_title = run.approval_gate.title if run.approval_gate else "Approval Gate"
        if not summary:
            if approved:
                summary = f"Approval gate '{gate_title}' approved"
            else:
                summary = f"Approval gate '{gate_title}' rejected: {reason or 'No reason provided'}"

        details: Dict[str, Any] = {
            "run_id": run.run_id,
            "gate_id": run.approval_gate.gate_id if run.approval_gate else None,
            "approved": approved,
            "reason": reason,
            "state_version": ver,
        }
        details.update(_source_file_correlation(run))
        event_id = f"{event_type.value}:{run.run_id}:r{ver}"
        ev = ActivityEvent(
            event_id=event_id,
            event_type=event_type,
            space_id=space_id,
            project_tags=[tag],
            resource_type="gate",
            resource_id=run.run_id,
            summary=summary,
            details=details,
            actor_uid=actor_uid,
        )
        return store.record_activity_event(ev)

    @staticmethod
    def list_activities(
        space_id: str,
        user: User,
        tag: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: int = 50,
    ) -> Tuple[List[ActivityEvent], Optional[str]]:
        from app.services.space_service import SpaceService
        SpaceService.get_space_with_auth(space_id, user)
        return store.list_activity_events(space_id, tag=tag, cursor=cursor, limit=limit)
