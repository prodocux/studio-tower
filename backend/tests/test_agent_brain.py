import io

import pytest
from app.agent.brain import AgentBrain
from app.agent.schemas import SceneBreakdown
from app.main import app
from app.models.run import RunStatus
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


def test_agent_brain_treatment_analysis_schema():
    treatment_sample = """
    PROJECT BERSAMA - FEATURE TREATMENT
    Act 1: Flood basin perimeter rescue. Captain Hadi coordinates amphibious drones.
    Act 2: Test Hangar engine calibration on sacrificial test aircraft. High explosion risk.
    """

    breakdown, telemetry = AgentBrain.analyze_treatment(treatment_sample, project_tag="block-a")

    assert isinstance(breakdown, SceneBreakdown)
    assert len(breakdown.scenes) >= 2
    assert len(breakdown.detected_conflicts) >= 1
    assert len(breakdown.recommended_gates) >= 1
    assert telemetry.tokens_used > 0
    assert len(telemetry.tool_calls) > 0


def test_chat_agent_invocation_and_run_creation():
    import os
    from unittest.mock import MagicMock, patch

    # 1. Alice creates space
    space = client.post("/v1/spaces", json={"name": "Disaster Unit Space"}, headers=ALICE_AUTH).json()
    space_id = space["space_id"]

    treatment_text = (
        b"BERSAMA: ACT 1 TREATMENT\n\n"
        b"EXT. FLOOD BASIN - DAWN\n"
        b"Rescue crew mobilizes. Cast: Captain Hadi, Pilot Maya.\n"
        b"INT. COMMAND TENT - DAY\n"
        b"Director reviews the shooting schedule with the production crew.\n"
        b"Stunt coordinator briefs the stunt team on Scene 2 high-risk sequences.\n"
        b"VFX: Digital water simulation required for exterior shots.\n"
        b"Screenplay by: Writer Studio. Script v1.0\n"
    )
    upload_res = client.post(
        f"/v1/spaces/{space_id}/files",
        files={"file": ("Bersama_Act1_2.txt", io.BytesIO(treatment_text), "text/plain")},
        data={"project_tag": "block-a"},
        headers=ALICE_AUTH,
    )
    file_id = upload_res.json()["file_id"]
    import json
    mock_breakdown_json = json.dumps({
        "project_title": "Bersama",
        "summary": "Act 1 treatment coverage.",
        "scenes": [
            {"scene_number": 1, "slugline": "EXT. FLOOD BASIN - DAWN", "description": "Rescue crew mobilizes."},
            {"scene_number": 2, "slugline": "INT. COMMAND TENT - DAY", "description": "Director reviews shooting schedule."}
        ],
        "detected_conflicts": [
            {"conflict_type": "casting_double_booking", "description": "Pilot Maya double booked across Scene 1 and Scene 2."}
        ],
        "recommended_gates": [
            {"gate_title": "Scene 2 Stunt Safety Gate", "description": "Safety review for high-risk action beats.", "risk_level": "high"}
        ]
    })

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = MagicMock(text=mock_breakdown_json)

    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaTestKey123"}):
        # 3a. Alice asks a document question (document_qa) — should NOT create a Run
        qa_res = client.post(
            "/v1/chat",
            json={
                "space_id": space_id,
                "content": "What scenes are in this treatment?",
                "project_tag": "block-a",
                "attachment_file_ids": [file_id],
                "intent": "document_qa",
            },
            headers=ALICE_AUTH,
        )
        assert qa_res.status_code == 200
        qa_data = qa_res.json()
        assert qa_data["user_message"]["role"] == "user"
        assert qa_data["agent_message"]["role"] == "agent"
        assert qa_data.get("run") is None, "document_qa must not create a Run"

        # 3b. Alice explicitly requests a scene breakdown — must create a Run and Gate
        chat_res = client.post(
            "/v1/chat",
            json={
                "space_id": space_id,
                "content": "@agent analyze and break down the treatment for Block A",
                "project_tag": "block-a",
                "attachment_file_ids": [file_id],
                "intent": "create_breakdown",
            },
            headers=ALICE_AUTH,
        )
        assert chat_res.status_code == 200
        data = chat_res.json()

    assert data["user_message"]["role"] == "user"
    assert data["agent_message"]["role"] == "agent"
    assert "Scene Breakdown Plan Generated" in data["agent_message"]["content"]
    assert "⚠️ **Detected Conflicts" in data["agent_message"]["content"]

    run = data["run"]
    assert run is not None
    assert run["space_id"] == space_id
    assert run["project_tag"] == "block-a"
    assert run["status"] == RunStatus.AWAITING_APPROVAL.value
    assert run["approval_gate"]["status"] == "pending"
    assert run["telemetry"]["duration_ms"] >= 0

    # 4. Bob tries to access runs in Alice's space -> 403
    bob_runs = client.get(f"/v1/spaces/{space_id}/runs", headers=BOB_AUTH)
    assert bob_runs.status_code == 403
