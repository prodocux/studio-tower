import io
import os
from unittest.mock import MagicMock, patch

import pytest
from app.agent.brain import AgentBrain
from app.core.config import settings
from app.integrations.grafana_mcp import grafana_mcp
from app.main import app
from app.models.idempotency import ChatIdempotencyRecord, ChatIdempotencyStatus
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


def test_gate1_agent_dm_plain_text_never_creates_run_or_gate():
    """
    Gate 1: Asking general questions like "What can this tool do?" in Agent DM
    must invoke conversational AI without creating a Run or Approval Gate.
    """
    dm_res = client.get("/v1/spaces", headers=ALICE_AUTH)
    assert dm_res.status_code == 200
    spaces = dm_res.json()
    agent_dm = next((s for s in spaces if s.get("kind") == "agent_dm"), None)
    assert agent_dm is not None
    dm_space_id = agent_dm["space_id"]

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = MagicMock(
        text="StudioTower is an observable film production preparation platform."
    )

    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        res = client.post(
            "/v1/chat",
            json={
                "space_id": dm_space_id,
                "content": "What can this tool do?",
            },
            headers=ALICE_AUTH,
        )
        assert res.status_code == 200
        data = res.json()

        assert data["user_message"]["role"] == "user"
        assert data["agent_message"] is not None
        assert data["agent_message"]["role"] == "agent"
        assert "StudioTower" in data["agent_message"]["content"]

        assert data.get("run") is None
        runs_in_space = store.list_runs_in_space(dm_space_id)
        assert len(runs_in_space) == 0


def test_gate2_agent_dm_attachment_defaults_to_document_qa():
    """
    Gate 2: Attaching a document without explicit intent defaults to document_qa
    and does NOT automatically trigger a scene breakdown run.
    """
    dm_res = client.get("/v1/spaces", headers=ALICE_AUTH)
    agent_dm = next(s for s in dm_res.json() if s.get("kind") == "agent_dm")
    dm_space_id = agent_dm["space_id"]

    doc_bytes = b"Scene 1: Exterior Lake. Elena meets Kael."
    upload_res = client.post(
        f"/v1/spaces/{dm_space_id}/files",
        files={"file": ("treatment.txt", io.BytesIO(doc_bytes), "text/plain")},
        headers=ALICE_AUTH,
    )
    assert upload_res.status_code == 200
    file_id = upload_res.json()["file_id"]

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = MagicMock(
        text="Based on the attached treatment, the scene takes place at an Exterior Lake."
    )

    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        res = client.post(
            "/v1/chat",
            json={
                "space_id": dm_space_id,
                "content": "Where does Scene 1 take place?",
                "attachment_file_ids": [file_id],
            },
            headers=ALICE_AUTH,
        )
        assert res.status_code == 200
        data = res.json()
        assert data["agent_message"] is not None
        assert data.get("run") is None
        assert len(store.list_runs_in_space(dm_space_id)) == 0


def test_gate3_create_breakdown_intent_explicitly_creates_run():
    """
    Gate 3: Only explicit create_breakdown intent generates a Run and Approval Gate.
    """
    space = client.post("/v1/spaces", json={"name": "Production Unit A"}, headers=ALICE_AUTH).json()
    space_id = space["space_id"]

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = MagicMock(
        text='{"project_title": "Unit A Plan", "summary": "Breakdown summary", "scenes": [{"scene_number": 1, "slugline": "EXT. LAKE - DAY", "description": "Elena meets Kael by the lake"}]}'
    )

    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        res = client.post(
            "/v1/chat",
            json={
                "space_id": space_id,
                "content": "Generate scene breakdown for Unit A",
                "intent": "create_breakdown",
            },
            headers=ALICE_AUTH,
        )
        assert res.status_code == 200
        data = res.json()
        assert data["run"] is not None
        assert data["run"]["space_id"] == space_id
        assert data["run"]["status"] in [RunStatus.COMPLETED.value, RunStatus.AWAITING_APPROVAL.value]


def test_gate4_shared_space_plain_message_does_not_invoke_ai():
    """
    Gate 4: Plain text messages in Shared Spaces without @agent or explicit intent
    must NOT invoke AI and should return user_message only.
    """
    space = client.post("/v1/spaces", json={"name": "Crew General"}, headers=ALICE_AUTH).json()
    space_id = space["space_id"]

    res = client.post(
        "/v1/chat",
        json={
            "space_id": space_id,
            "content": "Hey crew, lunch is arriving at 1:00 PM today.",
        },
        headers=ALICE_AUTH,
    )
    assert res.status_code == 200
    data = res.json()
    assert data["user_message"]["role"] == "user"
    assert data.get("agent_message") is None
    assert data.get("run") is None


def test_gate5_unknown_intent_returns_422_unprocessable_entity():
    """
    Unknown intent values must return 422 Unprocessable Entity and never fall back.
    """
    space = client.post("/v1/spaces", json={"name": "Crew Test"}, headers=ALICE_AUTH).json()
    space_id = space["space_id"]

    res = client.post(
        "/v1/chat",
        json={
            "space_id": space_id,
            "content": "Test unknown intent",
            "intent": "invalid_intent_xyz",
        },
        headers=ALICE_AUTH,
    )
    assert res.status_code == 422


def test_gate6_gemini_unavailable_fails_with_503_and_failed_idempotency():
    """
    Gate 6: When Gemini is unavailable, endpoint returns HTTP 503 (with Retry-After header),
    transitions idempotency status to FAILED, and creates zero agent messages in storage.
    """
    dm_res = client.get("/v1/spaces", headers=ALICE_AUTH)
    agent_dm = next(s for s in dm_res.json() if s.get("kind") == "agent_dm")
    dm_space_id = agent_dm["space_id"]
    client_msg_id = "cmsg_ai_fail_test_001"

    with patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
        res = client.post(
            "/v1/chat",
            json={
                "space_id": dm_space_id,
                "content": "Who is the lead pilot in this script?",
                "client_message_id": client_msg_id,
            },
            headers=ALICE_AUTH,
        )
        # Must fail with 503 Service Unavailable (NOT 200 OK!)
        assert res.status_code == 503
        assert res.headers.get("Retry-After") == "5"

        # Verify idempotency transitioned to FAILED (permitting retry)
        idemp_key = ChatIdempotencyRecord.compute_key(dm_space_id, "alice_01", client_msg_id)
        idemp_rec = store.get_chat_idempotency(idemp_key)
        assert idemp_rec is not None
        assert idemp_rec.status == ChatIdempotencyStatus.FAILED

        # Verify NO new agent message was persisted in the space for this failed query
        messages = store.list_messages(dm_space_id)
        failed_replies = [m for m in messages if m.role.value == "agent" and "pilot" in m.content.lower()]
        assert len(failed_replies) == 0


def test_gate7_query_trace_empty_returns_no_telemetry_flag():
    """
    Gate 7: Querying unrecorded traces returns has_real_telemetry=False, spans=[],
    and grafana_dashboard_url=None (no fake spans).
    """
    bundle = grafana_mcp.query_trace("trc_unrecorded_nonexistent_999", run_id="run_999", space_id="spc_999")
    assert bundle.total_spans == 0
    assert bundle.spans == []
    assert bundle.has_real_telemetry is False
    assert bundle.grafana_dashboard_url is None


def test_gate8_idempotency_payload_hash_includes_intent():
    """
    Gate 8: Reusing the same client_message_id with a different intent
    must raise HTTP 409 Conflict.
    """
    dm_res = client.get("/v1/spaces", headers=ALICE_AUTH)
    agent_dm = next(s for s in dm_res.json() if s.get("kind") == "agent_dm")
    dm_space_id = agent_dm["space_id"]
    client_msg_id = "cmsg_intent_conflict_test_001"

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = MagicMock(text="Conversation reply")

    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        # 1. Send as conversation
        res1 = client.post(
            "/v1/chat",
            json={
                "space_id": dm_space_id,
                "content": "Breakdown the project",
                "client_message_id": client_msg_id,
                "intent": "conversation",
            },
            headers=ALICE_AUTH,
        )
        assert res1.status_code == 200

        # 2. Retry same client_message_id with conflicting intent -> 409 Conflict
        res2 = client.post(
            "/v1/chat",
            json={
                "space_id": dm_space_id,
                "content": "Breakdown the project",
                "client_message_id": client_msg_id,
                "intent": "create_breakdown",
            },
            headers=ALICE_AUTH,
        )
        assert res2.status_code == 409
        assert "conflicting request parameters" in res2.json()["detail"].lower()


def test_gate9_diagnose_empty_trace_returns_no_telemetry_without_claiming_healthy():
    """
    Gate 9: Diagnosing a run with no telemetry data returns an explainable "No Telemetry Data Available"
    message, and NEVER falsely reports that all pipeline stages are healthy.
    """
    space_id = "space_telemetry_gate9"
    run = Run(
        space_id=space_id,
        project_tag="general",
        status=RunStatus.COMPLETED,
        prompt="Initial prompt",
        created_by="alice_01",
        trace_id="trc_empty_telemetry_test",
    )
    store.save_run(run)

    diagnosis = AgentBrain.diagnose_run_with_grafana(run, space_id)
    assert "No Telemetry Data Available" in diagnosis
    assert "All pipeline stages" not in diagnosis
    assert "healthy" not in diagnosis.lower()


def test_gate10_context_run_id_binds_to_discussion():
    """
    Gate 10: Passing context_run_id correctly incorporates that run into the AI discussion.
    """
    dm_res = client.get("/v1/spaces", headers=ALICE_AUTH)
    agent_dm = next(s for s in dm_res.json() if s.get("kind") == "agent_dm")
    dm_space_id = agent_dm["space_id"]

    run = Run(
        space_id=dm_space_id,
        project_tag="stunt",
        status=RunStatus.AWAITING_APPROVAL,
        prompt="Execute high fall stunt",
        created_by="alice_01",
        scene_breakdown={"project_title": "Stunt Block Alpha", "scenes": [{"scene_number": 4, "slugline": "EXT. ROOFTOP - NIGHT", "description": "High fall jump"}], "detected_conflicts": []},
    )
    store.save_run(run)

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = MagicMock(
        text="Reviewing Run for Stunt Block Alpha: High fall jump requires safety nets."
    )

    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        res = client.post(
            "/v1/chat",
            json={
                "space_id": dm_space_id,
                "content": f"Regarding Run #{run.run_id[-6:]}, let's review:",
                "intent": "conversation",
                "context_run_id": run.run_id,
            },
            headers=ALICE_AUTH,
        )
        assert res.status_code == 200
        data = res.json()
        assert data["agent_message"] is not None


def test_gate11_production_environment_blocks_simulation_hijacking():
    """
    Gate 11: In production mode (ENV=production), user messages containing 'simulate-failure'
    are treated as normal conversational text and do not trigger simulated failure runs.
    """
    from app.core.auth import get_current_user
    from app.models.user import User

    alice = User(uid="alice_01", email="alice@example.com", display_name="Alice")
    store.save_user(alice)
    app.dependency_overrides[get_current_user] = lambda: alice

    try:
        space = client.post("/v1/spaces", json={"name": "Prod Space"}, headers=ALICE_AUTH).json()
        space_id = space["space_id"]

        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = MagicMock(
            text="Discussing simulated failures in film production workflows."
        )

        with patch.object(settings, "ENV", "production"), \
             patch("google.genai.Client", return_value=mock_client), \
             patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
            res = client.post(
                "/v1/chat",
                json={
                    "space_id": space_id,
                    "content": "@agent simulate-failure on rigging equipment",
                    "intent": "conversation",
                },
                headers=ALICE_AUTH,
            )
            assert res.status_code == 200
            data = res.json()
            # In production, it must NOT create a simulated failed Run!
            assert data.get("run") is None
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_gate12_context_run_id_cross_space_tenancy_rejected_preflight():
    """
    Gate 12: Passing a Run ID belonging to Space B when chatting in Space A
    must fail with HTTP 404 in pre-flight, before calling Gemini, creating messages,
    or acquiring idempotency locks.
    """
    # Create Space A and Space B
    space_a = client.post("/v1/spaces", json={"name": "Space A"}, headers=ALICE_AUTH).json()
    space_b = client.post("/v1/spaces", json={"name": "Space B"}, headers=ALICE_AUTH).json()
    space_a_id = space_a["space_id"]
    space_b_id = space_b["space_id"]

    # Run belongs to Space B
    run_b = Run(
        space_id=space_b_id,
        project_tag="general",
        status=RunStatus.COMPLETED,
        prompt="Breakdown for Space B",
        created_by="alice_01",
    )
    store.save_run(run_b)

    cmsg_id = "cmsg_tenancy_violation_001"
    failing_ai = MagicMock()
    failing_ai.models.generate_content.side_effect = AssertionError("AI must NOT be called on tenancy violation!")

    with patch("google.genai.Client", return_value=failing_ai), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        res = client.post(
            "/v1/chat",
            json={
                "space_id": space_a_id,
                "content": "Discuss Run from other space",
                "client_message_id": cmsg_id,
                "intent": "conversation",
                "context_run_id": run_b.run_id,  # Run belonging to Space B!
            },
            headers=ALICE_AUTH,
        )
        assert res.status_code == 404
        assert "not found in space" in res.json()["detail"].lower()

        # Verify ZERO messages were saved in Space A or Space B
        assert len(store.list_messages(space_a_id)) == 0
        assert len(store.list_messages(space_b_id)) == 0

        # Verify ZERO idempotency locks left behind
        idemp_key = ChatIdempotencyRecord.compute_key(space_a_id, "alice_01", cmsg_id)
        assert store.get_chat_idempotency(idemp_key) is None


def test_gate13_explicit_document_qa_without_attachment_returns_422():
    """
    Gate 13: Explicitly requesting document_qa without providing any attachments
    returns HTTP 422 Unprocessable Entity.
    """
    space = client.post("/v1/spaces", json={"name": "Doc Space"}, headers=ALICE_AUTH).json()
    space_id = space["space_id"]

    res = client.post(
        "/v1/chat",
        json={
            "space_id": space_id,
            "content": "Analyze document",
            "intent": "document_qa",
            # No attachment_file_ids
        },
        headers=ALICE_AUTH,
    )
    assert res.status_code == 422
    assert "requires at least one attached" in res.json()["detail"].lower()

