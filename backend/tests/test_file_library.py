import io

import pytest
from app.main import app
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


def test_file_upload_and_tagging():
    # 1. Alice creates space
    space_res = client.post("/v1/spaces", json={"name": "Feature Prep"}, headers=ALICE_AUTH)
    space_id = space_res.json()["space_id"]

    # 2. Alice uploads a treatment PDF
    file_content = b"%PDF-1.4 Mock Treatment Content for Bersama Feature"
    files = {"file": ("treatment_v1.pdf", io.BytesIO(file_content), "application/pdf")}
    data = {"project_tag": "block-a"}

    upload_res = client.post(
        f"/v1/spaces/{space_id}/files",
        files=files,
        data=data,
        headers=ALICE_AUTH,
    )
    assert upload_res.status_code == 200
    file_rec = upload_res.json()
    assert file_rec["filename"] == "treatment_v1.pdf"
    assert file_rec["project_tags"] == ["block-a"]
    assert len(file_rec["sha256"]) == 64
    assert file_rec["size_bytes"] == len(file_content)

    # 3. List files with tag filter
    list_res = client.get(f"/v1/spaces/{space_id}/files?tag=block-a", headers=ALICE_AUTH)
    assert list_res.status_code == 200
    assert len(list_res.json()) == 1

    empty_tag_res = client.get(f"/v1/spaces/{space_id}/files?tag=vfx", headers=ALICE_AUTH)
    assert empty_tag_res.status_code == 200
    assert len(empty_tag_res.json()) == 0


def test_cross_space_file_isolation():
    # Alice creates Space A and uploads a confidential budget file
    space_a = client.post("/v1/spaces", json={"name": "Confidential Unit A"}, headers=ALICE_AUTH).json()
    space_a_id = space_a["space_id"]

    upload_res = client.post(
        f"/v1/spaces/{space_a_id}/files",
        files={"file": ("budget.xlsx", io.BytesIO(b"Secret Budget Data"), "application/vnd.ms-excel")},
        headers=ALICE_AUTH,
    )
    file_id = upload_res.json()["file_id"]

    # Bob attempts to list files in Space A -> 403 Forbidden
    bob_list = client.get(f"/v1/spaces/{space_a_id}/files", headers=BOB_AUTH)
    assert bob_list.status_code == 403

    # Bob attempts to download file directly from Space A -> 403 Forbidden
    bob_download = client.get(f"/v1/spaces/{space_a_id}/files/{file_id}/download", headers=BOB_AUTH)
    assert bob_download.status_code == 403


def test_cross_space_file_sharing_with_provenance():
    # Alice creates Space 1 and Space 2
    s1 = client.post("/v1/spaces", json={"name": "Space 1"}, headers=ALICE_AUTH).json()["space_id"]
    s2 = client.post("/v1/spaces", json={"name": "Space 2"}, headers=ALICE_AUTH).json()["space_id"]

    # Alice invites Bob to Space 2 only
    invite = client.post(f"/v1/spaces/{s2}/invites", json={}, headers=ALICE_AUTH).json()["token"]
    client.post(f"/v1/invites/{invite}/accept", headers=BOB_AUTH)

    # Alice uploads a script in Space 1
    upload_res = client.post(
        f"/v1/spaces/{s1}/files",
        files={"file": ("script_v2.pdf", io.BytesIO(b"Script v2 Text"), "application/pdf")},
        headers=ALICE_AUTH,
    )
    file_id = upload_res.json()["file_id"]

    # Bob tries to copy script from Space 1 to Space 2 -> 403 (Bob is not a member of Space 1)
    bob_share = client.post(
        f"/v1/spaces/{s1}/files/{file_id}/share",
        json={"target_space_id": s2},
        headers=BOB_AUTH,
    )
    assert bob_share.status_code == 403

    # Alice (member of both) shares script from Space 1 to Space 2
    alice_share = client.post(
        f"/v1/spaces/{s1}/files/{file_id}/share",
        json={"target_space_id": s2, "target_project_tags": ["shared-scripts"]},
        headers=ALICE_AUTH,
    )
    assert alice_share.status_code == 200
    copied_file = alice_share.json()
    assert copied_file["space_id"] == s2
    assert copied_file["copied_from_space_id"] == s1
    assert copied_file["copied_from_file_id"] == file_id
    assert copied_file["project_tags"] == ["shared-scripts"]

    # Bob can now see and download the copied file in Space 2
    bob_s2_files = client.get(f"/v1/spaces/{s2}/files", headers=BOB_AUTH).json()
    assert any(f["file_id"] == copied_file["file_id"] for f in bob_s2_files)


def test_accessible_only_file_search():
    # Alice uploads in Space 1
    s1 = client.post("/v1/spaces", json={"name": "Alice Space"}, headers=ALICE_AUTH).json()["space_id"]
    client.post(
        f"/v1/spaces/{s1}/files",
        files={"file": ("Bersama_Storyboard_Alpha.pdf", io.BytesIO(b"Storyboards"), "application/pdf")},
        headers=ALICE_AUTH,
    )

    # Alice searches for "storyboard" -> finds it
    alice_search = client.get("/v1/files/search?q=storyboard", headers=ALICE_AUTH).json()
    assert len(alice_search) == 1
    assert alice_search[0]["filename"] == "Bersama_Storyboard_Alpha.pdf"

    # Bob searches for "storyboard" -> returns 0 results (cannot access Space 1)
    bob_search = client.get("/v1/files/search?q=storyboard", headers=BOB_AUTH).json()
    assert len(bob_search) == 0
