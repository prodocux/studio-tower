import hashlib
import hmac
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal, Optional

from app.core.config import settings
from app.models.run import Run, RunStatus
from app.models.space import Space
from app.services.deliverable_service import DeliverableExecutionService
from app.services.storage import store
from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

logger = logging.getLogger("studiotower.maintenance")
router = APIRouter(prefix="/v1/maintenance", tags=["maintenance"])


class MaintenanceReconcileResponse(BaseModel):
    status: str
    timestamp: datetime
    metrics: dict
    errors: list[str] = []


class CreateSandboxRequest(BaseModel):
    name: Optional[str] = "Staging Sandbox"
    owner_uid: Optional[str] = None
    owner_email: Optional[str] = None
    ttl_hours: float = 1.0


class CreateSandboxResponse(BaseModel):
    space_id: str
    name: str
    expires_at: datetime
    is_sandbox: bool = True


class FailureInjectionRequest(BaseModel):
    space_id: str
    test_run_key: str = Field(..., min_length=1, max_length=64, description="Deterministic idempotency key for this test run")
    failure_code: Literal["PDX_RESOURCE_CONFLICT"] = "PDX_RESOURCE_CONFLICT"


class FailureInjectionResponse(BaseModel):
    status: str
    space_id: str
    run_id: str
    trace_id: str
    failure_code: str
    is_simulated_failure: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def _verify_maintenance_secret(
    secret: Optional[str] = None,
    authorization: Optional[str] = None,
) -> None:
    """
    Timing-safe verification of maintenance authorization.
    Accepts either:
    1. Google Cloud OIDC ID token in Authorization: Bearer <ID_TOKEN>
       verified by Google with issuer https://accounts.google.com and service account email
       authorized for this project.
    2. Shared X-StudioTower-Maintenance-Secret matching primary or previous configured secret.
    """
    # 1. Attempt Google OIDC Token verification
    if authorization and authorization.startswith("Bearer "):
        bearer_token = authorization.split(" ", 1)[1].strip()
        try:
            from google.oauth2 import id_token
            from google.auth.transport import requests as google_requests

            expected_audience = (
                getattr(settings, "STUDIO_TOWER_SCHEDULER_AUDIENCE", None)
                or getattr(settings, "STUDIO_TOWER_WORKER_SERVICE_URL", None)
            )
            if not expected_audience or expected_audience == "http://localhost:8000":
                raise ValueError("STUDIO_TOWER_SCHEDULER_AUDIENCE or valid HTTPS STUDIO_TOWER_WORKER_SERVICE_URL required for OIDC verification.")

            expected_sa = getattr(settings, "STUDIO_TOWER_SCHEDULER_SA", None)
            if not expected_sa:
                raise ValueError("STUDIO_TOWER_SCHEDULER_SA is required for OIDC authentication.")

            # Pass expected audience to Google verifier.
            # verify_oauth2_token raises ValueError / GoogleAuthError if audience mismatch or missing.
            claim = id_token.verify_oauth2_token(
                bearer_token,
                google_requests.Request(),
                audience=expected_audience,
            )

            issuer = claim.get("iss", "")
            if issuer not in ("https://accounts.google.com", "accounts.google.com"):
                raise ValueError(f"Invalid OIDC issuer: {issuer}")
            if not claim.get("email_verified", False):
                raise ValueError("OIDC email not verified")

            # Double check claim audience
            aud = claim.get("aud")
            if aud != expected_audience:
                raise ValueError(f"OIDC audience mismatch: expected '{expected_audience}', got '{aud}'")

            caller_email = claim.get("email", "")
            if caller_email != expected_sa:
                raise ValueError(f"OIDC caller email '{caller_email}' does not match required STUDIO_TOWER_SCHEDULER_SA '{expected_sa}'")

            logger.info("Maintenance authenticated via Google Cloud OIDC service account: %s", caller_email)
            return
        except Exception as e:
            logger.warning("Google OIDC token verification failed: %s", e)

    # 2. Timing-safe verification of shared maintenance secret
    valid_secrets = [settings.STUDIO_TOWER_MAINTENANCE_SECRET]
    prev_secret = getattr(settings, "STUDIO_TOWER_MAINTENANCE_SECRET_PREVIOUS", None)
    if prev_secret:
        valid_secrets.append(prev_secret)

    if secret:
        for s in valid_secrets:
            if s and hmac.compare_digest(secret.encode("utf-8"), s.encode("utf-8")):
                return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="INVALID_MAINTENANCE_SECRET: Authorization failed",
    )


def _assert_failure_injection_enabled() -> None:
    """Strictly assert failure injection is permitted in the current environment."""
    if settings.ENV in ("production", "prod"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Failure injection endpoints are strictly forbidden in production.",
        )
    if settings.ENV == "staging" and not settings.ENABLE_FAILURE_INJECTION:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Failure injection is disabled in staging (ENABLE_FAILURE_INJECTION=false).",
        )


@router.post("/sandboxes", response_model=CreateSandboxResponse)
def create_sandbox_endpoint(
    payload: Optional[CreateSandboxRequest] = None,
    maintenance_secret: Optional[str] = Header(None, alias="X-StudioTower-Maintenance-Secret"),
):
    """
    Creates an authoritative, server-provisioned ephemeral sandbox space for synthetic testing.
    Strictly forbidden in production. Requires maintenance authorization.
    Optionally assigns owner_uid and owner_email so test users have full space access.
    """
    _verify_maintenance_secret(maintenance_secret)
    _assert_failure_injection_enabled()

    ttl_hours = payload.ttl_hours if payload else 1.0
    expires = datetime.now(UTC) + timedelta(hours=ttl_hours)
    sandbox_id = f"staging-sandbox-{uuid.uuid4().hex[:12]}"
    creator_uid = payload.owner_uid if payload and payload.owner_uid else "system_maintenance"
    space_name = payload.name if payload and payload.name else "Staging Failure Injection Sandbox"

    # Ensure user record exists if owner_uid is supplied
    if payload and payload.owner_uid:
        user_email = payload.owner_email or f"{payload.owner_uid}@studiotower.test"
        if not store.get_user(payload.owner_uid):
            from app.models.user import User
            store.save_user(User(uid=payload.owner_uid, email=user_email, display_name="Staging Test User"))

    sandbox_space = Space(
        space_id=sandbox_id,
        name=space_name,
        created_by=creator_uid,
        is_sandbox=True,
        sandbox_expires_at=expires,
    )
    store.create_space(sandbox_space, creator_uid=creator_uid)
    return CreateSandboxResponse(
        space_id=sandbox_space.space_id,
        name=sandbox_space.name,
        expires_at=expires,
        is_sandbox=True,
    )


@router.post("/sandboxes/{space_id}/teardown")
def teardown_sandbox_endpoint(
    space_id: str,
    maintenance_secret: Optional[str] = Header(None, alias="X-StudioTower-Maintenance-Secret"),
):
    """
    Durably teardowns an ephemeral sandbox space and cascades deletion across all associated entities:
    memberships, invites, runs, messages, files, chunks, artifacts, diagnoses, claims, and metrics.
    Strictly forbidden in production. Requires maintenance authorization.
    """
    _verify_maintenance_secret(maintenance_secret)
    _assert_failure_injection_enabled()

    space = store.get_space(space_id)
    if not space:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sandbox space not found.")
    if not getattr(space, "is_sandbox", False):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="CANNOT_TEARDOWN_NON_SANDBOX: Target space is not an ephemeral sandbox.",
        )

    summary = store.delete_sandbox_cascade(space_id)
    if not summary.get("deleted"):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Sandbox cascade teardown failed or uncompleted: {summary}",
        )
    return {
        "status": "teardown_complete",
        "space_id": space_id,
        "summary": summary,
    }

@router.get("/sandboxes/{space_id}/cleanup-status")
def get_sandbox_cleanup_status_endpoint(
    space_id: str,
    maintenance_secret: Optional[str] = Header(None, alias="X-StudioTower-Maintenance-Secret"),
):
    """
    Checks the status of a sandbox cleanup job.
    Strictly forbidden in production. Requires maintenance authorization.
    """
    _verify_maintenance_secret(maintenance_secret)
    _assert_failure_injection_enabled()

    job = store.get_sandbox_cleanup_job(space_id)
    if not job:
        space = store.get_space(space_id)
        if space:
            return {"space_id": space_id, "phase": "pending", "status": "pending"}
        return {"space_id": space_id, "phase": "completed", "status": "completed"}

    phase = job.get("phase") or job.get("current_phase", "completed")
    return {
        "space_id": space_id,
        "phase": phase,
        "status": "completed" if phase == "completed" else "in_progress",
        "attempts": job.get("attempts", 0),
        "lease_owner": job.get("lease_owner"),
    }


@router.get("/sandboxes/{space_id}/verify-empty")
def verify_sandbox_empty_endpoint(
    space_id: str,
    maintenance_secret: Optional[str] = Header(None, alias="X-StudioTower-Maintenance-Secret"),
):
    """
    Verifies that a sandbox space, its child records across all collections, and all associated blobs are zero.
    Strictly forbidden in production. Requires maintenance authorization.
    """
    _verify_maintenance_secret(maintenance_secret)
    _assert_failure_injection_enabled()

    res = store.verify_sandbox_empty(space_id)
    if not res.get("empty"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Sandbox space '{space_id}' is not fully cleaned: {res}",
        )
    return {
        "status": "verified_empty",
        "space_id": space_id,
        "empty": True,
        "details": res,
    }


class MintCustomTokenRequest(BaseModel):
    identity: Literal["staging_test_owner", "staging_test_coordinator"]


class MintCustomTokenResponse(BaseModel):
    custom_token: str
    identity: str
    uid: str
    email: str
    expires_in_seconds: int = 3600


class RevokeTestUserRequest(BaseModel):
    identity: Literal["staging_test_owner", "staging_test_coordinator"]


ALLOWED_TEST_IDENTITIES = {
    "staging_test_owner": {
        "uid": "staging_test_owner_uid",
        "email": "staging-owner@studiotower.test",
        "name": "Staging Test Owner",
    },
    "staging_test_coordinator": {
        "uid": "staging_test_coordinator_uid",
        "email": "staging-coordinator@studiotower.test",
        "name": "Staging Test Coordinator",
    },
}


@router.post("/test-auth/mint-custom-token", response_model=MintCustomTokenResponse)
def mint_custom_token_endpoint(
    payload: MintCustomTokenRequest,
    maintenance_secret: Optional[str] = Header(None, alias="X-StudioTower-Maintenance-Secret"),
):
    """
    Mints a short-lived Firebase custom token for controlled synthetic Playwright tests.
    Strictly forbidden in production (returns 404).
    Guarded by maintenance secret and strict identity allowlist.
    """
    _verify_maintenance_secret(maintenance_secret)
    _assert_failure_injection_enabled()

    if payload.identity not in ALLOWED_TEST_IDENTITIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Identity '{payload.identity}' not in permitted test identities allowlist.",
        )

    info = ALLOWED_TEST_IDENTITIES[payload.identity]
    uid = info["uid"]
    email = info["email"]
    name = info["name"]

    # Pre-register user in storage
    from app.models.user import User
    existing = store.get_user(uid)
    if not existing:
        store.save_user(User(uid=uid, email=email, display_name=name))

    token_str = ""
    try:
        import firebase_admin
        from firebase_admin import auth as firebase_auth
        if not firebase_admin._apps:
            options = {}
            if settings.STUDIO_TOWER_FIREBASE_PROJECT_ID:
                options["projectId"] = settings.STUDIO_TOWER_FIREBASE_PROJECT_ID
            firebase_admin.initialize_app(options=options if options else None)
        token_bytes = firebase_auth.create_custom_token(uid, developer_claims={"test_identity": payload.identity})
        token_str = token_bytes.decode("utf-8") if isinstance(token_bytes, bytes) else str(token_bytes)
    except Exception as e:
        logger.error("Firebase Admin create_custom_token failed: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="FIREBASE_AUTH_UNAVAILABLE: Failed to mint authoritative custom token.",
        )

    return MintCustomTokenResponse(
        custom_token=token_str,
        identity=payload.identity,
        uid=uid,
        email=email,
        expires_in_seconds=3600,
    )


@router.post("/test-auth/revoke-test-user")
def revoke_test_user_endpoint(
    payload: RevokeTestUserRequest,
    maintenance_secret: Optional[str] = Header(None, alias="X-StudioTower-Maintenance-Secret"),
):
    """
    Revokes refresh tokens for synthetic test users.
    Strictly forbidden in production.
    """
    _verify_maintenance_secret(maintenance_secret)
    _assert_failure_injection_enabled()

    if payload.identity not in ALLOWED_TEST_IDENTITIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid test identity",
        )

    uid = ALLOWED_TEST_IDENTITIES[payload.identity]["uid"]
    try:
        import firebase_admin
        from firebase_admin import auth as firebase_auth
        if not firebase_admin._apps:
            options = {}
            if settings.STUDIO_TOWER_FIREBASE_PROJECT_ID:
                options["projectId"] = settings.STUDIO_TOWER_FIREBASE_PROJECT_ID
            firebase_admin.initialize_app(options=options if options else None)
        firebase_auth.revoke_refresh_tokens(uid)
    except Exception as e:
        trace_id = uuid.uuid4().hex[:16]
        logger.error("Revoke refresh tokens error [trace_id=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"TOKEN_REVOCATION_FAILED (trace_id: {trace_id}, retryable: true)",
        )

    return {"status": "revoked", "identity": payload.identity, "uid": uid}


@router.post("/failure-injection/simulate", response_model=FailureInjectionResponse)
def simulate_failure_endpoint(
    payload: FailureInjectionRequest,
    maintenance_secret: Optional[str] = Header(None, alias="X-StudioTower-Maintenance-Secret"),
):
    """
    Dedicated, server-isolated failure injection simulation endpoint.
    Guarded by:
    1. ENV in staging/test and ENABLE_FAILURE_INJECTION is True (forbidden in prod).
    2. Timing-safe maintenance secret comparison (hmac.compare_digest).
    3. Target space MUST exist, have is_sandbox=True, and sandbox_expires_at > now.
    4. Deterministic, idempotent Run creation with sanitized synthetic prompt.
    5. Authentic OTLP failure trace generation via OpenTelemetry and Grafana MCP facade.
    """
    _verify_maintenance_secret(maintenance_secret)
    _assert_failure_injection_enabled()

    space = store.get_space(payload.space_id)
    if not space:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Space '{payload.space_id}' not found.",
        )

    now_dt = datetime.now(UTC)
    if not space.is_sandbox or not space.sandbox_expires_at or space.sandbox_expires_at <= now_dt:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failure injection is strictly restricted to active, unexpired server-provisioned sandboxes.",
        )

    deterministic_run_id = f"run_fail_{hashlib.sha256(f'{payload.space_id}:{payload.test_run_key}'.encode()).hexdigest()[:16]}"
    existing_run = store.get_run(deterministic_run_id)
    if existing_run:
        return FailureInjectionResponse(
            run_id=existing_run.run_id,
            space_id=existing_run.space_id,
            status=existing_run.status.value if hasattr(existing_run.status, "value") else str(existing_run.status),
            failure_code=existing_run.failure_code or payload.failure_code,
            trace_id=existing_run.trace_id,
        )

    # Create deterministic failed run with sanitized prompt and authentic OTLP trace
    run_record = Run(
        run_id=deterministic_run_id,
        space_id=space.space_id,
        project_tag="general",
        status=RunStatus.FAILED,
        prompt="SIMULATED_FAILURE_INJECTION: Synthetic fault verification",
        created_by="system_maintenance",
        failure_code=payload.failure_code,
        error_summary="PDX Gate Constraint Violation: Exclusive tech asset lock conflict.",
    )
    from app.agent.brain import AgentBrain
    from app.integrations.grafana_mcp import grafana_mcp
    from app.core.otel import get_tracer, flush_telemetry
    from opentelemetry.trace import StatusCode

    tracer = get_tracer("studiotower.maintenance")
    with tracer.start_as_current_span("simulate_failure_injection") as span:
        span.set_attribute("space_id", space.space_id)
        span.set_attribute("run_id", deterministic_run_id)
        span.set_attribute("failure_code", payload.failure_code)
        span.set_attribute("test_run_key", payload.test_run_key)
        span.set_attribute("is_simulated_failure", True)
        span.set_status(StatusCode.ERROR, f"Simulated failure: {payload.failure_code}")

        grafana_mcp.record_simulated_failure_trace(run_record.trace_id, run_record.run_id, space.space_id)
        AgentBrain.diagnose_run_with_grafana(run_record, space.space_id)
        run_record = store.save_run(run_record)

    flush_telemetry(timeout_millis=1000)

    return FailureInjectionResponse(
        run_id=run_record.run_id,
        space_id=run_record.space_id,
        status="failed",
        failure_code=run_record.failure_code,
        trace_id=run_record.trace_id,
    )


@router.post("/reconcile-actions-and-cleanup", response_model=MaintenanceReconcileResponse)
def reconcile_actions_and_cleanup_endpoint(
    maintenance_secret: Optional[str] = Header(None, alias="X-StudioTower-Maintenance-Secret"),
    authorization: Optional[str] = Header(None, alias="Authorization"),
):
    """
    Internal reconciliation endpoint called periodically by Cloud Scheduler.
    Runs 6 maintenance cycles with error isolation:
    1. Pending & failed dispatch recovery.
    2. Stalled action execution lease reclamation.
    3. Orphaned / uncommitted artifact blob cleanup sweep.
    4. Stalled/expired approval runs.
    5. Pending telemetry verification runs.
    6. Expired sandbox spaces and associated test runs.
    """
    _verify_maintenance_secret(maintenance_secret, authorization=authorization)

    metrics = {
        "dispatches_reclaimed": 0,
        "executions_reclaimed": 0,
        "cleanups_swept": 0,
        "stalled_approvals_reconciled": 0,
        "telemetry_reconciled": 0,
        "sandboxes_swept": 0,
    }
    errors = []

    # Cycle 1: Recover pending/failed dispatches
    try:
        metrics["dispatches_reclaimed"] = DeliverableExecutionService.reclaim_pending_dispatches()
    except Exception as e:
        logger.error(f"Maintenance cycle 1 (dispatches) error: {e}")
        errors.append(f"dispatches: {e}")

    # Cycle 2: Reclaim stalled action executions
    try:
        metrics["executions_reclaimed"] = DeliverableExecutionService.reclaim_stalled_action_executions()
    except Exception as e:
        logger.error(f"Maintenance cycle 2 (executions) error: {e}")
        errors.append(f"executions: {e}")

    # Cycle 3: Sweep orphaned artifact cleanups
    try:
        metrics["cleanups_swept"] = DeliverableExecutionService.sweep_artifact_cleanups()
    except Exception as e:
        logger.error(f"Maintenance cycle 3 (cleanups) error: {e}")
        errors.append(f"cleanups: {e}")

    # Cycle 4: Reconcile stalled/expired approval runs
    try:
        metrics["stalled_approvals_reconciled"] = DeliverableExecutionService.scan_and_reconcile_stalled_approvals()
    except Exception as e:
        logger.error(f"Maintenance cycle 4 (approvals) error: {e}")
        errors.append(f"approvals: {e}")

    # Cycle 5: Reconcile pending telemetry verification runs
    try:
        from app.services.telemetry_service import TelemetryService
        telemetry_stats = TelemetryService.reconcile_pending_telemetry_runs(limit=100)
        metrics["telemetry_reconciled"] = telemetry_stats.get("checked", 0)
        metrics["telemetry_stats"] = telemetry_stats
    except Exception as e:
        logger.error(f"Maintenance cycle 5 (telemetry) error: {e}")
        errors.append(f"telemetry: {e}")

    # Cycle 6: Sweep expired sandboxes (durable cascade deletion)
    try:
        now_dt = datetime.now(UTC)
        swept = store.sweep_expired_sandboxes(now_dt=now_dt, limit=50)
        metrics["sandboxes_swept"] = swept
    except Exception as e:
        logger.error(f"Maintenance cycle 6 (sandboxes) error: {e}")
        errors.append(f"sandboxes: {e}")

    return MaintenanceReconcileResponse(
        status="success" if not errors else "partial_success",
        timestamp=datetime.now(UTC),
        metrics=metrics,
        errors=errors,
    )

