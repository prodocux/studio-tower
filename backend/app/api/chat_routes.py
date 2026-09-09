import contextlib
import hashlib
import logging
import re
import threading
import uuid
from datetime import UTC, datetime
from typing import Literal, Optional

from app.agent.brain import AgentBrain
from app.core.auth import get_current_user
from app.core.config import settings
from app.models.action_proposal import (
    ActionConfirmationPayload,
    ActionExecutionRecord,
    ActionExecutionStatus,
    ActionProposal,
    ActionSourceDescriptor,
    DispatchStatus,
    compute_canonical_proposal_hash,
    issue_action_token,
    verify_action_token,
)
from app.models.deliverable_spec import DeliverableSpecError, resolve_deliverable_spec
from app.models.file_record import IngestionStatus
from app.models.idempotency import ChatIdempotencyRecord, ChatIdempotencyStatus
from app.models.message import Message, MessageRole
from app.models.run import ApprovalGate, Run, RunStatus
from app.models.user import User
from app.services.deliverable_service import DeliverableExecutionService
from app.services.file_service import FileService
from app.services.space_service import SpaceService
from app.services.storage import StorageConflictError, store
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["chat"])

MACRO_ANALYTICAL_KEYWORDS = (
    "analyze",
    "break down",
    "breakdown",
    "synthesize",
    "synthesize 5",
    "canonical production deliverable",
    "stunt hazards",
    "departmental tags",
    "production considerations",
    "story about",
    "what is this about",
    "what is this",
    "talking about",
    "tell me about",
    "what does this",
    "summarize",
    "summary",
    "overview",
    "describe this",
    "場景拆解",
    "劇本分析",
    "製作考量",
    "交付物",
    "特技風險",
    "這是什麼",
    "在講什麼",
    "概述",
    "摘要",
    "講什麼",
)


def normalize_user_query(text: str) -> str:
    lowered = (text or "").lower().replace("’", "'").replace("‘", "'")
    lowered = re.sub(r"\bwhats\b", "what is", lowered)
    return lowered.replace("what's", "what is")


def is_macro_analytical_query(text: str) -> bool:
    blob = normalize_user_query(text)
    return any(keyword in blob for keyword in MACRO_ANALYTICAL_KEYWORDS)


class ThreadSafeIdempotencyTracker:
    """
    Thread-safe tracker coordinating version fencing, lease ownership, and health
    between the main worker thread and background heartbeat renewal threads.
    """
    def __init__(self, key: str, initial_version: int, lease_owner: Optional[str] = None):
        self.key = key
        self._version = initial_version
        self.lease_owner = lease_owner
        self._lock = threading.Lock()
        self._lease_healthy = True
        self._failure_code: Optional[str] = None

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def update_version(self, new_version: int) -> None:
        with self._lock:
            self._version = new_version

    def mark_unhealthy(self, code: str = "LEASE_LOST") -> None:
        with self._lock:
            self._lease_healthy = False
            self._failure_code = code

    @property
    def is_healthy(self) -> bool:
        with self._lock:
            return self._lease_healthy

    @property
    def failure_code(self) -> Optional[str]:
        with self._lock:
            return self._failure_code


@contextlib.contextmanager
def idempotency_heartbeat(
    tracker: Optional[ThreadSafeIdempotencyTracker],
    interval_sec: float = 15.0,
    extend_sec: int = 120,
    join_timeout_sec: float = 2.0,
):
    """
    Background heartbeat worker that periodically extends the active idempotency lease
    while long-running AI inference or database tasks are in flight.
    Synchronizes updated version numbers atomically with the main thread.
    """
    if not tracker:
        yield
        return

    stop_event = threading.Event()

    def _heartbeat_worker():
        while not stop_event.wait(interval_sec):
            if not tracker.is_healthy:
                break
            curr_ver = tracker.version
            try:
                ok, updated_rec = store.update_chat_idempotency_fenced(
                    tracker.key,
                    curr_ver,
                    expected_lease_owner=tracker.lease_owner,
                    extend_lease_seconds=extend_sec,
                )
                if ok and updated_rec:
                    # Critical: Always sync committed version to tracker first so main thread never uses stale version
                    tracker.update_version(updated_rec.version)
                else:
                    logger.warning("Heartbeat renewal rejected for key %s (lease lost or preempted)", tracker.key)
                    tracker.mark_unhealthy("LEASE_RENEWAL_REJECTED")
                    break
                if not tracker.is_healthy or stop_event.is_set():
                    break
            except Exception as e:
                logger.exception("Heartbeat renewal background exception for key %s: %s", tracker.key, e)
                tracker.mark_unhealthy("HEARTBEAT_ERROR")
                break

    t = threading.Thread(target=_heartbeat_worker, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop_event.set()
        t.join(timeout=join_timeout_sec)
        if t.is_alive():
            logger.error("Heartbeat background thread for key %s failed to exit within %.1fs join window", tracker.key, join_timeout_sec)
            tracker.mark_unhealthy("HEARTBEAT_JOIN_TIMEOUT")

        if not tracker.is_healthy:
            # Actively revoke lease and mark FAILED using expected_lease_owner so that even if a late
            # background heartbeat committed version v+1, this owner-level revocation succeeds atomically.
            fail_ok = False
            rec_after = None
            try:
                fail_ok, rec_after = store.update_chat_idempotency_fenced(
                    tracker.key,
                    expected_version=None,
                    expected_lease_owner=tracker.lease_owner,
                    status=ChatIdempotencyStatus.FAILED,
                    error_status_code=status.HTTP_409_CONFLICT,
                    error_detail="Heartbeat background worker lease lost or timed out.",
                    extend_lease_seconds=None,
                )
            except Exception as fail_err:
                logger.exception("Exception while marking idempotency key %s as FAILED: %s", tracker.key, fail_err)
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Database storage unavailable during lease revocation. Please retry shortly.",
                    headers={"Retry-After": "5"},
                ) from fail_err

            if not fail_ok:
                if rec_after and rec_after.lease_owner != tracker.lease_owner:
                    logger.warning(
                        "Idempotency key %s was preempted by newer leaseholder %s (our owner: %s)",
                        tracker.key,
                        rec_after.lease_owner,
                        tracker.lease_owner,
                    )
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Operation lease was acquired by a concurrent worker. State modification fenced off.",
                    )
                else:
                    logger.error(
                        "CRITICAL: Failed to mark idempotency key %s as FAILED on lease abort (failure_code=%s, lease_owner=%s)",
                        tracker.key,
                        tracker.failure_code,
                        tracker.lease_owner,
                    )
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="Database failed to transition idempotency state. Please retry shortly.",
                        headers={"Retry-After": "5"},
                    )

            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Operation lease lost or renewal timed out during processing. Please retry.",
            )


class ChatRequest(BaseModel):
    space_id: str
    content: str
    project_tag: str | None = "general"
    attachment_file_ids: list[str] | None = None
    context_file_ids: list[str] | None = None
    client_message_id: str | None = None
    intent: Literal["conversation", "document_qa", "create_breakdown"] | None = None
    context_run_id: str | None = None


class ChatResponse(BaseModel):
    user_message: Message
    agent_message: Message | None = None
    run: Run | None = None


@router.post("/chat", response_model=ChatResponse)
def execute_chat(
    payload: ChatRequest,
    current_user: User = Depends(get_current_user),
):
    """
    Post a user message and trigger Gemini AI reasoning when '@agent' or document analysis is requested.
    Fully idempotent across the complete operation lifecycle (user msg, agent msg, run) with version-fenced lease protection.
    """
    # 1. Pre-flight side-effect-free validation (Space access & attachment reading & Intent resolution)
    # MUST happen BEFORE acquiring idempotency lock to prevent orphaned IN_PROGRESS locks on validation errors.
    space = SpaceService.get_space_with_auth(payload.space_id, current_user)
    tag = payload.project_tag or "general"

    # Intent Validation & Resolution
    VALID_INTENTS = {"conversation", "document_qa", "create_breakdown"}
    if payload.intent is not None and payload.intent not in VALID_INTENTS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown intent: {payload.intent!r}. Must be one of: conversation, document_qa, create_breakdown.",
        )

    from app.models.space import SpaceKind
    is_agent_dm = (space.kind == SpaceKind.AGENT_DM)
    mentions_agent = "@agent" in payload.content.lower()

    def _log_legacy_intent_inference(resolved: str) -> None:
        """Log implicit intent inference for deprecation tracking."""
        logger.info(
            "legacy_intent_inference space=%s resolved=%s content_len=%d",
            space.space_id, resolved, len(payload.content),
        )

    # Tenancy check for context_run_id (Pre-flight, before acquiring idempotency lock)
    referenced_run = None
    if payload.context_run_id:
        referenced_run = store.get_run(payload.context_run_id)
        if not referenced_run or referenced_run.space_id != space.space_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Referenced run '{payload.context_run_id}' not found in space '{space.space_id}'.",
            )

    source_file_id = None
    doc_text = payload.content
    has_explicit_attachment = False  # True only when user explicitly attached a file to this message
    ocr_gap_warnings = []

    qa_file_ids = list(dict.fromkeys((payload.attachment_file_ids or []) + (payload.context_file_ids or [])))
    if payload.attachment_file_ids and len(payload.attachment_file_ids) > 0:
        has_explicit_attachment = True

    # Resolve effective intent per decision table
    if payload.intent is not None:
        effective_intent = payload.intent
    elif is_agent_dm:
        effective_intent = "document_qa" if has_explicit_attachment else "conversation"
        _log_legacy_intent_inference(effective_intent)
    elif mentions_agent:
        effective_intent = "document_qa" if has_explicit_attachment else "conversation"
        _log_legacy_intent_inference(effective_intent)
    else:
        effective_intent = None

    if effective_intent == "document_qa" and not qa_file_ids:
        ready_files = [
            f
            for f in store.list_files_in_space(space.space_id)
            if f.upload_status == "committed"
            and f.ingestion_status in (IngestionStatus.READY, IngestionStatus.READY_PARTIAL)
        ]
        ready_files.sort(key=lambda f: f.created_at or datetime.min.replace(tzinfo=UTC))
        if ready_files:
            qa_file_ids = [ready_files[-1].file_id]
        else:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Document QA mode requires at least one attached or referenced file.",
            )

    # Ingestion readiness gate and multi-document retrieval for Document QA
    qa_candidates = []
    if effective_intent == "document_qa" and qa_file_ids:
        for fid in qa_file_ids:
            f_rec = FileService.get_file_record(space.space_id, fid, current_user)
            if f_rec.upload_status != "committed":
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Document '{f_rec.filename}' upload is not yet committed.",
                )
            if f_rec.ingestion_status in (IngestionStatus.PENDING, IngestionStatus.EXTRACTING, IngestionStatus.INDEXING):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Document '{f_rec.filename}' is still processing. Please wait until indexing completes.",
                )
            if f_rec.ingestion_status == IngestionStatus.NEEDS_OCR:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Document '{f_rec.filename}' requires OCR processing before it can be used for Document QA.",
                )
            if f_rec.ingestion_status == IngestionStatus.FAILED:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Document '{f_rec.filename}' failed ingestion and cannot be used for Document QA.",
                )
            indexed_chunks = store.get_document_chunks(
                space.space_id,
                fid,
                getattr(f_rec, "active_generation", 0) or 0,
            )
            if not indexed_chunks:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Document '{f_rec.filename}' is still indexing. Please wait until chunks are ready.",
                )

        from app.services.retrieval_service import RetrievalService
        qa_candidates, ocr_gap_warnings = RetrievalService.retrieve_for_qa(
            space_id=space.space_id,
            query_text=payload.content,
            file_ids=qa_file_ids,
        )
    elif payload.attachment_file_ids and len(payload.attachment_file_ids) > 0:
        for fid in payload.attachment_file_ids:
            FileService.get_file_record(space.space_id, fid, current_user)
        source_file_id = payload.attachment_file_ids[0]
        content_bytes, file_rec = FileService.read_file_content(space.space_id, source_file_id, current_user)
        from app.integrations.prodocux_facade import ProDocuXFacade
        extract_res = ProDocuXFacade.extract_pdf_pages(content_bytes, filename=file_rec.filename)
        extracted_text = "\n\n".join(c.text_content for c in extract_res.chunks if c.text_content.strip())
        doc_text = extracted_text if extracted_text else content_bytes.decode("utf-8", errors="ignore")

    # 2. Idempotency Acquisition & Atomic State Machine
    idemp_rec = None
    idemp_key = None
    deterministic_user_msg_id = None
    deterministic_agent_msg_id = None
    deterministic_run_id = None

    if payload.client_message_id:
        idemp_key = ChatIdempotencyRecord.compute_key(space.space_id, current_user.uid, payload.client_message_id)
        deterministic_user_msg_id = ChatIdempotencyRecord.compute_deterministic_message_id(idemp_key, "user")
        deterministic_agent_msg_id = ChatIdempotencyRecord.compute_deterministic_message_id(idemp_key, "agent")
        deterministic_run_id = ChatIdempotencyRecord.compute_deterministic_run_id(idemp_key)
        payload_hash = ChatIdempotencyRecord.compute_payload_hash(
            payload.content,
            tag,
            payload.attachment_file_ids,
            intent=effective_intent,
            context_run_id=payload.context_run_id,
        )

        acquired, existing = store.acquire_chat_idempotency(
            idemp_key, space.space_id, current_user.uid, payload.client_message_id, payload_hash
        )

        if not acquired and existing:
            # 1. Sender security barrier
            if existing.sender_uid != current_user.uid:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Idempotency key does not belong to the current authenticated user.",
                )

            # 2. Payload consistency barrier
            if existing.payload_hash != payload_hash:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Idempotency key reused with conflicting request parameters.",
                )

            # 3. Already completed: return exact existing operation result without duplicating runs
            if existing.status == ChatIdempotencyStatus.COMPLETED:
                user_msg = store.get_message(existing.user_message_id or deterministic_user_msg_id)
                agent_msg = store.get_message(existing.agent_message_id or deterministic_agent_msg_id)
                run_rec = store.get_run(existing.run_id or deterministic_run_id)

                if not user_msg:
                    # Strict sender-scoped fallback
                    user_msg = store.get_user_message_by_client_id(space.space_id, current_user.uid, payload.client_message_id)

                if not user_msg:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail="Idempotency state inconsistency: referenced user message is missing.",
                    )

                return ChatResponse(user_message=user_msg, agent_message=agent_msg, run=run_rec)

            # 4. In progress: reject concurrent in-flight duplication
            if existing.status == ChatIdempotencyStatus.IN_PROGRESS:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="A request with this idempotency key is currently processing. Please wait.",
                )

            # 5. Failed: atomic Compare-And-Swap (CAS) transition from FAILED -> IN_PROGRESS
            if existing.status == ChatIdempotencyStatus.FAILED:
                transitioned, updated_rec = store.transition_chat_idempotency_status(
                    idemp_key,
                    expected_status=ChatIdempotencyStatus.FAILED,
                    new_status=ChatIdempotencyStatus.IN_PROGRESS,
                )
                if not transitioned:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="A concurrent retry for this failed operation is already in progress.",
                    )
                idemp_rec = updated_rec
        else:
            idemp_rec = existing

    # Thread-safe Version & Idempotency Tracker
    tracker: Optional[ThreadSafeIdempotencyTracker] = None
    if idemp_rec:
        tracker = ThreadSafeIdempotencyTracker(idemp_rec.key, idemp_rec.version, lease_owner=idemp_rec.lease_owner)

    def _fenced_checkpoint(
        status_val: Optional[ChatIdempotencyStatus] = None,
        user_msg_id: Optional[str] = None,
        agent_msg_id: Optional[str] = None,
        run_id_val: Optional[str] = None,
        err_code: Optional[int] = None,
        err_detail_str: Optional[str] = None,
        extend_lease_seconds: Optional[int] = 120,
    ) -> None:
        """Atomic version-fenced mutation helper with active lease extension."""
        nonlocal idemp_rec, tracker
        if not idemp_rec or not tracker:
            return
        if not tracker.is_healthy:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Operation lease lost or renewal timed out during processing. Please retry.",
            )
        lease_to_extend = extend_lease_seconds if status_val not in (ChatIdempotencyStatus.COMPLETED, ChatIdempotencyStatus.FAILED) else None
        curr_ver = tracker.version
        ok, updated = store.update_chat_idempotency_fenced(
            tracker.key,
            curr_ver,
            expected_lease_owner=tracker.lease_owner,
            status=status_val,
            user_message_id=user_msg_id,
            agent_message_id=agent_msg_id,
            run_id=run_id_val,
            error_status_code=err_code,
            error_detail=err_detail_str,
            extend_lease_seconds=lease_to_extend,
        )
        if not ok or not updated:
            tracker.mark_unhealthy("State modification fenced off by newer worker.")
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Operation lease lost to concurrent worker. State modification fenced off.",
            )
        idemp_rec = updated
        tracker.update_version(updated.version)

    # 3. Stateful execution wrapped in comprehensive failure lifecycle trap
    try:
        # Checkpoint or reuse user message (Deterministic crash recovery)
        target_user_msg_id = (idemp_rec.user_message_id if idemp_rec and idemp_rec.user_message_id else None) or deterministic_user_msg_id
        user_msg = store.get_message(target_user_msg_id) if target_user_msg_id else None

        if not user_msg:
            user_msg = Message(
                message_id=target_user_msg_id or f"msg_{uuid.uuid4().hex[:12]}",
                space_id=space.space_id,
                sender_uid=current_user.uid,
                sender_name=current_user.display_name or current_user.email.split("@")[0],
                role=MessageRole.USER,
                content=payload.content,
                project_tag=tag,
                attachment_file_ids=payload.attachment_file_ids or [],
                client_message_id=payload.client_message_id,
            )
            store.add_message(user_msg)
            _fenced_checkpoint(user_msg_id=user_msg.message_id)

        # No AI call for Shared Space plain messages
        if effective_intent is None:
            _fenced_checkpoint(status_val=ChatIdempotencyStatus.COMPLETED)
            return ChatResponse(user_message=user_msg)

        # Conversation and document_qa → discuss_with_user / answer_document_qa (no Run/Gate created)
        if effective_intent in ("conversation", "document_qa"):
            target_agent_msg_id = (idemp_rec.agent_message_id if idemp_rec and idemp_rec.agent_message_id else None) or deterministic_agent_msg_id
            agent_msg = store.get_message(target_agent_msg_id) if target_agent_msg_id else None

            if not agent_msg:
                # Chronological ascending sort (oldest to newest)
                raw_msgs = store.list_messages(space.space_id, project_tag=tag)
                recent_msgs = sorted(
                    raw_msgs,
                    key=lambda m: (m.created_at or datetime.min.replace(tzinfo=UTC), m.message_id),
                )[-10:]
                citations = []

                # List available committed files in current space
                space_files_list = []
                try:
                    committed_files = store.list_files_in_space(space.space_id, project_tag=tag)
                    space_files_list = [f.filename for f in committed_files if f.upload_status == "committed"]
                except Exception:
                    pass

                # Resolve target files from explicit attachments or persistent context_file_ids
                active_target_file_ids = list(dict.fromkeys((payload.attachment_file_ids or []) + (payload.context_file_ids or [])))
                if not active_target_file_ids:
                    try:
                        all_space_files = store.list_files_in_space(space.space_id)
                        ready_files = [
                            f for f in all_space_files
                            if f.upload_status == "committed"
                            and f.ingestion_status in (IngestionStatus.READY, IngestionStatus.READY_PARTIAL)
                        ]
                        if ready_files:
                            active_target_file_ids = [ready_files[0].file_id]
                    except Exception as e:
                        logger.warning("Failed to auto-resolve space files: %s", e)

                # Macro-analytical queries (breakdowns, scenes, hazards, deliverables) require synthesis rather than single-quote extraction
                is_macro_query = is_macro_analytical_query(payload.content)

                if effective_intent == "document_qa" and not is_macro_query:
                    if qa_candidates:
                        reply_text, citations = AgentBrain.answer_document_qa(
                            user_text=payload.content,
                            space_name=space.name if hasattr(space, "name") else "",
                            tag=tag,
                            candidates=qa_candidates,
                            ocr_warnings=ocr_gap_warnings,
                            chat_history=recent_msgs,
                        )
                    else:
                        from app.services.document_context_service import DocumentContextService
                        target_fids = list(dict.fromkeys((payload.attachment_file_ids or []) + (payload.context_file_ids or [])))
                        if not target_fids:
                            all_space_files = store.list_files_in_space(space.space_id)
                            ready_files = [
                                f for f in all_space_files
                                if f.upload_status == "committed"
                                and f.ingestion_status in (IngestionStatus.READY, IngestionStatus.READY_PARTIAL)
                            ]
                            if ready_files:
                                target_fids = [ready_files[0].file_id]

                        ctx_res = DocumentContextService.build_context_for_discussion(
                            space_id=space.space_id,
                            target_file_ids=target_fids,
                            current_user=current_user,
                            query_text=payload.content,
                            token_budget=14000,
                        )
                        evidence_str = DocumentContextService.format_evidence_for_prompt(ctx_res.evidence_blocks) if ctx_res.is_ready else ""
                        referenced_filename = ctx_res.resolved_documents[0].filename if ctx_res.resolved_documents else None

                        reply_text = AgentBrain.discuss_with_user(
                            user_text=payload.content,
                            space_name=space.name if hasattr(space, "name") else "",
                            space_id=space.space_id,
                            tag=tag,
                            evidence_text=evidence_str,
                            chat_history=recent_msgs,
                            referenced_file=referenced_filename,
                            query_coverage=ctx_res.query_coverage,
                        )
                else:
                    # Prioritize explicitly referenced context_run_id strictly within the current space
                    active_runs = []
                    if payload.context_run_id:
                        referenced_run = store.get_run(payload.context_run_id)
                        if referenced_run and referenced_run.space_id == space.space_id:
                            active_runs.append(referenced_run)
                        elif referenced_run:
                            logger.warning(
                                "Cross-space context run %s ignored (belongs to %s, current space is %s)",
                                payload.context_run_id,
                                referenced_run.space_id,
                                space.space_id,
                            )
                    elif any(kw in payload.content.lower() for kw in ["run", "breakdown", "scene", "場景", "拆解", "衝突", "排程", "schedule", "gate", "審批"]):
                        for r in store.list_runs_in_space(space.space_id, project_tag=tag)[:2]:
                            active_runs.append(r)

                    evidence_str = ""
                    context_ocr_warnings = []
                    coverage_mode = "full"
                    referenced_filename = None

                    if active_target_file_ids:
                        from app.services.document_context_service import DocumentContextService
                        ctx_res = DocumentContextService.build_context_for_discussion(
                            space_id=space.space_id,
                            target_file_ids=active_target_file_ids,
                            current_user=current_user,
                            query_text=payload.content,
                            token_budget=14000,
                        )
                        if ctx_res.is_ready:
                            evidence_str = DocumentContextService.format_evidence_for_prompt(ctx_res.evidence_blocks)
                            context_ocr_warnings = ctx_res.ocr_warnings
                            coverage_mode = ctx_res.query_coverage
                            if ctx_res.resolved_documents:
                                referenced_filename = ctx_res.resolved_documents[0].filename

                    reply_text = AgentBrain.discuss_with_user(
                        user_text=payload.content,
                        space_name=space.name if hasattr(space, "name") else "",
                        space_id=space.space_id,
                        tag=tag,
                        doc_text=doc_text if doc_text != payload.content else "",
                        evidence_text=evidence_str,
                        chat_history=recent_msgs,
                        active_runs=active_runs,
                        referenced_file=referenced_filename,
                        space_files=space_files_list,
                        ocr_warnings=context_ocr_warnings,
                        query_coverage=coverage_mode,
                    )

                proposed_action = None
                if effective_intent != "document_qa" or is_macro_query:
                    action_intent = AgentBrain.extract_action_proposal(
                        user_text=payload.content,
                        available_files=space_files_list,
                    )
                    if action_intent and action_intent.has_proposal:
                        target_sources: list[ActionSourceDescriptor] = []
                        for fid in (active_target_file_ids or action_intent.source_file_ids):
                            f_obj = store.get_file(fid)
                            if f_obj and f_obj.space_id == space.space_id and not f_obj.cleanup_pending:
                                target_sources.append(
                                    ActionSourceDescriptor(
                                        file_id=f_obj.file_id,
                                        active_generation=f_obj.active_generation,
                                        content_hash=f_obj.sha256 or "",
                                        space_id=space.space_id,
                                    )
                                )

                        proposed_action = issue_action_token(
                            space_id=space.space_id,
                            project_tag=tag,
                            user_id=current_user.uid,
                            title=action_intent.title,
                            description=action_intent.description,
                            sources=target_sources,
                            action_type=action_intent.action_type,
                            output_format=action_intent.output_format,
                            metadata={
                                "request_text": payload.content,
                                "deliverable_spec": {
                                    "audience": action_intent.audience,
                                    "purpose": action_intent.purpose,
                                    "layout": action_intent.layout,
                                    "density": action_intent.density,
                                    "editable": action_intent.editable,
                                    "sections": action_intent.sections,
                                    "unknown_value_policy": action_intent.unknown_value_policy,
                                }
                            },
                            ttl_seconds=300,
                        )

                agent_msg = Message(
                    message_id=target_agent_msg_id or f"msg_{uuid.uuid4().hex[:12]}",
                    space_id=space.space_id,
                    sender_uid="agent_studiotower",
                    sender_name="StudioTower Agent",
                    role=MessageRole.AGENT,
                    content=reply_text,
                    citations=citations,
                    project_tag=tag,
                    proposed_action=proposed_action,
                )
                store.add_message(agent_msg)
                _fenced_checkpoint(agent_msg_id=agent_msg.message_id)

            _fenced_checkpoint(
                status_val=ChatIdempotencyStatus.COMPLETED,
                agent_msg_id=agent_msg.message_id,
            )
            return ChatResponse(user_message=user_msg, agent_message=agent_msg)
        # AI Reasoning & Run Checkpoint (Deterministic crash recovery: reuse if exists in storage)
        target_run_id = (idemp_rec.run_id if idemp_rec and idemp_rec.run_id else None) or deterministic_run_id
        run_record = store.get_run(target_run_id) if target_run_id else None

        if not run_record:
            with idempotency_heartbeat(tracker):
                breakdown, telemetry = AgentBrain.analyze_treatment(doc_text, project_tag=tag)


            gate_obj = None
            run_status = RunStatus.COMPLETED

            if breakdown.recommended_gates:
                first_gate = breakdown.recommended_gates[0]
                gate_obj = ApprovalGate(
                    title=first_gate.gate_title,
                    description=first_gate.description,
                    risk_level=first_gate.risk_level,
                    status="pending",
                )
                run_status = RunStatus.AWAITING_APPROVAL

            run_record = Run(
                run_id=target_run_id or f"run_{uuid.uuid4().hex[:12]}",
                space_id=space.space_id,
                project_tag=tag,
                status=run_status,
                prompt=payload.content,
                source_file_id=source_file_id,
                scene_breakdown=breakdown.model_dump(),
                approval_gate=gate_obj,
                created_by=current_user.uid,
                telemetry=telemetry,
            )
            run_record = store.save_run_fenced(
                run_record,
                idemp_key=tracker.key if tracker else None,
                expected_version=tracker.version if tracker else None,
            )
            _fenced_checkpoint(run_id_val=run_record.run_id)

        # Agent Message Checkpoint (Deterministic crash recovery: reuse if exists in storage)
        target_agent_msg_id = (idemp_rec.agent_message_id if idemp_rec and idemp_rec.agent_message_id else None) or deterministic_agent_msg_id
        agent_msg = store.get_message(target_agent_msg_id) if target_agent_msg_id else None

        if not agent_msg:
            breakdown_dict = run_record.scene_breakdown or {}
            num_scenes = len(breakdown_dict.get("scenes", []))
            project_title = breakdown_dict.get("project_title", "Scene Plan")
            summary_text = breakdown_dict.get("summary", "Scene breakdown generated.")
            conflicts = breakdown_dict.get("detected_conflicts", [])

            conflict_summary = (
                f"\n\n⚠️ **Detected Conflicts ({len(conflicts)})**:\n"
                + "\n".join([f"- **{c.get('conflict_type', 'Conflict')}**: {c.get('description', '')}" for c in conflicts])
                if conflicts
                else ""
            )

            gate_summary = ""
            if run_record.approval_gate:
                gate_summary = (
                    f"\n\n🛑 **Approval Gate Required**: `{run_record.approval_gate.title}`\n"
                    f"> *{run_record.approval_gate.description}* (Risk Level: `{run_record.approval_gate.risk_level}`)"
                )

            agent_response_text = (
                f"🎬 **Scene Breakdown Plan Generated** (`{project_title}`)\n"
                f"Extracted **{num_scenes} scenes** for track `#{tag}`.\n\n"
                f"{summary_text}"
                f"{conflict_summary}"
                f"{gate_summary}"
            )

            agent_msg = Message(
                message_id=target_agent_msg_id or f"msg_{uuid.uuid4().hex[:12]}",
                space_id=space.space_id,
                sender_uid="agent_studiotower",
                sender_name="StudioTower Agent",
                role=MessageRole.AGENT,
                content=agent_response_text,
                project_tag=tag,
            )
            store.add_message(agent_msg)
            _fenced_checkpoint(agent_msg_id=agent_msg.message_id)

        # Final transition to COMPLETED
        _fenced_checkpoint(
            status_val=ChatIdempotencyStatus.COMPLETED,
            agent_msg_id=agent_msg.message_id,
            run_id_val=run_record.run_id,
        )

        return ChatResponse(
            user_message=user_msg,
            agent_message=agent_msg,
            run=run_record,
        )
    except StorageConflictError as conflict_err:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Operation lease lost to concurrent worker: {conflict_err}",
        ) from conflict_err
    except HTTPException as http_exc:
        try:
            if tracker and tracker.lease_owner:
                store.update_chat_idempotency_fenced(
                    tracker.key,
                    expected_version=None,
                    expected_lease_owner=tracker.lease_owner,
                    status=ChatIdempotencyStatus.FAILED,
                    error_status_code=http_exc.status_code,
                    error_detail=str(http_exc.detail),
                    extend_lease_seconds=None,
                )
            else:
                _fenced_checkpoint(
                    status_val=ChatIdempotencyStatus.FAILED,
                    err_code=http_exc.status_code,
                    err_detail_str=str(http_exc.detail),
                )
        except Exception as checkpoint_err:
            logger.warning("Failed to record FAILED status for key %s: %s", tracker.key if tracker else "None", checkpoint_err)
        raise
    except Exception as exc:
        trace_id = f"trc_{uuid.uuid4().hex[:12]}"
        logger.exception("Unhandled internal chat exception [%s]: %s", trace_id, exc)
        try:
            if tracker and tracker.lease_owner:
                store.update_chat_idempotency_fenced(
                    tracker.key,
                    expected_version=None,
                    expected_lease_owner=tracker.lease_owner,
                    status=ChatIdempotencyStatus.FAILED,
                    error_status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    error_detail=f"Internal error [trace_id: {trace_id}]",
                    extend_lease_seconds=None,
                )
            else:
                _fenced_checkpoint(
                    status_val=ChatIdempotencyStatus.FAILED,
                    err_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    err_detail_str=f"Internal error [trace_id: {trace_id}]",
                )
        except Exception as checkpoint_err:
            logger.warning("Failed to record FAILED status for key %s: %s", tracker.key if tracker else "None", checkpoint_err)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal server error occurred while processing chat request. Trace ID: {trace_id}",
        ) from exc


@router.get("/spaces/{space_id}/runs", response_model=list[Run])
def list_runs(
    space_id: str,
    tag: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
):
    """List execution runs for a space."""
    SpaceService.get_space_with_auth(space_id, current_user)
    return store.list_runs_in_space(space_id, project_tag=tag)


@router.get("/spaces/{space_id}/runs/{run_id}", response_model=Run)
def get_run(
    space_id: str,
    run_id: str,
    current_user: User = Depends(get_current_user),
):
    """Get single run bundle."""
    SpaceService.get_space_with_auth(space_id, current_user)
    run = store.get_run(run_id)
    if not run or run.space_id != space_id:
        raise HTTPException(status_code=404, detail="Run not found in this Space")
    return run


@router.post("/spaces/{space_id}/actions/confirm")
def confirm_action_proposal(
    space_id: str,
    payload: ActionConfirmationPayload,
    current_user: User = Depends(get_current_user),
):
    """
    Confirm and execute a cryptographically signed ActionProposal.
    Enforces tenant scoping, signature verification, expiration check, per-file generation fencing,
    and server-side CAS idempotency.
    """
    space = SpaceService.get_space_with_auth(space_id, current_user)

    # 1. Locate the proposal in the Space's messages
    messages = store.list_messages(space_id)
    proposal: Optional[ActionProposal] = None
    for m in messages:
        if m.proposed_action and m.proposed_action.action_id == payload.action_id:
            proposal = m.proposed_action
            break

    if not proposal:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="ACTION_PROPOSAL_NOT_FOUND: Proposal not found in this space",
        )

    # 2. Cryptographic signature and expiration verification (256-bit HMAC, exact exp_ts equality)
    if not verify_action_token(proposal, expected_user_id=current_user.uid, expected_space_id=space_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ACTION_PROPOSAL_INVALID_OR_EXPIRED: Token signature is invalid or lease expired",
        )

    # 3. Active Tag Verification (Must not be archived)
    target_tag = proposal.project_tag or "general"
    if target_tag != "general" and target_tag != "all":
        space_tag = next((t for t in space.tags if t.slug == target_tag), None)
        if not space_tag or space_tag.archived:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"ACTION_STALE: Target project track '#{target_tag}' is archived or does not exist",
            )

    # 4. Action Type Allowlist
    ALLOWED_ACTION_TYPES = {
        "create_call_sheet",
        "generate_shot_list",
        "stunt_risk_breakdown",
        "export_production_budget",
        "create_scene_breakdown",
        "create_deliverable",
        "generate_pitch_deck",
    }
    if proposal.action_type not in ALLOWED_ACTION_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"ACTION_TYPE_UNAUTHORIZED: Action type '{proposal.action_type}' is not in the deliverable allowlist",
        )

    allowed_output_formats = {"pdf", "docx", "xlsx", "pptx", "csv", "json"}
    if proposal.output_format is not None and proposal.output_format.value not in allowed_output_formats:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="OUTPUT_FORMAT_UNSUPPORTED: Select pdf, docx, xlsx, pptx, csv, or json",
        )

    # The signed, server-reconstructed presentation contract is authoritative.
    # Reject incompatible format/layout combinations before creating a Run or
    # dispatching a worker; render-time fallback is intentionally forbidden.
    try:
        resolve_deliverable_spec(proposal)
    except DeliverableSpecError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"DELIVERABLE_SPEC_UNSUPPORTED: {exc}",
        ) from exc

    # 5. Per-File Generation and Content Hash Fencing
    sources_to_check = proposal.sources if proposal.sources else [
        ActionSourceDescriptor(file_id=fid, active_generation=1, space_id=space_id)
        for fid in proposal.source_file_ids
    ]
    for src in sources_to_check:
        f_rec = store.get_file(src.file_id)
        if not f_rec or f_rec.space_id != space_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"ACTION_STALE: SOURCE_FILE_DELETED: Source file '{src.file_id}' no longer exists in this space",
            )
        if f_rec.cleanup_pending or f_rec.cleanup_status in ("deleting", "failed"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"ACTION_STALE: SOURCE_FILE_DELETED: Source file '{src.file_id}' is marked for deletion",
            )
        if f_rec.ingestion_status not in (IngestionStatus.READY, IngestionStatus.READY_PARTIAL):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"ACTION_STALE: SOURCE_FILE_NOT_READY: Source file '{src.file_id}' is in state '{f_rec.ingestion_status}'",
            )
        if f_rec.active_generation != src.active_generation:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"ACTION_STALE: SOURCE_FILE_GENERATION_MISMATCH: Source file '{src.file_id}' was modified (gen {f_rec.active_generation} vs expected {src.active_generation})",
            )
        if src.content_hash and f_rec.sha256 and f_rec.sha256 != src.content_hash:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"ACTION_STALE: SOURCE_FILE_HASH_MISMATCH: Source file '{src.file_id}' content was altered",
            )

    # 6. Server-Authoritative CAS Idempotency Check & Snapshot Integrity
    canonical_hash = compute_canonical_proposal_hash(proposal)
    run_id = f"run_act_{hashlib.sha256(proposal.action_id.encode()).hexdigest()[:12]}"
    new_exec = ActionExecutionRecord(
        action_id=proposal.action_id,
        run_id=run_id,
        space_id=space_id,
        project_tag=proposal.project_tag or "general",
        user_id=current_user.uid,
        status=ActionExecutionStatus.PENDING,
        proposal_snapshot=proposal,
        proposal_snapshot_hash=canonical_hash,
        proposal_verified_at=datetime.now(UTC),
    )
    created, target_exec = store.create_action_execution_if_absent(new_exec)

    from app.services.action_runner import get_action_runner
    runner = get_action_runner()

    if not created:
        # Idempotent retry: if dispatch failed, pending, or confirmation pending, attempt recovery dispatch
        if target_exec.dispatch_status in (
            DispatchStatus.PENDING,
            DispatchStatus.FAILED,
            DispatchStatus.DISPATCH_CONFIRMATION_PENDING,
        ):
            lease_token = uuid.uuid4().hex
            claimed_disp, claimed_exec, disp_reason = store.claim_action_dispatch(proposal.action_id, lease_token)
            if claimed_disp:
                task_name = None
                try:
                    task_name = runner.dispatch_action(
                        target_exec.proposal_snapshot or proposal,
                        current_user.uid,
                        dispatch_generation=claimed_exec.dispatch_generation,
                    )
                except Exception as e:
                    store.record_dispatch_failure(proposal.action_id, lease_token, claimed_exec.dispatch_version, str(e))

                if task_name is not None:
                    try:
                        store.record_dispatch_success(proposal.action_id, lease_token, claimed_exec.dispatch_version, task_name)
                    except Exception as succ_err:
                        logger.error(f"Failed to record dispatch success for retry action {proposal.action_id}: {succ_err}")
                        try:
                            store.mark_dispatch_confirmation_uncertain(
                                proposal.action_id, lease_token, claimed_exec.dispatch_version, task_name, str(succ_err)
                            )
                        except Exception as uncert_err:
                            logger.error(
                                f"Failed to mark dispatch uncertain for retry action {proposal.action_id}: {uncert_err}. "
                                "Retaining in DISPATCHING for lease-based recovery without advancing generation."
                            )

        return {
            "status": "confirmed",
            "action_id": proposal.action_id,
            "run_id": target_exec.run_id,
            "execution_status": target_exec.status.value,
            "message": None,
        }

    # 7. Atomic Dispatch via claim_action_dispatch
    lease_token = uuid.uuid4().hex
    claimed_disp, claimed_exec, disp_reason = store.claim_action_dispatch(proposal.action_id, lease_token)
    if claimed_disp:
        task_name = None
        try:
            task_name = runner.dispatch_action(
                proposal,
                current_user.uid,
                dispatch_generation=claimed_exec.dispatch_generation,
            )
        except Exception as e:
            store.record_dispatch_failure(proposal.action_id, lease_token, claimed_exec.dispatch_version, str(e))

        if task_name is not None:
            try:
                store.record_dispatch_success(proposal.action_id, lease_token, claimed_exec.dispatch_version, task_name)
            except Exception as succ_err:
                logger.error(f"Failed to record dispatch success for action {proposal.action_id}: {succ_err}")
                try:
                    store.mark_dispatch_confirmation_uncertain(
                        proposal.action_id, lease_token, claimed_exec.dispatch_version, task_name, str(succ_err)
                    )
                except Exception as uncert_err:
                    logger.error(
                        f"Failed to mark dispatch uncertain for action {proposal.action_id}: {uncert_err}. "
                        "Retaining in DISPATCHING for lease-based recovery without advancing generation."
                    )

    # 8. Create confirmation acknowledgment message in Space
    ack_msg = Message(
        space_id=space_id,
        sender_uid="agent_studiotower",
        sender_name="StudioTower Agent",
        role=MessageRole.AGENT,
        content=f"🚀 Action confirmed! Execution started for: **{proposal.title}** (Run ID: `{run_id}`).",
        project_tag=proposal.project_tag or "general",
        run_id=run_id,
    )
    saved_msg = store.add_message(ack_msg)

    # Link run_id permanently to the original proposal message in store
    try:
        store.update_proposal_message_run_id(space_id, proposal.action_id, run_id)
    except Exception as e:
        logger.warning(f"Failed to link run_id to proposal message: {e}")

    curr_exec = store.get_action_execution(proposal.action_id) or target_exec

    return {
        "status": "confirmed",
        "action_id": proposal.action_id,
        "run_id": run_id,
        "execution_status": curr_exec.status.value,
        "message": saved_msg.model_dump(mode="json"),
    }


class ActionExecuteWorkerRequest(BaseModel):
    space_id: str
    action_id: str
    user_id: str


@router.post("/spaces/{space_id}/actions/{action_id}/execute")
def execute_action_worker_endpoint(
    space_id: str,
    action_id: str,
    request: Request,
    payload: Optional[ActionExecuteWorkerRequest] = None,
    task_secret: Optional[str] = Header(None, alias="X-StudioTower-Task-Secret"),
    traceparent: Optional[str] = Header(None, alias="traceparent"),
):
    """
    Cloud Tasks worker execution callback with dual-secret rotation and snapshot integrity.
    Extracts distributed tracing context (W3C traceparent) to maintain authentic parent-child hierarchy.
    """
    import hmac

    valid_secrets = [settings.STUDIO_TOWER_TASK_SECRET]
    if settings.STUDIO_TOWER_TASK_SECRET_PREVIOUS:
        valid_secrets.append(settings.STUDIO_TOWER_TASK_SECRET_PREVIOUS)

    authed = any(
        hmac.compare_digest(task_secret.encode("utf-8"), s.encode("utf-8"))
        for s in valid_secrets
        if s and task_secret
    )
    if not authed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="INVALID_TASK_SECRET: Cloud Tasks authentication failed",
        )

    if payload:
        if payload.space_id != space_id or payload.action_id != action_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="PAYLOAD_PATH_MISMATCH: Payload space_id or action_id does not match route path",
            )

    exec_rec = store.get_action_execution(action_id)
    if not exec_rec or exec_rec.space_id != space_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Action execution '{action_id}' not found in space '{space_id}'",
        )

    user_id = exec_rec.user_id
    user = store.get_user(user_id) or User(uid=user_id, email=f"{user_id}@system.internal", display_name="Task Worker")

    # Proposal Reconstruction strictly from snapshot with integrity check (fail-closed, no mutable chat fallback)
    proposal = exec_rec.proposal_snapshot
    if not proposal:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="SNAPSHOT_MISSING: Action execution proposal snapshot is missing. Refusing fallback to mutable chat messages.",
        )

    calculated_hash = compute_canonical_proposal_hash(proposal)
    if exec_rec.proposal_snapshot_hash and calculated_hash != exec_rec.proposal_snapshot_hash:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="PROPOSAL_INTEGRITY_COMPROMISED: Snapshot hash does not match canonical payload",
        )

    from app.core.otel import extract_traceparent
    carrier = dict(request.headers)
    if traceparent:
        carrier["traceparent"] = traceparent
    parent_ctx = extract_traceparent(carrier)

    run, final_exec = DeliverableExecutionService.execute_action(
        proposal, user, parent_context=parent_ctx
    )
    return {
        "status": "success",
        "action_id": action_id,
        "run_id": run.run_id if run else None,
        "run_status": run.status.value if run else "noop",
        "execution_status": final_exec.status.value if final_exec else "noop",
    }
