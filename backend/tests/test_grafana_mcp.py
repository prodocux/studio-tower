import pytest
from app.agent.brain import AgentBrain
from app.integrations.grafana_mcp import grafana_mcp
from app.main import app
from app.models.run import Run, RunStatus
from app.services.storage import store
from fastapi.testclient import TestClient

client = TestClient(app)

ALICE_AUTH = {"Authorization": "Bearer dev:alice_01:alice@example.com:Alice"}
BOB_AUTH = {"Authorization": "Bearer dev:bob_02:bob@example.com:Bob"}


@pytest.fixture(autouse=True)
def clean_store():
    store.clear()
    yield
    store.clear()


def test_grafana_mcp_trace_recording_and_query():
    trace_id = "trc_test_grafana_001"
    run_id = "run_test_001"
    space_id = "space_test_001"

    grafana_mcp.record_span(
        trace_id=trace_id,
        span_id="spn_test_1",
        name="test_operation",
        duration_ms=45,
        status="ok",
        attributes={"test.key": "test_val"},
    )

    bundle = grafana_mcp.query_trace(trace_id, run_id=run_id, space_id=space_id)
    assert bundle.trace_id == trace_id
    assert bundle.total_spans == 1
    assert bundle.spans[0].name == "test_operation"
    # Unverified local diagnostic span without backend TelemetryService verification remains has_real_telemetry=False
    assert bundle.has_real_telemetry is False
    assert bundle.is_local_diagnostic is True
    assert bundle.grafana_dashboard_url is None


def test_agent_diagnose_failure_with_grafana():
    space_id = "space_test_002"
    run = Run(
        space_id=space_id,
        project_tag="stunts",
        status=RunStatus.FAILED,
        prompt="Execute aircraft test",
        created_by="alice_01",
    )
    store.save_run(run)

    # Record simulated failure in Grafana MCP
    grafana_mcp.record_simulated_failure_trace(run.trace_id, run.run_id, space_id)
    sim_bundle = grafana_mcp.query_trace(run.trace_id, run.run_id, space_id)
    assert any(s.name == "gemini_genai_reasoning" for s in sim_bundle.spans)
    assert any(s.attributes.get("ai.model") == "gemini-3.6-flash" for s in sim_bundle.spans)
    assert any(s.attributes.get("ai.sdk") == "google-genai" for s in sim_bundle.spans)

    # Run diagnosis
    diagnosis = AgentBrain.diagnose_run_with_grafana(run, space_id)

    assert "🚨 **Grafana MCP Telemetry Diagnosis" in diagnosis
    assert "pdx_resource_constraint_evaluator" in diagnosis
    assert "Sacrificial Test Aircraft" in diagnosis
    assert "Recommended AI Remediation" in diagnosis


def test_chat_simulate_failure_and_diagnose_endpoint():
    maint_secret = "test-maint-secret-32b-length-ok"
    maint_headers = {"X-StudioTower-Maintenance-Secret": maint_secret}

    from unittest.mock import patch
    from app.core.config import settings
    from app.models.space import MembershipRole
    from app.models.user import User

    with patch.object(settings, "ENV", "staging"), \
         patch.object(settings, "ENABLE_FAILURE_INJECTION", True), \
         patch.object(settings, "STUDIO_TOWER_MAINTENANCE_SECRET", maint_secret):
        # Create authoritative sandbox space
        sb_res = client.post("/v1/maintenance/sandboxes", headers=maint_headers)
        assert sb_res.status_code == 200
        space_id = sb_res.json()["space_id"]

        # Add Alice as member so she can access space
        alice = User(uid="alice_01", email="alice@example.com", display_name="Alice")
        store.save_user(alice)
        store.add_member(space_id, "alice_01", MembershipRole.MEMBER)

        # Trigger simulated failure via dedicated maintenance endpoint
        sim_res = client.post(
            "/v1/maintenance/failure-injection/simulate",
            json={
                "space_id": space_id,
                "test_run_key": "stunt_overlap_test",
                "failure_code": "PDX_RESOURCE_CONFLICT",
            },
            headers=maint_headers,
        )
        assert sim_res.status_code == 200
        data = sim_res.json()
        assert data["status"] == "failed"
        run_id = data["run_id"]
        trace_id = data["trace_id"]

        # Production trace endpoint strictly FAILS CLOSED (404) for simulated runs (zero leakage into prod)
        trace_res = client.get(f"/v1/spaces/{space_id}/runs/{run_id}/trace", headers=ALICE_AUTH)
        assert trace_res.status_code == 404

        # Simulated trace remains strictly accessible via Grafana MCP for diagnostics
        mcp_bundle = grafana_mcp.query_trace(trace_id, run_id=run_id, space_id=space_id)
        assert mcp_bundle.total_spans > 0
        assert mcp_bundle.has_real_telemetry is False
        assert any(s.status == "error" for s in mcp_bundle.spans)
        assert any(s.name == "pdx_resource_constraint_evaluator" for s in mcp_bundle.spans)

        # Bob (non-member) cannot access trace -> 403 Forbidden
        bob_trace = client.get(f"/v1/spaces/{space_id}/runs/{run_id}/trace", headers=BOB_AUTH)
        assert bob_trace.status_code == 403
