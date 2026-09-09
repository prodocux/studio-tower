import collections
import concurrent.futures
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, status
from pydantic import ValidationError

from app.core.telemetry_sanitizer import _PROHIBITED_PATTERNS
from app.integrations.grafana_mcp import grafana_mcp
from app.models.run import Run
from app.models.telemetry import (
    ERROR_CODE_SAFE_SUMMARIES,
    SPAN_ALLOWED_ATTRIBUTES,
    DiagnosisClaimRecord,
    DiagnosisClaimStatus,
    DiagnosisConfidenceEnum,
    DiagnosisRecord,
    EvidenceSpanSummary,
    GeminiDiagnosisResponse,
    RunTraceResponse,
    SpanWaterfallNode,
    SuggestedActionEnum,
    TelemetryErrorCode,
    get_safe_error_summary,
    normalize_failure_code,
)
from app.models.user import User
from app.services.storage import StorageConflictError, StorageUnavailableError, store

logger = logging.getLogger("studiotower.diagnosis_service")

# Contract Constants
GEMINI_DIAGNOSIS_TIMEOUT_SECONDS = 3.0
DIAGNOSIS_CACHE_TTL_SECONDS = 600.0  # 10 minutes
DEFAULT_USER_RATE_LIMIT_PER_MINUTE = 10
DEFAULT_SPACE_RATE_LIMIT_PER_MINUTE = 30
RETRY_AFTER_SECONDS = 5

# Shared bounded thread pool and admission semaphore for Gemini diagnosis workers
_DIAGNOSIS_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="diagnosis-worker"
)
_DIAGNOSIS_SEMAPHORE = threading.BoundedSemaphore(8)


def compute_deterministic_diagnosis_id(
    space_id: str,
    run_id: str,
    trace_id: str,
    generation: int,
    schema_ver: int = 1,
) -> str:
    """
    Computes a deterministic diagnosis ID rooted in run identity, trace ID, generation, and schema version.
    Ensures idempotency across workers and storage backends.
    """
    seed = f"{space_id}:{run_id}:{trace_id}:{generation}:v{schema_ver}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"diag_{digest}"


def sanitize_text(text: str) -> str:
    """
    Second-pass regex scrubber for output strings and pre-input string attributes.
    Scrubs API keys, Bearer tokens, JWTs, emails, credentials, and filesystem paths.
    """
    if not text:
        return ""
    sanitized = text
    sanitized = re.sub(r"sk-[a-zA-Z0-9_\-]{16,}", "[REDACTED]", sanitized)
    sanitized = re.sub(r"bearer\s+[a-zA-Z0-9_\-\.]+", "[REDACTED]", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"ey[a-zA-Z0-9_\-]{20,}\.[a-zA-Z0-9_\-]{20,}\.[a-zA-Z0-9_\-]+", "[REDACTED]", sanitized)
    sanitized = re.sub(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", "[REDACTED]", sanitized)
    sanitized = re.sub(
        r"(?:gs://[^\n\s]+|/(?:[a-zA-Z0-9_\-]+/)+[a-zA-Z0-9_\.\-]+|[A-Za-z]:\\[^\n\s]+)",
        "[REDACTED]",
        sanitized,
    )
    return sanitized


def format_diagnosis_markdown(record: DiagnosisRecord, trace_id: str) -> str:
    """
    Format user-facing markdown representation for Run.agent_diagnosis.
    Maintains compatibility with legacy UI expectations while reflecting authentic evidence.
    """
    if record.diagnostic_status == "unavailable" or not record.faulting_span:
        if record.diagnostic_status == "no_failure_evidence":
            msg = (
                f"✅ **Grafana MCP Telemetry Inspection (Trace `{trace_id}`)**\n\n"
                f"• **Status**: `no_failure_evidence`\n"
                f"• All recorded spans completed successfully."
            )
        else:
            msg = (
                f"ℹ️ **No Telemetry Data Available (Trace `{trace_id}`)**\n\n"
                f"No OpenTelemetry spans have been recorded for this production task."
            )
        mcp_line = _format_mcp_markdown_line(record)
        if mcp_line:
            msg += f"\n\n{mcp_line}"
        return msg

    lines = [
        f"🚨 **Grafana MCP Telemetry Diagnosis (Trace `{trace_id}`)**\n",
        f"• **Faulting Span**: `{record.faulting_span.name}` (Span ID: `{record.faulting_span.span_id}`)",
        f"• **Root Cause**: {record.error_summary or record.error_code}",
    ]
    mcp_line = _format_mcp_markdown_line(record)
    if mcp_line:
        lines.append(mcp_line)
    if record.faulting_span.key_attributes:
        lines.append(f"• **Attributes Flagged**: `{json.dumps(record.faulting_span.key_attributes)}`")
    if record.recommendations:
        lines.append("\n💡 **Recommended AI Remediation**:")
        for i, rec in enumerate(record.recommendations, 1):
            lines.append(f"{i}. {rec}")
    return "\n".join(lines)


def _format_mcp_markdown_line(record: DiagnosisRecord) -> str:
    if record.mcp_connected:
        tools = ", ".join(record.mcp_tools_called) or "tools/list"
        return f"• **Grafana Cloud MCP**: connected (`{tools}`)"
    if record.mcp_error:
        return "• **Grafana Cloud MCP**: configured but fail-closed (server unreachable or unauthorized)."
    return ""


def _attach_mcp_evidence(record: DiagnosisRecord, mcp_runtime) -> DiagnosisRecord:
    record.mcp_connected = bool(mcp_runtime.connected)
    record.mcp_tools_called = list(mcp_runtime.tools_called or [])
    record.mcp_error = mcp_runtime.error
    extra: list[str] = []
    if mcp_runtime.connected:
        extra.extend(list(mcp_runtime.summaries or [])[:3])
    elif mcp_runtime.error:
        extra.append("Grafana Cloud MCP connection failed (fail-closed); local telemetry used.")
    if extra:
        record.observations = list(record.observations or []) + extra
    return record


class DiagnosisRateLimiter:
    """
    Dual-layer sliding window rate limiter (user-level and space-level).
    Thread-safe in-memory tracking with controlled 429 Retry-After responses.
    """

    def __init__(
        self,
        user_limit_per_min: int = DEFAULT_USER_RATE_LIMIT_PER_MINUTE,
        space_limit_per_min: int = DEFAULT_SPACE_RATE_LIMIT_PER_MINUTE,
    ):
        self._lock = threading.Lock()
        self.user_limit = user_limit_per_min
        self.space_limit = space_limit_per_min
        self._user_requests: Dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self._space_requests: Dict[str, collections.deque] = collections.defaultdict(collections.deque)

    def reset(self) -> None:
        with self._lock:
            self._user_requests.clear()
            self._space_requests.clear()

    def check_and_record(self, space_id: str, user_id: str) -> None:
        now = time.monotonic()
        cutoff = now - 60.0

        with self._lock:
            user_deque = self._user_requests[user_id]
            while user_deque and user_deque[0] < cutoff:
                user_deque.popleft()

            space_deque = self._space_requests[space_id]
            while space_deque and space_deque[0] < cutoff:
                space_deque.popleft()

            if len(user_deque) >= self.user_limit:
                logger.warning("User rate limit exceeded for user %s in space %s", user_id, space_id)
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="User diagnosis rate limit exceeded. Please wait before retrying.",
                    headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
                )

            if len(space_deque) >= self.space_limit:
                logger.warning("Space rate limit exceeded for space %s by user %s", space_id, user_id)
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Space diagnosis rate limit exceeded. Please wait before retrying.",
                    headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
                )

            user_deque.append(now)
            space_deque.append(now)


class DiagnosisService:
    """
    Authoritative service implementing Phase E5 Evidence-based AI Failure Diagnosis Contract.
    """

    _rate_limiter = DiagnosisRateLimiter()
    _meta_lock = threading.RLock()
    _inflight_locks: Dict[str, threading.Lock] = {}
    _cache: Dict[str, Tuple[DiagnosisRecord, float]] = {}

    _engine_execution_count: int = 0
    _gemini_call_count: int = 0

    @classmethod
    def reset_state(cls) -> None:
        with cls._meta_lock:
            cls._cache.clear()
            cls._inflight_locks.clear()
            cls._engine_execution_count = 0
            cls._gemini_call_count = 0
        cls._rate_limiter.reset()
        if hasattr(store, "_sliding_rate_limits"):
            store._sliding_rate_limits.clear()
        if hasattr(store, "_diagnosis_claims"):
            store._diagnosis_claims.clear()

    @classmethod
    def check_rate_limit(cls, space_id: str, user_id: str) -> None:
        user_lim = getattr(cls._rate_limiter, "user_limit", DEFAULT_USER_RATE_LIMIT_PER_MINUTE)
        space_lim = getattr(cls._rate_limiter, "space_limit", DEFAULT_SPACE_RATE_LIMIT_PER_MINUTE)
        try:
            allowed, retry_after = store.check_and_record_dual_rate_limit(
                space_id=space_id,
                user_id=user_id,
                user_limit=user_lim,
                space_limit=space_lim,
                window_seconds=60.0,
            )
        except StorageUnavailableError as exc:
            logger.error("Rate limiting storage service unavailable: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Rate limiting storage service temporarily unavailable",
            )
        if not allowed:
            logger.warning(
                "Diagnosis rate limit exceeded for user %s in space %s (Retry-After: %s)",
                user_id,
                space_id,
                retry_after,
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Diagnosis rate limit exceeded. Please wait before retrying.",
                headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
            )

    @classmethod
    def _get_from_cache(cls, key: str) -> Optional[DiagnosisRecord]:
        now = time.monotonic()
        with cls._meta_lock:
            entry = cls._cache.get(key)
            if entry:
                record, expiry = entry
                if now < expiry:
                    return record
                cls._cache.pop(key, None)
        return None

    @classmethod
    def _put_in_cache(cls, key: str, record: DiagnosisRecord) -> None:
        with cls._meta_lock:
            cls._cache[key] = (record, time.monotonic() + DIAGNOSIS_CACHE_TTL_SECONDS)

    @classmethod
    def diagnose_run(
        cls,
        space_id: str,
        run_id: str,
        user: Optional[User] = None,
    ) -> DiagnosisRecord:
        run = store.get_run(run_id)
        if not run or run.space_id != space_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found in this Space")

        cache_key = f"{space_id}:{run_id}:{run.trace_id}:{run.telemetry_generation}:v1"
        deterministic_id = compute_deterministic_diagnosis_id(
            space_id=space_id,
            run_id=run_id,
            trace_id=run.trace_id,
            generation=run.telemetry_generation,
            schema_ver=1,
        )

        cached = cls._get_from_cache(cache_key)
        if cached:
            return cached

        # Check if already committed in store
        existing_diag = store.get_diagnosis(space_id, run_id)
        if (
            existing_diag
            and existing_diag.trace_id == run.trace_id
            and existing_diag.telemetry_generation == run.telemetry_generation
        ):
            cls._put_in_cache(cache_key, existing_diag)
            return existing_diag

        # Single-flight & cross-instance lease claim
        worker_id = f"worker_{os.getpid()}_{threading.get_ident()}_{uuid.uuid4().hex[:6]}"
        claimed, claim_record, completed_diag = store.claim_run_diagnosis(
            space_id=space_id,
            run_id=run_id,
            expected_generation=run.telemetry_generation,
            lease_owner=worker_id,
            lease_duration_sec=15.0,
            expected_trace_id=run.trace_id,
            schema_version=1,
        )

        if not claimed:
            if (
                completed_diag
                and completed_diag.telemetry_generation == run.telemetry_generation
                and completed_diag.trace_id == run.trace_id
            ):
                cls._put_in_cache(cache_key, completed_diag)
                return completed_diag

            # Another worker holds active lease. Poll for up to 2.5s for completion
            deadline = time.time() + 2.5
            while time.time() < deadline:
                time.sleep(0.1)
                diag = store.get_diagnosis(space_id, run_id)
                if (
                    diag
                    and diag.trace_id == run.trace_id
                    and diag.telemetry_generation == run.telemetry_generation
                ):
                    cls._put_in_cache(cache_key, diag)
                    return diag

            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Diagnosis already in progress on another worker. Please retry shortly.",
                headers={"Retry-After": "3"},
            )

        # Claim succeeded. Execute pipeline and atomically commit with claim lease
        try:
            record = cls._execute_diagnosis_pipeline(
                space_id=space_id,
                run=run,
                deterministic_id=deterministic_id,
                lease_owner=worker_id,
                lease_token=claim_record.lease_token,
            )
            cls._put_in_cache(cache_key, record)
            return record
        except Exception as exc:
            if hasattr(store, "fail_run_diagnosis_claim"):
                store.fail_run_diagnosis_claim(
                    space_id=space_id,
                    run_id=run_id,
                    lease_owner=worker_id,
                    lease_token=claim_record.lease_token,
                )
            else:
                store.complete_run_diagnosis_claim(
                    space_id=space_id,
                    run_id=run_id,
                    lease_owner=worker_id,
                    lease_token=claim_record.lease_token,
                    status=DiagnosisClaimStatus.FAILED.value,
                )
            raise

    @classmethod
    def diagnose_run_for_run(cls, run: Run, space_id: str) -> DiagnosisRecord:
        """
        Internal entry point for AgentBrain / chat pipelines where Run object is already held.
        Operates directly on the provided Run instance without failing if not yet saved to store.
        """
        cache_key = f"{space_id}:{run.run_id}:{run.trace_id}:{run.telemetry_generation}:v1"
        deterministic_id = compute_deterministic_diagnosis_id(
            space_id=space_id,
            run_id=run.run_id,
            trace_id=run.trace_id,
            generation=run.telemetry_generation,
            schema_ver=1,
        )

        cached = cls._get_from_cache(cache_key)
        if cached:
            run.latest_diagnosis_id = cached.diagnosis_id
            run.latest_diagnosis_status = cached.diagnostic_status
            run.agent_diagnosis = format_diagnosis_markdown(cached, run.trace_id)
            return cached

        with cls._meta_lock:
            cached = cls._get_from_cache(cache_key)
            if cached:
                run.latest_diagnosis_id = cached.diagnosis_id
                run.latest_diagnosis_status = cached.diagnostic_status
                run.agent_diagnosis = format_diagnosis_markdown(cached, run.trace_id)
                return cached
            if cache_key not in cls._inflight_locks:
                cls._inflight_locks[cache_key] = threading.Lock()
            single_flight_lock = cls._inflight_locks[cache_key]

        with single_flight_lock:
            try:
                cached = cls._get_from_cache(cache_key)
                if cached:
                    run.latest_diagnosis_id = cached.diagnosis_id
                    run.latest_diagnosis_status = cached.diagnostic_status
                    run.agent_diagnosis = format_diagnosis_markdown(cached, run.trace_id)
                    return cached

                record = cls._execute_diagnosis_pipeline(
                    space_id=space_id,
                    run=run,
                    deterministic_id=deterministic_id,
                )
                cls._put_in_cache(cache_key, record)
                return record
            finally:
                with cls._meta_lock:
                    cls._inflight_locks.pop(cache_key, None)

    @classmethod
    def _execute_diagnosis_pipeline(
        cls,
        space_id: str,
        run: Run,
        deterministic_id: Optional[str] = None,
        lease_owner: Optional[str] = None,
        lease_token: Optional[str] = None,
    ) -> DiagnosisRecord:
        with cls._meta_lock:
            cls._engine_execution_count += 1

        trace_bundle: RunTraceResponse = grafana_mcp.query_trace(
            trace_id=run.trace_id,
            run_id=run.run_id,
            space_id=space_id,
        )
        mcp_runtime = grafana_mcp.query_runtime_observability(
            run_id=run.run_id,
            trace_id=run.trace_id,
            space_id=space_id,
        )

        is_simulated = run.trace_id in grafana_mcp._simulated_trace_ids
        is_local = getattr(trace_bundle, "is_local_diagnostic", False) or is_simulated
        diag_id = deterministic_id or compute_deterministic_diagnosis_id(
            space_id=space_id,
            run_id=run.run_id,
            trace_id=run.trace_id,
            generation=run.telemetry_generation,
            schema_ver=1,
        )

        if trace_bundle.total_spans == 0:
            record = DiagnosisRecord(
                diagnosis_id=diag_id,
                space_id=space_id,
                run_id=run.run_id,
                trace_id=run.trace_id,
                telemetry_generation=run.telemetry_generation,
                schema_version=1,
                engine="rule_based",
                diagnostic_status="unavailable",
                error_summary="No telemetry data recorded for this run",
                confidence="low",
                observations=["No OpenTelemetry spans recorded for this production task."],
                likely_causes=[],
                recommendations=[],
                evidence_span_ids=[],
                has_real_telemetry=trace_bundle.has_real_telemetry,
                grafana_dashboard_url=trace_bundle.grafana_dashboard_url,
                is_local_diagnostic=is_local,
            )
            cls._commit_diagnosis(
                space_id,
                run,
                _attach_mcp_evidence(record, mcp_runtime),
                lease_owner=lease_owner,
                lease_token=lease_token,
            )
            return record

        error_spans = [s for s in trace_bundle.spans if s.status == "error"]

        if len(error_spans) == 0:
            record = DiagnosisRecord(
                diagnosis_id=diag_id,
                space_id=space_id,
                run_id=run.run_id,
                trace_id=run.trace_id,
                telemetry_generation=run.telemetry_generation,
                schema_version=1,
                engine="rule_based",
                diagnostic_status="no_failure_evidence",
                error_summary="",
                confidence="high",
                observations=[f"All {trace_bundle.total_spans} recorded spans completed successfully with status OK."],
                likely_causes=[],
                recommendations=[],
                evidence_span_ids=[],
                has_real_telemetry=trace_bundle.has_real_telemetry,
                grafana_dashboard_url=trace_bundle.grafana_dashboard_url,
                is_local_diagnostic=is_local,
            )
            cls._commit_diagnosis(
                space_id,
                run,
                _attach_mcp_evidence(record, mcp_runtime),
                lease_owner=lease_owner,
                lease_token=lease_token,
            )
            return record

        can_use_ai = (
            trace_bundle.has_real_telemetry is True
            and getattr(run, "telemetry_status", "not_instrumented") == "available"
            and not is_local
            and not is_simulated
        )

        record: Optional[DiagnosisRecord] = None

        if can_use_ai:
            record = cls._try_gemini_diagnosis(
                space_id=space_id,
                run=run,
                trace_bundle=trace_bundle,
                error_spans=error_spans,
                deterministic_id=diag_id,
            )

        if record is None:
            is_fallback = can_use_ai
            record = cls._execute_rule_based_diagnosis(
                space_id=space_id,
                run=run,
                trace_bundle=trace_bundle,
                error_spans=error_spans,
                is_fallback=is_fallback,
                is_local=is_local,
                deterministic_id=diag_id,
            )

        cls._commit_diagnosis(
            space_id,
            run,
            _attach_mcp_evidence(record, mcp_runtime),
            lease_owner=lease_owner,
            lease_token=lease_token,
        )
        return record

    @classmethod
    def _execute_rule_based_diagnosis(
        cls,
        space_id: str,
        run: Run,
        trace_bundle: RunTraceResponse,
        error_spans: List[SpanWaterfallNode],
        is_fallback: bool,
        is_local: bool,
        deterministic_id: Optional[str] = None,
    ) -> DiagnosisRecord:
        faulting = error_spans[0]
        code = normalize_failure_code(
            faulting.error_code
            or faulting.attributes.get("gate.rule_id")
            or faulting.attributes.get("error_code")
            or run.failure_code
        )
        summary = get_safe_error_summary(code) or "Resource contention exception"

        key_attrs: Dict[str, Any] = {}
        for k, v in faulting.attributes.items():
            if k in SPAN_ALLOWED_ATTRIBUTES:
                key_attrs[k] = sanitize_text(str(v)) if isinstance(v, str) else v
            elif is_local:
                key_attrs[k] = sanitize_text(str(v)) if isinstance(v, str) else v

        faulting_summary = EvidenceSpanSummary(
            span_id=faulting.span_id,
            name=faulting.name[:64],
            duration_ms=faulting.duration_ms,
            status=faulting.status,
            error_code=code,
            key_attributes=key_attrs,
        )

        observations = [f"Span '{faulting.name[:64]}' failed with status 'error'."]
        likely_causes = [summary]
        evidence_span_ids = [s.span_id for s in error_spans]

        if code == TelemetryErrorCode.EXCLUSIVE_TECH_LOCK_VIOLATION.value:
            recommendations = [
                "Resolve resource contention between overlapping units.",
                "Add turnaround buffer for shared cast and technical assets on the call sheet.",
                f"Re-trigger schedule compilation under track '#{run.project_tag}'.",
            ]
            suggested_action = "reconfigure"
            is_retryable = True
            confidence = "high"
        elif code in (TelemetryErrorCode.STORAGE_COMMIT_TIMEOUT.value, TelemetryErrorCode.AI_REASONING_TIMEOUT.value):
            recommendations = [
                "Increase operation timeout budget.",
                "Retry execution with exponential backoff.",
            ]
            suggested_action = "wait_and_retry"
            is_retryable = True
            confidence = "high"
        elif code == TelemetryErrorCode.APPROVAL_GATE_REJECTED.value:
            recommendations = [
                "Review gate rejection feedback.",
                "Update execution parameters and request re-approval.",
            ]
            suggested_action = "reconfigure"
            is_retryable = False
            confidence = "high"
        elif code == TelemetryErrorCode.TENANT_ACCESS_DENIED.value:
            recommendations = [
                "Verify Space tenant boundary and caller permissions.",
            ]
            suggested_action = "contact_support"
            is_retryable = False
            confidence = "high"
        else:
            recommendations = [
                "Inspect trace logs and telemetry spans for unexpected exceptions.",
                "Retry operation if failure was transient.",
            ]
            suggested_action = "investigate"
            is_retryable = False
            confidence = "medium"

        diag_id = deterministic_id or compute_deterministic_diagnosis_id(
            space_id, run.run_id, run.trace_id, run.telemetry_generation, 1
        )

        return DiagnosisRecord(
            diagnosis_id=diag_id,
            space_id=space_id,
            run_id=run.run_id,
            trace_id=run.trace_id,
            telemetry_generation=run.telemetry_generation,
            schema_version=1,
            engine="rule_based",
            diagnostic_status="fallback" if is_fallback else "complete",
            faulting_span=faulting_summary,
            error_code=code,
            error_summary=summary,
            observations=observations,
            likely_causes=likely_causes,
            recommendations=recommendations,
            evidence_span_ids=evidence_span_ids,
            is_retryable=is_retryable,
            suggested_action=suggested_action,
            confidence=confidence,
            grafana_dashboard_url=trace_bundle.grafana_dashboard_url,
            has_real_telemetry=trace_bundle.has_real_telemetry,
            is_local_diagnostic=is_local,
        )

    @classmethod
    def _try_gemini_diagnosis(
        cls,
        space_id: str,
        run: Run,
        trace_bundle: RunTraceResponse,
        error_spans: List[SpanWaterfallNode],
        deterministic_id: Optional[str] = None,
    ) -> Optional[DiagnosisRecord]:
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            logger.info("Gemini API key not configured; falling back to rule-based engine")
            return None

        normalized_error_spans: List[Dict[str, Any]] = []
        for s in error_spans:
            s_attrs = getattr(s, "attributes", {}) or {}
            norm_code = normalize_failure_code(
                getattr(s, "error_code", None)
                or s_attrs.get("gate.rule_id")
                or s_attrs.get("error_code")
                or run.failure_code
            )
            filtered_attrs: Dict[str, Any] = {}
            for k, v in s_attrs.items():
                if k in SPAN_ALLOWED_ATTRIBUTES:
                    filtered_attrs[k] = sanitize_text(str(v)[:64]) if isinstance(v, str) else v

            normalized_error_spans.append(
                {
                    "span_id": s.span_id,
                    "name": s.name[:64],
                    "duration_ms": s.duration_ms,
                    "status": s.status,
                    "error_code": norm_code,
                    "safe_summary": get_safe_error_summary(norm_code),
                    "key_attributes": filtered_attrs,
                }
            )

        prompt_payload = {
            "run_id": run.run_id,
            "project_tag": run.project_tag,
            "total_spans": trace_bundle.total_spans,
            "error_spans": normalized_error_spans,
            "instruction": (
                "Analyze the provided execution failure spans. Identify the root cause and provide remediations. "
                "You MUST only cite span_ids that exist in the error_spans list. "
                "Output strictly valid JSON conforming to the requested schema."
            ),
        }

        with cls._meta_lock:
            cls._gemini_call_count += 1

        # Non-blocking admission semaphore: strictly bounds concurrent workers to 8
        if not _DIAGNOSIS_SEMAPHORE.acquire(blocking=False):
            logger.warning("Diagnosis admission limit reached (8 active workers). Failing over to rule-based engine.")
            return None

        try:
            future = _DIAGNOSIS_EXECUTOR.submit(cls._call_gemini_api, prompt_payload, api_key)
        except Exception as exc:
            # ONLY release manually if executor submission itself failed
            _DIAGNOSIS_SEMAPHORE.release()
            logger.warning("Failed to submit diagnosis task to executor: %s", exc)
            return None

        # Once submitted successfully, permit release is exclusively managed by done callback
        future.add_done_callback(lambda _: _DIAGNOSIS_SEMAPHORE.release())

        raw_response = None
        try:
            raw_response = future.result(timeout=GEMINI_DIAGNOSIS_TIMEOUT_SECONDS)
        except (concurrent.futures.TimeoutError, TimeoutError):
            future.cancel()
            logger.warning("Gemini diagnosis timed out after %ss limit", GEMINI_DIAGNOSIS_TIMEOUT_SECONDS)
            return None
        except Exception as exc:
            logger.warning("Gemini diagnosis API call raised exception: %s", exc)
            return None

        if not raw_response:
            return None

        # Gate E42: Strict Pydantic parsing with extra="forbid" and Enum validation
        try:
            gemini_resp = GeminiDiagnosisResponse.model_validate_json(raw_response)
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Gemini response schema validation failed: %s. Failing back to rule-based.", exc)
            return None

        # Gate E42: Error-span isolation (rejects OK spans, non-existent spans)
        error_span_map = {s.span_id: s for s in error_spans if s.status == "error"}

        if gemini_resp.faulting_span_id not in error_span_map:
            logger.warning(
                "Gemini cited faulting_span_id '%s' which is not an error span. Rejecting.",
                gemini_resp.faulting_span_id,
            )
            return None

        for cited_id in gemini_resp.evidence_span_ids:
            if cited_id not in error_span_map:
                logger.warning(
                    "Gemini cited hallucinated or non-error evidence span_id '%s'. Rejecting.",
                    cited_id,
                )
                return None

        primary_span = error_span_map[gemini_resp.faulting_span_id]
        primary_attrs = getattr(primary_span, "attributes", {}) or {}
        primary_code = normalize_failure_code(
            gemini_resp.error_code
            or getattr(primary_span, "error_code", None)
            or primary_attrs.get("gate.rule_id")
            or primary_attrs.get("error_code")
        )

        faulting_summary = EvidenceSpanSummary(
            span_id=primary_span.span_id,
            name=primary_span.name[:64],
            duration_ms=primary_span.duration_ms,
            status=primary_span.status,
            error_code=primary_code,
            key_attributes={
                k: sanitize_text(str(v)) if isinstance(v, str) else v
                for k, v in primary_attrs.items()
                if k in SPAN_ALLOWED_ATTRIBUTES
            },
        )

        clean_summary = sanitize_text(gemini_resp.error_summary or get_safe_error_summary(primary_code) or "")
        clean_observations = [sanitize_text(obs) for obs in gemini_resp.observations]
        clean_causes = [sanitize_text(c) for c in gemini_resp.likely_causes]
        clean_recommendations = [sanitize_text(r) for r in gemini_resp.recommendations]

        diag_id = deterministic_id or compute_deterministic_diagnosis_id(
            space_id, run.run_id, run.trace_id, run.telemetry_generation, 1
        )

        suggested_action_str = (
            gemini_resp.suggested_action.value
            if hasattr(gemini_resp.suggested_action, "value")
            else str(gemini_resp.suggested_action)
        )
        confidence_str = (
            gemini_resp.confidence.value
            if hasattr(gemini_resp.confidence, "value")
            else str(gemini_resp.confidence)
        )

        return DiagnosisRecord(
            diagnosis_id=diag_id,
            space_id=space_id,
            run_id=run.run_id,
            trace_id=run.trace_id,
            telemetry_generation=run.telemetry_generation,
            schema_version=1,
            engine="gemini_flash",
            model_id=os.environ.get("GEMINI_MODEL", "gemini-3.6-flash"),
            diagnostic_status="complete",
            faulting_span=faulting_summary,
            error_code=primary_code,
            error_summary=clean_summary,
            observations=clean_observations,
            likely_causes=clean_causes,
            recommendations=clean_recommendations,
            evidence_span_ids=gemini_resp.evidence_span_ids if gemini_resp.evidence_span_ids else [primary_span.span_id],
            is_retryable=gemini_resp.is_retryable,
            suggested_action=suggested_action_str,
            confidence=confidence_str,
            grafana_dashboard_url=trace_bundle.grafana_dashboard_url,
            has_real_telemetry=trace_bundle.has_real_telemetry,
            is_local_diagnostic=False,
        )

    @classmethod
    def _call_gemini_api(cls, payload: Dict[str, Any], api_key: str) -> str:
        """
        Live Gemini API invocation via google-genai SDK.
        Configured with strict 3.0s HTTP timeout, structured response_schema, and zero-temperature.
        """
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise RuntimeError("google-genai SDK not installed") from exc

        client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=int(GEMINI_DIAGNOSIS_TIMEOUT_SECONDS * 1000)),
        )
        prompt = (
            "You are an expert distributed systems reliability engineer. "
            "Analyze the provided execution failure spans. Identify the root cause and provide remediations.\n\n"
            f"Context:\n{json.dumps(payload, indent=2)}"
        )
        response = client.models.generate_content(
            model=os.environ.get("GEMINI_MODEL", "gemini-3.6-flash"),
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=GeminiDiagnosisResponse,
                temperature=0.0,
                max_output_tokens=1024,
            ),
        )
        if not response or not response.text:
            raise ValueError("Empty response from Gemini API")
        return response.text

    @classmethod
    def shutdown_executor(cls) -> None:
        """Shutdown shared thread pool on app lifespan exit without blocking pending threads."""
        _DIAGNOSIS_EXECUTOR.shutdown(wait=False, cancel_futures=True)

    @classmethod
    def _commit_diagnosis(
        cls,
        space_id: str,
        run: Run,
        record: DiagnosisRecord,
        lease_owner: Optional[str] = None,
        lease_token: Optional[str] = None,
    ) -> None:
        existing = store.get_run(run.run_id)
        if existing and existing.space_id == space_id:
            try:
                if lease_owner is not None and lease_token is not None and hasattr(store, "atomic_commit_diagnosis_with_claim"):
                    committed = store.atomic_commit_diagnosis_with_claim(
                        space_id=space_id,
                        run_id=run.run_id,
                        diagnosis=record,
                        expected_generation=run.telemetry_generation,
                        lease_owner=lease_owner,
                        lease_token=lease_token,
                        expected_trace_id=run.trace_id,
                        expected_diagnosis_revision=run.diagnosis_revision,
                    )
                else:
                    committed = store.atomic_commit_diagnosis(
                        space_id=space_id,
                        run_id=run.run_id,
                        diagnosis=record,
                        expected_generation=run.telemetry_generation,
                        expected_trace_id=run.trace_id,
                        expected_diagnosis_revision=run.diagnosis_revision,
                    )
                if not committed:
                    raise StorageConflictError("CAS conflict: stale lease, telemetry_generation, trace_id, or diagnosis_revision")
            except StorageConflictError as e:
                logger.warning("Storage conflict committing diagnosis: %s", e)
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Diagnosis conflict: {e}",
                ) from e
            except StorageUnavailableError as e:
                logger.error("Storage unavailable committing diagnosis: %s", e)
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Storage backend temporarily unavailable",
                ) from e

            # Only update in-memory run object on successful commit
            run.latest_diagnosis_id = record.diagnosis_id
            run.latest_diagnosis_status = record.diagnostic_status
            run.agent_diagnosis = format_diagnosis_markdown(record, run.trace_id)
        else:
            run.latest_diagnosis_id = record.diagnosis_id
            run.latest_diagnosis_status = record.diagnostic_status
            run.agent_diagnosis = format_diagnosis_markdown(record, run.trace_id)
            store.save_diagnosis(record)
