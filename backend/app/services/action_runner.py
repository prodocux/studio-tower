import hashlib
import json
import logging
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Optional

from app.core.config import settings
from app.core.otel import trace_stage, inject_traceparent, extract_traceparent
from app.core.telemetry_tenant import compute_tenant_hash
from app.models.action_proposal import ActionProposal
from app.models.user import User
from app.services.deliverable_service import DeliverableExecutionService
from app.services.storage import store

logger = logging.getLogger("studiotower.action_runner")

_action_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="action_worker")


def _execute_action_worker(
    space_id: str,
    action_id: str,
    user_id: str,
    worker_id: Optional[str] = None,
    parent_traceparent: Optional[str] = None,
):
    try:
        parent_ctx = extract_traceparent({"traceparent": parent_traceparent}) if parent_traceparent else None
        user = store.get_user(user_id) or User(uid=user_id, email=f"{user_id}@system.internal", display_name="Internal Worker")
        exec_rec = store.get_action_execution(action_id)
        proposal = None
        if exec_rec and exec_rec.proposal_snapshot:
            proposal = exec_rec.proposal_snapshot
        else:
            msgs = store.list_messages(space_id)
            for m in reversed(msgs):
                if m.proposed_action and m.proposed_action.action_id == action_id:
                    proposal = m.proposed_action
                    break

        if not proposal:
            logger.error(f"Action execution {action_id} not found for worker execution")
            return
        DeliverableExecutionService.execute_action(proposal, user, worker_id=worker_id, parent_context=parent_ctx)
    except Exception as e:
        logger.exception(f"Action execution worker failed for {action_id}: {e}")


class ActionRunner(ABC):
    """Abstract job runner for asynchronous deliverable action executions."""

    @abstractmethod
    def dispatch_action(
        self,
        proposal: ActionProposal,
        user_id: str,
        dispatch_generation: int = 1,
    ) -> str:
        """Dispatches the action and returns deterministic task name or identifier."""
        pass


class InlineActionRunner(ActionRunner):
    """Executes action synchronously in the calling thread (local dev & tests)."""

    def dispatch_action(
        self,
        proposal: ActionProposal,
        user_id: str,
        dispatch_generation: int = 1,
    ) -> str:
        user = store.get_user(user_id) or User(uid=user_id, email=f"{user_id}@test.internal", display_name="Inline User")
        DeliverableExecutionService.execute_action(proposal, user)
        action_hash = hashlib.sha256(proposal.action_id.encode("utf-8")).hexdigest()[:32]
        return f"inline-action-{action_hash}-d{dispatch_generation}"


class ThreadPoolActionRunner(ActionRunner):
    """Dispatches action execution to background worker threads."""

    def dispatch_action(
        self,
        proposal: ActionProposal,
        user_id: str,
        dispatch_generation: int = 1,
    ) -> str:
        space_hash, _ = compute_tenant_hash(proposal.space_id)
        exec_rec = store.get_action_execution(proposal.action_id)
        run_id = exec_rec.run_id if exec_rec else f"run_{proposal.action_id}"

        with trace_stage(
            "action_dispatch",
            space_id_hash=space_hash,
            run_id=run_id,
            stage="dispatch",
            action_type=proposal.action_type,
        ):
            carrier = {}
            inject_traceparent(carrier)
            _action_executor.submit(
                _execute_action_worker,
                proposal.space_id,
                proposal.action_id,
                user_id,
                None,
                carrier.get("traceparent"),
            )
        action_hash = hashlib.sha256(proposal.action_id.encode("utf-8")).hexdigest()[:32]
        return f"threadpool-action-{action_hash}-d{dispatch_generation}"


class CloudTasksActionRunner(ActionRunner):
    """
    Production durable Cloud Tasks runner.
    Enqueues an HTTP task to Google Cloud Tasks with deterministic naming and AlreadyExists absorption.
    """

    def __init__(self):
        try:
            from google.cloud import tasks_v2
            self.client = tasks_v2.CloudTasksClient()
        except Exception as e:
            logger.warning(f"Could not initialize CloudTasksClient for ActionRunner: {e}")
            self.client = None

    def dispatch_action(
        self,
        proposal: ActionProposal,
        user_id: str,
        dispatch_generation: int = 1,
    ) -> str:
        if not self.client:
            raise RuntimeError("CloudTasksClient is not initialized.")

        project = settings.STUDIO_TOWER_CLOUD_TASKS_PROJECT or settings.STUDIO_TOWER_FIREBASE_PROJECT_ID
        location = settings.STUDIO_TOWER_CLOUD_TASKS_LOCATION
        queue = settings.STUDIO_TOWER_ACTION_QUEUE
        queue_path = self.client.queue_path(project, location, queue)

        action_hash = hashlib.sha256(proposal.action_id.encode("utf-8")).hexdigest()[:32]
        task_name = f"{queue_path}/tasks/action-{action_hash}-d{dispatch_generation}"

        target_url = f"{settings.STUDIO_TOWER_WORKER_SERVICE_URL.rstrip('/')}/v1/spaces/{proposal.space_id}/actions/{proposal.action_id}/execute"
        payload = {
            "space_id": proposal.space_id,
            "action_id": proposal.action_id,
            "user_id": user_id,
        }

        space_hash, _ = compute_tenant_hash(proposal.space_id)
        exec_rec = store.get_action_execution(proposal.action_id)
        run_id = exec_rec.run_id if exec_rec else f"run_{proposal.action_id}"

        with trace_stage(
            "action_dispatch",
            space_id_hash=space_hash,
            run_id=run_id,
            stage="dispatch",
            action_type=proposal.action_type,
        ):
            task_headers = {
                "Content-Type": "application/json",
                "X-StudioTower-Task-Secret": settings.STUDIO_TOWER_TASK_SECRET,
            }
            inject_traceparent(task_headers)

            task = {
                "name": task_name,
                "http_request": {
                    "http_method": 1,  # POST
                    "url": target_url,
                    "headers": task_headers,
                    "body": json.dumps(payload).encode("utf-8"),
                },
            }

            try:
                self.client.create_task(parent=queue_path, task=task)
                logger.info(f"Dispatched action {proposal.action_id} to Cloud Tasks task {task_name}")
                return task_name
            except Exception as e:
                err_str = str(e)
                if "AlreadyExists" in err_str or "ALREADY_EXISTS" in err_str or "409" in err_str:
                    logger.info(f"Cloud Task {task_name} already exists; absorbing as idempotent dispatch success.")
                    return task_name
                logger.error(f"Failed to enqueue action {proposal.action_id} to Cloud Tasks: {e}")
                raise


def get_action_runner() -> ActionRunner:
    runner_type = settings.ACTION_RUNNER
    if runner_type == "cloud_tasks":
        return CloudTasksActionRunner()
    elif runner_type == "threadpool":
        return ThreadPoolActionRunner()
    return InlineActionRunner()
