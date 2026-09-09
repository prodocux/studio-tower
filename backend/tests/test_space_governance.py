import pytest
from app.main import app
from app.models.file_record import FileRecord, FileSourceType
from app.models.run import ApprovalGate, Run, RunStatus
from app.models.space import MembershipRole
from app.services.storage import store
from fastapi.testclient import TestClient

client = TestClient(app)

ALICE_OWNER = {"Authorization": "Bearer dev:alice_owner:alice@customdomain.io:Alice Director"}
BOB_COORDINATOR = {"Authorization": "Bearer dev:bob_coord:bob@filmcrew.org:Bob Stunt Coordinator"}
CHARLIE_MEMBER = {"Authorization": "Bearer dev:charlie_mem:charlie@vfxhouse.net:Charlie VFX Lead"}
DAVE_OUTSIDER = {"Authorization": "Bearer dev:dave_out:dave@other.com:Dave Outsider"}


@pytest.fixture(autouse=True)
def clean_store():
    store.clear()
    yield
    store.clear()


def test_space_context_capabilities_matrix():
    # 1. Alice creates space (becomes OWNER)
    space = client.post("/v1/spaces", json={"name": "Governance Test Space"}, headers=ALICE_OWNER).json()
    space_id = space["space_id"]

    # 2. Invite Bob as COORDINATOR and Charlie as MEMBER
    inv_bob = client.post(f"/v1/spaces/{space_id}/invites", json={"role": "coordinator"}, headers=ALICE_OWNER).json()
    client.post(f"/v1/invites/{inv_bob['token']}/accept", headers=BOB_COORDINATOR)

    inv_charlie = client.post(f"/v1/spaces/{space_id}/invites", json={"role": "member"}, headers=ALICE_OWNER).json()
    client.post(f"/v1/invites/{inv_charlie['token']}/accept", headers=CHARLIE_MEMBER)

    # 3. Check Alice (OWNER) context & capabilities
    alice_ctx = client.get(f"/v1/spaces/{space_id}/context", headers=ALICE_OWNER).json()
    assert alice_ctx["current_user_role"] == MembershipRole.OWNER.value
    assert alice_ctx["member_count"] == 3
    assert alice_ctx["capabilities"]["can_invite"] is True
    assert alice_ctx["capabilities"]["can_manage_members"] is True
    assert alice_ctx["capabilities"]["can_change_role"] is True
    assert alice_ctx["capabilities"]["can_remove_member"] is True
    assert alice_ctx["capabilities"]["can_transfer_ownership"] is True
    assert alice_ctx["capabilities"]["can_approve_runs"] is True
    assert alice_ctx["capabilities"]["can_manage_tags"] is True
    assert alice_ctx["capabilities"]["can_leave_space"] is False  # Sole owner with other members cannot leave

    # 4. Check Bob (COORDINATOR) context & capabilities
    bob_ctx = client.get(f"/v1/spaces/{space_id}/context", headers=BOB_COORDINATOR).json()
    assert bob_ctx["current_user_role"] == MembershipRole.COORDINATOR.value
    assert bob_ctx["capabilities"]["can_invite"] is True
    assert bob_ctx["capabilities"]["can_manage_members"] is False
    assert bob_ctx["capabilities"]["can_change_role"] is False
    assert bob_ctx["capabilities"]["can_remove_member"] is False
    assert bob_ctx["capabilities"]["can_transfer_ownership"] is False
    assert bob_ctx["capabilities"]["can_approve_runs"] is True
    assert bob_ctx["capabilities"]["can_leave_space"] is True

    # 5. Check Charlie (MEMBER) context & capabilities
    charlie_ctx = client.get(f"/v1/spaces/{space_id}/context", headers=CHARLIE_MEMBER).json()
    assert charlie_ctx["current_user_role"] == MembershipRole.MEMBER.value
    assert charlie_ctx["capabilities"]["can_invite"] is False
    assert charlie_ctx["capabilities"]["can_manage_members"] is False
    assert charlie_ctx["capabilities"]["can_approve_runs"] is False
    assert charlie_ctx["capabilities"]["can_leave_space"] is True

    # 6. Outsider access is forbidden (403)
    outsider_res = client.get(f"/v1/spaces/{space_id}/context", headers=DAVE_OUTSIDER)
    assert outsider_res.status_code == 403


def test_list_members_returns_real_user_profiles_without_fake_domain():
    space = client.post("/v1/spaces", json={"name": "Real Profile Space"}, headers=ALICE_OWNER).json()
    space_id = space["space_id"]

    inv = client.post(f"/v1/spaces/{space_id}/invites", json={"role": "member"}, headers=ALICE_OWNER).json()
    client.post(f"/v1/invites/{inv['token']}/accept", headers=CHARLIE_MEMBER)

    members = client.get(f"/v1/spaces/{space_id}/members", headers=ALICE_OWNER).json()
    assert len(members) == 2

    alice = next(m for m in members if m["uid"] == "alice_owner")
    assert alice["display_name"] == "Alice Director"
    assert alice["email"] == "alice@customdomain.io"  # Real custom email preserved

    charlie = next(m for m in members if m["uid"] == "charlie_mem")
    assert charlie["display_name"] == "Charlie VFX Lead"
    assert charlie["email"] == "charlie@vfxhouse.net"  # Real custom email preserved


def test_ownership_transfer_atomic_and_cas_rejection():
    space = client.post("/v1/spaces", json={"name": "Transfer Space"}, headers=ALICE_OWNER).json()
    space_id = space["space_id"]

    inv_bob = client.post(f"/v1/spaces/{space_id}/invites", json={"role": "coordinator"}, headers=ALICE_OWNER).json()
    client.post(f"/v1/invites/{inv_bob['token']}/accept", headers=BOB_COORDINATOR)

    # 1. Attempting to remove OWNER directly -> 400 Bad Request
    remove_owner_res = client.delete(f"/v1/spaces/{space_id}/members/alice_owner", headers=ALICE_OWNER)
    assert remove_owner_res.status_code == 400
    assert "Cannot remove Space Owner" in remove_owner_res.json()["detail"]

    # 2. Non-owner (Bob) attempts to transfer ownership -> 403 Forbidden
    unauth_transfer = client.post(
        f"/v1/spaces/{space_id}/transfer-ownership",
        json={"new_owner_uid": "bob_coord"},
        headers=BOB_COORDINATOR,
    )
    assert unauth_transfer.status_code == 403

    # 3. Transfer ownership to Bob
    transfer_res = client.post(
        f"/v1/spaces/{space_id}/transfer-ownership",
        json={"new_owner_uid": "bob_coord"},
        headers=ALICE_OWNER,
    )
    assert transfer_res.status_code == 200
    assert transfer_res.json()["new_owner_uid"] == "bob_coord"

    # Verify Bob is now OWNER, Alice is ADMIN
    bob_ctx = client.get(f"/v1/spaces/{space_id}/context", headers=BOB_COORDINATOR).json()
    assert bob_ctx["current_user_role"] == "owner"

    alice_ctx = client.get(f"/v1/spaces/{space_id}/context", headers=ALICE_OWNER).json()
    assert alice_ctx["current_user_role"] == "admin"


def test_lineage_strict_foreign_key_edges_no_cross_run_false_connections():
    space = client.post("/v1/spaces", json={"name": "DAG Space"}, headers=ALICE_OWNER).json()
    space_id = space["space_id"]

    # Create two source files
    file1 = FileRecord(
        file_id="file_script_01",
        space_id=space_id,
        filename="Script_Ep1.pdf",
        uploaded_by="alice_owner",
        source_type=FileSourceType.USER_UPLOAD,
        sha256="abc123sha",
        size_bytes=1024,
    )
    file2 = FileRecord(
        file_id="file_script_02",
        space_id=space_id,
        filename="Script_Ep2.pdf",
        uploaded_by="alice_owner",
        source_type=FileSourceType.USER_UPLOAD,
        sha256="def456sha",
        size_bytes=2048,
    )
    store.save_file(file1)
    store.save_file(file2)

    # Run 1 is ONLY associated with file 1
    run1 = Run(
        run_id="run_001",
        space_id=space_id,
        source_file_id="file_script_01",
        prompt="Analyze Episode 1",
        status=RunStatus.COMPLETED,
        approval_gate=ApprovalGate(gate_id="gate_001", title="Ep 1 Stunt Gate", description="Ep 1 Stunt Risk"),
        output_artifact_ids=["file_art_01"],
        created_by="alice_owner",
    )
    # Output artifact for Run 1
    art1 = FileRecord(
        file_id="file_art_01",
        space_id=space_id,
        filename="ep1_pdx_manifest.json",
        uploaded_by="system",
        source_type=FileSourceType.PDX_ARTIFACT,
        run_id="run_001",
        sha256="art1sha",
        size_bytes=512,
    )
    store.save_run(run1)
    store.save_file(art1)

    # Run 2 is independent with NO source file or output artifacts
    run2 = Run(
        run_id="run_002",
        space_id=space_id,
        source_file_id=None,
        prompt="Chat summary query",
        status=RunStatus.COMPLETED,
        created_by="alice_owner",
    )
    store.save_run(run2)

    # Fetch lineage DAG
    dag = client.get(f"/v1/spaces/{space_id}/lineage", headers=ALICE_OWNER).json()
    nodes = dag["nodes"]
    edges = dag["edges"]

    # Total nodes: file1, file2, run1, gate_001, art1, run2 = 6 nodes
    assert len(nodes) == 6

    # Verify edges:
    # file1 -> run1 (analyzed_by)
    # run1 -> gate_001 (gated_by)
    # gate_001 -> art1 (generated_by)
    assert len(edges) == 3

    assert {"from": "file_script_01", "to": "run_001", "relation": "analyzed_by"} in edges
    assert {"from": "run_001", "to": "gate_001", "relation": "gated_by"} in edges
    assert {"from": "gate_001", "to": "file_art_01", "relation": "generated_by"} in edges

    # Critical Assertion: file2 and run2 have NO false edges connected to run1, gate1, or art1!
    for e in edges:
        assert e["from"] != "file_script_02"
        assert e["to"] != "file_script_02"
        assert e["from"] != "run_002"
        assert e["to"] != "run_002"


def test_search_users_and_direct_add_member():
    # 1. Register users via GET /v1/me
    client.get("/v1/me", headers=ALICE_OWNER)
    client.get("/v1/me", headers=BOB_COORDINATOR)
    client.get("/v1/me", headers=CHARLIE_MEMBER)

    # 2. Search users by email
    search_res = client.get("/v1/users/search?q=bob@filmcrew.org", headers=ALICE_OWNER).json()
    assert len(search_res) >= 1
    assert search_res[0]["email"] == "bob@filmcrew.org"

    # 3. Create space as Alice
    space = client.post("/v1/spaces", json={"name": "Direct Add Test Space"}, headers=ALICE_OWNER).json()
    space_id = space["space_id"]

    # 4. Directly add Bob as coordinator
    add_res = client.post(
        f"/v1/spaces/{space_id}/members/direct-add",
        json={"email_or_uid": "bob@filmcrew.org", "role": "coordinator"},
        headers=ALICE_OWNER,
    ).json()

    assert add_res["email"] == "bob@filmcrew.org"
    assert add_res["role"] == "coordinator"

    # 5. Verify Bob is now a member with coordinator role in space context
    bob_ctx = client.get(f"/v1/spaces/{space_id}/context", headers=BOB_COORDINATOR).json()
    assert bob_ctx["current_user_role"] == "coordinator"
    assert bob_ctx["capabilities"]["can_invite"] is True

