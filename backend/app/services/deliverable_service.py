import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.core.otel import (
    generate_otel_trace_id,
    get_initial_telemetry_status,
    trace_stage,
)
from app.core.telemetry_tenant import compute_tenant_hash
from app.models.action_proposal import (
    ActionExecutionRecord,
    ActionExecutionStatus,
    ActionProposal,
    ActionSourceDescriptor,
    ArtifactDescriptor,
    DeliverableFormat,
)
from app.models.activity import ActivityEvent, ActivityEventType
from app.models.file_record import FileRecord, FileSourceType, IngestionStatus
from app.models.run import ApprovalGate, Run, RunStatus, RunTelemetry
from app.models.user import User
from app.services.storage import StorageConflictError, store
from opentelemetry import trace
from opentelemetry.trace import SpanContext

logger = logging.getLogger("studiotower.deliverable")


class ExecutionHeartbeatTracker:
    """Thread-safe background heartbeat tracker maintaining lease health and state version."""

    def __init__(self, action_id: str, worker_id: str, lease_token: str, initial_version: int):
        self.action_id = action_id
        self.worker_id = worker_id
        self.lease_token = lease_token
        self.current_version = initial_version
        self.lease_healthy = True
        self.failure_code: Optional[str] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def start(self, interval_seconds: float = 10.0, extension_seconds: int = 60):
        def _heartbeat_loop():
            while not self._stop_event.wait(timeout=interval_seconds):
                try:
                    ok, new_ver = store.heartbeat_action_execution(
                        self.action_id,
                        self.worker_id,
                        self.lease_token,
                        extension_seconds=extension_seconds,
                    )
                    with self._lock:
                        if ok:
                            self.current_version = new_ver
                        else:
                            self.lease_healthy = False
                            self.failure_code = "HEARTBEAT_LEASE_LOST"
                            break
                except Exception as e:
                    logger.warning(f"Heartbeat failed for action execution {self.action_id}: {e}")
                    with self._lock:
                        self.lease_healthy = False
                        self.failure_code = str(e)
                    break

        self._thread = threading.Thread(
            target=_heartbeat_loop,
            daemon=True,
            name=f"exec_hb_{self.action_id[:8]}",
        )
        self._thread.start()

    def stop_and_verify(self, timeout: float = 5.0) -> Tuple[bool, int, Optional[str]]:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                with self._lock:
                    self.lease_healthy = False
                    self.failure_code = "HEARTBEAT_JOIN_TIMEOUT"
        with self._lock:
            return self.lease_healthy, self.current_version, self.failure_code


class DeliverableExecutionService:
    @classmethod
    def execute_action(
        cls,
        proposal: ActionProposal,
        actor: User,
        worker_id: Optional[str] = None,
        lease_token: Optional[str] = None,
        lease_duration_seconds: int = 120,
        parent_context: Optional[SpanContext] = None,
    ) -> Tuple[Run, ActionExecutionRecord]:
        """
        Executes an action proposal through the fenced 3-phase lifecycle state machine.
        1. Atomically claims execution lease via storage layer CAS.
        2. Maintains active lease via background heartbeat tracker.
        3. Generates authentic deliverable content derived directly from source document chunks.
        4. Saves physical blob (Phase: uploaded).
        5. Commits metadata FileRecord and ArtifactDescriptor (Phase: committed) with durable crash compensation.
        6. Evaluates risk gate: AWAITING_APPROVAL (critical risk) or COMPLETED.
        """
        now = datetime.now(UTC)
        current_worker = worker_id or f"worker_{uuid.uuid4().hex[:8]}"

        # 1. Storage-Authoritative Atomic Claim (PENDING/retryable FAILED -> RUNNING)
        claimed, exec_rec, reason = store.claim_action_execution(
            proposal.action_id,
            worker_id=current_worker,
            lease_duration_seconds=lease_duration_seconds,
        )
        if not claimed:
            if reason in ("ACTIVE_LEASE_HELD", "TERMINAL_NOOP", "TERMINAL_FAILED", "TERMINAL_EXPIRED"):
                existing_run = store.get_run(exec_rec.run_id) if exec_rec else None
                return existing_run or Run(run_id="noop", space_id=proposal.space_id, created_by=actor.uid), exec_rec
            raise StorageConflictError(f"Action execution claim failed: {reason}")

        current_token = exec_rec.lease_token

        # Start thread-safe heartbeat tracker to continuously renew lease during deliverable generation
        hb_tracker = ExecutionHeartbeatTracker(
            action_id=proposal.action_id,
            worker_id=current_worker,
            lease_token=current_token,
            initial_version=exec_rec.state_version,
        )
        start_time_sec = time.time()
        hb_tracker.start(interval_seconds=10.0, extension_seconds=lease_duration_seconds)

        # Retrieve or initialize linked Run with immutable action_id
        run = store.get_run(exec_rec.run_id)
        if not run:
            run = Run(
                run_id=exec_rec.run_id,
                space_id=proposal.space_id,
                action_id=proposal.action_id,
                project_tag=proposal.project_tag,
                trace_id=generate_otel_trace_id(),
                status=RunStatus.RUNNING,
                prompt=proposal.title,
                source_file_id=proposal.source_file_ids[0] if proposal.source_file_ids else None,
                plan={"title": proposal.title, "description": proposal.description, "action_type": proposal.action_type},
                created_by=actor.uid,
            )
            store.save_run(run)
        else:
            if not run.action_id:
                run.action_id = proposal.action_id
            if not run.trace_id or len(run.trace_id.replace("trc_", "")) != 32:
                run.trace_id = generate_otel_trace_id()
            store.save_run(run)

        space_hash, _ = compute_tenant_hash(proposal.space_id)

        # Phase 3 & Child span: Metadata Commit & Durable Compensation Guard
        artifact_id = "unallocated"
        filename = "unallocated"
        blob_saved = False
        try:
            # Root span: action_execution wraps the full worker execution (E1.5)
            # If parent_context is provided (e.g. from Cloud Tasks), joins distributed trace; otherwise creates true root span.
            with trace_stage(
                name="action_execution",
                space_id_hash=space_hash,
                run_id=run.run_id,
                stage="dispatch",
                action_type=proposal.action_type,
                parent_context=parent_context,
            ):
                # Capture authentic active trace ID (from upstream parent or true root span)
                active_sc = trace.get_current_span().get_span_context()
                if active_sc and active_sc.is_valid:
                    run.trace_id = f"{active_sc.trace_id:032x}"
                    store.save_run(run)
                # 2. Child span: Generate Authentic Deliverable Content
                with trace_stage(
                    name="action_execute_generation",
                    space_id_hash=space_hash,
                    run_id=run.run_id,
                    stage="generation",
                    action_type=proposal.action_type,
                    attributes={"artifact_count": 1},
                ):
                    deterministic_hash = hashlib.sha256(f"{proposal.action_id}_{proposal.action_type}".encode("utf-8")).hexdigest()
                    artifact_id = f"art_{deterministic_hash[:16]}"

                    filename, media_type, content_bytes, is_critical_risk, gate_desc = cls._generate_content_from_sources(
                        proposal, actor
                    )
                    content_hash = hashlib.sha256(content_bytes).hexdigest()
                    size_bytes = len(content_bytes)

                # Phase 2: Upload physical blob
                storage_path = ""
                blob_saved = False
                target_file_id = f"file_art_{artifact_id[4:]}"
                try:
                    clean_name = re.sub(r"[^\w\.-]", "_", filename)
                    storage_rel_path = f"{proposal.space_id}/files/{target_file_id}_{clean_name}"

                    if settings.STUDIO_TOWER_ARTIFACT_BACKEND == "gcs":
                        from google.cloud import storage as gcs
                        client = gcs.Client()
                        bucket_name = settings.STUDIO_TOWER_GCS_BUCKET
                        if not bucket_name or bucket_name == "agentic-cinema-demo-2026-artifacts":
                            bucket_name = "agentic-cinema-demo-2026-studiotower-artifacts"
                        bucket = client.bucket(bucket_name)
                        blob = bucket.blob(storage_rel_path)
                        blob.upload_from_string(content_bytes, content_type=media_type)
                        storage_path = storage_rel_path
                    elif settings.STUDIO_TOWER_ARTIFACT_BACKEND == "local":
                        data_dir_abs = os.path.abspath(settings.STUDIO_TOWER_DATA_DIR)
                        full_path = os.path.abspath(os.path.join(data_dir_abs, storage_rel_path))
                        os.makedirs(os.path.dirname(full_path), exist_ok=True)
                        with open(full_path, "wb") as f:
                            f.write(content_bytes)
                        storage_path = storage_rel_path
                    else:
                        storage_path = storage_rel_path

                    # Keep memory & disk cache for immediate retrieval
                    disk_res = store.save_artifact_blob(
                        space_id=proposal.space_id,
                        artifact_id=artifact_id,
                        filename=filename,
                        data=content_bytes,
                    )
                    store.save_file_blob(target_file_id, content_bytes)
                    if not storage_path:
                        storage_path = disk_res
                    blob_saved = True
                except Exception as e:
                    _, latest_v, _ = hb_tracker.stop_and_verify()
                    logger.error(f"Failed to save artifact blob for {artifact_id}: {e}")
                    exec_rec.status = ActionExecutionStatus.FAILED
                    exec_rec.failure_code = "ERR_BLOB_WRITE_FAILED"
                    store.save_action_execution_fenced(exec_rec, expected_version=latest_v)
                    run.status = RunStatus.FAILED
                    run.failure_code = "ERR_BLOB_WRITE_FAILED"
                    store.save_run(run)
                    raise

                with trace_stage(
                    name="artifact_storage_commit",
                    space_id_hash=space_hash,
                    run_id=run.run_id,
                    stage="storage_commit",
                    action_type=proposal.action_type,
                    attributes={"artifact_count": 1},
                ):
                    # Check heartbeat health before committing
                    hb_ok, latest_ver, hb_err = hb_tracker.stop_and_verify()
                    if not hb_ok:
                        if blob_saved:
                            if settings.STUDIO_TOWER_ARTIFACT_BACKEND == "gcs":
                                try:
                                    from google.cloud import storage as gcs
                                    client = gcs.Client()
                                    bucket_name = settings.STUDIO_TOWER_GCS_BUCKET or "agentic-cinema-demo-2026-studiotower-artifacts"
                                    client.bucket(bucket_name).blob(storage_path).delete()
                                except Exception:
                                    pass
                            store.delete_artifact_blob(proposal.space_id, artifact_id, filename)
                        raise StorageConflictError(f"Execution lease renewal failed during execution: {hb_err}")

                    # Create FileRecord in space file center
                    file_rec = FileRecord(
                        file_id=target_file_id,
                        space_id=proposal.space_id,
                        filename=filename,
                        content_type=media_type,
                        size_bytes=size_bytes,
                        sha256=content_hash,
                        storage_path=storage_path,
                        project_tags=[proposal.project_tag],
                        uploaded_by=actor.uid,
                        source_type=FileSourceType.PDX_ARTIFACT,
                        run_id=run.run_id,
                        upload_status="committed",
                        publication_status="pending_approval" if is_critical_risk else "published",
                        ingestion_status=IngestionStatus.READY,
                        active_generation=1,
                    )
                    store.save_file(file_rec)

                    # Create ArtifactDescriptor
                    visibility = "pending_approval" if is_critical_risk else "published"
                    artifact_desc = ArtifactDescriptor(
                        artifact_id=artifact_id,
                        space_id=proposal.space_id,
                        run_id=run.run_id,
                        filename=filename,
                        media_type=media_type,
                        size_bytes=size_bytes,
                        sha256=content_hash,
                        storage_path=storage_path,
                        download_endpoint=f"/v1/spaces/{proposal.space_id}/artifacts/{artifact_id}/download",
                        visibility=visibility,
                        created_at=now,
                    )
                    store.save_artifact_record(artifact_desc)

                    # Update Run and Execution records
                    if artifact_id not in exec_rec.output_artifact_ids:
                        exec_rec.output_artifact_ids.append(artifact_id)
                    exec_rec.manifest_file_id = file_rec.file_id
                    if artifact_id not in run.output_artifact_ids:
                        run.output_artifact_ids.append(artifact_id)
                    run.manifest_file_id = file_rec.file_id

                    initial_status = get_initial_telemetry_status()
                    elapsed_ms = max(1, int((time.time() - start_time_sec) * 1000))

                    export_now = datetime.now(UTC)
                    run.telemetry_status = initial_status
                    run.telemetry_export_started_at = export_now
                    run.telemetry_attempts = 0
                    run.telemetry_next_check_at = None
                    run.telemetry_last_checked_at = None
                    run.telemetry_lease_token = None
                    run.telemetry_lease_until = None
                    run.telemetry = RunTelemetry(
                        duration_ms=elapsed_ms,
                        tokens_used=None,
                        tool_calls=[proposal.action_type],
                        has_real_telemetry=False,
                        telemetry_status=initial_status,
                        telemetry_export_started_at=export_now,
                        telemetry_attempts=0,
                        telemetry_next_check_at=None,
                        telemetry_last_checked_at=None,
                        telemetry_lease_token=None,
                        telemetry_lease_until=None,
                        ai_engine="studiotower-pdx-generator",
                    )
                    if is_critical_risk:
                        gate = ApprovalGate(
                            gate_id=f"gate_{uuid.uuid4().hex[:8]}",
                            title=f"Approval Gate: {proposal.title}",
                            description=gate_desc,
                            risk_level="critical",
                            status="pending",
                        )
                        run.approval_gate = gate
                        run.status = RunStatus.AWAITING_APPROVAL
                        exec_rec.status = ActionExecutionStatus.AWAITING_APPROVAL

                        def _mut_gate(r_target: Run):
                            r_target.approval_gate = gate
                            r_target.telemetry = run.telemetry
                            r_target.telemetry_status = run.telemetry_status
                            r_target.output_artifact_ids = list(run.output_artifact_ids)
                            r_target.manifest_file_id = run.manifest_file_id

                        updated_r = store.compare_and_swap_run_status(
                            run.run_id,
                            expected_status=RunStatus.RUNNING,
                            new_status=RunStatus.AWAITING_APPROVAL,
                            mutator_fn=_mut_gate,
                            actor_uid=actor.uid,
                        )
                        if updated_r:
                            run = updated_r
                        else:
                            store.save_run(run)
                    else:
                        # Transition to COMPLETED
                        run.status = RunStatus.COMPLETED
                        exec_rec.status = ActionExecutionStatus.COMPLETED

                        def _mut_comp(r_target: Run):
                            r_target.telemetry = run.telemetry
                            r_target.telemetry_status = run.telemetry_status
                            r_target.output_artifact_ids = list(run.output_artifact_ids)
                            r_target.manifest_file_id = run.manifest_file_id

                        updated_r = store.compare_and_swap_run_status(
                            run.run_id,
                            expected_status=RunStatus.RUNNING,
                            new_status=RunStatus.COMPLETED,
                            mutator_fn=_mut_comp,
                            actor_uid=actor.uid,
                        )
                        if updated_r:
                            run = updated_r
                        else:
                            store.save_run(run)
            exec_rec = store.save_action_execution_fenced(
                exec_rec,
                expected_version=latest_ver,
                expected_owner=current_worker,
                expected_token=current_token,
            )

            # Emit atomic Outbox Activity Event
            event_type = (
                ActivityEventType.GATE_REQUESTED
                if is_critical_risk
                else ActivityEventType.RUN_COMPLETED
            )
            summary = (
                f"Action '{proposal.title}' awaiting approval: {gate_desc}"
                if is_critical_risk
                else f"Action '{proposal.title}' successfully completed artifact {filename}"
            )
            store.record_activity_event(
                ActivityEvent(
                    space_id=proposal.space_id,
                    project_tag=proposal.project_tag,
                    event_type=event_type,
                    resource_type="artifact" if not is_critical_risk else "gate",
                    resource_id=artifact_id if not is_critical_risk else (run.approval_gate.gate_id if run.approval_gate else artifact_id),
                    summary=summary,
                    actor_uid=actor.uid,
                    details={
                        "action_id": proposal.action_id,
                        "run_id": run.run_id,
                        "artifact_id": artifact_id,
                        "file_id": (
                            (proposal.source_file_ids[0] if proposal.source_file_ids else "")
                            or (proposal.sources[0].file_id if proposal.sources else "")
                            or (run.source_file_id or "")
                        ),
                        "filename": filename,
                        "sha256": content_hash,
                        "size_bytes": size_bytes,
                    },
                )
            )

            return run, exec_rec

        except Exception as e:
            _, latest_v, _ = hb_tracker.stop_and_verify()
            logger.error(f"Phase 3 Metadata commit failed for {artifact_id}, initiating compensation: {e}")
            if blob_saved:
                deleted = store.delete_artifact_blob(proposal.space_id, artifact_id, filename)
                if not deleted:
                    store.enqueue_artifact_cleanup(
                        space_id=proposal.space_id,
                        artifact_id=artifact_id,
                        filename=filename,
                        reason="METADATA_COMMIT_FAILED",
                    )
                    logger.warning(f"Durable cleanup job enqueued for orphaned artifact {artifact_id}")

            exec_rec.status = ActionExecutionStatus.FAILED
            exec_rec.failure_code = "ERR_METADATA_COMMIT_FAILED"
            try:
                store.save_action_execution_fenced(exec_rec, expected_version=latest_v)
            except Exception:
                pass
            run.status = RunStatus.FAILED
            run.failure_code = "ERR_METADATA_COMMIT_FAILED"
            store.save_run(run)
            raise

    @classmethod
    def reclaim_stalled_action_executions(cls, space_id: Optional[str] = None, max_attempts: int = 3) -> int:
        """
        Storage-authoritative stalled execution recovery for Cloud Run & Firestore.
        Queries stalled executions from storage backend, checks retry bounds, and re-dispatches or terminally fails them.
        """
        stalled_list = store.list_stalled_action_executions(space_id)
        reclaimed_count = 0
        now = datetime.now(UTC)

        for rec in stalled_list:
            if rec.attempts >= max_attempts:
                # Terminal failure
                reclaimed = store.reclaim_action_execution_lease(
                    rec.action_id,
                    lease_token=rec.lease_token,
                    is_terminal=True,
                    failure_code="ERR_LEASE_EXPIRED",
                )
                if reclaimed:
                    reclaimed_count += 1
                    logger.info(f"Terminally failed stalled action execution {rec.action_id}")
            else:
                # Recover and re-dispatch
                reclaimed = store.reclaim_action_execution_lease(
                    rec.action_id,
                    lease_token=rec.lease_token,
                    next_retry_at=now + timedelta(seconds=5),
                    is_terminal=False,
                )
                if reclaimed:
                    reclaimed_count += 1
                    from app.services.action_runner import get_action_runner
                    msgs = store.list_messages(rec.space_id)
                    prop = None
                    for m in reversed(msgs):
                        if m.proposed_action and m.proposed_action.action_id == rec.action_id:
                            prop = m.proposed_action
                            break
                    if prop:
                        try:
                            get_action_runner().dispatch_action(prop, rec.user_id)
                        except Exception as e:
                            logger.error(f"Failed to re-dispatch action {rec.action_id}: {e}")
                    logger.info(f"Reclaimed and re-dispatched stalled action execution {rec.action_id}")
        return reclaimed_count

    @classmethod
    def reclaim_pending_dispatches(cls, space_id: Optional[str] = None) -> int:
        """Recovers and re-dispatches pending, failed, or timed-out action dispatches via CAS."""
        from app.services.action_runner import get_action_runner

        undispatched = store.list_undispatched_actions(space_id)
        recovered_count = 0
        runner = get_action_runner()

        for rec in undispatched:
            prop = rec.proposal_snapshot
            if not prop:
                continue

            recovery_token = f"reconcile_{uuid.uuid4().hex[:8]}"
            claimed, claimed_rec, reason = store.claim_action_dispatch(
                rec.action_id,
                lease_token=recovery_token,
                lease_seconds=120,
            )
            if not claimed or not claimed_rec:
                continue

            try:
                task_name = runner.dispatch_action(
                    prop,
                    user_id=claimed_rec.user_id,
                    dispatch_generation=claimed_rec.dispatch_generation,
                )
            except Exception as e:
                logger.error(f"Failed to enqueue action {rec.action_id} via runner: {e}")
                store.record_dispatch_failure(
                    rec.action_id,
                    lease_token=recovery_token,
                    expected_version=claimed_rec.dispatch_version,
                    error=str(e),
                    is_retryable=True,
                )
                continue

            try:
                success = store.record_dispatch_success(
                    rec.action_id,
                    lease_token=recovery_token,
                    expected_version=claimed_rec.dispatch_version,
                    task_name=task_name,
                )
                if success:
                    recovered_count += 1
                    logger.info(f"Re-dispatched pending action {rec.action_id} as {task_name}")
                else:
                    try:
                        store.mark_dispatch_confirmation_uncertain(
                            rec.action_id,
                            lease_token=recovery_token,
                            expected_version=claimed_rec.dispatch_version,
                            task_name=task_name,
                            error="DISPATCH_SUCCESS_RECORD_REJECTED",
                        )
                    except Exception as uncert_err:
                        logger.error(f"Failed to mark uncertain on reclaim rejection for {rec.action_id}: {uncert_err}")
            except Exception as e:
                logger.error(f"Failed to record dispatch success for action {rec.action_id}: {e}")
                try:
                    store.mark_dispatch_confirmation_uncertain(
                        rec.action_id,
                        lease_token=recovery_token,
                        expected_version=claimed_rec.dispatch_version,
                        task_name=task_name,
                        error=str(e),
                    )
                except Exception as uncert_err:
                    logger.error(f"Failed to mark uncertain on reclaim exception for {rec.action_id}: {uncert_err}")
        return recovered_count

    @classmethod
    def reconcile_approval_commit(
        cls, space_id: str, run_id: str, force_aborted: bool = False
    ) -> Tuple[str, Optional[Run]]:
        """
        Reconcile an approval commit with uncertain outcome.
        Reads all artifacts, files, and run state:
        - If all artifacts & files are published: confirms commit, sets Run to COMPLETED, does not clean up.
        - If still pending_approval and decision lease expired: transitions Run to FAILED, enqueues cleanup jobs.
        """
        run = store.get_run(run_id)
        if not run:
            return "NOT_FOUND", None

        if run.status == RunStatus.COMPLETED and (run.approval_commit_status == "committed" or (run.approval_gate and run.approval_gate.status == "approved")):
            return "ALREADY_COMPLETED", run

        if run.status == RunStatus.FAILED and getattr(run, "approval_commit_status", None) == "aborted":
            return "ALREADY_ABORTED", run

        art_ids = list(run.output_artifact_ids or [])
        if run.manifest_file_id and run.manifest_file_id not in art_ids:
            art_ids.append(run.manifest_file_id)

        if not art_ids:
            ok, updated_run = store.reconcile_run_approval_atomic(
                space_id=space_id,
                run_id=run_id,
                target_status=RunStatus.FAILED,
                approval_commit_status="aborted",
                gate_status="failed",
                expected_run_status=RunStatus.RUNNING,
                failure_code="PDX_EXECUTION_FAILURE",
                error_summary="PDX deterministic artifact generation encountered an internal error.",
            )
            return "NO_ARTIFACTS_FAILED", updated_run or run

        all_published = True
        for art_id in art_ids:
            art = store.get_artifact(space_id, art_id)
            f_rec = store.get_file(art_id)
            art_pub = (art is not None and art.visibility == "published")
            file_pub = (f_rec is not None and f_rec.publication_status == "published")
            if not (art_pub and file_pub):
                all_published = False
                break

        if all_published:
            ok, updated_run = store.reconcile_run_approval_atomic(
                space_id=space_id,
                run_id=run_id,
                target_status=RunStatus.COMPLETED,
                approval_commit_status="committed",
                gate_status="approved",
                expected_run_status=RunStatus.RUNNING,
            )
            if ok:
                logger.info(f"Reconcile confirmed approval commit for run {run_id}")
                return "COMMITTED_PUBLISHED", updated_run or run
            refetched = store.get_run(run_id)
            if refetched and refetched.status == RunStatus.COMPLETED:
                return "ALREADY_COMPLETED", refetched
            if refetched and refetched.status == RunStatus.FAILED:
                return "ALREADY_ABORTED", refetched
            return "CONFLICT", refetched or run

        # Staging artifacts uncommitted
        # Check if decision lease is still active
        now = datetime.now(UTC)
        gate = run.approval_gate
        is_lease_active = False
        if not force_aborted and gate and gate.status == "approving" and gate.decision_lease_token:
            if gate.decision_lease_until:
                if gate.decision_lease_until > now:
                    is_lease_active = True
            elif getattr(run, "uncertain_since", None):
                if run.uncertain_since + timedelta(seconds=30) > now:
                    is_lease_active = True
            elif getattr(run, "approval_commit_status", None) != "uncertain":
                is_lease_active = True

        if is_lease_active:
            # Active approval in flight
            return "IN_PROGRESS", run

        # Gate decision expired or aborted: mark failed and enqueue durable cleanup atomically
        cleanup_items = []
        for art_id in art_ids:
            art = store.get_artifact(space_id, art_id)
            fname = art.filename if art else f"{art_id}.bin"
            cleanup_items.append({
                "artifact_id": art_id,
                "filename": fname,
                "reason": "APPROVAL_TRANSACTION_ABORTED",
                "staging_version": run.state_version,
            })

        ok, updated_run = store.reconcile_run_approval_atomic(
            space_id=space_id,
            run_id=run_id,
            target_status=RunStatus.FAILED,
            approval_commit_status="aborted",
            gate_status="failed",
            expected_run_status=RunStatus.RUNNING,
            failure_code="APPROVAL_TRANSACTION_ABORTED",
            error_summary="Approval transaction failed or aborted.",
            cleanup_items=cleanup_items,
        )
        if ok:
            logger.info(f"Reconcile aborted approval commit for run {run_id}, enqueued cleanups")
            return "ABORTED_CLEANUP_ENQUEUED", updated_run or run
        refetched = store.get_run(run_id)
        if refetched and refetched.status == RunStatus.FAILED and getattr(refetched, "approval_commit_status", None) == "aborted":
            return "ALREADY_ABORTED", refetched
        if refetched and refetched.status == RunStatus.COMPLETED:
            return "ALREADY_COMPLETED", refetched
        return "CONFLICT", refetched or run

    @classmethod
    def scan_and_reconcile_stalled_approvals(cls, space_id: Optional[str] = None) -> int:
        """
        Scans for Runs in RUNNING status with expired approval decision leases or timed-out uncertain states.
        Fences and reconciles them atomically to FAILED with durable cleanup intents.
        """
        now = datetime.now(UTC)
        stalled_runs = store.list_stalled_approval_runs(space_id=space_id, now=now)
        reconciled_count = 0
        for r in stalled_runs:
            try:
                status_res, _ = cls.reconcile_approval_commit(r.space_id, r.run_id, force_aborted=False)
                if status_res in ("ABORTED_CLEANUP_ENQUEUED", "COMMITTED_PUBLISHED"):
                    reconciled_count += 1
            except Exception as e:
                logger.error(f"Error reconciling stalled approval for run {r.run_id}: {e}")
        return reconciled_count

    @classmethod
    def sweep_artifact_cleanups(cls, batch_size: int = 20) -> int:
        """Sweeper job that cleans up orphaned or uncommitted artifact blobs."""
        pending_jobs = store.list_pending_artifact_cleanups()
        swept_count = 0
        worker_id = f"sweeper_{uuid.uuid4().hex[:8]}"

        for job in pending_jobs[:batch_size]:
            job_id = job.get("job_id")
            claimed = store.claim_artifact_cleanup_job(job_id, worker_id=worker_id, lease_seconds=60)
            if not claimed:
                continue

            space_id = claimed["space_id"]
            artifact_id = claimed["artifact_id"]
            filename = claimed["filename"]
            lease_token = claimed.get("lease_token")
            expected_ver = claimed.get("version")

            # SAFETY GUARD: verify publication status before deletion!
            art_desc = store.get_artifact(space_id, artifact_id)
            file_rec = store.get_file(artifact_id)
            if (art_desc and art_desc.visibility == "published") or (file_rec and file_rec.publication_status == "published"):
                logger.info(f"Artifact {artifact_id} is published. Cancelling cleanup job {job_id}.")
                store.cancel_artifact_cleanup_job(job_id, reason="ALREADY_PUBLISHED")
                continue

            try:
                # Synchronously delete artifact blob and verify actual deletion
                deleted = store.delete_artifact_blob(space_id, artifact_id, filename)
                if not deleted:
                    store.record_cleanup_failure(
                        job_id,
                        worker_id=worker_id,
                        error="BLOB_DELETION_RETURNED_FALSE",
                        lease_token=lease_token,
                        expected_version=expected_ver,
                    )
                    logger.warning(f"Blob deletion returned False for job {job_id}")
                else:
                    completed = store.complete_artifact_cleanup(
                        job_id,
                        worker_id=worker_id,
                        lease_token=lease_token,
                        expected_version=expected_ver,
                    )
                    if completed:
                        swept_count += 1
                        logger.info(f"Cleaned up orphaned artifact {artifact_id} ({filename})")
                    else:
                        logger.warning(f"Cleanup completion rejected (fenced/expired) for job {job_id}")
            except Exception as e:
                logger.warning(f"Cleanup failed for job {job_id}: {e}")
                store.record_cleanup_failure(
                    job_id,
                    worker_id=worker_id,
                    error=str(e),
                    lease_token=lease_token,
                    expected_version=expected_ver,
                )

        return swept_count

    @classmethod
    def _extract_source_document_data(cls, proposal: ActionProposal) -> Dict[str, Any]:
        """
        Extracts structured screenplay and production data directly from fenced source file document chunks.
        Extracts: real scenes, sluglines, characters, stunts/hazards, dialogue snippets, and provenance metadata.
        """
        sources_to_query: List[ActionSourceDescriptor] = list(proposal.sources) if proposal.sources else []
        if not sources_to_query and proposal.source_file_ids:
            for fid in proposal.source_file_ids:
                f_obj = store.get_file(fid)
                if f_obj:
                    sources_to_query.append(
                        ActionSourceDescriptor(
                            file_id=f_obj.file_id,
                            active_generation=f_obj.active_generation,
                            content_hash=f_obj.sha256 or "",
                            space_id=proposal.space_id,
                        )
                    )

        extracted_text_blocks: List[str] = []
        source_provenance: List[Dict[str, Any]] = []

        for src in sources_to_query:
            chunks = store.get_document_chunks(proposal.space_id, src.file_id, src.active_generation)
            if chunks:
                # normalized_text is the canonical basis shared by retrieval,
                # citations, and deliverable rendering.
                chunk_texts = [c.normalized_text for c in chunks if c.normalized_text]
                extracted_text_blocks.extend(chunk_texts)
                source_provenance.append({
                    "file_id": src.file_id,
                    "generation": src.active_generation,
                    "chunk_count": len(chunks),
                    "sha256": src.content_hash,
                })
            else:
                f_rec = store.get_file(src.file_id)
                if f_rec:
                    source_provenance.append({
                        "file_id": src.file_id,
                        "generation": src.active_generation,
                        "filename": f_rec.filename,
                        "sha256": src.content_hash or f_rec.sha256,
                    })

        full_text = "\n".join(extracted_text_blocks)

        # Parse only the canonical screenplay portion of each slugline. Notes
        # after DAY/NIGHT (stage, unit, LED volume, rigs) are production
        # elements and must not contaminate scene identity or location.
        slug_text = re.sub(r"(?im)^\s*slugline:\s*", "", full_text)
        slugline_pattern = re.compile(
            r"^(?:(?:SCENE|Scene)\s+(?P<number>[A-Z0-9.-]+)\s*:?\s*)?"
            r"(?P<prefix>INT/EXT\.|INT\.|EXT\.|I/E\.)\s+"
            r"(?P<body>.*?\b(?P<time>DAY|NIGHT|DAWN|DUSK)\b)"
            r"(?:\s*\|\s*(?P<notes>.*))?$",
            re.IGNORECASE | re.MULTILINE,
        )
        found_sluglines = list(slugline_pattern.finditer(slug_text))
        scenes: List[Dict[str, Any]] = []
        is_ungrounded = False

        if found_sluglines:
            for ordinal, match in enumerate(found_sluglines[:40], 1):
                scene_number = (match.group("number") or str(ordinal)).upper()
                prefix = match.group("prefix").upper()
                body = re.sub(r"\s+", " ", match.group("body").strip()).upper()
                day_night = match.group("time").upper()
                clean_slug = f"{prefix} {body}"
                loc = re.sub(rf"\s*-\s*{re.escape(day_night)}\s*$", "", body).strip(" -|")
                scenes.append({
                    "scene_number": scene_number,
                    "slugline": clean_slug,
                    "day_night": day_night,
                    "location": loc or "TBD / REVIEW",
                })
        else:
            is_ungrounded = True

        # 2. Parse character cues only from standalone screenplay cue lines. Do
        # not treat every uppercase word in treatments, metadata, or technical
        # notes as cast; that polluted every output format with project terms.
        unique_chars = []
        if not is_ungrounded:
            excluded_words = {
                "INT", "EXT", "DAY", "NIGHT", "SCENE", "CUT", "FADE", "CONTINUED",
                "TITLE", "THE", "AND", "WITH", "FROM", "PAGE", "PROJECT", "PRODUCTION",
                "BREAKDOWN", "DELIVERABLE", "STATUS", "DATE", "ISSUE", "REVISION", "SOURCE",
                "TECHNICAL", "CAMERA", "DEPARTMENT", "DIRECTIVES", "SAFETY", "MATRIX",
            }
            lines = full_text.splitlines()
            production_terms = {
                "AGENT", "AGENTS", "AI", "APPROVAL", "CAMERA", "COMPLIANCE", "CONTROL",
                "COORDINATOR", "DEPARTMENT", "DETERMINISTIC", "DIRECTIVE", "DIRECTIVES",
                "DOMAIN", "GATE", "GATES", "LED", "MANDATORY", "MCP", "OBSERVABILITY",
                "PIPELINE", "PLATFORM", "PROTOCOL", "RIG", "RIGGING", "RISK", "SAFETY",
                "SECURITY", "SHOWRUNNER", "SIGN-OFF", "STAGE", "TECHNICAL", "UNIT", "VFX",
                "VOLUME", "WORKFLOW",
            }
            for line_index, raw_line in enumerate(lines):
                candidate = raw_line.strip().strip(":")
                words = candidate.split()
                if not (1 <= len(words) <= 4 and 2 <= len(candidate) <= 32):
                    continue
                if candidate != candidate.upper() or not re.fullmatch(r"[A-Z][A-Z .'-]*", candidate):
                    continue
                tokens = {word.strip(".'-") for word in words}
                if tokens & (excluded_words | production_terms) or any(
                    marker in candidate for marker in ("INT.", "EXT.", "SCENE ")
                ):
                    continue
                next_line = next((line.strip() for line in lines[line_index + 1:] if line.strip()), "")
                if not next_line or next_line == next_line.upper():
                    continue
                if candidate not in unique_chars:
                    unique_chars.append(candidate)
                if len(unique_chars) >= 12:
                    break
            # Treatments often introduce a character inline using an uppercase
            # name followed by prose (for example, ``HANNAH leaps``). This is
            # narrower than collecting every uppercase token and preserves
            # useful cast evidence without admitting metadata keyword lists.
            if len(unique_chars) < 12:
                for inline_name in re.findall(r"\b([A-Z][A-Z'-]{1,31})\s+(?=[a-z])", full_text):
                    if inline_name not in excluded_words and inline_name not in unique_chars:
                        unique_chars.append(inline_name)
                    if len(unique_chars) >= 12:
                        break

        # 3. Detect Stunt & Hazard Keywords from Source Text
        stunt_keywords = ["fall", "wire", "jump", "fight", "explosion", "fire", "crash", "stunt", "airbag", "high speed", "hazard", "rigging"]
        stunts_found = []
        lower_text = full_text.lower()
        for kw in stunt_keywords:
            if kw in lower_text:
                stunts_found.append(kw)

        department_patterns = {
            "Stunts / Safety": r"\b(stunt|wire|rigging|fall|fight|airbag|hazard)\b",
            "VFX / Virtual Production": r"\b(vfx|visual effects?|led volume|virtual production)\b",
            "SFX": r"\b(sfx|special effects?|explosion|practical fire)\b",
            "Props": r"\b(props?|hand prop|hero prop)\b",
            "Wardrobe": r"\b(wardrobe|costume)\b",
            "Hair / Makeup": r"\b(hair|makeup|prosthetic)\b",
            "Vehicles": r"\b(vehicle|car|truck|motorcycle|aircraft)\b",
            "Background / Extras": r"\b(extras?|background performers?|crowd)\b",
            "Animals": r"\b(animal|dog|horse|livestock)\b",
            "Special Equipment": r"\b(aerial unit|splinter unit|wire gantry|crane|drone)\b",
        }
        production_elements = [
            label for label, pattern in department_patterns.items()
            if re.search(pattern, lower_text, re.IGNORECASE)
        ]

        # Scope cast and production elements to the scene segment between this
        # slugline and the next. Global lists remain useful for summaries, but
        # scene breakdowns must not copy unrelated evidence into every scene.
        if found_sluglines:
            for scene_index, (scene, match) in enumerate(zip(scenes, found_sluglines[:8])):
                segment_end = (
                    found_sluglines[scene_index + 1].start()
                    if scene_index + 1 < min(len(found_sluglines), 8)
                    else len(full_text)
                )
                segment = full_text[match.end():segment_end]
                segment_lines = segment.splitlines()
                scene_chars: List[str] = []
                for line_index, raw_line in enumerate(segment_lines):
                    candidate = raw_line.strip().strip(":")
                    words = candidate.split()
                    if not (1 <= len(words) <= 4 and 2 <= len(candidate) <= 32):
                        continue
                    if candidate != candidate.upper() or not re.fullmatch(r"[A-Z][A-Z .'-]*", candidate):
                        continue
                    tokens = {word.strip(".'-") for word in words}
                    if tokens & (excluded_words | production_terms):
                        continue
                    next_line = next(
                        (line.strip() for line in segment_lines[line_index + 1:] if line.strip()),
                        "",
                    )
                    if not next_line or next_line == next_line.upper():
                        continue
                    if candidate not in scene_chars:
                        scene_chars.append(candidate)
                for inline_name in re.findall(r"\b([A-Z][A-Z'-]{1,31})\s+(?=[a-z])", segment):
                    if (
                        inline_name not in excluded_words
                        and inline_name not in production_terms
                        and inline_name not in scene_chars
                    ):
                        scene_chars.append(inline_name)
                scene["characters"] = scene_chars[:12]
                scene_lower = f"{match.group('notes') or ''}\n{segment}".lower()
                scene["production_elements"] = [
                    label for label, pattern in department_patterns.items()
                    if re.search(pattern, scene_lower, re.IGNORECASE)
                ]

        return {
            "full_text": full_text,
            "scenes": scenes,
            "characters": unique_chars,
            "stunts_found": stunts_found,
            "production_elements": production_elements,
            "provenance": source_provenance,
            "is_ungrounded": is_ungrounded,
        }

    @classmethod
    def _build_pdf_budget(cls, proposal: ActionProposal, actor: User, extracted: Dict[str, Any]) -> bytes:
        import io

        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

        try:
            pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
            font_name = "STSong-Light"
        except Exception:
            font_name = "Helvetica"

        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            "BudgetPdfTitle",
            parent=styles["Heading1"],
            fontName=font_name,
            fontSize=16,
            leading=20,
            textColor=colors.HexColor("#0f172a"),
            spaceAfter=4,
        )
        subtitle_style = ParagraphStyle(
            "BudgetPdfSubtitle",
            parent=styles["Normal"],
            fontName=font_name,
            fontSize=9,
            leading=13,
            textColor=colors.HexColor("#475569"),
            spaceAfter=10,
        )
        cell_style = ParagraphStyle(
            "BudgetPdfCell",
            parent=styles["Normal"],
            fontName=font_name,
            fontSize=8,
            leading=11,
            textColor=colors.HexColor("#1e293b"),
        )
        cell_bold = ParagraphStyle(
            "BudgetPdfCellBold",
            parent=cell_style,
            fontName=font_name,
            fontSize=8,
            leading=11,
            textColor=colors.HexColor("#0f172a"),
        )

        buf = io.BytesIO()
        doc = SimpleDocTemplate(
            buf,
            pagesize=letter,
            leftMargin=36,
            rightMargin=36,
            topMargin=36,
            bottomMargin=36,
        )
        elements = []

        elements.append(Paragraph(f"STUDIOTOWER PRODUCTION BUDGET: {proposal.title}", title_style))
        elements.append(Paragraph(
            f"Space: {proposal.space_id} | Track: #{proposal.project_tag} | Prepared By: {actor.display_name} | Date: {datetime.now(UTC).strftime('%Y-%m-%d')} | Currency: USD",
            subtitle_style,
        ))

        scenes = extracted.get("scenes", [])
        chars = extracted.get("characters", [])
        scene_count = max(1, len(scenes))
        cast_count = max(2, len(chars))

        items = [
            ("1001", "Above The Line", "Directing & Production Supervision", "1", "$120,000.00", "$120,000.00", 120000.0),
            ("2001", "Cast & Talent", f"Principal Cast Compensation ({cast_count} roles)", str(cast_count), "$15,000.00", f"${cast_count * 15000:,.2f}", cast_count * 15000.0),
            ("3001", "Production Crew", "Camera Operator & Grip Department", str(scene_count * 2), "$1,200.00", f"${scene_count * 2400:,.2f}", scene_count * 2400.0),
            ("4001", "Equipment & Stage", "Camera, Lighting & Grip Package", str(scene_count), "$4,500.00", f"${scene_count * 4500:,.2f}", scene_count * 4500.0),
            ("5001", "Location & Permits", f"Location Fees ({scene_count} locations)", str(scene_count), "$3,500.00", f"${scene_count * 3500:,.2f}", scene_count * 3500.0),
        ]
        total_sum = sum(row[6] for row in items)

        table_data = [
            ["Account Code", "Category", "Description", "Qty", "Rate", "Total USD"]
        ]
        for row in items:
            table_data.append([
                Paragraph(row[0], cell_style),
                Paragraph(row[1], cell_bold),
                Paragraph(row[2], cell_style),
                Paragraph(row[3], cell_style),
                Paragraph(row[4], cell_style),
                Paragraph(row[5], cell_bold),
            ])

        table_data.append([
            Paragraph("TOTAL", cell_bold),
            Paragraph("", cell_style),
            Paragraph("Estimated Total Production Budget", cell_bold),
            Paragraph("", cell_style),
            Paragraph("", cell_style),
            Paragraph(f"${total_sum:,.2f}", cell_bold),
        ])

        t_budget = Table(table_data, colWidths=[65, 105, 190, 40, 70, 70])
        t_budget.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e293b")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), font_name),
            ("FONTSIZE", (0, 0), (-1, 0), 8),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
            ("TOPPADDING", (0, 0), (-1, 0), 6),
            ("ALIGN", (0, 0), (-1, -1), "LEFT"),
            ("ALIGN", (3, 0), (5, -1), "RIGHT"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
            ("BACKGROUND", (0, 1), (-1, -2), colors.HexColor("#ffffff")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.HexColor("#ffffff"), colors.HexColor("#f8fafc")]),
            ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#e2e8f0")),
            ("LINEABOVE", (0, -1), (-1, -1), 1.5, colors.HexColor("#0f172a")),
            ("TOPPADDING", (0, -1), (-1, -1), 6),
            ("BOTTOMPADDING", (0, -1), (-1, -1), 6),
        ]))
        elements.append(t_budget)
        elements.append(Spacer(1, 16))

        # Signatures / Approval Block
        sig_data = [
            ["Producer Approval Signature", "Production Accountant", "Studio Executive Signoff"],
            ["\n___________________________\nDate:", "\n___________________________\nDate:", "\n___________________________\nDate:"],
        ]
        t_sig = Table(sig_data, colWidths=[180, 180, 180])
        t_sig.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), font_name),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#475569")),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        elements.append(t_sig)

        doc.build(elements)
        return buf.getvalue()

    @classmethod
    def _build_pdf_call_sheet(cls, proposal: ActionProposal, actor: User, extracted: Dict[str, Any]) -> bytes:
        import io

        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

        try:
            pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
            font_name = "STSong-Light"
        except Exception:
            font_name = "Helvetica"

        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            "CallSheetTitle",
            parent=styles["Heading1"],
            fontName=font_name,
            fontSize=16,
            leading=20,
            textColor=colors.HexColor("#1a1a2e"),
            spaceAfter=6,
        )
        norm_style = ParagraphStyle(
            "CallSheetNormal",
            parent=styles["Normal"],
            fontName=font_name,
            fontSize=9,
            leading=12,
            textColor=colors.HexColor("#333333"),
        )
        bold_style = ParagraphStyle(
            "CallSheetBold",
            parent=norm_style,
            fontName=font_name,
            fontSize=10,
            leading=14,
            textColor=colors.HexColor("#1a1a2e"),
        )

        buf = io.BytesIO()
        doc = SimpleDocTemplate(
            buf,
            pagesize=letter,
            leftMargin=36,
            rightMargin=36,
            topMargin=36,
            bottomMargin=36,
        )
        elements = []

        elements.append(Paragraph(f"STUDIOTOWER PRODUCTION CALL SHEET: {proposal.title}", title_style))
        elements.append(Paragraph(f"Space: {proposal.space_id} | Track: #{proposal.project_tag} | Prepared by: {actor.display_name} | Call Time: 07:00 AM", norm_style))
        elements.append(Spacer(1, 10))

        # Metadata table
        meta_data = [
            ["Call Date", datetime.now(UTC).strftime("%Y-%m-%d"), "Sunrise", "06:12 AM", "Sunset", "07:45 PM"],
            ["Nearest Hospital", "Mercy Regional Medical Center (Tel: 911)", "Weather", "Fair / Clear 24°C", "Sound Stage", "Main Production Unit"],
        ]
        t_meta = Table(meta_data, colWidths=[100, 170, 80, 100, 80, 90])
        t_meta.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f1f4f8")),
            ("FONTNAME", (0, 0), (-1, -1), font_name),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ]))
        elements.append(t_meta)
        elements.append(Spacer(1, 12))

        # Cast Table
        elements.append(Paragraph("CAST & TALENT LINEUP", bold_style))
        cast_headers = ["Role / Character", "Talent Status", "Pickup", "Call Time", "On Set"]
        cast_rows = [cast_headers]
        chars = extracted.get("characters", [])
        if chars:
            for i, c in enumerate(chars[:6]):
                pickup = f"0{6 + (i // 2)}:{15 * (i % 4):02d} AM"
                call_t = f"0{7 + (i // 2)}:{15 * (i % 4):02d} AM"
                cast_rows.append([c, "On Call", pickup, call_t, "08:30 AM"])
        else:
            cast_rows.append(["[Principal Cast 1]", "On Call", "06:30 AM", "07:30 AM", "08:30 AM"])
            cast_rows.append(["[Supporting Cast 2]", "On Call", "07:00 AM", "08:00 AM", "09:00 AM"])

        t_cast = Table(cast_rows, colWidths=[180, 100, 80, 80, 80])
        t_cast.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e293b")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
            ("FONTNAME", (0, 0), (-1, -1), font_name),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ]))
        elements.append(t_cast)
        elements.append(Spacer(1, 12))

        # Scene Schedule Table
        elements.append(Paragraph("SCENE SHOOTING SCHEDULE", bold_style))
        scene_headers = ["Scene #", "Slugline / Setting", "D/N", "Pages", "Location / Notes"]
        scene_rows = [scene_headers]
        scenes = extracted.get("scenes", [])
        if scenes:
            for s in scenes[:8]:
                scene_rows.append([f"Scene {s.get('scene_number', 1)}", s.get("slugline", "EXT. SET - DAY"), s.get("day_night", "DAY"), "2 3/8", s.get("location", "Soundstage")])
        else:
            scene_rows.append(["Scene 1", f"EXT. {proposal.title.upper()} - DAY", "DAY", "3 1/8", "Main Unit Stage"])

        t_scenes = Table(scene_rows, colWidths=[60, 240, 50, 50, 120])
        t_scenes.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f766e")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
            ("FONTNAME", (0, 0), (-1, -1), font_name),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ]))
        elements.append(t_scenes)
        elements.append(Spacer(1, 12))

        # Provenance footer table
        elements.append(Paragraph("DATA INTEGRITY & PROVENANCE (PRODOCUX / PDX-ENGINE)", bold_style))
        prov = extracted.get("provenance", [])
        prov_rows = [["Source File ID", "Generation", "SHA-256 Checksum", "Ingestion Status"]]
        for p in prov:
            prov_rows.append([p.get("file_id", "source"), f"Gen {p.get('generation', 1)}", p.get("sha256", "verified")[:24] + "...", "VERIFIED"])
        if not prov:
            prov_rows.append(["Ungrounded Template", "Gen 1", proposal.action_id[:24] + "...", "TEMPLATE_MODE"])

        t_prov = Table(prov_rows, colWidths=[140, 70, 210, 100])
        t_prov.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#334155")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
            ("FONTNAME", (0, 0), (-1, -1), font_name),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ]))
        elements.append(t_prov)

        doc.build(elements)
        return buf.getvalue()

    @classmethod
    def _build_pdf_scene_breakdown(cls, proposal: ActionProposal, actor: User, extracted: Dict[str, Any]) -> bytes:
        import io

        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

        try:
            pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
            font_name = "STSong-Light"
        except Exception:
            font_name = "Helvetica"

        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            "BreakdownTitle",
            parent=styles["Heading1"],
            fontName=font_name,
            fontSize=16,
            leading=20,
            textColor=colors.HexColor("#1e293b"),
            spaceAfter=6,
        )
        norm_style = ParagraphStyle(
            "BreakdownNormal",
            parent=styles["Normal"],
            fontName=font_name,
            fontSize=9,
            leading=12,
            textColor=colors.HexColor("#333333"),
        )
        bold_style = ParagraphStyle(
            "BreakdownBold",
            parent=norm_style,
            fontName=font_name,
            fontSize=10,
            leading=14,
            textColor=colors.HexColor("#1e293b"),
        )

        buf = io.BytesIO()
        doc = SimpleDocTemplate(
            buf,
            pagesize=letter,
            leftMargin=36,
            rightMargin=36,
            topMargin=36,
            bottomMargin=36,
            title=f"STUDIOTOWER SCENE BREAKDOWN: {proposal.title}",
        )
        elements = []

        elements.append(Paragraph(f"STUDIOTOWER SCENE BREAKDOWN REPORT: {proposal.title}", title_style))
        elements.append(Paragraph(f"Space: {proposal.space_id} | Track: #{proposal.project_tag} | Prepared by: {actor.display_name} | Date: {datetime.now(UTC).strftime('%Y-%m-%d')}", norm_style))
        elements.append(Spacer(1, 10))

        # Scene Details Table
        elements.append(Paragraph("SCENE INDEX & SETTINGS", bold_style))
        scene_headers = ["#", "Slugline / Setting", "D/N", "Location", "Cast"]
        scene_rows = [scene_headers]
        scenes = extracted.get("scenes", [])
        if scenes:
            for ordinal, s in enumerate(scenes, 1):
                scene_chars = s.get("characters") or extracted.get("characters") or []
                cast_label = ", ".join(scene_chars[:6]) if scene_chars else "TBD / REVIEW"
                scene_rows.append([
                    Paragraph(str(s.get("scene_number") or ordinal), norm_style),
                    Paragraph(s.get("slugline") or "TBD", norm_style),
                    Paragraph(s.get("day_night") or "TBD", norm_style),
                    Paragraph(s.get("location") or "TBD", norm_style),
                    Paragraph(cast_label, norm_style),
                ])
        else:
            scene_rows.append([
                Paragraph("1", norm_style),
                Paragraph(f"EXT. {proposal.title.upper()} - DAY", norm_style),
                Paragraph("DAY", norm_style),
                Paragraph("Main Location", norm_style),
                Paragraph("Principal Cast", norm_style),
            ])

        t_scenes = Table(scene_rows, colWidths=[40, 220, 50, 120, 110])
        t_scenes.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e3a8a")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
            ("FONTNAME", (0, 0), (-1, -1), font_name),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        elements.append(t_scenes)
        elements.append(Spacer(1, 12))

        # Stunt & Safety Highlights
        elements.append(Paragraph("SAFETY, STUNT & RIGGING HIGHLIGHTS", bold_style))
        stunts = extracted.get("stunts_found", [])
        stunt_text = f"Identified Action & Safety Cues: {', '.join(stunts)}" if stunts else "No high-hazard stunts identified for these scenes."
        elements.append(Paragraph(stunt_text, norm_style))
        elements.append(Spacer(1, 12))

        # Provenance footer
        elements.append(Paragraph("DATA INTEGRITY & PROVENANCE (PRODOCUX / PDX-ENGINE)", bold_style))
        prov = extracted.get("provenance", [])
        prov_rows = [["Source File ID", "Generation", "SHA-256 Checksum", "Ingestion Status"]]
        for p in prov:
            prov_rows.append([p.get("file_id", "source"), f"Gen {p.get('generation', 1)}", p.get("sha256", "verified")[:24] + "...", "VERIFIED"])
        if not prov:
            prov_rows.append(["Ungrounded Template", "Gen 1", proposal.action_id[:24] + "...", "TEMPLATE_MODE"])

        t_prov = Table(prov_rows, colWidths=[140, 70, 210, 100])
        t_prov.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#334155")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
            ("FONTNAME", (0, 0), (-1, -1), font_name),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ]))
        elements.append(t_prov)

        doc.build(elements)
        return buf.getvalue()

    @classmethod
    def _build_csv_scene_breakdown(cls, proposal: ActionProposal, actor: User, extracted: Dict[str, Any]) -> bytes:
        scenes = extracted.get("scenes", [])
        characters = extracted.get("characters", [])
        target_scenes = scenes if scenes else [{"scene_number": 1, "slugline": f"EXT. {proposal.title.upper()} - DAY", "day_night": "DAY", "location": "Main Stage"}]
        rows = [
            ["STUDIOTOWER SCENE BREAKDOWN TABLE", f"Space: {proposal.space_id}", f"Track: #{proposal.project_tag}"],
            ["Title", proposal.title, "Prepared By", actor.display_name, "Date", datetime.now(UTC).strftime("%Y-%m-%d")],
            [],
            ["Scene #", "Slugline / Setting", "D/N", "Location", "Cast / Characters", "Notes"],
        ]
        for sc in target_scenes:
            cast_str = ", ".join(characters[:3]) if characters else "Principal Performer"
            rows.append([
                f"Scene {sc.get('scene_number', 1)}",
                sc.get("slugline", "EXT. SET - DAY"),
                sc.get("day_night", "DAY"),
                sc.get("location", "Main Stage"),
                cast_str,
                "Principal coverage",
            ])
        csv_text = "\n".join([",".join([f'"{cell}"' if "," in str(cell) else str(cell) for cell in row]) for row in rows])
        return csv_text.encode("utf-8")

    @classmethod
    def _build_xlsx_scene_breakdown(cls, proposal: ActionProposal, actor: User, extracted: Dict[str, Any]) -> bytes:
        import io

        import openpyxl
        from openpyxl.styles import Font, PatternFill

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Scene Breakdown"

        header_fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
        header_font = Font(name="Arial", size=10, bold=True, color="FFFFFF")

        ws.append([f"STUDIOTOWER SCENE BREAKDOWN: {proposal.title}"])
        ws.append([f"Space: {proposal.space_id}", f"Track: #{proposal.project_tag}", f"Prepared By: {actor.display_name}", f"Date: {datetime.now(UTC).strftime('%Y-%m-%d')}"])
        ws.append([])

        headers = ["Scene #", "Slugline / Setting", "D/N", "Location", "Cast / Characters", "Notes"]
        ws.append(headers)

        scenes = extracted.get("scenes", [])
        characters = extracted.get("characters", [])
        target_scenes = scenes if scenes else [{"scene_number": 1, "slugline": f"EXT. {proposal.title.upper()} - DAY", "day_night": "DAY", "location": "Main Stage"}]
        for sc in target_scenes:
            cast_str = ", ".join(characters[:3]) if characters else "Principal Performer"
            ws.append([
                f"Scene {sc.get('scene_number', 1)}",
                sc.get("slugline", "EXT. SET - DAY"),
                sc.get("day_night", "DAY"),
                sc.get("location", "Main Stage"),
                cast_str,
                "Principal coverage",
            ])

        for col_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=4, column=col_idx)
            cell.fill = header_fill
            cell.font = header_font

        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    @classmethod
    def _build_pdf_stunt_risk(cls, proposal: ActionProposal, actor: User, extracted: Dict[str, Any], stunt_items: List[Dict[str, Any]]) -> bytes:
        import io

        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

        try:
            pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
            font_name = "STSong-Light"
        except Exception:
            font_name = "Helvetica"

        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            "StuntTitle",
            parent=styles["Heading1"],
            fontName=font_name,
            fontSize=15,
            leading=19,
            textColor=colors.HexColor("#991b1b"),
            spaceAfter=6,
        )
        norm_style = ParagraphStyle(
            "StuntNormal",
            parent=styles["Normal"],
            fontName=font_name,
            fontSize=9,
            leading=12,
            textColor=colors.HexColor("#333333"),
        )
        bold_style = ParagraphStyle(
            "StuntBold",
            parent=norm_style,
            fontName=font_name,
            fontSize=10,
            leading=14,
            textColor=colors.HexColor("#1e293b"),
        )

        buf = io.BytesIO()
        doc = SimpleDocTemplate(
            buf,
            pagesize=letter,
            leftMargin=36,
            rightMargin=36,
            topMargin=36,
            bottomMargin=36,
            title=f"STUDIOTOWER CRITICAL STUNT RISK ASSESSMENT: {proposal.title}",
            keywords=["CRITICAL", "STUNT", "HAZARD"],
        )
        elements = []

        elements.append(Paragraph(f"STUDIOTOWER CRITICAL STUNT & SAFETY RISK ASSESSMENT: {proposal.title}", title_style))
        elements.append(Paragraph(f"Space: {proposal.space_id} | Track: #{proposal.project_tag} | Assessor: {actor.display_name} | Risk Level: CRITICAL", norm_style))
        elements.append(Spacer(1, 8))

        # Critical Notice Banner
        banner_data = [[
            "CRITICAL RISK GATE: Mandatory Safety Coordinator Approval Required Prior to Filming"
        ]]
        t_banner = Table(banner_data, colWidths=[540])
        t_banner.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fee2e2")),
            ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#991b1b")),
            ("FONTNAME", (0, 0), (-1, -1), font_name),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#dc2626")),
        ]))
        elements.append(t_banner)
        elements.append(Spacer(1, 10))

        # Stunt Items Table
        elements.append(Paragraph("HIGH-HAZARD SEQUENCES & RIGGING REQUIREMENTS", bold_style))
        stunt_headers = ["Stunt ID", "Scene / Slugline", "Hazard Description", "Cast Involved", "Risk Level"]
        stunt_rows = [stunt_headers]
        for st in stunt_items:
            stunt_rows.append([
                st.get("stunt_id", "STUNT-101"),
                st.get("scene", "EXT. SET - DAY"),
                st.get("action_description", "Stunt Sequence"),
                ", ".join(st.get("cast_involved", ["Principal Cast"])),
                st.get("risk_level", "CRITICAL"),
            ])

        t_stunts = Table(stunt_rows, colWidths=[65, 175, 170, 75, 55])
        t_stunts.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#991b1b")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
            ("FONTNAME", (0, 0), (-1, -1), font_name),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fef2f2")]),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#fca5a5")),
        ]))
        elements.append(t_stunts)
        elements.append(Spacer(1, 12))

        # Safety protocols
        elements.append(Paragraph("MANDATORY SAFETY PROTOCOLS & CONTINGENCIES", bold_style))
        protocols = [
            "• Full safety perimeter and stunt coordinator inspection mandatory prior to call.",
            "• Dedicated medical personnel on set during high-hazard scene execution.",
            "• Safety rigging certified under production safety compliance standards.",
            "• Emergency stop signal established and rehearsed with entire crew.",
        ]
        for p in protocols:
            elements.append(Paragraph(p, norm_style))
        elements.append(Spacer(1, 12))

        # Sign-off Approval Block
        elements.append(Paragraph("SAFETY COORDINATOR APPROVAL GATE", bold_style))
        sign_rows = [
            ["Role", "Name", "Gate Status", "Signature / Decision Token", "Date"],
            ["Stunt Coordinator", actor.display_name, "PENDING COORDINATOR APPROVAL", "[GATE PROTECTED]", datetime.now(UTC).strftime("%Y-%m-%d")],
        ]
        t_sign = Table(sign_rows, colWidths=[100, 110, 150, 110, 70])
        t_sign.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e293b")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
            ("FONTNAME", (0, 0), (-1, -1), font_name),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ]))
        elements.append(t_sign)
        elements.append(Spacer(1, 12))

        # Provenance footer
        elements.append(Paragraph("DATA INTEGRITY & PROVENANCE (PRODOCUX / PDX-ENGINE)", bold_style))
        prov = extracted.get("provenance", [])
        prov_rows = [["Source File ID", "Generation", "SHA-256 Checksum", "Ingestion Status"]]
        for p in prov:
            prov_rows.append([p.get("file_id", "source"), f"Gen {p.get('generation', 1)}", p.get("sha256", "verified")[:24] + "...", "VERIFIED"])
        if not prov:
            prov_rows.append(["Ungrounded Template", "Gen 1", proposal.action_id[:24] + "...", "TEMPLATE_MODE"])

        t_prov = Table(prov_rows, colWidths=[140, 70, 210, 100])
        t_prov.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#334155")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
            ("FONTNAME", (0, 0), (-1, -1), font_name),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ]))
        elements.append(t_prov)

        doc.build(elements)
        return buf.getvalue()

    @classmethod
    def _build_csv_stunt_risk(cls, proposal: ActionProposal, actor: User, extracted: Dict[str, Any], stunt_items: List[Dict[str, Any]]) -> bytes:
        rows = [
            ["STUDIOTOWER CRITICAL STUNT RISK ASSESSMENT", f"Space: {proposal.space_id}", f"Track: #{proposal.project_tag}"],
            ["Title", proposal.title, "Assessed By", actor.display_name, "Date", datetime.now(UTC).strftime("%Y-%m-%d"), "Risk Level", "CRITICAL"],
            [],
            ["Stunt ID", "Scene / Slugline", "Hazard Description", "Cast Involved", "Rigging Required", "Risk Level"],
        ]
        for st in stunt_items:
            rows.append([
                st.get("stunt_id", "STUNT-101"),
                st.get("scene", "EXT. SET - DAY"),
                st.get("action_description", "Stunt Sequence"),
                ", ".join(st.get("cast_involved", ["Principal Cast"])),
                str(st.get("wire_rigging_required", True)),
                st.get("risk_level", "CRITICAL"),
            ])
        csv_text = "\n".join([",".join([f'"{cell}"' if "," in str(cell) else str(cell) for cell in row]) for row in rows])
        return csv_text.encode("utf-8")

    @classmethod
    def _build_docx_stunt_risk(cls, proposal: ActionProposal, actor: User, extracted: Dict[str, Any], stunt_items: List[Dict[str, Any]]) -> bytes:
        import io

        import docx

        doc = docx.Document()
        doc.add_heading(f"CRITICAL STUNT RISK ASSESSMENT: {proposal.title}", 0)
        doc.add_paragraph(f"Space: {proposal.space_id} | Track: #{proposal.project_tag} | Assessor: {actor.display_name}")
        doc.add_paragraph("RISK LEVEL: CRITICAL | Mandatory Safety Coordinator Approval Required Prior to Filming")
        doc.add_heading("1. High Hazard Stunt Sequences", level=1)
        for st in stunt_items:
            doc.add_paragraph(f"• {st.get('stunt_id')}: {st.get('scene')} - {st.get('action_description')} [CRITICAL]")
        doc.add_heading("2. Mandatory Safety Protocols", level=1)
        doc.add_paragraph("• Full safety perimeter and stunt coordinator inspection mandatory prior to call.")
        doc.add_paragraph("• Dedicated medical personnel on set during high-hazard scene execution.")
        doc.add_paragraph("• Safety rigging certified under production safety compliance standards.")
        buf = io.BytesIO()
        doc.save(buf)
        return buf.getvalue()

    @classmethod
    def _build_docx_document(cls, proposal: ActionProposal, actor: User, extracted: Dict[str, Any]) -> bytes:
        import io

        import docx

        doc = docx.Document()
        doc.add_heading(proposal.title, 0)
        doc.add_paragraph(f"Space: {proposal.space_id} | Track: #{proposal.project_tag} | Prepared By: {actor.display_name}")
        doc.add_paragraph(f"Created: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')} | Action ID: {proposal.action_id}")
        doc.add_paragraph("--------------------------------------------------------------------------------")

        doc.add_heading("1. Cast & Principal Talent", level=1)
        chars = extracted.get("characters", [])
        if chars:
            for c in chars:
                doc.add_paragraph(f"• {c} (Principal Performer)")
        else:
            doc.add_paragraph("• Principal Performer 1\n• Supporting Performer 2")

        doc.add_heading("2. Scene Breakdown & Production Notes", level=1)
        scenes = extracted.get("scenes", [])
        if scenes:
            for sc in scenes:
                doc.add_heading(f"Scene {sc.get('scene_number', 1)}: {sc.get('slugline', 'UNTITLED')}", level=2)
                p = doc.add_paragraph()
                p.add_run(f"Setting: {sc.get('day_night', 'DAY')} | Location: {sc.get('location', 'Studio Stage')}\n")
                p.add_run("Coverage: Principal unit coverage with standard camera and sound packages.")
        else:
            doc.add_paragraph("Scene 1: EXT. MAIN PRODUCTION LOCATION - DAY\nStandard camera coverage.")

        doc.add_heading("3. Safety, Stunts & Contingency", level=1)
        stunts = extracted.get("stunts_found", [])
        if stunts:
            doc.add_paragraph(f"Stunt & Hazard cues detected from screenplay: {', '.join(stunts)}.")
        doc.add_paragraph("All high-hazard sequences require safety coordinator walk-through prior to camera rolling.")

        buf = io.BytesIO()
        doc.save(buf)
        return buf.getvalue()

    @classmethod
    def _build_xlsx_budget(cls, proposal: ActionProposal, actor: User, extracted: Dict[str, Any]) -> bytes:
        import io

        import openpyxl
        from openpyxl.styles import Alignment, Font, PatternFill

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Production Budget"

        header_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
        header_font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
        total_fill = PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type="solid")
        total_font = Font(name="Arial", size=10, bold=True)

        ws.append([f"STUDIOTOWER PRODUCTION BUDGET: {proposal.title}"])
        ws.append([f"Space: {proposal.space_id}", f"Track: #{proposal.project_tag}", f"Prepared By: {actor.display_name}", f"Date: {datetime.now(UTC).strftime('%Y-%m-%d')}"])
        ws.append([])

        headers = ["Account Code", "Category", "Description", "Qty", "Rate (USD)", "Total (USD)"]
        ws.append(headers)

        scenes = extracted.get("scenes", [])
        chars = extracted.get("characters", [])
        scene_count = max(1, len(scenes))
        cast_count = max(2, len(chars))

        items = [
            ("1001", "Above The Line", "Directing & Production Supervision", 1, 120000.0, 120000.0),
            ("2001", "Cast & Talent", f"Principal Cast Compensation ({cast_count} roles)", cast_count, 15000.0, cast_count * 15000.0),
            ("3001", "Production Crew", "Camera Operator & Grip Department", scene_count * 2, 1200.0, scene_count * 2400.0),
            ("4001", "Equipment & Stage", "Camera, Lighting & Grip Package", scene_count, 4500.0, scene_count * 4500.0),
            ("5001", "Location & Permits", f"Location Fees ({scene_count} locations)", scene_count, 3500.0, scene_count * 3500.0),
        ]

        total_sum = sum(row[5] for row in items)
        for row in items:
            ws.append(list(row))

        ws.append(["TOTAL", "", "Estimated Total Production Budget", "", "", total_sum])

        for col_idx, col in enumerate(ws.columns, start=1):
            max_len = max(len(str(cell.value or "")) for cell in col)
            ws.column_dimensions[openpyxl.utils.get_column_letter(col_idx)].width = max(max_len + 3, 14)

        for cell in ws[4]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center")

        last_row = ws.max_row
        for cell in ws[last_row]:
            cell.fill = total_fill
            cell.font = total_font

        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    @classmethod
    def _build_pptx_deck(cls, proposal: ActionProposal, actor: User, extracted: Dict[str, Any]) -> bytes:
        import io

        import pptx

        prs = pptx.Presentation()

        # Slide 1: Title
        slide1 = prs.slides.add_slide(prs.slide_layouts[0])
        slide1.shapes.title.text = proposal.title
        slide1.placeholders[1].text = f"StudioTower Film Production Proposal\nSpace: {proposal.space_id} | Track: #{proposal.project_tag}\nProducer: {actor.display_name}"

        # Slide 2: Scene & Location Breakdown
        slide2 = prs.slides.add_slide(prs.slide_layouts[1])
        slide2.shapes.title.text = "Scene & Location Breakdown"
        tf2 = slide2.placeholders[1].text_frame
        tf2.text = "Screenplay Locations & Scenes:"
        scenes = extracted.get("scenes", [])
        if scenes:
            for sc in scenes[:5]:
                p = tf2.add_paragraph()
                p.text = f"• Scene {sc.get('scene_number', 1)}: {sc.get('slugline', 'EXT. LOCATION - DAY')} ({sc.get('location', 'Stage')})"
        else:
            p = tf2.add_paragraph()
            p.text = f"• Scene 1: EXT. {proposal.title.upper()} - DAY (Main Location)"

        # Slide 3: Cast & Talent
        slide3 = prs.slides.add_slide(prs.slide_layouts[1])
        slide3.shapes.title.text = "Cast & Talent Roster"
        tf3 = slide3.placeholders[1].text_frame
        tf3.text = "Key Roles & Performers:"
        chars = extracted.get("characters", [])
        if chars:
            for c in chars[:6]:
                p = tf3.add_paragraph()
                p.text = f"• {c}: Principal Cast / On Set Call"
        else:
            p = tf3.add_paragraph()
            p.text = "• Lead Protagonist & Supporting Cast (TBD)"

        # Slide 4: Safety & Production Plan
        slide4 = prs.slides.add_slide(prs.slide_layouts[1])
        slide4.shapes.title.text = "Production Safety & Provenance"
        tf4 = slide4.placeholders[1].text_frame
        tf4.text = "Deterministic Governance Controls:"
        p1 = tf4.add_paragraph()
        p1.text = f"• Source Verification: {len(extracted.get('provenance', []))} document chunks cryptographically hashed"
        p2 = tf4.add_paragraph()
        stunts = extracted.get("stunts_found", [])
        p2.text = f"• Safety Checks: Stunt & hazard review ({', '.join(stunts) if stunts else 'No critical stunts detected'})"
        p3 = tf4.add_paragraph()
        p3.text = "• Approval Gates: Pre-shoot human coordinator approval mandatory"

        buf = io.BytesIO()
        prs.save(buf)
        return buf.getvalue()

    @classmethod
    def _generate_content_from_sources_legacy(
        cls, proposal: ActionProposal, actor: User
    ) -> Tuple[str, str, bytes, bool, str]:
        """
        Generates authentic deliverable data directly derived from source file document chunks and provenance.
        Supports the 5 ProDocuX canonical formats (PDF, DOCX, XLSX, PPTX, CSV) and structured JSON gates.
        Returns: (filename, media_type, content_bytes, is_critical_risk, gate_description)
        """
        act_type = proposal.action_type
        title_slug = proposal.title.lower().replace(" ", "_").replace("/", "_")[:32]
        title_lower = proposal.title.lower()
        desc_lower = (proposal.description or "").lower()
        extracted = cls._extract_source_document_data(proposal)
        scenes = extracted["scenes"]
        characters = extracted["characters"]
        stunts_found = extracted["stunts_found"]
        provenance = extracted["provenance"]
        is_ungrounded = extracted["is_ungrounded"]

        # Legacy rollback path still obeys the signed structured format contract.
        # Never infer a file type from model-authored title or description text.
        requested_format = proposal.output_format or DeliverableFormat.PDF
        wants_pdf = requested_format == DeliverableFormat.PDF
        wants_docx = requested_format == DeliverableFormat.DOCX
        wants_xlsx = requested_format == DeliverableFormat.XLSX
        wants_pptx = requested_format == DeliverableFormat.PPTX
        wants_csv = requested_format == DeliverableFormat.CSV
        wants_json = "json" in title_lower or "json" in desc_lower or title_lower.endswith(".json")

        is_stunt = act_type == "stunt_risk_breakdown" or any(w in title_lower for w in ("stunt", "hazard", "risk", "特技", "安全", "風險"))
        is_budget = act_type == "export_production_budget" or any(w in title_lower for w in ("budget", "預算", "費用", "cost", "spend"))
        is_deck = act_type in ("generate_pitch_deck", "generate_lookbook") or any(w in title_lower for w in ("lookbook", "pitch", "deck", "簡報", "提案"))
        is_treatment = act_type == "create_treatment_document" or any(w in title_lower for w in ("treatment", "梗概", "大綱", "故事"))
        is_call_sheet = act_type == "create_call_sheet" or any(w in title_lower for w in ("call sheet", "call_sheet", "通告", "callsheet"))
        is_shot_list = act_type == "generate_shot_list" or any(w in title_lower for w in ("shot list", "shot_list", "分鏡", "鏡頭"))

        # 1. Critical Risk Stunt Gate: Supports all formats (PDF, DOCX, CSV, XLSX, JSON) with Critical Risk Gate
        if is_stunt:
            target_scenes = scenes[:3] if scenes else [{"scene_number": 1, "slugline": f"EXT. {proposal.title.upper()} - DAY", "location": "Main Unit Location"}]
            stunt_items = []
            for i, sc in enumerate(target_scenes, 1):
                stunt_items.append({
                    "stunt_id": f"STUNT-{100 + i}",
                    "scene": sc["slugline"],
                    "action_description": f"Stunt action sequence in {sc['location']}" + (f" ({', '.join(stunts_found)})" if stunts_found else ""),
                    "cast_involved": characters[:2] if characters else ["Principal Performer"],
                    "wire_rigging_required": (
                        True if ("wire" in stunts_found or "fall" in stunts_found) else None
                    ),
                    "risk_level": "CRITICAL",
                })

            gate_msg = f"Critical Risk Stunt Gate: Safety Coordinator approval mandatory for {len(stunt_items)} stunt sequence(s) before filming."

            if wants_json:
                data = {
                    "report_title": proposal.title,
                    "space_id": proposal.space_id,
                    "project_tag": proposal.project_tag,
                    "assessed_by": actor.display_name,
                    "risk_category": "CRITICAL",
                    "source_provenance": provenance,
                    "is_ungrounded_template": is_ungrounded,
                    "stunts": stunt_items,
                    "safety_precautions": [
                        "Full safety perimeter and stunt coordinator inspection mandatory prior to call",
                        "Dedicated medical personnel on set during high-hazard scene execution",
                        "Safety rigging certified under production safety compliance standards",
                    ],
                }
                return f"{title_slug}.json", "application/json", json.dumps(data, indent=2).encode("utf-8"), True, gate_msg
            elif wants_docx:
                docx_bytes = cls._build_docx_stunt_risk(proposal, actor, extracted, stunt_items)
                return f"{title_slug}.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", docx_bytes, True, gate_msg
            elif wants_csv:
                csv_bytes = cls._build_csv_stunt_risk(proposal, actor, extracted, stunt_items)
                return f"{title_slug}.csv", "text/csv", csv_bytes, True, gate_msg
            elif wants_xlsx:
                xlsx_bytes = cls._build_xlsx_scene_breakdown(proposal, actor, extracted)
                return f"{title_slug}.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", xlsx_bytes, True, gate_msg
            else:
                pdf_bytes = cls._build_pdf_stunt_risk(proposal, actor, extracted, stunt_items)
                return f"{title_slug}.pdf", "application/pdf", pdf_bytes, True, gate_msg

        # 2. Production Budget: supports XLSX, PDF, CSV, JSON
        if is_budget:
            if wants_pdf:
                filename = f"{title_slug}.pdf"
                media_type = "application/pdf"
                pdf_bytes = cls._build_pdf_budget(proposal, actor, extracted)
                return filename, media_type, pdf_bytes, False, ""
            elif wants_csv:
                filename = f"{title_slug}.csv"
                media_type = "text/csv"
                scene_count = max(1, len(scenes))
                cast_count = max(2, len(characters))
                rows = [
                    ["STUDIOTOWER PRODUCTION BUDGET BREAKDOWN", f"Space: {proposal.space_id}", f"Track: #{proposal.project_tag}"],
                    ["Title", proposal.title, "Currency", "USD"],
                    [],
                    ["Account Code", "Category", "Description", "Qty", "Rate", "Total USD"],
                    ["1001", "Above The Line", "Directing & Production Supervision", "1", "120000.00", "120000.00"],
                    ["2001", "Cast & Talent", f"Principal Cast Compensation ({cast_count} roles)", str(cast_count), "15000.00", f"{cast_count * 15000:.2f}"],
                    ["3001", "Production Crew", "Camera Operator & Grip Department", str(scene_count * 2), "1200.00", f"{scene_count * 2400:.2f}"],
                    ["4001", "Equipment & Stage", "Camera, Lighting & Grip Package", str(scene_count), "4500.00", f"{scene_count * 4500:.2f}"],
                    ["5001", "Location & Permits", f"Location Fees ({scene_count} locations)", str(scene_count), "3500.00", f"{scene_count * 3500:.2f}"],
                    ["TOTAL", "", "Estimated Total Production Budget", "", "", f"{120000 + cast_count * 15000 + scene_count * 10400:.2f}"],
                ]
                csv_text = "\n".join([",".join([f'"{cell}"' if "," in str(cell) else str(cell) for cell in row]) for row in rows])
                return filename, media_type, csv_text.encode("utf-8"), False, ""
            elif wants_json:
                data = {
                    "budget_title": proposal.title,
                    "space_id": proposal.space_id,
                    "project_tag": proposal.project_tag,
                    "currency": "USD",
                    "line_items": [
                        {"code": "1001", "category": "Above The Line", "description": "Directing & Production Supervision", "total_usd": 120000.0},
                        {"code": "2001", "category": "Cast & Talent", "description": "Principal Cast Compensation", "total_usd": len(characters) * 15000.0 if characters else 30000.0},
                        {"code": "3001", "category": "Production Crew", "description": "Camera & Grip", "total_usd": max(1, len(scenes)) * 2400.0},
                    ],
                    "total_usd": 120000.0 + (len(characters) * 15000.0 if characters else 30000.0) + max(1, len(scenes)) * 2400.0,
                }
                return f"{title_slug}.json", "application/json", json.dumps(data, indent=2).encode("utf-8"), False, ""
            else:
                filename = f"{title_slug}.xlsx"
                media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                xlsx_bytes = cls._build_xlsx_budget(proposal, actor, extracted)
                return filename, media_type, xlsx_bytes, False, ""

        # 3. Pitch Deck / Lookbook: PPTX or PDF
        if is_deck:
            if wants_pdf:
                filename = f"{title_slug}.pdf"
                media_type = "application/pdf"
                pdf_bytes = cls._build_pdf_call_sheet(proposal, actor, extracted)
                return filename, media_type, pdf_bytes, False, ""
            filename = f"{title_slug}.pptx"
            media_type = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            pptx_bytes = cls._build_pptx_deck(proposal, actor, extracted)
            return filename, media_type, pptx_bytes, False, ""

        # 4. Treatment Document: DOCX or PDF
        if is_treatment:
            if wants_pdf:
                filename = f"{title_slug}.pdf"
                media_type = "application/pdf"
                pdf_bytes = cls._build_pdf_scene_breakdown(proposal, actor, extracted)
                return filename, media_type, pdf_bytes, False, ""
            filename = f"{title_slug}.docx"
            media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            docx_bytes = cls._build_docx_document(proposal, actor, extracted)
            return filename, media_type, docx_bytes, False, ""

        # 5. Call Sheet: PDF if requested, otherwise CSV default
        if is_call_sheet:
            if wants_pdf:
                filename = f"{title_slug}.pdf"
                media_type = "application/pdf"
                pdf_bytes = cls._build_pdf_call_sheet(proposal, actor, extracted)
                return filename, media_type, pdf_bytes, False, ""
            else:
                filename = f"{title_slug}.csv"
                media_type = "text/csv"
                rows = [
                    ["STUDIOTOWER PRODUCTION CALL SHEET", f"Space: {proposal.space_id}", f"Track: #{proposal.project_tag}"],
                    ["Title", proposal.title, "Date", datetime.now(UTC).strftime("%Y-%m-%d"), "Call Time", "07:00 AM"],
                    ["Prepared By", actor.display_name, "Source Files Ingested", str(len(provenance))],
                ]
                if is_ungrounded:
                    rows.extend([
                        [],
                        ["[DRAFT PRODUCTION TEMPLATE - UNGROUNDED: NO SCRIPT SOURCES INGESTED]"],
                        ["Notice", "This call sheet was generated from template parameters without parsed screenplay scenes."],
                        [],
                        ["CAST & TALENT LINEUP"],
                        ["Role / Character", "Talent Status", "Pickup", "Call Time", "Set Call"],
                        ["[Cast Member 1]", "TBD", "07:00 AM", "08:00 AM", "09:00 AM"],
                        ["[Cast Member 2]", "TBD", "07:30 AM", "08:30 AM", "09:30 AM"],
                        [],
                        ["SCENE SHOOTING SCHEDULE"],
                        ["Scene #", "Slugline / Setting", "D/N", "Location"],
                        ["Scene 1", f"EXT. {proposal.title.upper()} - DAY", "DAY", "Main Location Stage"],
                    ])
                else:
                    rows.extend([
                        [],
                        ["CAST & TALENT LINEUP"],
                        ["Role / Character", "Talent Status", "Pickup", "Call Time", "Set Call"],
                    ])
                    for i, char_name in enumerate(characters[:6]):
                        pickup_time = f"0{6 + (i // 2)}:{15 * (i % 4):02d} AM"
                        call_time = f"0{7 + (i // 2)}:{15 * (i % 4):02d} AM"
                        rows.append([char_name, "On Call", pickup_time, call_time, "08:30 AM"])

                    rows.extend([
                        [],
                        ["SCENE SHOOTING SCHEDULE"],
                        ["Scene #", "Slugline / Setting", "D/N", "Location"],
                    ])
                    for sc in scenes:
                        rows.append([f"Scene {sc['scene_number']}", sc["slugline"], sc["day_night"], sc["location"]])

                rows.extend([
                    [],
                    ["PROVENANCE & INTEGRITY"],
                    ["Action ID", proposal.action_id],
                    ["Sources Count", str(len(provenance))],
                ])
                for p in provenance:
                    rows.append(["Source File", p.get("file_id", ""), f"Gen {p.get('generation', 1)}", p.get("sha256", "")])

                csv_text = "\n".join([",".join([f'"{cell}"' if "," in str(cell) else str(cell) for cell in row]) for row in rows])
                return filename, media_type, csv_text.encode("utf-8"), False, ""

        # 6. Shot List: CSV default, supports PDF, DOCX, XLSX
        if is_shot_list:
            if wants_pdf:
                filename = f"{title_slug}.pdf"
                media_type = "application/pdf"
                pdf_bytes = cls._build_pdf_scene_breakdown(proposal, actor, extracted)
                return filename, media_type, pdf_bytes, False, ""
            elif wants_docx:
                filename = f"{title_slug}.docx"
                media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                docx_bytes = cls._build_docx_document(proposal, actor, extracted)
                return filename, media_type, docx_bytes, False, ""
            elif wants_xlsx:
                filename = f"{title_slug}.xlsx"
                media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                xlsx_bytes = cls._build_xlsx_scene_breakdown(proposal, actor, extracted)
                return filename, media_type, xlsx_bytes, False, ""
            else:
                filename = f"{title_slug}.csv"
                media_type = "text/csv"
                rows = [
                    ["Shot #", "Scene #", "Shot Type", "Angle", "Movement", "Lens", "Description"],
                ]
                shot_types = ["Wide (WS)", "Medium (MS)", "Close-up (CU)", "Over the Shoulder (OTS)"]
                angles = ["Eye Level", "Low Angle", "High Angle", "Profile"]
                movements = ["Static", "Dolly In", "Pan Right", "Handheld"]
                lenses = ["24mm Prime", "35mm Prime", "50mm Prime", "85mm Prime"]

                target_scenes = scenes[:4] if scenes else [{"scene_number": 1, "slugline": f"EXT. {proposal.title.upper()} - DAY"}]
                for sc_idx, sc in enumerate(target_scenes, 1):
                    for sh_idx, st in enumerate(shot_types, 1):
                        rows.append([
                            f"{sc_idx}{chr(64 + sh_idx)}",
                            str(sc["scene_number"]),
                            st,
                            angles[sh_idx - 1],
                            movements[sh_idx - 1],
                            lenses[sh_idx - 1],
                            f"Coverage for {sc['slugline']} with {characters[0] if characters else 'Principal Actor'}",
                        ])

                csv_text = "\n".join([",".join([f'"{cell}"' if "," in str(cell) else str(cell) for cell in row]) for row in rows])
                return filename, media_type, csv_text.encode("utf-8"), False, ""

        # 7. Scene Breakdown & General Actions: Default to PDF, supports DOCX, XLSX, CSV, JSON
        if wants_json:
            filename = f"{title_slug}.json"
            media_type = "application/json"
            target_scenes = scenes if scenes else [{"scene_number": 1, "slugline": f"EXT. {proposal.title.upper()} - DAY", "day_night": "DAY", "location": "Main Stage"}]
            data = {
                "breakdown_title": proposal.title,
                "space_id": proposal.space_id,
                "project_tag": proposal.project_tag,
                "source_provenance": provenance,
                "is_ungrounded_template": is_ungrounded,
                "scenes": [
                    {
                        "scene_number": sc["scene_number"],
                        "slugline": sc["slugline"],
                        "day_night": sc["day_night"],
                        "location": sc["location"],
                        "cast": characters[:3] if characters else ["Principal Performer"],
                    }
                    for sc in target_scenes
                ],
            }
            return filename, media_type, json.dumps(data, indent=2).encode("utf-8"), False, ""
        elif wants_docx:
            filename = f"{title_slug}.docx"
            media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            docx_bytes = cls._build_docx_document(proposal, actor, extracted)
            return filename, media_type, docx_bytes, False, ""
        elif wants_xlsx:
            filename = f"{title_slug}.xlsx"
            media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            xlsx_bytes = cls._build_xlsx_scene_breakdown(proposal, actor, extracted)
            return filename, media_type, xlsx_bytes, False, ""
        elif wants_csv:
            filename = f"{title_slug}.csv"
            media_type = "text/csv"
            csv_bytes = cls._build_csv_scene_breakdown(proposal, actor, extracted)
            return filename, media_type, csv_bytes, False, ""
        elif wants_pptx:
            filename = f"{title_slug}.pptx"
            media_type = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            pptx_bytes = cls._build_pptx_deck(proposal, actor, extracted)
            return filename, media_type, pptx_bytes, False, ""
        else:
            # Default for scene breakdowns and general deliverables is a polished PDF document
            filename = f"{title_slug}.pdf"
            media_type = "application/pdf"
            pdf_bytes = cls._build_pdf_scene_breakdown(proposal, actor, extracted)
            return filename, media_type, pdf_bytes, False, ""

    @classmethod
    def _load_source_evidence(cls, proposal: ActionProposal) -> Dict[str, Any]:
        """Load indexed source text for Gemini. Does not decide file content."""
        sources_to_query: List[ActionSourceDescriptor] = list(proposal.sources) if proposal.sources else []
        if not sources_to_query and proposal.source_file_ids:
            for fid in proposal.source_file_ids:
                f_obj = store.get_file(fid)
                if f_obj:
                    sources_to_query.append(
                        ActionSourceDescriptor(
                            file_id=f_obj.file_id,
                            active_generation=f_obj.active_generation,
                            content_hash=f_obj.sha256 or "",
                            space_id=proposal.space_id,
                        )
                    )
        extracted_text_blocks: List[str] = []
        source_provenance: List[Dict[str, Any]] = []
        for src in sources_to_query:
            chunks = store.get_document_chunks(proposal.space_id, src.file_id, src.active_generation)
            if chunks:
                extracted_text_blocks.extend(c.normalized_text for c in chunks if c.normalized_text)
                source_provenance.append({
                    "file_id": src.file_id,
                    "generation": src.active_generation,
                    "chunk_count": len(chunks),
                    "sha256": src.content_hash,
                })
            else:
                f_rec = store.get_file(src.file_id)
                if f_rec:
                    source_provenance.append({
                        "file_id": src.file_id,
                        "generation": src.active_generation,
                        "filename": f_rec.filename,
                        "sha256": src.content_hash or f_rec.sha256,
                    })
        return {"full_text": "\n".join(extracted_text_blocks), "provenance": source_provenance}

    @classmethod
    def _extracted_from_gemini(cls, content: Any, provenance: List[Dict[str, Any]], full_text: str) -> Dict[str, Any]:
        scenes: List[Dict[str, Any]] = []
        for scene in getattr(content, "scenes", None) or []:
            scenes.append({
                "scene_number": scene.scene_number,
                "slugline": scene.slugline,
                "day_night": scene.day_night or "TBD",
                "location": scene.location or "TBD",
                "characters": list(scene.characters or []),
                "production_elements": list(scene.production_elements or []),
                "description": scene.description or "",
            })
        return {
            "full_text": full_text,
            "scenes": scenes,
            "characters": list(getattr(content, "characters", None) or []),
            "stunts_found": list(getattr(content, "stunts_found", None) or []),
            "production_elements": list(getattr(content, "production_elements", None) or []),
            "provenance": provenance,
            "is_ungrounded": not scenes,
            "summary": getattr(content, "summary", "") or "",
        }

    @classmethod
    def _generate_content_from_sources(
        cls, proposal: ActionProposal, actor: User
    ) -> Tuple[str, str, bytes, bool, str]:
        """Gemini decides file content; ProDocuX only renders the chosen export format."""
        from app.agent.brain import AgentBrain
        from app.services.prodocux_deliverable_renderer import render_deliverable

        evidence = cls._load_source_evidence(proposal)
        metadata = proposal.metadata or {}
        request_text = str(metadata.get("request_text") or "").strip()
        user_request = "\n".join(
            part for part in (proposal.title, proposal.description or "", request_text) if part
        )
        ai_content = AgentBrain.compose_deliverable_content(
            action_type=proposal.action_type,
            title=proposal.title,
            description=proposal.description or "",
            user_request=user_request,
            source_text=evidence.get("full_text") or "",
            project_tag=proposal.project_tag,
        )
        extracted = cls._extracted_from_gemini(
            ai_content,
            evidence.get("provenance") or [],
            evidence.get("full_text") or "",
        )
        return render_deliverable(proposal, actor, extracted)
