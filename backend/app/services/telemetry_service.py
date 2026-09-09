import json
import logging
import threading
import time
import urllib.error
import urllib.request
import uuid
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel

from app.core.config import settings
from app.core.otel import get_in_memory_spans, is_telemetry_available
from app.core.telemetry_sanitizer import sanitize_span_attributes
from app.core.telemetry_tenant import compute_tenant_hash, verify_tenant_hash
from app.models.run import Run, RunStatus
from app.models.telemetry import (
    SPAN_ALLOWED_ATTRIBUTES,
    RunTraceResponse,
    SpanWaterfallNode,
    TelemetryErrorCode,
    TelemetryMetricsSummary,
    TelemetryStatus,
    get_safe_error_summary,
    normalize_failure_code,
)
from app.services.storage import store

logger = logging.getLogger("studiotower.telemetry_service")


def calculate_histogram_percentiles(
    histogram: Dict[str, int],
    total_samples: int,
) -> Tuple[float, float, bool]:
    """
    Computes approximate P50 and P95 latency percentiles using linear interpolation
    across fixed histogram buckets.
    For samples in the +Inf bucket, values are capped at the highest finite bucket upper bound (120,000ms)
    and flagged with is_capped=True.
    Returns (p50_ms, p95_ms, latency_percentile_capped).
    """
    if total_samples <= 0 or not histogram:
        return 0.0, 0.0, False

    bucket_ranges = [
        ("le_25", 0.0, 25.0),
        ("le_50", 25.0, 50.0),
        ("le_100", 50.0, 100.0),
        ("le_250", 100.0, 250.0),
        ("le_500", 250.0, 500.0),
        ("le_1000", 500.0, 1000.0),
        ("le_2500", 1000.0, 2500.0),
        ("le_5000", 2500.0, 5000.0),
        ("le_10000", 5000.0, 10000.0),
        ("le_30000", 10000.0, 30000.0),
        ("le_60000", 30000.0, 60000.0),
        ("le_120000", 60000.0, 120000.0),
    ]

    highest_bound = float(bucket_ranges[-1][2])

    def _interpolate(rank: float) -> Tuple[float, bool]:
        accum = 0
        for key, lower, upper in bucket_ranges:
            count = histogram.get(key, 0)
            if count <= 0:
                continue
            prev_accum = accum
            accum += count
            if accum >= rank:
                fraction = (rank - prev_accum) / float(count) if count > 0 else 0.0
                return round(lower + fraction * (upper - lower), 2), False
        # If rank exceeds finite buckets, it lands in le_inf
        inf_count = histogram.get("le_inf", 0)
        return highest_bound, (inf_count > 0)

    p50, cap50 = _interpolate(total_samples * 0.50)
    p95, cap95 = _interpolate(total_samples * 0.95)
    return p50, p95, (cap50 or cap95)


class TracingBackendError(Exception):
    """Raised when the tracing backend returns a server error or connection failure."""
    pass


class TracingBackendClient(ABC):
    """Abstract client for querying tracing backends (Tempo, OTLP, InMemory)."""

    @abstractmethod
    def get_trace(self, trace_id: str) -> Optional[Dict[str, Any]]:
        """
        Fetch trace by trace_id.
        Returns:
            - Dict representing trace with list of spans/attributes if found.
            - None if trace does not exist (e.g. HTTP 404).
        Raises:
            - TracingBackendError on network/server failures.
        """
        pass


class TempoTracingClient(TracingBackendClient):
    """Queries external Tempo / OTLP HTTP trace endpoint."""

    def __init__(self, endpoint: Optional[str] = None, timeout_seconds: Optional[float] = None):
        self.endpoint = (endpoint or settings.TEMPO_QUERY_ENDPOINT or "").rstrip("/")
        self.timeout = timeout_seconds or settings.TEMPO_QUERY_TIMEOUT_SECONDS

    def get_trace(self, trace_id: str) -> Optional[Dict[str, Any]]:
        if not self.endpoint:
            return None

        url = f"{self.endpoint}/api/traces/{trace_id}"
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "StudioTower-TelemetryReconciler/1.0"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    return data
                return None
        except urllib.error.HTTPError as e:
            if e.code == 404:
                # Trace not yet ingested / not found
                return None
            logger.warning("Tempo query returned HTTP %d for trace %s: %s", e.code, trace_id, e.reason)
            raise TracingBackendError(f"Tempo query HTTP {e.code}: {e.reason}") from e
        except urllib.error.URLError as e:
            logger.warning("Tempo connection failed for trace %s: %s", trace_id, e.reason)
            raise TracingBackendError(f"Tempo connection failed: {e.reason}") from e
        except Exception as e:
            logger.warning("Tempo unexpected error for trace %s: %s", trace_id, e)
            raise TracingBackendError(f"Tempo unexpected error: {e}") from e


class InMemoryTracingClient(TracingBackendClient):
    """Queries authentic in-memory spans captured via OpenTelemetry in test / local development environments."""

    def get_trace(self, trace_id: str) -> Optional[Dict[str, Any]]:
        clean_tid = trace_id.lower().replace("trc_", "")
        spans = get_in_memory_spans()
        matching_spans = []

        for s in spans:
            s_tid = f"{s.context.trace_id:032x}"
            if s_tid == clean_tid or s_tid.startswith(clean_tid) or clean_tid.startswith(s_tid):
                matching_spans.append({
                    "name": s.name,
                    "span_id": f"{s.context.span_id:016x}",
                    "trace_id": s_tid,
                    "attributes": dict(s.attributes or {}),
                    "parent_span_id": f"{s.parent.span_id:016x}" if s.parent else None,
                })

        if not matching_spans:
            return None

        return {
            "trace_id": clean_tid,
            "spans": matching_spans,
        }


class MockTracingClient(TracingBackendClient):
    """Deterministic mock client for unit and contract testing."""

    def __init__(self, traces: Optional[Dict[str, Any]] = None, fail_trace_ids: Optional[set] = None):
        self.traces = traces or {}
        self.fail_trace_ids = fail_trace_ids or set()
        self.call_count: int = 0

    def get_trace(self, trace_id: str) -> Optional[Dict[str, Any]]:
        self.call_count += 1
        if trace_id in self.fail_trace_ids:
            raise TracingBackendError(f"Simulated network timeout for {trace_id}")
        return self.traces.get(trace_id)


def get_default_tracing_client() -> TracingBackendClient:
    """Returns TempoTracingClient if endpoint configured; falls back to InMemoryTracingClient."""
    if settings.TEMPO_QUERY_ENDPOINT:
        return TempoTracingClient()
    return InMemoryTracingClient()


def _normalize_trace_id(tid: Optional[str]) -> str:
    if not tid:
        return ""
    return tid.lower().replace("trc_", "")


def verify_trace_contract(
    trace_data: Optional[Dict[str, Any]],
    space_id: str,
    run_id: str,
    expected_trace_id: Optional[str] = None,
) -> bool:
    """
    E2 Requirement 1: Only set to 'available' after verifying the real trace exists
    and matches both the tenant hash and Run ID.
    - Expected trace ID: required and must match verifiable trace evidence in trace_data.
    - Tenant boundary: All spans with space_id_hash must match authentic HMAC hash (including rotated keys).
    - Run boundary: All spans with run_id must match expected run_id.
    - Joint execution node satisfaction: At least one authentic execution node must
      simultaneously satisfy Trace ID, tenant hash, and Run ID contracts.
    """
    if not trace_data or not isinstance(trace_data, dict):
        return False

    if not expected_trace_id or not expected_trace_id.strip():
        logger.warning("Trace contract verification rejected: expected_trace_id is required")
        return False

    clean_expected_tid = _normalize_trace_id(expected_trace_id)
    if not clean_expected_tid:
        return False

    # Check top-level trace_id evidence
    top_tid = trace_data.get("trace_id") or trace_data.get("traceId")
    top_tid_clean = _normalize_trace_id(top_tid)
    if top_tid and top_tid_clean != clean_expected_tid:
        logger.warning(
            "Trace ID mismatch: top-level has %s, expected %s",
            top_tid,
            expected_trace_id,
        )
        return False

    # Extract spans across multiple OTLP / Tempo formats
    spans: List[Dict[str, Any]] = []
    if "spans" in trace_data and isinstance(trace_data["spans"], list):
        for s in trace_data["spans"]:
            attrs = s.get("attributes") or {}
            if isinstance(attrs, list):
                flattened = {}
                for a in attrs:
                    k = a.get("key")
                    v = a.get("value", {})
                    if isinstance(v, dict):
                        flattened[k] = v.get("stringValue") or v.get("intValue") or v.get("boolValue")
                    else:
                        flattened[k] = v
                attrs = flattened
            spans.append({
                "name": s.get("name"),
                "span_id": s.get("span_id") or s.get("spanId"),
                "trace_id": s.get("trace_id") or s.get("traceId"),
                "attributes": attrs,
            })
    elif "batches" in trace_data or "resourceSpans" in trace_data:
        batches = trace_data.get("batches") or trace_data.get("resourceSpans") or []
        for b in batches:
            scope_spans = b.get("scopeSpans") or []
            for ss in scope_spans:
                for s in ss.get("spans", []):
                    attrs = {}
                    raw_attrs = s.get("attributes", [])
                    if isinstance(raw_attrs, list):
                        for a in raw_attrs:
                            k = a.get("key")
                            v = a.get("value", {})
                            if isinstance(v, dict):
                                attrs[k] = v.get("stringValue") or v.get("intValue") or v.get("boolValue")
                            else:
                                attrs[k] = v
                    elif isinstance(raw_attrs, dict):
                        attrs = raw_attrs
                    spans.append({
                        "name": s.get("name"),
                        "span_id": s.get("spanId") or s.get("span_id"),
                        "trace_id": s.get("traceId") or s.get("trace_id"),
                        "attributes": attrs,
                    })

    if not spans:
        return False

    # Verifiable Trace ID evidence: either top_level matches clean_expected_tid
    # or at least one span explicitly matches clean_expected_tid
    has_verifiable_trace_evidence = (top_tid_clean == clean_expected_tid) or any(
        _normalize_trace_id(s.get("trace_id")) == clean_expected_tid for s in spans if s.get("trace_id")
    )
    if not has_verifiable_trace_evidence:
        logger.warning(
            "Trace contract verification rejected: no verifiable trace ID evidence matching %s",
            expected_trace_id,
        )
        return False

    valid_execution_names = {
        "action_execution",
        "action_dispatch",
        "pipeline_stage",
        "deliverable_execution",
        "tool_call",
        "model_generation",
    }

    has_authentic_execution_node = False

    for s in spans:
        span_name = s.get("name") or ""
        s_tid = s.get("trace_id")
        s_tid_clean = _normalize_trace_id(s_tid)

        # If span has an explicit trace ID, it must match expected_trace_id
        if s_tid and s_tid_clean != clean_expected_tid:
            logger.warning(
                "Span trace ID mismatch: span %s has %s, expected %s",
                span_name,
                s_tid,
                expected_trace_id,
            )
            return False

        attrs = s.get("attributes") or {}
        span_space_hash = attrs.get("space_id_hash") or attrs.get("space.id.hash")
        span_run_id = attrs.get("run_id") or attrs.get("run.id")

        # Multi-tenant boundary check on any span that carries space_id_hash
        if span_space_hash:
            if not verify_tenant_hash(space_id, span_space_hash):
                logger.warning(
                    "Tenant boundary violation: Trace contains span for tenant %s, does not match space %s",
                    span_space_hash,
                    space_id,
                )
                return False

        # Run isolation check on any span that carries run_id
        if span_run_id:
            if span_run_id != run_id:
                logger.warning(
                    "Run isolation violation: Trace contains span for run %s, expected %s",
                    span_run_id,
                    run_id,
                )
                return False

        # Joint Execution Node Satisfaction:
        # A trusted execution node must SIMULTANEOUSLY satisfy:
        # 1. Execution name
        # 2. Verifiable trace ID (span explicit match or inherited from verified top-level)
        # 3. Authentic tenant HMAC match
        # 4. Authentic Run ID match
        is_exec_name = (
            span_name in valid_execution_names
            or any(prefix in span_name for prefix in ("studiotower.", "action.", "pipeline."))
        )
        span_trace_verified = (s_tid_clean == clean_expected_tid) or (not s_tid and top_tid_clean == clean_expected_tid)
        span_tenant_verified = bool(span_space_hash and verify_tenant_hash(space_id, span_space_hash))
        span_run_verified = bool(span_run_id and span_run_id == run_id)

        if is_exec_name and span_trace_verified and span_tenant_verified and span_run_verified:
            has_authentic_execution_node = True

    return has_authentic_execution_node


class TelemetryVerificationResult(BaseModel):
    run_id: str
    space_id: str
    trace_id: str
    previous_status: str
    current_status: str
    telemetry_generation: int
    matched: bool = False
    throttled: bool = False
    stale_rejected: bool = False
    attempts: int = 0


class TelemetryService:
    """Service managing telemetry lifecycle, verification, and multi-tenant reconciliation."""

    @classmethod
    def verify_run_telemetry(
        cls,
        space_id: str,
        run_id: str,
        force: bool = False,
        backend_client: Optional[TracingBackendClient] = None,
    ) -> TelemetryVerificationResult:
        """
        Verify telemetry availability for a specific Run with version-fenced CAS updates,
        anti-entropy tenant checks, rate-limiting, and single-flight lease acquisition.
        """
        client = backend_client or get_default_tracing_client()
        run = store.get_run(run_id)

        if not run or run.space_id != space_id:
            raise ValueError(f"Run {run_id} not found in space {space_id}")

        now = datetime.now(UTC)
        prev_status = getattr(run, "telemetry_status", TelemetryStatus.NOT_INSTRUMENTED.value)
        current_gen = getattr(run, "telemetry_generation", 1) or 1
        attempts = getattr(run, "telemetry_attempts", 0) or 0

        # Idempotent terminal state: already confirmed available
        if prev_status == TelemetryStatus.AVAILABLE.value:
            return TelemetryVerificationResult(
                run_id=run_id,
                space_id=space_id,
                trace_id=run.trace_id,
                previous_status=prev_status,
                current_status=prev_status,
                telemetry_generation=current_gen,
                matched=True,
                attempts=attempts,
            )

        # Non-force check on permanently unavailable
        if prev_status == TelemetryStatus.UNAVAILABLE.value and not force:
            return TelemetryVerificationResult(
                run_id=run_id,
                space_id=space_id,
                trace_id=run.trace_id,
                previous_status=prev_status,
                current_status=prev_status,
                telemetry_generation=current_gen,
                matched=False,
                attempts=attempts,
            )

        # Claim verification lease (Single-flight protection & rate limiting)
        claim_result = store.claim_run_telemetry_verification(
            space_id=space_id,
            run_id=run_id,
            lease_seconds=10.0,
            force=force,
            min_interval_seconds=settings.TELEMETRY_VERIFICATION_THROTTLE_SECONDS,
        )
        if claim_result is None:
            # Another worker is in-flight, or rate-limited by hard floor / throttle interval / next_check_at
            return TelemetryVerificationResult(
                run_id=run_id,
                space_id=space_id,
                trace_id=run.trace_id,
                previous_status=prev_status,
                current_status=prev_status,
                telemetry_generation=current_gen,
                matched=False,
                throttled=True,
                attempts=attempts,
            )

        claimed_run, lease_token = claim_result
        current_gen = getattr(claimed_run, "telemetry_generation", 1) or 1
        attempts = getattr(claimed_run, "telemetry_attempts", 0) or 0

        # If OTel is permanently unconfigured or disabled, transition to unavailable
        if not is_telemetry_available() and not isinstance(client, (InMemoryTracingClient, MockTracingClient)):
            updated = store.update_run_telemetry_status(
                space_id=space_id,
                run_id=run_id,
                expected_trace_id=claimed_run.trace_id,
                new_status=TelemetryStatus.UNAVAILABLE.value,
                expected_generation=current_gen,
                expected_lease_token=lease_token,
            )
            return TelemetryVerificationResult(
                run_id=run_id,
                space_id=space_id,
                trace_id=claimed_run.trace_id,
                previous_status=prev_status,
                current_status=TelemetryStatus.UNAVAILABLE.value if updated else prev_status,
                telemetry_generation=updated.telemetry_generation if updated else current_gen,
                matched=False,
                stale_rejected=updated is None,
                attempts=attempts + 1,
            )

        # Calculate export age from telemetry_export_started_at (or updated_at / created_at)
        export_started = (
            getattr(claimed_run, "telemetry_export_started_at", None)
            or getattr(claimed_run, "updated_at", None)
            or getattr(claimed_run, "created_at", now)
        )
        age_seconds = (now - export_started).total_seconds()

        # Query tracing backend
        trace_data = None
        backend_error = False
        try:
            trace_data = client.get_trace(claimed_run.trace_id)
        except TracingBackendError as e:
            logger.info("Transient tracing backend error during verification of run %s: %s", run_id, e)
            backend_error = True
        except Exception as e:
            logger.error("Unexpected error querying tracing backend for run %s: %s", run_id, e)
            backend_error = True

        # Evaluate contract match & status determination
        is_matched = False
        target_status: str
        next_check: Optional[datetime] = None
        next_attempts = attempts + 1

        if trace_data:
            is_matched = verify_trace_contract(
                trace_data,
                space_id,
                run_id,
                expected_trace_id=claimed_run.trace_id,
            )
            if is_matched:
                target_status = TelemetryStatus.AVAILABLE.value
                next_check = None
            else:
                # Explicit contract mismatch / security boundary violation
                logger.warning(
                    "Trace contract verification failed for run %s (trace %s); marking UNAVAILABLE",
                    run_id,
                    claimed_run.trace_id,
                )
                target_status = TelemetryStatus.UNAVAILABLE.value
                next_check = None
        else:
            # Trace not returned: either 404 (not yet ingested) or backend_error (transient network error)
            if (
                next_attempts >= settings.TELEMETRY_VERIFICATION_MAX_ATTEMPTS
                or age_seconds > settings.TELEMETRY_VERIFICATION_GRACE_PERIOD_SECONDS
            ):
                target_status = TelemetryStatus.UNAVAILABLE.value
                next_check = None
            else:
                target_status = TelemetryStatus.DELAYED.value
                backoff_seconds = min(2 ** next_attempts, 15)
                next_check = now + timedelta(seconds=backoff_seconds)

        # Atomic version-fenced and lease-fenced CAS write-back
        updated_run = store.update_run_telemetry_status(
            space_id=space_id,
            run_id=run_id,
            expected_trace_id=claimed_run.trace_id,
            new_status=target_status,
            expected_generation=current_gen,
            expected_lease_token=lease_token,
            next_check_at=next_check,
        )

        if not updated_run:
            logger.warning(
                "Stale telemetry verification rejected for run %s (gen %d, lease %s)",
                run_id,
                current_gen,
                lease_token,
            )
            return TelemetryVerificationResult(
                run_id=run_id,
                space_id=space_id,
                trace_id=claimed_run.trace_id,
                previous_status=prev_status,
                current_status=prev_status,
                telemetry_generation=current_gen,
                matched=is_matched,
                stale_rejected=True,
                attempts=attempts,
            )

        return TelemetryVerificationResult(
            run_id=run_id,
            space_id=space_id,
            trace_id=claimed_run.trace_id,
            previous_status=prev_status,
            current_status=updated_run.telemetry_status,
            telemetry_generation=updated_run.telemetry_generation,
            matched=is_matched,
            attempts=getattr(updated_run, "telemetry_attempts", next_attempts),
        )

    @classmethod
    def reconcile_pending_telemetry_runs(
        cls,
        space_id: Optional[str] = None,
        limit: int = 50,
        backend_client: Optional[TracingBackendClient] = None,
    ) -> Dict[str, int]:
        """
        Scan and reconcile runs in 'exporting' or 'delayed' status across one or all spaces
        using authoritative store queries.
        """
        stats = {
            "checked": 0,
            "available": 0,
            "delayed": 0,
            "unavailable": 0,
            "throttled": 0,
            "stale_rejected": 0,
        }

        # Retrieve pending runs authoritatively from storage
        now = datetime.now(UTC)
        target_runs: List[Run] = store.list_pending_telemetry_runs(
            space_id=space_id,
            limit=limit,
            now=now,
        )

        for r in target_runs:
            stats["checked"] += 1
            res = cls.verify_run_telemetry(
                space_id=r.space_id,
                run_id=r.run_id,
                force=False,
                backend_client=backend_client,
            )
            if res.stale_rejected:
                stats["stale_rejected"] += 1
            elif res.throttled:
                stats["throttled"] += 1
            elif res.current_status == TelemetryStatus.AVAILABLE.value:
                stats["available"] += 1
            elif res.current_status == TelemetryStatus.DELAYED.value:
                stats["delayed"] += 1
            elif res.current_status == TelemetryStatus.UNAVAILABLE.value:
                stats["unavailable"] += 1

        return stats

    _metrics_cache: Dict[Tuple[str, int, Optional[str]], Tuple[float, TelemetryMetricsSummary]] = {}
    _metrics_cache_lock = threading.Lock()

    @classmethod
    def get_run_trace_waterfall(
        cls,
        space_id: str,
        run_id: str,
        backend_client: Optional[TracingBackendClient] = None,
    ) -> List[SpanWaterfallNode]:
        """
        Secure multi-tenant trace query proxy (E3 Requirement 1).
        Verifies tenant boundary and run identity via verify_trace_contract.
        Scrubs all non-allowlisted attributes, applies relative offset timing,
        and constructs a structured hierarchy of SpanWaterfallNode objects.
        Fails closed with empty list if the trace is not found or violates multi-tenancy.
        """
        run = store.get_run(run_id)
        if not run or run.space_id != space_id:
            raise ValueError(f"Run {run_id} not found in space {space_id}")

        if not run.trace_id:
            return []

        client = backend_client or get_default_tracing_client()
        try:
            trace_data = client.get_trace(run.trace_id)
        except Exception as e:
            logger.warning("Error fetching trace %s from backend: %s", run.trace_id, e)
            return []

        if not trace_data:
            return []

        # Validate multi-tenant trace contract
        is_valid = verify_trace_contract(
            trace_data=trace_data,
            space_id=space_id,
            run_id=run_id,
            expected_trace_id=run.trace_id,
        )
        if not is_valid:
            logger.warning(
                "Tenant boundary or trace contract violation for run %s (trace %s) in space %s",
                run_id,
                run.trace_id,
                space_id,
            )
            return []

        # Parse spans from trace_data (supporting standard spans and Tempo batches/resourceSpans)
        raw_spans: List[Dict[str, Any]] = []
        if "spans" in trace_data and isinstance(trace_data["spans"], list):
            raw_spans = trace_data["spans"]
        elif "batches" in trace_data or "resourceSpans" in trace_data:
            batches = trace_data.get("batches") or trace_data.get("resourceSpans") or []
            for b in batches:
                scope_spans = b.get("scopeSpans") or []
                for ss in scope_spans:
                    for s in ss.get("spans", []):
                        raw_spans.append(s)

        if not raw_spans:
            return []

        default_base_time = run.created_at or datetime.now(UTC)

        def _parse_span_time(val, fallback_dt: datetime) -> datetime:
            if val is None:
                return fallback_dt
            if isinstance(val, (int, float)):
                sec = float(val)
                if sec > 1e16:   # Nanoseconds
                    sec /= 1e9
                elif sec > 1e13: # Microseconds
                    sec /= 1e6
                elif sec > 1e10: # Milliseconds
                    sec /= 1e3
                return datetime.fromtimestamp(sec, tz=UTC)
            if isinstance(val, str):
                try:
                    return datetime.fromisoformat(val)
                except Exception:
                    pass
            return fallback_dt

        parsed_items = []
        for s in raw_spans:
            s_name = s.get("name") or "unknown_span"
            s_id = s.get("span_id") or s.get("spanId") or uuid.uuid4().hex[:16]
            parent_id = s.get("parent_span_id") or s.get("parentSpanId")

            # Extract start and end times
            start_raw = s.get("start_time") or s.get("startTimeUnixNano") or s.get("start_time_iso")
            end_raw = s.get("end_time") or s.get("endTimeUnixNano") or s.get("end_time_iso")
            start_dt = _parse_span_time(start_raw, default_base_time)
            end_dt = _parse_span_time(end_raw, start_dt + timedelta(milliseconds=1))
            if end_dt < start_dt:
                end_dt = start_dt

            # Extract and sanitize attributes
            attrs = s.get("attributes") or {}
            if isinstance(attrs, list):
                flat = {}
                for a in attrs:
                    k = a.get("key")
                    v = a.get("value", {})
                    if isinstance(v, dict):
                        flat[k] = v.get("stringValue") or v.get("intValue") or v.get("boolValue")
                    else:
                        flat[k] = v
                attrs = flat

            clean_attrs = sanitize_span_attributes(attrs)

            # Determine status & error code
            span_status = "ok"
            s_stat_obj = s.get("status")
            if isinstance(s_stat_obj, dict):
                code_str = str(s_stat_obj.get("code") or s_stat_obj.get("statusCode") or "")
                if "error" in code_str.lower() or code_str == "2":
                    span_status = "error"
            elif isinstance(s_stat_obj, str) and "error" in s_stat_obj.lower():
                span_status = "error"

            err_code = clean_attrs.get("error_code")
            if err_code:
                span_status = "error"

            # Strictly resolve safe error summary from normalized error_code
            # Raw error_message from upstream spans is dropped to prevent secret/path/email leaks
            safe_error_msg = None
            if err_code or span_status == "error":
                safe_error_msg = get_safe_error_summary(err_code)

            parsed_items.append({
                "span_id": str(s_id)[:64],
                "parent_span_id": str(parent_id)[:64] if parent_id else None,
                "name": str(s_name)[:128],
                "service_name": str(clean_attrs.get("service_name") or "studiotower-api")[:64],
                "start_dt": start_dt,
                "end_dt": end_dt,
                "status": span_status,
                "attributes": clean_attrs,
                "error_code": err_code,
                "error_message": safe_error_msg,
            })

        if not parsed_items:
            return []

        # Root timestamp is earliest start_dt
        root_start = min(item["start_dt"] for item in parsed_items)

        nodes: List[SpanWaterfallNode] = []
        for item in parsed_items:
            offset_ms = max(0, int((item["start_dt"] - root_start).total_seconds() * 1000))
            duration_ms = max(0, int((item["end_dt"] - item["start_dt"]).total_seconds() * 1000))

            nodes.append(SpanWaterfallNode(
                span_id=item["span_id"],
                parent_span_id=item["parent_span_id"],
                name=item["name"],
                service_name=item["service_name"],
                start_time_iso=item["start_dt"].isoformat(),
                end_time_iso=item["end_dt"].isoformat(),
                duration_ms=duration_ms,
                offset_ms=offset_ms,
                status=item["status"],
                attributes=item["attributes"],
                error_code=item["error_code"],
                error_message=item["error_message"],
            ))

        nodes.sort(key=lambda n: (n.offset_ms, n.duration_ms))
        return nodes

    @classmethod
    def get_run_trace_bundle(
        cls,
        space_id: str,
        run_id: str,
        backend_client: Optional[TracingBackendClient] = None,
    ) -> Optional[RunTraceResponse]:
        """
        Unified multi-tenant trace bundle query proxy.
        Returns a structured RunTraceResponse containing sanitized waterfall spans,
        or None if trace does not exist or fails security/tenant checks.
        Strictly rejects simulated in-memory mock traces and fails closed.
        """
        run = store.get_run(run_id)
        if not run or run.space_id != space_id:
            raise ValueError(f"Run {run_id} not found in space {space_id}")

        if not run.trace_id:
            return None

        # Only query authentic OTLP/Tempo telemetry from backend client
        nodes = cls.get_run_trace_waterfall(space_id=space_id, run_id=run_id, backend_client=backend_client)
        if not nodes:
            return None

        # Strict E4 Prerequisite for Grafana deep links:
        # 1. Authentic spans exist (len(nodes) > 0)
        # 2. run.telemetry_status == "available" (not delayed, exporting, not_instrumented, or unavailable)
        # 3. trace_id not in simulated_trace_ids
        from app.integrations.grafana_mcp import grafana_mcp
        from app.core.grafana_links import build_grafana_dashboard_url

        dash_url = None
        is_real_telemetry = (run.telemetry_status == TelemetryStatus.AVAILABLE.value)
        is_not_simulated = run.trace_id not in getattr(grafana_mcp, "_simulated_trace_ids", set())

        if len(nodes) > 0 and is_real_telemetry and is_not_simulated:
            dash_url = build_grafana_dashboard_url(
                trace_id=run.trace_id,
                run_id=run.run_id,
                space_id=space_id,
                start_time=run.created_at,
                end_time=run.updated_at or run.created_at,
            )

        return RunTraceResponse(
            trace_id=run.trace_id,
            run_id=run.run_id,
            space_id=space_id,
            service_name="studiotower-api",
            total_spans=len(nodes),
            spans=nodes,
            grafana_dashboard_url=dash_url,
            has_real_telemetry=is_real_telemetry,
        )

    @classmethod
    def get_space_metrics_summary(
        cls,
        space_id: str,
        time_window_hours: int = 24,
        project_tag: Optional[str] = None,
        bypass_cache: bool = False,
    ) -> TelemetryMetricsSummary:
        """
        Low-cardinality pre-aggregated telemetry metrics rollup (E3 Requirement 2).
        Enforces time boundaries (1 <= time_window_hours <= 168), anti-abuse rate-limiting,
        and strictly avoids full-collection scans by aggregating pre-aggregated hourly buckets.
        """
        if time_window_hours < 1 or time_window_hours > 168:
            raise ValueError("time_window_hours must be between 1 and 168 (up to 7 days)")

        cache_key = (space_id, time_window_hours, project_tag)
        now_ts = time.time()
        if not bypass_cache:
            with cls._metrics_cache_lock:
                cached = cls._metrics_cache.get(cache_key)
                if cached and (now_ts - cached[0]) < 2.0:  # 2.0s cache window to throttle rapid spam
                    return cached[1]

        now = datetime.now(UTC)
        start_time = now - timedelta(hours=time_window_hours)

        # 1. Query pre-aggregated rollups (at most 24 docs for 24h, 168 docs for 7d)
        rollups = store.get_metric_rollups(
            space_id=space_id,
            start_time=start_time,
            end_time=now,
            project_tag=project_tag,
        )

        merged_histogram: Dict[str, int] = {}
        failures_by_code: Dict[str, int] = {}
        total_runs = 0
        completed_runs = 0
        failed_runs = 0
        total_tokens = 0
        estimated_cost: Optional[float] = None

        if not rollups:
            # Bounded contract: ZERO calls to store.list_runs_for_metrics or runs collection!
            # Returns warming_up with sample_count=0 and honest cost status unavailable
            summary = TelemetryMetricsSummary(
                space_id=space_id,
                project_tag=project_tag,
                time_window_hours=time_window_hours,
                rollup_schema_version=2,
                data_status="warming_up",
                cost_data_status="unavailable",
                sample_count=0,
                total_runs=0,
                completed_runs=0,
                failed_runs=0,
                success_rate=0.0,
                latency_percentile_method="histogram",
                latency_p50_ms=0.0,
                latency_p95_ms=0.0,
                latency_percentile_capped=False,
                total_tokens_used=0,
                estimated_cost_usd=None,
                pricing_version=None,
                failures_by_code={},
            )
            if not bypass_cache:
                with cls._metrics_cache_lock:
                    cls._metrics_cache[cache_key] = (now_ts, summary)
            return summary

        # Aggregate across rollups
        cost_sum = 0.0
        total_priced_runs = 0
        total_unpriced_runs = 0
        collected_pricing_versions: List[str] = []
        is_pricing_truncated = False
        sample_count = 0
        for r in rollups:
            total_runs += r.total_runs
            completed_runs += r.completed_runs
            failed_runs += r.failed_runs
            sample_count += r.total_runs
            total_tokens += r.total_tokens
            if r.estimated_cost_usd is not None:
                cost_sum += r.estimated_cost_usd
            total_priced_runs += getattr(r, "priced_run_count", 0)
            total_unpriced_runs += getattr(r, "unpriced_run_count", 0)
            if getattr(r, "pricing_versions_truncated", False):
                is_pricing_truncated = True
            for pv in getattr(r, "pricing_versions", []):
                if pv and pv not in collected_pricing_versions:
                    if len(collected_pricing_versions) < 5:
                        collected_pricing_versions.append(pv)
                    else:
                        is_pricing_truncated = True
            for code, count in r.failures_by_code.items():
                norm_code = normalize_failure_code(code)
                failures_by_code[norm_code] = failures_by_code.get(norm_code, 0) + count
            for b_key, b_count in r.latency_histogram.items():
                merged_histogram[b_key] = merged_histogram.get(b_key, 0) + b_count

        # Slice E Honest Cost contract resolution:
        # None when no priced runs exist; distinct partial vs available status
        if collected_pricing_versions:
            if is_pricing_truncated or len(collected_pricing_versions) > 1:
                pricing_ver = "mixed"
            else:
                pricing_ver = collected_pricing_versions[0]
        else:
            pricing_ver = None

        if total_priced_runs == 0:
            estimated_cost = None
            cost_status = "unavailable"
            pricing_ver = None
        elif total_unpriced_runs > 0:
            estimated_cost = round(cost_sum, 6)
            cost_status = "partial"
        else:
            estimated_cost = round(cost_sum, 6)
            cost_status = "available"

        total_latency_samples = sum(merged_histogram.values())
        p50_ms, p95_ms, is_capped = calculate_histogram_percentiles(merged_histogram, total_latency_samples)
        success_rate = round(completed_runs / total_runs, 4) if total_runs > 0 else 0.0

        summary = TelemetryMetricsSummary(
            space_id=space_id,
            project_tag=project_tag,
            time_window_hours=time_window_hours,
            rollup_schema_version=2,
            data_status="available" if total_runs > 0 else "warming_up",
            cost_data_status=cost_status,
            sample_count=sample_count,
            total_runs=total_runs,
            completed_runs=completed_runs,
            failed_runs=failed_runs,
            success_rate=success_rate,
            latency_percentile_method="histogram",
            latency_p50_ms=p50_ms,
            latency_p95_ms=p95_ms,
            latency_percentile_capped=is_capped,
            total_tokens_used=total_tokens,
            estimated_cost_usd=estimated_cost,
            pricing_version=pricing_ver,
            pricing_versions_truncated=is_pricing_truncated,
            failures_by_code=failures_by_code,
        )

        with cls._metrics_cache_lock:
            cls._metrics_cache[cache_key] = (now_ts, summary)

        return summary
