import concurrent.futures
import contextlib
import io
import os
import threading
from unittest.mock import MagicMock, patch

import pytest
from app.agent.brain import AgentBrain
from app.core.config import settings
from app.main import app
from app.services.readiness_service import ReadinessService
from fastapi import HTTPException
from fastapi.testclient import TestClient


def test_liveness_endpoint():
    client = TestClient(app)
    res = client.get("/healthz")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["app"] == settings.APP_NAME
    assert data["version"] == settings.APP_VERSION


def test_readiness_probe_live_success():
    client = TestClient(app)

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = MagicMock(text="pong")

    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        ReadinessService.get_readiness(force=True)
        res = client.get("/readyz")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "ok"
        assert data["ai_ready"] is True
        assert data["ai_mode"] == "live"
        assert data["ai_model"] == settings.GEMINI_MODEL
        assert data["last_error_code"] is None
        assert data["last_latency_ms"] >= 0
        assert "components" in data
        assert data["components"]["database"]["status"] == "ok"
        assert data["components"]["artifacts"]["status"] == "ok"
        assert data["components"]["ai"]["status"] == "ok"


def test_readiness_missing_api_key_returns_503_when_fallback_disallowed():
    client = TestClient(app)

    with patch.dict(os.environ, {}, clear=True), \
         patch.object(settings, "AI_FALLBACK_ALLOWED", False):
        ReadinessService.get_readiness(force=True)
        res = client.get("/readyz")
        assert res.status_code == 503
        data = res.json()
        assert data["status"] == "degraded"
        assert data["ai_ready"] is False
        assert data["last_error_code"] == 401
        assert "credentials missing" in data["last_error_message"]


def test_readiness_missing_api_key_returns_200_when_fallback_explicitly_allowed():
    client = TestClient(app)

    with patch.dict(os.environ, {}, clear=True), \
         patch.object(settings, "AI_FALLBACK_ALLOWED", True):
        ReadinessService.get_readiness(force=True)
        res = client.get("/readyz")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "ok"
        assert data["ai_ready"] is True
        assert data["ai_mode"] == "deterministic_fallback"


def test_readiness_probe_degraded_returns_http_503_on_404():
    client = TestClient(app)

    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = Exception("404 NOT_FOUND. Model not found at https://generativelanguage.googleapis.com/v1beta/models/invalid")

    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        ReadinessService.get_readiness(force=True)
        res = client.get("/readyz")
        assert res.status_code == 503
        data = res.json()
        assert data["status"] == "degraded"
        assert data["ai_ready"] is False
        assert data["last_error_code"] == 404
        assert "generativelanguage.googleapis.com" not in data.get("last_error_message", "")
        assert data["last_error_message"] == "Configured AI model is unavailable or unsupported"


def test_readiness_probe_degraded_returns_http_503_on_429():
    client = TestClient(app)

    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = Exception("429 RESOURCE_EXHAUSTED. Quota exceeded for project 12345")

    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        ReadinessService.get_readiness(force=True)
        res = client.get("/readyz")
        assert res.status_code == 503
        data = res.json()
        assert data["status"] == "degraded"
        assert data["ai_ready"] is False
        assert data["last_error_code"] == 429
        assert "project 12345" not in data.get("last_error_message", "")
        assert data["last_error_message"] == "AI service rate limit or quota exceeded"


def test_readiness_probe_gcs_bucket_validation():
    import sys

    from app.services.file_service import check_artifact_storage_readiness

    # Case A: GCS mode without bucket config
    with patch.object(settings, "STUDIO_TOWER_ARTIFACT_BACKEND", "gcs"), \
         patch.object(settings, "STUDIO_TOWER_GCS_BUCKET", ""):
        res = check_artifact_storage_readiness()
        assert res["status"] == "degraded"
        assert "not configured" in res["error"]

    # Case B: GCS bucket write/read/delete probe success
    mock_blob = MagicMock()
    mock_blob.download_as_bytes.return_value = b"probe_ok"
    mock_bucket = MagicMock()
    mock_bucket.blob.return_value = mock_blob
    mock_gcs_client = MagicMock()
    mock_gcs_client.bucket.return_value = mock_bucket
    mock_storage_mod = MagicMock()
    mock_storage_mod.Client.return_value = mock_gcs_client

    with patch.object(settings, "STUDIO_TOWER_ARTIFACT_BACKEND", "gcs"), \
         patch.object(settings, "STUDIO_TOWER_GCS_BUCKET", "prod-artifacts-bucket"), \
         patch.dict(sys.modules, {"google.cloud.storage": mock_storage_mod}):
        res = check_artifact_storage_readiness()
        assert res["status"] == "ok"
        assert res["backend"] == "gcs"
        assert res["bucket"] == "prod-artifacts-bucket"
        assert mock_blob.upload_from_string.called
        assert mock_blob.delete.called


def test_readiness_probe_gcs_delete_failure_causes_degraded():
    import sys

    from app.services.file_service import check_artifact_storage_readiness

    # Simulate GCS service account missing storage.objects.delete permission
    mock_blob = MagicMock()
    mock_blob.download_as_bytes.return_value = b"probe_ok"
    mock_blob.delete.side_effect = Exception("403 Forbidden: Caller does not have storage.objects.delete")
    mock_bucket = MagicMock()
    mock_bucket.blob.return_value = mock_blob
    mock_gcs_client = MagicMock()
    mock_gcs_client.bucket.return_value = mock_bucket
    mock_storage_mod = MagicMock()
    mock_storage_mod.Client.return_value = mock_gcs_client

    with patch.object(settings, "STUDIO_TOWER_ARTIFACT_BACKEND", "gcs"), \
         patch.object(settings, "STUDIO_TOWER_GCS_BUCKET", "prod-artifacts-bucket"), \
         patch.dict(sys.modules, {"google.cloud.storage": mock_storage_mod}):
        res = check_artifact_storage_readiness()
        assert res["status"] == "degraded"
        assert res["backend"] == "gcs"
        assert "permission denied" in res["error"] or "write/read/delete error" in res["error"]


def test_readiness_probe_firestore_client_validation():
    from app.services.storage import check_storage_readiness

    with patch.object(settings, "STUDIO_TOWER_STORE", "firestore"), \
         patch("app.services.storage.store", MagicMock(spec=[])):
        res = check_storage_readiness()
        assert res["status"] == "degraded"
        assert res["type"] == "firestore"
        assert "uninitialized" in res["error"]


def test_readiness_probe_concurrent_thundering_herd():
    """
    Test true concurrent multi-threaded execution arriving at expired cache simultaneously.
    Asserts single-flight lock permits exactly 1 call to Gemini generate_content.
    """
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = MagicMock(text="pong")

    num_threads = 10
    barrier = threading.Barrier(num_threads)
    results = []

    def worker():
        barrier.wait()
        return AgentBrain.check_readiness(force_probe=False)

    # Invalidate cache
    AgentBrain._readiness_cache = {"status": "unknown", "last_probe_time": 0.0, "ai_ready": False, "ai_mode": "live", "last_error_code": None, "last_latency_ms": 0}

    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(worker) for _ in range(num_threads)]
            for f in concurrent.futures.as_completed(futures):
                results.append(f.result())

    # Assert exactly 1 probe execution despite 10 concurrent threads
    assert mock_client.models.generate_content.call_count == 1
    assert len(results) == num_threads
    for r in results:
        assert r["status"] == "ok"
        assert r["ai_ready"] is True


def test_inference_failure_immediately_degrades_readiness_cache():
    """
    Assert: First cache is primed as healthy (200) -> actual inference fails -> /readyz immediately returns 503.
    """
    client = TestClient(app)

    # 1. Prime ReadinessService cache as healthy (would normally hold for 60s)
    mock_client_ok = MagicMock()
    mock_client_ok.models.generate_content.return_value = MagicMock(text="pong")
    with patch("google.genai.Client", return_value=mock_client_ok), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        # Ensure it is currently healthy
        ReadinessService._cached_result = {
            "status": "ok",
            "is_healthy": True,
            "app": settings.APP_NAME,
            "version": settings.APP_VERSION,
            "auth_mode": settings.STUDIO_TOWER_AUTH_MODE,
            "store": settings.STUDIO_TOWER_STORE,
            "components": {
                "database": {"status": "ok"},
                "artifacts": {"status": "ok"},
                "ai": {"status": "ok", "ready": True},
            },
            "ai_ready": True,
        }
        ReadinessService._last_probe_time = 9999999999.0  # Far in future
        assert client.get("/readyz").status_code == 200

    # 2. Trigger real inference failure in AgentBrain.analyze_treatment
    mock_client_fail = MagicMock()
    mock_client_fail.models.generate_content.side_effect = Exception("429 RESOURCE_EXHAUSTED. Quota exceeded")

    with patch("google.genai.Client", return_value=mock_client_fail), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}), \
         patch.object(settings, "AI_FALLBACK_ALLOWED", False):
        with pytest.raises(HTTPException) as exc_info:
            AgentBrain.analyze_treatment("Sample treatment text", "test-tag")
        assert exc_info.value.status_code == 503
        assert "rate-limited" in exc_info.value.detail

    # 3. Assert /readyz immediately returns 503 degraded without waiting for TTL!
    res_after = client.get("/readyz")
    assert res_after.status_code == 503
    assert res_after.json()["status"] == "degraded"
    assert res_after.json()["components"]["ai"]["status"] == "degraded"


def test_analyze_treatment_timeout_returns_503_with_retry_after():
    """
    Assert transport / connection timeouts return HTTP 503 with Retry-After header.
    """
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = TimeoutError("Deadline exceeded during model inference")

    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}), \
         patch.object(settings, "AI_FALLBACK_ALLOWED", False):
        with pytest.raises(HTTPException) as exc_info:
            AgentBrain.analyze_treatment("Sample script", "test-tag")
        assert exc_info.value.status_code == 503
        assert exc_info.value.headers.get("Retry-After") == "30"
        assert "timed out" in exc_info.value.detail or "temporarily unavailable" in exc_info.value.detail


def test_chat_message_idempotency_prevents_duplicate_records():
    """
    Assert that retrying a chat message with the same client_message_id does NOT duplicate records.
    """
    from app.models.user import User
    from app.services.space_service import SpaceService
    from app.services.storage import store

    user = User(uid="user_idemp_1", email="idemp@example.com", display_name="Idemp User")
    space = SpaceService.ensure_agent_dm(user)

    client = TestClient(app)
    headers = {"Authorization": f"Bearer dev:{user.uid}:{user.email}:{user.display_name}"}

    client_msg_id = "cmsg_test_idemp_12345"

    # Upload a film document first so the chat request routes to analyze_treatment (not conversational)
    film_doc = (
        b"EXT. STUNT TEST ALPHA LOCATION - DAY\n"
        b"Stunt crew assembles. Cast: Lead Stunt Actor.\n"
        b"Director calls action. VFX team on standby.\n"
        b"Production schedule: Scene 1, Act 1.\n"
        b"Screenplay by Test Writer. Crew briefed.\n"
    )
    upload_res = client.post(
        f"/v1/spaces/{space.space_id}/files",
        files={"file": ("stunt_test_alpha.txt", io.BytesIO(film_doc), "text/plain")},
        data={"project_tag": "general"},
        headers=headers,
    )
    assert upload_res.status_code == 200
    file_id = upload_res.json()["file_id"]

    # Simulate AI failure on first attempt
    mock_client_fail = MagicMock()
    mock_client_fail.models.generate_content.side_effect = TimeoutError("Simulated transport timeout")

    with patch("google.genai.Client", return_value=mock_client_fail), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}), \
         patch.object(settings, "AI_FALLBACK_ALLOWED", False):
        res1 = client.post(
            "/v1/chat",
            json={
                "space_id": space.space_id,
                "content": "@agent Breakdown Scene: Stunt Test Alpha",
                "client_message_id": client_msg_id,
                "attachment_file_ids": [file_id],
                "intent": "create_breakdown",
            },
            headers=headers,
        )
        assert res1.status_code == 503

    # Check store has 1 message
    msgs_after_first = store.list_messages(space.space_id)
    user_msgs_first = [m for m in msgs_after_first if m.sender_uid == user.uid]
    assert len(user_msgs_first) == 1
    assert user_msgs_first[0].client_message_id == client_msg_id

    # User clicks Retry (same client_message_id) and this time AI succeeds
    mock_client_ok = MagicMock()
    mock_client_ok.models.generate_content.return_value = MagicMock(
        text='{"project_title": "Valid Breakdown", "summary": "Valid summary", "scenes": []}'
    )

    with patch("google.genai.Client", return_value=mock_client_ok), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        res2 = client.post(
            "/v1/chat",
            json={
                "space_id": space.space_id,
                "content": "@agent Breakdown Scene: Stunt Test Alpha",
                "client_message_id": client_msg_id,
                "attachment_file_ids": [file_id],
                "intent": "create_breakdown",
            },
            headers=headers,
        )
        assert res2.status_code == 200

    # Assert STILL only 1 user message in storage (NO DUPLICATE!)
    msgs_after_retry = store.list_messages(space.space_id)
    user_msgs_retry = [m for m in msgs_after_retry if m.sender_uid == user.uid]
    assert len(user_msgs_retry) == 1
    assert user_msgs_retry[0].message_id == user_msgs_first[0].message_id


def test_gcs_delete_failure_persists_orphan_tracking_record():
    """
    Assert that GCS delete failure persists an orphan FileRecord in cleanup_pending status.
    """
    import sys

    from app.services.file_service import check_artifact_storage_readiness
    from app.services.storage import store

    mock_blob = MagicMock()
    mock_blob.download_as_bytes.return_value = b"probe_ok"
    mock_blob.delete.side_effect = Exception("403 Forbidden on storage.objects.delete")
    mock_bucket = MagicMock()
    mock_bucket.blob.return_value = mock_blob
    mock_gcs_client = MagicMock()
    mock_gcs_client.bucket.return_value = mock_bucket
    mock_storage_mod = MagicMock()
    mock_storage_mod.Client.return_value = mock_gcs_client

    with patch.object(settings, "STUDIO_TOWER_ARTIFACT_BACKEND", "gcs"), \
         patch.object(settings, "STUDIO_TOWER_GCS_BUCKET", "prod-artifacts-bucket"), \
         patch.dict(sys.modules, {"google.cloud.storage": mock_storage_mod}):
        res = check_artifact_storage_readiness()
        assert res["status"] == "degraded"

    # Assert an orphan tracking record was saved in store
    all_files = list(store.files.values())
    probe_orphans = [f for f in all_files if f.uploaded_by == "system_healthz"]
    assert len(probe_orphans) >= 1
    assert probe_orphans[0].cleanup_status == "pending"
    assert probe_orphans[0].cleanup_pending is True
    assert probe_orphans[0].upload_status == "failed"


def test_different_users_same_client_message_id_isolated():
    """
    Assert that different users in the same space using the same client_message_id
    maintain isolated idempotency scopes and cannot hijack each other's messages.
    """
    from app.models.space import MembershipRole
    from app.models.user import User
    from app.services.space_service import SpaceService
    from app.services.storage import store

    user_a = User(uid="user_iso_a", email="a@example.com", display_name="User A")
    user_b = User(uid="user_iso_b", email="b@example.com", display_name="User B")

    space = SpaceService.create_shared_space("Shared Isolation Space", user_a)
    store.memberships[(space.space_id, user_b.uid)] = MembershipRole.MEMBER

    client = TestClient(app)
    shared_client_msg_id = "cmsg_shared_123"

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = MagicMock(
        text='{"project_title": "ISO", "summary": "ISO summary", "scenes": []}'
    )

    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        res_a = client.post(
            "/v1/chat",
            json={"space_id": space.space_id, "content": "@agent Task from A", "client_message_id": shared_client_msg_id},
            headers={"Authorization": f"Bearer dev:{user_a.uid}:{user_a.email}:{user_a.display_name}"},
        )
        assert res_a.status_code == 200

        res_b = client.post(
            "/v1/chat",
            json={"space_id": space.space_id, "content": "@agent Task from B", "client_message_id": shared_client_msg_id},
            headers={"Authorization": f"Bearer dev:{user_b.uid}:{user_b.email}:{user_b.display_name}"},
        )
        assert res_b.status_code == 200

    msgs = store.list_messages(space.space_id)
    user_msgs_a = [m for m in msgs if m.sender_uid == user_a.uid and m.content == "@agent Task from A"]
    user_msgs_b = [m for m in msgs if m.sender_uid == user_b.uid and m.content == "@agent Task from B"]
    assert len(user_msgs_a) == 1
    assert len(user_msgs_b) == 1


def test_same_idempotency_key_with_conflicting_payload_returns_409():
    """
    Assert that sending a conflicting payload under an existing client_message_id returns HTTP 409 Conflict.
    """
    from app.models.user import User
    from app.services.space_service import SpaceService

    user = User(uid="user_conflict_1", email="conf@example.com", display_name="Conflict User")
    space = SpaceService.ensure_agent_dm(user)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer dev:{user.uid}:{user.email}:{user.display_name}"}

    client_msg_id = "cmsg_conflict_test_999"

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = MagicMock(
        text='{"project_title": "Conf Test", "summary": "Summary", "scenes": []}'
    )

    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        res1 = client.post(
            "/v1/chat",
            json={"space_id": space.space_id, "content": "@agent Original Payload", "client_message_id": client_msg_id},
            headers=headers,
        )
        assert res1.status_code == 200

        # Attempt to reuse same key with conflicting content
        res2 = client.post(
            "/v1/chat",
            json={"space_id": space.space_id, "content": "@agent Conflicting Modified Payload", "client_message_id": client_msg_id},
            headers=headers,
        )
        assert res2.status_code == 409
        assert "conflicting" in res2.json()["detail"].lower()


def test_completed_chat_retry_returns_cached_bundle_with_zero_duplicate_runs():
    """
    Assert that after a successful chat request, a retry with identical client_message_id
    returns the exact cached user_msg, agent_msg, and run with ZERO new runs created.
    """
    from app.models.user import User
    from app.services.space_service import SpaceService
    from app.services.storage import store

    user = User(uid="user_retry_cached_1", email="cached@example.com", display_name="Cached User")
    space = SpaceService.ensure_agent_dm(user)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer dev:{user.uid}:{user.email}:{user.display_name}"}

    client_msg_id = "cmsg_retry_cached_abc"

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = MagicMock(
        text='{"project_title": "Cache Test", "summary": "Summary", "scenes": []}'
    )

    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        res1 = client.post(
            "/v1/chat",
            json={"space_id": space.space_id, "content": "@agent Breakdown Scene 1", "client_message_id": client_msg_id, "intent": "create_breakdown"},
            headers=headers,
        )
        assert res1.status_code == 200
        data1 = res1.json()
        assert data1["run"] is not None
        run_id_1 = data1["run"]["run_id"]
        agent_msg_id_1 = data1["agent_message"]["message_id"]

        # Call Gemini should have happened once
        assert mock_client.models.generate_content.call_count == 1

        # Simulate client retry (e.g. response was dropped in transit)
        res2 = client.post(
            "/v1/chat",
            json={"space_id": space.space_id, "content": "@agent Breakdown Scene 1", "client_message_id": client_msg_id, "intent": "create_breakdown"},
            headers=headers,
        )
        assert res2.status_code == 200
        data2 = res2.json()

        # Assert Gemini was NOT called again (ZERO redundant calls!)
        assert mock_client.models.generate_content.call_count == 1

        # Assert returned run and agent message match the original
        assert data2["run"]["run_id"] == run_id_1
        assert data2["agent_message"]["message_id"] == agent_msg_id_1

    # Assert total runs in space is exactly 1 (ZERO duplicates!)
    runs_in_space = store.list_runs_in_space(space.space_id)
    assert len(runs_in_space) == 1
    assert runs_in_space[0].run_id == run_id_1


def test_firestore_idempotency_concurrency_atomic_acquire():
    """
    Assert FirestoreStore.acquire_chat_idempotency uses atomic transactions
    and returns False for concurrent attempts on the same key.
    """
    import sys

    from app.services.storage import FirestoreStore

    mock_fs_client = MagicMock()
    doc_snapshot = MagicMock()
    doc_snapshot.exists = False

    doc_ref = MagicMock()
    mock_fs_client.collection.return_value.document.return_value = doc_ref

    mock_tx = MagicMock()
    mock_fs_client.transaction.return_value = mock_tx

    mock_firestore_mod = MagicMock()
    mock_firestore_mod.transactional = lambda fn: fn

    with patch.dict(sys.modules, {"google.cloud.firestore": mock_firestore_mod}):
        doc_ref.get.return_value = doc_snapshot
        fs_store = FirestoreStore(client=mock_fs_client)
        key = "idemp_key_concurrency_test"
        acquired1, rec1 = fs_store.acquire_chat_idempotency(key, "sp1", "u1", "cmsg1", "hash1")
        assert acquired1 is True
        assert rec1.key == key

        # Second call: document already exists -> acquired False
        doc_snapshot.exists = True
        doc_snapshot.to_dict.return_value = rec1.model_dump(mode="json")
        acquired2, rec2 = fs_store.acquire_chat_idempotency(key, "sp1", "u1", "cmsg1", "hash1")
        assert acquired2 is False
        assert rec2.key == key


def test_gcs_orphan_probe_cleaned_by_maintenance_worker():
    """
    Assert that when a GCS probe delete fails, the persisted orphan record
    is discovered by store.list_cleanup_pending_files() and successfully cleaned
    by FileService.retry_pending_cleanups().
    """
    import sys

    from app.services.file_service import FileService, check_artifact_storage_readiness
    from app.services.storage import store

    mock_blob = MagicMock()
    mock_blob.download_as_bytes.return_value = b"probe_ok"
    mock_blob.delete.side_effect = Exception("403 Forbidden on initial delete")
    mock_blob.exists.return_value = True

    mock_bucket = MagicMock()
    mock_bucket.blob.return_value = mock_blob
    mock_gcs_client = MagicMock()
    mock_gcs_client.bucket.return_value = mock_bucket
    mock_storage_mod = MagicMock()
    mock_storage_mod.Client.return_value = mock_gcs_client

    with patch.object(settings, "STUDIO_TOWER_ARTIFACT_BACKEND", "gcs"), \
         patch.object(settings, "STUDIO_TOWER_GCS_BUCKET", "prod-artifacts-bucket"), \
         patch.dict(sys.modules, {"google.cloud.storage": mock_storage_mod}):
        # 1. Trigger readiness check with delete failure
        res = check_artifact_storage_readiness()
        assert res["status"] == "degraded"

        # 2. Check pending cleanup files contains the orphan probe record
        pending_files = store.list_cleanup_pending_files()
        probe_orphans = [f for f in pending_files if f.uploaded_by == "system_healthz"]
        assert len(probe_orphans) >= 1

        # 3. Simulate background maintenance run where GCS delete succeeds now
        mock_blob.delete.side_effect = None  # GCS permission resolved
        cleanup_stats = FileService.retry_pending_cleanups()
        assert cleanup_stats["cleaned"] >= 1

        # 4. Verify probe record is removed from storage
        all_remaining = [f for f in store.files.values() if f.uploaded_by == "system_healthz"]
        assert len(all_remaining) == 0


def test_concurrent_retry_on_failed_state_atomic_cas():
    """
    Assert that when an operation is in FAILED status, multiple concurrent retry requests
    undergo atomic CAS: exactly one acquires IN_PROGRESS execution rights, while concurrent
    attempts receive HTTP 409 Conflict.
    """
    from app.models.idempotency import ChatIdempotencyRecord, ChatIdempotencyStatus
    from app.models.user import User
    from app.services.space_service import SpaceService
    from app.services.storage import store

    user = User(uid="user_cas_1", email="cas@example.com", display_name="CAS User")
    space = SpaceService.ensure_agent_dm(user)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer dev:{user.uid}:{user.email}:{user.display_name}"}

    client_msg_id = "cmsg_cas_test_001"
    idemp_key = ChatIdempotencyRecord.compute_key(space.space_id, user.uid, client_msg_id)
    payload_hash = ChatIdempotencyRecord.compute_payload_hash("@agent Test Scene", "general", None, intent="conversation")

    # 1. Manually seed record in FAILED status (simulating a prior failed attempt)
    failed_rec = ChatIdempotencyRecord(
        key=idemp_key,
        space_id=space.space_id,
        sender_uid=user.uid,
        client_message_id=client_msg_id,
        payload_hash=payload_hash,
        status=ChatIdempotencyStatus.FAILED,
        error_status_code=503,
        error_detail="Simulated initial failure",
    )
    store.chat_idempotency[idemp_key] = failed_rec

    # 2. First CAS transition succeeds: FAILED -> IN_PROGRESS
    trans1, rec1 = store.transition_chat_idempotency_status(
        idemp_key,
        expected_status=ChatIdempotencyStatus.FAILED,
        new_status=ChatIdempotencyStatus.IN_PROGRESS,
    )
    assert trans1 is True
    assert rec1.status == ChatIdempotencyStatus.IN_PROGRESS

    # 3. Concurrent second CAS transition attempting FAILED -> IN_PROGRESS MUST fail (returns False)
    trans2, rec2 = store.transition_chat_idempotency_status(
        idemp_key,
        expected_status=ChatIdempotencyStatus.FAILED,
        new_status=ChatIdempotencyStatus.IN_PROGRESS,
    )
    assert trans2 is False
    assert rec2.status == ChatIdempotencyStatus.IN_PROGRESS

    # 4. HTTP layer test: request while IN_PROGRESS returns 409
    res_conflict = client.post(
        "/v1/chat",
        json={"space_id": space.space_id, "content": "@agent Test Scene", "client_message_id": client_msg_id},
        headers=headers,
    )
    assert res_conflict.status_code == 409
    assert "currently processing" in res_conflict.json()["detail"].lower()


def test_preflight_attachment_error_does_not_leave_orphaned_in_progress_lock():
    """
    Assert that when an invalid attachment ID is passed, the request fails with 404
    BEFORE acquiring an idempotency lock, leaving NO orphaned IN_PROGRESS record in storage.
    """
    from app.models.idempotency import ChatIdempotencyRecord
    from app.models.user import User
    from app.services.space_service import SpaceService
    from app.services.storage import store

    user = User(uid="user_preflight_1", email="preflight@example.com", display_name="Preflight User")
    space = SpaceService.ensure_agent_dm(user)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer dev:{user.uid}:{user.email}:{user.display_name}"}

    client_msg_id = "cmsg_preflight_attach_fail"
    idemp_key = ChatIdempotencyRecord.compute_key(space.space_id, user.uid, client_msg_id)

    # Send with non-existent attachment
    res_err = client.post(
        "/v1/chat",
        json={
            "space_id": space.space_id,
            "content": "@agent Analyze attachment",
            "attachment_file_ids": ["file_non_existent_9999"],
            "client_message_id": client_msg_id,
        },
        headers=headers,
    )
    assert res_err.status_code == 404

    # Assert NO idempotency record exists in store (no orphaned lock!)
    assert store.get_chat_idempotency(idemp_key) is None

    # Subsequent valid request with same client_message_id can execute cleanly
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = MagicMock(
        text='{"project_title": "Valid Run", "summary": "Summary", "scenes": []}'
    )
    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        res_ok = client.post(
            "/v1/chat",
            json={"space_id": space.space_id, "content": "@agent Analyze text directly", "client_message_id": client_msg_id, "intent": "create_breakdown"},
            headers=headers,
        )
        assert res_ok.status_code == 200
        assert res_ok.json()["run"] is not None


def test_partial_failure_during_completion_records_failed_and_permits_retry():
    """
    Assert that if an unexpected error occurs during database write after AI reasoning,
    the comprehensive try/except trap records status=FAILED, allowing subsequent retries.
    """
    from app.models.idempotency import ChatIdempotencyRecord, ChatIdempotencyStatus
    from app.models.user import User
    from app.services.space_service import SpaceService
    from app.services.storage import store

    user = User(uid="user_partial_fail_1", email="part@example.com", display_name="Partial Fail User")
    space = SpaceService.ensure_agent_dm(user)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer dev:{user.uid}:{user.email}:{user.display_name}"}

    client_msg_id = "cmsg_partial_fail_test"
    idemp_key = ChatIdempotencyRecord.compute_key(space.space_id, user.uid, client_msg_id)

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = MagicMock(
        text='{"project_title": "Partial Test", "summary": "Summary", "scenes": []}'
    )

    # Simulate database disk error on save_run_fenced
    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}), \
         patch.object(store, "save_run_fenced", side_effect=RuntimeError("Simulated disk I/O failure on run")):
        res_fail = client.post(
            "/v1/chat",
            json={"space_id": space.space_id, "content": "@agent Breakdown Scene", "client_message_id": client_msg_id, "intent": "create_breakdown"},
            headers=headers,
        )
        assert res_fail.status_code == 500
        assert "trace id: trc_" in res_fail.json()["detail"].lower()
        assert "disk i/o failure" not in res_fail.json()["detail"].lower()

    # Verify idempotency record transitioned to FAILED (NOT stuck in IN_PROGRESS!)
    rec = store.get_chat_idempotency(idemp_key)
    assert rec is not None
    assert rec.status == ChatIdempotencyStatus.FAILED
    assert rec.error_status_code == 500

    # Subsequent retry (disk I/O recovered) successfully transitions and completes
    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        res_retry = client.post(
            "/v1/chat",
            json={"space_id": space.space_id, "content": "@agent Breakdown Scene", "client_message_id": client_msg_id, "intent": "create_breakdown"},
            headers=headers,
        )
        assert res_retry.status_code == 200
        assert res_retry.json()["run"] is not None

    rec_done = store.get_chat_idempotency(idemp_key)
    assert rec_done.status == ChatIdempotencyStatus.COMPLETED


def test_completed_fallback_does_not_cross_user_boundaries():
    """
    Assert that if a COMPLETED record has a missing user_message_id and another user
    in the same space used the same client_message_id, the fallback strictly searches
    under current_user.uid and raises 500 on inconsistency rather than leaking the other user's message.
    """
    from app.models.idempotency import ChatIdempotencyRecord, ChatIdempotencyStatus
    from app.models.message import Message, MessageRole
    from app.models.space import MembershipRole
    from app.models.user import User
    from app.services.space_service import SpaceService
    from app.services.storage import store

    user_a = User(uid="user_boundary_a", email="a_bound@example.com", display_name="User A")
    user_b = User(uid="user_boundary_b", email="b_bound@example.com", display_name="User B")

    space = SpaceService.create_shared_space("Boundary Space", user_a)
    store.memberships[(space.space_id, user_b.uid)] = MembershipRole.MEMBER

    client = TestClient(app)
    shared_cmsg_id = "cmsg_shared_boundary_key"

    # User B has a message in space with shared_cmsg_id
    msg_b = Message(
        space_id=space.space_id,
        sender_uid=user_b.uid,
        sender_name=user_b.display_name,
        role=MessageRole.USER,
        content="Secret message from B",
        client_message_id=shared_cmsg_id,
    )
    store.add_message(msg_b)

    # Seed a COMPLETED record for User A where user_message_id is intentionally missing/dangling
    key_a = ChatIdempotencyRecord.compute_key(space.space_id, user_a.uid, shared_cmsg_id)
    payload_hash_a = ChatIdempotencyRecord.compute_payload_hash("Task from A", "general", None)
    idemp_a = ChatIdempotencyRecord(
        key=key_a,
        space_id=space.space_id,
        sender_uid=user_a.uid,
        client_message_id=shared_cmsg_id,
        payload_hash=payload_hash_a,
        status=ChatIdempotencyStatus.COMPLETED,
        user_message_id="msg_non_existent_dangling_id",
    )
    store.chat_idempotency[key_a] = idemp_a

    # User A calls /chat with their client_message_id
    res = client.post(
        "/v1/chat",
        json={"space_id": space.space_id, "content": "Task from A", "client_message_id": shared_cmsg_id},
        headers={"Authorization": f"Bearer dev:{user_a.uid}:{user_a.email}:{user_a.display_name}"},
    )

    # Must raise 500 error on inconsistency, and NEVER return User B's message!
    assert res.status_code == 500
    assert "inconsistency" in res.json()["detail"].lower()


def test_stale_worker_lease_expired_cannot_overwrite_newer_lease_holder():
    """
    Assert that when Worker 1 acquires lease (version 1) and times out,
    Worker 2 takes over lease (bumping version to 2).
    When Worker 1 attempts to update with stale version 1, the write is strictly fenced out.
    """
    from datetime import UTC, datetime, timedelta

    from app.models.idempotency import ChatIdempotencyStatus
    from app.services.storage import store

    key = "idemp_fencing_test_key_01"
    now = datetime.now(UTC)

    # 1. Worker 1 acquires record (version=1, lease 120s)
    acquired, rec1 = store.acquire_chat_idempotency(key, "sp1", "u1", "cmsg_f1", "hash1", lease_duration_seconds=120)
    assert acquired is True
    assert rec1.version == 1

    # 2. Simulate lease expiration: lease_until set to past
    rec1.lease_until = now - timedelta(seconds=10)
    store.chat_idempotency[key] = rec1

    # 3. Worker 2 takes over the expired lease
    acquired2, rec2 = store.acquire_chat_idempotency(key, "sp1", "u1", "cmsg_f1", "hash1", lease_duration_seconds=120)
    assert acquired2 is True
    assert rec2.version == 2
    assert rec2.status == ChatIdempotencyStatus.IN_PROGRESS

    # 4. Worker 1 attempts to complete using stale expected_version=1 -> MUST BE REJECTED!
    ok1, updated1 = store.update_chat_idempotency_fenced(
        key,
        expected_version=1,
        status=ChatIdempotencyStatus.COMPLETED,
        user_message_id="msg_stale_1",
    )
    assert ok1 is False
    assert updated1.version == 2
    assert updated1.status == ChatIdempotencyStatus.IN_PROGRESS

    # 5. Worker 2 completes using active expected_version=2 -> MUST SUCCEED!
    ok2, updated2 = store.update_chat_idempotency_fenced(
        key,
        expected_version=2,
        status=ChatIdempotencyStatus.COMPLETED,
        user_message_id="msg_valid_2",
    )
    assert ok2 is True
    assert updated2.version == 3
    assert updated2.status == ChatIdempotencyStatus.COMPLETED
    assert updated2.user_message_id == "msg_valid_2"


def test_run_persisted_checkpoint_reused_on_retry_zero_duplicate_run():
    """
    Assert that if save_run succeeds and checkpoints run_id into idempotency record,
    but a failure happens afterwards during agent message or completion:
    When retried, the retry handler reuses the existing run_record with ZERO new Gemini calls
    and ZERO duplicate runs created in storage.
    """
    from app.models.idempotency import ChatIdempotencyRecord
    from app.models.user import User
    from app.services.space_service import SpaceService
    from app.services.storage import store

    user = User(uid="user_reuse_run_1", email="reuse@example.com", display_name="Reuse Run User")
    space = SpaceService.ensure_agent_dm(user)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer dev:{user.uid}:{user.email}:{user.display_name}"}

    client_msg_id = "cmsg_checkpoint_reuse_test"
    idemp_key = ChatIdempotencyRecord.compute_key(space.space_id, user.uid, client_msg_id)

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = MagicMock(
        text='{"project_title": "Checkpoint Project", "summary": "Initial Run Summary", "scenes": []}'
    )

    # 1. First execution: Gemini runs, save_run succeeds, but adding agent_message fails
    original_add_message = store.add_message
    call_count = {"add_message": 0}

    def failing_add_message(msg):
        call_count["add_message"] += 1
        if call_count["add_message"] >= 2:  # Allow user message (1st call), fail on agent message (2nd call)
            raise RuntimeError("Database connection dropped while persisting agent message")
        return original_add_message(msg)

    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}), \
         patch.object(store, "add_message", side_effect=failing_add_message):
        res_fail = client.post(
            "/v1/chat",
            json={"space_id": space.space_id, "content": "@agent Generate Breakdown", "client_message_id": client_msg_id, "intent": "create_breakdown"},
            headers=headers,
        )
        assert res_fail.status_code == 500

    # Verify run was saved and checkpointed in idempotency record
    idemp_rec = store.get_chat_idempotency(idemp_key)
    assert idemp_rec is not None
    assert idemp_rec.run_id is not None
    saved_run_id = idemp_rec.run_id
    assert store.get_run(saved_run_id) is not None

    # Count runs in space: exactly 1
    runs_before_retry = store.list_runs_in_space(space.space_id)
    assert len(runs_before_retry) == 1

    # 2. Retry execution: Gemini is mocked to raise error if called (verifying it is NOT called)
    failing_ai_client = MagicMock()
    failing_ai_client.models.generate_content.side_effect = AssertionError("Gemini AI should NOT be called on run reuse retry!")

    with patch("google.genai.Client", return_value=failing_ai_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        res_retry = client.post(
            "/v1/chat",
            json={"space_id": space.space_id, "content": "@agent Generate Breakdown", "client_message_id": client_msg_id, "intent": "create_breakdown"},
            headers=headers,
        )
        assert res_retry.status_code == 200
        data = res_retry.json()
        assert data["run"]["run_id"] == saved_run_id
        assert data["agent_message"] is not None

    # Count runs in space after retry: STILL EXACTLY 1 (ZERO duplicate runs!)
    runs_after_retry = store.list_runs_in_space(space.space_id)
    assert len(runs_after_retry) == 1


def test_firestore_fenced_update_prevents_stale_version_overwrites():
    """
    Assert that FirestoreStore.update_chat_idempotency_fenced executes transactional
    version checks and rejects writes with outdated versions.
    """
    import sys
    from unittest.mock import MagicMock

    from app.models.idempotency import ChatIdempotencyRecord, ChatIdempotencyStatus
    from app.services.storage import FirestoreStore

    mock_fs_client = MagicMock()
    doc_snapshot = MagicMock()
    doc_snapshot.exists = True
    current_rec = ChatIdempotencyRecord(
        key="fs_fence_test",
        space_id="sp1",
        sender_uid="u1",
        client_message_id="c1",
        payload_hash="h1",
        status=ChatIdempotencyStatus.IN_PROGRESS,
        version=5,
    )
    doc_snapshot.to_dict.return_value = current_rec.model_dump(mode="json")

    doc_ref = MagicMock()
    doc_ref.get.return_value = doc_snapshot
    mock_fs_client.collection.return_value.document.return_value = doc_ref

    mock_tx = MagicMock()
    mock_fs_client.transaction.return_value = mock_tx

    mock_firestore_mod = MagicMock()
    mock_firestore_mod.transactional = lambda fn: fn

    with patch.dict(sys.modules, {"google.cloud.firestore": mock_firestore_mod}):
        fs_store = FirestoreStore(client=mock_fs_client)

        # Worker attempts to update with stale expected_version=3 (actual is 5)
        ok_stale, rec_stale = fs_store.update_chat_idempotency_fenced(
            "fs_fence_test",
            expected_version=3,
            status=ChatIdempotencyStatus.COMPLETED,
        )
        assert ok_stale is False
        assert rec_stale.version == 5

        # Worker updates with valid expected_version=5
        ok_valid, rec_valid = fs_store.update_chat_idempotency_fenced(
            "fs_fence_test",
            expected_version=5,
            status=ChatIdempotencyStatus.COMPLETED,
            run_id="run_valid_123",
        )
        assert ok_valid is True
        assert rec_valid.version == 6
        assert rec_valid.status == ChatIdempotencyStatus.COMPLETED


def test_agent_message_checkpoint_persisted_and_reused_on_retry():
    """
    Assert that if save_run and add_message(agent_msg) both succeed, but final COMPLETED
    fenced update fails: on retry, both existing run and agent_message are reused with 0 duplication.
    """
    from app.models.idempotency import ChatIdempotencyRecord, ChatIdempotencyStatus
    from app.models.message import Message, MessageRole
    from app.models.run import Run, RunStatus
    from app.models.user import User
    from app.services.space_service import SpaceService
    from app.services.storage import store

    user = User(uid="user_reuse_agent_msg", email="reuse_agent@example.com", display_name="Agent Reuse User")
    space = SpaceService.ensure_agent_dm(user)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer dev:{user.uid}:{user.email}:{user.display_name}"}

    client_msg_id = "cmsg_agent_checkpoint_test"
    idemp_key = ChatIdempotencyRecord.compute_key(space.space_id, user.uid, client_msg_id)

    # 1. Pre-seed existing Run and Agent Message in storage
    saved_run = Run(
        space_id=space.space_id,
        project_tag="general",
        status=RunStatus.COMPLETED,
        prompt="@agent Breakdown Project",
        created_by=user.uid,
    )
    store.save_run(saved_run)

    saved_agent_msg = Message(
        space_id=space.space_id,
        sender_uid="agent_studiotower",
        sender_name="StudioTower Agent",
        role=MessageRole.AGENT,
        content="🎬 **Pre-existing Scene Breakdown**",
    )
    store.add_message(saved_agent_msg)

    # 2. Seed FAILED idempotency record with both run_id and agent_message_id checkpointed
    payload_hash = ChatIdempotencyRecord.compute_payload_hash("@agent Breakdown Project", "general", None, intent="create_breakdown")
    failed_rec = ChatIdempotencyRecord(
        key=idemp_key,
        space_id=space.space_id,
        sender_uid=user.uid,
        client_message_id=client_msg_id,
        payload_hash=payload_hash,
        status=ChatIdempotencyStatus.FAILED,
        run_id=saved_run.run_id,
        agent_message_id=saved_agent_msg.message_id,
        error_status_code=500,
        error_detail="Simulated failure before final completion write",
    )
    store.chat_idempotency[idemp_key] = failed_rec

    # 3. Retry execution: Gemini is mocked to fail if called (verifying 0 Gemini calls)
    mock_ai = MagicMock()
    mock_ai.models.generate_content.side_effect = AssertionError("AI should NOT be called!")

    with patch("google.genai.Client", return_value=mock_ai), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        res = client.post(
            "/v1/chat",
            json={"space_id": space.space_id, "content": "@agent Breakdown Project", "client_message_id": client_msg_id, "intent": "create_breakdown"},
            headers=headers,
        )
        assert res.status_code == 200
        data = res.json()
        assert data["run"]["run_id"] == saved_run.run_id
        assert data["agent_message"]["message_id"] == saved_agent_msg.message_id

    # Verify idempotency record is COMPLETED
    done_rec = store.get_chat_idempotency(idemp_key)
    assert done_rec.status == ChatIdempotencyStatus.COMPLETED
    assert done_rec.run_id == saved_run.run_id
    assert done_rec.agent_message_id == saved_agent_msg.message_id


def test_crash_after_save_run_before_checkpoint_recovers_without_duplicate_run():
    """
    Assert that if save_run commits the deterministic Run to database, but the process crashes
    BEFORE run_id is checkpointed in the idempotency record (idemp_rec.run_id is still None):
    On retry, the worker discovers the deterministic Run in storage, reuses it without re-calling Gemini,
    and creates ZERO duplicate Runs.
    """
    from app.models.idempotency import ChatIdempotencyRecord, ChatIdempotencyStatus
    from app.models.user import User
    from app.services.space_service import SpaceService
    from app.services.storage import store

    user = User(uid="user_crash_run_window", email="crash_run@example.com", display_name="Crash Window User")
    space = SpaceService.ensure_agent_dm(user)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer dev:{user.uid}:{user.email}:{user.display_name}"}

    client_msg_id = "cmsg_crash_window_test_001"
    idemp_key = ChatIdempotencyRecord.compute_key(space.space_id, user.uid, client_msg_id)
    deterministic_run_id = ChatIdempotencyRecord.compute_deterministic_run_id(idemp_key)

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = MagicMock(
        text='{"project_title": "Deterministic Recovery Plan", "summary": "Crash Recovery Summary", "scenes": []}'
    )

    # 1. First execution: save_run succeeds, but crash happens in _fenced_checkpoint (simulating crash before run_id checkpoint)
    original_update_fenced = store.update_chat_idempotency_fenced

    def crashing_checkpoint(key, expected_version, **kwargs):
        if kwargs.get("run_id") is not None:
            # Simulate hard crash/exception right when checkpointing run_id
            raise RuntimeError("Process crashed immediately after save_run and before run_id checkpoint was persisted")
        return original_update_fenced(key, expected_version, **kwargs)

    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}), \
         patch.object(store, "update_chat_idempotency_fenced", side_effect=crashing_checkpoint):
        res_fail = client.post(
            "/v1/chat",
            json={"space_id": space.space_id, "content": "@agent Breakdown Crash Scenario", "client_message_id": client_msg_id, "intent": "create_breakdown"},
            headers=headers,
        )
        assert res_fail.status_code == 500

    # Verify deterministic Run WAS persisted in store
    committed_run = store.get_run(deterministic_run_id)
    assert committed_run is not None
    assert committed_run.run_id == deterministic_run_id

    # Verify idempotency record does NOT have run_id (reproducing the exact crash window)
    rec = store.get_chat_idempotency(idemp_key)
    assert rec.run_id is None
    assert rec.status == ChatIdempotencyStatus.FAILED

    # Count runs in space before retry: exactly 1
    runs_before = store.list_runs_in_space(space.space_id)
    assert len(runs_before) == 1

    # 2. Retry execution: Gemini is mocked to raise error if called (verifying 0 Gemini calls)
    failing_ai = MagicMock()
    failing_ai.models.generate_content.side_effect = AssertionError("Gemini AI should NOT be re-called on deterministic run recovery!")

    with patch("google.genai.Client", return_value=failing_ai), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        res_retry = client.post(
            "/v1/chat",
            json={"space_id": space.space_id, "content": "@agent Breakdown Crash Scenario", "client_message_id": client_msg_id, "intent": "create_breakdown"},
            headers=headers,
        )
        assert res_retry.status_code == 200
        data = res_retry.json()
        assert data["run"]["run_id"] == deterministic_run_id
        assert data["agent_message"] is not None

    # Count runs in space after retry: STILL EXACTLY 1 (ZERO duplicate runs!)
    runs_after = store.list_runs_in_space(space.space_id)
    assert len(runs_after) == 1


def test_crash_after_agent_message_before_checkpoint_recovers_without_duplicate_message():
    """
    Assert that if add_message(agent_msg) succeeds in persisting deterministic Agent Message,
    but crash happens before agent_message_id is checkpointed in the idempotency record:
    On retry, the worker discovers the deterministic Agent Message in storage and reuses it,
    preventing duplicate agent messages in the space.
    """
    from app.models.idempotency import ChatIdempotencyRecord, ChatIdempotencyStatus
    from app.models.user import User
    from app.services.space_service import SpaceService
    from app.services.storage import store

    user = User(uid="user_crash_msg_window", email="crash_msg@example.com", display_name="Crash Msg User")
    space = SpaceService.ensure_agent_dm(user)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer dev:{user.uid}:{user.email}:{user.display_name}"}

    client_msg_id = "cmsg_crash_msg_window_001"
    idemp_key = ChatIdempotencyRecord.compute_key(space.space_id, user.uid, client_msg_id)
    deterministic_agent_msg_id = ChatIdempotencyRecord.compute_deterministic_message_id(idemp_key, "agent")

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = MagicMock(
        text='{"project_title": "Msg Recovery Plan", "summary": "Msg Summary", "scenes": []}'
    )

    # 1. First execution: add_message(agent_msg) succeeds, but crash happens before agent_message_id checkpoint
    original_update_fenced = store.update_chat_idempotency_fenced

    def crashing_checkpoint_on_agent_msg(key, expected_version, **kwargs):
        if kwargs.get("agent_message_id") is not None and kwargs.get("status") != ChatIdempotencyStatus.COMPLETED:
            raise RuntimeError("Process crashed right after add_message(agent_msg) before checkpoint")
        return original_update_fenced(key, expected_version, **kwargs)

    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}), \
         patch.object(store, "update_chat_idempotency_fenced", side_effect=crashing_checkpoint_on_agent_msg):
        res_fail = client.post(
            "/v1/chat",
            json={"space_id": space.space_id, "content": "@agent Breakdown Msg Window", "client_message_id": client_msg_id, "intent": "create_breakdown"},
            headers=headers,
        )
        assert res_fail.status_code == 500

    # Verify deterministic Agent Message WAS persisted in storage
    committed_agent_msg = store.get_message(deterministic_agent_msg_id)
    assert committed_agent_msg is not None

    # 2. Retry execution
    failing_ai = MagicMock()
    failing_ai.models.generate_content.side_effect = AssertionError("AI should NOT be re-called!")

    # 2. Retry execution
    failing_ai = MagicMock()
    failing_ai.models.generate_content.side_effect = AssertionError("AI should NOT be re-called!")

    with patch("google.genai.Client", return_value=failing_ai), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        res_retry = client.post(
            "/v1/chat",
            json={"space_id": space.space_id, "content": "@agent Breakdown Msg Window", "client_message_id": client_msg_id, "intent": "create_breakdown"},
            headers=headers,
        )
        assert res_retry.status_code == 200
        data = res_retry.json()
        assert data["agent_message"]["message_id"] == deterministic_agent_msg_id

    # Verify messages in space: exactly 1 user message and exactly 1 breakdown agent message (plus 1 initial DM welcome message)
    all_msgs = store.list_messages(space.space_id)
    user_msgs = [m for m in all_msgs if m.role.value == "user"]
    breakdown_msgs = [m for m in all_msgs if m.message_id == deterministic_agent_msg_id]
    agent_msgs = [m for m in all_msgs if m.role.value == "agent"]
    assert len(user_msgs) == 1
    assert len(breakdown_msgs) == 1
    assert len(agent_msgs) == 2  # 1 welcome + 1 breakdown, exactly 0 duplicate breakdown messages!



def test_lease_renewal_on_progressive_checkpoints_extends_lease():
    """
    Assert that each progressive checkpoint extends lease_until forward in time by 120s,
    ensuring active long-running operations retain their valid lease.
    """
    from datetime import UTC, datetime, timedelta

    from app.services.storage import store

    key = "idemp_lease_extend_test_key"
    acquired, rec = store.acquire_chat_idempotency(key, "sp1", "u1", "cmsg_l1", "hash1", lease_duration_seconds=120)
    assert acquired is True
    initial_lease = rec.lease_until

    # Simulate 30 seconds passing
    now_30s = datetime.now(UTC) + timedelta(seconds=30)
    with patch("app.services.storage.datetime") as mock_dt:
        mock_dt.now.return_value = now_30s
        mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        ok, updated = store.update_chat_idempotency_fenced(
            key,
            expected_version=1,
            user_message_id="msg_1",
            extend_lease_seconds=120,
        )
        assert ok is True
        assert updated.lease_until > initial_lease
        assert updated.lease_until == now_30s + timedelta(seconds=120)


def test_concurrent_memory_store_add_message_with_same_deterministic_id_is_idempotent():
    """
    Assert that multiple concurrent or repeated calls to MemoryStore.add_message with the
    same deterministic message_id deduplicate cleanly within the Space message list.
    """
    from app.models.message import Message, MessageRole
    from app.services.storage import store

    space_id = "space_idemp_msg_test"
    det_msg_id = "msg_deterministic_test_abc"

    msg1 = Message(
        message_id=det_msg_id,
        space_id=space_id,
        sender_uid="user_1",
        sender_name="User One",
        role=MessageRole.USER,
        content="First write attempt",
    )
    msg2 = Message(
        message_id=det_msg_id,
        space_id=space_id,
        sender_uid="user_1",
        sender_name="User One",
        role=MessageRole.USER,
        content="Second duplicate write attempt",
    )

    store.add_message(msg1)
    store.add_message(msg2)

    msgs = store.list_messages(space_id)
    matching = [m for m in msgs if m.message_id == det_msg_id]
    assert len(matching) == 1
    assert matching[0].content == "First write attempt"


def test_save_run_fenced_blocks_expired_or_preempted_zombie_worker():
    """
    Assert that if Worker 1 starts reasoning with version 1, its lease expires, and Worker 2
    takes over the lease (bumping version to 2) and saves Run 2:
    When zombie Worker 1 attempts save_run_fenced with expected_version=1, it is strictly rejected.
    """
    from datetime import UTC, datetime, timedelta

    import pytest
    from app.models.run import Run, RunStatus
    from app.services.storage import StorageConflictError, store

    key = "idemp_fenced_run_test_key"
    now = datetime.now(UTC)

    # 1. Worker 1 acquires lease (version 1)
    acquired, rec1 = store.acquire_chat_idempotency(key, "sp1", "u1", "cmsg_r1", "hash1", lease_duration_seconds=120)
    assert acquired is True
    assert rec1.version == 1

    # 2. Worker 1 times out (lease expires)
    rec1.lease_until = now - timedelta(seconds=10)
    store.chat_idempotency[key] = rec1

    # 3. Worker 2 takes over lease (version 2)
    acquired2, rec2 = store.acquire_chat_idempotency(key, "sp1", "u1", "cmsg_r1", "hash1", lease_duration_seconds=120)
    assert acquired2 is True
    assert rec2.version == 2

    # Worker 2 saves Run successfully
    run_worker_2 = Run(
        run_id="run_worker_2_id",
        space_id="sp1",
        project_tag="general",
        status=RunStatus.COMPLETED,
        prompt="Worker 2 prompt",
        created_by="u1",
    )
    saved_w2 = store.save_run_fenced(run_worker_2, idemp_key=key, expected_version=2)
    assert saved_w2.run_id == "run_worker_2_id"

    # 4. Zombie Worker 1 wakes up and attempts to save its Run using stale expected_version=1
    run_zombie_1 = Run(
        run_id="run_zombie_1_id",
        space_id="sp1",
        project_tag="general",
        status=RunStatus.COMPLETED,
        prompt="Zombie Worker 1 prompt",
        created_by="u1",
    )
    with pytest.raises(StorageConflictError) as exc_info:
        store.save_run_fenced(run_zombie_1, idemp_key=key, expected_version=1)

    assert "fencing conflict" in str(exc_info.value).lower()
    # Confirm zombie run was NOT saved
    assert store.get_run("run_zombie_1_id") is None
    # Confirm Worker 2 run remains intact
    assert store.get_run("run_worker_2_id") is not None


def test_canonical_json_payload_hashing_prevents_delimiter_collision():
    """
    Assert that canonical JSON hashing prevents field delimiter ambiguities across content, tag, and attachments.
    """
    from app.models.idempotency import ChatIdempotencyRecord

    # Case A: tag has comma/colon, attachments empty
    hash_a = ChatIdempotencyRecord.compute_payload_hash(
        content="hello",
        project_tag="track,part:1",
        attachment_file_ids=[],
    )

    # Case B: tag different, attachment contains the string
    hash_b = ChatIdempotencyRecord.compute_payload_hash(
        content="hello",
        project_tag="track",
        attachment_file_ids=["part:1"],
    )

    assert hash_a != hash_b


def test_long_ai_inference_with_multiple_heartbeat_renewals_succeeds_without_self_fencing():
    """
    Assert that during a long-running AI inference where background heartbeat performs
    multiple lease renewal cycles (bumping version multiple times), the ThreadSafeIdempotencyTracker
    synchronizes the latest version to the main thread so save_run_fenced and final completion
    succeed seamlessly without self-fencing or 409 conflicts.
    """
    import time

    from app.api.chat_routes import idempotency_heartbeat as orig_heartbeat
    from app.models.idempotency import ChatIdempotencyRecord, ChatIdempotencyStatus
    from app.models.user import User
    from app.services.space_service import SpaceService
    from app.services.storage import store

    user = User(uid="user_heartbeat_multi_1", email="heartbeat@example.com", display_name="Heartbeat User")
    space = SpaceService.ensure_agent_dm(user)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer dev:{user.uid}:{user.email}:{user.display_name}"}

    client_msg_id = "cmsg_heartbeat_multicycle_01"
    idemp_key = ChatIdempotencyRecord.compute_key(space.space_id, user.uid, client_msg_id)

    mock_client = MagicMock()

    # Simulate AI inference taking 0.25 seconds while heartbeat interval is 0.05 seconds (at least 3 renewals!)
    def slow_generate(*args, **kwargs):
        time.sleep(0.20)
        return MagicMock(
            text='{"project_title": "Heartbeat Multi Plan", "summary": "Heartbeat Multi Summary", "scenes": []}'
        )

    mock_client.models.generate_content.side_effect = slow_generate

    def fast_heartbeat(tracker, **kwargs):
        return orig_heartbeat(tracker, interval_sec=0.04, extend_sec=120)

    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}), \
         patch("app.api.chat_routes.idempotency_heartbeat", side_effect=fast_heartbeat):
        res = client.post(
            "/v1/chat",
            json={"space_id": space.space_id, "content": "@agent Long Inference Plan", "client_message_id": client_msg_id, "intent": "create_breakdown"},
            headers=headers,
        )
        assert res.status_code == 200
        data = res.json()
        assert data["run"] is not None
        assert data["agent_message"] is not None


    # Verify idempotency record has bumped version multiple times and ended in COMPLETED
    rec = store.get_chat_idempotency(idemp_key)
    assert rec is not None
    assert rec.status == ChatIdempotencyStatus.COMPLETED
    # version should be at least 4 (initial=1, user_msg checkpoint=2, at least 1-2 heartbeats=3-4, run checkpoint=5, etc.)
    assert rec.version >= 4


def test_ai_inference_timeout_returns_immediately_non_blocking():
    """
    Assert that when AI inference exceeds timeout, analyze_treatment aborts immediately
    via non-blocking executor without waiting for blocked worker thread.
    """
    import time

    import pytest
    from app.agent.brain import AgentBrain

    # Set 0.05s timeout
    with patch.dict(os.environ, {"AI_INFERENCE_TIMEOUT_SECONDS": "0.05", "GEMINI_API_KEY": "AIzaFakeKey123"}):
        mock_client = MagicMock()

        def hanging_call(*args, **kwargs):
            time.sleep(1.0)  # Hang for 1.0 second
            return MagicMock(text='{}')

        mock_client.models.generate_content.side_effect = hanging_call

        with patch("google.genai.Client", return_value=mock_client):
            start_t = time.time()
            with pytest.raises(Exception) as exc_info:
                AgentBrain.analyze_treatment("Sample doc text", project_tag="general")
            elapsed = time.time() - start_t

            # Verify it raised 503 (timeout) and returned in < 0.4s (NOT waiting for the 1.0s thread)
            assert "timed out" in str(exc_info.value).lower() or exc_info.value.status_code == 503
            assert elapsed < 0.4


def test_heartbeat_stuck_beyond_join_timeout_fails_closed_and_rejects_commit():
    """
    Assert that if the background lease renewal thread hangs indefinitely inside storage I/O,
    the idempotency_heartbeat context manager detects t.is_alive() after the 2.0s join timeout,
    marks tracker.is_healthy=False, and fails closed (HTTP 409) rather than letting the main thread
    commit unsynchronized state.
    """
    import contextlib
    import time

    from app.api.chat_routes import ThreadSafeIdempotencyTracker
    from app.api.chat_routes import idempotency_heartbeat as orig_heartbeat
    from app.models.user import User
    from app.services.space_service import SpaceService
    from app.services.storage import store

    user = User(uid="user_hb_join_hang_1", email="hbhang@example.com", display_name="HB Hang User")
    space = SpaceService.ensure_agent_dm(user)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer dev:{user.uid}:{user.email}:{user.display_name}"}

    client_msg_id = "cmsg_hb_join_hang_01"

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = MagicMock(
        text='{"project_title": "HB Hang Plan", "summary": "HB Hang Summary", "scenes": []}'
    )

    # Patch storage update_chat_idempotency_fenced to hang when called from background heartbeat
    orig_update_fenced = store.update_chat_idempotency_fenced

    def hanging_update_fenced(key, expected_version, **kwargs):
        # If called for extend_lease_seconds without status/messages, simulate database freeze
        if kwargs.get("extend_lease_seconds") and not kwargs.get("status") and not kwargs.get("user_message_id") and not kwargs.get("run_id"):
            time.sleep(1.0)  # Hang inside background thread
            return True, None
        return orig_update_fenced(key, expected_version, **kwargs)

    @contextlib.contextmanager
    def fast_hang_heartbeat(tracker: ThreadSafeIdempotencyTracker):
        with orig_heartbeat(tracker, interval_sec=0.02, extend_sec=120, join_timeout_sec=0.05):
            yield

    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}), \
         patch.object(store, "update_chat_idempotency_fenced", side_effect=hanging_update_fenced), \
         patch("app.api.chat_routes.idempotency_heartbeat", side_effect=fast_hang_heartbeat):
        # Give AI a tiny delay to ensure background thread enters the hanging storage call
        def ai_call_with_small_sleep(*args, **kwargs):
            time.sleep(0.06)
            return MagicMock(
                text='{"project_title": "HB Hang Plan", "summary": "HB Hang Summary", "scenes": []}'
            )

        mock_client.models.generate_content.side_effect = ai_call_with_small_sleep

        res = client.post(
            "/v1/chat",
            json={"space_id": space.space_id, "content": "@agent Test HB Hang", "client_message_id": client_msg_id, "intent": "create_breakdown"},
            headers=headers,
        )

        # Main thread MUST fail-closed with 409
        assert res.status_code == 409
        assert "lease lost or renewal timed out" in res.json()["detail"].lower()
        # Verify no raw exception strings leaked
        assert "traceback" not in res.json()["detail"].lower()


def test_multiple_gemini_timeouts_bound_active_background_threads_to_pool_limit():
    """
    Assert that concurrent Gemini timeouts do not spawn unbounded abandoned threads.
    Active worker threads remain strictly bounded by _AI_EXECUTOR._max_workers.
    """
    import concurrent.futures
    import time

    import pytest
    from app.agent.brain import _AI_EXECUTOR, AgentBrain

    mock_client = MagicMock()

    def slow_hanging_gemini(*args, **kwargs):
        time.sleep(0.5)
        return MagicMock(text='{}')

    mock_client.models.generate_content.side_effect = slow_hanging_gemini

    # Trigger 10 concurrent calls with 0.05s timeout
    with patch.dict(os.environ, {"AI_INFERENCE_TIMEOUT_SECONDS": "0.05", "GEMINI_API_KEY": "AIzaFakeKey123"}), \
         patch("google.genai.Client", return_value=mock_client):

        def run_one():
            with pytest.raises(Exception):
                AgentBrain.analyze_treatment("Sample Treatment", project_tag="general")

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as client_sim_pool:
            futures = [client_sim_pool.submit(run_one) for _ in range(10)]
            concurrent.futures.wait(futures)

        # Assert total threads in the shared AI pool did not exceed max_workers limit
        assert len(_AI_EXECUTOR._threads) <= _AI_EXECUTOR._max_workers


def test_bulkhead_saturation_fast_rejects_with_http_503():
    """
    Assert that when AI concurrency capacity is saturated, the bulkhead immediately
    rejects subsequent requests with HTTP 503 and Retry-After header without queuing.
    """
    import threading
    import time

    from app.agent.brain import AgentBrain

    # Saturate the bulkhead semaphore completely
    with patch("app.agent.brain.AI_MAX_CONCURRENT_INFERENCES", 1), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        _sem = threading.Semaphore(1)
        _sem.acquire()  # Drain capacity to 0

        with patch("app.agent.brain._AI_SEMAPHORE", _sem):
            start_t = time.time()
            with pytest.raises(Exception) as exc_info:
                AgentBrain.analyze_treatment("Bulkhead Test Treatment", project_tag="general")
            elapsed = time.time() - start_t

            # Verify it raised 503 and returned in under 0.05s (zero queue latency)
            assert exc_info.value.status_code == 503
            assert "operating at maximum concurrency capacity" in exc_info.value.detail.lower()
            assert exc_info.value.headers.get("Retry-After") == "5"
            assert elapsed < 0.05


def test_heartbeat_join_timeout_marks_failed_permitting_immediate_retry():
    """
    Assert that after a heartbeat join timeout fails-closed, the idempotency record is actively
    transitioned to FAILED with no lease lockout, so an immediate client retry succeeds.
    """
    from app.api.chat_routes import ThreadSafeIdempotencyTracker
    from app.api.chat_routes import idempotency_heartbeat as orig_heartbeat
    from app.models.idempotency import ChatIdempotencyRecord, ChatIdempotencyStatus
    from app.models.user import User
    from app.services.space_service import SpaceService
    from app.services.storage import store

    user = User(uid="user_hb_recov_1", email="hbrecov@example.com", display_name="HB Recov User")
    space = SpaceService.ensure_agent_dm(user)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer dev:{user.uid}:{user.email}:{user.display_name}"}

    client_msg_id = "cmsg_hb_recov_test_01"
    idemp_key = ChatIdempotencyRecord.compute_key(space.space_id, user.uid, client_msg_id)

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = MagicMock(
        text='{"project_title": "HB Recovery Plan", "summary": "HB Recovery Summary", "scenes": []}'
    )

    # 1. Simulate join timeout on first attempt
    @contextlib.contextmanager
    def join_timeout_heartbeat(tracker: ThreadSafeIdempotencyTracker):
        with orig_heartbeat(tracker, interval_sec=0.01, extend_sec=120, join_timeout_sec=0.02):
            tracker.mark_unhealthy("HEARTBEAT_JOIN_TIMEOUT")
            yield

    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}), \
         patch("app.api.chat_routes.idempotency_heartbeat", side_effect=join_timeout_heartbeat):
        res_fail = client.post(
            "/v1/chat",
            json={"space_id": space.space_id, "content": "@agent Test HB Recovery", "client_message_id": client_msg_id, "intent": "create_breakdown"},
            headers=headers,
        )
        assert res_fail.status_code == 409

    # Verify status in database is FAILED (NOT stuck in IN_PROGRESS!)
    rec = store.get_chat_idempotency(idemp_key)
    assert rec is not None
    assert rec.status == ChatIdempotencyStatus.FAILED

    # 2. Immediate retry without delay successfully completes
    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        res_retry = client.post(
            "/v1/chat",
            json={"space_id": space.space_id, "content": "@agent Test HB Recovery", "client_message_id": client_msg_id, "intent": "create_breakdown"},
            headers=headers,
        )
        assert res_retry.status_code == 200
        assert res_retry.json()["run"] is not None


def test_bulkhead_semaphore_held_until_background_future_completes_after_timeout():
    """
    Assert that when a client request times out (e.g. 0.05s), the bulkhead semaphore permit
    is NOT prematurely returned by the request thread, but remains held until the background
    worker in _AI_EXECUTOR completes its I/O.
    """
    import threading
    import time

    from app.agent.brain import AgentBrain

    mock_client = MagicMock()

    # Background worker simulates 0.25s execution
    def slow_ai_call(*args, **kwargs):
        time.sleep(0.25)
        return MagicMock(text='{"project_title": "Slow Plan", "summary": "Slow Summary", "scenes": []}')

    mock_client.models.generate_content.side_effect = slow_ai_call

    # Set capacity=1 and timeout=0.05s
    sem = threading.Semaphore(1)
    with patch("app.agent.brain._AI_SEMAPHORE", sem), \
         patch("app.agent.brain.AI_MAX_CONCURRENT_INFERENCES", 1), \
         patch.dict(os.environ, {"AI_INFERENCE_TIMEOUT_SECONDS": "0.05", "GEMINI_API_KEY": "AIzaFakeKey123"}), \
         patch("google.genai.Client", return_value=mock_client):

        # Call 1: Times out at 0.05s
        with pytest.raises(Exception) as exc_info:
            AgentBrain.analyze_treatment("Treatment 1", project_tag="general")
        assert exc_info.value.status_code == 503

        # Immediately at t=0.08s: Background future is STILL RUNNING in executor thread!
        # The semaphore permit MUST NOT be available yet!
        acquired_immediately = sem.acquire(blocking=False)
        assert acquired_immediately is False, "Semaphore permit was prematurely released before background future completed!"

        # Wait for background future to complete (t=0.30s)
        time.sleep(0.25)

        # Now the done callback has executed and released the permit
        acquired_after_completion = sem.acquire(blocking=False)
        assert acquired_after_completion is True, "Semaphore permit was not released after background future completed!"
        sem.release()


def test_heartbeat_late_commit_race_with_join_timeout_atomically_revoked_by_owner_token():
    """
    Assert that if a background heartbeat commits version v+1 just around join timeout,
    the owner-token-based revocation succeeds in marking FAILED with no lease lockout,
    and subsequent late renewals are rejected.
    """
    import time

    from app.api.chat_routes import ThreadSafeIdempotencyTracker
    from app.api.chat_routes import idempotency_heartbeat as orig_heartbeat
    from app.models.idempotency import ChatIdempotencyRecord, ChatIdempotencyStatus
    from app.models.user import User
    from app.services.space_service import SpaceService
    from app.services.storage import store

    user = User(uid="user_owner_race_1", email="ownerrace@example.com", display_name="Owner Race User")
    space = SpaceService.ensure_agent_dm(user)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer dev:{user.uid}:{user.email}:{user.display_name}"}

    client_msg_id = "cmsg_owner_race_01"
    idemp_key = ChatIdempotencyRecord.compute_key(space.space_id, user.uid, client_msg_id)

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = MagicMock(
        text='{"project_title": "Race Plan", "summary": "Race Summary", "scenes": []}'
    )

    # 1. First execution: Simulate background heartbeat bumping version to v+1 in storage,
    # but join timeout occurs before the main thread can see the updated version.
    orig_update_fenced = store.update_chat_idempotency_fenced

    def racing_heartbeat_update(key, expected_version, **kwargs):
        # Background renewal succeeds in DB (bumping version to 2)
        return orig_update_fenced(key, expected_version, **kwargs)

    @contextlib.contextmanager
    def racing_heartbeat(tracker: ThreadSafeIdempotencyTracker):
        with orig_heartbeat(tracker, interval_sec=0.01, extend_sec=120, join_timeout_sec=0.03):
            time.sleep(0.04)  # Let background thread perform renewal in DB
            tracker.mark_unhealthy("HEARTBEAT_JOIN_TIMEOUT")
            yield

    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}), \
         patch("app.api.chat_routes.idempotency_heartbeat", side_effect=racing_heartbeat):
        res_fail = client.post(
            "/v1/chat",
            json={"space_id": space.space_id, "content": "@agent Test Owner Race", "client_message_id": client_msg_id, "intent": "create_breakdown"},
            headers=headers,
        )
        assert res_fail.status_code == 409

    # Verify status in database is FAILED (NOT stuck in IN_PROGRESS!)
    rec = store.get_chat_idempotency(idemp_key)
    assert rec is not None
    assert rec.status == ChatIdempotencyStatus.FAILED
    assert rec.lease_owner is None
    assert rec.lease_until is None

    # Verify that any late heartbeat trying to renew with old owner token is rejected
    ok, _ = store.update_chat_idempotency_fenced(
        idemp_key,
        expected_version=rec.version,
        expected_lease_owner="old_stale_owner",
        extend_lease_seconds=120,
    )
    assert ok is False

    # 2. Immediate client retry succeeds with 0-second lockout
    with patch("google.genai.Client", return_value=mock_client), \
         patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaFakeKey123"}):
        res_retry = client.post(
            "/v1/chat",
            json={"space_id": space.space_id, "content": "@agent Test Owner Race", "client_message_id": client_msg_id, "intent": "create_breakdown"},
            headers=headers,
        )
        assert res_retry.status_code == 200
        assert res_retry.json()["run"] is not None











