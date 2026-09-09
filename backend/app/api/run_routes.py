import logging
import uuid
from datetime import UTC, datetime, timedelta

from app.agent.brain import AgentBrain
from app.core.auth import get_current_user
from app.integrations.pdx_engine import PDXEngine
from app.models.activity import ActivityEventType
from app.models.message import Message, MessageRole
from app.models.run import Run, RunStatus
from app.models.space import MembershipRole
from app.models.telemetry import DiagnosisRecord
from app.models.user import User
from app.services.activity_service import ActivityService
from app.services.diagnosis_service import DiagnosisService
from app.services.space_service import SpaceService
from app.services.storage import StorageConflictError, store
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

logger = logging.getLogger("studiotower.run_routes")
router = APIRouter(prefix="/v1", tags=["runs"])


class ApproveRunRequest(BaseModel):
    approved: bool = True
    rejection_reason: str | None = None


@router.post("/spaces/{space_id}/runs/{run_id}/approve", response_model=Run)
def approve_run_gate(
    space_id: str,
    run_id: str,
    payload: ApproveRunRequest,
    current_user: User = Depends(get_current_user),
):
    """
    Approve or reject a paused risk gate in a Run.
    When approved, resumes execution and triggers PDX deterministic artifact bundling.
    """
    # Role authorization check for high-risk gate approval
    user_role = SpaceService.get_user_role(space_id, current_user)
    if user_role not in (MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.COORDINATOR):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: only Space owners, administrators, or production coordinators are authorized to approve high-risk gates",
        )

    run = store.get_run(run_id)
    if not run or run.space_id != space_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Run not found in this Space",
        )

    is_awaiting = (run.status == RunStatus.AWAITING_APPROVAL)
    is_approving_recoverable = False
    if run.status == RunStatus.RUNNING and run.approval_gate:
        if run.approval_gate.status in ("pending", "approving"):
            lease_until = getattr(run.approval_gate, "decision_lease_until", None)
            now_dt = datetime.now(UTC)
            if lease_until is None or now_dt >= lease_until or run.approval_gate.status == "pending":
                is_approving_recoverable = True

    if not is_awaiting and not is_approving_recoverable:
        if run.approval_gate and run.approval_gate.status in ("approved", "rejected"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"GATE_ALREADY_DECIDED: Gate is already {run.approval_gate.status}.",
            )
        status_str = run.status.value if hasattr(run.status, "value") else str(run.status)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Run is in state '{status_str}', not 'awaiting_approval'",
        )

    tag = run.project_tag or "general"

    if not payload.approved:
        def mutate_reject(r: Run):
            if r.approval_gate:
                r.approval_gate.status = "rejected"
        try:
            cas_run, _ = store.reject_gate_and_fail_artifacts_atomic(
                space_id=space_id,
                run_id=run_id,
                approver_uid=current_user.uid,
                rejection_reason=payload.rejection_reason,
            )
        except StorageConflictError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="GATE_ALREADY_DECIDED: Run is no longer awaiting approval.",
            )

        store.add_message(
            Message(
                space_id=space_id,
                sender_uid="agent_studiotower",
                sender_name="StudioTower Agent",
                role=MessageRole.AGENT,
                content=f"❌ **Risk Gate Rejected**: {cas_run.approval_gate.title if cas_run.approval_gate else 'Approval'}\nReason: {payload.rejection_reason or 'No reason specified'}. Execution halted.",
                project_tag=tag,
            )
        )
        return cas_run

    decision_token = uuid.uuid4().hex
    lease_duration_seconds = 60

    def mutate_approving(r: Run):
        if r.approval_gate:
            r.approval_gate.status = "approving"
            r.approval_gate.decision_lease_token = decision_token
            r.approval_gate.decision_by = current_user.uid
            r.approval_gate.decision_started_at = datetime.now(UTC)
            r.approval_gate.decision_lease_until = datetime.now(UTC) + timedelta(seconds=lease_duration_seconds)
        r.error_summary = None

    expected_cas_status = RunStatus.RUNNING if is_approving_recoverable else RunStatus.AWAITING_APPROVAL
    cas_run = store.compare_and_swap_run_status(
        run_id,
        expected_status=expected_cas_status,
        new_status=RunStatus.RUNNING,
        mutator_fn=mutate_approving,
        actor_uid=current_user.uid,
    )
    if not cas_run:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="GATE_ALREADY_DECIDED: Run has already been approved or transitioned by another request.",
        )
    initial_art_ids = list(cas_run.output_artifact_ids or [])
    trace_id = getattr(cas_run, "trace_id", None) or uuid.uuid4().hex[:12]

    # Stage 1: Deterministic PDX deliverable execution and bundling
    try:
        bundled_run = PDXEngine.execute_and_bundle(space_id, cas_run, current_user)
        output_artifacts = list(dict.fromkeys((bundled_run.output_artifact_ids or []) + initial_art_ids))
    except Exception as pdx_err:
        logger.error(f"PDX generation failed for run {run_id}: {pdx_err} [trace_id={trace_id}]")
        # Definite planning/generation failure: safe to abort and cleanup staging files
        try:
            from app.services.deliverable_service import DeliverableExecutionService
            DeliverableExecutionService.reconcile_approval_commit(space_id, run_id, force_aborted=True)
        except Exception as r_err:
            logger.warning(f"Immediate reconcile abort encountered error for run {run_id}: {r_err}")

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"PDX artifact generation failed: internal planning failure. Trace ID: {trace_id}",
        )

    # Stage 2: Atomic storage publishing commit
    try:
        # Single atomic storage transaction: Run -> COMPLETED, Gate -> approved, Artifacts -> published, ActionExecution -> COMPLETED, Outbox event
        completed_run, _ = store.approve_gate_and_publish_artifacts_atomic(
            space_id=space_id,
            run_id=run_id,
            approver_uid=current_user.uid,
            output_artifact_ids=output_artifacts,
            plan=bundled_run.plan,
            decision_lease_token=decision_token,
        )
    except StorageConflictError as conflict_err:
        logger.warning(f"Storage conflict during approval commit for run {run_id}: {conflict_err}")
        # Re-read authoritative state to see if another concurrent request completed it
        authoritative_run = store.get_run(run_id)
        if authoritative_run and authoritative_run.status == RunStatus.COMPLETED:
            completed_run = authoritative_run
        else:
            def mutate_rollback(r: Run):
                if r.approval_gate:
                    r.approval_gate.status = "pending"
                    r.approval_gate.decision_lease_token = None
                    r.approval_gate.decision_by = None
                    r.approval_gate.decision_started_at = None
                    r.approval_gate.decision_lease_until = None
            try:
                store.compare_and_swap_run_status(
                    run_id,
                    expected_status=RunStatus.RUNNING,
                    new_status=RunStatus.AWAITING_APPROVAL,
                    mutator_fn=mutate_rollback,
                    actor_uid=current_user.uid,
                )
            except Exception as roll_err:
                logger.warning(f"Failed to rollback CAS state for run {run_id}: {roll_err}")

            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="GATE_ALREADY_DECIDED: Run has already been approved or transitioned by another request.",
            )
    except Exception as commit_err:
        logger.error(f"Approval publishing commit failed/uncertain for run {run_id}: {commit_err} [trace_id={trace_id}]")
        def mutate_uncertain(r: Run):
            r.approval_commit_status = "uncertain"
            r.uncertain_since = datetime.now(UTC)
            r.updated_at = datetime.now(UTC)

        store.compare_and_swap_run_status(
            run_id,
            expected_status=RunStatus.RUNNING,
            new_status=RunStatus.RUNNING,
            mutator_fn=mutate_uncertain,
            actor_uid=current_user.uid,
        )

        # Attempt immediate reconcile-first evaluation without forcing abort (preserves active lease)
        try:
            from app.services.deliverable_service import DeliverableExecutionService
            reconcile_status, reconciled_run = DeliverableExecutionService.reconcile_approval_commit(
                space_id, run_id, force_aborted=False
            )
            logger.info(f"Immediate reconcile status for run {run_id}: {reconcile_status}")
            if reconcile_status in ("ALREADY_COMPLETED", "COMMITTED_PUBLISHED") and reconciled_run:
                completed_run = reconciled_run
            else:
                completed_run = None
        except Exception as r_err:
            logger.warning(f"Immediate reconcile encountered error for run {run_id}: {r_err}")
            completed_run = None

        if not completed_run:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"APPROVAL_COMMIT_UNCERTAIN: Approval transaction state is uncertain. Please retry with trace_id={trace_id}.",
            )

    artifact_names = [
        store.get_file(fid).filename for fid in completed_run.output_artifact_ids if store.get_file(fid)
    ]
    manifest_name = (
        store.get_file(completed_run.manifest_file_id).filename
        if completed_run.manifest_file_id and store.get_file(completed_run.manifest_file_id)
        else "RunManifest.json"
    )

    artifacts_list_str = "\n".join([f"- 📄 `{name}`" for name in artifact_names])
    agent_notice = (
        f"✅ **Risk Gate Approved & PDX Execution Completed!**\n"
        f"Approved by **{current_user.display_name or current_user.email}**.\n\n"
        f"**Deterministic Artifacts Generated ({len(artifact_names)}):**\n"
        f"{artifacts_list_str}\n"
        f"- 📦 `{manifest_name}` *(Cryptographic SHA-256 Manifest)*\n\n"
        f"*All artifacts have been indexed into Space file library and Right-Panel Lineage DAG.*"
    )

    store.add_message(
        Message(
            space_id=space_id,
            sender_uid="agent_studiotower",
            sender_name="StudioTower Agent",
            role=MessageRole.AGENT,
            content=agent_notice,
            project_tag=tag,
            attachment_file_ids=completed_run.output_artifact_ids + ([completed_run.manifest_file_id] if completed_run.manifest_file_id else []),
        )
    )

    return completed_run


@router.post("/spaces/{space_id}/runs/{run_id}/diagnose", response_model=DiagnosisRecord)
def diagnose_run(
    space_id: str,
    run_id: str,
    current_user: User = Depends(get_current_user),
):
    """
    Invoke DiagnosisService to inspect execution traces and synthesize an evidence-grounded root cause diagnosis.
    Workflow-side-effect-free: only records DiagnosisRecord and updates run diagnosis pointers.
    Auth and rate limits are checked BEFORE cache reads.
    """
    SpaceService.get_space_with_auth(space_id, current_user)
    SpaceService.check_can_diagnose_runs(space_id, current_user)

    # Check dual-layer rate limit BEFORE checking or returning cache
    DiagnosisService.check_rate_limit(space_id=space_id, user_id=current_user.uid)

    run = store.get_run(run_id)
    if not run or run.space_id != space_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found in this Space")

    diagnosis = DiagnosisService.diagnose_run(space_id=space_id, run_id=run_id, user=current_user)
    return diagnosis


@router.post("/spaces/{space_id}/runs/{run_id}/retry", response_model=Run)
def retry_run(
    space_id: str,
    run_id: str,
    current_user: User = Depends(get_current_user),
):
    """
    Retry a failed Run.
    Requires caller to be Space OWNER, ADMIN, or COORDINATOR.
    """
    user_role = SpaceService.get_user_role(space_id, current_user)
    if user_role not in (MembershipRole.OWNER, MembershipRole.ADMIN, MembershipRole.COORDINATOR):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: only Space owners, administrators, or coordinators can retry failed runs",
        )

    run = store.get_run(run_id)
    if not run or run.space_id != space_id:
        raise HTTPException(status_code=404, detail="Run not found")

    if run.status != RunStatus.FAILED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Only runs in 'failed' state can be retried (current state: '{run.status.value}')",
        )

    if not run.is_retryable:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Run {run.run_id} cannot be retried (failure code: '{run.failure_code or 'NON_RETRYABLE'}'). Only transient PDX execution failures can be retried.",
        )

    if run.approval_gate and run.approval_gate.status == "rejected":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot retry a run that was rejected by risk gate. Submit a new run with adjusted parameters.",
        )

    tag = run.project_tag or "general"

    def mutate_retry(r: Run):
        r.error_summary = None

    cas_run = store.compare_and_swap_run_status(
        run_id,
        expected_status=RunStatus.FAILED,
        new_status=RunStatus.RUNNING,
        mutator_fn=mutate_retry,
    )
    if not cas_run:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Run state conflict: Run is not in failed state or retry is already executing.",
        )

    try:
        completed_run = PDXEngine.execute_and_bundle(space_id, cas_run, current_user)
        completed_run.status = RunStatus.COMPLETED
        completed_run.failure_code = None
        completed_run.is_retryable = False
        store.save_run(completed_run)
    except Exception:
        cas_run.status = RunStatus.FAILED
        cas_run.failure_code = "PDX_EXECUTION_FAILURE"
        cas_run.is_retryable = True
        cas_run.error_summary = "PDX deterministic artifact generation encountered an internal error upon retry."
        store.save_run(cas_run)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="PDX artifact generation retry failed. Correlated trace indexed for investigation.",
        )

    store.add_message(
        Message(
            space_id=space_id,
            sender_uid="agent_studiotower",
            sender_name="StudioTower Agent",
            role=MessageRole.AGENT,
            content=f"🔄 **Run `{run.run_id}` Retried & Successfully Completed!**\nArtifacts generated and re-indexed.",
            project_tag=tag,
            attachment_file_ids=completed_run.output_artifact_ids,
        )
    )

    return completed_run
