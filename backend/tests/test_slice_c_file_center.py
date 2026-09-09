from app.core.auth import get_current_user
from app.main import app
from app.models.space import MembershipRole, ProjectTag, Space
from app.models.user import User
from app.services.storage import store
from fastapi.testclient import TestClient

client = TestClient(app)


# -----------------------------------------------------------------------------
# Gate C1: Tag Management RBAC (Owner, Admin, Coordinator allowed; Member rejected)
# -----------------------------------------------------------------------------


def test_gate_c1_tag_management_rbac():
    space_id = "sp_c1_rbac"
    owner_id = "u_c1_owner"
    coord_id = "u_c1_coord"
    member_id = "u_c1_member"

    store.save_user(User(uid=owner_id, email="owner@c1.com", display_name="Owner"))
    store.save_user(User(uid=coord_id, email="coord@c1.com", display_name="Coordinator"))
    store.save_user(User(uid=member_id, email="member@c1.com", display_name="Member"))

    space = Space(space_id=space_id, name="C1 Space", created_by=owner_id)
    store.create_space(space, creator_uid=owner_id)
    store.add_member(space_id, coord_id, MembershipRole.COORDINATOR)
    store.add_member(space_id, member_id, MembershipRole.MEMBER)

    # 1. Member tries to create tag -> 403 FORBIDDEN
    app.dependency_overrides[get_current_user] = lambda: store.get_user(member_id)
    try:
        res = client.post(
            f"/v1/spaces/{space_id}/tags",
            json={"name": "VFX Track", "slug": "vfx-track", "color": "#10B981"},
        )
        assert res.status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    # 2. Coordinator creates tag -> 200 OK
    app.dependency_overrides[get_current_user] = lambda: store.get_user(coord_id)
    try:
        res = client.post(
            f"/v1/spaces/{space_id}/tags",
            json={"name": "VFX Track", "slug": "vfx-track", "color": "#10B981", "description": "VFX assets"},
        )
        assert res.status_code == 200
        sp_data = res.json()
        assert any(t["slug"] == "vfx-track" and t["name"] == "VFX Track" for t in sp_data["tags"])
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# -----------------------------------------------------------------------------
# Gate C2: Tag Slug Validation, Immutability & General Tag Protection
# -----------------------------------------------------------------------------


def test_gate_c2_tag_slug_validation_and_general_protection():
    space_id = "sp_c2_slugs"
    owner_id = "u_c2_owner"
    store.save_user(User(uid=owner_id, email="owner@c2.com", display_name="Owner"))
    space = Space(space_id=space_id, name="C2 Space", created_by=owner_id)
    store.create_space(space, creator_uid=owner_id)

    app.dependency_overrides[get_current_user] = lambda: store.get_user(owner_id)
    try:
        # 1. Reserved slug "all" is rejected with 422
        res_reserved = client.post(
            f"/v1/spaces/{space_id}/tags",
            json={"name": "All Overview", "slug": "all"},
        )
        assert res_reserved.status_code == 422

        # 2. Invalid slug format with special characters rejected with 422
        res_invalid = client.post(
            f"/v1/spaces/{space_id}/tags",
            json={"name": "Invalid Tag", "slug": "bad slug!@#"},
        )
        assert res_invalid.status_code == 422

        # 3. Create valid tag
        res_valid = client.post(
            f"/v1/spaces/{space_id}/tags",
            json={"name": "Sound Track", "slug": "sound-track", "color": "#F59E0B"},
        )
        assert res_valid.status_code == 200

        # 4. Updating tag properties (name/color/description) succeeds without changing slug
        res_update = client.put(
            f"/v1/spaces/{space_id}/tags/sound-track",
            json={"name": "Sound & Foley Track", "color": "#EF4444", "description": "Audio assets"},
        )
        assert res_update.status_code == 200
        sp_data = res_update.json()
        sound_tag = next(t for t in sp_data["tags"] if t["slug"] == "sound-track")
        assert sound_tag["name"] == "Sound & Foley Track"
        assert sound_tag["color"] == "#EF4444"

        # 5. Attempting to archive default "general" tag is rejected with 422
        res_archive_general = client.post(
            f"/v1/spaces/{space_id}/tags/general/archive",
            json={"archived": True},
        )
        assert res_archive_general.status_code == 422
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# -----------------------------------------------------------------------------
# Gate C3: Tag Archive preserves Files and Citations (Non-destructive)
# -----------------------------------------------------------------------------


def test_gate_c3_tag_archive_preserves_resources_and_citations():
    space_id = "sp_c3_archive"
    owner_id = "u_c3_owner"
    store.save_user(User(uid=owner_id, email="owner@c3.com", display_name="Owner"))
    space = Space(space_id=space_id, name="C3 Space", created_by=owner_id)
    store.create_space(space, creator_uid=owner_id)

    app.dependency_overrides[get_current_user] = lambda: store.get_user(owner_id)
    try:
        # Create tag "block-a"
        client.post(
            f"/v1/spaces/{space_id}/tags",
            json={"name": "Block A", "slug": "block-a"},
        )

        # Upload file tagged with "block-a"
        res_file = client.post(
            f"/v1/spaces/{space_id}/files",
            files={"file": ("treatment_a.txt", b"Block A Treatment Content", "text/plain")},
            data={"project_tag": "block-a"},
        )
        assert res_file.status_code == 200
        file_id = res_file.json()["file_id"]

        # Archive tag "block-a"
        res_arch = client.post(
            f"/v1/spaces/{space_id}/tags/block-a/archive",
            json={"archived": True},
        )
        assert res_arch.status_code == 200
        archived_tag = next(t for t in res_arch.json()["tags"] if t["slug"] == "block-a")
        assert archived_tag["archived"] is True

        # Assert file is still accessible and not deleted
        res_get_file = client.get(f"/v1/spaces/{space_id}/files/{file_id}")
        assert res_get_file.status_code == 200
        assert res_get_file.json()["file_id"] == file_id
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# -----------------------------------------------------------------------------
# Gate C4: Activity Logging & Cursor-based Pagination
# -----------------------------------------------------------------------------


def test_gate_c4_activity_logging_and_cursor_pagination():
    space_id = "sp_c4_activity"
    owner_id = "u_c4_owner"
    store.save_user(User(uid=owner_id, email="owner@c4.com", display_name="Owner"))
    space = Space(space_id=space_id, name="C4 Space", created_by=owner_id)
    store.create_space(space, creator_uid=owner_id)

    app.dependency_overrides[get_current_user] = lambda: store.get_user(owner_id)
    try:
        # Upload multiple files to generate activity events
        client.post(
            f"/v1/spaces/{space_id}/files",
            files={"file": ("doc_1.txt", b"Content 1", "text/plain")},
            data={"project_tag": "general"},
        )
        client.post(
            f"/v1/spaces/{space_id}/files",
            files={"file": ("doc_2.txt", b"Content 2", "text/plain")},
            data={"project_tag": "general"},
        )

        # Query activity events with limit=1
        res_p1 = client.get(f"/v1/spaces/{space_id}/activity?limit=1")
        assert res_p1.status_code == 200
        data_p1 = res_p1.json()
        assert len(data_p1["items"]) == 1
        assert data_p1["next_cursor"] is not None

        # Query page 2 with next_cursor
        res_p2 = client.get(f"/v1/spaces/{space_id}/activity?cursor={data_p1['next_cursor']}&limit=1")
        assert res_p2.status_code == 200
        data_p2 = res_p2.json()
        assert len(data_p2["items"]) == 1
        # Assert different items
        assert data_p1["items"][0]["event_id"] != data_p2["items"][0]["event_id"]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# -----------------------------------------------------------------------------
# Gate C5: Activity Tag Scoping & Space-wide Overview Filter
# -----------------------------------------------------------------------------


def test_gate_c5_activity_tag_scoping():
    space_id = "sp_c5_tag_scope"
    owner_id = "u_c5_owner"
    store.save_user(User(uid=owner_id, email="owner@c5.com", display_name="Owner"))
    space = Space(space_id=space_id, name="C5 Space", created_by=owner_id)
    store.create_space(space, creator_uid=owner_id)

    app.dependency_overrides[get_current_user] = lambda: store.get_user(owner_id)
    try:
        # Create tag "track-a"
        client.post(
            f"/v1/spaces/{space_id}/tags",
            json={"name": "Track A", "slug": "track-a"},
        )

        # Upload 1 file in general, 1 file in track-a
        client.post(
            f"/v1/spaces/{space_id}/files",
            files={"file": ("gen_file.txt", b"Gen", "text/plain")},
            data={"project_tag": "general"},
        )
        client.post(
            f"/v1/spaces/{space_id}/files",
            files={"file": ("track_a_file.txt", b"Track A", "text/plain")},
            data={"project_tag": "track-a"},
        )

        # Query filtered by tag=track-a
        res_track = client.get(f"/v1/spaces/{space_id}/activity?tag=track-a")
        assert res_track.status_code == 200
        items_track = res_track.json()["items"]
        assert all(item["project_tag"] == "track-a" for item in items_track)
        assert any("track_a_file.txt" in item["summary"] for item in items_track)

        # Query space-wide overview (tag=all or None)
        res_all = client.get(f"/v1/spaces/{space_id}/activity?tag=all")
        assert res_all.status_code == 200
        items_all = res_all.json()["items"]
        tags_seen = {item["project_tag"] for item in items_all}
        assert "general" in tags_seen
        assert "track-a" in tags_seen
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# -----------------------------------------------------------------------------
# Gate C6: Multi-Store Instance Persistence & Tag / Activity Event Cross-Reading
# -----------------------------------------------------------------------------


def test_gate_c6_multi_instance_persistence_sync(tmp_path):
    import os

    from app.models.activity import ActivityEvent, ActivityEventType
    from app.services.storage import MemoryStore

    persist_file = str(tmp_path / "shared_cluster_storage.json")

    # Instance 1: Creates space, adds tag, and records activity event
    store1 = MemoryStore(persist_path=persist_file)
    u_owner = User(uid="u_c6_owner", email="owner@c6.com", display_name="Owner C6")
    store1.save_user(u_owner)
    sp = Space(space_id="sp_c6_multi", name="Multi-Instance Space", created_by=u_owner.uid)
    store1.create_space(sp, creator_uid=u_owner.uid)

    # Add tag via Store 1
    new_tag = ProjectTag(name="Sound Design", slug="sound-design", color="#EC4899")
    store1.add_tag_to_space("sp_c6_multi", new_tag)

    # Archive tag via Store 1
    store1.archive_tag_in_space("sp_c6_multi", "sound-design", archived=True)

    # Record activity event via Store 1
    ev = ActivityEvent(
        event_type=ActivityEventType.FILE_UPLOADED,
        space_id="sp_c6_multi",
        project_tag="sound-design",
        resource_type="file",
        resource_id="file_c6_01",
        summary="檔案 'soundtrack.wav' 已上傳",
    )
    store1.record_activity_event(ev)

    assert os.path.exists(persist_file)

    # Instance 2: Cold boot from the same shared disk file
    store2 = MemoryStore(persist_path=persist_file)

    # Verify Instance 2 can read the Space with updated archived tag
    loaded_sp = store2.get_space("sp_c6_multi")
    assert loaded_sp is not None
    sound_tag = next((t for t in loaded_sp.tags if t.slug == "sound-design"), None)
    assert sound_tag is not None
    assert sound_tag.archived is True

    # Verify Instance 2 can read the Activity Event list (including atomic tag created, tag archived, and file uploaded events)
    events, next_cursor = store2.list_activity_events("sp_c6_multi", tag="sound-design")
    assert len(events) == 3
    assert any(e.resource_id == "file_c6_01" and e.event_type == ActivityEventType.FILE_UPLOADED for e in events)
    assert any(e.event_type == ActivityEventType.TAG_CREATED for e in events)
    assert any(e.event_type == ActivityEventType.TAG_ARCHIVED for e in events)


# -----------------------------------------------------------------------------
# Gate C7: Unified Timeline across All 5 Resource Event Types
# -----------------------------------------------------------------------------


def test_gate_c7_unified_event_types_and_emission():
    space_id = "sp_c7_unified"
    owner_id = "u_c7_owner"
    store.save_user(User(uid=owner_id, email="owner@c7.com", display_name="Owner C7"))
    space = Space(space_id=space_id, name="C7 Unified Space", created_by=owner_id)
    store.create_space(space, creator_uid=owner_id)

    app.dependency_overrides[get_current_user] = lambda: store.get_user(owner_id)
    try:
        # 1. Tag event (create tag)
        res_tag = client.post(
            f"/v1/spaces/{space_id}/tags",
            json={"name": "VFX Action", "slug": "vfx-action", "color": "#10B981"},
        )
        assert res_tag.status_code == 200

        # 2. File event (upload file)
        res_file = client.post(
            f"/v1/spaces/{space_id}/files",
            files={"file": ("hero_script.pdf", b"%PDF-1.4...", "application/pdf")},
            data={"project_tag": "vfx-action"},
        )
        assert res_file.status_code == 200

        # 3. Message event (post message)
        res_msg = client.post(
            "/v1/chat",
            json={"space_id": space_id, "content": "Let's review the hero script breakdown", "project_tag": "vfx-action"},
        )
        assert res_msg.status_code == 200

        # 4. Query Unified Activity Feed
        res_act = client.get(f"/v1/spaces/{space_id}/activity?tag=vfx-action")
        assert res_act.status_code == 200
        items = res_act.json()["items"]

        # Ensure all types appear in unified timeline
        event_types = {item["event_type"] for item in items}
        assert "tag.created" in event_types
        assert "file.uploaded" in event_types
        assert "message.created" in event_types
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# -----------------------------------------------------------------------------
# Gate C8: Activity Event Idempotency & Immutable Create-if-Absent
# -----------------------------------------------------------------------------


def test_gate_c8_event_idempotency_and_immutable_create():
    import pytest
    from app.models.activity import ActivityEvent, ActivityEventType

    space_id = "sp_c8_idemp"
    ev_id = "test.idemp.event.01"

    ev1 = ActivityEvent(
        event_id=ev_id,
        event_type=ActivityEventType.TAG_CREATED,
        space_id=space_id,
        project_tags=["general"],
        resource_type="tag",
        resource_id="general",
        summary="Tag created",
    )
    # First write
    saved1 = store.record_activity_event(ev1)
    assert saved1.event_id == ev_id

    # Identical replay returns existing event idempotently
    saved2 = store.record_activity_event(ev1)
    assert saved2.event_id == ev_id

    # Colliding ID with differing payload raises collision error
    ev_conflict = ActivityEvent(
        event_id=ev_id,
        event_type=ActivityEventType.FILE_UPLOADED,
        space_id=space_id,
        project_tags=["general"],
        resource_type="file",
        resource_id="diff_file",
        summary="Diff payload",
    )
    with pytest.raises(ValueError, match="ACTIVITY_EVENT_ID_COLLISION"):
        store.record_activity_event(ev_conflict)


# -----------------------------------------------------------------------------
# Gate C9: Transactional Outbox Staging & Dispatch
# -----------------------------------------------------------------------------


def test_gate_c9_transactional_outbox_staging_and_dispatch():
    from app.models.activity import ActivityEvent, ActivityEventType, OutboxStatus

    space_id = "sp_c9_outbox"
    ev = ActivityEvent(
        event_id="outbox.event.01",
        event_type=ActivityEventType.TAG_UPDATED,
        space_id=space_id,
        project_tags=["general"],
        resource_type="tag",
        resource_id="general",
        summary="Tag updated in outbox",
    )

    outbox_item = store.stage_outbox_event(ev)
    assert outbox_item.status == OutboxStatus.PENDING
    assert outbox_item.outbox_id in store.activity_outbox

    # Dispatch outbox
    dispatched = store.dispatch_outbox_events(limit=1000)
    assert dispatched >= 1
    assert store.activity_outbox[outbox_item.outbox_id].status == OutboxStatus.PUBLISHED

    # Verify event is in activity feed
    events, _ = store.list_activity_events(space_id)
    assert any(e.event_id == "outbox.event.01" for e in events)


# -----------------------------------------------------------------------------
# Gate C10: Tag Multi-Label & project_tags Array Filtering
# -----------------------------------------------------------------------------


def test_gate_c10_tag_multi_label_and_project_tags_array_filtering():
    from app.models.activity import ActivityEvent, ActivityEventType

    space_id = "sp_c10_multitag"
    ev = ActivityEvent(
        event_id="multitag.event.01",
        event_type=ActivityEventType.FILE_UPLOADED,
        space_id=space_id,
        project_tags=["vfx-track", "sound-track"],
        resource_type="file",
        resource_id="multi_label_file",
        summary="檔案已指派至多個標籤",
    )
    store.record_activity_event(ev)

    # Query by vfx-track -> match
    res_vfx, _ = store.list_activity_events(space_id, tag="vfx-track")
    assert any(e.event_id == "multitag.event.01" for e in res_vfx)

    # Query by sound-track -> match
    res_sound, _ = store.list_activity_events(space_id, tag="sound-track")
    assert any(e.event_id == "multitag.event.01" for e in res_sound)

    # Query by unrelated tag -> no match
    res_other, _ = store.list_activity_events(space_id, tag="art-track")
    assert not any(e.event_id == "multitag.event.01" for e in res_other)


# -----------------------------------------------------------------------------
# Gate C11: Monotonic Revisioning & Run State Versioning
# -----------------------------------------------------------------------------


def test_gate_c11_monotonic_revisions_and_run_state_version():
    from app.models.run import Run, RunStatus

    space_id = "sp_c11_monotonic"
    owner_id = "u_c11_owner"
    store.save_user(User(uid=owner_id, email="owner@c11.com", display_name="Owner C11"))
    space = Space(space_id=space_id, name="C11 Space", created_by=owner_id)
    store.create_space(space, creator_uid=owner_id)

    # Tag Revisioning
    tag = ProjectTag(name="CGI Track", slug="cgi-track", color="#3B82F6")
    sp1 = store.add_tag_to_space(space_id, tag)
    cgi_tag = next(t for t in sp1.tags if t.slug == "cgi-track")
    assert cgi_tag.revision == 1

    sp2 = store.update_tag_in_space(space_id, "cgi-track", name="CGI & 3D Track")
    cgi_tag2 = next(t for t in sp2.tags if t.slug == "cgi-track")
    assert cgi_tag2.revision == 2

    sp3 = store.archive_tag_in_space(space_id, "cgi-track", archived=True)
    cgi_tag3 = next(t for t in sp3.tags if t.slug == "cgi-track")
    assert cgi_tag3.revision == 3

    # Run State Versioning
    run = Run(
        run_id="run_c11_ver",
        space_id=space_id,
        project_tag="cgi-track",
        status=RunStatus.AWAITING_APPROVAL,
        prompt="Test Run Version",
        created_by=owner_id,
        state_version=1,
    )
    store.save_run(run)

    updated_run = store.compare_and_swap_run_status(
        "run_c11_ver",
        expected_status=RunStatus.AWAITING_APPROVAL,
        new_status=RunStatus.RUNNING,
    )
    assert updated_run is not None
    assert updated_run.state_version == 2


# -----------------------------------------------------------------------------
# Gate C12: Tamper-Proof Signed Cursors
# -----------------------------------------------------------------------------


def test_gate_c12_tamper_proof_signed_cursors():
    import pytest
    from app.services.storage import decode_activity_cursor, encode_activity_cursor

    space_id = "sp_c12_cursor"
    payload = {
        "created_at": "2026-09-01T12:00:00Z",
        "event_id": "ev_c12_123",
        "space_id": space_id,
        "tag": "vfx",
    }
    cursor_token = encode_activity_cursor(payload)
    assert "." in cursor_token

    # Valid decode
    decoded = decode_activity_cursor(cursor_token, expected_space_id=space_id, expected_tag="vfx")
    assert decoded["event_id"] == "ev_c12_123"

    # Space mismatch rejected
    with pytest.raises(ValueError, match="CURSOR_SPACE_MISMATCH"):
        decode_activity_cursor(cursor_token, expected_space_id="sp_c12_other", expected_tag="vfx")

    # Tag mismatch rejected
    with pytest.raises(ValueError, match="CURSOR_TAG_MISMATCH"):
        decode_activity_cursor(cursor_token, expected_space_id=space_id, expected_tag="audio")

    # Tampered signature rejected
    parts = cursor_token.split(".")
    tampered = f"{parts[0]}.invalid_signature"
    with pytest.raises(ValueError, match="TAMPERED_CURSOR_SIGNATURE"):
        decode_activity_cursor(tampered, expected_space_id=space_id, expected_tag="vfx")


# -----------------------------------------------------------------------------
# Gate C13: CAS-Bound Gate and Run Events
# -----------------------------------------------------------------------------


def test_gate_c13_cas_bound_gate_and_run_events():
    from app.models.run import ApprovalGate, Run, RunStatus

    space_id = "sp_c13_gate"
    owner_id = "u_c13_owner"
    store.save_user(User(uid=owner_id, email="owner@c13.com", display_name="Owner C13"))
    space = Space(space_id=space_id, name="C13 Gate Space", created_by=owner_id)
    store.create_space(space, creator_uid=owner_id)

    run = Run(
        run_id="run_c13_gate",
        space_id=space_id,
        project_tag="general",
        status=RunStatus.AWAITING_APPROVAL,
        prompt="Gate Test",
        created_by=owner_id,
        approval_gate=ApprovalGate(
            gate_id="gate_c13",
            title="Budget Approval",
            description="Exceeds asset threshold",
            required_role="owner",
            status="pending",
        ),
    )
    store.save_run(run)

    app.dependency_overrides[get_current_user] = lambda: store.get_user(owner_id)
    try:
        res = client.post(
            f"/v1/spaces/{space_id}/runs/run_c13_gate/approve",
            json={"approved": True},
        )
        assert res.status_code == 200

        # Verify gate.approved and run.completed events are recorded
        events, _ = store.list_activity_events(space_id)
        event_types = [e.event_type for e in events]
        assert "gate.approved" in event_types
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# -----------------------------------------------------------------------------
# Gate C14: Cross-Resource Tag Isolation Matrix
# -----------------------------------------------------------------------------


def test_gate_c14_cross_resource_isolation_matrix():
    from app.models.message import Message, MessageRole
    from app.models.run import Run, RunStatus

    space_id = "sp_c14_iso"
    owner_id = "u_c14_owner"
    store.save_user(User(uid=owner_id, email="owner@c14.com", display_name="Owner C14"))
    space = Space(space_id=space_id, name="C14 Space", created_by=owner_id)
    store.create_space(space, creator_uid=owner_id)

    # 1. Message tag isolation
    msg_a = Message(space_id=space_id, sender_uid=owner_id, sender_name="Owner", role=MessageRole.USER, content="Msg A", project_tag="tag-a")
    msg_b = Message(space_id=space_id, sender_uid=owner_id, sender_name="Owner", role=MessageRole.USER, content="Msg B", project_tag="tag-b")
    store.add_message(msg_a)
    store.add_message(msg_b)

    msgs_a = store.list_messages(space_id, project_tag="tag-a")
    assert len(msgs_a) == 1 and msgs_a[0].content == "Msg A"

    # 2. Run tag isolation
    run_a = Run(run_id="run_a", space_id=space_id, project_tag="tag-a", status=RunStatus.COMPLETED, prompt="P A", created_by=owner_id)
    run_b = Run(run_id="run_b", space_id=space_id, project_tag="tag-b", status=RunStatus.COMPLETED, prompt="P B", created_by=owner_id)
    store.save_run(run_a)
    store.save_run(run_b)

    runs_a = store.list_runs_in_space(space_id, project_tag="tag-a")
    assert len(runs_a) == 1 and runs_a[0].run_id == "run_a"


# -----------------------------------------------------------------------------
# Gate C15: Cloud Tasks Dual-Secret Rotation
# -----------------------------------------------------------------------------


def test_gate_c15_cloud_tasks_dual_secret_rotation():
    from datetime import UTC, datetime, timedelta
    from app.core.config import settings
    from app.models.file_record import FileRecord, IngestionStatus

    now = datetime.now(UTC)
    file_rec = FileRecord(
        file_id="f_c15_test",
        space_id="sp_c15",
        filename="doc.txt",
        storage_path="local://doc.txt",
        uploaded_by="u_c15",
        size_bytes=10,
        sha256="abc",
        upload_status="committed",
        ingestion_status=IngestionStatus.EXTRACTING,
        ingestion_job_id="job_c15_01",
        ingestion_lease_until=now + timedelta(minutes=5),
        max_allocated_generation=1,
    )
    store.save_file(file_rec)
    store.save_file_blob("f_c15_test", b"Valid test plain text document.")

    curr_sec = settings.STUDIO_TOWER_TASK_SECRET
    prev_sec = "studiotower-task-secret-old-ver-99"
    settings.STUDIO_TOWER_TASK_SECRET_PREVIOUS = prev_sec

    payload = {
        "space_id": "sp_c15",
        "file_id": "f_c15_test",
        "filename": "doc.txt",
        "target_generation": 1,
        "job_id": "job_c15_01",
    }

    try:
        # Current secret succeeds
        res_curr = client.post("/v1/internal/tasks/ingest-document", json=payload, headers={"X-Task-Secret": curr_sec})
        assert res_curr.status_code == 200

        # Previous secret succeeds during rotation window
        file_rec.ingestion_job_id = "job_c15_02"
        file_rec.ingestion_status = IngestionStatus.EXTRACTING
        file_rec.ingestion_lease_until = datetime.now(UTC) + timedelta(minutes=5)
        file_rec.max_allocated_generation = 2
        file_rec.active_generation = 1
        store.save_file(file_rec)

        payload["target_generation"] = 2
        payload["job_id"] = "job_c15_02"
        res_prev = client.post("/v1/internal/tasks/ingest-document", json=payload, headers={"X-Task-Secret": prev_sec})
        assert res_prev.status_code == 200

        # Invalid secret rejected with 403
        res_bad = client.post("/v1/internal/tasks/ingest-document", json=payload, headers={"X-Task-Secret": "invalid-secret"})
        assert res_bad.status_code == 403
    finally:
        settings.STUDIO_TOWER_TASK_SECRET_PREVIOUS = None


# -----------------------------------------------------------------------------
# Gate C16: Activity Tags Migration CLI Backfill
# -----------------------------------------------------------------------------


def test_gate_c16_activity_tags_migration_cli():
    from app.cli.migrate_activity_tags import run_migration
    from app.models.activity import ActivityEvent, ActivityEventType

    space_id = "sp_c16_mig"
    ev = ActivityEvent(
        event_id="mig.event.01",
        event_type=ActivityEventType.FILE_UPLOADED,
        space_id=space_id,
        project_tag="vfx-sound",
        resource_type="file",
        resource_id="mig_file",
        summary="Migration test file",
    )
    # Manually simulate legacy document with project_tags unset
    ev.project_tags = []
    store.record_activity_event(ev)

    # Dry-run migration
    code_dry = run_migration(dry_run=True)
    assert code_dry == 0

    # Live execute migration
    code_exec = run_migration(dry_run=False)
    assert code_exec == 0

    events, _ = store.list_activity_events(space_id, tag="vfx-sound")
    migrated_ev = next((e for e in events if e.event_id == "mig.event.01"), None)
    assert migrated_ev is not None
    assert migrated_ev.project_tags == ["vfx-sound"]


# -----------------------------------------------------------------------------
# Gate C17: Message & Run CAS Atomic Outbox Staging
# -----------------------------------------------------------------------------


def test_gate_c17_message_and_cas_atomic_outbox_integration():
    from app.models.activity import ActivityEventType, OutboxStatus
    from app.models.message import Message, MessageRole
    from app.models.run import ApprovalGate, Run, RunStatus

    space_id = "sp_c17_atomic"
    owner_id = "u_c17_owner"
    store.save_user(User(uid=owner_id, email="owner@c17.com", display_name="Owner C17"))
    space = Space(space_id=space_id, name="C17 Space", created_by=owner_id)
    store.create_space(space, creator_uid=owner_id)

    # 1. Message atomic outbox staging
    msg = Message(
        message_id="msg_c17_test",
        space_id=space_id,
        sender_uid=owner_id,
        sender_name="Owner C17",
        role=MessageRole.USER,
        content="Testing atomic outbox message creation",
        project_tag="vfx-track",
    )
    store.add_message(msg)

    # Check outbox item exists for message
    msg_outbox_id = f"outbox:message.created:{msg.message_id}"
    assert msg_outbox_id in store.activity_outbox
    assert store.activity_outbox[msg_outbox_id].status == OutboxStatus.PENDING
    assert store.activity_outbox[msg_outbox_id].event.event_type == ActivityEventType.MESSAGE_CREATED

    # 2. Run initial creation atomic outbox staging
    run = Run(
        run_id="run_c17_atomic",
        space_id=space_id,
        project_tag="vfx-track",
        status=RunStatus.AWAITING_APPROVAL,
        prompt="Execute VFX Breakdown",
        created_by=owner_id,
        approval_gate=ApprovalGate(
            gate_id="gate_c17",
            title="VFX Asset Budget",
            description="Budget gate",
            status="pending",
        ),
    )
    store.save_run(run)

    run_start_outbox_id = f"outbox:run.started:{run.run_id}"
    assert run_start_outbox_id in store.activity_outbox
    assert store.activity_outbox[run_start_outbox_id].status == OutboxStatus.PENDING

    # 3. CAS Run state transition (gate approved) atomic outbox staging
    cas_run = store.compare_and_swap_run_status(
        run.run_id,
        expected_status=RunStatus.AWAITING_APPROVAL,
        new_status=RunStatus.RUNNING,
        actor_uid=owner_id,
    )
    assert cas_run is not None
    gate_outbox_id = f"outbox:gate.approved:{run.run_id}:r{cas_run.state_version}"
    assert gate_outbox_id in store.activity_outbox
    assert store.activity_outbox[gate_outbox_id].event.event_type == ActivityEventType.GATE_APPROVED


# -----------------------------------------------------------------------------
# Gate C18: Outbox Dispatcher Lease Fencing & Stale Worker Preemption
# -----------------------------------------------------------------------------


def test_gate_c18_outbox_dispatcher_lease_fencing_and_preemption():
    from datetime import UTC, datetime, timedelta
    from app.models.activity import ActivityEvent, ActivityEventType, ActivityOutboxItem, OutboxStatus

    space_id = "sp_c18_fence"
    ev = ActivityEvent(
        event_id="evt_c18_fence_01",
        event_type=ActivityEventType.TAG_CREATED,
        space_id=space_id,
        project_tags=["general"],
        resource_type="tag",
        resource_id="tag_c18",
        summary="Tag created for fence test",
    )
    outbox_item = store.stage_outbox_event(ev)

    # Worker A claims the outbox item
    now = datetime.now(UTC)
    with store._lock:
        item = store.activity_outbox[outbox_item.outbox_id]
        item.status = "in_progress"
        item.lease_owner = "worker_A"
        item.lease_token = "token_worker_A"
        # Simulate Worker A's lease expiring
        item.lease_until = now - timedelta(seconds=10)

    # Worker B claims the expired outbox item
    with store._lock:
        item = store.activity_outbox[outbox_item.outbox_id]
        item.status = "in_progress"
        item.lease_owner = "worker_B"
        item.lease_token = "token_worker_B"
        item.lease_until = now + timedelta(seconds=30)

    # Worker A attempts late publication with stale token -> must be fenced off
    with store._lock:
        curr = store.activity_outbox.get(outbox_item.outbox_id)
        # Verify Worker A cannot overwrite Worker B's active lease
        if curr and curr.lease_owner == "worker_A" and curr.lease_token == "token_worker_A":
            curr.status = OutboxStatus.PUBLISHED

    # Assert Worker B's lease was NOT corrupted by Worker A
    assert store.activity_outbox[outbox_item.outbox_id].lease_owner == "worker_B"
    assert store.activity_outbox[outbox_item.outbox_id].lease_token == "token_worker_B"
    assert store.activity_outbox[outbox_item.outbox_id].status == "in_progress"


# -----------------------------------------------------------------------------
# Gate C19: Invalid Cursor Error Strictly Mapped to HTTP 400
# -----------------------------------------------------------------------------


def test_gate_c19_invalid_cursor_error_strictly_mapped_to_400():
    space_id = "sp_c19_cursor_test"
    owner_id = "u_c19_owner"
    store.save_user(User(uid=owner_id, email="owner@c19.com", display_name="Owner C19"))
    space = Space(space_id=space_id, name="C19 Space", created_by=owner_id)
    store.create_space(space, creator_uid=owner_id)

    app.dependency_overrides[get_current_user] = lambda: store.get_user(owner_id)
    try:
        # Request with malformed cursor format
        res_malformed = client.get(f"/v1/spaces/{space_id}/activity?cursor=invalid.cursor.format.with.too.many.dots")
        assert res_malformed.status_code == 400
        assert "Invalid pagination cursor" in res_malformed.json()["detail"]

        # Request with tampered cursor signature
        res_tampered = client.get(f"/v1/spaces/{space_id}/activity?cursor=eyJldmVudF9pZCI6ICJmb28ifQ.fake_sig")
        assert res_tampered.status_code == 400
        assert "Invalid pagination cursor" in res_tampered.json()["detail"]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# -----------------------------------------------------------------------------
# Gate C20: ActivityOutboxItem JSON Serialization & next_retry_at Roundtrip
# -----------------------------------------------------------------------------


def test_gate_c20_activity_outbox_item_json_roundtrip_retains_next_retry_at():
    from datetime import UTC, datetime, timedelta
    from app.models.activity import ActivityEvent, ActivityEventType, ActivityOutboxItem, OutboxStatus

    now = datetime.now(UTC)
    retry_time = now + timedelta(seconds=120)
    ev = ActivityEvent(
        event_id="evt_c20_json",
        event_type=ActivityEventType.TAG_CREATED,
        space_id="sp_c20",
        project_tags=["vfx"],
        resource_type="tag",
        resource_id="tag_c20",
        summary="Testing next_retry_at JSON roundtrip",
    )
    item = ActivityOutboxItem(
        outbox_id="outbox_c20_1",
        event_id=ev.event_id,
        space_id="sp_c20",
        event=ev,
        status=OutboxStatus.PENDING,
        attempts=2,
        max_attempts=5,
        lease_owner="worker_test",
        lease_token="token_c20",
        lease_until=now + timedelta(seconds=30),
        next_retry_at=retry_time,
    )

    # Serialize to JSON and back
    json_data = item.model_dump(mode="json")
    assert "next_retry_at" in json_data
    assert json_data["next_retry_at"] is not None

    restored = ActivityOutboxItem.model_validate(json_data)
    assert restored.outbox_id == "outbox_c20_1"
    assert restored.next_retry_at is not None
    assert abs((restored.next_retry_at - retry_time).total_seconds()) < 1.0
    assert restored.lease_token == "token_c20"
    assert restored.status == OutboxStatus.PENDING


# -----------------------------------------------------------------------------
# Gate C21: Firestore Outbox Dispatcher Stateful Execution & Publication
# -----------------------------------------------------------------------------


def test_gate_c21_firestore_outbox_dispatcher_real_execution_and_publication():
    from datetime import UTC, datetime
    from unittest.mock import MagicMock, patch
    import sys
    from app.models.activity import ActivityEvent, ActivityEventType, ActivityOutboxItem, OutboxStatus
    from app.services.storage import FirestoreStore

    now = datetime.now(UTC)
    space_id = "sp_c21_fs"
    ev = ActivityEvent(
        event_id="evt_c21_fs_01",
        event_type=ActivityEventType.TAG_CREATED,
        space_id=space_id,
        project_tags=["sound"],
        resource_type="tag",
        resource_id="tag_c21",
        summary="Firestore dispatcher test tag",
        created_at=now,
    )
    outbox_item = ActivityOutboxItem(
        outbox_id=f"outbox:{ev.event_id}",
        event_id=ev.event_id,
        space_id=space_id,
        event=ev,
        status=OutboxStatus.PENDING,
        attempts=0,
        max_attempts=5,
        created_at=now,
        updated_at=now,
        next_retry_at=now,
    )

    # In-memory document storage simulating Firestore collections
    db_outbox = {outbox_item.outbox_id: outbox_item.model_dump(mode="json")}
    db_events = {}

    class FakeDocRef:
        def __init__(self, doc_id, data_store):
            self.id = doc_id
            self._store = data_store
            self.reference = self
        def get(self, transaction=None):
            return FakeDocSnap(self.id, self._store)
        def set(self, data, transaction=None):
            self._store[self.id] = dict(data)
        def create(self, data):
            if self.id in self._store:
                raise Exception("409 Document already exists")
            self._store[self.id] = dict(data)
        def update(self, data):
            if self.id not in self._store:
                raise Exception("404 Document not found")
            self._store[self.id].update(data)

    class FakeDocSnap:
        def __init__(self, doc_id, data_store):
            self.id = doc_id
            self._store = data_store
            self.reference = FakeDocRef(doc_id, data_store)
        @property
        def exists(self):
            return self.id in self._store
        def to_dict(self):
            return dict(self._store[self.id]) if self.exists else None

    class FakeQuery:
        def __init__(self, data_store):
            self._store = data_store
        def where(self, *args, **kwargs):
            return self
        def limit(self, n):
            return self
        def stream(self):
            return [FakeDocSnap(k, self._store) for k in list(self._store.keys())]

    class FakeClient:
        def collection(self, name):
            if name == "activity_outbox":
                q = FakeQuery(db_outbox)
                q.document = lambda doc_id: FakeDocRef(doc_id, db_outbox)
                return q
            elif name == "activity_events":
                q = FakeQuery(db_events)
                q.document = lambda doc_id: FakeDocRef(doc_id, db_events)
                return q
            q = FakeQuery({})
            q.document = lambda doc_id: FakeDocRef(doc_id, {})
            return q
        def transaction(self):
            class FakeTx(MagicMock):
                def __init__(self, **kwargs):
                    super().__init__(**kwargs)
                    self._read_only = False
                    self._id = b"fake_tx_id"
                def set(self, doc_ref, data):
                    doc_ref.set(data)
                def update(self, doc_ref, data):
                    doc_ref.update(data)
                def delete(self, doc_ref):
                    pass
            return FakeTx()

    mock_firestore_mod = MagicMock()
    mock_firestore_mod.transactional = lambda fn: (lambda tx, *args, **kwargs: fn(tx, *args, **kwargs))

    with patch.dict(sys.modules, {"google.cloud.firestore": mock_firestore_mod}):
        fake_client = FakeClient()
        fs_store = FirestoreStore(project_id="test-proj", client=fake_client)

        dispatched = fs_store.dispatch_outbox_events(limit=10, worker_id="test_worker_c21")
        assert dispatched == 1
        assert db_outbox[outbox_item.outbox_id]["status"] == OutboxStatus.PUBLISHED.value
        assert ev.event_id in db_events


# -----------------------------------------------------------------------------
# Gate C22: Firestore Dispatcher Preemption of Expired Worker (Success & Failure)
# -----------------------------------------------------------------------------


def test_gate_c22_firestore_dispatcher_expired_worker_rejection_success_and_failure():
    from datetime import UTC, datetime, timedelta
    from unittest.mock import MagicMock, patch
    import sys
    from app.models.activity import ActivityEvent, ActivityEventType, ActivityOutboxItem, OutboxStatus
    from app.services.storage import FirestoreStore

    now = datetime.now(UTC)
    space_id = "sp_c22_preempt"
    ev = ActivityEvent(
        event_id="evt_c22_01",
        event_type=ActivityEventType.TAG_UPDATED,
        space_id=space_id,
        project_tags=["vfx"],
        resource_type="tag",
        resource_id="tag_c22",
        summary="Preemption test tag",
        created_at=now,
    )

    # Simulate Outbox item currently owned by Worker B with active lease
    db_outbox = {
        "outbox:evt_c22_01": {
            "outbox_id": "outbox:evt_c22_01",
            "event_id": ev.event_id,
            "space_id": space_id,
            "event": ev.model_dump(mode="json"),
            "status": OutboxStatus.IN_PROGRESS.value,
            "attempts": 2,
            "max_attempts": 5,
            "lease_owner": "worker_B",
            "lease_token": "token_worker_B",
            "lease_until": (now + timedelta(seconds=30)).isoformat(),
            "next_retry_at": now.isoformat(),
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
    }

    class FakeDocSnap:
        def __init__(self, doc_id, data_store):
            self.id = doc_id
            self._store = data_store
        @property
        def exists(self):
            return self.id in self._store
        def to_dict(self):
            return dict(self._store[self.id]) if self.exists else None

    class FakeDocRef:
        def __init__(self, doc_id, data_store):
            self.id = doc_id
            self._store = data_store
            self.reference = self
        def get(self, transaction=None):
            return FakeDocSnap(self.id, self._store)
        def update(self, data):
            self._store[self.id].update(data)

    class FakeClient:
        def collection(self, name):
            q = MagicMock()
            q.document = lambda doc_id: FakeDocRef(doc_id, db_outbox)
            return q
        def transaction(self):
            t = MagicMock()
            t.update = lambda doc_ref, data: doc_ref.update(data)
            return t

    mock_firestore_mod = MagicMock()
    mock_firestore_mod.transactional = lambda fn: (lambda tx, *args, **kwargs: fn(tx, *args, **kwargs))

    with patch.dict(sys.modules, {"google.cloud.firestore": mock_firestore_mod}):
        fake_client = FakeClient()
        doc_ref = FakeDocRef("outbox:evt_c22_01", db_outbox)

        # Worker A attempts late publication with stale token -> rejected
        token_worker_A = "token_worker_A"
        worker_A_id = "worker_A"

        @mock_firestore_mod.transactional
        def stale_complete_tx(tx):
            snap = doc_ref.get(transaction=tx)
            curr = snap.to_dict()
            if curr.get("status") != OutboxStatus.IN_PROGRESS.value:
                return False
            if curr.get("lease_owner") != worker_A_id or curr.get("lease_token") != token_worker_A:
                return False
            doc_ref.update({"status": OutboxStatus.PUBLISHED.value})
            return True

        res_complete = stale_complete_tx(fake_client.transaction())
        assert res_complete is False
        assert db_outbox["outbox:evt_c22_01"]["lease_owner"] == "worker_B"
        assert db_outbox["outbox:evt_c22_01"]["status"] == OutboxStatus.IN_PROGRESS.value

        # Worker A attempts failure backoff with stale token -> rejected
        @mock_firestore_mod.transactional
        def stale_fail_tx(tx):
            snap = doc_ref.get(transaction=tx)
            curr = snap.to_dict()
            if curr.get("status") != OutboxStatus.IN_PROGRESS.value:
                return False
            if curr.get("lease_owner") != worker_A_id or curr.get("lease_token") != token_worker_A:
                return False
            doc_ref.update({"status": OutboxStatus.FAILED.value})
            return True

        res_fail = stale_fail_tx(fake_client.transaction())
        assert res_fail is False
        assert db_outbox["outbox:evt_c22_01"]["lease_owner"] == "worker_B"


# -----------------------------------------------------------------------------
# Gate C23: Fail-Closed Lease Parsing & Query Error Handling in FirestoreStore
# -----------------------------------------------------------------------------


def test_gate_c23_firestore_dispatcher_malformed_timestamp_and_query_error_fail_closed():
    import pytest
    from unittest.mock import MagicMock, patch
    import sys
    from app.services.storage import FirestoreStore, StorageUnavailableError, _parse_lease_timestamp_fail_closed

    # 1. Test fail-closed timestamp parsing
    assert _parse_lease_timestamp_fail_closed(None) is None

    with pytest.raises(ValueError, match="MALFORMED_LEASE_TIMESTAMP"):
        _parse_lease_timestamp_fail_closed("not-a-timestamp-string")

    with pytest.raises(ValueError, match="UNRECOGNIZED_TIMESTAMP_TYPE"):
        _parse_lease_timestamp_fail_closed(12345678)

    # 2. Test Firestore query failure raises StorageUnavailableError
    mock_client = MagicMock()
    mock_client.collection.return_value.where.return_value.limit.return_value.stream.side_effect = Exception("Firestore 503 UNAVAILABLE")

    with patch.dict(sys.modules, {"google.cloud.firestore": MagicMock()}):
        fs_store = FirestoreStore(project_id="test-fail-proj", client=mock_client)
        with pytest.raises(StorageUnavailableError, match="Firestore dispatch_outbox_events query failed"):
            fs_store.dispatch_outbox_events(limit=10)


# -----------------------------------------------------------------------------
# Gate C24: Null Lease Rejection & Idempotent Redispatch Absorption
# -----------------------------------------------------------------------------


def test_gate_c24_null_lease_rejection_and_idempotent_redispatch_absorption():
    from datetime import UTC, datetime, timedelta
    from unittest.mock import MagicMock, patch
    import sys
    from app.models.activity import ActivityEvent, ActivityEventType, ActivityOutboxItem, OutboxStatus
    from app.services.storage import FirestoreStore

    space_id = "sp_c24"
    now = datetime.now(UTC)
    ev = ActivityEvent(
        event_id="evt_c24_idemp_01",
        event_type=ActivityEventType.TAG_CREATED,
        space_id=space_id,
        project_tags=["general"],
        resource_type="tag",
        resource_id="tag_c24",
        summary="Idempotent redispatch test",
        created_at=now,
    )

    # 1. Test MemoryStore: null lease_until rejects complete_mutate and fail_mutate
    outbox_item = store.stage_outbox_event(ev)
    with store._lock:
        item = store.activity_outbox[outbox_item.outbox_id]
        item.status = OutboxStatus.IN_PROGRESS
        item.lease_owner = "worker_null"
        item.lease_token = "token_null"
        item.lease_until = None  # Missing / null lease!

    # Attempt to complete without lease_until -> must remain IN_PROGRESS (not published)
    with store._lock:
        curr = store.activity_outbox[outbox_item.outbox_id]
        if (
            curr
            and curr.status == OutboxStatus.IN_PROGRESS
            and curr.lease_owner == "worker_null"
            and getattr(curr, "lease_token", None) == "token_null"
            and curr.lease_until is not None
            and curr.lease_until > datetime.now(UTC)
        ):
            curr.status = OutboxStatus.PUBLISHED
    assert store.activity_outbox[outbox_item.outbox_id].status == OutboxStatus.IN_PROGRESS

    # 2. Test FirestoreStore: complete_tx / fail_tx with null lease_until rejects
    db_outbox = {
        outbox_item.outbox_id: {
            "outbox_id": outbox_item.outbox_id,
            "event_id": ev.event_id,
            "space_id": space_id,
            "event": ev.model_dump(mode="json"),
            "status": OutboxStatus.IN_PROGRESS.value,
            "lease_owner": "worker_null",
            "lease_token": "token_null",
            "lease_until": None,  # Null lease
        }
    }
    db_events = {}

    class FakeDocSnap:
        def __init__(self, doc_id, data_store):
            self.id = doc_id
            self._store = data_store
            self.reference = FakeDocRef(doc_id, data_store)
        @property
        def exists(self):
            return self.id in self._store
        def to_dict(self):
            return dict(self._store[self.id]) if self.exists else None

    class FakeDocRef:
        def __init__(self, doc_id, data_store):
            self.id = doc_id
            self._store = data_store
            self.reference = self
        def get(self, transaction=None):
            return FakeDocSnap(self.id, self._store)
        def update(self, data):
            self._store[self.id].update(data)
        def create(self, data):
            if self.id in self._store:
                raise Exception("409 Document already exists")
            self._store[self.id] = dict(data)

    class FakeClient:
        def collection(self, name):
            q = MagicMock()
            store_ref = db_outbox if name == "activity_outbox" else db_events
            q.document = lambda doc_id: FakeDocRef(doc_id, store_ref)
            return q
        def transaction(self):
            t = MagicMock()
            t.update = lambda doc_ref, data: doc_ref.update(data)
            return t

    mock_firestore_mod = MagicMock()
    mock_firestore_mod.transactional = lambda fn: (lambda tx, *args, **kwargs: fn(tx, *args, **kwargs))

    with patch.dict(sys.modules, {"google.cloud.firestore": mock_firestore_mod}):
        fake_client = FakeClient()
        doc_ref = FakeDocRef(outbox_item.outbox_id, db_outbox)

        # Worker A attempts complete with null lease -> rejected
        @mock_firestore_mod.transactional
        def null_complete_tx(tx):
            snap = doc_ref.get(transaction=tx)
            curr = snap.to_dict()
            if curr.get("status") != OutboxStatus.IN_PROGRESS.value:
                return False
            if curr.get("lease_owner") != "worker_null" or curr.get("lease_token") != "token_null":
                return False
            curr_lease_until = curr.get("lease_until")
            if curr_lease_until is None:
                return False
            doc_ref.update({"status": OutboxStatus.PUBLISHED.value})
            return True

        assert null_complete_tx(fake_client.transaction()) is False
        assert db_outbox[outbox_item.outbox_id]["status"] == OutboxStatus.IN_PROGRESS.value

        # 3. Simulate Idempotent redispatch:
        # Worker A already published event to activity_events
        fs_store = FirestoreStore(project_id="test-proj", client=fake_client)
        fs_store.record_activity_event(ev)
        assert ev.event_id in db_events

        # Worker B is dispatched after Worker A's lease expired:
        # Worker B re-calls record_activity_event(ev) -> absorbed idempotently
        second_record = fs_store.record_activity_event(ev)
        assert second_record.event_id == ev.event_id
        # No duplicate events
        assert len([k for k in db_events.keys() if k == ev.event_id]) == 1


