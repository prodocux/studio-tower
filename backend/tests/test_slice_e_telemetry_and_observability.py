import hashlib
import json
import time
import uuid
import pytest
from typing import Any, Dict, List, Optional
from datetime import UTC, datetime, timedelta

from app.core.config import settings
from opentelemetry import trace

from app.core.otel import (
    initialize_otel,
    get_tracer,
    record_pipeline_span,
    trace_stage,
    flush_telemetry,
    get_in_memory_spans,
    clear_in_memory_spans,
    get_telemetry_exporter_metrics,
    reset_telemetry_exporter_metrics,
    ResilientSpanExporter,
)
from app.core.pricing_catalog import (
    calculate_estimated_cost,
    get_model_family,
    PRICING_CATALOG_VERSION,
)
from app.core.telemetry_sanitizer import (
    sanitize_span_attributes,
    assert_attributes_scrubbed,
)
from app.core.telemetry_tenant import (
    compute_tenant_hash,
    verify_tenant_hash,
    configure_telemetry_keys,
)
from app.models.action_proposal import (
    ActionExecutionRecord,
    ActionExecutionStatus,
    ActionProposal,
    compute_canonical_proposal_hash,
)
from app.models.run import Run, RunStatus, RunTelemetry
from app.models.space import Space
from app.models.telemetry import (
    TelemetryStatus,
    TelemetryErrorCode,
    ALLOWED_TELEMETRY_ERROR_CODES,
    SPAN_ALLOWED_ATTRIBUTES,
    PROMETHEUS_ALLOWED_METRIC_LABELS,
)
from app.models.user import User
from app.services.deliverable_service import DeliverableExecutionService
from app.services.storage import store
from app.services.telemetry_service import TelemetryService
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult


@pytest.fixture(autouse=True)
def setup_telemetry_test_env():
    # Use in-memory provider for deterministic testing
    initialize_otel(use_in_memory=True)
    flush_telemetry()
    clear_in_memory_spans()
    reset_telemetry_exporter_metrics()
    store.clear()
    yield
    flush_telemetry()
    clear_in_memory_spans()
    reset_telemetry_exporter_metrics()
    store.clear()


# =============================================================================
# Gate E1: HMAC Tenant Identity & Dual-Key Rotation
# =============================================================================


def test_gate_e1_hmac_tenant_hash_unidirectionality_and_rotation():
    space_id = "sp_e1_film_lot"
    raw_sha = hashlib.sha256(space_id.encode()).hexdigest()

    # Configure dual keys (primary k1, secondary k2)
    configure_telemetry_keys({"k1": "secret-key-primary-v1", "k2": "secret-key-secondary-v2"}, active_key_id="k1")

    # 1. Compute HMAC tenant hash
    tenant_hash, kid = compute_tenant_hash(space_id)
    assert kid == "k1"
    assert tenant_hash.startswith("th_")
    # Must NOT equal raw sha256 (prevents dictionary/rainbow attacks)
    assert tenant_hash != f"th_{raw_sha[:24]}"
    assert space_id not in tenant_hash

    # 2. Verification against active primary key succeeds
    assert verify_tenant_hash(space_id, tenant_hash) is True
    # Verification against wrong space_id fails
    assert verify_tenant_hash("sp_other_space", tenant_hash) is False

    # 3. Key Rotation: Promote k2 as active
    configure_telemetry_keys({"k1": "secret-key-primary-v1", "k2": "secret-key-secondary-v2"}, active_key_id="k2")

    # Historical hash generated with k1 still verifies against secondary keyring
    assert verify_tenant_hash(space_id, tenant_hash) is True

    # New hash generated with k2
    new_hash, new_kid = compute_tenant_hash(space_id)
    assert new_kid == "k2"
    assert new_hash != tenant_hash
    assert verify_tenant_hash(space_id, new_hash) is True

    # 4. Unknown/revoked key fails verification
    configure_telemetry_keys({"k3": "brand-new-isolated-key"}, active_key_id="k3")
    assert verify_tenant_hash(space_id, tenant_hash) is False
    assert verify_tenant_hash(space_id, new_hash) is False


# =============================================================================
# Gate E2: Span Attribute Allowlist & Sensitive Field Scrubbing
# =============================================================================


def test_gate_e2_span_attribute_allowlist_and_sensitive_field_scrubbing():
    # Attempt to inject unauthorized sensitive fields and forbidden patterns
    toxic_attrs = {
        # Forbidden keys: must be completely stripped
        "authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.secretpayload.sig",
        "api_key": "ai_zaSyA123456789SecretKey",
        "user_email": "director@studio.com",
        "raw_prompt": "INT. BANK VAULT - NIGHT. The thieves crack the safe code 1234.",
        "internal_path": "/var/data/spaces/sp_123/files/confidential.pdf",
        "gcs_uri": "gs://studiotower-prod-vault/deliverables/master.mov",
        "local_path": "D:\\ProDocuX\\incubator\\studiotower\\backend\\data\\secret.key",
        # Allowed keys containing sensitive substrings: values must be sanitized
        "space_id_hash": "th_847192847192847192847192",
        "run_id": "run_e2_valid",
        "stage": "generation",
        "action_type": "generate_call_sheet",
        "status": "ok",
        "error_code": "CUSTOM_LEAKY_EXCEPTION: Connection failed at director@studio.com",
        "model_id": "gemini-2.0-flash",
        "input_tokens": 512,
        "output_tokens": 128,
        "artifact_count": 2,
    }

    clean = sanitize_span_attributes(toxic_attrs)

    # 1. Prohibited keys are completely omitted
    assert "authorization" not in clean
    assert "api_key" not in clean
    assert "user_email" not in clean
    assert "raw_prompt" not in clean
    assert "internal_path" not in clean
    assert "gcs_uri" not in clean
    assert "local_path" not in clean

    # 2. Only strictly allowed keys are present
    assert set(clean.keys()).issubset(SPAN_ALLOWED_ATTRIBUTES)

    # 3. Sensitive pattern inside allowed error_code is standardized/sanitized
    # Custom non-enum string maps to UNKNOWN_INTERNAL_ERROR
    assert clean["error_code"] == TelemetryErrorCode.UNKNOWN_INTERNAL_ERROR.value

    # 4. Clean validation helper confirms no leaks
    assert assert_attributes_scrubbed(clean) is True

    # 5. [P0 Verification] Synthetically raise an exception with a secret sentinel
    # Ensure neither span events nor status.description leak the raw exception
    sentinel = "SYNTHETIC_PRIVATE_SENTINEL_SECRET_TOKEN_9999 /etc/shadow director@secret.com"
    with pytest.raises(RuntimeError):
        with trace_stage("test_sensitive_exception", "th_safe", "run_safe", stage="generation"):
            raise RuntimeError(sentinel)
    flush_telemetry()
    err_spans = [s for s in get_in_memory_spans() if s.name == "test_sensitive_exception"]
    assert len(err_spans) > 0
    err_span = err_spans[-1]

    # A. No OTel "exception" event was recorded
    event_names = [e.name for e in err_span.events]
    assert "exception" not in event_names
    for ev in err_span.events:
        for val in ev.attributes.values():
            assert sentinel not in str(val)

    # B. Status description does NOT contain the raw exception message
    assert sentinel not in (err_span.status.description or "")
    assert err_span.status.description == TelemetryErrorCode.UNKNOWN_INTERNAL_ERROR.value
    assert err_span.attributes.get("error_code") == TelemetryErrorCode.UNKNOWN_INTERNAL_ERROR.value

    # C. Verify manual stage_result['error_code'] normalization against free-text bypasses
    with trace_stage("test_manual_error_normalization", "th_safe", "run_safe", stage="generation") as res:
        res["status"] = "error"
        res["error_code"] = "ARBITRARY_LEAKY_FREE_TEXT_SECRET_XYZ"
    flush_telemetry()
    norm_span = next(s for s in get_in_memory_spans() if s.name == "test_manual_error_normalization")
    assert norm_span.attributes.get("error_code") == TelemetryErrorCode.UNKNOWN_INTERNAL_ERROR.value
    assert norm_span.status.description == TelemetryErrorCode.UNKNOWN_INTERNAL_ERROR.value
    assert res["error_code"] == TelemetryErrorCode.UNKNOWN_INTERNAL_ERROR.value

    # D. Valid enum code is preserved
    with trace_stage("test_valid_enum_error", "th_safe", "run_safe", stage="generation") as res2:
        res2["status"] = "error"
        res2["error_code"] = TelemetryErrorCode.PDX_EXECUTION_FAILURE.value
    flush_telemetry()
    enum_span = next(s for s in get_in_memory_spans() if s.name == "test_valid_enum_error")
    assert enum_span.attributes.get("error_code") == TelemetryErrorCode.PDX_EXECUTION_FAILURE.value
    assert enum_span.status.description == TelemetryErrorCode.PDX_EXECUTION_FAILURE.value


# =============================================================================
# Gate E3: Low-Cardinality Metric Label Enforcement
# =============================================================================


def test_gate_e3_low_cardinality_metric_label_enforcement():
    # Enforces E0 Contract 2: High-cardinality fields are strictly forbidden as metric labels
    forbidden_metric_labels = {
        "run_id",
        "trace_id",
        "user_id",
        "space_id",
        "prompt",
        "file_name",
        "action_id",
        "arbitrary_user_tag",
    }

    for forbidden in forbidden_metric_labels:
        assert forbidden not in PROMETHEUS_ALLOWED_METRIC_LABELS

    # Only strictly controlled low-cardinality dimensions allowed
    assert PROMETHEUS_ALLOWED_METRIC_LABELS == {
        "action_type",
        "status",
        "stage",
        "model_family",
    }

    # Model family maps fine-grained model ID to controlled enum
    assert get_model_family("models/gemini-2.0-flash-exp") == "gemini-2.0-flash"
    assert get_model_family("gemini-1.5-flash-8b") == "gemini-1.5-flash"
    assert get_model_family("gemini-1.5-pro-preview-0409") == "gemini-1.5-pro"
    assert get_model_family("unknown-custom-model") == "other"
    assert get_model_family(None) == "unknown"


# =============================================================================
# Gate E4: Honest Versioned Token Cost Estimation
# =============================================================================


def test_gate_e4_honest_versioned_token_cost_estimation():
    # 1. Valid model with authoritative token breakdown
    # gemini-2.0-flash: in=0.10/M, out=0.40/M, cached=0.025/M
    cost = calculate_estimated_cost(
        model_id="gemini-2.0-flash",
        input_tokens=1_000_000,
        output_tokens=500_000,
        cached_tokens=200_000,
    )
    # Billable in: 800k * 0.10/M = 0.08
    # Cached: 200k * 0.025/M = 0.005
    # Out: 500k * 0.40/M = 0.20
    # Expected total = 0.285 USD
    assert cost == 0.285

    # 2. Honest contract: Unknown model returns None (Zero Guessing)
    assert calculate_estimated_cost("gpt-4-custom-proxy", 100, 100) is None
    assert calculate_estimated_cost(None, 100, 100) is None

    # 3. Missing tokens return None (Never estimate from character/word counts)
    assert calculate_estimated_cost("gemini-2.0-flash", None, 100) is None
    assert calculate_estimated_cost("gemini-2.0-flash", 100, None) is None

    # Pricing version is stamped
    assert PRICING_CATALOG_VERSION == "2026-Q1"


# =============================================================================
# Gate E5: OpenTelemetry Real Pipeline Span Recording & Batch Exporter
# =============================================================================


def test_gate_e5_real_pipeline_span_recording_and_batch_export():
    space_id = "sp_e5_pipeline"
    run_id = "run_e5_001"
    trace_id = "trc_84719284719284719284719284719284"
    space_hash, _ = compute_tenant_hash(space_id)

    # 1. Record authentic spans across pipeline stages
    record_pipeline_span(
        name="action_dispatch",
        space_id_hash=space_hash,
        run_id=run_id,
        trace_id_str=trace_id,
        stage="dispatch",
        action_type="export_scene_list",
        status="ok",
    )
    record_pipeline_span(
        name="action_execute_generation",
        space_id_hash=space_hash,
        run_id=run_id,
        trace_id_str=trace_id,
        stage="generation",
        action_type="export_scene_list",
        status="ok",
        attributes={"artifact_count": 1},
    )
    record_pipeline_span(
        name="artifact_storage_commit",
        space_id_hash=space_hash,
        run_id=run_id,
        trace_id_str=trace_id,
        stage="storage_commit",
        action_type="export_scene_list",
        status="ok",
    )

    # 2. Flush batch processor
    flush_ok = flush_telemetry()
    assert flush_ok is True

    # 3. Verify in-memory collector captured all 3 spans
    spans = [s for s in get_in_memory_spans() if s.attributes.get("run_id") == run_id]
    assert len(spans) == 3

    names = [s.name for s in spans]
    assert names == ["action_dispatch", "action_execute_generation", "artifact_storage_commit"]

    # 4. Spans carry genuine timestamps (start_time < end_time)
    for s in spans:
        assert s.start_time is not None
        assert s.end_time is not None
        assert s.end_time >= s.start_time
        assert s.attributes["space_id_hash"] == space_hash
        assert s.attributes["run_id"] == run_id
        # Raw space_id is NOT in attributes
        assert "space_id" not in s.attributes


# =============================================================================
# Gate E6: Exporter Resilience — Failures Do Not Break Core User Journeys
# =============================================================================


def test_gate_e6_exporter_resilience_preserves_core_user_journey():
    # Simulate completely dead / crashing OTLP exporter
    class CrashingExporter(SpanExporter):
        def export(self, spans):
            raise ConnectionError("OTLP Collector connection refused on port 4318: unreachable")
        def shutdown(self):
            pass
        def force_flush(self, timeout_millis=2000):
            return False

    resilient = ResilientSpanExporter(CrashingExporter())

    user = User(uid="u_e6_actor", email="actor@e6.com")
    store.save_user(user)
    space = Space(space_id="sp_e6_resilient", name="Resilience Space", created_by=user.uid)
    store.create_space(space, creator_uid=user.uid)

    proposal = ActionProposal(
        action_id="act_e6_resilience",
        space_id=space.space_id,
        user_id=user.uid,
        action_type="generate_shot_list",
        title="Shot List Extraction",
        description="Extract scene angles",
        sources=[],
        project_tag="general",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    exec_rec = ActionExecutionRecord(
        action_id=proposal.action_id,
        space_id=proposal.space_id,
        user_id=user.uid,
        run_id="run_e6_resilience",
        status=ActionExecutionStatus.PENDING,
        state_version=1,
    )
    store.create_action_execution_if_absent(exec_rec)

    # 1. Directly invoke export via crashing resilient exporter: returns FAILURE, logs warning, never raises
    export_res = resilient.export([])
    assert export_res == SpanExportResult.FAILURE

    # Dropped metrics counter incremented
    metrics = get_telemetry_exporter_metrics()
    assert metrics["exporter_failures"] >= 1

    # 2. Core deliverable execution continues successfully regardless of exporter health
    run, updated_exec = DeliverableExecutionService.execute_action(proposal, user)

    assert run.status == RunStatus.COMPLETED
    assert updated_exec.status == ActionExecutionStatus.COMPLETED
    assert run.telemetry_status == TelemetryStatus.EXPORTING.value
    assert len(run.output_artifact_ids) == 1


# =============================================================================
# Gate E7: Version-Fenced Telemetry Status Transition
# =============================================================================


def test_gate_e7_version_fenced_telemetry_status_transition():
    space_id = "sp_e7_fencing"
    run_id = "run_e7_fencing"
    trace_id_v1 = "trc_11111111111111111111111111111111"
    trace_id_v2 = "trc_22222222222222222222222222222222"

    run = Run(
        run_id=run_id,
        space_id=space_id,
        trace_id=trace_id_v1,
        status=RunStatus.COMPLETED,
        telemetry_status=TelemetryStatus.EXPORTING.value,
        telemetry_generation=1,
        created_by="u_e7",
    )
    store.save_run(run)

    # 1. Valid async confirmation transitions EXPORTING -> AVAILABLE with matching generation
    updated_run = store.update_run_telemetry_status(
        space_id=space_id,
        run_id=run_id,
        expected_trace_id=trace_id_v1,
        new_status=TelemetryStatus.AVAILABLE.value,
        expected_generation=1,
        expected_status=TelemetryStatus.EXPORTING.value,
    )
    assert updated_run is not None
    assert updated_run.telemetry_status == TelemetryStatus.AVAILABLE.value
    assert updated_run.telemetry.has_real_telemetry is True
    assert updated_run.telemetry_generation == 2
    assert updated_run.telemetry_last_checked_at is not None

    # 2. Stale worker attempting to verify with old generation (1) is rejected (CAS fencing)
    stale_update = store.update_run_telemetry_status(
        space_id=space_id,
        run_id=run_id,
        expected_trace_id=trace_id_v1,
        new_status=TelemetryStatus.DELAYED.value,
        expected_generation=1,  # Stale! Current is 2
    )
    assert stale_update is None
    # State remains AVAILABLE
    refetched = store.get_run(run_id)
    assert refetched.telemetry_status == TelemetryStatus.AVAILABLE.value

    # 3. New execution runs and updates run with a new trace_id_v2
    refetched.trace_id = trace_id_v2
    refetched.telemetry_status = TelemetryStatus.EXPORTING.value
    refetched.telemetry_generation = 3
    store.save_run(refetched)

    # Late confirmation worker for trace_id_v1 cannot overwrite trace_id_v2!
    late_trace_update = store.update_run_telemetry_status(
        space_id=space_id,
        run_id=run_id,
        expected_trace_id=trace_id_v1,  # Does not match active trace_id_v2
        new_status=TelemetryStatus.AVAILABLE.value,
        expected_generation=3,
    )
    assert late_trace_update is None

    # 4. Correct confirmation worker for trace_id_v2 succeeds
    valid_trace_update = store.update_run_telemetry_status(
        space_id=space_id,
        run_id=run_id,
        expected_trace_id=trace_id_v2,
        new_status=TelemetryStatus.AVAILABLE.value,
        expected_generation=3,
    )
    assert valid_trace_update is not None
    assert valid_trace_update.telemetry_status == TelemetryStatus.AVAILABLE.value
    assert valid_trace_update.telemetry_generation == 4


# =============================================================================
# Gate E8: Production HMAC Key Entropy & Secret Manager Enforcement via ENV=production
# =============================================================================


def test_gate_e8_production_hmac_entropy_and_secret_enforcement(monkeypatch):
    from app.core.telemetry_tenant import initialize_telemetry_tenant_keys, compute_tenant_hash
    from app.core.otel import get_tracer

    # 1. Pure ENV=production with missing key raises RuntimeError (without passing environment argument)
    monkeypatch.setenv("ENV", "production")
    monkeypatch.delenv("TELEMETRY_TENANT_KEY_PRIMARY", raising=False)
    with pytest.raises(RuntimeError, match="TELEMETRY_TENANT_KEY_PRIMARY must be set"):
        initialize_telemetry_tenant_keys()

    # 2. Pure ENV=production with short key (< 32 bytes) raises RuntimeError
    monkeypatch.setenv("TELEMETRY_TENANT_KEY_PRIMARY", "short-16byte-key!")
    with pytest.raises(RuntimeError, match="length too short"):
        initialize_telemetry_tenant_keys()

    # 3. Pure ENV=production with valid >= 32 byte secret succeeds
    valid_key = "prod-telemetry-master-secret-key-32bytes-entropy-secure!"
    monkeypatch.setenv("TELEMETRY_TENANT_KEY_PRIMARY", valid_key)
    initialize_telemetry_tenant_keys()
    h, kid = compute_tenant_hash("sp_prod_1")
    assert kid == "k1"

    # 4. In ENV=production without OTLP endpoint, get_tracer returns NoOpTracer (never lazy in_memory)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    tracer = get_tracer("test_prod_noop")
    assert isinstance(tracer, trace.NoOpTracer)

    # Restore dev/test defaults
    monkeypatch.setenv("ENV", "test")
    monkeypatch.delenv("TELEMETRY_TENANT_KEY_PRIMARY", raising=False)
    initialize_telemetry_tenant_keys(environment="test")


# =============================================================================
# Gate E9: End-to-End Distributed traceparent Propagation & True Root Spans
# =============================================================================


def test_gate_e9_w3c_traceparent_injection_and_extraction():
    import json
    from unittest.mock import MagicMock
    from app.core.otel import (
        inject_traceparent,
        extract_traceparent,
        trace_stage,
        generate_otel_trace_id,
        generate_otel_span_id,
    )
    from app.services.action_runner import CloudTasksActionRunner
    from fastapi.testclient import TestClient
    from app.main import app

    test_client = TestClient(app)

    # 1. Unit verification: inject into carrier and extract as remote SpanContext
    carrier = {}
    with trace_stage("test_upstream_dispatch", "th_test123", "run_test", stage="dispatch"):
        inject_traceparent(carrier)

    assert "traceparent" in carrier
    parts = carrier["traceparent"].split("-")
    assert len(parts) == 4
    assert parts[0] == "00"
    assert len(parts[1]) == 32
    assert len(parts[2]) == 16
    assert int(parts[3], 16) & 0x01 == 1
    assert parts[3] in ("01", "03")

    extracted_ctx = extract_traceparent(carrier)
    assert extracted_ctx is not None
    assert extracted_ctx.is_valid is True
    assert extracted_ctx.is_remote is True
    assert f"{extracted_ctx.trace_id:032x}" == parts[1]
    assert f"{extracted_ctx.span_id:016x}" == parts[2]

    # 2. End-to-End Real Dispatch Span -> Cloud Tasks Task Headers -> Worker Execution Waterfall
    user = User(uid="u_e9_worker", email="worker@e9.com")
    store.save_user(user)
    space = Space(space_id="sp_e9_dist", name="Distributed Space", created_by=user.uid)
    store.create_space(space, creator_uid=user.uid)

    proposal = ActionProposal(
        action_id="act_e9_distributed",
        space_id=space.space_id,
        user_id=user.uid,
        action_type="export_scene_list",
        title="Distributed Worker Action",
        description="Verify distributed context propagation",
        sources=[],
        project_tag="general",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    exec_rec = ActionExecutionRecord(
        action_id=proposal.action_id,
        space_id=proposal.space_id,
        user_id=user.uid,
        run_id="run_e9_dist",
        status=ActionExecutionStatus.PENDING,
        proposal_snapshot=proposal,
        proposal_snapshot_hash=compute_canonical_proposal_hash(proposal),
        state_version=1,
    )
    store.create_action_execution_if_absent(exec_rec)

    # Set up CloudTasksActionRunner with mocked client to intercept actual create_task payload
    runner = CloudTasksActionRunner()
    captured_tasks = []
    fake_client = MagicMock()
    fake_client.queue_path.return_value = "projects/test/locations/loc/queues/actions"
    fake_client.create_task.side_effect = lambda parent, task: captured_tasks.append((parent, task))
    runner.client = fake_client

    clear_in_memory_spans()

    # Dispatch action through real CloudTasksActionRunner!
    task_name = runner.dispatch_action(proposal, user.uid, dispatch_generation=1)
    assert task_name.startswith("projects/test/locations/loc/queues/actions/tasks/action-")

    flush_telemetry()
    assert len(captured_tasks) == 1
    _, intercepted_task = captured_tasks[0]
    http_req = intercepted_task["http_request"]
    intercepted_headers = http_req["headers"]
    intercepted_body = json.loads(http_req["body"].decode("utf-8"))

    # Assert real traceparent header was generated during dispatch
    assert "traceparent" in intercepted_headers

    # Verify real action_dispatch span was exported
    dispatch_spans = [s for s in get_in_memory_spans() if s.name == "action_dispatch" and s.attributes.get("run_id") == exec_rec.run_id]
    assert len(dispatch_spans) == 1
    dispatch_span = dispatch_spans[0]
    dispatch_tid = f"{dispatch_span.context.trace_id:032x}"
    dispatch_sid = f"{dispatch_span.context.span_id:016x}"

    # Confirm intercepted traceparent matches the actual exported dispatch span
    traceparent_val = intercepted_headers["traceparent"]
    assert traceparent_val.startswith(f"00-{dispatch_tid}-{dispatch_sid}-")

    # Feed the intercepted headers and body directly into the worker endpoint
    res = test_client.post(
        f"/v1/spaces/{space.space_id}/actions/{proposal.action_id}/execute",
        headers=intercepted_headers,
        json=intercepted_body,
    )
    assert res.status_code == 200

    flush_telemetry()
    worker_spans = [
        s for s in get_in_memory_spans()
        if s.attributes.get("run_id") == exec_rec.run_id and s.name != "action_dispatch"
    ]
    assert len(worker_spans) == 3
    spans_by_name = {s.name: s for s in worker_spans}

    root_worker_span = spans_by_name["action_execution"]
    # Verified: Parent of root worker span matches the REAL EXPORTED dispatch span!
    assert root_worker_span.parent is not None
    assert root_worker_span.parent.is_remote is True
    assert f"{root_worker_span.context.trace_id:032x}" == dispatch_tid
    assert f"{root_worker_span.parent.span_id:016x}" == dispatch_sid

    # 3. Direct execution without upstream context creates a TRUE ROOT SPAN (parent is None)
    proposal_root = ActionProposal(
        action_id="act_e9_true_root",
        space_id=space.space_id,
        user_id=user.uid,
        action_type="export_scene_list",
        title="True Root Action",
        description="Verify true root has no missing parent",
        sources=[],
        project_tag="general",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    exec_root = ActionExecutionRecord(
        action_id=proposal_root.action_id,
        space_id=proposal.space_id,
        user_id=user.uid,
        run_id="run_e9_true_root",
        status=ActionExecutionStatus.PENDING,
        state_version=1,
    )
    store.create_action_execution_if_absent(exec_root)

    clear_in_memory_spans()
    run_root, _ = DeliverableExecutionService.execute_action(proposal_root, user, parent_context=None)
    flush_telemetry()

    spans_root = [s for s in get_in_memory_spans() if s.attributes.get("run_id") == run_root.run_id]
    root_only_span = next(s for s in spans_root if s.name == "action_execution")
    # Verified: True root span has NO parent (never a fake NonRecordingSpan)!
    assert root_only_span.parent is None


# =============================================================================
# Gate E10: Genuine Parent-Child Span Hierarchy & Real Non-Zero Duration Timing
# =============================================================================


def test_gate_e10_parent_child_span_hierarchy_and_real_duration_timing():
    user = User(uid="u_e10_user", email="director@e10.com")
    store.save_user(user)
    space = Space(space_id="sp_e10_film", name="Hierarchy Test Space", created_by=user.uid)
    store.create_space(space, creator_uid=user.uid)

    proposal = ActionProposal(
        action_id="act_e10_hierarchy",
        space_id=space.space_id,
        user_id=user.uid,
        action_type="export_scene_list",
        title="Scene Breakdown Extraction",
        description="Extract scene hierarchy",
        sources=[],
        project_tag="general",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    exec_rec = ActionExecutionRecord(
        action_id=proposal.action_id,
        space_id=proposal.space_id,
        user_id=user.uid,
        run_id="run_e10_hierarchy",
        status=ActionExecutionStatus.PENDING,
        state_version=1,
    )
    store.create_action_execution_if_absent(exec_rec)

    # Execute action through fenced lifecycle
    run, updated_exec = DeliverableExecutionService.execute_action(proposal, user)

    # Flush batch processor to in-memory exporter
    flush_telemetry()
    spans = [s for s in get_in_memory_spans() if s.attributes.get("run_id") == run.run_id]

    assert len(spans) == 3
    spans_by_name = {s.name: s for s in spans}

    assert "action_execution" in spans_by_name
    assert "action_execute_generation" in spans_by_name
    assert "artifact_storage_commit" in spans_by_name

    root_span = spans_by_name["action_execution"]
    gen_span = spans_by_name["action_execute_generation"]
    commit_span = spans_by_name["artifact_storage_commit"]

    # 1. All spans share the same authentic 32-hex trace_id
    assert root_span.context.trace_id == gen_span.context.trace_id
    assert root_span.context.trace_id == commit_span.context.trace_id
    assert f"{root_span.context.trace_id:032x}" == run.trace_id

    # 2. Child spans have parent_span_id matching root span's span_id
    assert gen_span.parent.span_id == root_span.context.span_id
    assert commit_span.parent.span_id == root_span.context.span_id

    # 3. Spans measure genuine duration (start_time < end_time)
    for s in spans:
        assert s.start_time is not None
        assert s.end_time is not None
        assert s.end_time >= s.start_time

    # 4. run.telemetry.has_real_telemetry is False until async verification!
    assert run.telemetry.has_real_telemetry is False
    assert run.telemetry_status == TelemetryStatus.EXPORTING.value


# =============================================================================
# Gate E11: Unconfigured OTLP Sets Status Unavailable (Never Fake Available)
# =============================================================================


def test_gate_e11_unconfigured_otlp_sets_status_unavailable(monkeypatch):
    from app.core.otel import (
        initialize_otel,
        is_telemetry_available,
        get_initial_telemetry_status,
        shutdown_otel,
    )

    # In production without OTLP endpoint configured
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    configured = initialize_otel(
        service_name="studiotower-api",
        otlp_endpoint=None,
        use_in_memory=False,
        environment="production",
    )

    assert configured is False
    assert is_telemetry_available() is False
    # Must explicitly declare UNAVAILABLE
    assert get_initial_telemetry_status() == TelemetryStatus.UNAVAILABLE.value

    # Restore in-memory provider for test environment
    shutdown_otel()
    initialize_otel(use_in_memory=True, environment="test")


# =============================================================================
# Gate E12: Strict Model ID Pricing Catalog Validation Without Substring Collision
# =============================================================================


def test_gate_e12_strict_model_pricing_catalog_exact_match():
    # 1. Exact canonical model succeeds
    cost = calculate_estimated_cost("models/gemini-2.0-flash", 100_000, 10_000)
    assert cost is not None
    assert cost > 0

    # 2. Substring collision attempts return None
    assert calculate_estimated_cost("gemini-2.0-flash-fake-postfix", 100_000, 10_000) is None
    assert calculate_estimated_cost("fake-prefix-gemini-2.0-flash", 100_000, 10_000) is None
    assert calculate_estimated_cost("gemini-1.5-pro-custom-hacked", 100_000, 10_000) is None

    # 3. Expired date window returns None
    past_validity = datetime(2027, 1, 1, tzinfo=UTC)
    assert calculate_estimated_cost("gemini-2.0-flash", 100_000, 10_000, evaluation_time=past_validity) is None

    # 4. Unknown catalog version returns None
    assert calculate_estimated_cost("gemini-2.0-flash", 100_000, 10_000, catalog_version="1999-Q1") is None


# =============================================================================
# Gate E13: TracerProvider Lifecycle Isolation Across Shutdown & Re-initialization
# =============================================================================


def test_gate_e13_tracer_provider_lifecycle_isolation():
    from app.core.otel import (
        initialize_otel,
        shutdown_otel,
        get_in_memory_spans,
        flush_telemetry,
        trace_stage,
    )

    # 1. Cycle 1: Initialize, record span, flush, verify export
    initialize_otel(use_in_memory=True, environment="test")
    with trace_stage("span_cycle_1", "th_c1", "run_c1", stage="generation"):
        pass
    flush_telemetry()
    spans_1 = [s for s in get_in_memory_spans() if s.name == "span_cycle_1"]
    assert len(spans_1) == 1

    # 2. Shutdown provider cleanly
    shutdown_otel(timeout_millis=1000)

    # 3. Cycle 2: Re-initialize fresh provider, record span, flush, verify export works cleanly
    initialize_otel(use_in_memory=True, environment="test")
    with trace_stage("span_cycle_2", "th_c2", "run_c2", stage="storage_commit"):
        pass
    flush_telemetry()
    spans_2 = [s for s in get_in_memory_spans() if s.name == "span_cycle_2"]
    assert len(spans_2) == 1
# =============================================================================
# Gate E14: Authentic Trace Verification, Rotated Keys, & Tenant / Run Identity Matching
# =============================================================================


def test_gate_e14_trace_verification_and_tenant_run_identity_matching():
    import hmac
    import hashlib
    from app.services.telemetry_service import (
        TelemetryService,
        MockTracingClient,
        verify_trace_contract,
    )
    from app.core.telemetry_tenant import (
        compute_tenant_hash,
        configure_telemetry_keys,
        initialize_telemetry_tenant_keys,
    )

    user = User(uid="u_e14_dir", email="director@e14.com")
    store.save_user(user)
    space_a = Space(space_id="sp_e14_alpha", name="Alpha Studio", created_by=user.uid)
    space_b = Space(space_id="sp_e14_beta", name="Beta Studio", created_by=user.uid)
    store.create_space(space_a, creator_uid=user.uid)
    store.create_space(space_b, creator_uid=user.uid)

    hash_a, _ = compute_tenant_hash(space_a.space_id)
    hash_b, _ = compute_tenant_hash(space_b.space_id)

    run_a = Run(
        run_id="run_e14_valid",
        space_id=space_a.space_id,
        created_by=user.uid,
        trace_id="0123456789abcdef0123456789abcdef",
        telemetry_status=TelemetryStatus.EXPORTING.value,
        telemetry_generation=1,
    )
    store.save_run(run_a)

    # 1. Matching tenant hash AND matching run_id -> Contract matches!
    valid_trace = {
        "trace_id": run_a.trace_id,
        "spans": [
            {
                "name": "action_execution",
                "span_id": "0123456789abcdef",
                "trace_id": run_a.trace_id,
                "attributes": {
                    "space_id_hash": hash_a,
                    "run_id": run_a.run_id,
                    "action_type": "export_scene_list",
                },
            }
        ],
    }
    assert verify_trace_contract(valid_trace, space_a.space_id, run_a.run_id, expected_trace_id=run_a.trace_id) is True

    # Verification service transitions status to AVAILABLE with has_real_telemetry=True
    mock_client = MockTracingClient(traces={run_a.trace_id: valid_trace})
    res = TelemetryService.verify_run_telemetry(
        space_id=space_a.space_id,
        run_id=run_a.run_id,
        backend_client=mock_client,
        force=True,
    )
    assert res.current_status == TelemetryStatus.AVAILABLE.value
    assert res.matched is True

    updated_run = store.get_run(run_a.run_id)
    assert updated_run.telemetry_status == TelemetryStatus.AVAILABLE.value
    assert updated_run.telemetry.has_real_telemetry is True
    assert updated_run.telemetry_generation == 2

    # 2. Rotated Historical Key Support (kid / versioning)
    new_key = "primary-key-entropy-32-bytes-long-secure-new!"
    old_key = "historical-key-entropy-32-bytes-prev-old!"
    configure_telemetry_keys(
        keys={"k1": old_key, "k2": new_key},
        active_key_id="k2",
    )
    # Generate historical span hash using rotated old key (k1)
    old_hash, _ = compute_tenant_hash(space_a.space_id, key_id="k1")
    historical_trace = {
        "trace_id": run_a.trace_id,
        "spans": [
            {
                "name": "pipeline_stage",
                "span_id": "fedcba9876543210",
                "trace_id": run_a.trace_id,
                "attributes": {
                    "space_id_hash": old_hash,  # Matches historical rotated key!
                    "run_id": run_a.run_id,
                },
            }
        ],
    }
    # Trace signed with historical key passes verification!
    assert verify_trace_contract(historical_trace, space_a.space_id, run_a.run_id, expected_trace_id=run_a.trace_id) is True
    # Restore dev/test defaults
    initialize_telemetry_tenant_keys(environment="test")

    # 3. Foreign tenant hash -> Strict tenant boundary violation rejects match!
    foreign_trace = {
        "trace_id": "foreign_trace_tid_12345",
        "spans": [
            {
                "name": "action_execution",
                "span_id": "0123456789abcdef",
                "attributes": {
                    "space_id_hash": hash_b,  # Foreign tenant!
                    "run_id": "run_e14_foreign",
                },
            }
        ],
    }
    assert verify_trace_contract(foreign_trace, space_a.space_id, "run_e14_foreign") is False

    # 4. Mixed run in same trace -> Rejected
    mixed_run_trace = {
        "trace_id": run_a.trace_id,
        "spans": [
            {
                "name": "action_execution",
                "span_id": "0123456789abcdef",
                "attributes": {
                    "space_id_hash": hash_a,
                    "run_id": "different_run_id_999",  # Mixed run!
                },
            }
        ],
    }
    assert verify_trace_contract(mixed_run_trace, space_a.space_id, run_a.run_id) is False

    # 5. Expected Trace ID mismatch -> Rejected
    assert verify_trace_contract(valid_trace, space_a.space_id, run_a.run_id, expected_trace_id="wrong_expected_trace_id") is False

    # 6. Infrastructure-only trace (missing execution span) -> Rejected
    infra_only_trace = {
        "trace_id": run_a.trace_id,
        "spans": [
            {
                "name": "random_unknown_span",
                "span_id": "0123456789abcdef",
                "attributes": {"space_id_hash": hash_a, "run_id": run_a.run_id},
            }
        ],
    }
    assert verify_trace_contract(infra_only_trace, space_a.space_id, run_a.run_id, expected_trace_id=run_a.trace_id) is False

    # 7. Trace completely missing Trace ID evidence -> Rejected (Item 3 fix)
    trace_missing_tid = {
        "spans": [
            {
                "name": "action_execution",
                "span_id": "0123456789abcdef",
                "attributes": {"space_id_hash": hash_a, "run_id": run_a.run_id},
            }
        ]
    }
    assert verify_trace_contract(trace_missing_tid, space_a.space_id, run_a.run_id, expected_trace_id=run_a.trace_id) is False

    # 8. Separate execution node and tenant/run span without joint satisfaction -> Rejected (Item 3 fix)
    split_spans_trace = {
        "trace_id": run_a.trace_id,
        "spans": [
            {
                "name": "action_execution",  # Has execution name, but lacks tenant/run
                "span_id": "1111111111111111",
                "attributes": {},
            },
            {
                "name": "random_non_exec_span",  # Has tenant/run, but NOT an execution node
                "span_id": "2222222222222222",
                "attributes": {"space_id_hash": hash_a, "run_id": run_a.run_id},
            },
        ],
    }
    assert verify_trace_contract(split_spans_trace, space_a.space_id, run_a.run_id, expected_trace_id=run_a.trace_id) is False

    # 9. Tempo OTLP batches format support
    tempo_otlp_trace = {
        "batches": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "name": "action_execution",
                                "spanId": "abcdef1234567890",
                                "traceId": run_a.trace_id,
                                "attributes": [
                                    {"key": "space_id_hash", "value": {"stringValue": hash_a}},
                                    {"key": "run_id", "value": {"stringValue": run_a.run_id}},
                                ],
                            }
                        ]
                    }
                ]
            }
        ]
    }
    assert verify_trace_contract(tempo_otlp_trace, space_a.space_id, run_a.run_id, expected_trace_id=run_a.trace_id) is True


# =============================================================================
# Gate E15: In-Flight Service Race, Expired Lease & CAS Version Fencing
# =============================================================================


def test_gate_e15_cas_version_fencing_rejects_stale_verifications():
    from app.services.telemetry_service import TelemetryService, TracingBackendClient
    from app.core.telemetry_tenant import compute_tenant_hash

    user = User(uid="u_e15_cas", email="cas@e15.com")
    store.save_user(user)
    space = Space(space_id="sp_e15_fencing", name="Fencing Space", created_by=user.uid)
    store.create_space(space, creator_uid=user.uid)

    hash_val, _ = compute_tenant_hash(space.space_id)
    trace_id = "11111111111111111111111111111111"

    run = Run(
        run_id="run_e15_cas",
        space_id=space.space_id,
        created_by=user.uid,
        trace_id=trace_id,
        telemetry_status=TelemetryStatus.EXPORTING.value,
        telemetry_generation=1,
    )
    store.save_run(run)

    trace_data = {
        "trace_id": trace_id,
        "spans": [
            {
                "name": "action_execution",
                "span_id": "0123456789abcdef",
                "attributes": {"space_id_hash": hash_val, "run_id": run.run_id},
            }
        ],
    }

    # 1. Custom backend client that triggers a concurrent run generation bump while get_trace() is in-flight
    class InFlightRaceBackendClient(TracingBackendClient):
        def __init__(self, run_obj: Run):
            self.run_obj = run_obj

        def get_trace(self, tid: str):
            # In-flight race: Another process modifies the run generation before backend returns!
            bumped = store.update_run_telemetry_status(
                space_id=self.run_obj.space_id,
                run_id=self.run_obj.run_id,
                expected_trace_id=self.run_obj.trace_id,
                new_status=TelemetryStatus.EXPORTING.value,
                expected_generation=1,
            )
            assert bumped is not None
            assert bumped.telemetry_generation == 2
            return trace_data

    race_client = InFlightRaceBackendClient(run)

    # Worker 1 initiates verify_run_telemetry with snapshot gen=1
    res = TelemetryService.verify_run_telemetry(
        space_id=space.space_id,
        run_id=run.run_id,
        backend_client=race_client,
        force=True,
    )

    # When Worker 1 returns from get_trace and attempts write-back, CAS fails!
    assert res.stale_rejected is True
    assert res.matched is True

    # Storage remains at generation 2 and was not overwritten by stale Worker 1
    current_run = store.get_run(run.run_id)
    assert current_run.telemetry_generation == 2
    assert current_run.telemetry_status == TelemetryStatus.EXPORTING.value

    # 2. Item 1 Fix: Expired lease rejection ("租約過期但尚未被接管")
    run_exp = Run(
        run_id="run_e15_expired_lease",
        space_id=space.space_id,
        created_by=user.uid,
        trace_id="11111111111111111111111111111112",
        telemetry_status=TelemetryStatus.EXPORTING.value,
        telemetry_generation=1,
        telemetry_lease_token="tok_expired",
        telemetry_lease_until=datetime.now(UTC) - timedelta(seconds=2),  # Expired lease!
    )
    store.save_run(run_exp)

    # Attempting write-back with valid token but expired lease_until is strictly rejected!
    res_exp = store.update_run_telemetry_status(
        space_id=space.space_id,
        run_id=run_exp.run_id,
        expected_trace_id=run_exp.trace_id,
        new_status=TelemetryStatus.AVAILABLE.value,
        expected_generation=1,
        expected_lease_token="tok_expired",
    )
    assert res_exp is None  # Rejected!

    # 3. Item 1 Fix: Missing lease rejection ("租約缺失")
    run_missing_lease = Run(
        run_id="run_e15_missing_lease",
        space_id=space.space_id,
        created_by=user.uid,
        trace_id="11111111111111111111111111111113",
        telemetry_status=TelemetryStatus.EXPORTING.value,
        telemetry_generation=1,
        telemetry_lease_token="tok_valid",
        telemetry_lease_until=None,  # Missing lease_until!
    )
    store.save_run(run_missing_lease)

    res_missing = store.update_run_telemetry_status(
        space_id=space.space_id,
        run_id=run_missing_lease.run_id,
        expected_trace_id=run_missing_lease.trace_id,
        new_status=TelemetryStatus.AVAILABLE.value,
        expected_generation=1,
        expected_lease_token="tok_valid",
    )
    assert res_missing is None  # Rejected!


# =============================================================================
# Gate E16: Grace Period, Single-Flight Concurrency & Error Differentiation
# =============================================================================


def test_gate_e16_delay_rate_throttling_and_unavailability_differentiation(monkeypatch):
    import time
    from concurrent.futures import ThreadPoolExecutor
    from app.services.telemetry_service import TelemetryService, MockTracingClient, TracingBackendClient

    user = User(uid="u_e16_rate", email="rate@e16.com")
    store.save_user(user)
    space = Space(space_id="sp_e16_rate", name="Rate Space", created_by=user.uid)
    store.create_space(space, creator_uid=user.uid)

    now = datetime.now(UTC)
    # Long running job: created 2 hours ago, but export started 5 seconds ago!
    run = Run(
        run_id="run_e16_transient",
        space_id=space.space_id,
        created_by=user.uid,
        trace_id="33333333333333333333333333333333",
        created_at=now - timedelta(hours=2),
        telemetry_export_started_at=now - timedelta(seconds=5),
        telemetry_status=TelemetryStatus.EXPORTING.value,
        telemetry_generation=1,
        telemetry_attempts=0,
    )
    store.save_run(run)

    # 1. First verification with 404 (not yet ingested):
    # Grace period measured from telemetry_export_started_at (5s < 30s) -> DELAYED, NOT UNAVAILABLE!
    empty_mock = MockTracingClient(traces={})
    res1 = TelemetryService.verify_run_telemetry(
        space_id=space.space_id,
        run_id=run.run_id,
        backend_client=empty_mock,
        force=True,
    )
    assert res1.current_status == TelemetryStatus.DELAYED.value
    assert res1.matched is False
    assert res1.attempts == 1

    # Check that backoff timer next_check_at was set
    run_after_res1 = store.get_run(run.run_id)
    assert run_after_res1.telemetry_next_check_at is not None
    assert run_after_res1.telemetry_next_check_at > now

    # 2. Single-Flight Concurrency: Multiple simultaneous requests query backend strictly ONCE
    run_sf = Run(
        run_id="run_e16_single_flight",
        space_id=space.space_id,
        created_by=user.uid,
        trace_id="33333333333333333333333333333334",
        telemetry_status=TelemetryStatus.EXPORTING.value,
        telemetry_generation=1,
    )
    store.save_run(run_sf)

    class SlowMockTracingClient(TracingBackendClient):
        def __init__(self):
            self.call_count = 0

        def get_trace(self, tid: str):
            self.call_count += 1
            time.sleep(0.08)
            return None

    slow_client = SlowMockTracingClient()

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [
            executor.submit(
                TelemetryService.verify_run_telemetry,
                space_id=space.space_id,
                run_id=run_sf.run_id,
                backend_client=slow_client,
                force=False,
            )
            for _ in range(3)
        ]
        results = [f.result() for f in futures]

    # Exactly 1 thread won the lease and called the backend client!
    assert slow_client.call_count == 1
    # Exactly 1 thread got non-throttled, and 2 threads got throttled
    throttled_count = sum(1 for r in results if r.throttled)
    assert throttled_count == 2

    # 3. Verify hard rate limit floor: even with force=True, calling within < 1.0s is throttled
    fail_mock = MockTracingClient(fail_trace_ids={run.trace_id})
    res_floor = TelemetryService.verify_run_telemetry(
        space_id=space.space_id,
        run_id=run.run_id,
        backend_client=fail_mock,
        force=True,
    )
    assert res_floor.throttled is True

    # Advance telemetry_last_checked_at past hard floor (>= 1.0s) to allow attempt 2
    r_update = store.get_run(run.run_id)
    r_update.telemetry_last_checked_at = now - timedelta(seconds=2)
    store.save_run(r_update)

    res2 = TelemetryService.verify_run_telemetry(
        space_id=space.space_id,
        run_id=run.run_id,
        backend_client=fail_mock,
        force=True,
    )
    assert res2.current_status == TelemetryStatus.DELAYED.value
    assert res2.attempts == 2

    # Advance telemetry_last_checked_at past hard floor to allow attempt 3
    r_update = store.get_run(run.run_id)
    r_update.telemetry_last_checked_at = now - timedelta(seconds=2)
    store.save_run(r_update)

    # 4. Max attempts exceeded (attempt 3) -> Transitions to UNAVAILABLE
    res3 = TelemetryService.verify_run_telemetry(
        space_id=space.space_id,
        run_id=run.run_id,
        backend_client=empty_mock,
        force=True,
    )
    assert res3.current_status == TelemetryStatus.UNAVAILABLE.value
    assert res3.attempts == 3

    final_run = store.get_run(run.run_id)
    assert final_run.telemetry_status == TelemetryStatus.UNAVAILABLE.value
    assert final_run.telemetry.has_real_telemetry is False

    # 5. Item 4 Fix: force=True cannot infinitely spam terminal runs!
    # Within 60s terminal cooldown, force=True returns throttled and does NOT query backend
    query_mock = MockTracingClient(traces={})
    res_term_spam = TelemetryService.verify_run_telemetry(
        space_id=space.space_id,
        run_id=run.run_id,
        backend_client=query_mock,
        force=True,
    )
    assert res_term_spam.throttled is True
    assert query_mock.call_count == 0  # Backend was NOT queried!

    # Advance clock past terminal cooldown (>= 60s): controlled recovery is permitted and resets attempt budget
    r_term = store.get_run(run.run_id)
    r_term.telemetry_last_checked_at = now - timedelta(seconds=65)
    store.save_run(r_term)

    res_recovery = TelemetryService.verify_run_telemetry(
        space_id=space.space_id,
        run_id=run.run_id,
        backend_client=query_mock,
        force=True,
    )
    assert res_recovery.throttled is False
    assert query_mock.call_count == 1  # Controlled recovery queried backend once!
    assert res_recovery.attempts == 1  # Attempt budget was safely reset to 1!

    # 6. Item 2 Fix: Realistic long-running action execution path verifies telemetry_export_started_at
    # is stamped at action completion time, NOT at action dispatch/proposal time.
    from app.services.deliverable_service import DeliverableExecutionService
    proposal_long = ActionProposal(
        action_id="act_e16_long_task",
        space_id=space.space_id,
        user_id=user.uid,
        action_type="export_scene_list",
        title="Long-Running Scene Extraction",
        description="Verify export_started_at timing",
        sources=[],
        project_tag="general",
        expires_at=now + timedelta(hours=1),
    )
    exec_long = ActionExecutionRecord(
        action_id=proposal_long.action_id,
        space_id=proposal_long.space_id,
        user_id=user.uid,
        run_id="run_e16_long_task",
        status=ActionExecutionStatus.PENDING,
        state_version=1,
    )
    store.create_action_execution_if_absent(exec_long)

    # Initial run was created when action was proposed (e.g. 50 seconds in the past)
    simulated_start_time = datetime.now(UTC) - timedelta(seconds=50)
    init_run = Run(
        run_id=exec_long.run_id,
        space_id=space.space_id,
        created_by=user.uid,
        trace_id="88888888888888888888888888888888",
        created_at=simulated_start_time,
        updated_at=simulated_start_time,
        telemetry_status=TelemetryStatus.EXPORTING.value,
        telemetry_generation=1,
    )
    store.save_run(init_run)

    # Execute action through the genuine DeliverableExecutionService pipeline
    completed_run, _ = DeliverableExecutionService.execute_action(proposal_long, user)

    # Assert telemetry_export_started_at was stamped at actual completion time (> simulated_start_time + 40s)
    assert completed_run.telemetry_export_started_at is not None
    assert (completed_run.telemetry_export_started_at - simulated_start_time).total_seconds() > 40.0
    assert completed_run.telemetry_attempts == 0
    assert completed_run.telemetry_next_check_at is None

    # First 404 verification against this long task (where created_at was 50s ago):
    # Because export_started_at was stamped at completion, age_seconds is < 30s grace period -> DELAYED!
    res_long_404 = TelemetryService.verify_run_telemetry(
        space_id=space.space_id,
        run_id=completed_run.run_id,
        backend_client=empty_mock,
        force=True,
    )
    assert res_long_404.current_status == TelemetryStatus.DELAYED.value
    assert res_long_404.attempts == 1


# =============================================================================
# Gate E17: Background Reconciler, Cloud Scheduler Maintenance & Desensitized 500s
# =============================================================================


def test_gate_e17_background_reconciler_and_http_endpoint(monkeypatch):
    from app.services.telemetry_service import TelemetryService, MockTracingClient
    from app.core.telemetry_tenant import compute_tenant_hash
    from app.core.config import settings
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)

    user = User(uid="u_e17_user", email="owner@e17.com")
    store.save_user(user)
    space = Space(space_id="sp_e17_rec", name="Reconcile Space", created_by=user.uid)
    store.create_space(space, creator_uid=user.uid)

    hash_val, _ = compute_tenant_hash(space.space_id)

    # Create 2 runs: one valid trace, one missing trace
    run_avail = Run(
        run_id="run_e17_avail",
        space_id=space.space_id,
        created_by=user.uid,
        trace_id="44444444444444444444444444444444",
        telemetry_status=TelemetryStatus.EXPORTING.value,
        telemetry_generation=1,
    )
    run_delay = Run(
        run_id="run_e17_delay",
        space_id=space.space_id,
        created_by=user.uid,
        trace_id="55555555555555555555555555555555",
        telemetry_status=TelemetryStatus.EXPORTING.value,
        telemetry_generation=1,
    )
    store.save_run(run_avail)
    store.save_run(run_delay)

    traces = {
        run_avail.trace_id: {
            "trace_id": run_avail.trace_id,
            "spans": [
                {
                    "name": "action_execution",
                    "span_id": "0123456789abcdef",
                    "attributes": {"space_id_hash": hash_val, "run_id": run_avail.run_id},
                }
            ],
        }
    }
    mock_client = MockTracingClient(traces=traces)

    # 1. Background Reconciler scans and reconciles pending runs
    stats = TelemetryService.reconcile_pending_telemetry_runs(
        space_id=space.space_id,
        backend_client=mock_client,
    )
    assert stats["checked"] == 2
    assert stats["available"] == 1
    assert stats["delayed"] == 1

    # 2. Item 5 Fix: Cold start / pending list ordering does not miss expired jobs behind unexpired jobs
    now_pending = datetime.now(UTC)
    space_pending = Space(space_id="sp_e17_cold_start", name="Cold Start Space", created_by=user.uid)
    store.create_space(space_pending, creator_uid=user.uid)

    # Create 3 runs: First 2 runs have unexpired future next_check_at, 3rd run has expired past next_check_at
    for idx in range(2):
        r_unexpired = Run(
            run_id=f"run_e17_unexpired_{idx}",
            space_id=space_pending.space_id,
            created_by=user.uid,
            trace_id=f"6666666666666666666666666666666{idx}",
            telemetry_status=TelemetryStatus.DELAYED.value,
            telemetry_generation=1,
            telemetry_next_check_at=now_pending + timedelta(seconds=120),  # Not ready!
            telemetry_last_checked_at=now_pending,
        )
        store.save_run(r_unexpired)

    r_ready = Run(
        run_id="run_e17_ready_item",
        space_id=space_pending.space_id,
        created_by=user.uid,
        trace_id="77777777777777777777777777777777",
        telemetry_status=TelemetryStatus.DELAYED.value,
        telemetry_generation=1,
        telemetry_next_check_at=now_pending - timedelta(seconds=10),  # Ready for check!
        telemetry_last_checked_at=now_pending - timedelta(seconds=60),
    )
    store.save_run(r_ready)

    # With limit=1, the ready item must be found even though unexpired items preceded it in storage!
    pending_found = store.list_pending_telemetry_runs(space_id=space_pending.space_id, limit=1, now=now_pending)
    assert len(pending_found) == 1
    assert pending_found[0].run_id == "run_e17_ready_item"

    # 3. Cloud Scheduler Maintenance Trigger (Cycle 5 integration)
    maint_headers = {"X-StudioTower-Maintenance-Secret": settings.STUDIO_TOWER_MAINTENANCE_SECRET}
    maint_res = client.post("/v1/maintenance/reconcile-actions-and-cleanup", headers=maint_headers)
    assert maint_res.status_code == 200
    maint_data = maint_res.json()
    assert maint_data["status"] == "success"
    assert "telemetry_reconciled" in maint_data["metrics"]
    assert "telemetry_stats" in maint_data["metrics"]

    # 4. HTTP Endpoint verification: Unauthorized user gets 403
    foreign_user = User(uid="u_e17_stranger", email="stranger@e17.com")
    store.save_user(foreign_user)

    auth_headers = {"Authorization": f"Bearer dev:{foreign_user.uid}:{foreign_user.email}"}
    res_unauth = client.post(
        f"/v1/spaces/{space.space_id}/runs/{run_avail.run_id}/verify-telemetry",
        headers=auth_headers,
    )
    assert res_unauth.status_code == 403

    # 5. HTTP Endpoint verification: Space member gets 200 with verification details
    member_headers = {"Authorization": f"Bearer dev:{user.uid}:{user.email}"}
    res_auth = client.post(
        f"/v1/spaces/{space.space_id}/runs/{run_avail.run_id}/verify-telemetry",
        headers=member_headers,
        json={"force": True},
    )
    assert res_auth.status_code == 200
    data = res_auth.json()
    assert data["run_id"] == run_avail.run_id
    assert data["current_status"] == TelemetryStatus.AVAILABLE.value
    assert data["matched"] is True

    # 6. HTTP 500 Sanitization & Correlation ID: Internal exceptions return desensitized detail
    def mock_internal_error(*args, **kwargs):
        raise RuntimeError("LEAKED_INTERNAL_DATABASE_CREDENTIALS_SENTINEL")

    monkeypatch.setattr(TelemetryService, "verify_run_telemetry", mock_internal_error)

    res_err = client.post(
        f"/v1/spaces/{space.space_id}/runs/{run_avail.run_id}/verify-telemetry",
        headers=member_headers,
        json={"force": True},
    )
    assert res_err.status_code == 500
    err_detail = res_err.json()["detail"]
    assert "TELEMETRY_VERIFICATION_ERROR" in err_detail
    assert "correlation_id=corr_" in err_detail
    # Sensitive internal leak must NEVER be exposed to client
    assert "LEAKED_INTERNAL_DATABASE_CREDENTIALS_SENTINEL" not in err_detail


# -----------------------------------------------------------------------------
# Gate E18: Firestore list_pending_telemetry_runs Cursor Advancement & Bounded Scan
# -----------------------------------------------------------------------------


def test_gate_e18_firestore_pending_runs_cursor_bounded_scan_advancement():
    """
    Validates that FirestoreStore.list_pending_telemetry_runs:
    1. Persists continuation cursor to Firestore so a new Store instance (simulating
       Cloud Run cold starts or multi-instance scaling) picks up where the previous left off.
    2. Isolates progress per scope (global '__all__' vs each space_id) preventing cross-scope overwrites.
    3. Gracefully and boundedly recovers from the beginning if a cursor document is deleted/purged.
    """
    import sys
    from unittest.mock import MagicMock, patch
    from app.services.storage import FirestoreStore

    space_id = "sp_e18_firestore_scan"
    now = datetime.now(UTC)

    # In-memory document storage backing our fake Firestore client
    db_storage = {
        "runs": {},
        "telemetry_scan_cursors": {},
    }

    class FakeDocSnap:
        def __init__(self, doc_id, data):
            self.id = doc_id
            self._data = dict(data) if data else {}
        @property
        def exists(self):
            return bool(self._data)
        def to_dict(self):
            return dict(self._data)

    class FakeDocRef:
        def __init__(self, doc_id, storage_dict):
            self.id = doc_id
            self._storage = storage_dict
        def get(self, transaction=None):
            return FakeDocSnap(self.id, self._storage.get(self.id))
        def set(self, data):
            self._storage[self.id] = dict(data)
        def update(self, data):
            rec = self._storage.setdefault(self.id, {})
            rec.update(data)

    class FakeQuery:
        def __init__(self, docs_list):
            self._docs = list(docs_list)
        def where(self, field, op, val):
            if op == "==":
                filtered = [d for d in self._docs if d.to_dict().get(field) == val]
            elif op == "in":
                filtered = [d for d in self._docs if d.to_dict().get(field) in val]
            else:
                filtered = self._docs
            return FakeQuery(filtered)
        def order_by(self, field, **kwargs):
            return self
        def start_after(self, doc_snap):
            target_id = doc_snap.id if hasattr(doc_snap, "id") else str(doc_snap)
            for idx, d in enumerate(self._docs):
                if d.id == target_id:
                    return FakeQuery(self._docs[idx + 1:])
            return FakeQuery([])
        def stream(self):
            return iter(list(self._docs))

    class FakeCollection:
        def __init__(self, storage_dict):
            self._storage = storage_dict
        def document(self, doc_id):
            return FakeDocRef(doc_id, self._storage)
        def where(self, field, op, val):
            sorted_keys = sorted(self._storage.keys())
            docs = [FakeDocSnap(k, self._storage[k]) for k in sorted_keys]
            q = FakeQuery(docs)
            return q.where(field, op, val)

    class FakeTx:
        _read_only = False
        _id = b"fake_tx_id"
        _max_attempts = 5

        def set(self, ref, data):
            ref.set(data)

        def update(self, ref, data):
            ref.update(data)

    class FakeFirestoreClient:
        def __init__(self, backing_db):
            self._db = backing_db
        def collection(self, name):
            col_dict = self._db.setdefault(name, {})
            return FakeCollection(col_dict)
        def transaction(self):
            return FakeTx()

    mock_fs = MagicMock()
    mock_fs.transactional = lambda fn: (lambda tx, *args, **kwargs: fn(tx, *args, **kwargs))

    from contextlib import nullcontext
    gc = sys.modules.get("google.cloud")
    cm_gc = patch.object(gc, "firestore", mock_fs) if (gc and hasattr(gc, "firestore")) else nullcontext()

    with patch.dict(sys.modules, {"google.cloud.firestore": mock_fs}), cm_gc:
        fake_client = FakeFirestoreClient(db_storage)

        # 1. Populate Firestore with 505 runs in space_id:
        # - Items 000..499 (500 items): Unexpired future check time (now + 120s)
        # - Item 500 (1 item): Expired check time (now - 10s) -> The target due item!
        # - Items 501..504 (4 items): Unexpired future check time (now + 120s)
        db_runs = db_storage["runs"]
        for idx in range(500):
            r_unexpired = Run(
                run_id=f"run_e18_item_{idx:04d}",
                space_id=space_id,
                created_by="u_e18",
                trace_id=f"e18_{idx:032x}"[-32:],
                telemetry_status=TelemetryStatus.DELAYED.value,
                telemetry_generation=1,
                telemetry_next_check_at=now + timedelta(seconds=120),
                telemetry_last_checked_at=now,
            )
            db_runs[r_unexpired.run_id] = r_unexpired.model_dump(mode="json")

        r_target_due = Run(
            run_id="run_e18_item_0500",
            space_id=space_id,
            created_by="u_e18",
            trace_id="e18_due_target_0000000000000001",
            telemetry_status=TelemetryStatus.DELAYED.value,
            telemetry_generation=1,
            telemetry_next_check_at=now - timedelta(seconds=10),
            telemetry_last_checked_at=now - timedelta(seconds=60),
        )
        db_runs[r_target_due.run_id] = r_target_due.model_dump(mode="json")

        for idx in range(501, 505):
            r_trail = Run(
                run_id=f"run_e18_item_{idx:04d}",
                space_id=space_id,
                created_by="u_e18",
                trace_id=f"e18_trail_{idx:032x}"[-32:],
                telemetry_status=TelemetryStatus.DELAYED.value,
                telemetry_generation=1,
                telemetry_next_check_at=now + timedelta(seconds=120),
                telemetry_last_checked_at=now,
            )
            db_runs[r_trail.run_id] = r_trail.model_dump(mode="json")

        assert len(db_runs) == 505

        # 2. Batch 1: Query with limit=1 on Store Instance 1.
        # Scan budget is max(1 * 10, 500) = 500 items.
        # All first 500 items are unexpired. Batch 1 must return 0 items,
        # but MUST persist the continuation cursor to Firestore!
        fs_store_1 = FirestoreStore(project_id="test-pdx", client=fake_client)
        batch_1 = fs_store_1.list_pending_telemetry_runs(space_id=space_id, limit=1, now=now)
        assert len(batch_1) == 0
        assert fs_store_1.get_telemetry_scan_cursor(space_id) == "run_e18_item_0499"

        # Verify cursor was persisted in remote telemetry_scan_cursors collection
        assert "scope_" + space_id in db_storage["telemetry_scan_cursors"]
        assert db_storage["telemetry_scan_cursors"]["scope_" + space_id]["cursor_run_id"] == "run_e18_item_0499"

        # 3. Batch 2: Instantiate BRAND NEW FirestoreStore instance (cold start / container restart).
        # It must load the persisted cursor from telemetry_scan_cursors,
        # continue scanning from item 501 onward, and find the due target item!
        fs_store_2 = FirestoreStore(project_id="test-pdx", client=fake_client)
        batch_2 = fs_store_2.list_pending_telemetry_runs(space_id=space_id, limit=1, now=now)
        assert len(batch_2) == 1
        assert batch_2[0].run_id == "run_e18_item_0500"
        assert fs_store_2.get_telemetry_scan_cursor(space_id) == "run_e18_item_0500"

        # 4. Batch 3: Scan remaining items to stream end.
        # Stream is exhausted, so continuation cursor must wrap around and reset to None.
        batch_3 = fs_store_2.list_pending_telemetry_runs(space_id=space_id, limit=1, now=now)
        assert len(batch_3) == 0
        assert fs_store_2.get_telemetry_scan_cursor(space_id) is None

        # 5. Scope Isolation: Interleave Global, Space A, and Space B scans.
        # Ensure progress records are stored independently and never overwrite each other.
        space_a = "sp_e18_alpha"
        space_b = "sp_e18_beta"
        for i in range(5):
            ra = Run(run_id=f"run_alpha_{i:02d}", space_id=space_a, created_by="u_e18", telemetry_status="delayed", telemetry_next_check_at=now + timedelta(seconds=120))
            rb = Run(run_id=f"run_beta_{i:02d}", space_id=space_b, created_by="u_e18", telemetry_status="delayed", telemetry_next_check_at=now + timedelta(seconds=120))
            db_runs[ra.run_id] = ra.model_dump(mode="json")
            db_runs[rb.run_id] = rb.model_dump(mode="json")

        fs_store_iso = FirestoreStore(project_id="test-pdx", client=fake_client)

        # Scan Space A with max_scan=2
        fs_store_iso.list_pending_telemetry_runs(space_id=space_a, limit=1, now=now, max_scan=2)
        cursor_a = fs_store_iso.get_telemetry_scan_cursor(space_a)
        assert cursor_a == "run_alpha_01"

        # Interleave with Global scan (space_id=None) max_scan=3
        fs_store_iso.list_pending_telemetry_runs(space_id=None, limit=1, now=now, max_scan=3)
        cursor_global = fs_store_iso.get_telemetry_scan_cursor(None)
        assert cursor_global == "run_alpha_02"

        # Interleave with Space B scan max_scan=2
        fs_store_iso.list_pending_telemetry_runs(space_id=space_b, limit=1, now=now, max_scan=2)
        cursor_b = fs_store_iso.get_telemetry_scan_cursor(space_b)
        assert cursor_b == "run_beta_01"

        # Assert Space A cursor remained completely untouched by Global and Space B scans
        assert fs_store_iso.get_telemetry_scan_cursor(space_a) == "run_alpha_01"
        assert fs_store_iso.get_telemetry_scan_cursor(space_b) == "run_beta_01"
        assert cursor_a != cursor_b
        assert cursor_a != cursor_global

        # 6. Deleted Cursor Recovery:
        # If the run referenced by cursor is deleted/purged, scanner gracefully
        # recovers from the beginning without raising errors, strictly bound by scan_budget.
        sp_del = "sp_e18_del_recovery"
        r_del_target = Run(run_id="run_del_target_00", space_id=sp_del, created_by="u_e18", telemetry_status="delayed", telemetry_next_check_at=now - timedelta(seconds=10))
        db_runs[r_del_target.run_id] = r_del_target.model_dump(mode="json")

        # Seed cursor document pointing to a deleted / tombstoned run
        db_storage["telemetry_scan_cursors"]["scope_" + sp_del] = {
            "scope_key": "scope_" + sp_del,
            "cursor_run_id": "run_tombstoned_nonexistent_9999",
            "version": 1,
            "lease_token": None,
            "lease_until": None,
        }

        recov_runs = fs_store_iso.list_pending_telemetry_runs(space_id=sp_del, limit=1, now=now)
        assert len(recov_runs) == 1
        assert recov_runs[0].run_id == "run_del_target_00"

        from app.services.storage import StorageUnavailableError

        # 7. Claim Transaction Failure:
        # If client.transaction() or _claim_cursor_tx fails, it must raise StorageUnavailableError
        # and strictly avoid unauthenticated fallback writes.
        failing_claim_client = FakeFirestoreClient(db_storage)
        failing_claim_client.transaction = MagicMock(side_effect=RuntimeError("Simulated claim transaction network abort"))
        fs_store_claim_fail = FirestoreStore(project_id="test-pdx", client=failing_claim_client)

        sp_claim_fail = "sp_e18_claim_fail"
        with pytest.raises(StorageUnavailableError) as exc_claim:
            fs_store_claim_fail.list_pending_telemetry_runs(space_id=sp_claim_fail, limit=1, now=now)
        assert "Failed to claim telemetry scan cursor" in str(exc_claim.value)
        assert f"scope_{sp_claim_fail}" not in db_storage.get("telemetry_scan_cursors", {})

        # 8. Worker Preemption:
        # Worker 1 starts scan, but before commit, Worker 2 takes over and commits version 2.
        # Worker 1's commit must be rejected without altering Worker 2's cursor position or version.
        sp_preempt = "sp_e18_preempt"
        r_preempt_1 = Run(run_id="run_preempt_01", space_id=sp_preempt, created_by="u_e18", telemetry_status="delayed", telemetry_next_check_at=now - timedelta(seconds=10))
        r_preempt_2 = Run(run_id="run_preempt_02", space_id=sp_preempt, created_by="u_e18", telemetry_status="delayed", telemetry_next_check_at=now - timedelta(seconds=10))
        db_runs[r_preempt_1.run_id] = r_preempt_1.model_dump(mode="json")
        db_runs[r_preempt_2.run_id] = r_preempt_2.model_dump(mode="json")

        preempt_client = FakeFirestoreClient(db_storage)
        orig_stream = FakeQuery.stream

        def preempting_stream(self_q):
            cur_doc = db_storage["telemetry_scan_cursors"].get(f"scope_{sp_preempt}")
            if cur_doc and cur_doc.get("lease_token"):
                # Worker 2 took over and advanced the cursor to version 2
                db_storage["telemetry_scan_cursors"][f"scope_{sp_preempt}"] = {
                    "scope_key": f"scope_{sp_preempt}",
                    "cursor_run_id": "run_worker2_advanced_pos",
                    "lease_token": None,
                    "lease_until": None,
                    "version": cur_doc.get("version", 1) + 1,
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            return orig_stream(self_q)

        with patch.object(FakeQuery, "stream", preempting_stream):
            fs_store_preempt = FirestoreStore(project_id="test-pdx", client=preempt_client)
            w1_runs = fs_store_preempt.list_pending_telemetry_runs(space_id=sp_preempt, limit=1, now=now, max_scan=1)
            assert len(w1_runs) == 1
            assert w1_runs[0].run_id == "run_preempt_01"
            # Database cursor was NOT overwritten by Worker 1! It remains at Worker 2's position!
            assert db_storage["telemetry_scan_cursors"][f"scope_{sp_preempt}"]["cursor_run_id"] == "run_worker2_advanced_pos"
            assert db_storage["telemetry_scan_cursors"][f"scope_{sp_preempt}"]["version"] == 2

        # 9. Commit Rejection on Lease Expiration:
        # If the lease expires before commit, commit is rejected and does not advance cursor.
        sp_expired = "sp_e18_expired"
        r_exp = Run(run_id="run_exp_01", space_id=sp_expired, created_by="u_e18", telemetry_status="delayed", telemetry_next_check_at=now - timedelta(seconds=10))
        db_runs[r_exp.run_id] = r_exp.model_dump(mode="json")

        def expiring_stream(self_q):
            cur_doc = db_storage["telemetry_scan_cursors"].get(f"scope_{sp_expired}")
            if cur_doc and cur_doc.get("lease_token"):
                past_time = datetime.now(UTC) - timedelta(seconds=5)
                cur_doc["lease_until"] = past_time.isoformat()
            return orig_stream(self_q)

        with patch.object(FakeQuery, "stream", expiring_stream):
            fs_store_exp = FirestoreStore(project_id="test-pdx", client=preempt_client)
            exp_runs = fs_store_exp.list_pending_telemetry_runs(space_id=sp_expired, limit=1, now=now, max_scan=1)
            assert len(exp_runs) == 1
            assert db_storage["telemetry_scan_cursors"][f"scope_{sp_expired}"]["version"] == 1
            assert db_storage["telemetry_scan_cursors"][f"scope_{sp_expired}"]["cursor_run_id"] is None

        # 10. Commit Transaction Failure:
        # If commit transaction raises an exception, it must raise StorageUnavailableError
        # and strictly avoid falling back to unconditional .set() calls.
        sp_commit_fail = "sp_e18_commit_fail"
        r_cf = Run(run_id="run_cf_01", space_id=sp_commit_fail, created_by="u_e18", telemetry_status="delayed", telemetry_next_check_at=now - timedelta(seconds=10))
        db_runs[r_cf.run_id] = r_cf.model_dump(mode="json")

        commit_fail_client = FakeFirestoreClient(db_storage)
        orig_transaction = commit_fail_client.transaction
        tx_call_count = 0

        def failing_commit_tx():
            nonlocal tx_call_count
            tx_call_count += 1
            if tx_call_count >= 2:  # First tx is claim; second tx is commit
                raise RuntimeError("Simulated network outage during commit transaction")
            return orig_transaction()

        commit_fail_client.transaction = failing_commit_tx
        fs_store_cf = FirestoreStore(project_id="test-pdx", client=commit_fail_client)

        with pytest.raises(StorageUnavailableError) as exc_commit:
            fs_store_cf.list_pending_telemetry_runs(space_id=sp_commit_fail, limit=1, now=now, max_scan=1)
        assert "Failed to commit telemetry scan cursor" in str(exc_commit.value)
        # Ensure no fallback .set() updated cursor_run_id
        assert db_storage["telemetry_scan_cursors"][f"scope_{sp_commit_fail}"]["cursor_run_id"] is None
        assert db_storage["telemetry_scan_cursors"][f"scope_{sp_commit_fail}"]["version"] == 1

        # 11. Explicit Cursor Read-Only Guarantee:
        # Passing cursor="run_xxx" must NOT acquire a lease or mutate telemetry_scan_cursors
        sp_ro = "sp_e18_readonly"
        r_ro_1 = Run(run_id="run_ro_01", space_id=sp_ro, created_by="u_e18", telemetry_status="delayed", telemetry_next_check_at=now - timedelta(seconds=10))
        r_ro_2 = Run(run_id="run_ro_02", space_id=sp_ro, created_by="u_e18", telemetry_status="delayed", telemetry_next_check_at=now - timedelta(seconds=10))
        db_runs[r_ro_1.run_id] = r_ro_1.model_dump(mode="json")
        db_runs[r_ro_2.run_id] = r_ro_2.model_dump(mode="json")

        fs_store_ro = FirestoreStore(project_id="test-pdx", client=fake_client)
        ro_runs = fs_store_ro.list_pending_telemetry_runs(space_id=sp_ro, limit=1, now=now, cursor="run_ro_01")
        assert len(ro_runs) == 1
        assert ro_runs[0].run_id == "run_ro_02"
        assert f"scope_{sp_ro}" not in db_storage.get("telemetry_scan_cursors", {})


# =============================================================================
# Gate E19: Multi-Tenant Trace Query Proxy & Span Waterfall Sanitization
# =============================================================================


def test_gate_e19_multi_tenant_trace_query_proxy_and_waterfall_sanitization(monkeypatch):
    """
    Validates that GET /v1/spaces/{space_id}/runs/{run_id}/trace:
    1. Authenticated space member receives valid, sanitized SpanWaterfallNode list.
    2. Relative offset timing and parent-child hierarchy are properly constructed.
    3. Sensitive and unallowlisted attributes (raw prompts, tokens, emails) are scrubbed.
    4. Non-member receives HTTP 403 Forbidden.
    5. Trace with foreign tenant hash or mismatched Run ID returns HTTP 404 (fails closed).
    6. Non-existent trace returns HTTP 404.
    """
    from fastapi.testclient import TestClient
    from app.main import app
    from app.core.telemetry_tenant import compute_tenant_hash
    from app.services.telemetry_service import MockTracingClient

    client = TestClient(app)

    user_a = User(uid="u_e19_member", email="member@e19.com")
    user_b = User(uid="u_e19_outsider", email="outsider@e19.com")
    store.save_user(user_a)
    store.save_user(user_b)

    space_a = Space(space_id="sp_e19_alpha", name="Alpha Space", created_by=user_a.uid)
    space_b = Space(space_id="sp_e19_beta", name="Beta Space", created_by=user_b.uid)
    store.create_space(space_a, creator_uid=user_a.uid)
    store.create_space(space_b, creator_uid=user_b.uid)

    hash_a, _ = compute_tenant_hash(space_a.space_id)
    hash_b, _ = compute_tenant_hash(space_b.space_id)

    # 1. Authentic run in Space A
    run_valid = Run(
        run_id="run_e19_valid",
        space_id=space_a.space_id,
        created_by=user_a.uid,
        trace_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        telemetry_status=TelemetryStatus.AVAILABLE.value,
    )
    store.save_run(run_valid)

    now = datetime.now(UTC)
    t0 = now.isoformat()
    t1 = (now + timedelta(milliseconds=120)).isoformat()
    t2 = (now + timedelta(milliseconds=350)).isoformat()

    valid_trace_data = {
        "trace_id": run_valid.trace_id,
        "spans": [
            {
                "name": "action_execution",
                "span_id": "root_span_01",
                "parent_span_id": None,
                "start_time_iso": t0,
                "end_time_iso": t2,
                "attributes": {
                    "space_id_hash": hash_a,
                    "run_id": run_valid.run_id,
                    "action_type": "generate_deliverable",
                    "secret_api_key": "sk-leak-test-12345",  # Must be stripped!
                    "raw_prompt": "Confidential prompt content",  # Must be stripped!
                },
            },
            {
                "name": "action_execute_generation",
                "span_id": "child_span_02",
                "parent_span_id": "root_span_01",
                "start_time_iso": t1,
                "end_time_iso": t2,
                "attributes": {
                    "space_id_hash": hash_a,
                    "run_id": run_valid.run_id,
                    "stage": "generation",
                    "model_id": "gemini-2.0-flash",
                },
            },
        ],
    }

    # 2. Run in Space A pointing to a foreign tenant's trace (Space B's HMAC hash)
    run_foreign = Run(
        run_id="run_e19_foreign",
        space_id=space_a.space_id,
        created_by=user_a.uid,
        trace_id="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        telemetry_status=TelemetryStatus.EXPORTING.value,
    )
    store.save_run(run_foreign)

    foreign_trace_data = {
        "trace_id": run_foreign.trace_id,
        "spans": [
            {
                "name": "action_execution",
                "span_id": "foreign_span_01",
                "start_time_iso": t0,
                "end_time_iso": t1,
                "attributes": {
                    "space_id_hash": hash_b,  # Foreign tenant!
                    "run_id": "different_run",
                    "sensitive_foreign_payload": "TOP_SECRET",
                },
            }
        ],
    }

    mock_client = MockTracingClient(traces={
        run_valid.trace_id: valid_trace_data,
        run_foreign.trace_id: foreign_trace_data,
    })

    monkeypatch.setattr("app.services.telemetry_service.get_default_tracing_client", lambda: mock_client)

    auth_member = {"Authorization": f"Bearer dev:{user_a.uid}:{user_a.email}"}
    auth_outsider = {"Authorization": f"Bearer dev:{user_b.uid}:{user_b.email}"}

    # Test 1: Space member queries valid run -> 200 OK with sanitized waterfall
    res_valid = client.get(
        f"/v1/spaces/{space_a.space_id}/runs/{run_valid.run_id}/trace",
        headers=auth_member,
    )
    assert res_valid.status_code == 200
    bundle = res_valid.json()
    assert bundle["trace_id"] == run_valid.trace_id
    assert bundle["has_real_telemetry"] is True
    nodes = bundle["spans"]
    assert len(nodes) == 2
    root_node = nodes[0]
    child_node = nodes[1]
    assert root_node["name"] == "action_execution"
    assert root_node["offset_ms"] == 0
    assert root_node["duration_ms"] == 350
    assert child_node["name"] == "action_execute_generation"
    assert child_node["offset_ms"] == 120
    assert child_node["duration_ms"] == 230

    # Verify attribute allowlist scrubbing
    assert "space_id_hash" in root_node["attributes"]
    assert "run_id" in root_node["attributes"]
    assert "secret_api_key" not in root_node["attributes"]
    assert "raw_prompt" not in root_node["attributes"]

    # Test 2: Outsider queries Space A run -> 403 Forbidden
    res_forbidden = client.get(
        f"/v1/spaces/{space_a.space_id}/runs/{run_valid.run_id}/trace",
        headers=auth_outsider,
    )
    assert res_forbidden.status_code == 403

    # Test 3: Foreign tenant trace -> 404 (zero leakage of foreign_trace_data)
    res_foreign = client.get(
        f"/v1/spaces/{space_a.space_id}/runs/{run_foreign.run_id}/trace",
        headers=auth_member,
    )
    assert res_foreign.status_code == 404
    assert "TOP_SECRET" not in res_foreign.text

    # Test 4: Run with non-existent trace in backend -> 404
    run_missing = Run(
        run_id="run_e19_missing",
        space_id=space_a.space_id,
        created_by=user_a.uid,
        trace_id="cccccccccccccccccccccccccccccccc",
    )
    store.save_run(run_missing)
    res_missing = client.get(
        f"/v1/spaces/{space_a.space_id}/runs/{run_missing.run_id}/trace",
        headers=auth_member,
    )
    assert res_missing.status_code == 404


# =============================================================================
# Gate E20: Low-Cardinality Pre-Aggregated Metrics Rollups & Bounded Query
# =============================================================================


def test_gate_e20_low_cardinality_pre_aggregated_metrics_rollups_and_anti_abuse():
    """
    Validates that GET /v1/spaces/{space_id}/telemetry/metrics:
    1. Rejects out-of-bounds time windows (< 1h or > 168h) with HTTP 422.
    2. Calculates exact P50/P95 latencies, success rates, failure counts, and honest token costs.
    3. Reads pre-aggregated hourly buckets and avoids full collection unindexed scans.
    4. Automatically records hourly metric rollups on CAS status transition to COMPLETED/FAILED.
    5. Returns cached summary on rapid repeat requests within throttle interval.
    """
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)

    user = User(uid="u_e20_metrics", email="metrics@e20.com")
    store.save_user(user)
    space = Space(space_id="sp_e20_metrics", name="Metrics Space", created_by=user.uid)
    store.create_space(space, creator_uid=user.uid)

    auth_headers = {"Authorization": f"Bearer dev:{user.uid}:{user.email}"}

    # 1. Bounds Validation: time_window_hours < 1 or > 168 returns 422
    res_too_small = client.get(
        f"/v1/spaces/{space.space_id}/telemetry/metrics?time_window_hours=0",
        headers=auth_headers,
    )
    assert res_too_small.status_code == 422

    res_too_large = client.get(
        f"/v1/spaces/{space.space_id}/telemetry/metrics?time_window_hours=200",
        headers=auth_headers,
    )
    assert res_too_large.status_code == 422

    # 2. Populate pre-aggregated hourly rollups in storage
    now = datetime.now(UTC)
    # Seed 5 runs across two hours:
    # Hour -2: 2 completed runs (durations: 100ms, 200ms, tokens: 50, cost: 0.0001)
    store.record_metric_rollup_event(
        space_id=space.space_id,
        project_tag="general",
        duration_ms=100,
        status="completed",
        tokens_used=20,
        estimated_cost_usd=0.00004,
        pricing_version=PRICING_CATALOG_VERSION,
        timestamp=now - timedelta(hours=2),
    )
    store.record_metric_rollup_event(
        space_id=space.space_id,
        project_tag="general",
        duration_ms=200,
        status="completed",
        tokens_used=30,
        estimated_cost_usd=0.00006,
        pricing_version=PRICING_CATALOG_VERSION,
        timestamp=now - timedelta(hours=2),
    )

    # Hour -1: 2 completed runs (300ms, 400ms) + 1 failed run (500ms, PDX_EXECUTION_FAILURE)
    store.record_metric_rollup_event(
        space_id=space.space_id,
        project_tag="general",
        duration_ms=300,
        status="completed",
        tokens_used=50,
        estimated_cost_usd=0.00010,
        pricing_version=PRICING_CATALOG_VERSION,
        timestamp=now - timedelta(hours=1),
    )
    store.record_metric_rollup_event(
        space_id=space.space_id,
        project_tag="general",
        duration_ms=400,
        status="completed",
        tokens_used=50,
        estimated_cost_usd=0.00010,
        pricing_version=PRICING_CATALOG_VERSION,
        timestamp=now - timedelta(hours=1),
    )
    store.record_metric_rollup_event(
        space_id=space.space_id,
        project_tag="general",
        duration_ms=500,
        status="failed",
        failure_code=TelemetryErrorCode.PDX_EXECUTION_FAILURE.value,
        tokens_used=10,
        estimated_cost_usd=0.00002,
        pricing_version=PRICING_CATALOG_VERSION,
        timestamp=now - timedelta(hours=1),
    )

    # 3. Query metrics endpoint: 24 hour window
    res_metrics = client.get(
        f"/v1/spaces/{space.space_id}/telemetry/metrics?time_window_hours=24",
        headers=auth_headers,
    )
    assert res_metrics.status_code == 200
    metrics = res_metrics.json()
    assert metrics["total_runs"] == 5
    assert metrics["completed_runs"] == 4
    assert metrics["failed_runs"] == 1
    assert metrics["success_rate"] == 0.8
    assert metrics["rollup_schema_version"] == 2
    assert metrics["data_status"] == "available"
    assert metrics["sample_count"] == 5
    assert metrics["latency_percentile_method"] == "histogram"
    # Approximate P50/P95 from fixed histogram linear interpolation
    assert 200.0 <= metrics["latency_p50_ms"] <= 300.0
    assert 400.0 <= metrics["latency_p95_ms"] <= 500.0
    assert metrics["latency_percentile_capped"] is False
    assert metrics["total_tokens_used"] == 160
    assert abs(metrics["estimated_cost_usd"] - 0.00032) < 1e-6
    assert metrics["cost_data_status"] == "available"
    assert metrics["pricing_version"] == PRICING_CATALOG_VERSION
    assert metrics["pricing_versions_truncated"] is False
    assert metrics["currency"] == "USD"
    assert metrics["failures_by_code"] == {TelemetryErrorCode.PDX_EXECUTION_FAILURE.value: 1}

    # 4. CAS automatic hook verification: Transitioning run via compare_and_swap_run_status
    # automatically writes to space_metric_rollups
    r_cas = Run(
        run_id="run_e20_cas_test",
        space_id=space.space_id,
        created_by=user.uid,
        status=RunStatus.RUNNING,
        telemetry=RunTelemetry(duration_ms=250, tokens_used=40, estimated_cost_usd=0.00008, pricing_version=PRICING_CATALOG_VERSION),
    )
    store.save_run(r_cas)

    # CAS transition to COMPLETED
    cas_res = store.compare_and_swap_run_status(
        run_id=r_cas.run_id,
        expected_status=RunStatus.RUNNING,
        new_status=RunStatus.COMPLETED,
    )
    assert cas_res is not None
    assert cas_res.status == RunStatus.COMPLETED

    # Check that a rollup was recorded for current hour
    cur_hour = datetime.now(UTC).strftime("%Y-%m-%dT%H")
    rollups = store.get_metric_rollups(
        space_id=space.space_id,
        start_time=datetime.now(UTC) - timedelta(minutes=5),
        end_time=datetime.now(UTC) + timedelta(minutes=5),
    )
    assert any(r.hour_str == cur_hour and r.completed_runs >= 1 for r in rollups)

    # 5. Anti-Abuse Cache: Rapid second request returns cached summary
    res_cached = client.get(
        f"/v1/spaces/{space.space_id}/telemetry/metrics?time_window_hours=24",
        headers=auth_headers,
    )
    assert res_cached.status_code == 200
    assert res_cached.json()["generated_at"] == metrics["generated_at"]


# =============================================================================
# Gate E21: Strict Firestore Fake Enforcing Read-Before-Write
# =============================================================================
def test_gate_e21_strict_firestore_read_before_write_enforcement():
    """
    Validates that FirestoreStore.compare_and_swap_run_status strictly executes
    all reads before any writes. If a transaction attempts to execute tx.get()
    after tx.set(), the StrictFakeTransaction raises a ReadAfterWriteError (google.cloud.exceptions.Conflict).
    """
    from google.cloud.exceptions import Conflict
    from app.services.storage import FirestoreStore

    class ReadAfterWriteError(Conflict):
        pass

    class StrictFakeDocumentSnapshot:
        def __init__(self, exists: bool, data: Optional[Dict[str, Any]] = None):
            self.exists = exists
            self._data = data or {}

        def to_dict(self):
            return dict(self._data)

    class StrictFakeDocumentReference:
        def __init__(self, collection_name: str, doc_id: str, store_dict: Dict[str, Dict[str, Any]]):
            self.collection_name = collection_name
            self.doc_id = doc_id
            self.path = f"{collection_name}/{doc_id}"
            self._store_dict = store_dict

        def get(self, transaction=None):
            if transaction:
                return transaction.get(self)
            data = self._store_dict.get(self.path)
            return StrictFakeDocumentSnapshot(data is not None, data)

    class StrictFakeCollectionReference:
        def __init__(self, name: str, store_dict: Dict[str, Dict[str, Any]]):
            self.name = name
            self._store_dict = store_dict

        def document(self, doc_id: str):
            return StrictFakeDocumentReference(self.name, doc_id, self._store_dict)

        def where(self, field: str, op: str, value: Any):
            return self

        def stream(self):
            prefix = f"{self.name}/"
            res = []
            for k, v in self._store_dict.items():
                if k.startswith(prefix):
                    res.append(StrictFakeDocumentSnapshot(True, v))
            return res

    import unittest.mock

    class StrictFakeTransaction:
        _read_only = False
        _id = b"fake_tx"
        _max_attempts = 5

        def __init__(self, store_dict: Dict[str, Dict[str, Any]]):
            self._store_dict = store_dict
            self._writes_performed: List[str] = []

        def get(self, doc_ref: StrictFakeDocumentReference) -> StrictFakeDocumentSnapshot:
            # STRICT FIRESTORE CONSTRAINT: No reads after any write!
            if len(self._writes_performed) > 0:
                raise ReadAfterWriteError(
                    f"Firestore transaction violation: Read-after-write detected! "
                    f"Attempted to read '{doc_ref.path}' after writing '{self._writes_performed}'"
                )
            data = self._store_dict.get(doc_ref.path)
            return StrictFakeDocumentSnapshot(data is not None, data)

        def set(self, doc_ref: StrictFakeDocumentReference, data: Dict[str, Any]):
            self._writes_performed.append(doc_ref.path)
            self._store_dict[doc_ref.path] = data

    class StrictFakeFirestoreClient:
        def __init__(self):
            self._store_dict: Dict[str, Dict[str, Any]] = {}

        def collection(self, name: str):
            return StrictFakeCollectionReference(name, self._store_dict)

        def transaction(self):
            return StrictFakeTransaction(self._store_dict)

    client = StrictFakeFirestoreClient()
    fs_store = FirestoreStore(client=client)

    # Seed an initial run in RUNNING status
    test_run = Run(
        run_id="run_e21_strict_cas",
        space_id="sp_e21",
        status=RunStatus.RUNNING,
        created_by="user_e21",
        telemetry=RunTelemetry(duration_ms=180, tokens_used=50, estimated_cost_usd=0.0001),
    )
    client._store_dict[f"runs/{test_run.run_id}"] = test_run.model_dump(mode="json")

    # Execute CAS transition to COMPLETED under strict read-before-write checking
    with unittest.mock.patch("google.cloud.firestore.transactional", lambda fn: (lambda tx, *args, **kwargs: fn(tx, *args, **kwargs))):
        updated_run = fs_store.compare_and_swap_run_status(
            run_id=test_run.run_id,
            expected_status=RunStatus.RUNNING,
            new_status=RunStatus.COMPLETED,
        )

    assert updated_run is not None
    assert updated_run.status == RunStatus.COMPLETED
    assert f"runs/{test_run.run_id}" in client._store_dict
    assert client._store_dict[f"runs/{test_run.run_id}"]["status"] == RunStatus.COMPLETED.value

    # Verify that space_metric_rollups was also written with schema version 2
    cur_hour = datetime.now(UTC).strftime("%Y-%m-%dT%H")
    rollup_key = f"space_metric_rollups/sp_e21:general:{cur_hour}"
    assert rollup_key in client._store_dict
    rollup_data = client._store_dict[rollup_key]
    assert rollup_data["rollup_schema_version"] == 2
    assert rollup_data["completed_runs"] == 1
    assert rollup_data["total_runs"] == 1
    assert "le_250" in rollup_data["latency_histogram"]


# =============================================================================
# Gate E22: Trace Endpoint Fails Closed When Only Simulated Spans Exist
# =============================================================================
def test_gate_e22_trace_endpoint_fails_closed_on_simulated_traces():
    """
    Validates that GET /v1/spaces/{space_id}/runs/{run_id}/trace fails closed (HTTP 404):
    1. If spans only exist in grafana_mcp._traces (mock/simulated).
    2. The production trace endpoint NEVER promotes simulated data to real telemetry.
    3. has_real_telemetry is NEVER set to True for simulated data.
    4. Tests against the REAL default tracing client (InMemoryTracingClient) WITHOUT monkeypatching.
    """
    from fastapi.testclient import TestClient
    from app.main import app
    from app.integrations.grafana_mcp import grafana_mcp

    client = TestClient(app)

    user = User(uid="u_e22", email="u22@test.com")
    store.save_user(user)
    space = Space(space_id="sp_e22", name="E22 Space", created_by=user.uid)
    store.create_space(space, creator_uid=user.uid)

    run = Run(
        run_id="run_e22_simulated",
        space_id=space.space_id,
        created_by=user.uid,
        trace_id="simulated_trace_id_222222222222",
        status=RunStatus.FAILED,
    )
    store.save_run(run)

    # 1. Record simulated failure ONLY in grafana_mcp._traces
    grafana_mcp.record_simulated_failure_trace(run.trace_id, run.run_id, space.space_id)
    assert run.trace_id in grafana_mcp._traces

    # 2. DO NOT monkeypatch get_default_tracing_client.
    # The real default InMemoryTracingClient must NOT have accepted or stored this simulated trace.

    auth = {"Authorization": f"Bearer dev:{user.uid}:{user.email}"}

    # 3. Query trace endpoint -> MUST return 404 (Not Found / Unavailable)
    res = client.get(
        f"/v1/spaces/{space.space_id}/runs/{run.run_id}/trace",
        headers=auth,
    )
    assert res.status_code == 404
    # Zero simulated span leakage into production trace response
    assert "Sacrificial Test Aircraft" not in res.text


# =============================================================================
# Gate E23: Safe Error Code Only - Secret, Path, Email Suppression
# =============================================================================
def test_gate_e23_error_message_sanitization_and_secret_suppression(monkeypatch):
    """
    Validates that trace spans with sensitive error messages (API keys, file paths, emails):
    1. Strictly drop raw error messages.
    2. Map to safe human descriptions via error_code -> ERROR_CODE_SAFE_SUMMARIES.
    3. Suppress all secrets, paths, and email addresses.
    4. Enforce max length on name (<=128) and service_name (<=64).
    """
    from fastapi.testclient import TestClient
    from app.main import app
    from app.core.telemetry_tenant import compute_tenant_hash
    from app.services.telemetry_service import MockTracingClient

    client = TestClient(app)

    user = User(uid="u_e23", email="u23@test.com")
    store.save_user(user)
    space = Space(space_id="sp_e23", name="E23 Space", created_by=user.uid)
    store.create_space(space, creator_uid=user.uid)
    space_hash, _ = compute_tenant_hash(space.space_id)

    run = Run(
        run_id="run_e23_leak_test",
        space_id=space.space_id,
        created_by=user.uid,
        trace_id="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        status=RunStatus.FAILED,
        telemetry_status=TelemetryStatus.AVAILABLE.value,
    )
    store.save_run(run)

    now = datetime.now(UTC)
    t0 = now.isoformat()
    t1 = (now + timedelta(milliseconds=150)).isoformat()

    # Raw span from backend containing sensitive data and long strings
    trace_data = {
        "trace_id": run.trace_id,
        "spans": [
            {
                "name": "action_execution",
                "span_id": "span_root_23",
                "parent_span_id": None,
                "start_time_iso": t0,
                "end_time_iso": t1,
                "status": "error",
                "attributes": {
                    "space_id_hash": space_hash,
                    "run_id": run.run_id,
                    "error_code": TelemetryErrorCode.STORAGE_COMMIT_TIMEOUT.value,
                    "service_name": "studiotower-api-service-name-that-is-way-longer-than-sixty-four-characters-overflow",
                    "raw_leak_key": "sk-leak-production-key-9999",
                    "raw_leak_path": "/var/secrets/vault/app_creds.json",
                    "raw_leak_email": "admin_leaked@tenant.org",
                    "error_message": "Critical crash at /var/secrets/vault/app_creds.json with key sk-leak-production-key-9999 for admin_leaked@tenant.org",
                },
            },
            {
                "name": "pipeline." + ("substep_bounds_check_" * 15),
                "span_id": "span_child_23",
                "parent_span_id": "span_root_23",
                "start_time_iso": t0,
                "end_time_iso": t1,
                "status": "ok",
                "attributes": {
                    "space_id_hash": space_hash,
                    "run_id": run.run_id,
                },
            },
        ],
    }

    mock_client = MockTracingClient(traces={run.trace_id: trace_data})
    monkeypatch.setattr("app.services.telemetry_service.get_default_tracing_client", lambda: mock_client)

    auth = {"Authorization": f"Bearer dev:{user.uid}:{user.email}"}
    res = client.get(
        f"/v1/spaces/{space.space_id}/runs/{run.run_id}/trace",
        headers=auth,
    )
    assert res.status_code == 200
    bundle = res.json()
    assert bundle["has_real_telemetry"] is True
    spans = bundle["spans"]
    assert len(spans) == 2
    root_s = next(s for s in spans if s["span_id"] == "span_root_23")
    child_s = next(s for s in spans if s["span_id"] == "span_child_23")

    # Verify normalized error code and controlled safe summary
    assert root_s["error_code"] == TelemetryErrorCode.STORAGE_COMMIT_TIMEOUT.value
    assert root_s["error_message"] == "Storage transaction or commit operation exceeded allocated timeout budget."

    # Verify complete absence of raw sensitive leaked strings
    res_text = res.text
    assert "sk-leak" not in res_text
    assert "/var/secrets" not in res_text
    assert "admin_leaked@tenant.org" not in res_text
    assert "raw_leak_key" not in res_text

    # Bounds check
    assert len(root_s["name"]) <= 128
    assert len(root_s["service_name"]) <= 64
    assert len(child_s["name"]) <= 128


# =============================================================================
# Gate E24: Zero Runs Collection Scan on Rollup Cache Miss
# =============================================================================
def test_gate_e24_zero_runs_scan_on_rollup_miss(monkeypatch):
    """
    Validates that when rollups do not exist for the queried time window:
    1. Returns HTTP 200 with data_status="warming_up" and sample_count=0.
    2. store.list_runs_for_metrics is called exactly 0 times.
    3. ZERO unindexed full-collection scans in Firestore or storage.
    """
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)

    user = User(uid="u_e24", email="u24@test.com")
    store.save_user(user)
    space = Space(space_id="sp_e24_empty", name="E24 Empty Space", created_by=user.uid)
    store.create_space(space, creator_uid=user.uid)

    call_counts = {"list_runs_for_metrics": 0}

    def _spy_list_runs_for_metrics(*args, **kwargs):
        call_counts["list_runs_for_metrics"] += 1
        return []

    monkeypatch.setattr(store, "list_runs_for_metrics", _spy_list_runs_for_metrics)

    auth = {"Authorization": f"Bearer dev:{user.uid}:{user.email}"}
    res = client.get(
        f"/v1/spaces/{space.space_id}/telemetry/metrics?time_window_hours=24&bypass_cache=true",
        headers=auth,
    )
    assert res.status_code == 200
    metrics = res.json()

    assert metrics["data_status"] == "warming_up"
    assert metrics["sample_count"] == 0
    assert metrics["total_runs"] == 0
    assert metrics["rollup_schema_version"] == 2
    assert metrics["latency_percentile_method"] == "histogram"
    assert metrics["latency_percentile_capped"] is False
    assert metrics["estimated_cost_usd"] is None
    assert metrics["cost_data_status"] == "unavailable"
    assert metrics["pricing_version"] is None

    # Strictly verify ZERO calls to runs collection
    assert call_counts["list_runs_for_metrics"] == 0


# =============================================================================
# Gate E25: Strictly Bounded Rollup Size Under High-Volume Ingestion
# =============================================================================
def test_gate_e25_strictly_bounded_rollup_size_under_volume():
    """
    Validates that recording 10,000 run completions in a single hourly bucket:
    1. Produces a strictly O(1) constant document size (< 1 KB), immune to 1 MiB overflow.
    2. latency_histogram correctly accumulates counts across fixed buckets.
    3. failures_by_code strictly bounds dictionary keys to allowed TelemetryErrorCode values.
    4. Approximate P50 and P95 are calculated via histogram linear interpolation.
    """
    import json
    space_id = "sp_e25_volume"
    tag = "general"
    now = datetime.now(UTC)

    # Ingest 10,000 events with diverse latencies and failure codes
    for i in range(10000):
        # Latency distribution: 50% short (<100ms), 40% medium (100-500ms), 10% long (1000-8000ms)
        if i % 10 < 5:
            dur = 40 + (i % 50)
        elif i % 10 < 9:
            dur = 150 + (i % 300)
        else:
            dur = 1200 + (i % 6000)

        status_val = "completed" if (i % 5 != 0) else "failed"
        # Test unknown/arbitrary failure code normalization
        fc = TelemetryErrorCode.STORAGE_CONFLICT.value if (i % 10 == 0) else "arbitrary_custom_error_string_xyz"

        store.record_metric_rollup_event(
            space_id=space_id,
            project_tag=tag,
            duration_ms=dur,
            status=status_val,
            failure_code=fc if status_val == "failed" else None,
            tokens_used=10,
            estimated_cost_usd=0.00002,
            pricing_version=PRICING_CATALOG_VERSION,
            timestamp=now,
        )

    # Fetch rollup
    rollups = store.get_metric_rollups(
        space_id=space_id,
        start_time=now - timedelta(minutes=5),
        end_time=now + timedelta(minutes=5),
    )
    assert len(rollups) == 1
    r = rollups[0]

    # 1. Verify O(1) bounded document size (< 1000 bytes)
    serialized = json.dumps(r.model_dump(mode="json"))
    doc_bytes = len(serialized.encode("utf-8"))
    assert doc_bytes < 1000, f"Rollup document size was {doc_bytes} bytes; expected < 1000 bytes!"

    # 2. Total runs & histogram integrity
    assert r.total_runs == 10000
    assert sum(r.latency_histogram.values()) == 10000
    assert r.completed_runs == 8000
    assert r.failed_runs == 2000

    # 3. Failures dictionary bounded: arbitrary error must have converged to UNKNOWN_INTERNAL_ERROR
    assert set(r.failures_by_code.keys()).issubset(ALLOWED_TELEMETRY_ERROR_CODES)
    assert TelemetryErrorCode.UNKNOWN_INTERNAL_ERROR.value in r.failures_by_code
    assert "arbitrary_custom_error_string_xyz" not in r.failures_by_code

    # 4. Approximate P50 and P95 via histogram linear interpolation
    summary = TelemetryService.get_space_metrics_summary(
        space_id=space_id,
        time_window_hours=1,
        bypass_cache=True,
    )
    assert summary.total_runs == 10000
    assert summary.sample_count == 10000
    assert summary.data_status == "available"
    assert summary.cost_data_status == "available"
    assert summary.pricing_version == PRICING_CATALOG_VERSION
    assert summary.pricing_versions_truncated is False
    assert summary.latency_percentile_method == "histogram"
    assert summary.latency_percentile_capped is False
    assert 50.0 <= summary.latency_p50_ms <= 250.0
    assert 1000.0 <= summary.latency_p95_ms <= 8000.0

    # 5. Verify capped percentile behavior when samples fall into +Inf (> 120000ms)
    space_inf = "sp_e25_inf"
    for _ in range(100):
        store.record_metric_rollup_event(
            space_id=space_inf,
            project_tag="general",
            duration_ms=150000,  # exceeds highest finite bucket (120,000ms)
            status="completed",
            tokens_used=10,
            estimated_cost_usd=0.00002,
            pricing_version=PRICING_CATALOG_VERSION,
            timestamp=now,
        )
    inf_summary = TelemetryService.get_space_metrics_summary(
        space_id=space_inf,
        time_window_hours=1,
        bypass_cache=True,
    )
    assert inf_summary.latency_percentile_capped is True
    # Highest finite bucket is 120000.0
    assert inf_summary.latency_p50_ms == 120000.0
    assert inf_summary.latency_p95_ms == 120000.0


# =============================================================================
# Gate E26: Honest Pricing Version - Unversioned Cost Fails to "Partial/Unavailable"
# =============================================================================
def test_gate_e26_honest_pricing_version_and_mixed_catalog_tracking():
    """
    Validates that:
    1. A run with estimated_cost_usd but NO pricing_version is NOT presumed to be PRICING_CATALOG_VERSION.
    2. It is counted in unpriced_run_count and does NOT pollute pricing_versions.
    3. Metrics summary reports cost_data_status="partial" (or "unavailable" if 0 priced runs) with pricing_version=None.
    4. When multiple valid pricing versions exist, summary reports pricing_version="mixed".
    5. Overflowing 5 distinct pricing versions flags pricing_versions_truncated=True and reports pricing_version="mixed".
    """
    space_id = "sp_e26_honest_pricing"
    now = datetime.now(UTC)

    # 1. Ingest a run with cost BUT NO pricing_version (e.g. legacy/external system)
    store.record_metric_rollup_event(
        space_id=space_id,
        project_tag="general",
        duration_ms=120,
        status="completed",
        tokens_used=50,
        estimated_cost_usd=0.00015,
        pricing_version=None,  # Missing version!
        timestamp=now,
    )

    rollups = store.get_metric_rollups(
        space_id=space_id,
        start_time=now - timedelta(minutes=5),
        end_time=now + timedelta(minutes=5),
    )
    assert len(rollups) == 1
    r = rollups[0]
    # Cost is tracked, but NOT counted as a priced run under any catalog
    assert r.priced_run_count == 0
    assert r.unpriced_run_count == 1
    assert r.pricing_versions == []
    assert r.estimated_cost_usd == 0.00015

    # Summary must report cost_data_status="unavailable" because 0 priced runs exist!
    summary_unpriced = TelemetryService.get_space_metrics_summary(
        space_id=space_id,
        time_window_hours=1,
        bypass_cache=True,
    )
    assert summary_unpriced.cost_data_status == "unavailable"
    assert summary_unpriced.pricing_version is None
    assert summary_unpriced.estimated_cost_usd is None

    # 2. Add a properly priced run with PRICING_CATALOG_VERSION
    store.record_metric_rollup_event(
        space_id=space_id,
        project_tag="general",
        duration_ms=130,
        status="completed",
        tokens_used=50,
        estimated_cost_usd=0.00010,
        pricing_version=PRICING_CATALOG_VERSION,
        timestamp=now,
    )

    summary_partial = TelemetryService.get_space_metrics_summary(
        space_id=space_id,
        time_window_hours=1,
        bypass_cache=True,
    )
    # Now we have 1 priced run and 1 unpriced run -> status MUST be "partial"
    assert summary_partial.cost_data_status == "partial"
    assert summary_partial.estimated_cost_usd == 0.00025
    assert summary_partial.pricing_version == PRICING_CATALOG_VERSION

    # 3. Add runs with different pricing versions -> summary must report "mixed"
    store.record_metric_rollup_event(
        space_id=space_id,
        project_tag="general",
        duration_ms=140,
        status="completed",
        tokens_used=50,
        estimated_cost_usd=0.00020,
        pricing_version="2025-Q4-legacy",
        timestamp=now,
    )

    summary_mixed = TelemetryService.get_space_metrics_summary(
        space_id=space_id,
        time_window_hours=1,
        bypass_cache=True,
    )
    assert summary_mixed.pricing_version == "mixed"
    assert summary_mixed.pricing_versions_truncated is False

    # 4. Truncation test: Ingest 5 more distinct pricing versions into a dedicated space
    space_trunc = "sp_e26_trunc"
    for i in range(7):
        store.record_metric_rollup_event(
            space_id=space_trunc,
            project_tag="general",
            duration_ms=100,
            status="completed",
            tokens_used=10,
            estimated_cost_usd=0.00001,
            pricing_version=f"v_catalog_{i}",
            timestamp=now,
        )

    r_trunc = store.get_metric_rollups(
        space_id=space_trunc,
        start_time=now - timedelta(minutes=5),
        end_time=now + timedelta(minutes=5),
    )[0]
    assert len(r_trunc.pricing_versions) == 5
    assert r_trunc.pricing_versions_truncated is True

    summary_trunc = TelemetryService.get_space_metrics_summary(
        space_id=space_trunc,
        time_window_hours=1,
        bypass_cache=True,
    )
    assert summary_trunc.pricing_version == "mixed"
    assert summary_trunc.pricing_versions_truncated is True


# =============================================================================
# Gate E27: Server-Templated Secure Grafana Deep Link Generation & Security
# =============================================================================
def test_gate_e27_secure_server_templated_grafana_deep_links(monkeypatch):
    """
    Validates Slice E Phase E4 Server-Templated Secure Grafana Deep Link Security Contracts:
    1. Zero default public domain trust: unconfigured GRAFANA_ALLOWED_HOSTS or unconfigured BASE_URL -> None.
    2. Strict urlsplit parsing:
       - parsed.hostname used directly (rejects netloc split hacks).
       - Insecure HTTP scheme -> None.
       - Userinfo / @ spoofing (credentials in URL) -> None.
       - Custom non-443 port -> None.
       - Base URL containing query parameters or fragments -> None.
       - Hostname trailing dot, control chars, or invalid hostname -> None.
       - Suffix / open redirect spoofing (grafana.example.com.attacker.org) -> None.
    3. Structured URL construction:
       - Validated regex identifiers: dashboard_id path traversal (../../admin) -> None.
       - Query parameter injection (trace_id="trc_123&admin=true#frag") -> safely URL-encoded into a single value.
    4. Authoritative server-generated time windows:
       - from and to are calculated from server timestamps with buffer padding.
       - from_ms < to_ms and (to_ms - from_ms) <= 24h.
       - Missing timestamps fail-close to None.
    5. Authentic telemetry prerequisites:
       - Simulated trace -> None.
       - Delayed / unavailable telemetry status -> None.
       - Zero spans -> None.
    6. Production settings validation:
       - Invalid GRAFANA_BASE_URL under ENV=production raises ValueError at startup.
    """
    from urllib.parse import parse_qs, urlsplit
    from app.core.config import Settings
    from app.core.grafana_links import (
        build_grafana_dashboard_url,
        validate_grafana_base_url,
    )
    from fastapi.testclient import TestClient
    from app.main import app

    valid_base = "https://grafana.internal.mycompany.org"
    allowed_hosts = ["grafana.internal.mycompany.org", "telemetry.prod.mycompany.org"]
    now = datetime.now(UTC)

    # 1. Zero Default Trust: Unconfigured Base URL or Allowed Hosts -> None
    assert build_grafana_dashboard_url("trc_1", "run_1", base_url="", allowed_hosts=allowed_hosts, start_time=now) is None
    assert build_grafana_dashboard_url("trc_1", "run_1", base_url=valid_base, allowed_hosts=[], start_time=now) is None
    assert build_grafana_dashboard_url("trc_1", "run_1", base_url=valid_base, allowed_hosts=["grafana.com"], start_time=now) is None

    # 2. Strict urlsplit Validation & Attack Vectors
    # A. Insecure HTTP scheme
    is_v, _, err = validate_grafana_base_url("http://grafana.internal.mycompany.org", allowed_hosts)
    assert is_v is False
    assert "not https" in err

    # B. Userinfo / @ spoofing
    is_v, _, err = validate_grafana_base_url("https://user:pass@grafana.internal.mycompany.org", allowed_hosts)
    assert is_v is False
    assert "userinfo" in err

    is_v, _, err = validate_grafana_base_url("https://grafana.internal.mycompany.org@attacker.org", allowed_hosts)
    assert is_v is False
    assert "userinfo" in err

    # C. Non-standard & malformed port fail-closed
    is_v, _, err = validate_grafana_base_url("https://grafana.internal.mycompany.org:8080", allowed_hosts)
    assert is_v is False
    assert "port" in err

    # Malformed / unparseable or out-of-range ports fail closed without unhandled exceptions
    is_v, _, err = validate_grafana_base_url("https://grafana.internal.mycompany.org:abc", allowed_hosts)
    assert is_v is False
    assert err is not None
    assert build_grafana_dashboard_url("trc_1", "run_1", base_url="https://grafana.internal.mycompany.org:abc", allowed_hosts=allowed_hosts, start_time=now) is None

    is_v, _, err = validate_grafana_base_url("https://grafana.internal.mycompany.org:999999", allowed_hosts)
    assert is_v is False
    assert err is not None
    assert build_grafana_dashboard_url("trc_1", "run_1", base_url="https://grafana.internal.mycompany.org:999999", allowed_hosts=allowed_hosts, start_time=now) is None

    # Standard port 443 is permitted
    is_v, clean, _ = validate_grafana_base_url("https://grafana.internal.mycompany.org:443", allowed_hosts)
    assert is_v is True
    assert clean == "https://grafana.internal.mycompany.org"

    # D. Base URL with queries or fragments
    is_v, _, err = validate_grafana_base_url("https://grafana.internal.mycompany.org/dashboards?admin=1", allowed_hosts)
    assert is_v is False
    assert "query" in err

    is_v, _, err = validate_grafana_base_url("https://grafana.internal.mycompany.org/dashboards#section", allowed_hosts)
    assert is_v is False
    assert "fragment" in err

    # E. Suffix & subdomain spoofing
    is_v, _, err = validate_grafana_base_url("https://grafana.internal.mycompany.org.attacker.com", allowed_hosts)
    assert is_v is False
    assert "not in allowed hosts" in err

    is_v, _, err = validate_grafana_base_url("https://fake-grafana.internal.mycompany.org", allowed_hosts)
    assert is_v is False

    # Exact host matching only: no wildcard suffix inference
    is_v, _, err = validate_grafana_base_url("https://other.prod.mycompany.org", allowed_hosts)
    assert is_v is False
    assert "not in allowed hosts" in err

    # F. Valid explicitly configured host matching
    is_v, clean, _ = validate_grafana_base_url("https://telemetry.prod.mycompany.org", allowed_hosts)
    assert is_v is True
    assert clean == "https://telemetry.prod.mycompany.org"

    # 3. Structured URL Construction & Identifier Regex Fencing
    # A. Path traversal in dashboard_id rejected
    assert build_grafana_dashboard_url(
        "trc_1", "run_1", dashboard_id="../../etc/passwd",
        base_url=valid_base, allowed_hosts=allowed_hosts, start_time=now,
    ) is None

    # B. Control chars / spaces in dashboard_id rejected
    assert build_grafana_dashboard_url(
        "trc_1", "run_1", dashboard_id="invalid dash uid<script>",
        base_url=valid_base, allowed_hosts=allowed_hosts, start_time=now,
    ) is None

    # C. Parameter injection attempt in trace_id
    toxic_trace = "trc_legit&is_admin=true#fragment"
    # Fails identifier regex check
    assert build_grafana_dashboard_url(
        toxic_trace, "run_1",
        base_url=valid_base, allowed_hosts=allowed_hosts, start_time=now,
    ) is None

    # 4. Authoritative Server-Generated Time Range Bounds
    # A. Missing start_time fails close to None (prevents unbounded queries)
    assert build_grafana_dashboard_url(
        "trc_001", "run_001",
        base_url=valid_base, allowed_hosts=allowed_hosts, start_time=None,
    ) is None

    # B. Accurate epoch millisecond calculation with 15min buffer
    start_t = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)
    end_t = datetime(2026, 9, 5, 12, 10, 0, tzinfo=UTC)
    url = build_grafana_dashboard_url(
        trace_id="trc_valid_001",
        run_id="run_valid_001",
        space_id="sp_prod_01",
        start_time=start_t,
        end_time=end_t,
        base_url=valid_base,
        allowed_hosts=allowed_hosts,
    )
    assert url is not None
    parsed = urlsplit(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "grafana.internal.mycompany.org"
    assert parsed.path == "/d/studiotower-monitor"

    qs = parse_qs(parsed.query)
    assert qs["var-trace_id"] == ["trc_valid_001"]
    assert qs["var-run_id"] == ["run_valid_001"]
    assert qs["var-space_id"] == ["sp_prod_01"]

    expected_from = int((start_t - timedelta(minutes=15)).timestamp() * 1000)
    expected_to = int((end_t + timedelta(minutes=15)).timestamp() * 1000)
    assert int(qs["from"][0]) == expected_from
    assert int(qs["to"][0]) == expected_to
    assert int(qs["from"][0]) < int(qs["to"][0])

    # C. Maximum window duration cap (24 hours = 86,400,000 ms)
    long_end = start_t + timedelta(days=5)  # 5 days
    url_long = build_grafana_dashboard_url(
        trace_id="trc_valid_001",
        run_id="run_valid_001",
        start_time=start_t,
        end_time=long_end,
        base_url=valid_base,
        allowed_hosts=allowed_hosts,
    )
    qs_long = parse_qs(urlsplit(url_long).query)
    window_diff_ms = int(qs_long["to"][0]) - int(qs_long["from"][0])
    assert window_diff_ms == 24 * 3600 * 1000

    # 5. Authentic Telemetry Endpoint Integration
    client = TestClient(app)
    user = User(uid="u_e27", email="u27@test.com")
    store.save_user(user)
    space = Space(space_id="sp_e27", name="E27 Space", created_by=user.uid)
    store.create_space(space, creator_uid=user.uid)

    auth = {"Authorization": f"Bearer dev:{user.uid}:{user.email}"}

    # Configure mock Grafana in settings
    monkeypatch.setattr(settings, "GRAFANA_BASE_URL", valid_base)
    monkeypatch.setattr(settings, "GRAFANA_ALLOWED_HOSTS", allowed_hosts)

    # A. Simulated trace -> returns HTTP 404 on endpoint; Grafana MCP returns simulated bundle with grafana_dashboard_url=None
    from app.integrations.grafana_mcp import grafana_mcp
    sim_run = Run(
        run_id="run_e27_sim",
        space_id=space.space_id,
        created_by=user.uid,
        trace_id="sim_trc_27",
        status=RunStatus.FAILED,
    )
    store.save_run(sim_run)
    grafana_mcp.record_simulated_failure_trace(sim_run.trace_id, sim_run.run_id, space.space_id)

    res_sim = client.get(f"/v1/spaces/{space.space_id}/runs/{sim_run.run_id}/trace", headers=auth)
    assert res_sim.status_code == 404

    mcp_sim = grafana_mcp.query_trace(sim_run.trace_id, sim_run.run_id, space.space_id)
    assert mcp_sim.has_real_telemetry is False
    assert mcp_sim.grafana_dashboard_url is None

    # B. Real trace with status="delayed" or "unavailable" -> grafana_dashboard_url MUST remain None
    real_run = Run(
        run_id="run_e27_real",
        space_id=space.space_id,
        created_by=user.uid,
        trace_id="44444444444444444444444444444444",
        status=RunStatus.COMPLETED,
        telemetry_status="delayed",  # Not available yet!
    )
    store.save_run(real_run)

    from app.services.telemetry_service import MockTracingClient
    from app.core.telemetry_tenant import compute_tenant_hash
    th, _ = compute_tenant_hash(space.space_id)
    t0_iso = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    t1_iso = datetime.now(UTC).isoformat()
    real_trace_data = {
        "trace_id": real_run.trace_id,
        "spans": [
            {
                "name": "action_execution",
                "span_id": "span_e27_root",
                "start_time_iso": t0_iso,
                "end_time_iso": t1_iso,
                "attributes": {
                    "space_id_hash": th,
                    "run_id": real_run.run_id,
                },
            }
        ],
    }
    mock_client = MockTracingClient(traces={real_run.trace_id: real_trace_data})
    monkeypatch.setattr("app.services.telemetry_service.get_default_tracing_client", lambda: mock_client)

    res_delayed = client.get(f"/v1/spaces/{space.space_id}/runs/{real_run.run_id}/trace", headers=auth)
    assert res_delayed.status_code == 200
    data_delayed = res_delayed.json()
    assert data_delayed["has_real_telemetry"] is False
    assert data_delayed["grafana_dashboard_url"] is None

    # MCP bundle query for delayed run also strictly returns None (never bypasses telemetry_status)
    mcp_delayed = grafana_mcp.query_trace(real_run.trace_id, real_run.run_id, space.space_id)
    assert mcp_delayed.grafana_dashboard_url is None

    # C. Status promoted to "available" -> grafana_dashboard_url is generated with valid bounds
    real_run.telemetry_status = "available"
    store.save_run(real_run)

    res_available = client.get(f"/v1/spaces/{space.space_id}/runs/{real_run.run_id}/trace", headers=auth)
    assert res_available.status_code == 200
    data_available = res_available.json()
    assert data_available["has_real_telemetry"] is True
    dash_url = data_available["grafana_dashboard_url"]
    assert dash_url is not None
    assert dash_url.startswith("https://grafana.internal.mycompany.org/d/studiotower-monitor?")
    assert f"var-trace_id={real_run.trace_id}" in dash_url
    assert f"var-run_id={real_run.run_id}" in dash_url
    assert f"var-space_id={space.space_id}" in dash_url

    # Zero leakage: No secrets or tokens in URL
    assert "token" not in dash_url.lower()
    assert "secret" not in dash_url.lower()
    assert "bearer" not in dash_url.lower()

    # MCP bundle now delegates and retrieves the exact authoritative URL
    mcp_available = grafana_mcp.query_trace(real_run.trace_id, real_run.run_id, space.space_id)
    assert mcp_available.grafana_dashboard_url == dash_url

    # Foreign space query fails closed: MCP returns None
    mcp_foreign = grafana_mcp.query_trace(real_run.trace_id, real_run.run_id, "sp_nonexistent")
    assert mcp_foreign.grafana_dashboard_url is None
    assert mcp_foreign.has_real_telemetry is False

    # D. Regression test: local non-simulated span when TelemetryService returns None / verification fails
    local_tid = "trc_local_unverified_999"
    local_rid = "run_local_unverified_999"
    grafana_mcp.record_span(
        trace_id=local_tid,
        span_id="spn_local_1",
        name="local_diagnostic_action",
        duration_ms=50,
        status="ok",
    )
    # Space/Run does not exist in store -> TelemetryService returns None -> strictly has_real_telemetry=False
    local_bundle = grafana_mcp.query_trace(local_tid, run_id=local_rid, space_id=space.space_id)
    assert local_bundle.total_spans == 1
    assert local_bundle.has_real_telemetry is False
    assert local_bundle.is_local_diagnostic is True
    assert local_bundle.grafana_dashboard_url is None

    # 6. Settings Pydantic Bounds Enforcement (P2)
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Settings(GRAFANA_TIME_BUFFER_MINUTES=0)
    with pytest.raises(ValidationError):
        Settings(GRAFANA_TIME_BUFFER_MINUTES=61)
    with pytest.raises(ValidationError):
        Settings(GRAFANA_MAX_TIME_WINDOW_HOURS=0)
    with pytest.raises(ValidationError):
        Settings(GRAFANA_MAX_TIME_WINDOW_HOURS=25)

    # 7. Production Settings Fail-Closed Enforcement
    with pytest.raises(ValueError, match="https://"):
        Settings(
            ENV="production",
            STUDIO_TOWER_AUTH_MODE="firebase",
            STUDIO_TOWER_FIREBASE_PROJECT_ID="proj",
            STUDIO_TOWER_STORE="firestore",
            STUDIO_TOWER_ARTIFACT_BACKEND="gcs",
            STUDIO_TOWER_GCS_BUCKET="bucket",
            INGESTION_RUNNER="cloud_tasks",
            ACTION_RUNNER="cloud_tasks",
            STUDIO_TOWER_TASK_SECRET="a"*32,
            STUDIO_TOWER_MAINTENANCE_SECRET="b"*32,
            CURSOR_SIGNING_SECRET="c"*32,
            ACTION_SIGNING_SECRET="d"*32,
            STUDIO_TOWER_WORKER_SERVICE_URL="https://worker.run.app",
            STUDIO_TOWER_ALLOWED_WORKER_HOST="worker.run.app",
            STUDIO_TOWER_SCHEDULER_SA="studiotower-api@proj.iam.gserviceaccount.com",
            GRAFANA_BASE_URL="http://insecure-grafana.com",
            GRAFANA_ALLOWED_HOSTS=["insecure-grafana.com"],
        )

    with pytest.raises(ValueError, match="GRAFANA_ALLOWED_HOSTS"):
        Settings(
            ENV="production",
            STUDIO_TOWER_AUTH_MODE="firebase",
            STUDIO_TOWER_FIREBASE_PROJECT_ID="proj",
            STUDIO_TOWER_STORE="firestore",
            STUDIO_TOWER_ARTIFACT_BACKEND="gcs",
            STUDIO_TOWER_GCS_BUCKET="bucket",
            INGESTION_RUNNER="cloud_tasks",
            ACTION_RUNNER="cloud_tasks",
            STUDIO_TOWER_TASK_SECRET="a"*32,
            STUDIO_TOWER_MAINTENANCE_SECRET="b"*32,
            CURSOR_SIGNING_SECRET="c"*32,
            ACTION_SIGNING_SECRET="d"*32,
            STUDIO_TOWER_WORKER_SERVICE_URL="https://worker.run.app",
            STUDIO_TOWER_ALLOWED_WORKER_HOST="worker.run.app",
            STUDIO_TOWER_SCHEDULER_SA="studiotower-api@proj.iam.gserviceaccount.com",
            GRAFANA_BASE_URL="https://grafana.example.com",
            GRAFANA_ALLOWED_HOSTS=[],
        )


from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


# =============================================================================
# Gate E28: Structured Evidence Matching for Error Spans
# =============================================================================


def test_gate_e28_structured_evidence_matching():
    from app.integrations.grafana_mcp import grafana_mcp

    user = User(uid="u_e28", email="alice_e28@example.com")
    store.save_user(user)
    space = Space(space_id="sp_e28", name="E28 Space", created_by=user.uid)
    store.create_space(space, creator_uid=user.uid)

    run = Run(
        run_id="run_e28",
        space_id=space.space_id,
        status=RunStatus.FAILED,
        prompt="Execute extraction step",
        created_by=user.uid,
        failure_code="INGESTION_EXTRACTION_FAILURE",
    )
    store.save_run(run)

    span = grafana_mcp.record_span(
        trace_id=run.trace_id,
        span_id="spn_e28_extract",
        name="prodocux_extraction",
        duration_ms=120,
        status="error",
        error_code="INGESTION_EXTRACTION_FAILURE",
        attributes={"action_type": "extract", "stage": "ingestion"},
    )

    auth = {"Authorization": f"Bearer dev:{user.uid}:{user.email}"}
    res = client.post(f"/v1/spaces/{space.space_id}/runs/{run.run_id}/diagnose", headers=auth)
    assert res.status_code == 200
    diag = res.json()

    assert diag["diagnostic_status"] == "complete"
    assert diag["error_code"] == "INGESTION_EXTRACTION_FAILURE"
    assert diag["faulting_span"]["span_id"] == "spn_e28_extract"
    assert diag["faulting_span"]["name"] == "prodocux_extraction"
    assert diag["evidence_span_ids"] == ["spn_e28_extract"]
    assert "Document ingestion or chunk extraction encountered a parsing error" in diag["error_summary"]

    updated_run = store.get_run(run.run_id)
    assert updated_run.latest_diagnosis_id == diag["diagnosis_id"]
    assert updated_run.latest_diagnosis_status == "complete"
    assert updated_run.agent_diagnosis is not None


# =============================================================================
# Gate E29: Strict Rejection of Hallucinated / Cross-Trace Span IDs
# =============================================================================


def test_gate_e29_rejection_of_hallucinated_span_ids(monkeypatch):
    from app.services.diagnosis_service import DiagnosisService
    from app.services.telemetry_service import MockTracingClient
    from app.core.telemetry_tenant import compute_tenant_hash

    DiagnosisService.reset_state()
    user = User(uid="u_e29", email="alice_e29@example.com")
    store.save_user(user)
    space = Space(space_id="sp_e29", name="E29 Space", created_by=user.uid)
    store.create_space(space, creator_uid=user.uid)
    hash_val, _ = compute_tenant_hash(space.space_id)
    trace_id = "22222222222222222222222222222222"

    run = Run(
        run_id="run_e29",
        space_id=space.space_id,
        status=RunStatus.FAILED,
        prompt="Process step",
        created_by=user.uid,
        trace_id=trace_id,
        telemetry_status=TelemetryStatus.AVAILABLE.value,
        telemetry_generation=1,
    )
    store.save_run(run)

    t0_iso = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    t1_iso = datetime.now(UTC).isoformat()
    trace_data = {
        "trace_id": trace_id,
        "spans": [
            {
                "name": "action_execution",
                "span_id": "valid_001",
                "start_time_iso": t0_iso,
                "end_time_iso": t1_iso,
                "attributes": {"space_id_hash": hash_val, "run_id": run.run_id, "action_type": "extract"},
                "status": "error",
                "error_code": "STORAGE_CONFLICT",
            }
        ],
    }

    mock_client = MockTracingClient(traces={trace_id: trace_data})
    monkeypatch.setattr("app.services.telemetry_service.get_default_tracing_client", lambda: mock_client)
    monkeypatch.setenv("GEMINI_API_KEY", "mock_key_test")

    hallucinated_json = json.dumps({
        "error_code": "STORAGE_CONFLICT",
        "error_summary": "State version conflict",
        "observations": ["Conflict detected"],
        "likely_causes": ["CAS conflict"],
        "recommendations": ["Retry CAS"],
        "evidence_span_ids": ["spn_hallucinated_999999"],
        "faulting_span_id": "spn_fake_fault",
        "is_retryable": True,
        "suggested_action": "retry",
        "confidence": "high",
    })
    monkeypatch.setattr(DiagnosisService, "_call_gemini_api", lambda payload, key: hallucinated_json)

    auth = {"Authorization": f"Bearer dev:{user.uid}:{user.email}"}
    res = client.post(f"/v1/spaces/{space.space_id}/runs/{run.run_id}/diagnose", headers=auth)
    assert res.status_code == 200
    diag = res.json()

    assert diag["engine"] == "rule_based"
    assert diag["diagnostic_status"] == "fallback"
    assert "spn_hallucinated_999999" not in diag["evidence_span_ids"]
    assert diag["faulting_span"]["span_id"] == "valid_001"


# =============================================================================
# Gate E30: Clean Trace Returns no_failure_evidence
# =============================================================================


def test_gate_e30_clean_trace_no_failure_evidence(monkeypatch):
    from app.services.diagnosis_service import DiagnosisService
    from app.services.telemetry_service import MockTracingClient
    from app.core.telemetry_tenant import compute_tenant_hash

    DiagnosisService.reset_state()
    user = User(uid="u_e30", email="alice_e30@example.com")
    store.save_user(user)
    space = Space(space_id="sp_e30", name="E30 Space", created_by=user.uid)
    store.create_space(space, creator_uid=user.uid)
    hash_val, _ = compute_tenant_hash(space.space_id)
    trace_id = "33333333333333333333333333333333"

    run = Run(
        run_id="run_e30",
        space_id=space.space_id,
        status=RunStatus.COMPLETED,
        prompt="Successful workflow",
        created_by=user.uid,
        trace_id=trace_id,
        telemetry_status=TelemetryStatus.AVAILABLE.value,
        telemetry_generation=1,
    )
    store.save_run(run)

    t0_iso = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    t1_iso = datetime.now(UTC).isoformat()
    trace_data = {
        "trace_id": trace_id,
        "spans": [
            {
                "name": "action.step_one",
                "span_id": "spn_clean_1",
                "start_time_iso": t0_iso,
                "end_time_iso": t1_iso,
                "attributes": {"space_id_hash": hash_val, "run_id": run.run_id, "status": "ok"},
                "status": "ok",
            },
            {
                "name": "action.step_two",
                "span_id": "spn_clean_2",
                "start_time_iso": t0_iso,
                "end_time_iso": t1_iso,
                "attributes": {"space_id_hash": hash_val, "run_id": run.run_id, "status": "ok"},
                "status": "ok",
            },
        ],
    }

    mock_client = MockTracingClient(traces={trace_id: trace_data})
    monkeypatch.setattr("app.services.telemetry_service.get_default_tracing_client", lambda: mock_client)

    auth = {"Authorization": f"Bearer dev:{user.uid}:{user.email}"}
    res = client.post(f"/v1/spaces/{space.space_id}/runs/{run.run_id}/diagnose", headers=auth)
    assert res.status_code == 200
    diag = res.json()

    assert diag["diagnostic_status"] == "no_failure_evidence"
    assert diag["confidence"] == "high"
    assert diag["likely_causes"] == []
    assert diag["recommendations"] == []
    assert len(diag["evidence_span_ids"]) == 0
    assert "successfully with status OK" in diag["observations"][0]


# =============================================================================
# Gate E31: Local Diagnostic & Simulated Spans Strictly Bypass Gemini
# =============================================================================


def test_gate_e31_local_and_simulated_spans_bypass_gemini(monkeypatch):
    from app.services.diagnosis_service import DiagnosisService
    from app.integrations.grafana_mcp import grafana_mcp
    from unittest.mock import MagicMock

    DiagnosisService.reset_state()
    user = User(uid="u_e31", email="alice_e31@example.com")
    store.save_user(user)
    space = Space(space_id="sp_e31", name="E31 Space", created_by=user.uid)
    store.create_space(space, creator_uid=user.uid)

    run = Run(
        run_id="run_e31_sim",
        space_id=space.space_id,
        status=RunStatus.FAILED,
        prompt="Simulated failure",
        created_by=user.uid,
    )
    store.save_run(run)

    grafana_mcp.record_simulated_failure_trace(run.trace_id, run.run_id, space.space_id)

    monkeypatch.setenv("GEMINI_API_KEY", "mock_key_e31")
    gemini_mock = MagicMock()
    monkeypatch.setattr(DiagnosisService, "_call_gemini_api", gemini_mock)

    auth = {"Authorization": f"Bearer dev:{user.uid}:{user.email}"}
    res = client.post(f"/v1/spaces/{space.space_id}/runs/{run.run_id}/diagnose", headers=auth)
    assert res.status_code == 200
    diag = res.json()

    assert gemini_mock.call_count == 0
    assert diag["engine"] == "rule_based"
    assert diag["is_local_diagnostic"] is True
    assert diag["diagnostic_status"] == "complete"
    assert diag["faulting_span"]["name"] == "pdx_resource_constraint_evaluator"


# =============================================================================
# Gate E32: Pre-Input & Post-Output Double-Pass Sanitization
# =============================================================================


def test_gate_e32_pre_input_and_post_output_double_sanitization(monkeypatch):
    from app.services.diagnosis_service import DiagnosisService
    from app.services.telemetry_service import MockTracingClient
    from app.core.telemetry_tenant import compute_tenant_hash

    DiagnosisService.reset_state()
    user = User(uid="u_e32", email="alice_e32@example.com")
    store.save_user(user)
    space = Space(space_id="sp_e32", name="E32 Space", created_by=user.uid)
    store.create_space(space, creator_uid=user.uid)
    hash_val, _ = compute_tenant_hash(space.space_id)
    trace_id = "44444444444444444444444444444444"

    run = Run(
        run_id="run_e32",
        space_id=space.space_id,
        status=RunStatus.FAILED,
        prompt="Extraction run",
        created_by=user.uid,
        trace_id=trace_id,
        telemetry_status=TelemetryStatus.AVAILABLE.value,
        telemetry_generation=1,
    )
    store.save_run(run)

    t0_iso = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    t1_iso = datetime.now(UTC).isoformat()
    trace_data = {
        "trace_id": trace_id,
        "spans": [
            {
                "name": "action_execution",
                "span_id": "spn_dirty_1",
                "start_time_iso": t0_iso,
                "end_time_iso": t1_iso,
                "attributes": {
                    "space_id_hash": hash_val,
                    "run_id": run.run_id,
                    "action_type": "extract",
                    "api_key": "sk-leak99999999999999999999",
                    "bearer_token": "Bearer secret_jwt_12345",
                },
                "status": "error",
                "error_code": "STORAGE_COMMIT_TIMEOUT",
                "error_message": "Failed at /home/user/secrets/db.key with sk-leak99999999999999999999",
            }
        ],
    }

    mock_client = MockTracingClient(traces={trace_id: trace_data})
    monkeypatch.setattr("app.services.telemetry_service.get_default_tracing_client", lambda: mock_client)
    monkeypatch.setenv("GEMINI_API_KEY", "mock_key_e32")

    captured_prompt = {}
    def mock_gemini_caller(payload, key):
        captured_prompt.update(payload)
        return json.dumps({
            "error_code": "STORAGE_COMMIT_TIMEOUT",
            "error_summary": "Storage commit timed out near /etc/internal/conf.yaml with sk-leak99999999999999999999",
            "observations": ["Observed key Bearer secret_session_token_xyz"],
            "likely_causes": ["Contact admin@company.internal for access"],
            "recommendations": ["Check credentials at C:\\Users\\Administrator\\keys.pem"],
            "evidence_span_ids": ["spn_dirty_1"],
            "faulting_span_id": "spn_dirty_1",
            "is_retryable": True,
            "suggested_action": "wait_and_retry",
            "confidence": "high",
        })

    monkeypatch.setattr(DiagnosisService, "_call_gemini_api", mock_gemini_caller)

    auth = {"Authorization": f"Bearer dev:{user.uid}:{user.email}"}
    res = client.post(f"/v1/spaces/{space.space_id}/runs/{run.run_id}/diagnose", headers=auth)
    assert res.status_code == 200
    diag = res.json()

    # Pre-Input verification: raw error_message and non-allowlisted attributes are discarded
    prompt_str = json.dumps(captured_prompt)
    assert "/home/user/secrets" not in prompt_str
    assert "sk-leak" not in prompt_str
    assert "secret_jwt" not in prompt_str

    # Post-Output verification: LLM response secrets scrubbed
    diag_str = json.dumps(diag)
    assert "sk-leak" not in diag_str
    assert "/etc/internal" not in diag_str
    assert "admin@company.internal" not in diag_str
    assert "secret_session_token_xyz" not in diag_str
    assert "C:\\Users\\Administrator" not in diag_str
    assert "[REDACTED]" in diag["error_summary"]


# =============================================================================
# Gate E33: Single-Flight & In-Memory Cache Deduplication
# =============================================================================


def test_gate_e33_single_flight_and_cache_deduplication():
    from app.services.diagnosis_service import DiagnosisService
    from app.integrations.grafana_mcp import grafana_mcp
    import concurrent.futures

    DiagnosisService.reset_state()
    user = User(uid="u_e33", email="alice_e33@example.com")
    store.save_user(user)
    space = Space(space_id="sp_e33", name="E33 Space", created_by=user.uid)
    store.create_space(space, creator_uid=user.uid)

    run = Run(
        run_id="run_e33_flight",
        space_id=space.space_id,
        status=RunStatus.FAILED,
        prompt="Concurrency flight run",
        created_by=user.uid,
    )
    store.save_run(run)
    grafana_mcp.record_simulated_failure_trace(run.trace_id, run.run_id, space.space_id)

    DiagnosisService._rate_limiter.user_limit = 50
    results = []
    auth = {"Authorization": f"Bearer dev:{user.uid}:{user.email}"}

    def call_diagnose():
        return client.post(f"/v1/spaces/{space.space_id}/runs/{run.run_id}/diagnose", headers=auth)

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(call_diagnose) for _ in range(20)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    assert len(results) == 20
    assert all(r.status_code == 200 for r in results)

    diag_ids = {r.json()["diagnosis_id"] for r in results}
    assert len(diag_ids) == 1

    assert DiagnosisService._engine_execution_count == 1


# =============================================================================
# Gate E34: Deterministic Fallback on Timeout, Missing Key, and Invalid JSON
# =============================================================================


def test_gate_e34_deterministic_fallback_on_timeout_and_errors(monkeypatch):
    from app.services.diagnosis_service import DiagnosisService
    from app.services.telemetry_service import MockTracingClient
    from app.core.telemetry_tenant import compute_tenant_hash

    DiagnosisService.reset_state()
    user = User(uid="u_e34", email="alice_e34@example.com")
    store.save_user(user)
    space = Space(space_id="sp_e34", name="E34 Space", created_by=user.uid)
    store.create_space(space, creator_uid=user.uid)
    hash_val, _ = compute_tenant_hash(space.space_id)
    auth = {"Authorization": f"Bearer dev:{user.uid}:{user.email}"}

    run_a = Run(
        run_id="run_e34_nokey",
        space_id=space.space_id,
        status=RunStatus.FAILED,
        prompt="No key test",
        created_by=user.uid,
        trace_id="55555555555555555555555555555555",
        telemetry_status=TelemetryStatus.AVAILABLE.value,
    )
    store.save_run(run_a)

    run_b = Run(
        run_id="run_e34_timeout",
        space_id=space.space_id,
        status=RunStatus.FAILED,
        prompt="Timeout test",
        created_by=user.uid,
        trace_id="66666666666666666666666666666666",
        telemetry_status=TelemetryStatus.AVAILABLE.value,
    )
    store.save_run(run_b)

    run_c = Run(
        run_id="run_e34_badjson",
        space_id=space.space_id,
        status=RunStatus.FAILED,
        prompt="Bad JSON test",
        created_by=user.uid,
        trace_id="77777777777777777777777777777777",
        telemetry_status=TelemetryStatus.AVAILABLE.value,
    )
    store.save_run(run_c)

    t0_iso = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    t1_iso = datetime.now(UTC).isoformat()

    def make_trace(tid, rid):
        return {
            "trace_id": tid,
            "spans": [
                {
                    "name": "action_execution",
                    "span_id": f"spn_{tid[:6]}",
                    "start_time_iso": t0_iso,
                    "end_time_iso": t1_iso,
                    "attributes": {"space_id_hash": hash_val, "run_id": rid, "action_type": "exec"},
                    "status": "error",
                    "error_code": "AI_REASONING_TIMEOUT",
                }
            ],
        }

    mock_client = MockTracingClient(traces={
        run_a.trace_id: make_trace(run_a.trace_id, run_a.run_id),
        run_b.trace_id: make_trace(run_b.trace_id, run_b.run_id),
        run_c.trace_id: make_trace(run_c.trace_id, run_c.run_id),
    })
    monkeypatch.setattr("app.services.telemetry_service.get_default_tracing_client", lambda: mock_client)

    # Sub-case A: Missing API Key
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    res_a = client.post(f"/v1/spaces/{space.space_id}/runs/{run_a.run_id}/diagnose", headers=auth)
    assert res_a.status_code == 200
    diag_a = res_a.json()
    assert diag_a["engine"] == "rule_based"
    assert diag_a["diagnostic_status"] == "fallback"

    # Sub-case B: Timeout > 3.0s
    monkeypatch.setenv("GEMINI_API_KEY", "mock_key")
    def timeout_gemini(payload, key):
        time.sleep(3.5)
        return "{}"

    monkeypatch.setattr(DiagnosisService, "_call_gemini_api", timeout_gemini)

    res_b = client.post(f"/v1/spaces/{space.space_id}/runs/{run_b.run_id}/diagnose", headers=auth)
    assert res_b.status_code == 200
    diag_b = res_b.json()
    assert diag_b["engine"] == "rule_based"
    assert diag_b["diagnostic_status"] == "fallback"

    # Sub-case C: Malformed JSON output
    monkeypatch.setattr(DiagnosisService, "_call_gemini_api", lambda payload, key: "INTERNAL SERVER ERROR NOT JSON")

    res_c = client.post(f"/v1/spaces/{space.space_id}/runs/{run_c.run_id}/diagnose", headers=auth)
    assert res_c.status_code == 200
    diag_c = res_c.json()
    assert diag_c["engine"] == "rule_based"
    assert diag_c["diagnostic_status"] == "fallback"


# =============================================================================
# Gate E35: Version-Fenced CAS Prevents Stale Diagnosis Overwrites
# =============================================================================


def test_gate_e35_version_fenced_cas_prevents_stale_overwrites():
    from app.models.telemetry import DiagnosisRecord

    user = User(uid="u_e35", email="alice_e35@example.com")
    store.save_user(user)
    space = Space(space_id="sp_e35", name="E35 Space", created_by=user.uid)
    store.create_space(space, creator_uid=user.uid)

    run = Run(
        run_id="run_e35_cas",
        space_id=space.space_id,
        status=RunStatus.FAILED,
        prompt="CAS version fencing test",
        created_by=user.uid,
        telemetry_generation=2,
    )
    store.save_run(run)

    stale_diag = DiagnosisRecord(
        space_id=space.space_id,
        run_id=run.run_id,
        trace_id=run.trace_id,
        telemetry_generation=1,
        schema_version=1,
        engine="rule_based",
        diagnostic_status="complete",
        error_summary="Stale diagnosis finding",
    )

    commit_ok = store.atomic_commit_diagnosis(
        space_id=space.space_id,
        run_id=run.run_id,
        diagnosis=stale_diag,
        expected_generation=1,
    )
    assert commit_ok is False

    fresh_run = store.get_run(run.run_id)
    assert fresh_run.latest_diagnosis_id is None

    valid_diag = DiagnosisRecord(
        space_id=space.space_id,
        run_id=run.run_id,
        trace_id=run.trace_id,
        telemetry_generation=2,
        schema_version=1,
        engine="rule_based",
        diagnostic_status="complete",
        error_summary="Fresh generation 2 diagnosis",
    )
    commit_valid = store.atomic_commit_diagnosis(
        space_id=space.space_id,
        run_id=run.run_id,
        diagnosis=valid_diag,
        expected_generation=2,
    )
    assert commit_valid is True

    committed_run = store.get_run(run.run_id)
    assert committed_run.latest_diagnosis_id == valid_diag.diagnosis_id


# =============================================================================
# Gate E36: Non-Member, Unauthorized Role & Cross-Space Access Fails Closed
# =============================================================================


def test_gate_e36_unauthorized_and_cross_space_access_fails_closed():
    from app.services.diagnosis_service import DiagnosisService
    from app.integrations.grafana_mcp import grafana_mcp
    from app.models.space import MembershipRole

    DiagnosisService.reset_state()
    alice = User(uid="alice_e36", email="alice_e36@example.com")
    bob = User(uid="bob_e36", email="bob_e36@example.com")
    store.save_user(alice)
    store.save_user(bob)

    space = Space(space_id="sp_e36", name="E36 Space", created_by=alice.uid)
    store.create_space(space, creator_uid=alice.uid)

    run = Run(
        run_id="run_e36",
        space_id=space.space_id,
        status=RunStatus.FAILED,
        prompt="Security auth fencing",
        created_by=alice.uid,
    )
    store.save_run(run)
    grafana_mcp.record_simulated_failure_trace(run.trace_id, run.run_id, space.space_id)

    alice_auth = {"Authorization": f"Bearer dev:{alice.uid}:{alice.email}"}
    bob_auth = {"Authorization": f"Bearer dev:{bob.uid}:{bob.email}"}

    # 1. Alice diagnoses run -> 200 OK (warms cache)
    alice_res = client.post(f"/v1/spaces/{space.space_id}/runs/{run.run_id}/diagnose", headers=alice_auth)
    assert alice_res.status_code == 200

    # 2. Bob (Non-member) calls diagnose -> strictly 403 Forbidden even with warm cache
    bob_res = client.post(f"/v1/spaces/{space.space_id}/runs/{run.run_id}/diagnose", headers=bob_auth)
    assert bob_res.status_code == 403

    # 3. Add Bob to space, but with can_diagnose_runs = False
    store.add_member(space.space_id, bob.uid, MembershipRole.MEMBER)
    bob.can_diagnose_runs = False
    store.save_user(bob)

    bob_res2 = client.post(f"/v1/spaces/{space.space_id}/runs/{run.run_id}/diagnose", headers=bob_auth)
    assert bob_res2.status_code == 403
    assert "can_diagnose_runs" in bob_res2.json()["detail"]

    # 4. Cross-Space run query -> 404 Not Found
    other_space = Space(space_id="sp_e36_other", name="Other Space", created_by=alice.uid)
    store.create_space(other_space, creator_uid=alice.uid)
    cross_res = client.post(f"/v1/spaces/{other_space.space_id}/runs/{run.run_id}/diagnose", headers=alice_auth)
    assert cross_res.status_code == 404

    # 5. Rate limiting: Exceeding rate limit returns 429 with Retry-After header before cache read
    DiagnosisService._rate_limiter.user_limit = 2
    for _ in range(2):
        client.post(f"/v1/spaces/{space.space_id}/runs/{run.run_id}/diagnose", headers=alice_auth)
    rate_res = client.post(f"/v1/spaces/{space.space_id}/runs/{run.run_id}/diagnose", headers=alice_auth)
    assert rate_res.status_code == 429
    assert rate_res.headers.get("Retry-After") == "5"


# =============================================================================
# Gate E37: Workflow-Side-Effect-Free Read-Only Semantics
# =============================================================================


def test_gate_e37_workflow_side_effect_free_read_only_semantics():
    from app.services.diagnosis_service import DiagnosisService
    from app.integrations.grafana_mcp import grafana_mcp
    from app.models.run import ApprovalGate

    DiagnosisService.reset_state()
    user = User(uid="u_e37", email="alice_e37@example.com")
    store.save_user(user)
    space = Space(space_id="sp_e37", name="E37 Space", created_by=user.uid)
    store.create_space(space, creator_uid=user.uid)

    gate = ApprovalGate(
        title="High-Risk Pyrotechnic Action",
        description="Requires authorization before drone burn",
        risk_level="critical",
        status="pending",
    )
    run = Run(
        run_id="run_e37_readonly",
        space_id=space.space_id,
        status=RunStatus.FAILED,
        prompt="Destructive action execution",
        created_by=user.uid,
        approval_gate=gate,
        action_id="act_e37_drone",
    )
    store.save_run(run)
    grafana_mcp.record_simulated_failure_trace(run.trace_id, run.run_id, space.space_id)

    auth = {"Authorization": f"Bearer dev:{user.uid}:{user.email}"}
    res = client.post(f"/v1/spaces/{space.space_id}/runs/{run.run_id}/diagnose", headers=auth)
    assert res.status_code == 200

    after_run = store.get_run(run.run_id)

    # 1. Execution status must not be modified
    assert after_run.status == RunStatus.FAILED
    # 2. Risk approval gate must not be altered
    assert after_run.approval_gate is not None
    assert after_run.approval_gate.status == "pending"
    # 3. Action ID and artifacts must not be modified
    assert after_run.action_id == "act_e37_drone"
    # 4. Only observability pointers updated
    assert after_run.latest_diagnosis_id is not None
    assert after_run.latest_diagnosis_status == "complete"
    assert after_run.agent_diagnosis is not None


# =============================================================================
# Gate E38: Non-Mocked Gemini SDK Assembly & Settings Injection
# =============================================================================
def test_gate_e38_gemini_sdk_assembly_and_settings_injection(monkeypatch):
    """
    Verifies that _call_gemini_api correctly instantiates google.genai.Client,
    passes the HTTP timeout of 3000ms, binds response_schema=GeminiDiagnosisResponse,
    and handles missing/invalid API keys gracefully.
    """
    from app.services.diagnosis_service import DiagnosisService
    from app.models.telemetry import GeminiDiagnosisResponse
    from google import genai
    from google.genai import types

    captured_init_args = {}
    captured_generate_args = {}

    orig_client_init = genai.Client.__init__

    def mock_client_init(self, *args, **kwargs):
        captured_init_args.update(kwargs)
        orig_client_init(self, *args, **kwargs)

    class MockGenerateResponse:
        text = json.dumps({
            "faulting_span_id": "spn_err_01",
            "error_code": "STORAGE_COMMIT_TIMEOUT",
            "error_summary": "Storage write operation timed out during CAS commit",
            "observations": ["Span latency exceeded threshold"],
            "likely_causes": ["Firestore high contention"],
            "recommendations": ["Increase timeout budget"],
            "evidence_span_ids": ["spn_err_01"],
            "is_retryable": True,
            "suggested_action": "wait_and_retry",
            "confidence": "high",
        })

    def mock_generate_content(self, *args, **kwargs):
        captured_generate_args.update(kwargs)
        return MockGenerateResponse()

    monkeypatch.setattr(genai.Client, "__init__", mock_client_init)
    monkeypatch.setattr(genai.models.Models, "generate_content", mock_generate_content)

    test_payload = {"run_id": "run_test_sdk", "project_tag": "vfx", "error_spans": []}
    res_text = DiagnosisService._call_gemini_api(test_payload, "mock_sdk_api_key")

    assert res_text is not None
    assert captured_init_args.get("api_key") == "mock_sdk_api_key"
    assert isinstance(captured_init_args.get("http_options"), types.HttpOptions)
    assert captured_init_args["http_options"].timeout == 3000
    assert captured_generate_args.get("config").response_schema == GeminiDiagnosisResponse
    assert captured_generate_args.get("config").response_mime_type == "application/json"


# =============================================================================
# Gate E39: Bounded Admission Semaphore & Non-Blocking Timeout Under Load
# =============================================================================
def test_gate_e39_bounded_admission_semaphore_and_non_blocking_timeout(monkeypatch):
    """
    Verifies that DiagnosisService enforces a hard admission limit of 8 concurrent workers.
    Over-limit requests fail over gracefully to rule-based diagnosis.
    Permits are strictly released via future.add_done_callback() without starvation or permit leakage.
    """
    import concurrent.futures
    import threading
    from app.services.diagnosis_service import (
        DiagnosisService,
        _DIAGNOSIS_SEMAPHORE,
    )
    from app.services.telemetry_service import MockTracingClient
    from app.core.telemetry_tenant import compute_tenant_hash

    DiagnosisService.reset_state()
    user = User(uid="u_e39", email="alice_e39@example.com")
    store.save_user(user)
    space = Space(space_id="sp_e39", name="E39 Space", created_by=user.uid)
    store.create_space(space, creator_uid=user.uid)
    hash_val, _ = compute_tenant_hash(space.space_id)

    barrier = threading.Barrier(8)
    active_in_gemini = 0
    max_concurrent_observed = 0
    lock = threading.Lock()

    def slow_gemini_call(payload, api_key):
        nonlocal active_in_gemini, max_concurrent_observed
        with lock:
            active_in_gemini += 1
            if active_in_gemini > max_concurrent_observed:
                max_concurrent_observed = active_in_gemini
        try:
            barrier.wait(timeout=2.0)
        except Exception:
            pass
        time.sleep(0.3)
        with lock:
            active_in_gemini -= 1
        return json.dumps({
            "faulting_span_id": payload["error_spans"][0]["span_id"],
            "error_code": "STORAGE_COMMIT_TIMEOUT",
            "error_summary": "Storage timeout",
            "observations": ["Slow execution"],
            "likely_causes": ["Contention"],
            "recommendations": ["Retry"],
            "evidence_span_ids": [payload["error_spans"][0]["span_id"]],
            "is_retryable": True,
            "suggested_action": "wait_and_retry",
            "confidence": "high",
        })

    monkeypatch.setattr(DiagnosisService, "_call_gemini_api", slow_gemini_call)
    monkeypatch.setenv("GEMINI_API_KEY", "mock_key_e39")
    DiagnosisService._rate_limiter.user_limit = 50

    t0_iso = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    t1_iso = datetime.now(UTC).isoformat()
    trace_dict = {}

    runs = []
    for i in range(12):
        r = Run(
            run_id=f"run_e39_{i}",
            space_id=space.space_id,
            status=RunStatus.FAILED,
            prompt=f"Load test run {i}",
            created_by=user.uid,
            trace_id=f"e390000000000000000000000000{i:04x}",
            telemetry_status="available",
        )
        store.save_run(r)
        runs.append(r)
        trace_dict[r.trace_id] = {
            "trace_id": r.trace_id,
            "spans": [
                {
                    "name": "action_execution",
                    "span_id": f"spn_err_{i}",
                    "start_time_iso": t0_iso,
                    "end_time_iso": t1_iso,
                    "attributes": {"space_id_hash": hash_val, "run_id": r.run_id, "action_type": "exec"},
                    "status": "error",
                    "error_code": "STORAGE_COMMIT_TIMEOUT",
                }
            ],
        }

    mock_client = MockTracingClient(traces=trace_dict)
    monkeypatch.setattr("app.services.telemetry_service.get_default_tracing_client", lambda: mock_client)

    auth = {"Authorization": f"Bearer dev:{user.uid}:{user.email}"}

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        futures = [
            pool.submit(lambda r=r: client.post(f"/v1/spaces/{space.space_id}/runs/{r.run_id}/diagnose", headers=auth))
            for r in runs
        ]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    assert len(results) == 12
    assert all(r.status_code == 200 for r in results)
    assert max_concurrent_observed <= 8
    engines = [r.json()["engine"] for r in results]
    assert "rule_based" in engines

    # After burst finishes, semaphore value must restore to exactly 8 (no leaked permits)
    time.sleep(0.5)
    acquired_count = 0
    while _DIAGNOSIS_SEMAPHORE.acquire(blocking=False):
        acquired_count += 1
    assert acquired_count == 8
    for _ in range(8):
        _DIAGNOSIS_SEMAPHORE.release()


# =============================================================================
# Gate E40: Concurrent Diagnosis Revision CAS Fencing
# =============================================================================
def test_gate_e40_concurrent_diagnosis_revision_cas_fencing():
    """
    Verifies that atomic_commit_diagnosis enforces expected_diagnosis_revision,
    monotonically increments run.diagnosis_revision, and rejects stale revisions with 409 Conflict.
    """
    from app.models.telemetry import DiagnosisRecord
    user = User(uid="u_e40", email="alice_e40@example.com")
    store.save_user(user)
    space = Space(space_id="sp_e40", name="E40 Space", created_by=user.uid)
    store.create_space(space, creator_uid=user.uid)

    run = Run(
        run_id="run_e40_rev",
        space_id=space.space_id,
        status=RunStatus.FAILED,
        prompt="Revision fencing test",
        created_by=user.uid,
        telemetry_generation=1,
        diagnosis_revision=0,
    )
    store.save_run(run)

    diag_1 = DiagnosisRecord(
        diagnosis_id="diag_rev_1",
        space_id=space.space_id,
        run_id=run.run_id,
        trace_id=run.trace_id,
        telemetry_generation=1,
        schema_version=1,
        engine="rule_based",
        diagnostic_status="complete",
        error_summary="First diagnosis commit",
    )

    # First commit: expected_diagnosis_revision=0 -> Success, revision becomes 1
    ok1 = store.atomic_commit_diagnosis(
        space_id=space.space_id,
        run_id=run.run_id,
        diagnosis=diag_1,
        expected_generation=1,
        expected_trace_id=run.trace_id,
        expected_diagnosis_revision=0,
    )
    assert ok1 is True
    run_after_1 = store.get_run(run.run_id)
    assert run_after_1.diagnosis_revision == 1
    assert run_after_1.latest_diagnosis_id == "diag_rev_1"

    # Second commit with stale expected_diagnosis_revision=0 -> Fails closed (False)
    diag_stale = DiagnosisRecord(
        diagnosis_id="diag_rev_stale",
        space_id=space.space_id,
        run_id=run.run_id,
        trace_id=run.trace_id,
        telemetry_generation=1,
        schema_version=1,
        engine="rule_based",
        diagnostic_status="complete",
        error_summary="Stale revision commit",
    )
    ok_stale = store.atomic_commit_diagnosis(
        space_id=space.space_id,
        run_id=run.run_id,
        diagnosis=diag_stale,
        expected_generation=1,
        expected_trace_id=run.trace_id,
        expected_diagnosis_revision=0,
    )
    assert ok_stale is False
    assert store.get_run(run.run_id).latest_diagnosis_id == "diag_rev_1"

    # Second commit with current expected_diagnosis_revision=1 -> Success, revision becomes 2
    diag_2 = DiagnosisRecord(
        diagnosis_id="diag_rev_2",
        space_id=space.space_id,
        run_id=run.run_id,
        trace_id=run.trace_id,
        telemetry_generation=1,
        schema_version=1,
        engine="rule_based",
        diagnostic_status="complete",
        error_summary="Second diagnosis commit",
    )
    ok2 = store.atomic_commit_diagnosis(
        space_id=space.space_id,
        run_id=run.run_id,
        diagnosis=diag_2,
        expected_generation=1,
        expected_trace_id=run.trace_id,
        expected_diagnosis_revision=1,
    )
    assert ok2 is True
    assert store.get_run(run.run_id).diagnosis_revision == 2
    assert store.get_run(run.run_id).latest_diagnosis_id == "diag_rev_2"


# =============================================================================
# Gate E41: Storage Commit Failure Fail-Closed & Zero Cache Poisoning
# =============================================================================
def test_gate_e41_storage_commit_failure_fail_closed(monkeypatch):
    """
    Verifies that when storage commit fails (CAS mismatch -> 409, DB failure -> 503):
    1. HTTP response strictly reflects the error status.
    2. Zero cache poisoning occurs (subsequent valid call does not return failed diagnosis).
    """
    from app.services.diagnosis_service import DiagnosisService
    from app.integrations.grafana_mcp import grafana_mcp
    from app.services.storage import StorageUnavailableError

    DiagnosisService.reset_state()
    user = User(uid="u_e41", email="alice_e41@example.com")
    store.save_user(user)
    space = Space(space_id="sp_e41", name="E41 Space", created_by=user.uid)
    store.create_space(space, creator_uid=user.uid)

    run = Run(
        run_id="run_e41_poison",
        space_id=space.space_id,
        status=RunStatus.FAILED,
        prompt="Storage failure test",
        created_by=user.uid,
        telemetry_generation=1,
    )
    store.save_run(run)
    grafana_mcp.record_simulated_failure_trace(run.trace_id, run.run_id, space.space_id)

    auth = {"Authorization": f"Bearer dev:{user.uid}:{user.email}"}

    def mock_failing_commit(*args, **kwargs):
        raise StorageUnavailableError("Database unreachable")

    monkeypatch.setattr(store, "atomic_commit_diagnosis", mock_failing_commit)

    res = client.post(f"/v1/spaces/{space.space_id}/runs/{run.run_id}/diagnose", headers=auth)
    assert res.status_code == 503
    assert "temporarily unavailable" in res.json()["detail"]

    cache_key = f"{space.space_id}:{run.run_id}:{run.trace_id}:{run.telemetry_generation}:v1"
    assert DiagnosisService._get_from_cache(cache_key) is None

    monkeypatch.undo()
    res_ok = client.post(f"/v1/spaces/{space.space_id}/runs/{run.run_id}/diagnose", headers=auth)
    assert res_ok.status_code == 200
    assert DiagnosisService._get_from_cache(cache_key) is not None


# =============================================================================
# Gate E42: Strict Pydantic Parsing & Error-Span Isolation
# =============================================================================
def test_gate_e42_strict_pydantic_parsing_and_error_span_isolation(monkeypatch):
    """
    Verifies that Gemini responses with:
    1. Extra undeclared fields (extra="forbid")
    2. Invalid enums
    3. Hallucinated spans or spans with status != "error" (e.g. OK span)
    are strictly rejected, failing back to the deterministic rule-based engine.
    """
    from app.services.diagnosis_service import DiagnosisService
    from app.services.telemetry_service import MockTracingClient
    from app.core.telemetry_tenant import compute_tenant_hash

    DiagnosisService.reset_state()
    user = User(uid="u_e42", email="alice_e42@example.com")
    store.save_user(user)
    space = Space(space_id="sp_e42", name="E42 Space", created_by=user.uid)
    store.create_space(space, creator_uid=user.uid)
    hash_val, _ = compute_tenant_hash(space.space_id)

    run = Run(
        run_id="run_e42_strict",
        space_id=space.space_id,
        status=RunStatus.FAILED,
        prompt="Strict parsing run",
        created_by=user.uid,
        trace_id="42424242424242424242424242424242",
        telemetry_status="available",
    )
    store.save_run(run)

    t0_iso = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    t1_iso = datetime.now(UTC).isoformat()

    mock_client = MockTracingClient(traces={
        run.trace_id: {
            "trace_id": run.trace_id,
            "spans": [
                {
                    "name": "pipeline_stage",
                    "span_id": "spn_ok_001",
                    "start_time_iso": t0_iso,
                    "end_time_iso": t1_iso,
                    "attributes": {"space_id_hash": hash_val, "run_id": run.run_id, "action_type": "exec"},
                    "status": "ok",
                },
                {
                    "name": "action_execution",
                    "span_id": "spn_error_002",
                    "start_time_iso": t0_iso,
                    "end_time_iso": t1_iso,
                    "attributes": {"space_id_hash": hash_val, "run_id": run.run_id, "action_type": "exec"},
                    "status": "error",
                    "error_code": "STORAGE_COMMIT_TIMEOUT",
                },
            ],
        }
    })
    monkeypatch.setattr("app.services.telemetry_service.get_default_tracing_client", lambda: mock_client)
    monkeypatch.setenv("GEMINI_API_KEY", "mock_key_e42")
    auth = {"Authorization": f"Bearer dev:{user.uid}:{user.email}"}

    # Sub-case A: Extra forbidden field in Gemini output -> Failover
    extra_field_json = json.dumps({
        "faulting_span_id": "spn_error_002",
        "error_code": "STORAGE_COMMIT_TIMEOUT",
        "error_summary": "Storage timeout",
        "observations": ["Timeout observed"],
        "likely_causes": ["Contention"],
        "recommendations": ["Retry"],
        "evidence_span_ids": ["spn_error_002"],
        "is_retryable": True,
        "suggested_action": "wait_and_retry",
        "confidence": "high",
        "unauthorized_extra_payload": "injected_data",
    })
    monkeypatch.setattr(DiagnosisService, "_call_gemini_api", lambda p, k: extra_field_json)
    res_a = client.post(f"/v1/spaces/{space.space_id}/runs/{run.run_id}/diagnose", headers=auth)
    assert res_a.status_code == 200
    assert res_a.json()["engine"] == "rule_based"
    assert res_a.json()["diagnostic_status"] == "fallback"

    # Sub-case B: Invalid enum in suggested_action -> Failover
    DiagnosisService.reset_state()
    bad_enum_json = json.dumps({
        "faulting_span_id": "spn_error_002",
        "error_code": "STORAGE_COMMIT_TIMEOUT",
        "error_summary": "Storage timeout",
        "observations": ["Timeout observed"],
        "likely_causes": ["Contention"],
        "recommendations": ["Retry"],
        "evidence_span_ids": ["spn_error_002"],
        "is_retryable": True,
        "suggested_action": "fly_to_outer_space",
        "confidence": "high",
    })
    monkeypatch.setattr(DiagnosisService, "_call_gemini_api", lambda p, k: bad_enum_json)
    res_b = client.post(f"/v1/spaces/{space.space_id}/runs/{run.run_id}/diagnose", headers=auth)
    assert res_b.status_code == 200
    assert res_b.json()["engine"] == "rule_based"

    # Sub-case C: Cites an OK span (spn_ok_001) as faulting span -> Error-span isolation rejects it
    DiagnosisService.reset_state()
    ok_span_cited_json = json.dumps({
        "faulting_span_id": "spn_ok_001",
        "error_code": "STORAGE_COMMIT_TIMEOUT",
        "error_summary": "Storage timeout",
        "observations": ["Timeout observed"],
        "likely_causes": ["Contention"],
        "recommendations": ["Retry"],
        "evidence_span_ids": ["spn_ok_001"],
        "is_retryable": True,
        "suggested_action": "wait_and_retry",
        "confidence": "high",
    })
    monkeypatch.setattr(DiagnosisService, "_call_gemini_api", lambda p, k: ok_span_cited_json)
    res_c = client.post(f"/v1/spaces/{space.space_id}/runs/{run.run_id}/diagnose", headers=auth)
    assert res_c.status_code == 200
    assert res_c.json()["engine"] == "rule_based"
    assert res_c.json()["faulting_span"]["span_id"] == "spn_error_002"


# =============================================================================
# Gate E43: Universal Attribute Allowlist & Secret Scrubbing
# =============================================================================
def test_gate_e43_universal_attribute_allowlist_and_secret_scrubbing(monkeypatch):
    """
    Validates that:
    1. Only attributes declared in SPAN_ALLOWED_ATTRIBUTES survive in production trace spans.
    2. Non-allowlisted keys (e.g. database_url, sql_query, internal_ip) are completely discarded.
    """
    from app.services.diagnosis_service import DiagnosisService
    from app.services.telemetry_service import MockTracingClient
    from app.core.telemetry_tenant import compute_tenant_hash

    DiagnosisService.reset_state()
    user = User(uid="u_e43", email="alice_e43@example.com")
    store.save_user(user)
    space = Space(space_id="sp_e43", name="E43 Space", created_by=user.uid)
    store.create_space(space, creator_uid=user.uid)
    hash_val, _ = compute_tenant_hash(space.space_id)

    run = Run(
        run_id="run_e43_attrs",
        space_id=space.space_id,
        status=RunStatus.FAILED,
        prompt="Attribute allowlist test",
        created_by=user.uid,
        telemetry_status="available",
    )
    store.save_run(run)

    t0_iso = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    t1_iso = datetime.now(UTC).isoformat()

    mock_client = MockTracingClient(traces={
        run.trace_id: {
            "trace_id": run.trace_id,
            "spans": [
                {
                    "name": "action_execution",
                    "span_id": "spn_dirty_attrs",
                    "start_time_iso": t0_iso,
                    "end_time_iso": t1_iso,
                    "attributes": {
                        "space_id_hash": hash_val,
                        "run_id": run.run_id,
                        "action_type": "compile",
                        "database_url": "postgres://user:pass@internal-db:5432/prod",
                        "raw_sql": "SELECT * FROM users WHERE ssn = '123-45-6789'",
                        "server_ip": "10.0.4.12",
                        "error_code": "STORAGE_COMMIT_TIMEOUT",
                    },
                    "status": "error",
                    "error_code": "STORAGE_COMMIT_TIMEOUT",
                }
            ],
        }
    })
    monkeypatch.setattr("app.services.telemetry_service.get_default_tracing_client", lambda: mock_client)

    auth = {"Authorization": f"Bearer dev:{user.uid}:{user.email}"}
    res = client.post(f"/v1/spaces/{space.space_id}/runs/{run.run_id}/diagnose", headers=auth)
    assert res.status_code == 200
    diag = res.json()

    key_attrs = diag["faulting_span"]["key_attributes"]
    assert "space_id_hash" in key_attrs
    assert "action_type" in key_attrs
    assert "database_url" not in key_attrs
    assert "raw_sql" not in key_attrs
    assert "server_ip" not in key_attrs


# =============================================================================
# Gate E44: Cross-Instance Claim Lease & Shared Sliding-Window Rate Limit
# =============================================================================
def test_gate_e44_cross_instance_claim_lease_and_shared_sliding_window():
    """
    Verifies:
    1. Claim Lease requires and validates lease_token.
    2. complete_run_diagnosis_claim fails if lease_token does not match or lease expired.
    3. check_and_record_dual_rate_limit enforces atomic continuous 60s sliding window.
    4. Rejection of Space limit does not consume User quota.
    """
    from app.models.telemetry import DiagnosisClaimStatus

    store.clear()
    space_id = "sp_e44"
    run_id = "run_e44"
    user_id = "u_e44"

    run = Run(
        run_id=run_id,
        space_id=space_id,
        status=RunStatus.FAILED,
        prompt="Claim test",
        created_by=user_id,
        telemetry_generation=1,
    )
    store.save_run(run)

    # 1. Claim execution rights
    claimed, claim_record, comp_diag = store.claim_run_diagnosis(
        space_id=space_id,
        run_id=run_id,
        expected_generation=1,
        lease_owner="worker_alpha",
        lease_duration_sec=10.0,
    )
    assert claimed is True
    assert claim_record is not None
    assert claim_record.lease_token is not None
    assert claim_record.status == DiagnosisClaimStatus.RUNNING.value

    # 2. Complete with incorrect lease_token -> Fails closed (False)
    wrong_complete = store.complete_run_diagnosis_claim(
        space_id=space_id,
        run_id=run_id,
        lease_owner="worker_alpha",
        lease_token="invalid_guessed_token_1234",
        status="completed",
    )
    assert wrong_complete is False

    # 3. Complete with correct lease_token -> Succeeds (True)
    valid_complete = store.complete_run_diagnosis_claim(
        space_id=space_id,
        run_id=run_id,
        lease_owner="worker_alpha",
        lease_token=claim_record.lease_token,
        status="completed",
        diagnosis_id="diag_completed_001",
    )
    assert valid_complete is True

    # 4. Sliding-Window Rate Limit: Atomicity and quota preservation
    # User limit = 3, Space limit = 2
    # Call 1: Allowed
    ok1, _ = store.check_and_record_dual_rate_limit(space_id, user_id, user_limit=3, space_limit=2, window_seconds=60.0)
    assert ok1 is True

    # Call 2: Allowed
    ok2, _ = store.check_and_record_dual_rate_limit(space_id, user_id, user_limit=3, space_limit=2, window_seconds=60.0)
    assert ok2 is True

    # Call 3: Space limit reached (2/2) -> Rejected
    ok3, retry3 = store.check_and_record_dual_rate_limit(space_id, user_id, user_limit=3, space_limit=2, window_seconds=60.0)
    assert ok3 is False
    assert retry3 > 0

    # User quota was NOT consumed by the rejected Call 3 in a different space
    other_space_id = "sp_e44_other"
    ok_user_quota, _ = store.check_and_record_dual_rate_limit(
        other_space_id, user_id, user_limit=3, space_limit=10, window_seconds=60.0
    )
    assert ok_user_quota is True


def test_gate_e45_gemini_bounded_semaphore_no_double_release(monkeypatch):
    """
    Gate E45: Gemini admission semaphore is a BoundedSemaphore(8).
    When Gemini API calls fail or raise exceptions repeatedly, the semaphore
    must never be double-released (which raises ValueError in BoundedSemaphore).
    After repeated failures, available permits must remain strictly at 8.
    """
    from app.services.diagnosis_service import _DIAGNOSIS_SEMAPHORE, DiagnosisService
    from app.models.telemetry import RunTraceResponse, SpanWaterfallNode

    monkeypatch.setenv("GEMINI_API_KEY", "test-key-e45")
    
    # Ensure semaphore is at full capacity (8)
    for _ in range(8):
        assert _DIAGNOSIS_SEMAPHORE.acquire(blocking=False) is True
    for _ in range(8):
        _DIAGNOSIS_SEMAPHORE.release()

    # Verify BoundedSemaphore property: releasing beyond initial count raises ValueError
    with pytest.raises(ValueError):
        _DIAGNOSIS_SEMAPHORE.release()

    # Mock _call_gemini_api to raise an exception immediately
    def mock_failing_gemini(*args, **kwargs):
        raise RuntimeError("Simulated transient Gemini exception")

    monkeypatch.setattr(DiagnosisService, "_call_gemini_api", mock_failing_gemini)

    dummy_run = Run(
        run_id="run_e45_test",
        space_id="sp_e45",
        status=RunStatus.FAILED,
        prompt="Test prompt",
        created_by="usr_e45",
        trace_id="trace_e45",
        telemetry_generation=1,
    )
    dummy_bundle = RunTraceResponse(
        trace_id="trace_e45",
        run_id="run_e45_test",
        space_id="sp_e45",
        total_spans=1,
        root_spans=[],
        has_real_telemetry=True,
    )
    dummy_error_spans = [
        SpanWaterfallNode(
            span_id="span_e45_err",
            name="error_step",
            duration_ms=100.0,
            status="error",
            error_code="SERVICE_UNAVAILABLE",
            start_time_iso="2026-09-05T12:00:00Z",
            end_time_iso="2026-09-05T12:00:00.100Z",
            offset_ms=0.0,
        )
    ]

    # Execute 20 consecutive failing calls.
    # If double-release occurred, ValueError would be thrown, or permits would drift.
    for i in range(20):
        res = DiagnosisService._try_gemini_diagnosis(
            space_id="sp_e45",
            run=dummy_run,
            trace_bundle=dummy_bundle,
            error_spans=dummy_error_spans,
        )
        assert res is None

    # Verify that exactly 8 permits can be acquired (meaning 0 permit leaks or inflation)
    acquired_permits = 0
    for _ in range(8):
        if _DIAGNOSIS_SEMAPHORE.acquire(blocking=False):
            acquired_permits += 1
    assert acquired_permits == 8
    # 9th acquire must fail (non-blocking)
    assert _DIAGNOSIS_SEMAPHORE.acquire(blocking=False) is False

    # Restore permits
    for _ in range(8):
        _DIAGNOSIS_SEMAPHORE.release()


def test_gate_e46_completed_claim_fencing_across_telemetry_generations():
    """
    Gate E46: Completed diagnosis claim must NOT return stale diagnosis from older
    telemetry generation or trace ID. When a Run advances to generation 2 or gets a new
    trace, claiming must issue a fresh claim and ignore the older generation claim.
    """
    from app.models.telemetry import DiagnosisRecord

    space_id = "sp_e46"
    run_id = "run_e46"
    user_id = "usr_e46"

    # Reset in-memory store claim structures
    if hasattr(store, "_diagnosis_claims"):
        store._diagnosis_claims.clear()

    # 1. Run at generation 1, trace 1
    run = Run(
        run_id=run_id,
        space_id=space_id,
        status=RunStatus.FAILED,
        prompt="Initial failure",
        created_by=user_id,
        trace_id="trace_gen_1",
        telemetry_generation=1,
    )
    store.save_run(run)

    # Claim for gen 1
    claimed, claim_rec, comp = store.claim_run_diagnosis(
        space_id=space_id,
        run_id=run_id,
        expected_generation=1,
        lease_owner="worker_1",
        lease_duration_sec=10.0,
        expected_trace_id="trace_gen_1",
    )
    assert claimed is True
    assert claim_rec.telemetry_generation == 1
    assert claim_rec.trace_id == "trace_gen_1"

    # Commit diagnosis for gen 1
    diag1 = DiagnosisRecord(
        diagnosis_id="diag_gen1_record",
        space_id=space_id,
        run_id=run_id,
        trace_id="trace_gen_1",
        telemetry_generation=1,
        schema_version=1,
        engine="rule_based",
        diagnostic_status="identified",
        error_code="RATE_LIMIT_EXCEEDED",
        error_summary="Generation 1 root cause",
        confidence="high",
        suggested_action="retry",
    )
    store.save_diagnosis(diag1)
    store.complete_run_diagnosis_claim(
        space_id=space_id,
        run_id=run_id,
        lease_owner="worker_1",
        lease_token=claim_rec.lease_token,
        diagnosis_id=diag1.diagnosis_id,
        status="completed",
    )

    # If queried for gen 1, it should return completed diag1
    c1, rec1, comp1 = store.claim_run_diagnosis(
        space_id=space_id,
        run_id=run_id,
        expected_generation=1,
        lease_owner="worker_2",
        expected_trace_id="trace_gen_1",
    )
    assert c1 is False
    assert comp1 is not None
    assert comp1.diagnosis_id == "diag_gen1_record"

    # 2. Advance Run to generation 2, new trace
    run.telemetry_generation = 2
    run.trace_id = "trace_gen_2"
    store.save_run(run)

    # Claiming for gen 2 MUST NOT return gen 1 diagnosis
    c2, rec2, comp2 = store.claim_run_diagnosis(
        space_id=space_id,
        run_id=run_id,
        expected_generation=2,
        lease_owner="worker_3",
        expected_trace_id="trace_gen_2",
    )
    assert c2 is True  # Successfully claimed fresh lease!
    assert comp2 is None  # Did NOT leak generation 1 diagnosis!
    assert rec2.telemetry_generation == 2
    assert rec2.trace_id == "trace_gen_2"


def test_gate_e47_firestore_rate_limiter_fail_closed_and_503(monkeypatch):
    """
    Gate E47: Firestore rate limiting must fail closed.
    When Firestore encounters an error during check_and_record_dual_rate_limit,
    it must raise StorageUnavailableError instead of failing open to MemoryStore.
    DiagnosisService.check_rate_limit must catch this and return HTTP 503.
    """
    from fastapi import HTTPException
    from app.services.storage import FirestoreStore, StorageUnavailableError
    from app.services.diagnosis_service import DiagnosisService
    from unittest.mock import MagicMock

    mock_client = MagicMock()
    # Force transaction to raise
    mock_client.transaction.side_effect = RuntimeError("Firestore unavailable (DEADLINE_EXCEEDED)")

    fs_store = FirestoreStore(project_id="test-proj", client=mock_client)

    # Directly verify FirestoreStore raises StorageUnavailableError
    with pytest.raises(StorageUnavailableError) as exc_info:
        fs_store.check_and_record_dual_rate_limit(
            space_id="sp_e47",
            user_id="usr_e47",
            user_limit=10,
            space_limit=30,
        )
    assert "Firestore rate limiting unavailable" in str(exc_info.value)

    # Verify DiagnosisService.check_rate_limit raises HTTP 503
    monkeypatch.setattr("app.services.diagnosis_service.store", fs_store)

    with pytest.raises(HTTPException) as http_exc:
        DiagnosisService.check_rate_limit("sp_e47", "usr_e47")

    assert http_exc.value.status_code == 503
    assert "Rate limiting storage service temporarily unavailable" in http_exc.value.detail


def test_gate_e48_atomic_commit_with_claim_lease_fencing(monkeypatch):
    """
    Gate E48: Atomic Diagnosis Commit with Claim Lease Fencing.
    Verifies that atomic_commit_diagnosis_with_claim binds Claim Lease validation
    (status==RUNNING, lease_owner, lease_token, lease_until > now) in the same
    transaction as Diagnosis creation and Run pointer/revision mutation.

    Verifies:
      1. MemoryStore: Stale Worker A is rejected; Worker B commits; late Worker A cleanup rejected.
      2. FirestoreStore (transactional fake): Same fencing across distributed Cloud Run model.
      3. DiagnosisService: Stale worker receives HTTP 409 Conflict with zero cache pollution.
    """
    from datetime import datetime, timedelta, UTC
    import sys
    from unittest.mock import MagicMock, patch
    from fastapi import HTTPException
    from app.models.run import Run, RunStatus
    from app.models.telemetry import DiagnosisRecord, DiagnosisClaimRecord, DiagnosisClaimStatus
    from app.services.storage import MemoryStore, FirestoreStore
    from app.services.diagnosis_service import DiagnosisService

    space_id = "sp_e48"
    run_id = "run_e48"
    trace_id = "trace_e48"

    # =========================================================================
    # Part 1: MemoryStore Verification
    # =========================================================================
    mem_store = MemoryStore()
    run = Run(
        run_id=run_id,
        space_id=space_id,
        status=RunStatus.FAILED,
        prompt="Gate E48 prompt",
        created_by="usr_e48",
        trace_id=trace_id,
        telemetry_generation=1,
        diagnosis_revision=0,
    )
    mem_store.save_run(run)

    # 1. Worker A claims lease
    claimed_a, claim_a, _ = mem_store.claim_run_diagnosis(
        space_id=space_id,
        run_id=run_id,
        expected_generation=1,
        lease_owner="worker_a",
        lease_duration_sec=10.0,
        expected_trace_id=trace_id,
    )
    assert claimed_a is True
    assert claim_a is not None
    token_a = claim_a.lease_token

    # 2. Artificially expire Worker A's lease in storage
    mem_store._diagnosis_claims[(space_id, run_id)]["lease_until"] = (
        datetime.now(UTC) - timedelta(seconds=1)
    ).isoformat()

    # 3. Worker B claims lease after Worker A's lease expired
    claimed_b, claim_b, _ = mem_store.claim_run_diagnosis(
        space_id=space_id,
        run_id=run_id,
        expected_generation=1,
        lease_owner="worker_b",
        lease_duration_sec=10.0,
        expected_trace_id=trace_id,
    )
    assert claimed_b is True
    assert claim_b is not None
    token_b = claim_b.lease_token
    assert token_b != token_a

    # 4. Stale Worker A attempts atomic commit with old token -> MUST BE REJECTED
    diag_a = DiagnosisRecord(
        diagnosis_id="diag_e48_a",
        space_id=space_id,
        run_id=run_id,
        trace_id=trace_id,
        telemetry_generation=1,
        schema_version=1,
        engine="rule_based",
        diagnostic_status="identified",
        error_code="SERVICE_UNAVAILABLE",
        error_summary="Stale diagnosis from Worker A",
    )
    ok_a = mem_store.atomic_commit_diagnosis_with_claim(
        space_id=space_id,
        run_id=run_id,
        diagnosis=diag_a,
        expected_generation=1,
        lease_owner="worker_a",
        lease_token=token_a,
        expected_trace_id=trace_id,
        expected_diagnosis_revision=0,
    )
    assert ok_a is False

    # Assert Worker A's rejected attempt did NOT mutate Run revision or pointer
    run_after_a = mem_store.get_run(run_id)
    assert run_after_a.diagnosis_revision == 0
    assert run_after_a.latest_diagnosis_id is None
    assert mem_store.get_diagnosis(space_id, run_id) is None
    # Worker B's claim remains RUNNING with Worker B's token
    current_claim = mem_store._diagnosis_claims[(space_id, run_id)]
    assert current_claim["status"] == "running"
    assert current_claim["lease_owner"] == "worker_b"
    assert current_claim["lease_token"] == token_b

    # 5. Legitimate Worker B attempts atomic commit with token B -> MUST SUCCEED
    diag_b = DiagnosisRecord(
        diagnosis_id="diag_e48_b",
        space_id=space_id,
        run_id=run_id,
        trace_id=trace_id,
        telemetry_generation=1,
        schema_version=1,
        engine="rule_based",
        diagnostic_status="identified",
        error_code="RATE_LIMIT_EXCEEDED",
        error_summary="Authentic diagnosis from Worker B",
    )
    ok_b = mem_store.atomic_commit_diagnosis_with_claim(
        space_id=space_id,
        run_id=run_id,
        diagnosis=diag_b,
        expected_generation=1,
        lease_owner="worker_b",
        lease_token=token_b,
        expected_trace_id=trace_id,
        expected_diagnosis_revision=0,
    )
    assert ok_b is True

    # Assert Worker B's commit atomically updated Run, Diagnosis, and Claim
    run_after_b = mem_store.get_run(run_id)
    assert run_after_b.diagnosis_revision == 1
    assert run_after_b.latest_diagnosis_id == "diag_e48_b"
    assert mem_store.get_diagnosis(space_id, run_id).diagnosis_id == "diag_e48_b"
    claim_after_b = mem_store._diagnosis_claims[(space_id, run_id)]
    assert claim_after_b["status"] == "completed"
    assert claim_after_b["diagnosis_id"] == "diag_e48_b"
    assert claim_after_b["lease_owner"] == "worker_b"

    # 6. Worker A tries late complete or fail cleanup -> MUST BE REJECTED
    late_comp_a = mem_store.complete_run_diagnosis_claim(
        space_id=space_id,
        run_id=run_id,
        lease_owner="worker_a",
        lease_token=token_a,
        status="completed",
    )
    assert late_comp_a is False
    late_fail_a = mem_store.fail_run_diagnosis_claim(
        space_id=space_id,
        run_id=run_id,
        lease_owner="worker_a",
        lease_token=token_a,
    )
    assert late_fail_a is False

    # Worker B's diagnosis and completed claim remain completely intact
    assert mem_store._diagnosis_claims[(space_id, run_id)]["status"] == "completed"
    assert mem_store._diagnosis_claims[(space_id, run_id)]["diagnosis_id"] == "diag_e48_b"
    assert mem_store._diagnosis_claims[(space_id, run_id)]["lease_owner"] == "worker_b"

    # =========================================================================
    # Part 2: FirestoreStore Transactional Fake Verification
    # =========================================================================
    db_runs = {}
    db_diagnoses = {}
    db_claims = {}

    class FakeDocSnap:
        def __init__(self, doc_id, store):
            self.id = doc_id
            self._store = store
        @property
        def exists(self):
            return self.id in self._store
        def to_dict(self):
            return dict(self._store[self.id]) if self.exists else None

    class FakeDocRef:
        def __init__(self, doc_id, store):
            self.id = doc_id
            self._store = store
        def get(self, transaction=None):
            return FakeDocSnap(self.id, self._store)
        def set(self, data, transaction=None):
            self._store[self.id] = dict(data)
        def update(self, data, transaction=None):
            if self.id not in self._store:
                raise Exception("Document not found")
            self._store[self.id].update(data)

    class FakeClient:
        def collection(self, name):
            class FakeCol:
                def __init__(self, data_map):
                    self._map = data_map
                def document(self, doc_id):
                    return FakeDocRef(doc_id, self._map)
            if name == "runs":
                return FakeCol(db_runs)
            elif name == "diagnoses":
                return FakeCol(db_diagnoses)
            elif name == "diagnosis_claims":
                return FakeCol(db_claims)
            return FakeCol({})

        def transaction(self):
            class FakeTx(MagicMock):
                def __init__(self, **kwargs):
                    super().__init__(**kwargs)
                    self._read_only = False
                    self._id = b"fake_tx_id"
                def set(self, ref, data):
                    ref.set(data)
                def update(self, ref, data):
                    ref.update(data)
            return FakeTx()

    mock_firestore_mod = MagicMock()
    mock_firestore_mod.transactional = lambda fn: (lambda tx, *args, **kwargs: fn(tx, *args, **kwargs))

    with patch.dict(sys.modules, {"google.cloud.firestore": mock_firestore_mod}), patch("google.cloud.firestore.transactional", lambda fn: fn):
        fake_fs_client = FakeClient()
        fs_store = FirestoreStore(project_id="test-proj", client=fake_fs_client)

        # Seed run
        fs_run = Run(
            run_id=run_id,
            space_id=space_id,
            status=RunStatus.FAILED,
            prompt="Gate E48 prompt",
            created_by="usr_e48",
            trace_id=trace_id,
            telemetry_generation=1,
            diagnosis_revision=0,
        )
        db_runs[run_id] = fs_run.model_dump(mode="json")

        # 1. Worker A claims in Firestore
        c_a_ok, c_a_rec, _ = fs_store.claim_run_diagnosis(
            space_id=space_id,
            run_id=run_id,
            expected_generation=1,
            lease_owner="worker_a",
            lease_duration_sec=10.0,
            expected_trace_id=trace_id,
        )
        assert c_a_ok is True
        fs_token_a = c_a_rec.lease_token

        # 2. Expire Worker A's lease
        db_claims[f"{space_id}_{run_id}"]["lease_until"] = (
            datetime.now(UTC) - timedelta(seconds=1)
        ).isoformat()

        # 3. Worker B claims in Firestore
        c_b_ok, c_b_rec, _ = fs_store.claim_run_diagnosis(
            space_id=space_id,
            run_id=run_id,
            expected_generation=1,
            lease_owner="worker_b",
            lease_duration_sec=10.0,
            expected_trace_id=trace_id,
        )
        assert c_b_ok is True
        fs_token_b = c_b_rec.lease_token
        assert fs_token_b != fs_token_a

        # 4. Stale Worker A attempts atomic commit -> Rejected
        fs_ok_a = fs_store.atomic_commit_diagnosis_with_claim(
            space_id=space_id,
            run_id=run_id,
            diagnosis=diag_a,
            expected_generation=1,
            lease_owner="worker_a",
            lease_token=fs_token_a,
            expected_trace_id=trace_id,
            expected_diagnosis_revision=0,
        )
        assert fs_ok_a is False
        assert db_runs[run_id].get("diagnosis_revision", 0) == 0
        assert "diag_e48_a" not in db_diagnoses

        # 5. Worker B commits -> Succeeds
        fs_ok_b = fs_store.atomic_commit_diagnosis_with_claim(
            space_id=space_id,
            run_id=run_id,
            diagnosis=diag_b,
            expected_generation=1,
            lease_owner="worker_b",
            lease_token=fs_token_b,
            expected_trace_id=trace_id,
            expected_diagnosis_revision=0,
        )
        assert fs_ok_b is True
        assert db_runs[run_id].get("diagnosis_revision") == 1
        assert db_runs[run_id].get("latest_diagnosis_id") == "diag_e48_b"
        assert db_claims[f"{space_id}_{run_id}"]["status"] == "completed"
        assert db_claims[f"{space_id}_{run_id}"]["diagnosis_id"] == "diag_e48_b"
        assert db_claims[f"{space_id}_{run_id}"]["lease_owner"] == "worker_b"

        # 6. Worker A late cleanup in Firestore -> Rejected
        fs_late_a = fs_store.complete_run_diagnosis_claim(
            space_id=space_id,
            run_id=run_id,
            lease_owner="worker_a",
            lease_token=fs_token_a,
            status="completed",
        )
        assert fs_late_a is False
        fs_fail_a = fs_store.fail_run_diagnosis_claim(
            space_id=space_id,
            run_id=run_id,
            lease_owner="worker_a",
            lease_token=fs_token_a,
        )
        assert fs_fail_a is False
        assert db_claims[f"{space_id}_{run_id}"]["status"] == "completed"
        assert db_claims[f"{space_id}_{run_id}"]["diagnosis_id"] == "diag_e48_b"

    # =========================================================================
    # Part 3: DiagnosisService Integration with Preemption
    # =========================================================================
    # Set global store to fresh memory store
    svc_store = MemoryStore()
    svc_run = Run(
        run_id="run_e48_svc",
        space_id=space_id,
        status=RunStatus.FAILED,
        prompt="Service test",
        created_by="usr_e48",
        trace_id=trace_id,
        telemetry_generation=1,
        diagnosis_revision=0,
    )
    svc_store.save_run(svc_run)
    monkeypatch.setattr("app.services.diagnosis_service.store", svc_store)

    # Worker 1 claims
    _, cl_1, _ = svc_store.claim_run_diagnosis(
        space_id=space_id,
        run_id="run_e48_svc",
        expected_generation=1,
        lease_owner="worker_1",
        lease_duration_sec=10.0,
        expected_trace_id=trace_id,
    )
    # Expire Worker 1
    svc_store._diagnosis_claims[(space_id, "run_e48_svc")]["lease_until"] = (
        datetime.now(UTC) - timedelta(seconds=1)
    ).isoformat()

    # Worker 2 preempts
    svc_store.claim_run_diagnosis(
        space_id=space_id,
        run_id="run_e48_svc",
        expected_generation=1,
        lease_owner="worker_2",
        lease_duration_sec=10.0,
        expected_trace_id=trace_id,
    )

    # When Worker 1 tries to commit via _commit_diagnosis, it must raise HTTPException(409)
    with pytest.raises(HTTPException) as conflict_exc:
        DiagnosisService._commit_diagnosis(
            space_id=space_id,
            run=svc_run,
            record=diag_a,
            lease_owner="worker_1",
            lease_token=cl_1.lease_token,
        )
    assert conflict_exc.value.status_code == 409
    assert "CAS conflict: stale lease" in conflict_exc.value.detail

    # Run in svc_store is untouched
    final_run = svc_store.get_run("run_e48_svc")
    assert final_run.diagnosis_revision == 0
    assert final_run.latest_diagnosis_id is None








