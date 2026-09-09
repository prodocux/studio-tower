import concurrent.futures
import hashlib
from datetime import UTC, datetime, timedelta
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from app.core.auth import get_current_user
from app.core.config import settings
from app.main import app
from app.models.action_proposal import (
    ActionExecutionRecord,
    ActionExecutionStatus,
    ActionProposal,
    ActionSourceDescriptor,
    ArtifactDescriptor,
    DispatchStatus,
    compute_canonical_proposal_hash,
    issue_action_token,
    verify_action_token,
)
from app.models.file_record import DocumentChunk, FileRecord, IngestionStatus
from app.models.message import Message, MessageRole
from app.models.run import ApprovalGate, Run, RunStatus
from app.models.space import MembershipRole, Space
from app.models.user import User
from app.services.deliverable_service import DeliverableExecutionService
from app.services.storage import StorageConflictError, store

client = TestClient(app)


# -----------------------------------------------------------------------------
# Gate D1: 20 Concurrent Confirm Requests Create Exactly 1 Run & 1 Execution (CAS Idempotent)
# -----------------------------------------------------------------------------


def test_gate_d1_concurrent_action_confirmation_idempotency():
    user_id = "u_d1_coord"
    space_id = "sp_d1_concurrent"
    user = User(uid=user_id, email="coord@d1.com", display_name="Coordinator D1")
    store.save_user(user)

    space = Space(space_id=space_id, name="Concurrent Space", created_by=user_id)
    store.create_space(space, creator_uid=user_id)

    # Add source file
    f_rec = FileRecord(
        file_id="file_d1_script",
        space_id=space_id,
        filename="script_d1.pdf",
        stored_path="spaces/sp_d1/script.pdf",
        size_bytes=1024,
        sha256="abc123d1",
        upload_status="committed",
        ingestion_status=IngestionStatus.READY,
        active_generation=1,
        uploaded_by=user_id,
    )
    store.save_file(f_rec)

    # Issue proposal
    sources = [
        ActionSourceDescriptor(
            file_id=f_rec.file_id,
            active_generation=1,
            content_hash="abc123d1",
            space_id=space_id,
        )
    ]
    proposal = issue_action_token(
        space_id=space_id,
        project_tag="general",
        user_id=user_id,
        title="Generate Daily Call Sheet",
        description="Day 1 Call Sheet",
        sources=sources,
        action_type="create_call_sheet",
        ttl_seconds=300,
    )

    msg = Message(
        space_id=space_id,
        sender_uid="agent_studiotower",
        sender_name="StudioTower Agent",
        role=MessageRole.AGENT,
        content="Prepared action proposal.",
        project_tag="general",
        proposed_action=proposal,
    )
    store.add_message(msg)

    # Run 20 concurrent confirmation requests
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        def _confirm_call():
            test_cli = TestClient(app)
            return test_cli.post(
                f"/v1/spaces/{space_id}/actions/confirm",
                json={"action_id": proposal.action_id},
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(_confirm_call) for _ in range(20)]
            results = [f.result() for f in futures]

        for r in results:
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["status"] == "confirmed"
            assert data["action_id"] == proposal.action_id

        # All 20 calls must yield the exact same run_id
        run_ids = {r.json()["run_id"] for r in results}
        assert len(run_ids) == 1, f"Expected exactly 1 Run ID, got: {run_ids}"

        # Verify only 1 Execution Record and 1 Run exist in store
        exec_rec = store.get_action_execution(proposal.action_id)
        assert exec_rec is not None
        assert exec_rec.run_id == list(run_ids)[0]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# -----------------------------------------------------------------------------
# Gate D2: 3-Phase Artifact Lifecycle & Crash Compensation
# -----------------------------------------------------------------------------


def test_gate_d2_artifact_lifecycle_and_crash_compensation():
    user_id = "u_d2_actor"
    space_id = "sp_d2_lifecycle"
    user = User(uid=user_id, email="actor@d2.com", display_name="Actor D2")
    store.save_user(user)

    space = Space(space_id=space_id, name="Lifecycle Space", created_by=user_id)
    store.create_space(space, creator_uid=user_id)

    proposal = issue_action_token(
        space_id=space_id,
        project_tag="general",
        user_id=user_id,
        title="Shot List Export",
        description="Extract scene shot angles",
        sources=[],
        action_type="generate_shot_list",
        ttl_seconds=300,
    )

    # Initialize execution record
    run_id = f"run_act_{hashlib.sha256(proposal.action_id.encode()).hexdigest()[:12]}"
    exec_rec = ActionExecutionRecord(
        action_id=proposal.action_id,
        run_id=run_id,
        space_id=space_id,
        project_tag="general",
        user_id=user_id,
        status=ActionExecutionStatus.PENDING,
    )
    store.create_action_execution_if_absent(exec_rec)

    # 1. Normal execution flow produces physical artifact and registers record
    run, updated_exec = DeliverableExecutionService.execute_action(proposal, user)
    assert run.status == RunStatus.COMPLETED
    assert updated_exec.status == ActionExecutionStatus.COMPLETED
    assert len(run.output_artifact_ids) == 1

    art_id = run.output_artifact_ids[0]
    art = store.get_artifact(space_id, art_id)
    assert art is not None
    assert art.filename.endswith(".csv")
    assert art.visibility == "published"

    # Verify physical blob exists
    blob = store.get_artifact_blob(space_id, art_id, art.filename)
    assert blob is not None
    assert b"STUDIOTOWER" in blob or b"Shot #" in blob


# -----------------------------------------------------------------------------
# Gate D3: Execution Worker Lease State Machine & Preemption
# -----------------------------------------------------------------------------


def test_gate_d3_execution_worker_lease_state_machine_and_preemption():
    user_id = "u_d3_worker"
    space_id = "sp_d3_lease"
    action_id = "act_d3_lease_test"
    run_id = "run_d3_lease_test"

    exec_rec = ActionExecutionRecord(
        action_id=action_id,
        run_id=run_id,
        space_id=space_id,
        user_id=user_id,
        status=ActionExecutionStatus.PENDING,
        state_version=1,
    )
    created, initial = store.create_action_execution_if_absent(exec_rec)
    assert created is True

    # Worker 1 claims lease
    now = datetime.now(UTC)
    initial.status = ActionExecutionStatus.RUNNING
    initial.lease_owner = "worker_1"
    initial.lease_token = "token_1"
    initial.lease_until = now + timedelta(seconds=60)
    saved_w1 = store.save_action_execution_fenced(initial, expected_version=1)
    assert saved_w1.state_version == 2

    # Stale Worker attempts to transition with outdated state_version -> StorageConflictError
    stale_update = saved_w1.model_copy(deep=True)
    stale_update.status = ActionExecutionStatus.COMPLETED
    with pytest.raises(StorageConflictError):
        store.save_action_execution_fenced(
            stale_update,
            expected_version=1,  # Stale version!
            expected_owner="worker_1",
            expected_token="token_1",
        )

    # Preempted Worker with wrong token -> StorageConflictError
    with pytest.raises(StorageConflictError):
        store.save_action_execution_fenced(
            stale_update,
            expected_version=2,
            expected_owner="worker_1",
            expected_token="wrong_token",
        )

    # Valid completion by active worker
    saved_w1.status = ActionExecutionStatus.COMPLETED
    final = store.save_action_execution_fenced(
        saved_w1,
        expected_version=2,
        expected_owner="worker_1",
        expected_token="token_1",
    )
    assert final.status == ActionExecutionStatus.COMPLETED
    assert final.state_version == 3


# -----------------------------------------------------------------------------
# Gate D4: 256-Bit Cryptographic Token & Dual-Key Secret Rotation (kid)
# -----------------------------------------------------------------------------


def test_gate_d4_256bit_hmac_and_dual_key_rotation():
    user_id = "u_d4_crypto"
    space_id = "sp_d4_crypto"

    # 1. Token signed with active key 'v2'
    proposal_v2 = issue_action_token(
        space_id=space_id,
        project_tag="general",
        user_id=user_id,
        title="Token V2 Test",
        description="Testing 256-bit token",
        sources=[],
        key_id="v2",
        ttl_seconds=300,
    )
    assert verify_action_token(proposal_v2, expected_user_id=user_id, expected_space_id=space_id) is True

    # 2. Token signed with previous key 'v1' still verifies under dual-key rotation
    proposal_v1 = issue_action_token(
        space_id=space_id,
        project_tag="general",
        user_id=user_id,
        title="Token V1 Test",
        description="Testing v1 key rotation support",
        sources=[],
        key_id="v1",
        ttl_seconds=300,
    )
    assert verify_action_token(proposal_v1, expected_user_id=user_id, expected_space_id=space_id) is True

    # 3. Unknown kid fails closed
    proposal_unknown = proposal_v2.model_copy(deep=True)
    proposal_unknown.action_id = proposal_unknown.action_id.replace(f"_{proposal_unknown.key_id}_", "_v999_")
    assert verify_action_token(proposal_unknown, expected_user_id=user_id, expected_space_id=space_id) is False


# -----------------------------------------------------------------------------
# Gate D5: Per-File Generation and Content Hash Drift Rejection (HTTP 409 ACTION_STALE)
# -----------------------------------------------------------------------------


def test_gate_d5_per_file_generation_and_hash_fencing():
    user_id = "u_d5_drift"
    space_id = "sp_d5_drift"
    user = User(uid=user_id, email="drift@d5.com", display_name="Drift D5")
    store.save_user(user)

    space = Space(space_id=space_id, name="Drift Space", created_by=user_id)
    store.create_space(space, creator_uid=user_id)

    f1 = FileRecord(
        file_id="file_d5_01",
        space_id=space_id,
        filename="script_scene1.pdf",
        size_bytes=2048,
        sha256="hash_scene1_v1",
        upload_status="committed",
        ingestion_status=IngestionStatus.READY,
        active_generation=1,
        uploaded_by=user_id,
    )
    f2 = FileRecord(
        file_id="file_d5_02",
        space_id=space_id,
        filename="script_scene2.pdf",
        size_bytes=3072,
        sha256="hash_scene2_v2",
        upload_status="committed",
        ingestion_status=IngestionStatus.READY,
        active_generation=2,
        uploaded_by=user_id,
    )
    store.save_file(f1)
    store.save_file(f2)

    # Multi-file proposal binding distinct generations
    sources = [
        ActionSourceDescriptor(file_id="file_d5_01", active_generation=1, content_hash="hash_scene1_v1", space_id=space_id),
        ActionSourceDescriptor(file_id="file_d5_02", active_generation=2, content_hash="hash_scene2_v2", space_id=space_id),
    ]
    proposal = issue_action_token(
        space_id=space_id,
        project_tag="general",
        user_id=user_id,
        title="Multi-File Breakdown",
        description="Testing per-file drift",
        sources=sources,
        ttl_seconds=300,
    )
    msg = Message(
        space_id=space_id,
        sender_uid="agent_studiotower",
        role=MessageRole.AGENT,
        content="Action proposal multi-file",
        proposed_action=proposal,
    )
    store.add_message(msg)

    app.dependency_overrides[get_current_user] = lambda: user
    try:
        # Simulate re-indexing on file 1 (generation 1 -> 2)
        f1.active_generation = 2
        store.save_file(f1)

        res_drift = client.post(
            f"/v1/spaces/{space_id}/actions/confirm",
            json={"action_id": proposal.action_id},
        )
        assert res_drift.status_code == 409
        assert "SOURCE_FILE_GENERATION_MISMATCH" in res_drift.json()["detail"]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# -----------------------------------------------------------------------------
# Gate D6: Approval Gate Atomic Visibility Policy & Outbox State Synchronization
# -----------------------------------------------------------------------------


def test_gate_d6_approval_gate_visibility_policy_and_cas():
    creator_id = "u_d6_coord"
    member_id = "u_d6_member"
    space_id = "sp_d6_gate"

    coord = User(uid=creator_id, email="coord@d6.com", display_name="Coordinator D6")
    member = User(uid=member_id, email="member@d6.com", display_name="Member D6")
    store.save_user(coord)
    store.save_user(member)

    space = Space(space_id=space_id, name="Gate Space", created_by=creator_id)
    store.create_space(space, creator_uid=creator_id)
    store.add_member(space_id, member_id, MembershipRole.MEMBER)

    # Issue stunt risk breakdown action (Critical risk -> AWAITING_APPROVAL)
    proposal = issue_action_token(
        space_id=space_id,
        project_tag="general",
        user_id=creator_id,
        title="Cliff Stunt Hazard Assessment",
        description="High Fall Wire Rigging",
        sources=[],
        action_type="stunt_risk_breakdown",
        ttl_seconds=300,
    )
    msg = Message(
        space_id=space_id,
        sender_uid="agent_studiotower",
        role=MessageRole.AGENT,
        content="Stunt risk action proposal",
        proposed_action=proposal,
    )
    store.add_message(msg)

    # Confirm action
    app.dependency_overrides[get_current_user] = lambda: coord
    try:
        res_conf = client.post(
            f"/v1/spaces/{space_id}/actions/confirm",
            json={"action_id": proposal.action_id},
        )
        assert res_conf.status_code == 200
        run_id = res_conf.json()["run_id"]
        run = store.get_run(run_id)
        assert run.status == RunStatus.AWAITING_APPROVAL
        art_id = run.output_artifact_ids[0]

        # Standard member cannot download unapproved artifact -> HTTP 403
        app.dependency_overrides[get_current_user] = lambda: member
        res_down_blocked = client.get(f"/v1/spaces/{space_id}/artifacts/{art_id}/download")
        assert res_down_blocked.status_code == 403
        assert "ARTIFACT_AWAITING_APPROVAL" in res_down_blocked.json()["detail"]

        # Coordinator approves the gate
        app.dependency_overrides[get_current_user] = lambda: coord
        res_approve = client.post(
            f"/v1/spaces/{space_id}/runs/{run_id}/approve",
            json={"approved": True},
        )
        assert res_approve.status_code == 200

        # Verify ActionExecution status transitioned to COMPLETED
        exec_rec = store.get_action_execution(proposal.action_id)
        assert exec_rec is not None
        assert exec_rec.status == ActionExecutionStatus.COMPLETED

        # Duplicate approval returns HTTP 409 GATE_ALREADY_DECIDED
        res_dup = client.post(
            f"/v1/spaces/{space_id}/runs/{run_id}/approve",
            json={"approved": True},
        )
        assert res_dup.status_code == 409
        assert "GATE_ALREADY_DECIDED" in res_dup.json()["detail"]

        # Standard member can now download published artifact
        app.dependency_overrides[get_current_user] = lambda: member
        res_down_ok = client.get(f"/v1/spaces/{space_id}/artifacts/{art_id}/download")
        assert res_down_ok.status_code == 200
        assert b"CRITICAL" in res_down_ok.content
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# -----------------------------------------------------------------------------
# Gate D7: Reverse-Cursor Message Pagination Contract
# -----------------------------------------------------------------------------


def test_gate_d7_reverse_cursor_pagination_contract():
    user_id = "u_d7_page"
    space_id = "sp_d7_page"
    user = User(uid=user_id, email="page@d7.com", display_name="Page D7")
    store.save_user(user)

    space = Space(space_id=space_id, name="Paging Space D7", created_by=user_id)
    store.create_space(space, creator_uid=user_id)

    # Insert 6 messages
    base_time = datetime.now(UTC) - timedelta(minutes=10)
    for i in range(6):
        store.add_message(
            Message(
                message_id=f"msg_d7_{i:02d}",
                space_id=space_id,
                sender_uid=user_id,
                role=MessageRole.USER,
                content=f"Message {i:02d}",
                project_tag="general",
                created_at=base_time + timedelta(minutes=i),
            )
        )

    app.dependency_overrides[get_current_user] = lambda: user
    try:
        # Request latest page of 3 messages (returns msg #03, #04, #05 in ASC order)
        res1 = client.get(f"/v1/spaces/{space_id}/messages/page?limit=3")
        assert res1.status_code == 200
        data1 = res1.json()
        assert len(data1["items"]) == 3
        assert data1["has_more"] is True
        assert [m["message_id"] for m in data1["items"]] == ["msg_d7_03", "msg_d7_04", "msg_d7_05"]

        # Request previous page using cursor
        cursor = data1["next_cursor"]
        res2 = client.get(f"/v1/spaces/{space_id}/messages/page?limit=3&cursor={cursor}")
        assert res2.status_code == 200
        data2 = res2.json()
        assert len(data2["items"]) == 3
        assert data2["has_more"] is False
        assert [m["message_id"] for m in data2["items"]] == ["msg_d7_00", "msg_d7_01", "msg_d7_02"]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# -----------------------------------------------------------------------------
# Gate D8: Untrusted Model Schema Sanitization
# -----------------------------------------------------------------------------


def test_gate_d8_untrusted_model_schema_sanitization():
    user_id = "u_d8_sanitize"
    space_id = "sp_d8_sanitize"
    user = User(uid=user_id, email="san@d8.com", display_name="Sanitize D8")
    store.save_user(user)

    space = Space(space_id=space_id, name="Sanitize Space", created_by=user_id)
    store.create_space(space, creator_uid=user_id)

    # Unknown / malicious action type rejected
    proposal_bad_type = issue_action_token(
        space_id=space_id,
        project_tag="general",
        user_id=user_id,
        title="Malicious Action",
        description="Exploit test",
        sources=[],
        action_type="arbitrary_system_command",
    )
    msg = Message(
        space_id=space_id,
        sender_uid="agent_studiotower",
        role=MessageRole.AGENT,
        content="Bad action proposal",
        proposed_action=proposal_bad_type,
    )
    store.add_message(msg)

    app.dependency_overrides[get_current_user] = lambda: user
    try:
        res = client.post(
            f"/v1/spaces/{space_id}/actions/confirm",
            json={"action_id": proposal_bad_type.action_id},
        )
        assert res.status_code == 400
        assert "ACTION_TYPE_UNAUTHORIZED" in res.json()["detail"]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# -----------------------------------------------------------------------------
# Gate D9: Authenticated Artifact Download Integrity
# -----------------------------------------------------------------------------


def test_gate_d9_authenticated_artifact_download_integrity():
    user_id = "u_d9_down"
    space_id = "sp_d9_down"
    user = User(uid=user_id, email="down@d9.com", display_name="Down D9")
    store.save_user(user)

    space = Space(space_id=space_id, name="Down Space", created_by=user_id)
    store.create_space(space, creator_uid=user_id)

    raw_csv = b"Account Code,Category,Total USD\n1001,Director,150000.00\n"
    csv_hash = hashlib.sha256(raw_csv).hexdigest()
    art_id = "art_d9_budget"

    store.save_artifact_blob(space_id, art_id, "budget_est.csv", raw_csv)
    art_desc = ArtifactDescriptor(
        artifact_id=art_id,
        space_id=space_id,
        run_id="run_d9_dummy",
        filename="budget_est.csv",
        media_type="text/csv",
        size_bytes=len(raw_csv),
        sha256=csv_hash,
        storage_path=f"spaces/{space_id}/artifacts/{art_id}/budget_est.csv",
        download_endpoint=f"/v1/spaces/{space_id}/artifacts/{art_id}/download",
        visibility="published",
    )
    store.save_artifact_record(art_desc)

    app.dependency_overrides[get_current_user] = lambda: user
    try:
        res = client.get(f"/v1/spaces/{space_id}/artifacts/{art_id}/download")
        assert res.status_code == 200
        assert res.headers["X-Artifact-Sha256"] == csv_hash
        assert res.content == raw_csv
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# -----------------------------------------------------------------------------
# Gate D10: Cross-User & Cross-Space Token Replay Rejection
# -----------------------------------------------------------------------------


def test_gate_d10_cross_user_and_cross_space_token_replay_rejection():
    victim_user = "u_d10_victim"
    attacker_user = "u_d10_attacker"
    space_id = "sp_d10_space"

    u_victim = User(uid=victim_user, email="vic@d10.com", display_name="Victim")
    u_attacker = User(uid=attacker_user, email="att@d10.com", display_name="Attacker")
    store.save_user(u_victim)
    store.save_user(u_attacker)

    space = Space(space_id=space_id, name="Replay Space", created_by=victim_user)
    store.create_space(space, creator_uid=victim_user)
    store.add_member(space_id, attacker_user, MembershipRole.MEMBER)

    proposal = issue_action_token(
        space_id=space_id,
        project_tag="general",
        user_id=victim_user,
        title="Victim Action Proposal",
        description="Targeted action",
        sources=[],
        action_type="create_call_sheet",
        ttl_seconds=300,
    )
    msg = Message(
        space_id=space_id,
        sender_uid="agent_studiotower",
        role=MessageRole.AGENT,
        content="Targeted proposal",
        proposed_action=proposal,
    )
    store.add_message(msg)

    # Attacker attempts to confirm Victim's proposal -> HTTP 400 ACTION_PROPOSAL_INVALID_OR_EXPIRED
    app.dependency_overrides[get_current_user] = lambda: u_attacker
    try:
        res = client.post(
            f"/v1/spaces/{space_id}/actions/confirm",
            json={"action_id": proposal.action_id},
        )
        assert res.status_code == 400
        assert "ACTION_PROPOSAL_INVALID_OR_EXPIRED" in res.json()["detail"]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# -----------------------------------------------------------------------------
# Gate D11: Storage-Authoritative Crash Recovery & Stalled Lease Reclamation
# -----------------------------------------------------------------------------


def test_gate_d11_hard_crash_recovery_and_stalled_lease_reclamation():
    user_id = "u_d11_crash"
    space_id = "sp_d11_crash"
    user = User(uid=user_id, email="crash@d11.com", display_name="Crash D11")
    store.save_user(user)

    space = Space(space_id=space_id, name="Crash Space", created_by=user_id)
    store.create_space(space, creator_uid=user_id)

    # 1. Create a stalled execution that exceeded max attempts (terminal failure)
    action_id_term = "act_d11_terminal"
    run_id_term = "run_d11_terminal"
    stalled_term = ActionExecutionRecord(
        action_id=action_id_term,
        run_id=run_id_term,
        space_id=space_id,
        project_tag="general",
        user_id=user_id,
        status=ActionExecutionStatus.RUNNING,
        attempts=3,  # Reached max attempts
        lease_owner="crashed_worker_1",
        lease_token="token_crashed_1",
        lease_until=datetime.now(UTC) - timedelta(minutes=5),
        state_version=3,
    )
    store.action_executions[action_id_term] = stalled_term
    store.save_run(Run(run_id=run_id_term, space_id=space_id, status=RunStatus.RUNNING, prompt="Crash Run", created_by=user_id))

    # 2. Create a stalled execution that can be re-dispatched (attempts < max)
    action_id_retry = "act_d11_retry"
    run_id_retry = "run_d11_retry"
    stalled_retry = ActionExecutionRecord(
        action_id=action_id_retry,
        run_id=run_id_retry,
        space_id=space_id,
        project_tag="general",
        user_id=user_id,
        status=ActionExecutionStatus.RUNNING,
        attempts=1,  # Can retry
        lease_owner="crashed_worker_2",
        lease_token="token_crashed_2",
        lease_until=datetime.now(UTC) - timedelta(minutes=2),
        state_version=1,
    )
    store.action_executions[action_id_retry] = stalled_retry

    # Storage-level listing must detect both stalled records
    stalled_list = store.list_stalled_action_executions(space_id)
    assert len(stalled_list) == 2

    # Run reclaimer
    reclaimed = DeliverableExecutionService.reclaim_stalled_action_executions(space_id, max_attempts=3)
    assert reclaimed == 2

    # Verify terminal record
    updated_term = store.get_action_execution(action_id_term)
    assert updated_term.status == ActionExecutionStatus.FAILED
    assert updated_term.failure_code == "ERR_LEASE_EXPIRED"

    # Verify retried record transitioned to PENDING for re-dispatch
    updated_retry = store.get_action_execution(action_id_retry)
    assert updated_retry.status == ActionExecutionStatus.PENDING


# -----------------------------------------------------------------------------
# Gate D12: Source-Derived Deliverable Generation & Provenance Fidelity
# -----------------------------------------------------------------------------


def test_gate_d12_source_derived_deliverable_generation_and_provenance():
    user_id = "u_d12_script"
    space_id = "sp_d12_fidelity"
    user = User(uid=user_id, email="script@d12.com", display_name="Writer D12")
    store.save_user(user)

    space = Space(space_id=space_id, name="Script Space", created_by=user_id)
    store.create_space(space, creator_uid=user_id)

    file_id = "file_d12_cyberpunk"
    script_text = (
        "SCENE 1: EXT. NEO SHIBUYA ROOFTOP - NIGHT\n"
        "Rain lashes against the neon billboards. KAZUMA stands at the edge.\n"
        "KAZUMA\n"
        "The neural link is synchronized.\n"
        "HANNAH leaps from the hovercraft with a grappling wire.\n"
        "SCENE 2: INT. SUB-LEVEL CRYPTO VAULT - NIGHT\n"
        "Security lasers sweep the server racks. Kazuma disables the grid.\n"
    )
    f_rec = FileRecord(
        file_id=file_id,
        space_id=space_id,
        filename="cyberpunk_heist.fountain",
        size_bytes=len(script_text),
        sha256=hashlib.sha256(script_text.encode()).hexdigest(),
        upload_status="committed",
        ingestion_status=IngestionStatus.READY,
        active_generation=1,
        uploaded_by=user_id,
    )
    store.save_file(f_rec)

    chunk = DocumentChunk(
        chunk_id="chk_d12_01",
        file_id=file_id,
        space_id=space_id,
        ingestion_version=1,
        ordinal=0,
        source_locator="page:1",
        raw_text=script_text,
        normalized_text=script_text,
        char_start=0,
        char_end=len(script_text),
        token_count=80,
        content_hash="hash_d12",
        extraction_method="screenplay",
        extractor_version="v1.0",
    )
    store.save_document_chunks(space_id, file_id, 1, [chunk])

    sources = [
        ActionSourceDescriptor(
            file_id=file_id,
            active_generation=1,
            content_hash=f_rec.sha256,
            space_id=space_id,
        )
    ]
    proposal = issue_action_token(
        space_id=space_id,
        project_tag="general",
        user_id=user_id,
        title="Neo Shibuya Heist Call Sheet",
        description="Day 1 Call Sheet for Neo Shibuya scenes",
        sources=sources,
        action_type="create_call_sheet",
        ttl_seconds=300,
    )

    run_id = f"run_act_{hashlib.sha256(proposal.action_id.encode()).hexdigest()[:12]}"
    exec_rec = ActionExecutionRecord(
        action_id=proposal.action_id,
        run_id=run_id,
        space_id=space_id,
        project_tag="general",
        user_id=user_id,
        status=ActionExecutionStatus.PENDING,
    )
    store.create_action_execution_if_absent(exec_rec)

    run, final_exec = DeliverableExecutionService.execute_action(proposal, user)
    assert run.status == RunStatus.COMPLETED
    assert len(run.output_artifact_ids) == 1

    art_id = run.output_artifact_ids[0]
    art = store.get_artifact(space_id, art_id)
    blob = store.get_artifact_blob(space_id, art_id, art.filename)
    assert blob is not None
    csv_content = blob.decode("utf-8")

    assert "KAZUMA" in csv_content
    assert "HANNAH" in csv_content
    assert "NEO SHIBUYA" in csv_content or "ROOFTOP" in csv_content
    assert "CRYPTO VAULT" in csv_content or "VAULT" in csv_content

    # Zero dummy demo text
    assert "Commander Vance" not in csv_content
    assert "Dr. Maya Lin" not in csv_content


# -----------------------------------------------------------------------------
# Gate D13: Cloud Tasks Worker Callback Endpoint Authentication & Execution
# -----------------------------------------------------------------------------


def test_gate_d13_cloud_tasks_worker_callback_and_auth():
    user_id = "u_d13_worker"
    space_id = "sp_d13_tasks"
    user = User(uid=user_id, email="worker@d13.com", display_name="Worker D13")
    store.save_user(user)

    space = Space(space_id=space_id, name="Cloud Tasks Space", created_by=user_id)
    store.create_space(space, creator_uid=user_id)

    proposal = issue_action_token(
        space_id=space_id,
        project_tag="general",
        user_id=user_id,
        title="Async Shot List Task",
        description="Task queue execution",
        sources=[],
        action_type="generate_shot_list",
        ttl_seconds=300,
    )
    run_id = f"run_act_{hashlib.sha256(proposal.action_id.encode()).hexdigest()[:12]}"
    exec_rec = ActionExecutionRecord(
        action_id=proposal.action_id,
        run_id=run_id,
        space_id=space_id,
        project_tag="general",
        user_id=user_id,
        status=ActionExecutionStatus.PENDING,
        proposal_snapshot=proposal,
        proposal_snapshot_hash=compute_canonical_proposal_hash(proposal),
    )
    store.create_action_execution_if_absent(exec_rec)

    # 1. Invalid Task Secret rejected with HTTP 403
    res_bad_secret = client.post(
        f"/v1/spaces/{space_id}/actions/{proposal.action_id}/execute",
        headers={"X-StudioTower-Task-Secret": "invalid-secret-key"},
    )
    assert res_bad_secret.status_code == 403
    assert "INVALID_TASK_SECRET" in res_bad_secret.json()["detail"]

    # 2. Valid Task Secret executes and completes action
    res_ok = client.post(
        f"/v1/spaces/{space_id}/actions/{proposal.action_id}/execute",
        headers={"X-StudioTower-Task-Secret": settings.STUDIO_TOWER_TASK_SECRET},
        json={"space_id": space_id, "action_id": proposal.action_id, "user_id": user_id},
    )
    assert res_ok.status_code == 200
    assert res_ok.json()["status"] == "success"
    assert res_ok.json()["execution_status"] == "completed"

    # 3. Missing proposal snapshot fails closed with 422 (refusing chat message scan fallback)
    missing_snap_action_id = "act_d13_no_snap"
    exec_no_snap = ActionExecutionRecord(
        action_id=missing_snap_action_id,
        run_id="run_d13_no_snap",
        space_id=space_id,
        project_tag="general",
        user_id=user_id,
        status=ActionExecutionStatus.PENDING,
        proposal_snapshot=None,
    )
    store.action_executions[missing_snap_action_id] = exec_no_snap

    res_no_snap = client.post(
        f"/v1/spaces/{space_id}/actions/{missing_snap_action_id}/execute",
        headers={"X-StudioTower-Task-Secret": settings.STUDIO_TOWER_TASK_SECRET},
        json={"space_id": space_id, "action_id": missing_snap_action_id, "user_id": user_id},
    )
    assert res_no_snap.status_code == 422
    assert "SNAPSHOT_MISSING" in res_no_snap.json()["detail"]

    # 4. Hash mismatch fails closed with 400
    tampered_action_id = "act_d13_tampered"
    exec_tampered = ActionExecutionRecord(
        action_id=tampered_action_id,
        run_id="run_d13_tampered",
        space_id=space_id,
        project_tag="general",
        user_id=user_id,
        status=ActionExecutionStatus.PENDING,
        proposal_snapshot=proposal,
        proposal_snapshot_hash="corrupted_hash_value",
    )
    store.action_executions[tampered_action_id] = exec_tampered

    res_tampered = client.post(
        f"/v1/spaces/{space_id}/actions/{tampered_action_id}/execute",
        headers={"X-StudioTower-Task-Secret": settings.STUDIO_TOWER_TASK_SECRET},
        json={"space_id": space_id, "action_id": tampered_action_id, "user_id": user_id},
    )
    assert res_tampered.status_code == 400
    assert "PROPOSAL_INTEGRITY_COMPROMISED" in res_tampered.json()["detail"]


# -----------------------------------------------------------------------------
# Gate D14: Durable Artifact Cleanup Queue Record Enqueue and Completion
# -----------------------------------------------------------------------------


def test_gate_d14_durable_artifact_cleanup_queue():
    space_id = "sp_d14_cleanup"
    art_id = "art_d14_orphaned"
    filename = "orphaned_schedule.csv"

    # Enqueue cleanup job
    store.enqueue_artifact_cleanup(space_id, art_id, filename, reason="METADATA_COMMIT_FAILED")

    # Verify job in queue
    pending = store.list_pending_artifact_cleanups()
    assert any(j["artifact_id"] == art_id for j in pending)

    # Complete cleanup job
    job_id = f"clean_art_{art_id}"
    store.complete_artifact_cleanup(job_id)

    # Verify job marked completed
    pending_after = store.list_pending_artifact_cleanups()
    assert not any(j["artifact_id"] == art_id for j in pending_after)


# -----------------------------------------------------------------------------
# Gate D15: Ungrounded Template Deliverable Disclaimers
# -----------------------------------------------------------------------------


def test_gate_d15_ungrounded_template_disclaimers():
    user_id = "u_d15_empty"
    space_id = "sp_d15_empty"
    user = User(uid=user_id, email="empty@d15.com", display_name="Empty Sources User")
    store.save_user(user)

    space = Space(space_id=space_id, name="Empty Sources Space", created_by=user_id)
    store.create_space(space, creator_uid=user_id)

    # Proposal with NO sources attached
    proposal = issue_action_token(
        space_id=space_id,
        project_tag="general",
        user_id=user_id,
        title="Blank Production Schedule",
        description="Template schedule",
        sources=[],
        action_type="create_call_sheet",
        ttl_seconds=300,
    )
    run_id = f"run_act_{hashlib.sha256(proposal.action_id.encode()).hexdigest()[:12]}"
    exec_rec = ActionExecutionRecord(
        action_id=proposal.action_id,
        run_id=run_id,
        space_id=space_id,
        project_tag="general",
        user_id=user_id,
        status=ActionExecutionStatus.PENDING,
        proposal_snapshot=proposal,
        proposal_snapshot_hash=compute_canonical_proposal_hash(proposal),
    )
    store.create_action_execution_if_absent(exec_rec)

    run, final_exec = DeliverableExecutionService.execute_action(proposal, user)
    assert run.status == RunStatus.COMPLETED

    art_id = run.output_artifact_ids[0]
    art = store.get_artifact(space_id, art_id)
    blob = store.get_artifact_blob(space_id, art_id, art.filename)
    assert blob is not None
    csv_text = blob.decode("utf-8")

    # MUST contain explicit ungrounded template notice
    assert "UNGROUNDED" in csv_text or "DRAFT PRODUCTION TEMPLATE" in csv_text


# -----------------------------------------------------------------------------
# Gate D16: Parallel Worker Concurrency & Loser Safe No-Op with Winner Heartbeat
# -----------------------------------------------------------------------------


def test_gate_d16_parallel_worker_concurrency_and_safe_loser_noop():
    user_id = "u_d16_worker"
    space_id = "sp_d16_concurrency"
    user = User(uid=user_id, email="worker@d16.com", display_name="Worker D16")
    store.save_user(user)

    space = Space(space_id=space_id, name="Concurrency Space", created_by=user_id)
    store.create_space(space, creator_uid=user_id)

    proposal = issue_action_token(
        space_id=space_id,
        project_tag="general",
        user_id=user_id,
        title="Concurrent Call Sheet",
        description="Testing parallel workers",
        sources=[],
        action_type="create_call_sheet",
        ttl_seconds=300,
    )
    run_id = f"run_act_{hashlib.sha256(proposal.action_id.encode()).hexdigest()[:12]}"
    exec_rec = ActionExecutionRecord(
        action_id=proposal.action_id,
        run_id=run_id,
        space_id=space_id,
        project_tag="general",
        user_id=user_id,
        status=ActionExecutionStatus.PENDING,
        proposal_snapshot=proposal,
        proposal_snapshot_hash=compute_canonical_proposal_hash(proposal),
    )
    store.create_action_execution_if_absent(exec_rec)

    # Worker A claims execution and starts
    run_a, exec_a = DeliverableExecutionService.execute_action(proposal, user, worker_id="worker_A")
    assert exec_a.status == ActionExecutionStatus.COMPLETED
    assert exec_a.lease_owner == "worker_A"

    art_id = run_a.output_artifact_ids[0]
    art = store.get_artifact(space_id, art_id)
    winner_blob = store.get_artifact_blob(space_id, art_id, art.filename)
    assert winner_blob is not None

    # Duplicate callback / Worker B arrives concurrently
    run_b, exec_b = DeliverableExecutionService.execute_action(proposal, user, worker_id="worker_B")
    # Loser gets no-op, doesn't modify state, and crucially DOES NOT wipe out Winner's blob!
    assert exec_b.status == ActionExecutionStatus.COMPLETED
    assert store.get_artifact_blob(space_id, art_id, art.filename) is not None


# -----------------------------------------------------------------------------
# Gate D17: Strict Firestore Read-Before-Write Order, Artifact Union Validation & Fail-Closed Rollback
# -----------------------------------------------------------------------------


def test_gate_d17_artifact_union_validation_and_fail_closed_rollback():
    user_id = "u_d17_approver"
    space_id = "sp_d17_union"
    other_space_id = "sp_d17_other"
    user = User(uid=user_id, email="approver@d17.com", display_name="Approver D17")
    store.save_user(user)

    store.create_space(Space(space_id=space_id, name="Union Space", created_by=user_id), creator_uid=user_id)
    store.create_space(Space(space_id=other_space_id, name="Other Space", created_by=user_id), creator_uid=user_id)

    run_id = "run_d17_test"
    gate = ApprovalGate(
        gate_id="gate_d17",
        title="Gate D17",
        description="Gate test",
        risk_level="critical",
        status="approving",
        decision_lease_token="token_d17_valid",
    )
    run = Run(
        run_id=run_id,
        space_id=space_id,
        status=RunStatus.RUNNING,
        approval_gate=gate,
        output_artifact_ids=["art_d17_initial"],
        created_by=user_id,
    )
    store.save_run(run)

    # Initial artifact
    art_initial = ArtifactDescriptor(
        artifact_id="art_d17_initial",
        space_id=space_id,
        run_id=run_id,
        filename="initial.csv",
        media_type="text/csv",
        size_bytes=10,
        sha256="hash1",
        storage_path="path1",
        download_endpoint="/download/1",
        visibility="pending_approval",
    )
    store.save_artifact_record(art_initial)

    # Cross-space artifact
    art_rogue = ArtifactDescriptor(
        artifact_id="art_d17_rogue",
        space_id=other_space_id,
        run_id=run_id,
        filename="rogue.csv",
        media_type="text/csv",
        size_bytes=10,
        sha256="hash2",
        storage_path="path2",
        download_endpoint="/download/2",
        visibility="pending_approval",
    )
    store.save_artifact_record(art_rogue)

    # 1. Attempting approval with rogue cross-space artifact MUST fail-closed
    with pytest.raises(StorageConflictError):
        store.approve_gate_and_publish_artifacts_atomic(
            space_id=space_id,
            run_id=run_id,
            approver_uid=user_id,
            output_artifact_ids=["art_d17_rogue"],
            decision_lease_token="token_d17_valid",
        )

    # Assert zero changes written
    assert store.get_run(run_id).status == RunStatus.RUNNING
    assert store.get_artifact(space_id, "art_d17_initial").visibility == "pending_approval"

    # 2. Decision lease token mismatch MUST fail-closed
    with pytest.raises(StorageConflictError):
        store.approve_gate_and_publish_artifacts_atomic(
            space_id=space_id,
            run_id=run_id,
            approver_uid=user_id,
            output_artifact_ids=[],
            decision_lease_token="wrong_token",
        )

    # 3. Valid union commit succeeds and publishes all union artifacts
    art_second = ArtifactDescriptor(
        artifact_id="art_d17_second",
        space_id=space_id,
        run_id=run_id,
        filename="second.csv",
        media_type="text/csv",
        size_bytes=10,
        sha256="hash3",
        storage_path="path3",
        download_endpoint="/download/3",
        visibility="pending_approval",
    )
    store.save_artifact_record(art_second)

    completed_run, _ = store.approve_gate_and_publish_artifacts_atomic(
        space_id=space_id,
        run_id=run_id,
        approver_uid=user_id,
        output_artifact_ids=["art_d17_second"],
        decision_lease_token="token_d17_valid",
    )
    assert completed_run.status == RunStatus.COMPLETED
    assert store.get_artifact(space_id, "art_d17_initial").visibility == "published"
    assert store.get_artifact(space_id, "art_d17_second").visibility == "published"


# -----------------------------------------------------------------------------
# Gate D18: Dispatch Fencing State Machine & AlreadyExists Absorption
# -----------------------------------------------------------------------------


def test_gate_d18_dispatch_fencing_and_already_exists():
    from app.services.action_runner import CloudTasksActionRunner

    action_id = "act_d18_fencing"
    run_id = "run_d18_fencing"
    space_id = "sp_d18"
    user_id = "u_d18"

    exec_rec = ActionExecutionRecord(
        action_id=action_id,
        run_id=run_id,
        space_id=space_id,
        project_tag="general",
        user_id=user_id,
        status=ActionExecutionStatus.PENDING,
    )
    store.action_executions[action_id] = exec_rec

    # 1. Claim dispatch
    token1 = "lease_tok_1"
    claimed, rec, reason = store.claim_action_dispatch(action_id, lease_token=token1, lease_seconds=30)
    assert claimed is True
    assert rec.dispatch_status == DispatchStatus.DISPATCHING
    version1 = rec.dispatch_version

    # Concurrent claim while lease active is rejected
    claimed2, _, reason2 = store.claim_action_dispatch(action_id, lease_token="token_2", lease_seconds=30)
    assert claimed2 is False
    assert reason2 == "ACTIVE_DISPATCH_HELD"

    # 2. Stale / wrong token cannot record success
    ok = store.record_dispatch_success(action_id, lease_token="wrong_token", expected_version=version1, task_name="task-1")
    assert ok is False

    # Valid token records success
    ok_succ = store.record_dispatch_success(action_id, lease_token=token1, expected_version=version1, task_name="task-1")
    assert ok_succ is True
    assert store.get_action_execution(action_id).dispatch_status == DispatchStatus.DISPATCHED

    # 3. AlreadyExists in CloudTasksActionRunner is treated as idempotent success
    runner = CloudTasksActionRunner()
    class FakeCloudTasksClient:
        def queue_path(self, *args):
            return "projects/p/locations/l/queues/q"
        def create_task(self, parent, task):
            raise Exception("409 ALREADY_EXISTS: Task already exists")

    runner.client = FakeCloudTasksClient()
    proposal = ActionProposal(
        action_id="act_d18_task",
        space_id="sp_d18",
        project_tag="general",
        user_id="u_d18",
        title="Test Task",
        description="desc",
        sources=[],
        action_type="create_call_sheet",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    task_name = runner.dispatch_action(proposal, "u_d18", dispatch_generation=1)
    assert "action-" in task_name


# -----------------------------------------------------------------------------
# Gate D19: Artifact Cleanup Sweeper Lifecycle & Reclaimer Takeover
# -----------------------------------------------------------------------------


def test_gate_d19_artifact_cleanup_sweeper_lifecycle():
    space_id = "sp_d19_sweep"
    art_id = "art_d19_orphaned"
    filename = "orphaned_test.pdf"

    # Save physical blob
    store.save_artifact_blob(space_id, art_id, filename, b"dummy orphaned data")
    assert store.get_artifact_blob(space_id, art_id, filename) is not None

    # Enqueue cleanup
    store.enqueue_artifact_cleanup(space_id, art_id, filename, reason="TEST_ORPHAN")

    # Run sweeper
    swept = DeliverableExecutionService.sweep_artifact_cleanups(batch_size=10)
    assert swept == 1

    # Physical blob must be wiped out
    assert store.get_artifact_blob(space_id, art_id, filename) is None

    # Re-sweeping empty queue does nothing
    swept_empty = DeliverableExecutionService.sweep_artifact_cleanups(batch_size=10)
    assert swept_empty == 0


# -----------------------------------------------------------------------------
# Gate D20: Maintenance Reconciliation Endpoint Error Isolation & Metrics
# -----------------------------------------------------------------------------


def test_gate_d20_maintenance_reconcile_endpoint(monkeypatch):
    # 1. Invalid maintenance secret fails with 403
    res_bad = client.post(
        "/v1/maintenance/reconcile-actions-and-cleanup",
        headers={"X-StudioTower-Maintenance-Secret": "invalid_secret_key"},
    )
    assert res_bad.status_code == 403

    # 2. Valid maintenance secret executes successfully
    res_ok = client.post(
        "/v1/maintenance/reconcile-actions-and-cleanup",
        headers={"X-StudioTower-Maintenance-Secret": settings.STUDIO_TOWER_MAINTENANCE_SECRET},
    )
    assert res_ok.status_code == 200
    data = res_ok.json()
    assert data["status"] == "success"
    assert "dispatches_reclaimed" in data["metrics"]
    assert "executions_reclaimed" in data["metrics"]
    assert "cleanups_swept" in data["metrics"]

    # 3. Error isolation: mock an error in cycle 1
    def faulty_reclaim_dispatches(*args, **kwargs):
        raise RuntimeError("Network glitch in dispatches")

    monkeypatch.setattr(DeliverableExecutionService, "reclaim_pending_dispatches", faulty_reclaim_dispatches)
    res_partial = client.post(
        "/v1/maintenance/reconcile-actions-and-cleanup",
        headers={"X-StudioTower-Maintenance-Secret": settings.STUDIO_TOWER_MAINTENANCE_SECRET},
    )
    assert res_partial.status_code == 200
    data_partial = res_partial.json()
    assert data_partial["status"] == "partial_success"
    assert len(data_partial["errors"]) > 0


# -----------------------------------------------------------------------------
# Gate D21: Long-Running Execution with Thread-Safe Heartbeat Tracker
# -----------------------------------------------------------------------------


def test_gate_d21_execution_heartbeat_tracker_version_tracking():
    action_id = "act_d21_hb"
    run_id = "run_d21_hb"
    space_id = "sp_d21"
    user_id = "u_d21"

    exec_rec = ActionExecutionRecord(
        action_id=action_id,
        run_id=run_id,
        space_id=space_id,
        project_tag="general",
        user_id=user_id,
        status=ActionExecutionStatus.RUNNING,
        lease_owner="worker_d21",
        lease_token="token_d21",
        lease_until=datetime.now(UTC) + timedelta(seconds=60),
        state_version=1,
    )
    store.action_executions[action_id] = exec_rec

    from app.services.deliverable_service import ExecutionHeartbeatTracker
    tracker = ExecutionHeartbeatTracker(
        action_id=action_id,
        worker_id="worker_d21",
        lease_token="token_d21",
        initial_version=1,
    )
    tracker.start(interval_seconds=0.1, extension_seconds=120)

    # Wait briefly for multiple heartbeat renewals
    import time
    time.sleep(0.35)

    healthy, latest_ver, err = tracker.stop_and_verify()
    assert healthy is True
    assert latest_ver > 1  # Successfully tracked state_version increments from background thread


# -----------------------------------------------------------------------------
# Gate D22: File Center, File Download & QA Retrieval Staging Gate Protection
# -----------------------------------------------------------------------------


def test_gate_d22_file_center_and_download_approval_guard():
    client = TestClient(app)
    space_id = "sp_d22_guard"
    coord_id = "u_d22_coord"
    member_id = "u_d22_member"

    # 1. Setup users and space
    u_coord = User(uid=coord_id, email="coord@d22.com", display_name="Coordinator D22")
    u_member = User(uid=member_id, email="member@d22.com", display_name="Member D22")
    store.save_user(u_coord)
    store.save_user(u_member)

    sp = Space(space_id=space_id, name="Staging Gate Space", created_by=coord_id)
    store.create_space(sp, creator_uid=coord_id)
    store.add_member(space_id, coord_id, MembershipRole.COORDINATOR)
    store.add_member(space_id, member_id, MembershipRole.MEMBER)

    # 2. Upload staging deliverable file with publication_status="pending_approval"
    from app.services.file_service import FileService
    from app.models.file_record import FileSourceType
    content = b"scene_number,slugline\n1,INT. LAB\n"
    staging_file = FileService.upload_file(
        space_id=space_id,
        filename="Staging_Schedule.csv",
        content=content,
        content_type="text/csv",
        user=u_coord,
        source_type=FileSourceType.PDX_ARTIFACT,
        publication_status="pending_approval",
    )
    assert staging_file.publication_status == "pending_approval"

    # Also register staging artifact descriptor
    art_desc = ArtifactDescriptor(
        artifact_id=staging_file.file_id,
        space_id=space_id,
        run_id="run_d22",
        filename=staging_file.filename,
        media_type="text/csv",
        size_bytes=len(content),
        sha256=staging_file.sha256,
        storage_path=staging_file.storage_path,
        download_endpoint=f"/v1/spaces/{space_id}/artifacts/{staging_file.file_id}/download",
        visibility="pending_approval",
    )
    store.save_artifact_record(art_desc)

    # 3. Non-coordinator queries File Center (/v1/spaces/{space_id}/files)
    token_member = f"dev:{member_id}:member@d22.com"
    res_list_mem = client.get(f"/v1/spaces/{space_id}/files", headers={"Authorization": f"Bearer {token_member}"})
    assert res_list_mem.status_code == 200
    listed_files_mem = res_list_mem.json()
    assert all(f["file_id"] != staging_file.file_id for f in listed_files_mem)

    # 4. Coordinator queries File Center: sees pending_approval file
    token_coord = f"dev:{coord_id}:coord@d22.com"
    res_list_coord = client.get(f"/v1/spaces/{space_id}/files", headers={"Authorization": f"Bearer {token_coord}"})
    assert res_list_coord.status_code == 200
    listed_files_coord = res_list_coord.json()
    assert any(f["file_id"] == staging_file.file_id for f in listed_files_coord)

    # 5. Non-coordinator attempts direct download: rejected with 403 ARTIFACT_AWAITING_APPROVAL
    res_dl_mem = client.get(
        f"/v1/spaces/{space_id}/files/{staging_file.file_id}/download",
        headers={"Authorization": f"Bearer {token_member}"},
    )
    assert res_dl_mem.status_code == 403
    assert "ARTIFACT_AWAITING_APPROVAL" in res_dl_mem.json()["detail"]

    # 6. Coordinator attempts download: permitted
    res_dl_coord = client.get(
        f"/v1/spaces/{space_id}/files/{staging_file.file_id}/download",
        headers={"Authorization": f"Bearer {token_coord}"},
    )
    assert res_dl_coord.status_code == 200
    assert res_dl_coord.content == content

    # 7. Document QA retrieval skips pending deliverable file
    from app.services.retrieval_service import RetrievalService
    cands, _ = RetrievalService.retrieve_for_qa(space_id, "schedule", [staging_file.file_id])
    assert len(cands) == 0

    # 8. Atomic Approval Transaction publishes both ArtifactDescriptor AND FileRecord
    gate = ApprovalGate(
        gate_id="gate_d22",
        title="Gate D22",
        description="Gate D22 approval",
        risk_level="critical",
        status="approving",
        decision_lease_token="lease_d22_token",
    )
    run = Run(
        run_id="run_d22",
        space_id=space_id,
        status=RunStatus.RUNNING,
        approval_gate=gate,
        output_artifact_ids=[staging_file.file_id],
        created_by=coord_id,
    )
    store.save_run(run)

    approved_run, _ = store.approve_gate_and_publish_artifacts_atomic(
        space_id=space_id,
        run_id="run_d22",
        approver_uid=coord_id,
        output_artifact_ids=[staging_file.file_id],
        decision_lease_token="lease_d22_token",
    )
    assert approved_run.status == RunStatus.COMPLETED

    # Verify FileRecord is now published
    updated_file = store.get_file(staging_file.file_id)
    assert updated_file.publication_status == "published"
    updated_art = store.get_artifact(space_id, staging_file.file_id)
    assert updated_art.visibility == "published"

    # Standard member can now download successfully
    res_dl_mem_after = client.get(
        f"/v1/spaces/{space_id}/files/{staging_file.file_id}/download",
        headers={"Authorization": f"Bearer {token_member}"},
    )
    assert res_dl_mem_after.status_code == 200


# -----------------------------------------------------------------------------
# Gate D23: Dispatch Generation Stability on Expired DISPATCHING Recovery
# -----------------------------------------------------------------------------


def test_gate_d23_dispatch_generation_stability_and_cas_reclaim():
    space_id = "sp_d23"
    action_id = "act_d23_reclaim"
    user_id = "u_d23"

    now = datetime.now(UTC)
    exec_rec = ActionExecutionRecord(
        action_id=action_id,
        run_id="run_d23",
        space_id=space_id,
        project_tag="general",
        user_id=user_id,
        status=ActionExecutionStatus.PENDING,
        dispatch_status=DispatchStatus.DISPATCHING,
        dispatch_lease_token="token_worker_crashed",
        dispatch_lease_until=now - timedelta(seconds=10),  # Expired lease
        dispatch_generation=2,
        task_name=f"action-{action_id[:16]}-d2",
        proposal_snapshot=ActionProposal(
            action_id=action_id,
            space_id=space_id,
            action_type="produce_schedule",
            title="Dispatch Generation Test",
            description="Testing dispatch generation stability",
            prompt="Test",
            project_tag="general",
            user_id=user_id,
            parameters={},
            source_file_ids=[],
            sources=[],
            risk_level="low",
            requires_approval=False,
            created_at=now,
            expires_at=now + timedelta(hours=1),
        ),
    )
    store.action_executions[action_id] = exec_rec

    # Reclaiming an expired DISPATCHING lease MUST retain dispatch_generation=2 to allow AlreadyExists absorption!
    claimed, rec, reason = store.claim_action_dispatch(action_id, lease_token="reclaim_tok", lease_seconds=60)
    assert claimed is True
    assert rec.dispatch_generation == 2  # NOT incremented to 3!
    assert rec.dispatch_status == DispatchStatus.DISPATCHING

    # Now verify that claim from PENDING DOES increment dispatch_generation
    rec.dispatch_status = DispatchStatus.PENDING
    rec.dispatch_lease_until = None
    store.action_executions[action_id] = rec
    claimed_pending, rec_pending, _ = store.claim_action_dispatch(action_id, lease_token="new_tok", lease_seconds=60)
    assert claimed_pending is True
    assert rec_pending.dispatch_generation == 3

    # Verify reclaim_pending_dispatches uses CAS claim:
    rec_pending.dispatch_status = DispatchStatus.DISPATCHING
    rec_pending.dispatch_lease_until = now - timedelta(seconds=5)
    store.action_executions[action_id] = rec_pending

    recovered = DeliverableExecutionService.reclaim_pending_dispatches(space_id)
    assert recovered == 1
    final_exec = store.get_action_execution(action_id)
    assert final_exec.dispatch_status == DispatchStatus.DISPATCHED


# -----------------------------------------------------------------------------
# Gate D24: Heartbeat Join Timeout Fail-Closed & Latest Version Fencing
# -----------------------------------------------------------------------------


def test_gate_d24_heartbeat_join_timeout_fail_closed():
    import threading
    from app.services.deliverable_service import ExecutionHeartbeatTracker

    tracker = ExecutionHeartbeatTracker(
        action_id="act_d24",
        worker_id="worker_d24",
        lease_token="tok_d24",
        initial_version=1,
    )

    # Simulate an uncooperative background thread that ignores the stop event
    hung_stop_event = threading.Event()
    def _hung_thread():
        hung_stop_event.wait(timeout=5.0)  # Sleeps past 0.05s join timeout

    t = threading.Thread(target=_hung_thread, daemon=True)
    tracker._thread = t
    t.start()

    # stop_and_verify with small timeout MUST detect thread is alive and return fail-closed
    healthy, ver, failure_code = tracker.stop_and_verify(timeout=0.05)
    hung_stop_event.set()  # clean up thread

    assert healthy is False
    assert failure_code == "HEARTBEAT_JOIN_TIMEOUT"


# -----------------------------------------------------------------------------
# Gate D25: Sweeper Verified Deletion & Cleanup Fencing
# -----------------------------------------------------------------------------


def test_gate_d25_sweeper_verified_deletion_and_fencing(monkeypatch):
    space_id = "sp_d25"
    art_id = "art_d25_sweeper"
    filename = "test_blob.csv"

    store.enqueue_artifact_cleanup(space_id, art_id, filename, reason="TEST_FAILED")
    job_id = f"clean_art_{art_id}"

    # 1. When storage.delete_artifact_blob returns False, sweeper must NOT complete job!
    monkeypatch.setattr(store, "delete_artifact_blob", lambda *args, **kwargs: False)
    swept_fail = DeliverableExecutionService.sweep_artifact_cleanups()
    assert swept_fail == 0

    job_after_fail = store.artifact_cleanups.get(job_id)
    assert job_after_fail["status"] == "pending"
    assert job_after_fail["retries"] == 1
    assert job_after_fail["last_error"] == "BLOB_DELETION_RETURNED_FALSE"

    # 2. When storage.delete_artifact_blob returns True, sweeper completes job with fencing!
    monkeypatch.setattr(store, "delete_artifact_blob", lambda *args, **kwargs: True)
    job_after_fail["next_retry_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    store.artifact_cleanups[job_id] = job_after_fail

    swept_ok = DeliverableExecutionService.sweep_artifact_cleanups()
    assert swept_ok == 1
    job_after_ok = store.artifact_cleanups.get(job_id)
    assert job_after_ok["status"] == "completed"


# -----------------------------------------------------------------------------
# Gate D26: Dynamic Unit Assignments & Hardcoded Artifact Deprecation
# -----------------------------------------------------------------------------


def test_gate_d26_dynamic_unit_assignments_and_pdx_staging():
    user_id = "u_d26"
    space_id = "sp_d26"
    user = User(uid=user_id, email="d26@pdx.com", display_name="PDX User")
    store.save_user(user)
    store.create_space(Space(space_id=space_id, name="PDX Space", created_by=user_id), creator_uid=user_id)

    run = Run(
        run_id="run_d26_test",
        space_id=space_id,
        status=RunStatus.RUNNING,
        approval_gate=ApprovalGate(
            gate_id="gate_d26",
            title="Gate D26",
            description="Gate D26",
            risk_level="critical",
            status="approving",
            decision_lease_token="lease_d26",
        ),
        created_by=user_id,
    )
    store.save_run(run)

    # Script breakdown data with dynamic scenes and characters
    breakdown_data = {
        "scenes": [
            {
                "scene_number": 101,
                "slugline": "EXT. NEO TOKYO HIGHWAY - DUSK",
                "resources": {
                    "locations": ["EXT. NEO TOKYO HIGHWAY - DUSK"],
                    "cast": ["KENJI", "AKIRA"],
                    "props": ["Motorcycle"],
                    "stunt_level": "High",
                    "vfx_tier": "Medium",
                },
            },
            {
                "scene_number": 102,
                "slugline": "INT. SUBWAY STATION - NIGHT",
                "resources": {
                    "locations": ["INT. SUBWAY STATION - NIGHT"],
                    "cast": ["REI"],
                    "props": ["Passcard"],
                    "stunt_level": "None",
                    "vfx_tier": "Low",
                },
            },
        ],
        "detected_conflicts": [],
    }
    run.scene_breakdown = breakdown_data
    store.save_run(run)

    from app.integrations.pdx_engine import PDXEngine
    run_res = PDXEngine.execute_and_bundle(space_id, run, user)
    artifacts = run_res.output_artifact_ids

    # Verify all created artifacts have publication_status=pending_approval
    for art_id in artifacts:
        f = store.get_file(art_id)
        assert f.publication_status == "pending_approval"
        blob = store.get_artifact_blob(space_id, art_id, f.filename)
        assert blob is not None

        # Verify NO hardcoded template characters or units exist!
        text_content = blob.decode("utf-8", errors="ignore")
        assert "Captain Hadi" not in text_content
        assert "Dr. Rizal" not in text_content
        assert "Disaster Perimeter" not in text_content
        assert "Hangar & Stunt" not in text_content

        # For the Unit Handoff Manifest, verify dynamic unit assignments:
        if "Unit_Handoff_Manifest" in f.filename:
            import json
            manifest = json.loads(text_content)
            assignments = manifest["unit_assignments"]
            assert len(assignments) == 2
            assert assignments[0]["lead"] == "KENJI"
            assert "EXT. NEO TOKYO HIGHWAY - DUSK" in assignments[0]["unit"]
            assert assignments[1]["lead"] == "REI"
            assert "INT. SUBWAY STATION - NIGHT" in assignments[1]["unit"]


# -----------------------------------------------------------------------------
# Gate D27: Enqueue Success with Post-Dispatch DB Exception, DISPATCH_CONFIRMATION_PENDING & AlreadyExists Absorption
# -----------------------------------------------------------------------------


def test_gate_d27_dispatch_confirmation_pending_and_already_exists_absorption():
    space_id = "sp_d27"
    action_id = "act_d27_pending_conf"
    user_id = "u_d27"
    now = datetime.now(UTC)

    # 1. Setup execution in DISPATCHING state with dispatch_generation=2
    task_name_gen2 = f"action-{action_id[:16]}-d2"
    exec_rec = ActionExecutionRecord(
        action_id=action_id,
        run_id="run_d27",
        space_id=space_id,
        project_tag="general",
        user_id=user_id,
        status=ActionExecutionStatus.PENDING,
        dispatch_status=DispatchStatus.DISPATCHING,
        dispatch_lease_token="lease_d27_init",
        dispatch_lease_until=now + timedelta(seconds=60),
        dispatch_generation=2,
        dispatch_version=1,
        task_name=task_name_gen2,
        proposal_snapshot=ActionProposal(
            action_id=action_id,
            space_id=space_id,
            action_type="produce_schedule",
            title="Gate D27 Test",
            description="Testing post-enqueue failure",
            prompt="Test",
            project_tag="general",
            user_id=user_id,
            parameters={},
            source_file_ids=[],
            sources=[],
            risk_level="low",
            requires_approval=False,
            created_at=now,
            expires_at=now + timedelta(hours=1),
        ),
    )
    store.action_executions[action_id] = exec_rec

    # 2. Simulate runner.dispatch_action succeeded (Cloud Task created), but DB record_dispatch_success threw exception.
    marked = store.mark_dispatch_confirmation_uncertain(
        action_id=action_id,
        lease_token="lease_d27_init",
        expected_version=1,
        task_name=task_name_gen2,
        error="TRANSIENT_FIRESTORE_TIMEOUT",
    )
    assert marked is True

    # Verify execution entered DISPATCH_CONFIRMATION_PENDING (NOT regular FAILED!)
    curr_rec = store.get_action_execution(action_id)
    assert curr_rec.dispatch_status == DispatchStatus.DISPATCH_CONFIRMATION_PENDING
    assert curr_rec.task_name == task_name_gen2
    assert curr_rec.dispatch_generation == 2

    # 3. Simulate recovery scheduler claiming this action:
    # Must preserve dispatch_generation=2 so retry creates/checks the exact same task name!
    claimed, claimed_rec, reason = store.claim_action_dispatch(action_id, lease_token="reclaim_tok", lease_seconds=60)
    assert claimed is True
    assert claimed_rec.dispatch_generation == 2  # NOT incremented to 3!
    assert claimed_rec.dispatch_status == DispatchStatus.DISPATCHING

    # 4. Calling dispatch_action with same task name hits AlreadyExists, then record_dispatch_success commits
    success = store.record_dispatch_success(
        action_id=action_id,
        lease_token="reclaim_tok",
        expected_version=claimed_rec.dispatch_version,
        task_name=task_name_gen2,
    )
    assert success is True
    final_rec = store.get_action_execution(action_id)
    assert final_rec.dispatch_status == DispatchStatus.DISPATCHED
    assert final_rec.task_name == task_name_gen2


# -----------------------------------------------------------------------------
# Gate D28: Reconcile-First Approval Failure Handling (Rollback vs Server-Committed)
# -----------------------------------------------------------------------------


def test_gate_d28a_approval_transaction_aborted_reconcile_cleans_up():
    from app.services.file_service import FileService
    from app.models.file_record import FileSourceType
    from app.models.space import Space
    space_id = "sp_d28a"
    run_id = "run_d28a"
    user_id = "u_d28a"

    store.create_space(Space(space_id=space_id, name="Space D28a", created_by=user_id, members=[user_id]), creator_uid=user_id)

    # 1. Create staging files & uncommitted Run
    f_schedule = FileService.upload_file(
        space_id=space_id,
        filename="Shoot_Schedule.csv",
        content=b"scene,1\n",
        content_type="text/csv",
        user=User(uid=user_id, email="d28a@pdx.com"),
        source_type=FileSourceType.PDX_ARTIFACT,
        run_id=run_id,
        publication_status="pending_approval",
        file_id=f"file_{run_id}_schedule",
    )
    store.save_artifact_blob(space_id, f_schedule.file_id, f_schedule.filename, b"scene,1\n")
    art_desc = ArtifactDescriptor(
        artifact_id=f_schedule.file_id,
        space_id=space_id,
        run_id=run_id,
        filename=f_schedule.filename,
        media_type="text/csv",
        size_bytes=len(b"scene,1\n"),
        sha256=f_schedule.sha256,
        storage_path=f_schedule.storage_path,
        download_endpoint=f"/v1/spaces/{space_id}/artifacts/{f_schedule.file_id}/download",
        visibility="pending_approval",
    )
    store.save_artifact_record(art_desc)

    run = Run(
        run_id=run_id,
        space_id=space_id,
        status=RunStatus.RUNNING,
        approval_gate=ApprovalGate(
            gate_id="gate_d28a",
            title="Gate D28a",
            description="Gate D28a",
            risk_level="critical",
            status="approving",
            decision_lease_token=None,  # Expired / aborted
        ),
        output_artifact_ids=[f_schedule.file_id],
        created_by=user_id,
    )
    store.save_run(run)

    # 2. Reconcile detects uncommitted artifacts & expired lease -> marks Run FAILED & enqueues cleanup
    status_res, reconciled_run = DeliverableExecutionService.reconcile_approval_commit(space_id, run_id)
    assert status_res == "ABORTED_CLEANUP_ENQUEUED"
    assert reconciled_run.status == RunStatus.FAILED

    # 3. Sweeper runs and deletes the staging blob
    swept = DeliverableExecutionService.sweep_artifact_cleanups()
    assert swept == 1
    blob = store.get_artifact_blob(space_id, f_schedule.file_id, f_schedule.filename)
    assert blob is None


def test_gate_d28b_approval_committed_server_side_reconcile_preserves_published():
    from app.services.file_service import FileService
    from app.models.file_record import FileSourceType
    from app.models.space import Space
    space_id = "sp_d28b"
    run_id = "run_d28b"
    user_id = "u_d28b"

    store.create_space(Space(space_id=space_id, name="Space D28b", created_by=user_id, members=[user_id]), creator_uid=user_id)

    # 1. Staging files exist and WERE committed to published on server, but client received network error
    f_schedule = FileService.upload_file(
        space_id=space_id,
        filename="Shoot_Schedule.csv",
        content=b"scene,1\n",
        content_type="text/csv",
        user=User(uid=user_id, email="d28b@pdx.com"),
        source_type=FileSourceType.PDX_ARTIFACT,
        run_id=run_id,
        publication_status="published",  # Committed!
        file_id=f"file_{run_id}_schedule",
    )
    store.save_artifact_blob(space_id, f_schedule.file_id, f_schedule.filename, b"scene,1\n")
    art_desc = ArtifactDescriptor(
        artifact_id=f_schedule.file_id,
        space_id=space_id,
        run_id=run_id,
        filename=f_schedule.filename,
        media_type="text/csv",
        size_bytes=len(b"scene,1\n"),
        sha256=f_schedule.sha256,
        storage_path=f_schedule.storage_path,
        download_endpoint=f"/v1/spaces/{space_id}/artifacts/{f_schedule.file_id}/download",
        visibility="published",  # Committed!
    )
    store.save_artifact_record(art_desc)

    run = Run(
        run_id=run_id,
        space_id=space_id,
        status=RunStatus.RUNNING,  # Client was still RUNNING due to network exception
        approval_gate=ApprovalGate(
            gate_id="gate_d28b",
            title="Gate D28b",
            description="Gate D28b",
            risk_level="critical",
            status="approving",
            decision_lease_token="lease_d28b",
        ),
        output_artifact_ids=[f_schedule.file_id],
        created_by=user_id,
    )
    store.save_run(run)

    # 2. Reconcile evaluates: sees all artifacts are published -> confirms Run COMPLETED, does not delete
    status_res, reconciled_run = DeliverableExecutionService.reconcile_approval_commit(space_id, run_id)
    assert status_res == "COMMITTED_PUBLISHED"
    assert reconciled_run.status == RunStatus.COMPLETED

    # 3. Simulate stray cleanup job in queue for this artifact
    job_id = store.enqueue_artifact_cleanup(space_id, f_schedule.file_id, f_schedule.filename, reason="STRAY")
    swept = DeliverableExecutionService.sweep_artifact_cleanups()
    # Sweeper aborts deletion because artifact is published!
    assert swept == 0
    blob = store.get_artifact_blob(space_id, f_schedule.file_id, f_schedule.filename)
    assert blob == b"scene,1\n"  # Preserved!
    job = store.artifact_cleanups.get(job_id)
    assert job["status"] == "cancelled"


# -----------------------------------------------------------------------------
# Gate D29: In-Flight Approval in RUNNING Rejects Concurrent Reject without Side-Effects
# -----------------------------------------------------------------------------


def test_gate_d29_in_flight_approval_blocks_concurrent_reject():
    space_id = "sp_d29"
    run_id = "run_d29"
    action_id = "act_d29"
    coord_id = "u_d29_coord"
    reviewer_id = "u_d29_reviewer"

    # 1. Run is currently in RUNNING state with active approval lease
    gate = ApprovalGate(
        gate_id="gate_d29",
        title="Gate D29",
        description="Gate D29 Approval",
        risk_level="critical",
        status="approving",
        decision_lease_token="lease_d29_active",
    )
    run = Run(
        run_id=run_id,
        space_id=space_id,
        action_id=action_id,
        status=RunStatus.RUNNING,
        approval_gate=gate,
        created_by=coord_id,
    )
    store.save_run(run)

    exec_rec = ActionExecutionRecord(
        action_id=action_id,
        run_id=run_id,
        space_id=space_id,
        user_id=coord_id,
        status=ActionExecutionStatus.RUNNING,
        state_version=1,
    )
    store.action_executions[action_id] = exec_rec

    # 2. Concurrent Reject request arrives: MUST be rejected with StorageConflictError
    with pytest.raises(StorageConflictError) as exc_info:
        store.reject_gate_and_fail_artifacts_atomic(
            space_id=space_id,
            run_id=run_id,
            approver_uid=reviewer_id,
            rejection_reason="Reviewer attempted concurrent reject",
        )
    assert "ACTIVE_DECISION_HELD" in str(exc_info.value)

    # 3. Verify Run, Gate, and ActionExecution were NOT modified
    curr_run = store.get_run(run_id)
    assert curr_run.status == RunStatus.RUNNING
    assert curr_run.approval_gate.status == "approving"
    curr_exec = store.get_action_execution(action_id)
    assert curr_exec.status == ActionExecutionStatus.RUNNING


# -----------------------------------------------------------------------------
# Gate D30: Storage Fail-Closed on None Leases & Sweeper Completion Metrics
# -----------------------------------------------------------------------------


def test_gate_d30_storage_fail_closed_and_sweeper_completion_metrics():
    space_id = "sp_d30"
    action_id = "act_d30"
    now = datetime.now(UTC)

    # 1. record_dispatch_success / failure with dispatch_lease_until=None MUST return False
    rec = ActionExecutionRecord(
        action_id=action_id,
        run_id="run_d30",
        space_id=space_id,
        user_id="u_d30",
        status=ActionExecutionStatus.PENDING,
        dispatch_status=DispatchStatus.DISPATCHING,
        dispatch_lease_token="tok_d30",
        dispatch_lease_until=None,  # No lease!
        dispatch_generation=1,
        dispatch_version=1,
    )
    store.action_executions[action_id] = rec

    assert store.record_dispatch_success(action_id, lease_token="tok_d30", expected_version=1, task_name="t1") is False
    assert store.record_dispatch_failure(action_id, lease_token="tok_d30", expected_version=1, error="err") is False

    # 2. heartbeat_action_execution with lease_until=None MUST return False
    rec.status = ActionExecutionStatus.RUNNING
    rec.lease_owner = "w_d30"
    rec.lease_token = "tok_d30"
    rec.lease_until = None
    store.action_executions[action_id] = rec
    hb_ok, _ = store.heartbeat_action_execution(action_id, worker_id="w_d30", lease_token="tok_d30")
    assert hb_ok is False

    # 3. complete_artifact_cleanup with missing owner/token/lease_until MUST return False
    job_id = store.enqueue_artifact_cleanup(space_id, "art_d30", "test.bin", reason="TEST")
    assert store.complete_artifact_cleanup(job_id, worker_id="w1", lease_token="t1") is False

    # 4. Sweeper does NOT increment swept_count when complete_artifact_cleanup returns False
    claimed = store.claim_artifact_cleanup_job(job_id, worker_id="sweeper_d30", lease_seconds=1)
    # Expire the lease manually
    store.artifact_cleanups[job_id]["lease_until"] = (now - timedelta(seconds=10)).isoformat()
    # Now run sweeper: complete_artifact_cleanup returns False due to expired lease
    swept = DeliverableExecutionService.sweep_artifact_cleanups()
    assert swept == 0


# -----------------------------------------------------------------------------
# Gate D31: PDX Deterministic Artifact Content, Hash, and Idempotency
# -----------------------------------------------------------------------------


def test_gate_d31_pdx_deterministic_content_and_hash_idempotency():
    from app.integrations.pdx_engine import PDXEngine
    space_id = "sp_d31"
    run_id = "run_d31"
    user_id = "u_d31"
    user = User(uid=user_id, email="d31@pdx.com")
    store.save_user(user)
    store.create_space(Space(space_id=space_id, name="Space D31", created_by=user_id), creator_uid=user_id)

    run = Run(
        run_id=run_id,
        space_id=space_id,
        status=RunStatus.RUNNING,
        created_at=datetime(2026, 3, 1, 10, 0, 0, tzinfo=UTC),
        approval_gate=ApprovalGate(
            gate_id="gate_d31",
            title="Gate D31",
            description="Gate D31 Description",
            risk_level="high",
            status="approving",
            decision_lease_token="lease_d31",
        ),
        created_by=user_id,
    )
    store.save_run(run)

    # First execution
    run_1 = PDXEngine.execute_and_bundle(space_id, run, user)
    artifacts_1 = [store.get_artifact(space_id, aid) for aid in run_1.output_artifact_ids]
    blobs_1 = {aid: store.get_artifact_blob(space_id, aid, a.filename) for aid, a in zip(run_1.output_artifact_ids, artifacts_1)}

    # Second execution for identical Run (retry)
    run_2 = PDXEngine.execute_and_bundle(space_id, run, user)
    artifacts_2 = [store.get_artifact(space_id, aid) for aid in run_2.output_artifact_ids]
    blobs_2 = {aid: store.get_artifact_blob(space_id, aid, a.filename) for aid, a in zip(run_2.output_artifact_ids, artifacts_2)}

    # Verify 100% byte, sha256, and ID equivalence
    assert run_1.output_artifact_ids == run_2.output_artifact_ids
    assert run_1.manifest_file_id == run_2.manifest_file_id
    assert run_1.manifest_file_id is not None

    all_ids = run_1.output_artifact_ids + [run_1.manifest_file_id]
    blobs_1[run_1.manifest_file_id] = store.get_artifact_blob(space_id, run_1.manifest_file_id, "RunManifest_31.json")
    blobs_2[run_2.manifest_file_id] = store.get_artifact_blob(space_id, run_2.manifest_file_id, "RunManifest_31.json")

    for aid in all_ids:
        art1 = store.get_artifact(space_id, aid)
        art2 = store.get_artifact(space_id, aid)
        assert art1.sha256 == art2.sha256
        assert blobs_1[aid] == blobs_2[aid]


# -----------------------------------------------------------------------------
# Gate D32: Firestore Reconcile Stateful Persistence Across Store Re-Instantiation
# -----------------------------------------------------------------------------


def test_gate_d32_firestore_reconcile_stateful_persistence():
    from app.services.storage import FirestoreStore
    import sys
    from unittest.mock import MagicMock, patch

    space_id = "sp_d32"
    run_id = "run_d32"
    user_id = "u_d32"
    action_id = "act_d32"

    db_store = {}

    class StatefulDocSnap:
        def __init__(self, doc_id, col_dict):
            self.id = doc_id
            self._dict = col_dict
        @property
        def exists(self):
            return self.id in self._dict
        def to_dict(self):
            return dict(self._dict[self.id]) if self.exists else None

    class StatefulDocRef:
        def __init__(self, doc_id, col_dict):
            self.id = doc_id
            self._dict = col_dict
            self.reference = self
        def get(self, transaction=None):
            return StatefulDocSnap(self.id, self._dict)
        def set(self, data):
            self._dict[self.id] = dict(data)
        def update(self, data):
            self._dict[self.id].update(data)

    class StatefulFakeClient:
        def __init__(self, backing_db):
            self._db = backing_db
        def collection(self, name):
            col_dict = self._db.setdefault(name, {})
            q = MagicMock()
            q.document = lambda doc_id: StatefulDocRef(doc_id, col_dict)
            return q
        def transaction(self):
            t = MagicMock()
            t.set = lambda ref, data: ref.set(data)
            t.update = lambda ref, data: ref.update(data)
            return t

    mock_fs = MagicMock()
    mock_fs.transactional = lambda fn: (lambda tx, *args, **kwargs: fn(tx, *args, **kwargs))

    with patch.dict(sys.modules, {"google.cloud.firestore": mock_fs}):
        client_1 = StatefulFakeClient(db_store)
        fs_store_1 = FirestoreStore(project_id="test-pdx", client=client_1)

        run = Run(
            run_id=run_id,
            space_id=space_id,
            action_id=action_id,
            status=RunStatus.RUNNING,
            state_version=1,
            approval_gate=ApprovalGate(
                gate_id="gate_d32",
                title="Gate D32",
                description="Gate D32",
                risk_level="high",
                status="approving",
            ),
            created_by=user_id,
        )
        fs_store_1.save_run(run)

        exec_rec = ActionExecutionRecord(
            action_id=action_id,
            run_id=run_id,
            space_id=space_id,
            user_id=user_id,
            status=ActionExecutionStatus.RUNNING,
            state_version=1,
        )
        client_1.collection("action_executions").document(action_id).set(exec_rec.model_dump(mode="json"))

        # Reconcile to COMPLETED
        ok, updated = fs_store_1.reconcile_run_approval_atomic(
            space_id=space_id,
            run_id=run_id,
            target_status=RunStatus.COMPLETED,
            approval_commit_status="committed",
            gate_status="approved",
        )
        assert ok is True
        assert updated.status == RunStatus.COMPLETED
        assert updated.state_version == 2

        # Re-instantiate a BRAND NEW FirestoreStore with the same backing store
        client_2 = StatefulFakeClient(db_store)
        fs_store_2 = FirestoreStore(project_id="test-pdx", client=client_2)

        persisted_run = fs_store_2.get_run(run_id)
        assert persisted_run is not None
        assert persisted_run.status == RunStatus.COMPLETED
        assert persisted_run.approval_commit_status == "committed"
        assert persisted_run.approval_gate.status == "approved"
        assert persisted_run.state_version == 2

        # Linked ActionExecution also persisted as COMPLETED
        act_snap = client_2.collection("action_executions").document(action_id).get()
        assert act_snap.to_dict()["status"] == ActionExecutionStatus.COMPLETED.value

        # Outbox item staged
        outbox_docs = db_store.get("activity_outbox", {})
        assert any(f"run.reconciled.completed:{run_id}" in k for k in outbox_docs.keys())


# -----------------------------------------------------------------------------
# Gate D33: Reconcile Abort Atomically Enqueues Cleanup Intents
# -----------------------------------------------------------------------------


def test_gate_d33_reconcile_abort_atomically_enqueues_cleanup_intents():
    space_id = "sp_d33"
    run_id = "run_d33"
    user_id = "u_d33"
    art_id = "art_d33_staging"

    run = Run(
        run_id=run_id,
        space_id=space_id,
        status=RunStatus.RUNNING,
        state_version=3,
        approval_gate=ApprovalGate(
            gate_id="gate_d33",
            title="Gate D33",
            description="Gate D33",
            risk_level="high",
            status="approving",
        ),
        output_artifact_ids=[art_id],
        created_by=user_id,
    )
    store.save_run(run)

    cleanup_items = [{
        "artifact_id": art_id,
        "filename": "Shoot_Schedule.csv",
        "reason": "APPROVAL_TRANSACTION_ABORTED",
        "staging_version": 3,
    }]

    ok, res_run = store.reconcile_run_approval_atomic(
        space_id=space_id,
        run_id=run_id,
        target_status=RunStatus.FAILED,
        approval_commit_status="aborted",
        gate_status="failed",
        failure_code="APPROVAL_TRANSACTION_ABORTED",
        error_summary="Abort test",
        cleanup_items=cleanup_items,
    )
    assert ok is True
    assert res_run.status == RunStatus.FAILED
    assert res_run.approval_commit_status == "aborted"

    # Cleanup job exists in queue immediately
    pending = store.list_pending_artifact_cleanups()
    assert any(job["artifact_id"] == art_id for job in pending)


# -----------------------------------------------------------------------------
# Gate D34: Double DB Failure on Dispatch Leaves DISPATCHING for Lease Recovery
# -----------------------------------------------------------------------------


def test_gate_d34_dispatch_double_db_failure_preserves_generation_and_task_name():
    space_id = "sp_d34"
    action_id = "act_d34"
    user_id = "u_d34"

    rec = ActionExecutionRecord(
        action_id=action_id,
        run_id="run_d34",
        space_id=space_id,
        user_id=user_id,
        status=ActionExecutionStatus.PENDING,
        dispatch_status=DispatchStatus.PENDING,
        dispatch_generation=0,
        dispatch_version=1,
    )
    store.action_executions[action_id] = rec

    token = "lease_d34_tok"
    claimed, exec_rec, _ = store.claim_action_dispatch(action_id, token)
    assert claimed is True
    assert exec_rec.dispatch_status == DispatchStatus.DISPATCHING
    assert exec_rec.dispatch_generation == 1

    # Simulate: Enqueue succeeded and produced task_name "task-123"
    # But record_dispatch_success fails with DB error, and mark_dispatch_confirmation_uncertain ALSO fails
    # In this case, code does NOT call record_dispatch_failure, so state remains DISPATCHING under token
    curr = store.get_action_execution(action_id)
    assert curr.dispatch_status == DispatchStatus.DISPATCHING

    # Fast forward past lease expiry
    store.action_executions[action_id].dispatch_lease_until = datetime.now(UTC) - timedelta(seconds=5)

    # Recovery claim MUST preserve generation 1
    rec_token = "lease_d34_rec"
    rec_claimed, rec_exec, _ = store.claim_action_dispatch(action_id, rec_token)
    assert rec_claimed is True
    assert rec_exec.dispatch_generation == 1  # Strictly preserved!


# -----------------------------------------------------------------------------
# Gate D35: Commit Timeout Reconcile Preserves Published Deliverables
# -----------------------------------------------------------------------------


def test_gate_d35_commit_timeout_preserves_published_deliverables():
    from app.services.file_service import FileService
    from app.models.file_record import FileSourceType
    space_id = "sp_d35"
    run_id = "run_d35"
    user_id = "u_d35"
    store.create_space(Space(space_id=space_id, name="Space D35", created_by=user_id), creator_uid=user_id)

    f_sched = FileService.upload_file(
        space_id=space_id,
        filename="Schedule.csv",
        content=b"scene,1\n",
        content_type="text/csv",
        user=User(uid=user_id, email="d35@pdx.com"),
        source_type=FileSourceType.PDX_ARTIFACT,
        run_id=run_id,
        publication_status="published",
        file_id=f"file_{run_id}_schedule",
    )
    store.save_artifact_blob(space_id, f_sched.file_id, f_sched.filename, b"scene,1\n")
    store.save_artifact_record(ArtifactDescriptor(
        artifact_id=f_sched.file_id,
        space_id=space_id,
        run_id=run_id,
        filename=f_sched.filename,
        media_type="text/csv",
        size_bytes=len(b"scene,1\n"),
        sha256=f_sched.sha256,
        storage_path=f_sched.storage_path,
        download_endpoint=f"/v1/spaces/{space_id}/artifacts/{f_sched.file_id}/download",
        visibility="published",
    ))

    run = Run(
        run_id=run_id,
        space_id=space_id,
        status=RunStatus.RUNNING,
        approval_commit_status="uncertain",
        approval_gate=ApprovalGate(
            gate_id="gate_d35",
            title="Gate D35",
            description="Gate D35",
            risk_level="high",
            status="approving",
            decision_lease_token="lease_d35",
        ),
        output_artifact_ids=[f_sched.file_id],
        created_by=user_id,
    )
    store.save_run(run)

    # Reconcile without force_aborted
    status_res, rec_run = DeliverableExecutionService.reconcile_approval_commit(space_id, run_id, force_aborted=False)
    assert status_res == "COMMITTED_PUBLISHED"
    assert rec_run.status == RunStatus.COMPLETED
    assert rec_run.approval_commit_status == "committed"

    # Blob is intact
    assert store.get_artifact_blob(space_id, f_sched.file_id, f_sched.filename) == b"scene,1\n"


# -----------------------------------------------------------------------------
# Gate D36: Manifest Lifecycle Published On Approval and Purged On Reconcile Abort
# -----------------------------------------------------------------------------


def test_gate_d36_manifest_lifecycle_published_or_purged():
    from app.services.file_service import FileService
    from app.models.file_record import FileSourceType
    space_id = "sp_d36"
    run_id = "run_d36"
    user_id = "u_d36"
    store.create_space(Space(space_id=space_id, name="Space D36", created_by=user_id), creator_uid=user_id)

    # 1. Staging manifest
    m_file = FileService.upload_file(
        space_id=space_id,
        filename="RunManifest_d36.json",
        content=b'{"manifest": true}',
        content_type="application/json",
        user=User(uid=user_id, email="d36@pdx.com"),
        source_type=FileSourceType.MANIFEST,
        run_id=run_id,
        publication_status="pending_approval",
        file_id=f"file_{run_id}_manifest",
    )
    store.save_artifact_blob(space_id, m_file.file_id, m_file.filename, b'{"manifest": true}')
    store.save_artifact_record(ArtifactDescriptor(
        artifact_id=m_file.file_id,
        space_id=space_id,
        run_id=run_id,
        filename=m_file.filename,
        media_type="application/json",
        size_bytes=len(b'{"manifest": true}'),
        sha256=m_file.sha256,
        storage_path=m_file.storage_path,
        download_endpoint=f"/v1/spaces/{space_id}/artifacts/{m_file.file_id}/download",
        visibility="pending_approval",
    ))

    run = Run(
        run_id=run_id,
        space_id=space_id,
        status=RunStatus.RUNNING,
        approval_gate=ApprovalGate(
            gate_id="gate_d36",
            title="Gate D36",
            description="Gate D36",
            risk_level="high",
            status="approving",
            decision_lease_token=None,  # Aborted
        ),
        manifest_file_id=m_file.file_id,
        output_artifact_ids=[m_file.file_id],
        created_by=user_id,
    )
    store.save_run(run)

    # Reconcile aborts and enqueues cleanup including manifest
    status_res, _ = DeliverableExecutionService.reconcile_approval_commit(space_id, run_id, force_aborted=True)
    assert status_res == "ABORTED_CLEANUP_ENQUEUED"

    # Sweeper runs and purges manifest blob
    swept = DeliverableExecutionService.sweep_artifact_cleanups()
    assert swept >= 1
    assert store.get_artifact_blob(space_id, m_file.file_id, m_file.filename) is None


# -----------------------------------------------------------------------------
# Gate D37: Confirm Action Route Handles Enqueue Success With DB Write Failure
# -----------------------------------------------------------------------------


def test_gate_d37_confirm_action_route_handles_enqueue_success_with_db_write_failure():
    space_id = "sp_d37"
    action_id = "act_d37"
    user_id = "u_d37"
    coord = User(uid=user_id, email="d37@pdx.com")
    store.save_user(coord)
    store.create_space(Space(space_id=space_id, name="Space D37", created_by=user_id), creator_uid=user_id)

    proposal = issue_action_token(
        space_id=space_id,
        project_tag="general",
        user_id=user_id,
        title="D37 Test Action",
        description="D37 Test Description",
        sources=[],
        action_type="create_call_sheet",
        ttl_seconds=300,
    )
    store.add_message(Message(
        space_id=space_id,
        sender_uid="agent_studiotower",
        role=MessageRole.AGENT,
        content="Action proposal",
        proposed_action=proposal,
    ))

    app.dependency_overrides[get_current_user] = lambda: coord
    try:
        mock_runner = MagicMock()
        mock_runner.dispatch_action.return_value = "tasks/d37-task-1"
        with patch("app.services.action_runner.get_action_runner", return_value=mock_runner):
            with patch.object(store, "record_dispatch_success", side_effect=Exception("Simulated Firestore timeout")):
                res = client.post(f"/v1/spaces/{space_id}/actions/confirm", json={"action_id": proposal.action_id})
                assert res.status_code == 200

                # Action execution MUST be DISPATCH_CONFIRMATION_PENDING, not FAILED
                curr_exec = store.get_action_execution(proposal.action_id)
                assert curr_exec is not None
                assert curr_exec.dispatch_status == DispatchStatus.DISPATCH_CONFIRMATION_PENDING
                assert curr_exec.task_name == "tasks/d37-task-1"
    finally:
        app.dependency_overrides.clear()


# -----------------------------------------------------------------------------
# Gate D38: Firestore Abort Cleanup Lifecycle in artifact_cleanups Collection
# -----------------------------------------------------------------------------


def test_gate_d38_firestore_abort_cleanup_lifecycle():
    from app.services.storage import FirestoreStore
    import sys
    from unittest.mock import MagicMock, patch

    space_id = "sp_d38"
    run_id = "run_d38"
    user_id = "u_d38"
    art_id = "file_d38_sched"

    db_store = {}

    class StatefulDocSnap:
        def __init__(self, doc_id, col_dict):
            self.id = doc_id
            self._dict = col_dict
        @property
        def exists(self):
            return self.id in self._dict
        def to_dict(self):
            return dict(self._dict[self.id]) if self.exists else None

    class StatefulDocRef:
        def __init__(self, doc_id, col_dict):
            self.id = doc_id
            self._dict = col_dict
            self.reference = self
        def get(self, transaction=None):
            return StatefulDocSnap(self.id, self._dict)
        def set(self, data):
            self._dict[self.id] = dict(data)
        def update(self, data):
            self._dict[self.id].update(data)

    class StatefulCollectionQuery:
        def __init__(self, col_dict, filters=None):
            self._dict = col_dict
            self._filters = filters or []
        def where(self, field, op, val):
            new_filters = list(self._filters) + [(field, op, val)]
            return StatefulCollectionQuery(self._dict, new_filters)
        def stream(self):
            res = []
            for doc_id, doc_val in self._dict.items():
                match = True
                for field, op, val in self._filters:
                    if op == "==" and doc_val.get(field) != val:
                        match = False
                        break
                if match:
                    res.append(StatefulDocSnap(doc_id, self._dict))
            return iter(res)
        def document(self, doc_id):
            return StatefulDocRef(doc_id, self._dict)

    class StatefulFakeClient:
        def __init__(self, backing_db):
            self._db = backing_db
        def collection(self, name):
            col_dict = self._db.setdefault(name, {})
            return StatefulCollectionQuery(col_dict)
        def transaction(self):
            t = MagicMock()
            t.set = lambda ref, data: ref.set(data)
            t.update = lambda ref, data: ref.update(data)
            return t

    mock_fs = MagicMock()
    mock_fs.transactional = lambda fn: (lambda tx, *args, **kwargs: fn(tx, *args, **kwargs))

    with patch.dict(sys.modules, {"google.cloud.firestore": mock_fs}):
        client_1 = StatefulFakeClient(db_store)
        fs_store_1 = FirestoreStore(project_id="test-pdx", client=client_1)

        run = Run(
            run_id=run_id,
            space_id=space_id,
            status=RunStatus.RUNNING,
            state_version=1,
            output_artifact_ids=[art_id],
            approval_gate=ApprovalGate(
                gate_id="gate_d38",
                title="Gate D38",
                description="Gate D38",
                risk_level="high",
                status="approving",
            ),
            created_by=user_id,
        )
        fs_store_1.save_run(run)

        # Reconcile abort with cleanup intent
        cleanup_items = [{
            "artifact_id": art_id,
            "filename": "schedule.json",
            "reason": "APPROVAL_TRANSACTION_ABORTED",
            "staging_version": 1,
        }]
        ok, rec_run = fs_store_1.reconcile_run_approval_atomic(
            space_id=space_id,
            run_id=run_id,
            target_status=RunStatus.FAILED,
            approval_commit_status="aborted",
            gate_status="failed",
            cleanup_items=cleanup_items,
        )
        assert ok is True
        assert rec_run.status == RunStatus.FAILED

        # Re-instantiate FirestoreStore from same underlying database
        client_2 = StatefulFakeClient(db_store)
        fs_store_2 = FirestoreStore(project_id="test-pdx", client=client_2)

        # Verify list_pending_artifact_cleanups reads the job from artifact_cleanups
        pending_jobs = fs_store_2.list_pending_artifact_cleanups()
        assert len(pending_jobs) == 1
        job = pending_jobs[0]
        assert job["job_id"] == f"cleanup:{art_id}:1"
        assert job["status"] == "pending"
        assert job["artifact_id"] == art_id

        # Verify no pending_artifact_cleanups collection was erroneously written
        assert "pending_artifact_cleanups" not in db_store or len(db_store["pending_artifact_cleanups"]) == 0
        assert f"cleanup:{art_id}:1" in db_store["artifact_cleanups"]

        # Claim the cleanup job
        claimed = fs_store_2.claim_artifact_cleanup_job(job["job_id"], worker_id="sweeper_alpha", lease_seconds=60)
        assert claimed is not None
        assert claimed["status"] == "in_progress"
        assert claimed["lease_owner"] == "sweeper_alpha"

        # Complete the cleanup job
        comp_ok = fs_store_2.complete_artifact_cleanup(
            job["job_id"], worker_id="sweeper_alpha", lease_token=claimed["lease_token"]
        )
        assert comp_ok is True

        # Verify completed job is no longer returned in pending list
        assert len(fs_store_2.list_pending_artifact_cleanups()) == 0


# -----------------------------------------------------------------------------
# Gate D39: TOCTOU Fencing Inside Reconcile Atomic Transaction
# -----------------------------------------------------------------------------


def test_gate_d39_toctou_fencing_rejects_stale_publication_state():
    from app.models.file_record import FileSourceType

    space_id = "sp_d39"
    run_id = "run_d39"
    user_id = "u_d39"
    art_id = "file_d39_art"

    user = User(uid=user_id, email="d39@pdx.com")
    store.save_user(user)
    store.create_space(Space(space_id=space_id, name="Space D39", created_by=user_id), creator_uid=user_id)

    run = Run(
        run_id=run_id,
        space_id=space_id,
        status=RunStatus.RUNNING,
        state_version=1,
        output_artifact_ids=[art_id],
        approval_gate=ApprovalGate(
            gate_id="gate_d39",
            title="Gate D39",
            description="Gate D39",
            risk_level="high",
            status="approving",
        ),
        created_by=user_id,
    )
    store.save_run(run)

    # Save artifact as pending_approval
    art = ArtifactDescriptor(
        artifact_id=art_id,
        space_id=space_id,
        run_id=run_id,
        filename="d39_deliverable.json",
        byte_size=100,
        sha256="dummy_sha",
        visibility="pending_approval",
    )
    store.save_artifact_record(art)
    f_rec = FileRecord(
        file_id=art_id,
        space_id=space_id,
        filename="d39_deliverable.json",
        byte_size=100,
        sha256_hash="dummy_sha",
        uploaded_by=user_id,
        source_type=FileSourceType.PDX_ARTIFACT,
        publication_status="pending_approval",
    )
    store.save_file(f_rec)

    # 1. Attempt reconcile to COMPLETED while artifact is NOT published -> MUST BE REJECTED
    ok, comp_run = store.reconcile_run_approval_atomic(
        space_id=space_id,
        run_id=run_id,
        target_status=RunStatus.COMPLETED,
        approval_commit_status="committed",
        gate_status="approved",
        expected_run_status=RunStatus.RUNNING,
    )
    assert ok is False
    assert comp_run is None

    # 2. Publish artifact and file
    art.visibility = "published"
    store.save_artifact_record(art)
    f_rec.publication_status = "published"
    store.save_file(f_rec)

    # 3. Attempt reconcile to FAILED when all deliverables are published -> MUST BE REJECTED
    ok, fail_run = store.reconcile_run_approval_atomic(
        space_id=space_id,
        run_id=run_id,
        target_status=RunStatus.FAILED,
        approval_commit_status="aborted",
        gate_status="failed",
        expected_run_status=RunStatus.RUNNING,
    )
    assert ok is False
    assert fail_run is None

    # 4. Reconcile to COMPLETED when published -> MUST SUCCEED
    ok, success_run = store.reconcile_run_approval_atomic(
        space_id=space_id,
        run_id=run_id,
        target_status=RunStatus.COMPLETED,
        approval_commit_status="committed",
        gate_status="approved",
        expected_run_status=RunStatus.RUNNING,
    )
    assert ok is True
    assert success_run.status == RunStatus.COMPLETED


# -----------------------------------------------------------------------------
# Gate D40: Idempotent Reconcile Prevents State Version and Job Duplication
# -----------------------------------------------------------------------------


def test_gate_d40_idempotent_reconcile_prevents_duplicate_versions_and_jobs():
    from app.services.deliverable_service import DeliverableExecutionService

    space_id = "sp_d40"
    run_id = "run_d40"
    user_id = "u_d40"
    art_id = "file_d40_art"

    user = User(uid=user_id, email="d40@pdx.com")
    store.save_user(user)
    store.create_space(Space(space_id=space_id, name="Space D40", created_by=user_id), creator_uid=user_id)

    run = Run(
        run_id=run_id,
        space_id=space_id,
        status=RunStatus.RUNNING,
        state_version=1,
        output_artifact_ids=[art_id],
        approval_gate=ApprovalGate(
            gate_id="gate_d40",
            title="Gate D40",
            description="Gate D40",
            risk_level="high",
            status="approving",
        ),
        created_by=user_id,
    )
    store.save_run(run)

    art = ArtifactDescriptor(
        artifact_id=art_id,
        space_id=space_id,
        run_id=run_id,
        filename="d40.json",
        byte_size=10,
        sha256="d40",
        visibility="pending_approval",
    )
    store.save_artifact_record(art)

    # First reconcile abort
    status_1, rec_1 = DeliverableExecutionService.reconcile_approval_commit(space_id, run_id, force_aborted=True)
    assert status_1 == "ABORTED_CLEANUP_ENQUEUED"
    assert rec_1.status == RunStatus.FAILED
    ver_1 = rec_1.state_version

    # Count outbox and cleanups
    reconcile_outbox_count_1 = len([o for o in store.activity_outbox.values() if f"run.reconciled.{RunStatus.FAILED.value}:{run_id}" in o.event_id])
    cleanup_count_1 = len([j for j in store.artifact_cleanups.values() if j.get("artifact_id") == art_id])
    assert reconcile_outbox_count_1 == 1
    assert cleanup_count_1 == 1

    # Second reconcile abort (retry)
    status_2, rec_2 = DeliverableExecutionService.reconcile_approval_commit(space_id, run_id, force_aborted=True)
    assert status_2 == "ALREADY_ABORTED"
    assert rec_2.state_version == ver_1

    # Verify no state version increment, no duplicate outbox event, and no duplicate cleanup job
    reconcile_outbox_count_2 = len([o for o in store.activity_outbox.values() if f"run.reconciled.{RunStatus.FAILED.value}:{run_id}" in o.event_id])
    cleanup_count_2 = len([j for j in store.artifact_cleanups.values() if j.get("artifact_id") == art_id])
    assert reconcile_outbox_count_2 == reconcile_outbox_count_1
    assert cleanup_count_2 == cleanup_count_1


# -----------------------------------------------------------------------------
# Gate D41: Single Cleanup Intent Strictly Without Duplicate Legacy Jobs
# -----------------------------------------------------------------------------


def test_gate_d41_single_cleanup_intent_no_duplicate_jobs():
    space_id = "sp_d41"
    art_id = "file_d41_art"

    # 1. Enqueue via enqueue_artifact_cleanup
    job_id = store.enqueue_artifact_cleanup(space_id, art_id, "d41.bin", "TEST_REASON", staging_version=1)
    assert job_id == f"cleanup:{art_id}:1"

    # Verify ONLY canonical ID is in store.artifact_cleanups
    assert f"cleanup:{art_id}:1" in store.artifact_cleanups
    assert f"clean_art_{art_id}" not in store.artifact_cleanups

    # Verify pending cleanups has exactly 1 entry for this artifact
    pending = store.list_pending_artifact_cleanups()
    art_jobs = [j for j in pending if j.get("artifact_id") == art_id]
    assert len(art_jobs) == 1
    assert art_jobs[0]["job_id"] == f"cleanup:{art_id}:1"


# -----------------------------------------------------------------------------
# Gate D42: Uncertain Approval Lease Expiration, Maintenance Reconciler & Cleanup
# -----------------------------------------------------------------------------


def test_gate_d42_uncertain_approval_lease_expiration_and_maintenance_reconcile():
    from app.services.deliverable_service import DeliverableExecutionService
    from app.models.action_proposal import ActionExecutionRecord, ActionExecutionStatus
    from app.models.file_record import FileSourceType

    space_id = "sp_d42"
    run_id = "run_d42"
    action_id = "act_d42"
    user_id = "u_d42"
    art_id = "file_d42_art"
    now = datetime.now(UTC)

    user = User(uid=user_id, email="d42@pdx.com")
    store.save_user(user)
    store.create_space(Space(space_id=space_id, name="Space D42", created_by=user_id), creator_uid=user_id)

    # 1. Create ActionExecutionRecord in RUNNING
    exec_rec = ActionExecutionRecord(
        action_id=action_id,
        space_id=space_id,
        user_id=user_id,
        run_id=run_id,
        status=ActionExecutionStatus.RUNNING,
        version=1,
    )
    store.action_executions[action_id] = exec_rec

    # 2. Create Run in RUNNING with approval_commit_status="uncertain" and active decision lease
    run = Run(
        run_id=run_id,
        space_id=space_id,
        action_id=action_id,
        status=RunStatus.RUNNING,
        state_version=1,
        output_artifact_ids=[art_id],
        approval_commit_status="uncertain",
        uncertain_since=now,
        approval_gate=ApprovalGate(
            gate_id="gate_d42",
            title="Gate D42",
            description="Gate D42",
            risk_level="high",
            status="approving",
            decision_lease_token="lease_d42_token",
            decision_lease_until=now + timedelta(seconds=60),
        ),
        created_by=user_id,
    )
    store.save_run(run)

    # 3. Save Artifact and FileRecord in pending_approval
    art = ArtifactDescriptor(
        artifact_id=art_id,
        space_id=space_id,
        run_id=run_id,
        filename="d42_staging.json",
        byte_size=120,
        sha256="d42_hash",
        visibility="pending_approval",
    )
    store.save_artifact_record(art)
    store.save_artifact_blob(space_id, art_id, "d42_staging.json", b'{"staging": "payload"}')

    f_rec = FileRecord(
        file_id=art_id,
        space_id=space_id,
        filename="d42_staging.json",
        byte_size=120,
        sha256_hash="d42_hash",
        uploaded_by=user_id,
        source_type=FileSourceType.PDX_ARTIFACT,
        publication_status="pending_approval",
    )
    store.save_file(f_rec)

    # 4. Decision lease is still active: reconciler returns IN_PROGRESS
    res_status, active_run = DeliverableExecutionService.reconcile_approval_commit(space_id, run_id, force_aborted=False)
    assert res_status == "IN_PROGRESS"
    assert active_run.status == RunStatus.RUNNING
    assert active_run.state_version == 1

    # 5. Advance lease expiration into past via formal CAS (no object aliasing dependency)
    def advance_time(r: Run):
        if r.approval_gate:
            r.approval_gate.decision_lease_until = now - timedelta(seconds=10)
        r.uncertain_since = now - timedelta(seconds=60)

    store.compare_and_swap_run_status(
        run_id=run_id,
        expected_status=RunStatus.RUNNING,
        new_status=RunStatus.RUNNING,
        mutator_fn=advance_time,
        actor_uid=user_id,
    )

    # 6. Maintenance reconciler scans and atomically reconciles expired uncertain run to FAILED
    reconciled_count = DeliverableExecutionService.scan_and_reconcile_stalled_approvals(space_id=space_id)
    assert reconciled_count == 1

    # Authoritative state validations: Run, Gate, and ActionExecution are FAILED
    aborted_run = store.get_run(run_id)
    assert aborted_run.status == RunStatus.FAILED
    assert aborted_run.approval_commit_status == "aborted"
    assert aborted_run.approval_gate.status == "failed"
    assert aborted_run.approval_gate.decision_lease_token is None
    assert aborted_run.approval_gate.decision_lease_until is None
    assert aborted_run.state_version == 3

    aborted_exec = store.action_executions.get(action_id)
    assert aborted_exec.status == ActionExecutionStatus.FAILED

    # 7. Atomically created unique cleanup intent and Outbox in same transaction
    reconcile_outbox_1 = [o for o in store.activity_outbox.values() if f"run.reconciled.{RunStatus.FAILED.value}:{run_id}" in o.event_id]
    assert len(reconcile_outbox_1) == 1

    cleanups_1 = [j for j in store.artifact_cleanups.values() if j.get("artifact_id") == art_id]
    assert len(cleanups_1) == 1
    cleanup_job = cleanups_1[0]
    assert cleanup_job["job_id"] == f"cleanup:{art_id}:2"
    assert cleanup_job["status"] == "pending"

    # 8. Rerunning maintenance / reconcile is idempotent: no state version increment, no duplicate cleanups or Outbox
    rerun_reconciled = DeliverableExecutionService.scan_and_reconcile_stalled_approvals(space_id=space_id)
    assert rerun_reconciled == 0
    refetched_run = store.get_run(run_id)
    assert refetched_run.state_version == 3

    reconcile_outbox_2 = [o for o in store.activity_outbox.values() if f"run.reconciled.{RunStatus.FAILED.value}:{run_id}" in o.event_id]
    assert len(reconcile_outbox_2) == 1

    cleanups_2 = [j for j in store.artifact_cleanups.values() if j.get("artifact_id") == art_id]
    assert len(cleanups_2) == 1

    # 9. Sweeper claims, verifies, and deletes staging Blob
    assert store.get_artifact_blob(space_id, art_id, "d42_staging.json") is not None
    swept_count = DeliverableExecutionService.sweep_artifact_cleanups()
    assert swept_count >= 1
    assert store.get_artifact_blob(space_id, art_id, "d42_staging.json") is None
    assert store.artifact_cleanups[f"cleanup:{art_id}:2"]["status"] == "completed"


# -----------------------------------------------------------------------------
# Gate D43: Strict Firestore Read-Before-Write Cleanup Lifecycle & Mutual Exclusion
# -----------------------------------------------------------------------------


def test_gate_d43_strict_firestore_read_before_write_cleanup_lifecycle():
    from app.services.storage import FirestoreStore, StorageConflictError
    import sys
    from unittest.mock import MagicMock, patch

    space_id = "sp_d43"
    run_id = "run_d43"
    user_id = "u_d43"
    art_id = "file_d43_art"

    db_store = {}

    class StrictDocSnap:
        def __init__(self, doc_id, col_dict):
            self.id = doc_id
            self._dict = col_dict
        @property
        def exists(self):
            return self.id in self._dict
        def to_dict(self):
            return dict(self._dict[self.id]) if self.exists else None

    class StrictDocRef:
        def __init__(self, doc_id, col_dict):
            self.id = doc_id
            self._dict = col_dict
            self.reference = self
        def get(self, transaction=None):
            if transaction and getattr(transaction, "write_started", False):
                raise RuntimeError("Firestore transaction read-after-write is prohibited: all reads must precede writes.")
            return StrictDocSnap(self.id, self._dict)
        def set(self, data):
            self._dict[self.id] = dict(data)
        def update(self, data):
            if self.id in self._dict:
                self._dict[self.id].update(data)
            else:
                self._dict[self.id] = dict(data)

    class StrictFirestoreTransaction(MagicMock):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._read_only = False
            self.write_started = False
        def set(self, ref, data):
            self.write_started = True
            ref.set(data)
        def update(self, ref, data):
            self.write_started = True
            ref.update(data)
        def delete(self, ref):
            self.write_started = True
            ref._dict.pop(ref.id, None)

    class StrictFirestoreFakeClient:
        def __init__(self, backing_db):
            self._db = backing_db
        def collection(self, name):
            col_dict = self._db.setdefault(name, {})
            q = MagicMock()
            q.document = lambda doc_id: StrictDocRef(doc_id, col_dict)
            q.where = lambda field, op, val: MagicMock(
                stream=lambda: [
                    StrictDocSnap(k, col_dict)
                    for k, v in col_dict.items()
                    if v.get(field) == val
                ]
            )
            return q
        def transaction(self):
            return StrictFirestoreTransaction()

    mock_fs = MagicMock()
    mock_fs.transactional = lambda fn: (lambda tx, *args, **kwargs: fn(tx, *args, **kwargs))

    try:
        from google.cloud import firestore as real_fs
        patch_decorator = patch.object(real_fs, "transactional", lambda fn: (lambda tx, *args, **kwargs: fn(tx, *args, **kwargs)))
    except Exception:
        patch_decorator = patch.dict(sys.modules, {"google.cloud.firestore": mock_fs})

    with patch_decorator:
        client = StrictFirestoreFakeClient(db_store)
        fs_store = FirestoreStore(project_id="test-pdx", client=client)

        # 1. Prepare artifact and file in pending_approval
        client.collection("artifacts").document(art_id).set({
            "artifact_id": art_id,
            "space_id": space_id,
            "run_id": run_id,
            "filename": "d43.json",
            "visibility": "pending_approval",
        })
        client.collection("files").document(art_id).set({
            "file_id": art_id,
            "space_id": space_id,
            "filename": "d43.json",
            "publication_status": "pending_approval",
        })

        # Enqueue cleanup job
        job_id = fs_store.enqueue_artifact_cleanup(space_id, art_id, "d43.json", "RECONCILE_ABORT", staging_version=1)
        assert job_id == f"cleanup:{art_id}:1"

        # TEST 1: Claim cleanup job (Read-before-write strictly verified)
        claimed = fs_store.claim_artifact_cleanup_job(job_id, worker_id="worker_1", lease_seconds=60)
        assert claimed is not None
        assert claimed["status"] == "in_progress"
        # Assert artifact and file marked cleaning
        art_doc = client.collection("artifacts").document(art_id).get().to_dict()
        file_doc = client.collection("files").document(art_id).get().to_dict()
        assert art_doc["visibility"] == "cleaning"
        assert file_doc["publication_status"] == "cleaning"

        # TEST 2: Record cleanup failure (Read-before-write strictly verified)
        fail_ok = fs_store.record_cleanup_failure(
            job_id,
            worker_id="worker_1",
            error="TEST_FAILURE",
            lease_token=claimed["lease_token"],
            expected_version=claimed["version"],
        )
        assert fail_ok is True
        # Assert artifact and file reverted to pending_approval
        art_doc = client.collection("artifacts").document(art_id).get().to_dict()
        file_doc = client.collection("files").document(art_id).get().to_dict()
        assert art_doc["visibility"] == "pending_approval"
        assert file_doc["publication_status"] == "pending_approval"

        # TEST 3: Cancel cleanup job (Read-before-write strictly verified)
        # Reset next_retry_at so it can be claimed immediately
        client.collection("artifact_cleanups").document(job_id).update({"next_retry_at": None})
        # Re-claim job
        claimed_2 = fs_store.claim_artifact_cleanup_job(job_id, worker_id="worker_2", lease_seconds=60)
        assert claimed_2 is not None
        cancel_ok = fs_store.cancel_artifact_cleanup_job(job_id, reason="TEST_CANCEL")
        assert cancel_ok is True
        art_doc = client.collection("artifacts").document(art_id).get().to_dict()
        file_doc = client.collection("files").document(art_id).get().to_dict()
        assert art_doc["visibility"] == "pending_approval"
        assert file_doc["publication_status"] == "pending_approval"

        # TEST 4: Complete cleanup job (Read-before-write strictly verified)
        # Reset job to pending and claim
        client.collection("artifact_cleanups").document(job_id).update({
            "status": "pending",
            "lease_owner": None,
            "lease_token": None,
            "lease_until": None,
        })
        claimed_3 = fs_store.claim_artifact_cleanup_job(job_id, worker_id="worker_3", lease_seconds=60)
        assert claimed_3 is not None
        comp_ok = fs_store.complete_artifact_cleanup(
            job_id,
            worker_id="worker_3",
            lease_token=claimed_3["lease_token"],
            expected_version=claimed_3["version"],
        )
        assert comp_ok is True
        art_doc = client.collection("artifacts").document(art_id).get().to_dict()
        file_doc = client.collection("files").document(art_id).get().to_dict()
        assert art_doc["visibility"] == "purged"
        assert file_doc["publication_status"] == "failed"

        # TEST 5: Mutual Exclusion: publishing must fail when artifact is cleaning
        run = Run(
            run_id=run_id,
            space_id=space_id,
            status=RunStatus.RUNNING,
            state_version=1,
            output_artifact_ids=[art_id],
            approval_gate=ApprovalGate(
                gate_id="gate_d43",
                title="Gate D43",
                description="Gate D43",
                risk_level="high",
                status="approving",
                decision_lease_token="lease_d43",
            ),
            created_by=user_id,
        )
        client.collection("runs").document(run_id).set(run.model_dump(mode="json"))

        # Mark artifact as cleaning
        client.collection("artifacts").document(art_id).update({"visibility": "cleaning"})
        client.collection("files").document(art_id).update({"publication_status": "cleaning"})

        # Publishing must be rejected with StorageConflictError
        import pytest
        with pytest.raises(StorageConflictError):
            fs_store.approve_gate_and_publish_artifacts_atomic(
                space_id=space_id,
                run_id=run_id,
                approver_uid=user_id,
                output_artifact_ids=[art_id],
                decision_lease_token="lease_d43",
            )

        # TEST 6: Mutual Exclusion: claim must be rejected and cancelled when artifact is published
        client.collection("artifacts").document(art_id).update({"visibility": "published"})
        client.collection("files").document(art_id).update({"publication_status": "published"})

        # New cleanup job for same artifact
        job_id_published = fs_store.enqueue_artifact_cleanup(space_id, art_id, "d43.json", "STRAY_CLEANUP", staging_version=2)
        claim_published = fs_store.claim_artifact_cleanup_job(job_id_published, worker_id="sweeper_beta")
        assert claim_published is None

        # Job cancelled and artifact remains published
        cancelled_job = client.collection("artifact_cleanups").document(job_id_published).get().to_dict()
        assert cancelled_job["status"] == "cancelled"
        assert cancelled_job["reason"] == "ALREADY_PUBLISHED"
        art_final = client.collection("artifacts").document(art_id).get().to_dict()
        assert art_final["visibility"] == "published"


def test_gate_d44_manifest_file_id_decoupling_and_approval_success():
    """
    Validates that approve_gate_and_publish_artifacts_atomic succeeds when run.manifest_file_id
    is a file record ID (e.g. 'file_art_...') and does not exist in the artifacts collection.
    Both the artifact and the corresponding file record must transition to published.
    """
    space_id = "sp_d44"
    run_id = "run_d44"
    user_id = "u_d44"
    art_id = "art_d44_budget"
    file_id = "file_art_d44_budget"

    user = User(uid=user_id, email="d44@test.com")
    store.save_user(user)
    store.create_space(Space(space_id=space_id, name="Space D44", created_by=user_id), creator_uid=user_id)

    run = Run(
        run_id=run_id,
        space_id=space_id,
        status=RunStatus.RUNNING,
        state_version=1,
        output_artifact_ids=[art_id],
        manifest_file_id=file_id,
        approval_gate=ApprovalGate(
            gate_id="gate_d44",
            title="Gate D44",
            description="Gate D44",
            risk_level="critical",
            status="approving",
            decision_lease_token="lease_d44_valid",
        ),
        created_by=user_id,
    )
    store.save_run(run)

    art = ArtifactDescriptor(
        artifact_id=art_id,
        space_id=space_id,
        run_id=run_id,
        filename="production_budget.pdf",
        media_type="application/pdf",
        size_bytes=2048,
        sha256="sha_d44_budget",
        storage_path=f"{space_id}/artifacts/{art_id}",
        download_endpoint=f"/download/{art_id}",
        visibility="pending_approval",
    )
    store.save_artifact_record(art)

    file_rec = FileRecord(
        file_id=file_id,
        space_id=space_id,
        filename="production_budget.pdf",
        content_type="application/pdf",
        size_bytes=2048,
        sha256="sha_d44_budget",
        storage_path=f"{space_id}/files/{file_id}",
        project_tags=["general"],
        uploaded_by=user_id,
        source_type="pdx_artifact",
        run_id=run_id,
        publication_status="pending_approval",
    )
    store.save_file(file_rec)

    # Approval commit MUST succeed and not crash on missing artifact for file_id
    completed_run, _ = store.approve_gate_and_publish_artifacts_atomic(
        space_id=space_id,
        run_id=run_id,
        approver_uid=user_id,
        output_artifact_ids=[art_id],
        decision_lease_token="lease_d44_valid",
    )

    assert completed_run.status == RunStatus.COMPLETED
    assert completed_run.approval_gate.status == "approved"
    assert store.get_artifact(space_id, art_id).visibility == "published"
    assert store.get_file(file_id).publication_status == "published"




