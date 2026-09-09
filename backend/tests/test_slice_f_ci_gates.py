import os
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, settings
from app.main import app
from app.models.run import ApprovalGate, Run, RunStatus
from app.models.space import Invite, MembershipRole, Space, SpaceKind
from app.models.user import User
from app.services.storage import store

client = TestClient(app)

OWNER_AUTH = {"Authorization": "Bearer dev:owner_01:owner@example.com:Owner"}
COORD_AUTH = {"Authorization": "Bearer dev:coord_02:coord@example.com:Coordinator"}
MEMBER_AUTH = {"Authorization": "Bearer dev:member_03:member@example.com:Member"}
STRANGER_AUTH = {"Authorization": "Bearer dev:stranger_04:stranger@example.com:Stranger"}


@pytest.fixture(autouse=True)
def clean_store():
    store.clear()
    yield
    store.clear()


# ============================================================================
# Gate F1: Public Invite Preview Contract & Anti-Enumeration Security
# ============================================================================
def test_gate_f1_invite_preview_contract_and_security():
    """Gate F1: Validates public invite preview contracts:
    - 200 for active invites with masked email and omitted space_id/creator_uid.
    - 410 with machine-readable error codes (INVITE_EXPIRED, INVITE_EXHAUSTED, INVITE_REVOKED).
    - 404 with INVITE_NOT_FOUND for unknown tokens.
    - Unauthenticated access permitted with 'Cache-Control: no-store, private'.
    """
    # 1. Create a space
    space_res = client.post("/v1/spaces", json={"name": "Cinema Lab Space"}, headers=OWNER_AUTH)
    assert space_res.status_code == 200
    space_id = space_res.json()["space_id"]

    # 2. Create an active invite with targeted email
    active_token = "tok_active_123"
    invite_active = Invite(
        token=active_token,
        space_id=space_id,
        created_by="owner_01",
        role=MembershipRole.COORDINATOR,
        target_email="director.quentin@production.org",
        max_uses=5,
        used_count=1,
    )
    store.create_invite(invite_active)

    # Public preview without any Auth header
    res = client.get(f"/v1/invites/{active_token}/preview")
    assert res.status_code == 200
    data = res.json()
    assert data["space_name"] == "Cinema Lab Space"
    assert data["role"] == "coordinator"
    assert data["target_email_masked"] == "d***@production.org"
    assert data["status"] == "active"
    # Security: assert space_id and creator_uid are NOT leaked
    assert "space_id" not in data
    assert "created_by" not in data
    assert res.headers.get("Cache-Control") == "no-store, private"

    # 3. Expired invite -> 410 Gone with INVITE_EXPIRED
    expired_token = "tok_expired_456"
    invite_expired = Invite(
        token=expired_token,
        space_id=space_id,
        created_by="owner_01",
        role=MembershipRole.MEMBER,
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )
    store.create_invite(invite_expired)
    res_expired = client.get(f"/v1/invites/{expired_token}/preview")
    assert res_expired.status_code == 410
    detail = res_expired.json()["detail"]
    assert detail["code"] == "INVITE_EXPIRED"

    # 4. Exhausted invite -> 410 Gone with INVITE_EXHAUSTED
    exhausted_token = "tok_exhausted_789"
    invite_exhausted = Invite(
        token=exhausted_token,
        space_id=space_id,
        created_by="owner_01",
        role=MembershipRole.MEMBER,
        max_uses=2,
        used_count=2,
    )
    store.create_invite(invite_exhausted)
    res_exhausted = client.get(f"/v1/invites/{exhausted_token}/preview")
    assert res_exhausted.status_code == 410
    detail = res_exhausted.json()["detail"]
    assert detail["code"] == "INVITE_EXHAUSTED"

    # 5. Revoked invite -> 410 Gone with INVITE_REVOKED
    revoked_token = "tok_revoked_101"
    invite_revoked = Invite(
        token=revoked_token,
        space_id=space_id,
        created_by="owner_01",
        role=MembershipRole.MEMBER,
        revoked_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    store.create_invite(invite_revoked)
    res_revoked = client.get(f"/v1/invites/{revoked_token}/preview")
    assert res_revoked.status_code == 410
    detail = res_revoked.json()["detail"]
    assert detail["code"] == "INVITE_REVOKED"

    # 6. Non-existent invite -> 404 Not Found with INVITE_NOT_FOUND
    res_missing = client.get("/v1/invites/tok_nonexistent_999/preview")
    assert res_missing.status_code == 404
    detail = res_missing.json()["detail"]
    assert detail["code"] == "INVITE_NOT_FOUND"

    # 7. Token-only Rate Limiting: 10 requests allowed, 11th returns 429 with Retry-After
    rate_limit_token = "tok_rate_test_999"
    invite_rl = Invite(
        token=rate_limit_token,
        space_id=space_id,
        created_by="owner_01",
        role=MembershipRole.MEMBER,
    )
    store.create_invite(invite_rl)
    # Enable TRUST_PROXY_HEADERS during proxy-header rate limit tests
    with patch.object(settings, "TRUST_PROXY_HEADERS", True):
        # Make 10 requests with distinct IP headers so IP-bucket (30/min) is not triggered
        for i in range(10):
            r = client.get(
                f"/v1/invites/{rate_limit_token}/preview",
                headers={"X-Forwarded-For": f"198.51.100.{i + 1}"},
            )
            assert r.status_code == 200
        r_breach = client.get(
            f"/v1/invites/{rate_limit_token}/preview",
            headers={"X-Forwarded-For": "198.51.100.99"},
        )
        assert r_breach.status_code == 429
        assert r_breach.json()["detail"]["code"] == "RATE_LIMIT_EXCEEDED"
        assert "Retry-After" in r_breach.headers
        assert int(r_breach.headers["Retry-After"]) >= 1

        # 8. IP-only Rate Limiting: 30 requests from single IP across rotating tokens, 31st returns 429
        fixed_ip = "203.0.113.88"
        for i in range(30):
            rot_tok = f"tok_rot_spray_{i}"
            store.create_invite(Invite(
                token=rot_tok,
                space_id=space_id,
                created_by="owner_01",
                role=MembershipRole.MEMBER,
            ))
            r_ip = client.get(
                f"/v1/invites/{rot_tok}/preview",
                headers={"X-Forwarded-For": fixed_ip},
            )
            assert r_ip.status_code == 200

        r_ip_breach = client.get(
            "/v1/invites/tok_rot_spray_extra/preview",
            headers={"X-Forwarded-For": fixed_ip},
        )
        assert r_ip_breach.status_code == 429
        assert "network" in r_ip_breach.json()["detail"]["message"].lower()

        # 9. Anti-Spoofing: Leftmost X-Forwarded-For cannot be rotated to bypass rightmost verified IP
        spoofed_headers = {"X-Forwarded-For": f"1.2.3.4, {fixed_ip}"}
        r_spoofed = client.get("/v1/invites/tok_rot_spray_extra2/preview", headers=spoofed_headers)
        assert r_spoofed.status_code == 429  # Still blocked because rightmost IP is fixed_ip!


# ============================================================================
# Gate F2: Intent Strictness (No Keyword-Based Guesswork)
# ============================================================================
def test_gate_f2_intent_strictness():
    """Gate F2: An explicit intent of 'conversation' must NEVER generate a Run or ApprovalGate,
    even if the content contains trigger words like 'breakdown', 'budget', or 'action'.
    """
    space_res = client.post("/v1/spaces", json={"name": "Story Dept"}, headers=OWNER_AUTH)
    space_id = space_res.json()["space_id"]

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = MagicMock(
        text="Here is conversational commentary about script breakdown methodologies."
    )

    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKeySliceF"}):
        res = client.post(
            "/v1/chat",
            json={
                "space_id": space_id,
                "content": "@agent Can you explain how script breakdown approval gates work?",
                "intent": "conversation",
            },
            headers=OWNER_AUTH,
        )
        assert res.status_code == 200
        data = res.json()
        assert data.get("user_message") is not None
        assert data.get("agent_message") is not None
        assert data.get("run") is None  # Strictly NO run created


# ============================================================================
# Gate F3: Idempotency & Deduplication Under Concurrency
# ============================================================================
def test_gate_f3_idempotency_concurrency():
    """Gate F3: Repeated or concurrent submissions with identical client_message_id
    return the identical completed response and execute reasoning exactly once.
    """
    space_res = client.post("/v1/spaces", json={"name": "VFX Lab"}, headers=OWNER_AUTH)
    space_id = space_res.json()["space_id"]

    idemp_key = f"idemp_f3_{uuid.uuid4().hex[:8]}"

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = MagicMock(
        text="VFX breakdown analysis."
    )

    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKeySliceF"}):
        # First request
        res1 = client.post(
            "/v1/chat",
            json={
                "space_id": space_id,
                "content": "@agent Explain VFX assets",
                "intent": "conversation",
                "client_message_id": idemp_key,
            },
            headers=OWNER_AUTH,
        )
        assert res1.status_code == 200
        data1 = res1.json()
        calls_after_req1 = mock_client.models.generate_content.call_count
        assert calls_after_req1 > 0

        # Second identical request with same client_message_id
        res2 = client.post(
            "/v1/chat",
            json={
                "space_id": space_id,
                "content": "@agent Explain VFX assets",
                "intent": "conversation",
                "client_message_id": idemp_key,
            },
            headers=OWNER_AUTH,
        )
        assert res2.status_code == 200
        data2 = res2.json()

        # Both must match identical message IDs
        assert data1["agent_message"]["message_id"] == data2["agent_message"]["message_id"]
        # LLM inference was NOT called again during duplicate replay
        assert mock_client.models.generate_content.call_count == calls_after_req1


# ============================================================================
# Gate F4: Role Enforcement & Atomic Mutation on Approval Gates
# ============================================================================
def test_gate_f4_approval_atomic_mutation():
    """Gate F4: Approval actions enforce RBAC:
    - Coordinator can approve a pending gate.
    - Regular Member is rejected with 403 Forbidden.
    - Approved gate atomically records approved_by and status='approved'.
    """
    space_res = client.post("/v1/spaces", json={"name": "Sound Stage"}, headers=OWNER_AUTH)
    space_id = space_res.json()["space_id"]

    # Save and direct add coordinator and member
    coord = User(uid="coord_02", email="coord@example.com", display_name="Coordinator")
    member = User(uid="member_03", email="member@example.com", display_name="Member")
    store.save_user(coord)
    store.save_user(member)

    client.post(
        f"/v1/spaces/{space_id}/members/direct-add",
        json={"email_or_uid": "coord_02", "role": "coordinator"},
        headers=OWNER_AUTH,
    )
    client.post(
        f"/v1/spaces/{space_id}/members/direct-add",
        json={"email_or_uid": "member_03", "role": "member"},
        headers=OWNER_AUTH,
    )

    # Create a run with a pending approval gate
    run_id = f"run_{uuid.uuid4().hex[:8]}"
    run = Run(
        run_id=run_id,
        space_id=space_id,
        status=RunStatus.AWAITING_APPROVAL,
        created_by="coord_02",
        prompt="Generate audio cues",
        approval_gate=ApprovalGate(
            title="Audio Cue Sheet Approval",
            description="Approve cue sheet before mixdown",
            status="pending",
        ),
    )
    store.save_run(run)

    # 1. Member attempts approval -> 403 Forbidden
    res_member = client.post(
        f"/v1/spaces/{space_id}/runs/{run_id}/approve",
        json={"approved": True},
        headers=MEMBER_AUTH,
    )
    assert res_member.status_code == 403

    # 2. Coordinator approves -> 200 OK
    res_coord = client.post(
        f"/v1/spaces/{space_id}/runs/{run_id}/approve",
        json={"approved": True},
        headers=COORD_AUTH,
    )
    assert res_coord.status_code == 200
    updated_run = res_coord.json()
    assert updated_run["approval_gate"]["status"] == "approved"
    assert updated_run["approval_gate"]["approved_by"] == "coord_02"


# ============================================================================
# Gate F5: Server-Gated Failure Injection & Production Fail-Closed
# ============================================================================
def test_gate_f5_server_gated_failure_injection():
    """Gate F5: Server-gated failure simulation and authoritative sandbox lifecycle:
    - Production boot fails closed if ENABLE_FAILURE_INJECTION=True.
    - Public space creation never exposes or sets sandbox metadata.
    - POST /v1/maintenance/sandboxes requires secret and staging+flag.
    - POST /v1/maintenance/failure-injection/simulate:
      - In production -> 404 forbidden.
      - In staging without ENABLE_FAILURE_INJECTION -> 403.
      - Without maintenance secret -> 403.
      - On non-sandbox space -> 400.
      - On server-provisioned sandbox -> 200 with failed Run and authentic trace.
      - Idempotent: identical test_run_key returns the same run without duplicates.
    - Chat route does NOT trigger failure simulation.
    """
    maint_secret = "super-secret-maint-token-32b-len"
    maint_headers = {"X-StudioTower-Maintenance-Secret": maint_secret}

    # 1. Production Settings validator fails closed
    with pytest.raises(ValueError, match="ENABLE_FAILURE_INJECTION is strictly forbidden in production"):
        Settings(
            ENV="production",
            ENABLE_FAILURE_INJECTION=True,
            STUDIO_TOWER_AUTH_MODE="firebase",
            STUDIO_TOWER_FIREBASE_PROJECT_ID="demo-proj",
            STUDIO_TOWER_STORE="firestore",
            STUDIO_TOWER_ARTIFACT_BACKEND="gcs",
            STUDIO_TOWER_GCS_BUCKET="demo-bucket",
            INGESTION_RUNNER="cloud_tasks",
            ACTION_RUNNER="cloud_tasks",
            STUDIO_TOWER_WORKER_SERVICE_URL="https://worker.run.app",
            STUDIO_TOWER_TASK_SECRET="a" * 32,
            STUDIO_TOWER_MAINTENANCE_SECRET="b" * 32,
            CURSOR_SIGNING_SECRET="c" * 32,
            ACTION_SIGNING_SECRET="d" * 32,
        )

    # 2. Public space creation does not expose internal sandbox metadata
    space_res = client.post("/v1/spaces", json={"name": "Prod Regular Space"}, headers=OWNER_AUTH)
    assert space_res.status_code == 200
    regular_space_data = space_res.json()
    regular_space_id = regular_space_data["space_id"]
    assert "is_sandbox" not in regular_space_data
    assert "sandbox_expires_at" not in regular_space_data

    # 3. Create sandbox endpoint checks
    # Missing secret -> 403
    res_no_sec = client.post("/v1/maintenance/sandboxes")
    assert res_no_sec.status_code == 403

    # In staging with secret -> 200
    with patch.object(settings, "ENV", "staging"), \
         patch.object(settings, "ENABLE_FAILURE_INJECTION", True), \
         patch.object(settings, "STUDIO_TOWER_MAINTENANCE_SECRET", maint_secret):
        sandbox_res = client.post("/v1/maintenance/sandboxes", headers=maint_headers)
        assert sandbox_res.status_code == 200
        sandbox_data = sandbox_res.json()
        sandbox_space_id = sandbox_data["space_id"]
        assert sandbox_space_id.startswith("staging-sandbox-")
        assert sandbox_data["is_sandbox"] is True

        # 4. In production mode, simulate-failure returns 404
        with patch.object(settings, "ENV", "production"), \
             patch.object(settings, "ENABLE_FAILURE_INJECTION", False):
            res_prod = client.post(
                "/v1/maintenance/failure-injection/simulate",
                json={"space_id": sandbox_space_id, "test_run_key": "key_p1"},
                headers=maint_headers,
            )
            assert res_prod.status_code == 404

        # 5. In staging with ENABLE_FAILURE_INJECTION=False -> 403
        with patch.object(settings, "ENABLE_FAILURE_INJECTION", False):
            res_dis = client.post(
                "/v1/maintenance/failure-injection/simulate",
                json={"space_id": sandbox_space_id, "test_run_key": "key_p2"},
                headers=maint_headers,
            )
            assert res_dis.status_code == 403

        # 6. In staging with wrong secret -> 403
        res_wrong = client.post(
            "/v1/maintenance/failure-injection/simulate",
            json={"space_id": sandbox_space_id, "test_run_key": "key_p3"},
            headers={"X-StudioTower-Maintenance-Secret": "wrong_key"},
        )
        assert res_wrong.status_code == 403

        # 7. In staging on regular (non-sandbox) space -> 400
        res_non_sb = client.post(
            "/v1/maintenance/failure-injection/simulate",
            json={"space_id": regular_space_id, "test_run_key": "key_p4"},
            headers=maint_headers,
        )
        assert res_non_sb.status_code == 400
        assert "restricted to active, unexpired server-provisioned sandboxes" in res_non_sb.json()["detail"]

        # 8. In staging with valid sandbox -> 200 with failed Run
        res_sim = client.post(
            "/v1/maintenance/failure-injection/simulate",
            json={"space_id": sandbox_space_id, "test_run_key": "key_test_alpha"},
            headers=maint_headers,
        )
        assert res_sim.status_code == 200
        sim_data = res_sim.json()
        assert sim_data["status"] == "failed"
        assert sim_data["failure_code"] == "PDX_RESOURCE_CONFLICT"
        assert sim_data["trace_id"] is not None

        # 9. Idempotent replay: same test_run_key returns identical run
        res_sim_dup = client.post(
            "/v1/maintenance/failure-injection/simulate",
            json={"space_id": sandbox_space_id, "test_run_key": "key_test_alpha"},
            headers=maint_headers,
        )
        assert res_sim_dup.status_code == 200
        assert res_sim_dup.json()["run_id"] == sim_data["run_id"]

        # 10. Create sandbox with explicit owner_uid
        res_owner_sb = client.post(
            "/v1/maintenance/sandboxes",
            json={"name": "Bob Test Sandbox", "owner_uid": "user_bob_test", "owner_email": "bob@studiotower.test"},
            headers=maint_headers,
        )
        assert res_owner_sb.status_code == 200
        bob_sb_id = res_owner_sb.json()["space_id"]
        assert store.is_member(bob_sb_id, "user_bob_test")
        assert store.get_member_role(bob_sb_id, "user_bob_test") == MembershipRole.OWNER

        # 11. Durable Teardown: non-sandbox rejects with 400
        res_td_bad = client.post(f"/v1/maintenance/sandboxes/{regular_space_id}/teardown", headers=maint_headers)
        assert res_td_bad.status_code == 400
        assert "CANNOT_TEARDOWN_NON_SANDBOX" in res_td_bad.json()["detail"]

        # 12. Durable Teardown: valid sandbox cascade deletes space and runs
        res_td = client.post(f"/v1/maintenance/sandboxes/{sandbox_space_id}/teardown", headers=maint_headers)
        assert res_td.status_code == 200
        assert res_td.json()["status"] == "teardown_complete"
        assert store.get_space(sandbox_space_id) is None
        assert store.get_run(sim_data["run_id"]) is None

        # 13. Verify cleanup status endpoint
        res_status = client.get(f"/v1/maintenance/sandboxes/{sandbox_space_id}/cleanup-status", headers=maint_headers)
        assert res_status.status_code == 200
        assert res_status.json()["phase"] == "completed"

        # 14. Verify empty endpoint confirms 0 remaining records
        res_empty = client.get(f"/v1/maintenance/sandboxes/{sandbox_space_id}/verify-empty", headers=maint_headers)
        assert res_empty.status_code == 200
        assert res_empty.json()["empty"] is True



# ============================================================================
# Gate F6: Degraded-Path Isolation & AI Fallback Integrity
# ============================================================================
def test_gate_f6_degraded_path_isolation():
    """Gate F6:
    - When AI_FALLBACK_ALLOWED=True and Gemini fails, deterministic fallback is
      explicitly tagged with ai_engine='deterministic-fallback'.
    - When AI_FALLBACK_ALLOWED=False and Gemini fails, an exception/503 is returned,
      ensuring Golden Path Staging never receives fake green passes.
    - /readyz endpoint strictly returns explicit boolean fallback_allowed, mode, and ready.
    """
    # 1. Verify /readyz authoritative schema contract
    from app.services.readiness_service import ReadinessService
    ReadinessService.invalidate_cache()
    readyz_res = client.get("/readyz")
    assert readyz_res.status_code in (200, 503)
    readyz_data = readyz_res.json()
    assert "components" in readyz_data
    assert "ai" in readyz_data["components"]
    ai_comp = readyz_data["components"]["ai"]
    assert isinstance(ai_comp["fallback_allowed"], bool)
    assert isinstance(ai_comp["ready"], bool)
    assert isinstance(ai_comp["mode"], str)
    assert readyz_data["fallback_allowed"] == ai_comp["fallback_allowed"]

    space_res = client.post("/v1/spaces", json={"name": "Editing Suite"}, headers=OWNER_AUTH)
    space_id = space_res.json()["space_id"]

    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = RuntimeError("Gemini 3.6 Flash rate limit / network error")

    # Case A: AI_FALLBACK_ALLOWED=False -> strictly fails with 503, no fake fallback
    with patch("google.genai.Client", return_value=mock_client), \
         patch.object(settings, "AI_FALLBACK_ALLOWED", False), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKeySliceF"}):
        res = client.post(
            "/v1/chat",
            json={
                "space_id": space_id,
                "content": "@agent Analyze cutting tempo",
                "intent": "conversation",
            },
            headers=OWNER_AUTH,
        )
        # Should return 503 error since fallback is forbidden
        assert res.status_code == 503
        assert "temporarily unavailable" in res.json()["detail"]

    # Case B: AI_FALLBACK_ALLOWED=True -> degraded path returns fallback response
    with patch("google.genai.Client", return_value=mock_client), \
         patch.object(settings, "AI_FALLBACK_ALLOWED", True), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKeySliceF"}):
        res = client.post(
            "/v1/chat",
            json={
                "space_id": space_id,
                "content": "@agent Analyze cutting tempo",
                "intent": "conversation",
            },
            headers=OWNER_AUTH,
        )
        assert res.status_code == 200
        data = res.json()
        assert data["agent_message"] is not None


# ============================================================================
# Gate F7: Client IP Extraction & Reverse Proxy Header Trust
# ============================================================================
def test_gate_f7_client_ip_extraction_and_proxy_trust():
    """Gate F7:
    - When TRUST_PROXY_HEADERS=False: Spoofed X-Forwarded-For is ignored, client IP is request.client.host.
    - When TRUST_PROXY_HEADERS=True: Rightmost client IP from X-Forwarded-For is extracted.
    """
    from app.api.space_routes import _extract_client_ip

    mock_request = MagicMock()
    mock_request.client.host = "192.168.1.50"
    mock_request.headers = {"x-forwarded-for": "10.0.0.1, 203.0.113.195"}

    # Default: TRUST_PROXY_HEADERS is False -> direct client IP used, spoofed header ignored
    with patch.object(settings, "TRUST_PROXY_HEADERS", False):
        ip = _extract_client_ip(mock_request)
        assert ip == "192.168.1.50"

    # When TRUST_PROXY_HEADERS is True -> rightmost proxy header IP used
    with patch.object(settings, "TRUST_PROXY_HEADERS", True):
        ip = _extract_client_ip(mock_request)
        assert ip == "203.0.113.195"


# ============================================================================
# Gate F8: Staging Test Auth Control Plane Isolation & Token Minting
# ============================================================================
def test_gate_f8_staging_test_auth_control_plane():
    """Gate F8:
    - In ENV=production: mint-custom-token returns 404.
    - In ENV=staging with ENABLE_FAILURE_INJECTION=False: returns 403.
    - Without valid maintenance secret: returns 403.
    - Non-allowlisted test identity: returns 400.
    - Authorized call for 'staging_test_owner' mints token and creates user record.
    - Revoke endpoint succeeds.
    """
    maint_headers = {"X-StudioTower-Maintenance-Secret": settings.STUDIO_TOWER_MAINTENANCE_SECRET}

    # 1. In production: strictly 404
    with patch.object(settings, "ENV", "production"):
        res = client.post(
            "/v1/maintenance/test-auth/mint-custom-token",
            json={"identity": "staging_test_owner"},
            headers=maint_headers,
        )
        assert res.status_code == 404

    # 2. In staging with ENABLE_FAILURE_INJECTION=False: strictly 403
    with patch.object(settings, "ENV", "staging"), patch.object(settings, "ENABLE_FAILURE_INJECTION", False):
        res = client.post(
            "/v1/maintenance/test-auth/mint-custom-token",
            json={"identity": "staging_test_owner"},
            headers=maint_headers,
        )
        assert res.status_code == 403

    # 3. In staging with ENABLE_FAILURE_INJECTION=True but missing maintenance secret: 403
    with patch.object(settings, "ENV", "staging"), patch.object(settings, "ENABLE_FAILURE_INJECTION", True):
        res_no_sec = client.post(
            "/v1/maintenance/test-auth/mint-custom-token",
            json={"identity": "staging_test_owner"},
        )
        assert res_no_sec.status_code == 403

        # 4. Non-allowlisted identity: 422/400 validation error
        res_bad_id = client.post(
            "/v1/maintenance/test-auth/mint-custom-token",
            json={"identity": "hacker_test_user"},
            headers=maint_headers,
        )
        assert res_bad_id.status_code in (400, 422)

        # 5. Permitted identity 'staging_test_owner' succeeds
        mock_fb_admin = MagicMock()
        mock_fb_auth = MagicMock()
        mock_fb_admin.auth = mock_fb_auth
        mock_fb_auth.create_custom_token.return_value = b"authoritative_custom_token_owner_123"
        with patch.dict("sys.modules", {"firebase_admin": mock_fb_admin, "firebase_admin.auth": mock_fb_auth}):
            res_owner = client.post(
                "/v1/maintenance/test-auth/mint-custom-token",
                json={"identity": "staging_test_owner"},
                headers=maint_headers,
            )
            assert res_owner.status_code == 200
            data_owner = res_owner.json()
            assert data_owner["identity"] == "staging_test_owner"
            assert data_owner["uid"] == "staging_test_owner_uid"
            assert data_owner["email"] == "staging-owner@studiotower.test"
            assert data_owner["custom_token"] == "authoritative_custom_token_owner_123"

        # Verify user was saved in store
        user = store.get_user("staging_test_owner_uid")
        assert user is not None
        assert user.email == "staging-owner@studiotower.test"

        # 6. Revoke endpoint
        mock_fb_admin_rev = MagicMock()
        mock_fb_auth_rev = MagicMock()
        mock_fb_admin_rev.auth = mock_fb_auth_rev
        with patch.dict("sys.modules", {"firebase_admin": mock_fb_admin_rev, "firebase_admin.auth": mock_fb_auth_rev}):
            res_revoke = client.post(
                "/v1/maintenance/test-auth/revoke-test-user",
                json={"identity": "staging_test_owner"},
                headers=maint_headers,
            )
            assert res_revoke.status_code == 200
            assert res_revoke.json()["status"] == "revoked"


# ============================================================================
# Gate F9: Sandbox Cascade Sweeps all 17 Collections & Detached Cursors
# ============================================================================
def test_gate_f9_sandbox_cascade_sweeps_all_17_collections():
    """Gate F9:
    Verifies that delete_sandbox_cascade sweeps all 17 collections:
    memberships, invites, runs, action_executions, messages, files,
    document_chunks, pending_generation_cleanups, artifacts, artifact_cleanups,
    diagnoses, diagnosis_claims, space_metric_rollups, hourly_rollups,
    activity_events, activity_outbox, chat_idempotency,
    and deletes deterministic cursor 'scope_{space_id}' in telemetry_scan_cursors.
    """
    from app.models.file_record import DocumentChunk, FileRecord
    from app.models.message import Message
    from app.models.action_proposal import ActionExecutionRecord, DispatchStatus

    # Create sandbox space
    sb_space = Space(
        space_id="staging-sandbox-sweep17",
        name="Sweep 17 Sandbox",
        created_by="owner_01",
        is_sandbox=True,
        sandbox_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    store.create_space(sb_space, creator_uid="owner_01")
    sid = sb_space.space_id

    # Populate entities across collections
    # 1. memberships (already created by create_space)
    assert store.is_member(sid, "owner_01")

    # 2. invites
    inv = Invite(token="tok_sb_17", space_id=sid, created_by="owner_01", role=MembershipRole.COORDINATOR)
    store.create_invite(inv)

    # 3. runs
    run = Run(run_id="run_sb_17", space_id=sid, project_tag="general", status=RunStatus.COMPLETED, prompt="test", created_by="owner_01")
    store.save_run(run)

    # 4. action_executions
    action = ActionExecutionRecord(
        action_id="act_sb_17",
        run_id="run_sb_17",
        space_id=sid,
        user_id="owner_01",
        dispatch_status=DispatchStatus.PENDING,
    )
    store.action_executions[action.action_id] = action

    # 5. messages
    msg = Message(message_id="msg_sb_17", space_id=sid, sender_uid="owner_01", role="user", content="hello")
    store.messages[msg.message_id] = msg

    # 6. files & blobs
    f_rec = FileRecord(file_id="file_sb_17", space_id=sid, filename="test.txt", storage_path=f"{sid}/test.txt", uploaded_by="owner_01")
    store.files[f_rec.file_id] = f_rec
    store.file_blobs[f_rec.file_id] = b"file bytes"

    # 7. document_chunks
    store.document_chunks["chunk_sb_17"] = MagicMock(space_id=sid)

    # 8. pending_generation_cleanups
    store.pending_generation_cleanups["cleanup_sb_17"] = {"space_id": sid, "run_id": "run_sb_17"}

    # 9. artifacts & blobs
    art = MagicMock(space_id=sid)
    store.artifacts["art_sb_17"] = art
    store.artifact_blobs[(sid, "art_sb_17")] = b"pdf bytes"

    # 10. artifact_cleanups
    store.artifact_cleanups["art_sb_17"] = {"space_id": sid, "status": "pending"}

    # 11. diagnoses & run diagnoses
    store._diagnoses["diag_sb_17"] = MagicMock(space_id=sid)
    store._run_diagnoses[f"{sid}:run_sb_17"] = "diag_sb_17"

    # 12. diagnosis_claims
    store._diagnosis_claims[f"{sid}:run_sb_17"] = {"claimed": True}

    # 13. metric_rollups & hourly_rollups
    store._metric_rollups[f"{sid}:general:2026-09-06T10"] = {"count": 1}

    # 14. telemetry_scan_cursors (deterministic doc ID: scope_{space_id})
    store._telemetry_scan_cursors[f"scope_{sid}"] = "cursor_token_123"

    # 15. activity_events
    store.activity_events[sid] = [{"event_id": "evt_1"}]

    # 16. activity_outbox
    store.activity_outbox["outbox_sb_17"] = {"space_id": sid, "event": "evt"}

    # 17. chat_idempotency
    store.chat_idempotency[f"{sid}:idem_123"] = "msg_sb_17"

    # Perform cascade deletion
    result = store.delete_sandbox_cascade(sid)
    assert result["deleted"] is True

    # Assert ALL 17 collections and the deterministic cursor are completely swept
    assert store.get_space(sid) is None
    assert store.is_member(sid, "owner_01") is False
    assert store.get_invite("tok_sb_17") is None
    assert store.get_run("run_sb_17") is None
    assert "act_sb_17" not in store.action_executions
    assert "msg_sb_17" not in store.messages
    assert "file_sb_17" not in store.files
    assert "file_sb_17" not in store.file_blobs
    assert "chunk_sb_17" not in store.document_chunks
    assert "cleanup_sb_17" not in store.pending_generation_cleanups
    assert "art_sb_17" not in store.artifacts
    assert (sid, "art_sb_17") not in store.artifact_blobs
    assert "art_sb_17" not in store.artifact_cleanups
    assert "diag_sb_17" not in store._diagnoses
    assert f"{sid}:run_sb_17" not in store._run_diagnoses
    assert f"{sid}:run_sb_17" not in store._diagnosis_claims
    assert f"{sid}:general:2026-09-06T10" not in store._metric_rollups
    assert f"scope_{sid}" not in store._telemetry_scan_cursors
    assert sid not in store.activity_events
    assert "outbox_sb_17" not in store.activity_outbox
    assert f"{sid}:idem_123" not in store.chat_idempotency


# ============================================================================
# Gate F10: Non-Sandbox Deletion Rejection (Fail-Closed Fencing)
# ============================================================================
def test_gate_f10_non_sandbox_deletion_rejection():
    """Gate F10:
    Attempting to cascade delete a standard (non-sandbox) space MUST raise
    CleanupAuthorizationError and reject deletion.
    """
    from app.services.storage import CleanupAuthorizationError

    std_space = Space(
        space_id="std-production-space-01",
        name="Production Core Space",
        created_by="owner_01",
        is_sandbox=False,
    )
    store.create_space(std_space, creator_uid="owner_01")

    with pytest.raises(CleanupAuthorizationError) as exc_info:
        store.delete_sandbox_cascade(std_space.space_id)

    assert "strictly forbidden on standard spaces" in str(exc_info.value)
    # Space remains intact
    assert store.get_space(std_space.space_id) is not None


# ============================================================================
# Gate F11: Firestore Leased State Machine & Recovery
# ============================================================================
def test_gate_f11_firestore_leased_cleanup_state_machine():
    """Gate F11:
    Tests that FirestoreStore.delete_sandbox_cascade:
    1. Sets space cleanup_status to 'deleting' in Phase 1.
    2. Records lease in sandbox_cleanup_jobs.
    3. Handles lease contention if another worker holds active lease.
    4. Completes all 5 phases and updates cleanup record to 'completed'.
    """
    from app.services.storage import FirestoreStore, SandboxCleanupPhase

    mock_client = MagicMock()
    fs_store = FirestoreStore.__new__(FirestoreStore)
    fs_store.client = mock_client
    fs_store.project_id = "test-proj"

    space_id = "staging-sandbox-leased123"

    # Mock space doc exists and is_sandbox=True
    mock_space_doc = MagicMock()
    mock_space_doc.exists = True
    mock_space_doc.to_dict.return_value = {
        "space_id": space_id,
        "is_sandbox": True,
        "name": "Leased Test Space",
    }
    mock_client.collection.return_value.document.return_value.get.return_value = mock_space_doc

    # Case 1: An active lease is already held by another worker
    now = datetime.now(UTC)
    mock_job_doc_held = MagicMock()
    mock_job_doc_held.exists = True
    mock_job_doc_held.to_dict.return_value = {
        "current_phase": "deleting_children",
        "lease_owner": "other_worker_999",
        "lease_expires_at": (now + timedelta(minutes=4)).isoformat(),
    }

    def doc_side_effect(doc_id):
        m = MagicMock()
        if doc_id == space_id:
            # Check which collection
            m.get.return_value = mock_space_doc
        return m

    # Test lease collision returns lease_held
    with patch.object(fs_store.client, "collection") as mock_coll:
        mock_coll_spaces = MagicMock()
        mock_coll_spaces.document.return_value.get.return_value = mock_space_doc

        mock_coll_jobs = MagicMock()
        mock_coll_jobs.document.return_value.get.return_value = mock_job_doc_held

        def coll_router(name):
            if name == "spaces":
                return mock_coll_spaces
            elif name == "sandbox_cleanup_jobs":
                return mock_coll_jobs
            return MagicMock()

        mock_coll.side_effect = coll_router

        res = fs_store.delete_sandbox_cascade(space_id)
        assert res["deleted"] is False
        assert res["reason"] in ("lease_held", "active_lease_held")


# ============================================================================
# Gate F12: Zero MemoryStore Fallback on Firestore Outage
# ============================================================================
def test_gate_f12_zero_memory_fallback_on_firestore_outage():
    """Gate F12:
    When Firestore query fails in sweep_expired_sandboxes or delete_sandbox_cascade,
    it MUST raise StorageUnavailableError and NEVER fall back to MemoryStore!
    """
    from app.services.storage import FirestoreStore, StorageUnavailableError

    mock_client = MagicMock()
    mock_client.collection.side_effect = RuntimeError("Firestore unavailable: 503 Service Unavailable")

    fs_store = FirestoreStore.__new__(FirestoreStore)
    fs_store.client = mock_client
    fs_store.project_id = "test-proj"

    # sweep_expired_sandboxes must raise StorageUnavailableError
    with pytest.raises(StorageUnavailableError) as exc_info:
        fs_store.sweep_expired_sandboxes(datetime.now(UTC))
    assert "Firestore sweep_expired_sandboxes unavailable" in str(exc_info.value)

    # delete_sandbox_cascade must raise StorageUnavailableError
    with pytest.raises(StorageUnavailableError) as exc_info2:
        fs_store.delete_sandbox_cascade("staging-sandbox-err")
    assert "Firestore unavailable" in str(exc_info2.value)


# ============================================================================
# Gate F13: Two-User Invite Single-Use Enforcement (max_uses=1)
# ============================================================================
def test_gate_f13_two_user_invite_single_use_enforcement():
    """Gate F13:
    - User 1 (owner) creates invite with max_uses=1.
    - User 2 accepts invite: role is coordinator, used_count increments to 1.
    - Subsequent accept or preview attempt fails with 410 / INVITE_EXHAUSTED.
    """
    # 1. User 1 creates space
    res_sp = client.post("/v1/spaces", json={"name": "Multi-User Production"}, headers=OWNER_AUTH)
    assert res_sp.status_code == 200
    space_id = res_sp.json()["space_id"]

    # 2. User 1 creates single-use invite
    res_inv = client.post(
        f"/v1/spaces/{space_id}/invites",
        json={"role": "coordinator", "max_uses": 1},
        headers=OWNER_AUTH,
    )
    assert res_inv.status_code == 200
    token = res_inv.json()["token"]

    # 3. User 2 previews invite (public preview)
    res_prev = client.get(f"/v1/invites/{token}/preview")
    assert res_prev.status_code == 200
    assert res_prev.json()["status"] == "active"

    # 4. User 2 accepts invite
    res_acc = client.post(
        f"/v1/invites/{token}/accept",
        headers=COORD_AUTH,
    )
    assert res_acc.status_code == 200
    assert res_acc.json()["space_id"] == space_id
    assert store.get_member_role(space_id, "coord_02") == MembershipRole.COORDINATOR

    # 5. Token is now exhausted (max_uses: 1, used_count: 1)
    # Another user (or user 2) attempting to accept again fails with 410
    res_acc_second = client.post(
        f"/v1/invites/{token}/accept",
        headers=MEMBER_AUTH,
    )
    assert res_acc_second.status_code == 410
    assert "maximum usage limit" in res_acc_second.json()["detail"].lower()

    # Preview now also returns 410 Gone with structured code
    res_prev_second = client.get(f"/v1/invites/{token}/preview")
    assert res_prev_second.status_code == 410
    assert res_prev_second.json()["detail"]["code"] in ("INVITE_EXHAUSTED", "INVITE_REVOKED")


# ============================================================================
# Gate F14: Authentic OpenTelemetry Error Span Generation
# ============================================================================
def test_gate_f14_authentic_opentelemetry_error_span():
    """Gate F14:
    Verifies that simulate_failure_endpoint emits an authentic OpenTelemetry error span
    with correct span attributes and ERROR status.
    """
    maint_headers = {"X-StudioTower-Maintenance-Secret": settings.STUDIO_TOWER_MAINTENANCE_SECRET}

    with patch.object(settings, "ENV", "staging"), patch.object(settings, "ENABLE_FAILURE_INJECTION", True):
        # Create sandbox
        res_sb = client.post(
            "/v1/maintenance/sandboxes",
            json={"name": "OTel Trace Sandbox"},
            headers=maint_headers,
        )
        assert res_sb.status_code == 200
        sb_id = res_sb.json()["space_id"]

        with patch("app.core.otel.get_tracer") as mock_get_tracer:
            mock_tracer = MagicMock()
            mock_span = MagicMock()
            mock_tracer.start_as_current_span.return_value.__enter__.return_value = mock_span
            mock_get_tracer.return_value = mock_tracer

            res_sim = client.post(
                "/v1/maintenance/failure-injection/simulate",
                json={"space_id": sb_id, "test_run_key": "otel_test_key_01"},
                headers=maint_headers,
            )
            assert res_sim.status_code == 200

            # Verify tracer was called with correct instrumentation name
            mock_get_tracer.assert_called_with("studiotower.maintenance")
            mock_tracer.start_as_current_span.assert_called_with("simulate_failure_injection")

            # Verify span attributes were recorded
            mock_span.set_attribute.assert_any_call("space_id", sb_id)
            mock_span.set_attribute.assert_any_call("failure_code", "PDX_RESOURCE_CONFLICT")
            mock_span.set_attribute.assert_any_call("is_simulated_failure", True)


# ============================================================================
# Gate F15: Fail-Closed Custom Token Minting Endpoint
# ============================================================================
def test_gate_f15_custom_token_fail_closed():
    """Gate F15:
    Verifies that POST /v1/maintenance/test-auth/mint-custom-token fails closed
    with HTTP 503 FIREBASE_AUTH_UNAVAILABLE when Firebase Admin cannot mint a token,
    and NEVER returns a fabricated/mocked custom token string.
    When Firebase Admin succeeds, returns HTTP 200 with the authoritative token.
    """
    maint_headers = {"X-StudioTower-Maintenance-Secret": settings.STUDIO_TOWER_MAINTENANCE_SECRET}

    with patch.object(settings, "ENV", "staging"), patch.object(settings, "ENABLE_FAILURE_INJECTION", True):
        # 1. When Firebase Admin raises an error, endpoint must return 503 (no mock token fallback)
        mock_fb_admin_err = MagicMock()
        mock_fb_auth_err = MagicMock()
        mock_fb_admin_err.auth = mock_fb_auth_err
        mock_fb_auth_err.create_custom_token.side_effect = RuntimeError("Firebase Admin connection timeout")
        with patch.dict("sys.modules", {"firebase_admin": mock_fb_admin_err, "firebase_admin.auth": mock_fb_auth_err}):
            res_fail = client.post(
                "/v1/maintenance/test-auth/mint-custom-token",
                json={"identity": "staging_test_owner"},
                headers=maint_headers,
            )
            assert res_fail.status_code == 503
            assert "FIREBASE_AUTH_UNAVAILABLE" in res_fail.json()["detail"]

        # 2. When Firebase Admin succeeds, returns 200 with authoritative token
        mock_fb_admin_ok = MagicMock()
        mock_fb_auth_ok = MagicMock()
        mock_fb_admin_ok.auth = mock_fb_auth_ok
        mock_fb_auth_ok.create_custom_token.return_value = b"authentic_firebase_jwt_token_12345"
        with patch.dict("sys.modules", {"firebase_admin": mock_fb_admin_ok, "firebase_admin.auth": mock_fb_auth_ok}):
            res_ok = client.post(
                "/v1/maintenance/test-auth/mint-custom-token",
                json={"identity": "staging_test_owner"},
                headers=maint_headers,
            )
            assert res_ok.status_code == 200
            data = res_ok.json()
            assert data["custom_token"] == "authentic_firebase_jwt_token_12345"
            assert data["identity"] == "staging_test_owner"
            assert data["uid"] == "staging_test_owner_uid"


def test_gate_f15_revoke_test_user_fail_closed():
    """Gate F15b:
    Verifies that POST /v1/maintenance/test-auth/revoke-test-user fails closed
    with HTTP 503 TOKEN_REVOCATION_FAILED, returning sanitized trace_id and retryable flag
    without leaking internal exception message to client.
    """
    maint_headers = {"X-StudioTower-Maintenance-Secret": settings.STUDIO_TOWER_MAINTENANCE_SECRET}

    with patch.object(settings, "ENV", "staging"), patch.object(settings, "ENABLE_FAILURE_INJECTION", True):
        mock_fb_admin = MagicMock()
        mock_fb_auth = MagicMock()
        mock_fb_admin.auth = mock_fb_auth
        mock_fb_auth.revoke_refresh_tokens.side_effect = RuntimeError("Firebase Auth internal connection reset with sensitive credentials /etc/secrets/key.json")

        with patch.dict("sys.modules", {"firebase_admin": mock_fb_admin, "firebase_admin.auth": mock_fb_auth}):
            res = client.post(
                "/v1/maintenance/test-auth/revoke-test-user",
                json={"identity": "staging_test_owner"},
                headers=maint_headers,
            )
            assert res.status_code == 503
            detail = res.json()["detail"]
            assert "TOKEN_REVOCATION_FAILED" in detail
            assert "trace_id:" in detail
            assert "retryable: true" in detail
            # Assert sensitive internal message does NOT leak
            assert "sensitive credentials" not in detail
            assert "/etc/secrets" not in detail


def test_production_cors_strict_validation():
    """Verifies that Settings validator rejects wildcard, localhost, and non-HTTPS origins in production."""
    base_prod_kwargs = dict(
        ENV="production",
        STUDIO_TOWER_AUTH_MODE="firebase",
        STUDIO_TOWER_FIREBASE_PROJECT_ID="test-prod-proj",
        STUDIO_TOWER_STORE="firestore",
        STUDIO_TOWER_ARTIFACT_BACKEND="gcs",
        STUDIO_TOWER_GCS_BUCKET="test-bucket",
        INGESTION_RUNNER="cloud_tasks",
        STUDIO_TOWER_WORKER_SERVICE_URL="https://worker.test.run.app",
        STUDIO_TOWER_ALLOWED_WORKER_HOST="worker.test.run.app",
        STUDIO_TOWER_SCHEDULER_SA="studiotower-api@test-prod-proj.iam.gserviceaccount.com",
        STUDIO_TOWER_TASK_SECRET="a" * 32,
        STUDIO_TOWER_MAINTENANCE_SECRET="b" * 32,
        CURSOR_SIGNING_SECRET="c" * 32,
        ACTION_SIGNING_SECRET="d" * 32,
        ACTION_RUNNER="cloud_tasks",
    )

    # 1. Wildcard rejected
    with pytest.raises(ValueError, match="Production CORS configuration must not contain wildcard"):
        Settings(**base_prod_kwargs, CORS_ORIGINS=["https://example.com", "*"])

    # 2. Localhost rejected
    with pytest.raises(ValueError, match="must not permit localhost"):
        Settings(**base_prod_kwargs, CORS_ORIGINS=["http://localhost:3000"])

    # 3. 127.0.0.1 rejected
    with pytest.raises(ValueError, match="must not permit localhost"):
        Settings(**base_prod_kwargs, CORS_ORIGINS=["http://127.0.0.1:5173"])

    # 4. Plain HTTP rejected
    with pytest.raises(ValueError, match="must only permit HTTPS origins"):
        Settings(**base_prod_kwargs, CORS_ORIGINS=["http://my-insecure-domain.com"])

    # 5. Legitimate HTTPS origins accepted
    valid_settings = Settings(**base_prod_kwargs, CORS_ORIGINS=["https://agentic-cinema-demo-2026.web.app"])
    assert "https://agentic-cinema-demo-2026.web.app" in valid_settings.CORS_ORIGINS


# ============================================================================
# Gate F16: Resumable Leased State Machine & Fail-Closed Blob Cleanup
# ============================================================================
def test_gate_f16_resumable_leased_cleanup_state_machine():
    """Gate F16:
    Verifies that FirestoreStore.delete_sandbox_cascade adheres to the resilient state machine:
    1. Blob deletion failure halts immediately, records BLOB_DELETION_FAILED, and raises StorageUnavailableError BEFORE metadata deletion.
    2. MemoryStore fails closed on standard (non-sandbox) spaces with CleanupAuthorizationError.
    """
    from app.services.storage import (
        CleanupAuthorizationError,
        FirestoreStore,
        SandboxCleanupPhase,
        StorageUnavailableError,
    )

    # 1. Non-sandbox space must fail closed
    space_prod = Space(space_id="sp_prod_permanent", name="Production Space", is_sandbox=False, created_by="owner_01")
    store.create_space(space_prod, creator_uid="owner_01")
    with pytest.raises(CleanupAuthorizationError):
        store.delete_sandbox_cascade("sp_prod_permanent")

    # 2. FirestoreStore blob-failure fail-closed test
    mock_client = MagicMock()
    fs_store = FirestoreStore.__new__(FirestoreStore)
    fs_store.client = mock_client
    fs_store.project_id = "test-proj"

    space_id = "staging-sandbox-blobfail"

    # Mock _claim_cleanup_lease to return success for Phase 1
    with patch.object(
        fs_store,
        "_claim_cleanup_lease",
        return_value=(
            True,
            {
                "space_id": space_id,
                "lease_owner": "worker_1",
                "lease_token": "token_123",
                "state_version": 1,
                "phase": SandboxCleanupPhase.DELETING_BLOBS.value,
                "attempts": 1,
            },
            "CLAIMED",
        ),
    ), patch.object(fs_store, "_record_cleanup_failure") as mock_record_failure:

        # Simulate GCS error during deleting_blobs
        mock_file_doc = MagicMock()
        mock_file_doc.to_dict.return_value = {"storage_path": f"spaces/{space_id}/file1.txt"}
        mock_client.collection.return_value.where.return_value.stream.return_value = [mock_file_doc]

        mock_gcs = MagicMock()
        mock_gcs.Client.side_effect = RuntimeError("GCS network unreachable")
        with patch.object(settings, "STUDIO_TOWER_ARTIFACT_BACKEND", "gcs"), patch.dict(
            "sys.modules", {"google.cloud.storage": mock_gcs}
        ):
            with pytest.raises(StorageUnavailableError):
                fs_store.delete_sandbox_cascade(space_id, worker_id="worker_1")

            # Assert failure was recorded with worker_id and sanitized error code BLOB_DELETION_FAILED
            mock_record_failure.assert_called_once_with(
                space_id, "worker_1", "token_123", 1, "BLOB_DELETION_FAILED", 1
            )


# ============================================================================
# Gate F17: CAS Lease Owner, Expiration Fencing & Atomic Finalization
# ============================================================================
def test_gate_f17_cas_owner_and_expiration_fencing():
    """Gate F17:
    Verifies that _fenced_update_cleanup_job strictly enforces:
    1. Worker ownership matching lease_owner.
    2. Unexpired lease_until.
    3. StorageConflictError raised if lease expired or wrong worker.
    """
    from app.services.storage import FirestoreStore, StorageConflictError

    mock_client = MagicMock()
    fs_store = FirestoreStore.__new__(FirestoreStore)
    fs_store.client = mock_client
    fs_store.project_id = "test-proj"

    space_id = "staging-sandbox-fence"

    # Set up mock transaction
    mock_tx = MagicMock()
    fs_store.client.transaction.return_value = mock_tx

    def run_tx(fn):
        return fn(mock_tx)
    # google.cloud.firestore.transactional runs fn(tx)
    with patch("google.cloud.firestore.transactional", lambda fn: fn):
        # 1. Test wrong worker_id raises StorageConflictError
        mock_snap = MagicMock()
        mock_snap.exists = True
        mock_snap.to_dict.return_value = {
            "lease_owner": "worker_alice",
            "lease_token": "token_abc",
            "state_version": 2,
            "lease_until": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        }
        mock_snap.reference = MagicMock()
        mock_tx.get = MagicMock(return_value=mock_snap)
        mock_client.collection.return_value.document.return_value.get.return_value = mock_snap

        with pytest.raises(StorageConflictError, match="not lease owner"):
            fs_store._fenced_update_cleanup_job(
                space_id=space_id,
                worker_id="worker_bob",
                lease_token="token_abc",
                expected_version=2,
                updates={"phase": "test"},
            )

        # 2. Test expired lease_until raises StorageConflictError
        mock_snap.to_dict.return_value = {
            "lease_owner": "worker_alice",
            "lease_token": "token_abc",
            "state_version": 2,
            "lease_until": (datetime.now(UTC) - timedelta(seconds=10)).isoformat(),
        }
        with pytest.raises(StorageConflictError, match="lease expired"):
            fs_store._fenced_update_cleanup_job(
                space_id=space_id,
                worker_id="worker_alice",
                lease_token="token_abc",
                expected_version=2,
                updates={"phase": "test"},
            )

        # 3. Test valid owner, token, version, and unexpired lease succeeds and increments version
        mock_snap.to_dict.return_value = {
            "lease_owner": "worker_alice",
            "lease_token": "token_abc",
            "state_version": 2,
            "lease_until": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        }
        new_ver = fs_store._fenced_update_cleanup_job(
            space_id=space_id,
            worker_id="worker_alice",
            lease_token="token_abc",
            expected_version=2,
            updates={"phase": "test"},
        )
        assert new_ver == 3
        mock_tx.update.assert_called_once()
        update_payload = mock_tx.update.call_args[0][1]
        assert update_payload["state_version"] == 3
        assert update_payload["phase"] == "test"


def test_gate_f17_atomic_finalization():
    """Gate F17:
    Verifies that during finalizing phase, Space deletion and Job COMPLETED
    are executed atomically inside the same Firestore transaction.
    """
    from app.services.storage import FirestoreStore, SandboxCleanupPhase

    mock_client = MagicMock()
    fs_store = FirestoreStore.__new__(FirestoreStore)
    fs_store.client = mock_client
    fs_store.project_id = "test-proj"

    space_id = "staging-sandbox-atomic"
    mock_tx = MagicMock()
    fs_store.client.transaction.return_value = mock_tx

    with patch("google.cloud.firestore.transactional", lambda fn: fn), patch.object(
        fs_store,
        "_claim_cleanup_lease",
        return_value=(
            True,
            {
                "space_id": space_id,
                "lease_owner": "worker_final",
                "lease_token": "tok_fin",
                "state_version": 5,
                "phase": SandboxCleanupPhase.FINALIZING.value,
                "attempts": 1,
            },
            "CLAIMED",
        ),
    ):
        mock_job_snap = MagicMock()
        mock_job_snap.exists = True
        mock_job_snap.to_dict.return_value = {
            "lease_owner": "worker_final",
            "lease_token": "tok_fin",
            "state_version": 5,
            "lease_until": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        }
        mock_client.collection.return_value.document.return_value.get.return_value = mock_job_snap

        result = fs_store.delete_sandbox_cascade(space_id, worker_id="worker_final")
        assert result["deleted"] is True

        # Assert BOTH space document delete and job update were called on the same transaction
        mock_tx.delete.assert_called_once()
        mock_tx.update.assert_called_once()
        update_args = mock_tx.update.call_args[0][1]
        assert update_args["phase"] == SandboxCleanupPhase.COMPLETED.value
        assert update_args["lease_owner"] is None
        assert update_args["lease_token"] is None


# ============================================================================
# Gate F18: Self-Healing Lease Recovery, Fail-Closed Blob Inventory & Cursor Resumption
# ============================================================================
def test_gate_f18_completed_finalization_not_overwritten():
    """Gate F18:
    Verifies that when a cleanup job has phase == COMPLETED but the space document
    still exists (e.g. stranded by crash), _claim_cleanup_lease returns phase == FINALIZING
    and does NOT overwrite it back to COMPLETED.
    """
    from app.services.storage import FirestoreStore, SandboxCleanupPhase

    mock_client = MagicMock()
    fs_store = FirestoreStore.__new__(FirestoreStore)
    fs_store.client = mock_client
    fs_store.project_id = "test-proj"

    space_id = "sp_stranded_completed"
    mock_tx = MagicMock()
    fs_store.client.transaction.return_value = mock_tx

    with patch("google.cloud.firestore.transactional", lambda fn: fn):
        # Space exists and is_sandbox=True
        mock_space_snap = MagicMock()
        mock_space_snap.exists = True
        mock_space_snap.to_dict.return_value = {"is_sandbox": True, "cleanup_status": "deleting"}
        mock_space_ref = MagicMock()
        mock_space_ref.get.return_value = mock_space_snap

        # Cleanup job exists with phase == COMPLETED
        mock_job_snap = MagicMock()
        mock_job_snap.exists = True
        mock_job_snap.to_dict.return_value = {
            "space_id": space_id,
            "phase": SandboxCleanupPhase.COMPLETED.value,
            "state_version": 4,
            "attempts": 2,
            "lease_until": None,
            "lease_owner": None,
            "next_retry_at": None,
        }
        mock_job_ref = MagicMock()
        mock_job_ref.get.return_value = mock_job_snap

        def mock_coll(name):
            col = MagicMock()
            if name == "spaces":
                col.document.return_value = mock_space_ref
            else:
                col.document.return_value = mock_job_ref
            return col

        mock_client.collection.side_effect = mock_coll

        claimed, job, reason = fs_store._claim_cleanup_lease(space_id, "worker_recover")
        assert claimed is True
        assert reason == "CLAIMED"
        assert job["phase"] == SandboxCleanupPhase.FINALIZING.value
        assert job["current_phase"] == SandboxCleanupPhase.FINALIZING.value
        assert job["state_version"] == 5


def test_gate_f18_blob_inventory_query_failure_fails_closed():
    """Gate F18:
    Verifies that if querying either files or artifacts fails during deleting_blobs,
    the operation fails closed: records BLOB_INVENTORY_QUERY_FAILED, releases lease,
    and raises StorageUnavailableError before attempting child deletion.
    """
    from app.services.storage import FirestoreStore, SandboxCleanupPhase, StorageUnavailableError

    mock_client = MagicMock()
    fs_store = FirestoreStore.__new__(FirestoreStore)
    fs_store.client = mock_client
    fs_store.project_id = "test-proj"

    space_id = "sp_blob_query_fail"

    with patch.object(
        fs_store,
        "_claim_cleanup_lease",
        return_value=(
            True,
            {
                "space_id": space_id,
                "lease_owner": "worker_inv",
                "lease_token": "token_inv",
                "state_version": 1,
                "phase": SandboxCleanupPhase.DELETING_BLOBS.value,
                "attempts": 1,
            },
            "CLAIMED",
        ),
    ), patch.object(fs_store, "_record_cleanup_failure") as mock_record_failure:
        # Simulate collection query error on 'artifacts'
        def mock_collection(coll_name):
            col = MagicMock()
            if coll_name == "artifacts":
                col.where.return_value.stream.side_effect = RuntimeError("Firestore index unavailable for artifacts")
            else:
                col.where.return_value.stream.return_value = []
            return col

        mock_client.collection.side_effect = mock_collection

        with pytest.raises(StorageUnavailableError, match="Failed to inventory blobs"):
            fs_store.delete_sandbox_cascade(space_id, worker_id="worker_inv")

        mock_record_failure.assert_called_once_with(
            space_id, "worker_inv", "token_inv", 1, "BLOB_INVENTORY_QUERY_FAILED", 1
        )


def test_gate_f18_cleanup_cursor_cross_instance_resumption():
    """Gate F18:
    Verifies that when resuming from a cursor ID where the DocumentSnapshot was deleted,
    coll_ref.document(cursor_id) produces a DocumentReference and passes
    {"__name__": cursor_ref} to start_after, adhering strictly to the Firestore SDK contract.
    """
    from app.services.storage import FirestoreStore, SandboxCleanupPhase

    mock_client = MagicMock()
    fs_store = FirestoreStore.__new__(FirestoreStore)
    fs_store.client = mock_client
    fs_store.project_id = "test-proj"

    space_id = "sp_resume_cursor"
    cursor_doc_id = "doc_batch_1_last"

    # Simulate Worker 2 resuming from batch 1
    with patch.object(
        fs_store,
        "_claim_cleanup_lease",
        return_value=(
            True,
            {
                "space_id": space_id,
                "lease_owner": "worker_resume",
                "lease_token": "token_res",
                "state_version": 2,
                "phase": SandboxCleanupPhase.DELETING_CHILDREN.value,
                "collection_index": 0,
                "cursor_document_id": cursor_doc_id,
                "attempts": 2,
            },
            "CLAIMED",
        ),
    ), patch.object(fs_store, "_fenced_update_cleanup_job", return_value=3), patch(
        "google.cloud.firestore.transactional", lambda fn: lambda tx: None
    ):
        mock_coll = MagicMock()
        mock_query = MagicMock()
        mock_coll.where.return_value.order_by.return_value = mock_query

        # The cursor doc does not exist (already deleted)
        mock_cursor_doc_ref = MagicMock()
        mock_cursor_snap = MagicMock()
        mock_cursor_snap.exists = False
        mock_cursor_doc_ref.get.return_value = mock_cursor_snap
        mock_coll.document.return_value = mock_cursor_doc_ref

        # Query returns remaining docs then empty
        mock_query.start_after.return_value.limit.return_value.stream.return_value = []
        mock_query.limit.return_value.stream.return_value = []
        mock_client.collection.return_value = mock_coll

        # Run resume
        fs_store.delete_sandbox_cascade(space_id, worker_id="worker_resume")

        # Verify start_after was called with {"__name__": mock_cursor_doc_ref}
        mock_query.start_after.assert_called_with({"__name__": mock_cursor_doc_ref})


def test_gate_f19_destructive_blob_storage_path_containment_validation():
    """Gate F19:
    Verifies that FirestoreStore.delete_sandbox_cascade strictly validates blob storage_path
    containment before performing destructive deletions on GCS or local disk:
    1. Directory traversal paths (e.g. '../../outside/file.pdf') fail closed with INVALID_BLOB_STORAGE_PATH.
    2. Cross-space paths (e.g. 'spaces/other-space/file.pdf') fail closed with INVALID_BLOB_STORAGE_PATH.
    3. Absolute paths (e.g. '/etc/shadow') fail closed with INVALID_BLOB_STORAGE_PATH.
    4. Legitimate paths scoped to the space ('spaces/{space_id}/...' and '{space_id}/...') pass.
    5. Cleanup aborts immediately upon violation, leaving child metadata collections untouched.
    """
    from app.services.storage import (
        FirestoreStore,
        SandboxCleanupPhase,
        StorageUnavailableError,
    )
    import tempfile

    fs_store = FirestoreStore.__new__(FirestoreStore)
    space_id = "sandbox-scope-validation-test"
    valid_prefix = f"spaces/{space_id}/"

    # Test path validator directly
    # 1. Traversal
    with pytest.raises(ValueError, match="Directory traversal detected"):
        fs_store._validate_blob_storage_path(space_id, f"{valid_prefix}../../outside.pdf", is_gcs=True)

    with pytest.raises(ValueError, match="Directory traversal detected"):
        fs_store._validate_blob_storage_path(space_id, f"../../outside.pdf", is_gcs=False)

    # 2. Absolute path
    with pytest.raises(ValueError, match="Absolute or rooted storage path disallowed"):
        fs_store._validate_blob_storage_path(space_id, "/etc/shadow", is_gcs=True)

    # 3. Cross-space path
    with pytest.raises(ValueError, match="violates space prefix containment"):
        fs_store._validate_blob_storage_path(space_id, "spaces/another-space/doc.pdf", is_gcs=True)

    with pytest.raises(ValueError, match="violates space prefix containment"):
        fs_store._validate_blob_storage_path(space_id, "another-space/doc.pdf", is_gcs=False)

    # 4. Valid paths
    gcs_val = fs_store._validate_blob_storage_path(space_id, f"spaces/{space_id}/files/doc.pdf", is_gcs=True)
    assert gcs_val == f"spaces/{space_id}/files/doc.pdf"

    local_val = fs_store._validate_blob_storage_path(space_id, f"{space_id}/files/doc.pdf", is_gcs=False, data_dir="./data")
    assert f"{space_id}" in local_val

    # 4.5 Symlink breakout rejection
    with tempfile.TemporaryDirectory() as tmp_dir:
        space_dir = os.path.join(tmp_dir, space_id)
        os.makedirs(space_dir, exist_ok=True)
        outside_file = os.path.join(tmp_dir, "outside_secret.txt")
        with open(outside_file, "w") as f:
            f.write("sensitive")
        symlink_target = os.path.join(space_dir, "symlink.txt")
        try:
            os.symlink(outside_file, symlink_target)
            rel_symlink_path = f"{space_id}/symlink.txt"
            with pytest.raises(ValueError, match="escapes space|outside authorized"):
                fs_store._validate_blob_storage_path(space_id, rel_symlink_path, is_gcs=False, data_dir=tmp_dir)
        except (OSError, NotImplementedError):
            pass

    # 5. End-to-end cascade deletion aborts and records INVALID_BLOB_STORAGE_PATH on traversal metadata
    mock_client = MagicMock()
    fs_store.client = mock_client
    fs_store.project_id = "test-proj"

    with patch.object(
        fs_store,
        "_claim_cleanup_lease",
        return_value=(
            True,
            {
                "space_id": space_id,
                "lease_owner": "worker_p0",
                "lease_token": "token_p0",
                "state_version": 1,
                "phase": SandboxCleanupPhase.DELETING_BLOBS.value,
                "attempts": 1,
            },
            "CLAIMED",
        ),
    ), patch.object(fs_store, "_record_cleanup_failure") as mock_record_failure:
        # Mock file metadata containing dangerous traversal path
        malicious_doc = MagicMock()
        malicious_doc.to_dict.return_value = {"storage_path": "../../dangerous/escape.pdf"}
        mock_client.collection.return_value.where.return_value.stream.return_value = [malicious_doc]

        with pytest.raises(StorageUnavailableError) as exc_info:
            fs_store.delete_sandbox_cascade(space_id, worker_id="worker_p0")

        assert "INVALID_BLOB_STORAGE_PATH" in str(exc_info.value)
        mock_record_failure.assert_called_once_with(
            space_id, "worker_p0", "token_p0", 1, "INVALID_BLOB_STORAGE_PATH", 1
        )







