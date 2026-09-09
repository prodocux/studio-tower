import io

import pytest
from app.integrations.pdx_engine import PDXEngine
from app.integrations.prodocux_facade import ProDocuXFacade
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


def test_prodocux_kernel_pdf_extraction():
    import pypdf
    from prodocux_kernel import __version__ as kernel_version

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    result = ProDocuXFacade.extract_pdf_pages(buf.getvalue(), filename="treatment.pdf")

    assert result.doc_sha256
    assert len(result.doc_sha256) == 64
    assert result.metadata["engine"] == f"prodocux=={kernel_version}"


def test_pdx_plan_generation():
    breakdown_mock = {
        "scenes": [
            {"scene_number": 1, "slugline": "EXT. FLOOD - DAWN", "resources": {"cast": ["Maya"], "locations": ["Basin"]}},
            {"scene_number": 2, "slugline": "INT. HANGAR - DAY", "resources": {"cast": ["Rizal"], "locations": ["Hangar"]}},
        ],
        "detected_conflicts": [{"conflict_type": "hero_tech_overallocated", "description": "Aircraft overlap"}],
    }

    plan = PDXEngine.generate_plan_matrix(breakdown_mock)
    assert plan["total_scenes"] == 2
    assert plan["estimated_shoot_days"] == 1
    assert len(plan["schedule_rows"]) == 2
    from pdx_artifact_engine import __version__ as engine_version

    assert plan["pdx_version"] == f"pdx-artifact-engine=={engine_version}"
    assert plan["execution_plan"]["schema_version"] == "pdx_execution_plan_v1"
    assert plan["pdx_plan_digest"]


def test_e2e_approval_and_pdx_artifact_generation():
    # 1. Alice creates space
    space = client.post("/v1/spaces", json={"name": "Disaster Unit Alpha"}, headers=ALICE_AUTH).json()
    space_id = space["space_id"]

    # 2. Alice uploads treatment PDF
    treatment_bytes = b"PROJECT BERSAMA: Flood Rescue and Sacrificial Aircraft Test"
    upload_res = client.post(
        f"/v1/spaces/{space_id}/files",
        files={"file": ("Bersama_Full.txt", io.BytesIO(treatment_bytes), "text/plain")},
        data={"project_tag": "block-a"},
        headers=ALICE_AUTH,
    )
    file_id = upload_res.json()["file_id"]

    # 3. Alice triggers @agent breakdown (explicit intent required to create a Run)
    chat_res = client.post(
        "/v1/chat",
        json={
            "space_id": space_id,
            "content": "@agent breakdown Bersama_Full.txt",
            "project_tag": "block-a",
            "attachment_file_ids": [file_id],
            "intent": "create_breakdown",
        },
        headers=ALICE_AUTH,
    )
    run_data = chat_res.json()["run"]
    run_id = run_data["run_id"]
    assert run_data["status"] == RunStatus.AWAITING_APPROVAL.value

    # 4. Bob (non-member) tries to approve -> 403 Forbidden
    bob_approve = client.post(
        f"/v1/spaces/{space_id}/runs/{run_id}/approve",
        json={"approved": True},
        headers=BOB_AUTH,
    )
    assert bob_approve.status_code == 403

    # 5. Alice approves the risk gate
    alice_approve = client.post(
        f"/v1/spaces/{space_id}/runs/{run_id}/approve",
        json={"approved": True},
        headers=ALICE_AUTH,
    )
    assert alice_approve.status_code == 200
    completed_run = alice_approve.json()

    assert completed_run["status"] == RunStatus.COMPLETED.value
    assert completed_run["approval_gate"]["status"] == "approved"
    assert completed_run["approval_gate"]["approved_by"] == "alice_01"
    assert len(completed_run["output_artifact_ids"]) == 3
    assert completed_run["manifest_file_id"] is not None

    # 6. Verify generated files exist in Space file library
    space_files = client.get(f"/v1/spaces/{space_id}/files", headers=ALICE_AUTH).json()
    assert len(space_files) >= 5  # original upload + 3 artifacts + 1 manifest

    manifest_file_rec = next(f for f in space_files if f["file_id"] == completed_run["manifest_file_id"])
    assert manifest_file_rec["filename"].startswith("RunManifest_")
    manifest_blob = store.get_artifact_blob(space_id, completed_run["manifest_file_id"], manifest_file_rec["filename"])
    import json

    packed = json.loads(manifest_blob.decode("utf-8"))
    assert packed["pdx_engine_baseline"].startswith("pdx-artifact-engine==")
    assert packed["pdx_run_manifest"]["schema_version"] == "pdx_run_manifest_v0"
    assert packed["pdx_run_manifest"]["status"] in {"completed", "completed_with_review"}
    assert packed["pdx_artifact_manifest"]["schema_version"] == "pdx_artifact_manifest_v0"

    # 7. Check that approval message was posted in chat
    msgs = client.get(f"/v1/spaces/{space_id}/messages", headers=ALICE_AUTH).json()
    last_msg = msgs[-1]
    assert "Risk Gate Approved & PDX Execution Completed!" in last_msg["content"]
    assert len(last_msg["attachment_file_ids"]) == 4
