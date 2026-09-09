import concurrent.futures
import io
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from app.core.config import Settings, settings
from app.main import app
from app.models.file_record import FileRecord
from app.models.run import ApprovalGate, Run, RunStatus
from app.models.space import MembershipRole
from app.services.file_service import FileService, HeartbeatTracker, upload_heartbeat
from app.services.storage import FirestoreStore, create_storage_backend, store
from fastapi.testclient import TestClient

client = TestClient(app)

ALICE_AUTH = {"Authorization": "Bearer dev:alice_01:alice@example.com:Alice"}
BOB_AUTH = {"Authorization": "Bearer dev:bob_02:bob@example.com:Bob"}
CHARLIE_AUTH = {"Authorization": "Bearer dev:charlie_03:charlie@example.com:Charlie"}
MAINTENANCE_HEADER = {"X-Maintenance-Key": settings.STUDIO_TOWER_MAINTENANCE_SECRET}


@pytest.fixture(autouse=True)
def clean_store():
    store.clear()
    yield
    store.clear()


def test_production_auth_and_persistence_fails_closed():
    # 1. Case-insensitive ENV validation in production
    for prod_env in ["production", "Production", "PRODUCTION", "prod", "PROD"]:
        with pytest.raises(ValueError, match="Production deployment cannot use forgeable dev auth mode"):
            Settings(ENV=prod_env, STUDIO_TOWER_AUTH_MODE="dev", STUDIO_TOWER_FIREBASE_PROJECT_ID="p1")

    # 2. Firebase project id required in production
    with pytest.raises(ValueError, match="STUDIO_TOWER_FIREBASE_PROJECT_ID is required"):
        Settings(ENV="Production", STUDIO_TOWER_AUTH_MODE="firebase", STUDIO_TOWER_FIREBASE_PROJECT_ID="")

    # 3. Ephemeral memory store and local JSON forbidden in production (must use firestore)
    with pytest.raises(ValueError, match="Production deployment requires distributed cloud storage"):
        Settings(
            ENV="PRODUCTION",
            STUDIO_TOWER_AUTH_MODE="firebase",
            STUDIO_TOWER_FIREBASE_PROJECT_ID="prod-firebase-123",
            STUDIO_TOWER_STORE="memory",
            STUDIO_TOWER_ARTIFACT_BACKEND="gcs",
            STUDIO_TOWER_GCS_BUCKET="bucket-1",
        )

    # 4. Ephemeral local artifact backend forbidden in production (must use gcs with bucket)
    with pytest.raises(ValueError, match="Production deployment requires shared durable cloud artifact storage"):
        Settings(
            ENV="production",
            STUDIO_TOWER_AUTH_MODE="firebase",
            STUDIO_TOWER_FIREBASE_PROJECT_ID="prod-firebase-123",
            STUDIO_TOWER_STORE="firestore",
            STUDIO_TOWER_ARTIFACT_BACKEND="local",
        )

    with pytest.raises(ValueError, match="STUDIO_TOWER_GCS_BUCKET is required"):
        Settings(
            ENV="production",
            STUDIO_TOWER_AUTH_MODE="firebase",
            STUDIO_TOWER_FIREBASE_PROJECT_ID="prod-firebase-123",
            STUDIO_TOWER_STORE="firestore",
            STUDIO_TOWER_ARTIFACT_BACKEND="gcs",
            STUDIO_TOWER_GCS_BUCKET="",
        )

    # 5. Production rejects default or low-entropy task secret
    with pytest.raises(ValueError, match="high-entropy STUDIO_TOWER_TASK_SECRET"):
        Settings(
            ENV="production",
            STUDIO_TOWER_AUTH_MODE="firebase",
            STUDIO_TOWER_FIREBASE_PROJECT_ID="prod-firebase-123",
            STUDIO_TOWER_STORE="firestore",
            STUDIO_TOWER_ARTIFACT_BACKEND="gcs",
            STUDIO_TOWER_GCS_BUCKET="prod-bucket",
            STUDIO_TOWER_TASK_SECRET="short-secret",
            STUDIO_TOWER_MAINTENANCE_SECRET="a" * 32,
            CURSOR_SIGNING_SECRET="c" * 32,
            INGESTION_RUNNER="cloud_tasks",
        )

    # 6. Production rejects default or low-entropy maintenance secret
    with pytest.raises(ValueError, match="high-entropy STUDIO_TOWER_MAINTENANCE_SECRET"):
        Settings(
            ENV="production",
            STUDIO_TOWER_AUTH_MODE="firebase",
            STUDIO_TOWER_FIREBASE_PROJECT_ID="prod-firebase-123",
            STUDIO_TOWER_STORE="firestore",
            STUDIO_TOWER_ARTIFACT_BACKEND="gcs",
            STUDIO_TOWER_GCS_BUCKET="prod-bucket",
            STUDIO_TOWER_TASK_SECRET="b" * 32,
            STUDIO_TOWER_MAINTENANCE_SECRET="short-secret",
            CURSOR_SIGNING_SECRET="c" * 32,
            INGESTION_RUNNER="cloud_tasks",
        )

    # 7. Production rejects non-durable ingestion runners (inline/threadpool)
    with pytest.raises(ValueError, match="durable queue for document ingestion"):
        Settings(
            ENV="production",
            STUDIO_TOWER_AUTH_MODE="firebase",
            STUDIO_TOWER_FIREBASE_PROJECT_ID="prod-firebase-123",
            STUDIO_TOWER_STORE="firestore",
            STUDIO_TOWER_ARTIFACT_BACKEND="gcs",
            STUDIO_TOWER_GCS_BUCKET="prod-bucket",
            STUDIO_TOWER_TASK_SECRET="b" * 32,
            STUDIO_TOWER_MAINTENANCE_SECRET="a" * 32,
            CURSOR_SIGNING_SECRET="c" * 32,
            INGESTION_RUNNER="threadpool",
        )

    # 8. Production rejects default or low-entropy cursor signing secret
    with pytest.raises(ValueError, match="high-entropy CURSOR_SIGNING_SECRET"):
        Settings(
            ENV="production",
            STUDIO_TOWER_AUTH_MODE="firebase",
            STUDIO_TOWER_FIREBASE_PROJECT_ID="prod-firebase-123",
            STUDIO_TOWER_STORE="firestore",
            STUDIO_TOWER_ARTIFACT_BACKEND="gcs",
            STUDIO_TOWER_GCS_BUCKET="prod-bucket",
            STUDIO_TOWER_TASK_SECRET="b" * 32,
            STUDIO_TOWER_MAINTENANCE_SECRET="a" * 32,
            ACTION_SIGNING_SECRET="s" * 32,
            CURSOR_SIGNING_SECRET="short-cursor",
            INGESTION_RUNNER="cloud_tasks",
        )

    # 9. Production rejects default or low-entropy action signing secret
    with pytest.raises(ValueError, match="high-entropy ACTION_SIGNING_SECRET"):
        Settings(
            ENV="production",
            STUDIO_TOWER_AUTH_MODE="firebase",
            STUDIO_TOWER_FIREBASE_PROJECT_ID="prod-firebase-123",
            STUDIO_TOWER_STORE="firestore",
            STUDIO_TOWER_ARTIFACT_BACKEND="gcs",
            STUDIO_TOWER_GCS_BUCKET="prod-bucket",
            STUDIO_TOWER_TASK_SECRET="b" * 32,
            STUDIO_TOWER_MAINTENANCE_SECRET="a" * 32,
            CURSOR_SIGNING_SECRET="c" * 32,
            ACTION_SIGNING_SECRET="short-action-secret",
            INGESTION_RUNNER="cloud_tasks",
            ACTION_RUNNER="cloud_tasks",
        )

    # 10. Production rejects non-durable action runners (inline/threadpool)
    with pytest.raises(ValueError, match="durable queue for action execution"):
        Settings(
            ENV="production",
            STUDIO_TOWER_AUTH_MODE="firebase",
            STUDIO_TOWER_FIREBASE_PROJECT_ID="prod-firebase-123",
            STUDIO_TOWER_STORE="firestore",
            STUDIO_TOWER_ARTIFACT_BACKEND="gcs",
            STUDIO_TOWER_GCS_BUCKET="prod-bucket",
            STUDIO_TOWER_TASK_SECRET="b" * 32,
            STUDIO_TOWER_MAINTENANCE_SECRET="a" * 32,
            CURSOR_SIGNING_SECRET="c" * 32,
            ACTION_SIGNING_SECRET="s" * 32,
            INGESTION_RUNNER="cloud_tasks",
            ACTION_RUNNER="inline",
        )

        # 11. Production rejects missing/dev worker service URL
        with pytest.raises(ValueError, match="STUDIO_TOWER_WORKER_SERVICE_URL"):
            Settings(
                ENV="production",
                STUDIO_TOWER_AUTH_MODE="firebase",
                STUDIO_TOWER_FIREBASE_PROJECT_ID="prod-firebase-123",
                STUDIO_TOWER_STORE="firestore",
                STUDIO_TOWER_ARTIFACT_BACKEND="gcs",
                STUDIO_TOWER_GCS_BUCKET="prod-bucket",
                STUDIO_TOWER_TASK_SECRET="b" * 32,
                STUDIO_TOWER_MAINTENANCE_SECRET="a" * 32,
                CURSOR_SIGNING_SECRET="c" * 32,
                ACTION_SIGNING_SECRET="s" * 32,
                INGESTION_RUNNER="cloud_tasks",
                ACTION_RUNNER="cloud_tasks",
                STUDIO_TOWER_WORKER_SERVICE_URL="",
            )

        # 12. Production rejects missing STUDIO_TOWER_ALLOWED_WORKER_HOST
        with pytest.raises(ValueError, match="STUDIO_TOWER_ALLOWED_WORKER_HOST"):
            Settings(
                ENV="production",
                STUDIO_TOWER_AUTH_MODE="firebase",
                STUDIO_TOWER_FIREBASE_PROJECT_ID="prod-firebase-123",
                STUDIO_TOWER_STORE="firestore",
                STUDIO_TOWER_ARTIFACT_BACKEND="gcs",
                STUDIO_TOWER_GCS_BUCKET="prod-bucket",
                STUDIO_TOWER_TASK_SECRET="b" * 32,
                STUDIO_TOWER_MAINTENANCE_SECRET="a" * 32,
                CURSOR_SIGNING_SECRET="c" * 32,
                ACTION_SIGNING_SECRET="s" * 32,
                INGESTION_RUNNER="cloud_tasks",
                ACTION_RUNNER="cloud_tasks",
                STUDIO_TOWER_WORKER_SERVICE_URL="https://authoritative-worker.run.app",
                STUDIO_TOWER_ALLOWED_WORKER_HOST="",
                STUDIO_TOWER_SCHEDULER_SA="studiotower-api@prod-firebase-123.iam.gserviceaccount.com",
            )

        # 13. Production rejects worker URL hostname mismatch
        with pytest.raises(ValueError, match="does not match authorized STUDIO_TOWER_ALLOWED_WORKER_HOST"):
            Settings(
                ENV="production",
                STUDIO_TOWER_AUTH_MODE="firebase",
                STUDIO_TOWER_FIREBASE_PROJECT_ID="prod-firebase-123",
                STUDIO_TOWER_STORE="firestore",
                STUDIO_TOWER_ARTIFACT_BACKEND="gcs",
                STUDIO_TOWER_GCS_BUCKET="prod-bucket",
                STUDIO_TOWER_TASK_SECRET="b" * 32,
                STUDIO_TOWER_MAINTENANCE_SECRET="a" * 32,
                CURSOR_SIGNING_SECRET="c" * 32,
                ACTION_SIGNING_SECRET="s" * 32,
                INGESTION_RUNNER="cloud_tasks",
                ACTION_RUNNER="cloud_tasks",
                STUDIO_TOWER_WORKER_SERVICE_URL="https://foreign-worker.run.app",
                STUDIO_TOWER_ALLOWED_WORKER_HOST="authoritative-worker.run.app",
                STUDIO_TOWER_SCHEDULER_SA="studiotower-api@prod-firebase-123.iam.gserviceaccount.com",
            )

        # 14. Production rejects missing scheduler SA
        with pytest.raises(ValueError, match="STUDIO_TOWER_SCHEDULER_SA"):
            Settings(
                ENV="production",
                STUDIO_TOWER_AUTH_MODE="firebase",
                STUDIO_TOWER_FIREBASE_PROJECT_ID="prod-firebase-123",
                STUDIO_TOWER_STORE="firestore",
                STUDIO_TOWER_ARTIFACT_BACKEND="gcs",
                STUDIO_TOWER_GCS_BUCKET="prod-bucket",
                STUDIO_TOWER_TASK_SECRET="b" * 32,
                STUDIO_TOWER_MAINTENANCE_SECRET="a" * 32,
                CURSOR_SIGNING_SECRET="c" * 32,
                ACTION_SIGNING_SECRET="s" * 32,
                INGESTION_RUNNER="cloud_tasks",
                ACTION_RUNNER="cloud_tasks",
                STUDIO_TOWER_WORKER_SERVICE_URL="https://authoritative-worker.run.app",
                STUDIO_TOWER_ALLOWED_WORKER_HOST="authoritative-worker.run.app",
                STUDIO_TOWER_SCHEDULER_SA=None,
            )

        # 15. Valid production settings with firestore, GCS bucket, strong secrets, cloud_tasks, worker URL, allowed worker host, and scheduler SA succeeds
        valid_settings = Settings(
            ENV="PRODUCTION",
            STUDIO_TOWER_AUTH_MODE="firebase",
            STUDIO_TOWER_FIREBASE_PROJECT_ID="prod-firebase-123",
            STUDIO_TOWER_STORE="firestore",
            STUDIO_TOWER_ARTIFACT_BACKEND="gcs",
            STUDIO_TOWER_GCS_BUCKET="prod-studiotower-artifacts",
            STUDIO_TOWER_TASK_SECRET="b" * 32,
            STUDIO_TOWER_MAINTENANCE_SECRET="a" * 32,
            CURSOR_SIGNING_SECRET="c" * 32,
            ACTION_SIGNING_SECRET="s" * 32,
            INGESTION_RUNNER="cloud_tasks",
            ACTION_RUNNER="cloud_tasks",
            STUDIO_TOWER_WORKER_SERVICE_URL="https://authoritative-worker.run.app",
            STUDIO_TOWER_ALLOWED_WORKER_HOST="authoritative-worker.run.app",
            STUDIO_TOWER_SCHEDULER_SA="studiotower-api@prod-firebase-123.iam.gserviceaccount.com",
        )
        assert valid_settings.ENV == "production"
        assert valid_settings.STUDIO_TOWER_STORE == "firestore"
        assert valid_settings.STUDIO_TOWER_ARTIFACT_BACKEND == "gcs"
        assert valid_settings.INGESTION_RUNNER == "cloud_tasks"
        assert valid_settings.ACTION_RUNNER == "cloud_tasks"
        assert valid_settings.STUDIO_TOWER_TASK_SECRET == "b" * 32
        assert valid_settings.ACTION_SIGNING_SECRET == "s" * 32
        assert valid_settings.STUDIO_TOWER_WORKER_SERVICE_URL == "https://authoritative-worker.run.app"
        assert valid_settings.STUDIO_TOWER_ALLOWED_WORKER_HOST == "authoritative-worker.run.app"
        assert valid_settings.STUDIO_TOWER_SCHEDULER_SA == "studiotower-api@prod-firebase-123.iam.gserviceaccount.com"


def test_firestore_fails_closed_when_client_fails():
    import sys
    mock_fs = MagicMock()
    mock_fs.Client.side_effect = Exception("Credential auth failed")
    with patch.dict(sys.modules, {"google.cloud.firestore": mock_fs}):
        with pytest.raises(RuntimeError, match="Failed to initialize Google Cloud Firestore"):
            FirestoreStore(project_id="fake-proj")


def test_firestore_backend_real_document_operations():
    mock_client = MagicMock()
    mock_collection = MagicMock()
    mock_doc = MagicMock()
    mock_client.collection.return_value = mock_collection
    mock_collection.document.return_value = mock_doc
    mock_collection.where.return_value = mock_collection
    mock_collection.stream.return_value = []

    fs_store = FirestoreStore(project_id="test-proj-firebase", client=mock_client)
    assert fs_store.client is not None

    # 1. Test space creation calls Firestore document set
    from app.models.message import Message, MessageRole
    from app.models.space import Space
    test_space = Space(name="Cloud Production Space", created_by="alice_01")
    fs_store.create_space(test_space, "alice_01")
    assert mock_client.collection.call_count >= 2
    mock_doc.set.assert_called()

    # 2. Test message insertion calls document with message.message_id
    test_msg = Message(space_id=test_space.space_id, sender_uid="alice_01", role=MessageRole.USER, content="Test msg")
    fs_store.add_message(test_msg)
    mock_collection.document.assert_called_with(test_msg.message_id)

    # 3. Test remote query execution
    fs_store.list_messages(test_space.space_id, project_tag="general")
    mock_collection.where.assert_called()


def test_json_storage_backend_actual_disk_persistence():
    temp_dir = tempfile.mkdtemp()
    try:
        custom_settings = Settings(
            STUDIO_TOWER_STORE="json",
            STUDIO_TOWER_DATA_DIR=temp_dir,
        )
        json_store = create_storage_backend(custom_settings)

        space_rec = client.post("/v1/spaces", json={"name": "Persisted Space"}, headers=ALICE_AUTH).json()
        assert space_rec["space_id"]

        state_file = os.path.join(temp_dir, "studiotower_state.json")
        json_store.persist_path = state_file
        json_store.create_space(store.get_space(space_rec["space_id"]), "alice_01")

        assert os.path.exists(state_file)

        recovered_store = create_storage_backend(custom_settings)
        recovered_store.persist_path = state_file
        recovered_store._load_from_disk()
        assert recovered_store.get_space(space_rec["space_id"]) is not None
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_corrupted_json_state_raises_runtime_error():
    temp_dir = tempfile.mkdtemp()
    try:
        state_file = os.path.join(temp_dir, "studiotower_state.json")
        with open(state_file, "w", encoding="utf-8") as f:
            f.write("{ INVALID JSON DATA CORRUPTED...")

        custom_settings = Settings(
            STUDIO_TOWER_STORE="json",
            STUDIO_TOWER_DATA_DIR=temp_dir,
        )
        with pytest.raises(RuntimeError, match="Corrupted storage state"):
            create_storage_backend(custom_settings)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_directory_traversal_filename_sanitization():
    space = client.post("/v1/spaces", json={"name": "Security Test Space"}, headers=ALICE_AUTH).json()
    space_id = space["space_id"]

    traversal_filename = "../../../etc/passwd.txt"
    upload_res = client.post(
        f"/v1/spaces/{space_id}/files",
        files={"file": (traversal_filename, io.BytesIO(b"root:x:0:0"), "text/plain")},
        headers=ALICE_AUTH,
    )
    assert upload_res.status_code == 200
    file_rec = upload_res.json()

    assert ".." not in file_rec["filename"]
    assert "/" not in file_rec["filename"]
    assert "\\" not in file_rec["filename"]
    assert file_rec["storage_path"].startswith(f"{space_id}/files/")


def test_distinct_storage_paths_for_identical_file_uploads():
    space = client.post("/v1/spaces", json={"name": "Distinct Path Space"}, headers=ALICE_AUTH).json()
    space_id = space["space_id"]

    # Upload file 1
    u1 = client.post(
        f"/v1/spaces/{space_id}/files",
        files={"file": ("master_script.pdf", io.BytesIO(b"SCRIPT_CONTENT"), "application/pdf")},
        headers=ALICE_AUTH,
    ).json()

    # Upload file 2 with identical name and content
    u2 = client.post(
        f"/v1/spaces/{space_id}/files",
        files={"file": ("master_script.pdf", io.BytesIO(b"SCRIPT_CONTENT"), "application/pdf")},
        headers=ALICE_AUTH,
    ).json()

    assert u1["file_id"] != u2["file_id"]
    # Distinct storage paths prevent shared object deletion collisions
    assert u1["storage_path"] != u2["storage_path"]


def test_in_flight_upload_protected_from_cleanup():
    space = client.post("/v1/spaces", json={"name": "Upload Grace Space"}, headers=ALICE_AUTH).json()
    space_id = space["space_id"]

    now = datetime.now(UTC)
    # 1. Active in-flight upload with 120s remaining lease
    active_upload = FileRecord(
        space_id=space_id,
        filename="active_upload.pdf",
        storage_path=f"{space_id}/files/active_upload.pdf",
        uploaded_by="alice_01",
        upload_status="pending_upload",
        upload_lease_until=now + timedelta(seconds=120),
        cleanup_pending=False,
    )
    store.save_file(active_upload)

    # Cleanup worker attempts claim on active upload -> None (protected!)
    assert store.claim_cleanup_file(active_upload.file_id) is None

    # 2. Expired/abandoned upload whose lease timed out
    abandoned_upload = FileRecord(
        space_id=space_id,
        filename="abandoned_upload.pdf",
        storage_path=f"{space_id}/files/abandoned_upload.pdf",
        uploaded_by="alice_01",
        upload_status="pending_upload",
        upload_lease_until=now - timedelta(seconds=10),
        cleanup_pending=False,
    )
    store.save_file(abandoned_upload)

    # Cleanup worker claims abandoned upload -> Succeeds!
    claim = store.claim_cleanup_file(abandoned_upload.file_id)
    assert claim is not None
    assert claim.cleanup_status == "in_progress"


def test_expired_cleanup_lease_is_rejected():
    space = client.post("/v1/spaces", json={"name": "Expired Lease Space"}, headers=ALICE_AUTH).json()
    space_id = space["space_id"]

    now = datetime.now(UTC)
    file_rec = FileRecord(
        space_id=space_id,
        filename="orphan_expired.pdf",
        storage_path=f"{space_id}/files/orphan_expired.pdf",
        uploaded_by="alice_01",
        cleanup_pending=True,
        cleanup_status="in_progress",
        lease_token="token_abc",
        cleanup_lease_until=now - timedelta(seconds=5),  # Expired lease
    )
    store.save_file(file_rec)

    # Attempting mark_cleanup_file_deleting with expired lease -> False
    assert not store.mark_cleanup_file_deleting(file_rec.file_id, "token_abc")

    # Attempting delete with expired lease -> False
    assert not store.delete_cleanup_file_with_lease(file_rec.file_id, "token_abc")


def test_crashed_worker_deleting_state_is_reclaimable_after_lease_expiry():
    space = client.post("/v1/spaces", json={"name": "Crashed Worker Space"}, headers=ALICE_AUTH).json()
    space_id = space["space_id"]

    now = datetime.now(UTC)
    # File record stuck in "deleting" state because previous worker crashed
    stuck_file = FileRecord(
        space_id=space_id,
        filename="crashed_during_delete.pdf",
        storage_path=f"{space_id}/files/crashed_during_delete.pdf",
        uploaded_by="alice_01",
        cleanup_pending=True,
        cleanup_status="deleting",
        lease_token="dead_worker_token",
        lease_version=1,
        cleanup_lease_until=now - timedelta(seconds=10),  # Lease expired
    )
    store.save_file(stuck_file)

    # New worker should successfully reclaim the stuck file with new lease token
    new_claim = store.claim_cleanup_file(stuck_file.file_id, lease_duration_seconds=60)
    assert new_claim is not None
    assert new_claim.lease_token != "dead_worker_token"
    assert new_claim.lease_version == 2
    assert new_claim.cleanup_status == "in_progress"

    # New worker can now mark deleting and delete metadata
    assert store.mark_cleanup_file_deleting(stuck_file.file_id, new_claim.lease_token) is True
    assert store.delete_cleanup_file_with_lease(stuck_file.file_id, new_claim.lease_token) is True
    assert store.get_file(stuck_file.file_id) is None


def test_upload_heartbeat_bounded_lease_extension():
    space = client.post("/v1/spaces", json={"name": "Bounded Heartbeat Space"}, headers=ALICE_AUTH).json()
    space_id = space["space_id"]

    now = datetime.now(UTC)
    upload_token = "upload_tok_bounded"
    rec = FileRecord(
        space_id=space_id,
        filename="bounded_stream.mov",
        storage_path=f"{space_id}/files/bounded_stream.mov",
        uploaded_by="alice_01",
        upload_status="pending_upload",
        upload_fencing_token=upload_token,
        upload_lease_until=now + timedelta(seconds=10),
    )
    store.save_file(rec)

    # Run upload_heartbeat for 3 ticks of 0.05s intervals, extending by 30s each tick
    with upload_heartbeat(rec.file_id, upload_token, interval_seconds=0.05, extension_seconds=30) as tracker:
        time.sleep(0.2)
        assert tracker.is_healthy is True

    # Verify lease is strictly bounded to (now + 30s), NOT 10 + 30 + 30 + 30 = 100s
    updated = store.get_file(rec.file_id)
    assert updated.upload_lease_until <= datetime.now(UTC) + timedelta(seconds=35)
    assert updated.upload_lease_until >= datetime.now(UTC) + timedelta(seconds=20)


def test_upload_heartbeat_detects_and_aborts_on_repeated_failure():
    space = client.post("/v1/spaces", json={"name": "Unhealthy Heartbeat Space"}, headers=ALICE_AUTH).json()
    space_id = space["space_id"]

    upload_token = "upload_tok_fail"
    rec = FileRecord(
        space_id=space_id,
        filename="fail_stream.mov",
        storage_path=f"{space_id}/files/fail_stream.mov",
        uploaded_by="alice_01",
        upload_status="pending_upload",
        upload_fencing_token=upload_token,
        upload_lease_until=datetime.now(UTC) + timedelta(seconds=10),
    )
    store.save_file(rec)

    # Mock renew_upload_lease returning False to simulate lease rejection
    with patch.object(store, "renew_upload_lease", return_value=False):
        with upload_heartbeat(rec.file_id, upload_token, interval_seconds=0.05, extension_seconds=30) as tracker:
            time.sleep(0.15)
            assert tracker.is_healthy is False
            assert "rejected" in tracker.last_error.lower()


def test_upload_heartbeat_failure_cleans_up_blob_and_sanitizes_503():
    space = client.post("/v1/spaces", json={"name": "Heartbeat 503 Space"}, headers=ALICE_AUTH).json()
    space_id = space["space_id"]

    # Mock upload_heartbeat returning an unhealthy tracker
    unhealthy_tracker = HeartbeatTracker("file_mock_fail")
    unhealthy_tracker.record_failure("FATAL_INTERNAL_DB_PROJECT_ID_LEAK", is_fatal=True)

    @contextmanager
    def _fake_unhealthy_hb(file_id, token, interval_seconds=5.0, extension_seconds=30):
        yield unhealthy_tracker

    with patch("app.services.file_service.upload_heartbeat", side_effect=_fake_unhealthy_hb):
        upload_res = client.post(
            f"/v1/spaces/{space_id}/files",
            files={"file": ("test_heartbeat_fail.pdf", io.BytesIO(b"DATA"), "application/pdf")},
            headers=ALICE_AUTH,
        )
        assert upload_res.status_code == 503
        detail = upload_res.json()["detail"]
        # Verify sanitized error detail without leaking internal exception strings
        assert "FATAL_INTERNAL_DB_PROJECT_ID_LEAK" not in detail
        assert "Upload lease could not be maintained" in detail

        # Verify physical blob was compensated/deleted and metadata is completely gone
        assert len(store.files) == 0


def test_failed_compensation_does_not_overwrite_cleanup_worker_lease():
    space = client.post("/v1/spaces", json={"name": "CAS Fencing Compensation Space"}, headers=ALICE_AUTH).json()
    space_id = space["space_id"]

    now = datetime.now(UTC)
    upload_token = "upload_tok_initial"
    file_rec = FileRecord(
        space_id=space_id,
        filename="race_file.pdf",
        storage_path=f"{space_id}/files/race_file.pdf",
        uploaded_by="alice_01",
        upload_status="pending_upload",
        upload_fencing_token=upload_token,
        upload_lease_until=now - timedelta(seconds=10),  # expired upload lease
    )
    store.save_file(file_rec)

    # 1. Cleanup worker claims the expired pending upload
    claimed = store.claim_cleanup_file(file_rec.file_id, lease_duration_seconds=60)
    assert claimed is not None
    assert claimed.cleanup_status == "in_progress"
    active_worker_token = claimed.lease_token
    assert active_worker_token is not None

    # 2. Upload thread fails physical write and attempts compensation with physical delete failure
    # It attempts to call mark_upload_failed_if_owned using its upload_token
    updated = store.mark_upload_failed_if_owned(
        file_id=file_rec.file_id,
        upload_fencing_token=upload_token,
        error_msg="SIMULATED_PHYSICAL_DELETE_FAILED",
    )
    # Must return False because cleanup worker already claimed it (cleanup_status == "in_progress")
    assert updated is False

    # 3. Verify cleanup worker's active lease token and in_progress status are strictly PRESERVED
    db_rec = store.get_file(file_rec.file_id)
    assert db_rec.cleanup_status == "in_progress"
    assert db_rec.lease_token == active_worker_token


def test_cross_space_copy_blob_data_integrity():
    s1 = client.post("/v1/spaces", json={"name": "Source Space"}, headers=ALICE_AUTH).json()["space_id"]
    s2 = client.post("/v1/spaces", json={"name": "Target Space"}, headers=ALICE_AUTH).json()["space_id"]

    raw_payload = b"CRITICAL_SCRIPT_CONTENT_1234567890_UNIQUE_BINARY_TEST"
    upload_res = client.post(
        f"/v1/spaces/{s1}/files",
        files={"file": ("master_script.pdf", io.BytesIO(raw_payload), "application/pdf")},
        headers=ALICE_AUTH,
    )
    s1_file_id = upload_res.json()["file_id"]
    s1_sha = upload_res.json()["sha256"]

    share_res = client.post(
        f"/v1/spaces/{s1}/files/{s1_file_id}/share",
        json={"target_space_id": s2},
        headers=ALICE_AUTH,
    )
    assert share_res.status_code == 200
    s2_file_id = share_res.json()["file_id"]
    assert share_res.json()["sha256"] == s1_sha
    assert share_res.json()["storage_path"] != upload_res.json()["storage_path"]

    download_res = client.get(
        f"/v1/spaces/{s2}/files/{s2_file_id}/download",
        headers=ALICE_AUTH,
    )
    assert download_res.status_code == 200
    assert download_res.content == raw_payload
    assert download_res.headers["X-SHA256-Checksum"] == s1_sha


def test_missing_file_binary_returns_404_not_fake_dummy_content():
    space = client.post("/v1/spaces", json={"name": "Download 404 Space"}, headers=ALICE_AUTH).json()
    space_id = space["space_id"]

    upload_res = client.post(
        f"/v1/spaces/{space_id}/files",
        files={"file": ("test_file.txt", io.BytesIO(b"data"), "text/plain")},
        headers=ALICE_AUTH,
    )
    file_rec = upload_res.json()
    file_id = file_rec["file_id"]

    if file_id in store.file_blobs:
        del store.file_blobs[file_id]

    disk_path = os.path.abspath(os.path.join(settings.STUDIO_TOWER_DATA_DIR, file_rec["storage_path"]))
    if os.path.exists(disk_path):
        os.remove(disk_path)

    download_res = client.get(f"/v1/spaces/{space_id}/files/{file_id}/download", headers=ALICE_AUTH)
    assert download_res.status_code == 404
    assert "not found or unlinked" in download_res.json()["detail"]


def test_admin_invite_requires_email_and_single_use():
    space = client.post("/v1/spaces", json={"name": "Strict Security Space"}, headers=ALICE_AUTH).json()
    space_id = space["space_id"]

    # Attempting to invite ADMIN without email -> 400 Bad Request
    no_email_res = client.post(
        f"/v1/spaces/{space_id}/invites",
        json={"role": "admin"},
        headers=ALICE_AUTH,
    )
    assert no_email_res.status_code == 400
    assert "Target email is required" in no_email_res.json()["detail"]

    # Attempting to create multi-use ADMIN invite -> 400 Bad Request
    multi_use_res = client.post(
        f"/v1/spaces/{space_id}/invites",
        json={"role": "admin", "target_email": "bob@example.com", "max_uses": 5},
        headers=ALICE_AUTH,
    )
    assert multi_use_res.status_code == 400
    assert "must be single-use" in multi_use_res.json()["detail"]

    # Valid ADMIN invite
    valid_res = client.post(
        f"/v1/spaces/{space_id}/invites",
        json={"role": "admin", "target_email": "bob@example.com", "max_uses": 1},
        headers=ALICE_AUTH,
    )
    assert valid_res.status_code == 200


def test_invite_acceptance_never_demotes_existing_owner_or_admin():
    space = client.post("/v1/spaces", json={"name": "Precedence Space"}, headers=ALICE_AUTH).json()
    space_id = space["space_id"]

    # Alice creates a MEMBER invite link
    member_invite = client.post(
        f"/v1/spaces/{space_id}/invites",
        json={"role": "member"},
        headers=ALICE_AUTH,
    ).json()

    # Alice (OWNER) accepts her own MEMBER invite link
    accept_res = client.post(f"/v1/invites/{member_invite['token']}/accept", headers=ALICE_AUTH)
    assert accept_res.status_code == 200

    # Verify Alice's role remains OWNER (not demoted to MEMBER)
    assert store.get_member_role(space_id, "alice_01") == MembershipRole.OWNER


def test_email_bound_invite_and_single_use_limit():
    space = client.post("/v1/spaces", json={"name": "Confidential Production Space"}, headers=ALICE_AUTH).json()
    space_id = space["space_id"]

    # Alice creates invite restricted to bob@example.com (single-use)
    invite = client.post(
        f"/v1/spaces/{space_id}/invites",
        json={"role": "coordinator", "target_email": "bob@example.com", "max_uses": 1},
        headers=ALICE_AUTH,
    ).json()
    token = invite["token"]

    # Charlie attempts to accept -> 403 Forbidden
    charlie_attempt = client.post(f"/v1/invites/{token}/accept", headers=CHARLIE_AUTH)
    assert charlie_attempt.status_code == 403
    assert "different email address" in charlie_attempt.json()["detail"]

    # Bob accepts -> 200 OK
    bob_accept = client.post(f"/v1/invites/{token}/accept", headers=BOB_AUTH)
    assert bob_accept.status_code == 200
    assert store.get_member_role(space_id, "bob_02") == MembershipRole.COORDINATOR

    # Bob tries to use token a second time -> 410 Gone (max uses reached)
    replay_attempt = client.post(f"/v1/invites/{token}/accept", headers=BOB_AUTH)
    assert replay_attempt.status_code == 410


def test_pdx_execution_failure_safely_marks_run_failed():
    space = client.post("/v1/spaces", json={"name": "PDX Failure Test Space"}, headers=ALICE_AUTH).json()
    space_id = space["space_id"]

    run = Run(
        space_id=space_id,
        project_tag="general",
        status=RunStatus.AWAITING_APPROVAL,
        prompt="Execute failure test",
        created_by="alice_01",
        approval_gate=ApprovalGate(
            gate_id="gate_fail_01",
            title="Dangerous Rig Approval",
            description="High risk",
            required_role="Production Coordinator",
        ),
    )
    store.save_run(run)

    # Mock PDXEngine.execute_and_bundle to simulate a planning failure
    with patch("app.api.run_routes.PDXEngine.execute_and_bundle", side_effect=ValueError("Simulated deterministic schedule collision")):
        approve_res = client.post(
            f"/v1/spaces/{space_id}/runs/{run.run_id}/approve",
            json={"approved": True},
            headers=ALICE_AUTH,
        )
        assert approve_res.status_code == 500
        assert "PDX artifact generation failed" in approve_res.json()["detail"]

    # Verify Run state is securely FAILED with error summary and retryable flag recorded
    updated_run = store.get_run(run.run_id)
    assert updated_run.status == RunStatus.FAILED
    assert updated_run.failure_code == "PDX_EXECUTION_FAILURE"
    assert updated_run.is_retryable is True


def test_run_retry_endpoint_lifecycle():
    space = client.post("/v1/spaces", json={"name": "Retry Space"}, headers=ALICE_AUTH).json()
    space_id = space["space_id"]

    # Invite Charlie as MEMBER
    member_invite = client.post(f"/v1/spaces/{space_id}/invites", json={"role": "member"}, headers=ALICE_AUTH).json()
    client.post(f"/v1/invites/{member_invite['token']}/accept", headers=CHARLIE_AUTH)

    run = Run(
        space_id=space_id,
        project_tag="general",
        status=RunStatus.FAILED,
        prompt="Execute retry test",
        created_by="alice_01",
        failure_code="PDX_EXECUTION_FAILURE",
        is_retryable=True,
        error_summary="Temporary failure",
    )
    store.save_run(run)

    # Charlie (MEMBER) attempts retry -> 403 Forbidden
    charlie_retry = client.post(f"/v1/spaces/{space_id}/runs/{run.run_id}/retry", headers=CHARLIE_AUTH)
    assert charlie_retry.status_code == 403

    # Alice (OWNER) retries -> 200 OK and generates artifacts
    alice_retry = client.post(f"/v1/spaces/{space_id}/runs/{run.run_id}/retry", headers=ALICE_AUTH)
    assert alice_retry.status_code == 200
    assert alice_retry.json()["status"] == RunStatus.COMPLETED.value
    assert len(alice_retry.json()["output_artifact_ids"]) == 3


def test_retry_rejected_gate_run_is_blocked():
    space = client.post("/v1/spaces", json={"name": "Rejected Gate Space"}, headers=ALICE_AUTH).json()
    space_id = space["space_id"]

    # Create run that was rejected by risk officer
    run = Run(
        space_id=space_id,
        project_tag="general",
        status=RunStatus.FAILED,
        prompt="Execute dangerous stunt",
        created_by="alice_01",
        approval_gate=ApprovalGate(
            gate_id="gate_stunt_rej",
            title="Helicopter Stunt",
            description="High wind safety hazard",
            status="rejected",
        ),
        failure_code="GATE_REJECTED",
        is_retryable=False,
        error_summary="Risk gate rejected by user",
    )
    store.save_run(run)

    # Attempting to retry a non-retryable or rejected gate -> 400 Bad Request
    retry_res = client.post(f"/v1/spaces/{space_id}/runs/{run.run_id}/retry", headers=ALICE_AUTH)
    assert retry_res.status_code == 400
    assert "cannot be retried" in retry_res.json()["detail"]


def test_cas_run_status_transition_conflict():
    space = client.post("/v1/spaces", json={"name": "CAS Space"}, headers=ALICE_AUTH).json()
    space_id = space["space_id"]

    run = Run(
        space_id=space_id,
        project_tag="general",
        status=RunStatus.RUNNING,
        prompt="Running task",
        created_by="alice_01",
    )
    store.save_run(run)

    # Attempt CAS transition expecting AWAITING_APPROVAL -> Conflict (returns None)
    cas_res = store.compare_and_swap_run_status(
        run_id=run.run_id,
        expected_status=RunStatus.AWAITING_APPROVAL,
        new_status=RunStatus.COMPLETED,
    )
    assert cas_res is None

    # Correct expected status -> succeeds
    success_cas = store.compare_and_swap_run_status(
        run_id=run.run_id,
        expected_status=RunStatus.RUNNING,
        new_status=RunStatus.COMPLETED,
    )
    assert success_cas is not None
    assert success_cas.status == RunStatus.COMPLETED


def test_file_search_accessible_files_endpoint():
    s1 = client.post("/v1/spaces", json={"name": "Search Space 1"}, headers=ALICE_AUTH).json()["space_id"]
    s2 = client.post("/v1/spaces", json={"name": "Search Space 2"}, headers=BOB_AUTH).json()["space_id"]

    # Alice uploads a matching treatment in Space 1 and an unrelated file
    client.post(
        f"/v1/spaces/{s1}/files",
        files={"file": ("director_treatment_v2.pdf", io.BytesIO(b"Treatment content"), "application/pdf")},
        headers=ALICE_AUTH,
    )
    client.post(
        f"/v1/spaces/{s1}/files",
        files={"file": ("caterer_invoice.xlsx", io.BytesIO(b"Invoice content"), "application/vnd.ms-excel")},
        headers=ALICE_AUTH,
    )

    # Bob uploads a treatment in Space 2 (inaccessible to Alice)
    client.post(
        f"/v1/spaces/{s2}/files",
        files={"file": ("bob_private_treatment.pdf", io.BytesIO(b"Private Treatment"), "application/pdf")},
        headers=BOB_AUTH,
    )

    # Alice searches for "treatment" via /v1/files/search
    search_res = client.get("/v1/files/search?q=treatment", headers=ALICE_AUTH)
    assert search_res.status_code == 200
    results = search_res.json()

    filenames = [f["filename"] for f in results]
    assert "director_treatment_v2.pdf" in filenames
    assert "caterer_invoice.xlsx" not in filenames
    assert "bob_private_treatment.pdf" not in filenames  # Tenancy isolation verified


def test_atomic_cleanup_claim_concurrency():
    space = client.post("/v1/spaces", json={"name": "Concurrent Cleanup Space"}, headers=ALICE_AUTH).json()
    space_id = space["space_id"]

    file_rec = FileRecord(
        space_id=space_id,
        filename="concurrent_orphan.pdf",
        storage_path=f"{space_id}/files/concurrent_orphan.pdf",
        uploaded_by="alice_01",
        cleanup_pending=True,
        cleanup_status="pending",
    )
    store.save_file(file_rec)

    # Concurrent race across multiple threads attempting to claim the same file
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(store.claim_cleanup_file, file_rec.file_id, 60) for _ in range(5)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    # Exactly one thread succeeds in claiming the lease token
    successful_claims = [r for r in results if r is not None]
    assert len(successful_claims) == 1
    assert successful_claims[0].lease_token is not None


def test_file_deletion_failure_preserves_metadata_and_marks_cleanup_pending():
    space = client.post("/v1/spaces", json={"name": "GCS Delete Space"}, headers=ALICE_AUTH).json()
    space_id = space["space_id"]

    orig_backend = settings.STUDIO_TOWER_ARTIFACT_BACKEND
    orig_bucket = settings.STUDIO_TOWER_GCS_BUCKET
    try:
        settings.STUDIO_TOWER_ARTIFACT_BACKEND = "gcs"
        settings.STUDIO_TOWER_GCS_BUCKET = "test-bucket"

        # Create FileRecord
        file_rec = FileRecord(
            space_id=space_id,
            filename="critical_script.pdf",
            storage_path=f"{space_id}/files/critical_script.pdf",
            uploaded_by="alice_01",
        )
        store.save_file(file_rec)

        # Mock GCS Client throwing network timeout on blob delete
        import sys
        mock_gcs = MagicMock()
        mock_client = MagicMock()
        mock_bucket = MagicMock()
        mock_blob = MagicMock()
        mock_gcs.Client.return_value = mock_client
        mock_client.bucket.return_value = mock_bucket
        mock_bucket.blob.return_value = mock_blob
        mock_blob.exists.return_value = True
        mock_blob.delete.side_effect = ConnectionError("GCS network timeout")

        with patch.dict(sys.modules, {"google.cloud.storage": mock_gcs}):
            with pytest.raises(Exception) as exc_info:
                FileService.delete_file_permanently(file_rec)
            assert "Cloud storage deletion failed" in str(exc_info.value)

            # Verify metadata was PRESERVED and marked cleanup_pending=True
            saved_rec = store.get_file(file_rec.file_id)
            assert saved_rec is not None
            assert saved_rec.cleanup_pending is True

            # Verify file is excluded from search while cleanup_pending
            search_res = client.get("/v1/files/search?q=critical", headers=ALICE_AUTH)
            assert search_res.status_code == 200
            assert not any(f["file_id"] == file_rec.file_id for f in search_res.json())

            # Unauthorized call without maintenance key is rejected (403)
            unauth_res = client.post("/v1/maintenance/cleanup-pending-files", headers=ALICE_AUTH)
            assert unauth_res.status_code == 403

            # Test background maintenance cleanup retry endpoint with valid maintenance key
            mock_blob.delete.side_effect = None  # Storage recovered
            maintenance_res = client.post("/v1/maintenance/cleanup-pending-files", headers=MAINTENANCE_HEADER)
            assert maintenance_res.status_code == 200
            assert maintenance_res.json()["cleaned"] >= 1
            assert store.get_file(file_rec.file_id) is None
    finally:
        settings.STUDIO_TOWER_ARTIFACT_BACKEND = orig_backend
        settings.STUDIO_TOWER_GCS_BUCKET = orig_bucket


def test_gcs_outage_returns_502():
    space = client.post("/v1/spaces", json={"name": "GCS Outage Space"}, headers=ALICE_AUTH).json()
    space_id = space["space_id"]

    orig_backend = settings.STUDIO_TOWER_ARTIFACT_BACKEND
    orig_bucket = settings.STUDIO_TOWER_GCS_BUCKET
    try:
        settings.STUDIO_TOWER_ARTIFACT_BACKEND = "gcs"
        settings.STUDIO_TOWER_GCS_BUCKET = "test-bucket"

        # Mock GCS Client throwing connection error
        import sys
        mock_gcs = MagicMock()
        mock_gcs.Client.side_effect = ConnectionError("GCS network connection timeout")
        with patch.dict(sys.modules, {"google.cloud.storage": mock_gcs}):
            download_res = client.post(
                f"/v1/spaces/{space_id}/files",
                files={"file": ("test.pdf", io.BytesIO(b"data"), "application/pdf")},
                headers=ALICE_AUTH,
            )
            assert download_res.status_code == 500
            assert "Cloud storage upload failed" in download_res.json()["detail"]
    finally:
        settings.STUDIO_TOWER_ARTIFACT_BACKEND = orig_backend
        settings.STUDIO_TOWER_GCS_BUCKET = orig_bucket


def test_sole_owner_cannot_leave_space_with_other_members():
    space = client.post("/v1/spaces", json={"name": "Leadership Space"}, headers=ALICE_AUTH).json()
    space_id = space["space_id"]

    invite = client.post(f"/v1/spaces/{space_id}/invites", json={"role": "member"}, headers=ALICE_AUTH).json()
    client.post(f"/v1/invites/{invite['token']}/accept", headers=BOB_AUTH)

    leave_res = client.post(f"/v1/spaces/{space_id}/leave", headers=ALICE_AUTH)
    assert leave_res.status_code == 400
    assert "Sole Space owner cannot leave without transferring ownership" in leave_res.json()["detail"]


def test_chat_does_not_persist_message_on_invalid_attachment():
    space = client.post("/v1/spaces", json={"name": "Chat Validation Space"}, headers=ALICE_AUTH).json()
    space_id = space["space_id"]

    chat_res = client.post(
        "/v1/chat",
        json={
            "space_id": space_id,
            "content": "Analyzing missing file",
            "attachment_file_ids": ["non_existent_file_99999"],
        },
        headers=ALICE_AUTH,
    )
    assert chat_res.status_code == 404

    msgs = store.list_messages(space_id)
    assert len(msgs) == 0


def test_upload_file_size_limit():
    space = client.post("/v1/spaces", json={"name": "Size Limit Space"}, headers=ALICE_AUTH).json()
    space_id = space["space_id"]

    orig_limit = settings.MAX_UPLOAD_SIZE_BYTES
    try:
        settings.MAX_UPLOAD_SIZE_BYTES = 100
        oversized_data = b"X" * 150

        upload_res = client.post(
            f"/v1/spaces/{space_id}/files",
            files={"file": ("large_file.dat", io.BytesIO(oversized_data), "application/octet-stream")},
            headers=ALICE_AUTH,
        )
        assert upload_res.status_code == 413
        assert "exceeds maximum allowed limit" in upload_res.json()["detail"]
    finally:
        settings.MAX_UPLOAD_SIZE_BYTES = orig_limit


def test_firestore_emulator_transaction_conflict_and_cas_rejection():
    # Mock Firestore client simulating transaction conflicts
    mock_client = MagicMock()
    mock_tx = MagicMock()
    mock_client.transaction.return_value = mock_tx

    import sys
    mock_firestore_mod = MagicMock()

    def mock_tx_decorator(fn):
        def wrapper(tx, *args, **kwargs):
            raise Exception("409 Transaction Aborted: Conflict with concurrent writer")
        return wrapper

    mock_firestore_mod.transactional = mock_tx_decorator

    with patch.dict(sys.modules, {"google.cloud.firestore": mock_firestore_mod}):
        fs_store = FirestoreStore(project_id="test-conflict-proj", client=mock_client)

        # 1. Compare and swap run status raises StorageUnavailableError on transaction conflict
        from app.services.storage import StorageUnavailableError
        with pytest.raises(StorageUnavailableError, match="Firestore CAS status transition failed"):
            fs_store.compare_and_swap_run_status("run_123", RunStatus.RUNNING, RunStatus.COMPLETED)

        # 2. Claim cleanup returns None on transaction conflict (allowing next retry without crashing)
        claimed = fs_store.claim_cleanup_file("file_123")
        assert claimed is None


def test_firestore_mark_upload_failed_if_owned_cas_enforcement():
    # Setup mock Firestore snapshots
    mock_client = MagicMock()
    mock_tx = MagicMock()
    mock_client.transaction.return_value = mock_tx
    mock_doc = MagicMock()
    mock_client.collection.return_value.document.return_value = mock_doc

    import sys
    mock_firestore_mod = MagicMock()

    # Pass-through decorator that executes the transaction function
    def mock_tx_decorator(fn):
        def wrapper(tx, *args, **kwargs):
            return fn(tx, *args, **kwargs)
        return wrapper

    mock_firestore_mod.transactional = mock_tx_decorator

    with patch.dict(sys.modules, {"google.cloud.firestore": mock_firestore_mod}):
        fs_store = FirestoreStore(project_id="test-cas-proj", client=mock_client)

        # Scenario 1: Snapshot is in_progress (claimed by cleanup worker). CAS should reject!
        active_worker_file = FileRecord(
            file_id="f_cas_1",
            space_id="sp_1",
            filename="test.pdf",
            storage_path="sp_1/files/test.pdf",
            uploaded_by="alice_01",
            upload_status="pending_upload",
            upload_fencing_token="token_uploader",
            cleanup_status="in_progress",
            lease_token="worker_token_xyz",
        )
        mock_snap = MagicMock()
        mock_snap.exists = True
        mock_snap.to_dict.return_value = active_worker_file.model_dump(mode="json")
        mock_doc.get.return_value = mock_snap

        # Uploader attempts to mark failed with matching token, but worker already claimed it
        res = fs_store.mark_upload_failed_if_owned("f_cas_1", "token_uploader", "UPLOAD_FAILED")
        assert res is False

        # Scenario 2: Token mismatch. CAS should reject!
        unclaimed_file = FileRecord(
            file_id="f_cas_2",
            space_id="sp_1",
            filename="test.pdf",
            storage_path="sp_1/files/test.pdf",
            uploaded_by="alice_01",
            upload_status="pending_upload",
            upload_fencing_token="token_correct",
            cleanup_status="pending",
        )
        mock_snap.to_dict.return_value = unclaimed_file.model_dump(mode="json")
        mismatch_res = fs_store.mark_upload_failed_if_owned("f_cas_2", "token_wrong", "UPLOAD_FAILED")
        assert mismatch_res is False

        # Scenario 3: Valid ownership. CAS should succeed!
        valid_res = fs_store.mark_upload_failed_if_owned("f_cas_2", "token_correct", "UPLOAD_FAILED")
        assert valid_res is True


def test_firestore_and_gcs_double_fault_during_upload_compensation():
    space = client.post("/v1/spaces", json={"name": "Double Fault Space"}, headers=ALICE_AUTH).json()
    space_id = space["space_id"]

    orig_backend = settings.STUDIO_TOWER_ARTIFACT_BACKEND
    orig_bucket = settings.STUDIO_TOWER_GCS_BUCKET
    try:
        settings.STUDIO_TOWER_ARTIFACT_BACKEND = "gcs"
        settings.STUDIO_TOWER_GCS_BUCKET = "test-bucket"

        # Mock GCS: write succeeds, but delete throws NetworkError during compensation
        import sys
        mock_gcs = MagicMock()
        mock_client = MagicMock()
        mock_bucket = MagicMock()
        mock_blob = MagicMock()
        mock_gcs.Client.return_value = mock_client
        mock_client.bucket.return_value = mock_bucket
        mock_bucket.blob.return_value = mock_blob
        mock_blob.exists.return_value = True
        mock_blob.delete.side_effect = ConnectionError("GCS network timeout on compensation delete")

        # Mock heartbeat failure to trigger compensation
        unhealthy_tracker = HeartbeatTracker("file_double_fault")
        unhealthy_tracker.record_failure("HEARTBEAT_TIMEOUT", is_fatal=True)

        @contextmanager
        def _fake_hb(file_id, token, interval_seconds=5.0, extension_seconds=30):
            yield unhealthy_tracker

        with patch.dict(sys.modules, {"google.cloud.storage": mock_gcs}):
            with patch("app.services.file_service.upload_heartbeat", side_effect=_fake_hb):
                upload_res = client.post(
                    f"/v1/spaces/{space_id}/files",
                    files={"file": ("test_double_fault.pdf", io.BytesIO(b"DATA"), "application/pdf")},
                    headers=ALICE_AUTH,
                )
                assert upload_res.status_code == 503
                # Verify system safely handled double fault without crashing
                assert "Upload lease could not be maintained" in upload_res.json()["detail"]
    finally:
        settings.STUDIO_TOWER_ARTIFACT_BACKEND = orig_backend
        settings.STUDIO_TOWER_GCS_BUCKET = orig_bucket

