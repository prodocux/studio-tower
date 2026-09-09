import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Dict, Iterator, List, Optional, Tuple

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider, ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.id_generator import RandomIdGenerator
from opentelemetry.trace import Status, StatusCode, SpanKind, SpanContext, TraceFlags

from app.core.config import settings
from app.core.telemetry_sanitizer import sanitize_span_attributes
from app.models.telemetry import TelemetryErrorCode, TelemetryStatus

logger = logging.getLogger("studiotower.otel")

_DROPPED_SPANS_COUNT = 0
_EXPORTED_SPANS_COUNT = 0
_EXPORTER_FAILURE_COUNT = 0
_METRICS_LOCK = threading.Lock()

_in_memory_exporter: Optional[InMemorySpanExporter] = None
_tracer_provider: Optional[TracerProvider] = None
_span_processor = None
_meter_provider = None
_is_configured_and_available: bool = False
_active_exporter_mode: str = "uninitialized"  # "otlp", "in_memory", "disabled"

_ID_GEN = RandomIdGenerator()
_TRACEPARENT_RE = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


class ResilientSpanExporter(SpanExporter):
    """
    Resilient decorator around any OTel SpanExporter.
    Catches, logs, and increments drop counters on exporter errors, ensuring
    core user workflows (chat, task execution, approval) are NEVER failed by telemetry issues.
    """

    def __init__(self, underlying_exporter: SpanExporter):
        self._underlying = underlying_exporter

    def export(self, spans: List[ReadableSpan]) -> SpanExportResult:
        global _EXPORTED_SPANS_COUNT, _DROPPED_SPANS_COUNT, _EXPORTER_FAILURE_COUNT
        try:
            res = self._underlying.export(spans)
            with _METRICS_LOCK:
                if res == SpanExportResult.SUCCESS:
                    _EXPORTED_SPANS_COUNT += len(spans)
                else:
                    _DROPPED_SPANS_COUNT += len(spans)
            return res
        except Exception as exc:
            with _METRICS_LOCK:
                _EXPORTER_FAILURE_COUNT += 1
                _DROPPED_SPANS_COUNT += len(spans)
            logger.warning(
                "OTel export encountered non-fatal error: %s",
                exc,
                extra={"dropped_spans": len(spans)},
            )
            return SpanExportResult.FAILURE

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        try:
            return self._underlying.force_flush(timeout_millis=timeout_millis)
        except Exception as exc:
            logger.warning("OTel force_flush failed: %s", exc)
            return False

    def shutdown(self):
        try:
            self._underlying.shutdown()
        except Exception as exc:
            logger.warning("OTel exporter shutdown failed: %s", exc)


def generate_otel_trace_id() -> str:
    """
    Generate authentic 32-character hexadecimal OpenTelemetry Trace ID.
    """
    tid_int = _ID_GEN.generate_trace_id()
    return f"{tid_int:032x}"


def generate_otel_span_id() -> str:
    """
    Generate an authentic 16-hex character OpenTelemetry span ID.
    """
    raw_int = _ID_GEN.generate_span_id()
    return f"{raw_int:016x}"


def inject_traceparent(carrier: Dict[str, Any]) -> Dict[str, Any]:
    """
    Serialize active span context into standard W3C traceparent string:
    '00-{32-hex trace_id}-{16-hex span_id}-01'
    """
    current_span = trace.get_current_span()
    ctx = current_span.get_span_context() if current_span else None
    if ctx and ctx.is_valid:
        tid = f"{ctx.trace_id:032x}"
        sid = f"{ctx.span_id:016x}"
        flags = f"{int(ctx.trace_flags):02x}"
        carrier["traceparent"] = f"00-{tid}-{sid}-{flags}"
    return carrier


def extract_traceparent(carrier: Dict[str, Any]) -> Optional[SpanContext]:
    """
    Deserialize W3C traceparent string into a valid OpenTelemetry SpanContext.
    """
    header = carrier.get("traceparent") or carrier.get("Traceparent")
    if not header or not isinstance(header, str):
        return None
    m = _TRACEPARENT_RE.match(header.strip().lower())
    if not m:
        return None
    tid_hex, sid_hex, flags_hex = m.groups()
    try:
        return SpanContext(
            trace_id=int(tid_hex, 16),
            span_id=int(sid_hex, 16),
            is_remote=True,
            trace_flags=TraceFlags(int(flags_hex, 16)),
        )
    except Exception:
        return None


def _otlp_signal_url(endpoint: str, signal: str) -> str:
    base = (endpoint or "").rstrip("/")
    for suffix in ("/v1/traces", "/v1/metrics", "/v1/logs"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return f"{base.rstrip('/')}/v1/{signal}"


def _parse_otlp_headers(raw: Optional[str] = None) -> Dict[str, str]:
    from urllib.parse import unquote

    value = raw if raw is not None else os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")
    if not value or not str(value).strip():
        return {}
    headers: Dict[str, str] = {}
    for part in str(value).split(","):
        if "=" not in part:
            continue
        key, item = part.split("=", 1)
        headers[key.strip()] = unquote(item.strip())
    return headers


def _init_meter_provider(resource: Resource, otlp_endpoint: Optional[str], headers: Optional[Dict[str, str]]) -> None:
    global _meter_provider
    from opentelemetry import metrics as otel_metrics
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader, PeriodicExportingMetricReader

    readers = []
    if otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

            exporter_kwargs: Dict[str, Any] = {
                "endpoint": _otlp_signal_url(otlp_endpoint, "metrics"),
                "timeout": int(os.environ.get("OTEL_EXPORTER_OTLP_TIMEOUT", "10")),
            }
            if headers:
                exporter_kwargs["headers"] = headers
            readers.append(
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(**exporter_kwargs),
                    export_interval_millis=int(os.environ.get("OTEL_METRIC_EXPORT_INTERVAL", "5000")),
                    export_timeout_millis=int(os.environ.get("OTEL_EXPORTER_OTLP_TIMEOUT", "10")) * 1000,
                )
            )
        except Exception as exc:
            logger.error("Failed to configure OTLPMetricExporter: %s", exc)
    if not readers:
        readers.append(InMemoryMetricReader())
    provider = MeterProvider(resource=resource, metric_readers=readers)
    _meter_provider = provider
    try:
        otel_metrics.set_meter_provider(provider)
    except Exception:
        pass


def initialize_otel(
    service_name: str = "studiotower-api",
    otlp_endpoint: Optional[str] = None,
    otlp_headers: Optional[Dict[str, str]] = None,
    use_in_memory: bool = False,
    environment: Optional[str] = None,
) -> bool:
    """
    Lifespan-managed initialization of OpenTelemetry SDK.
    In Production:
    - If OTLP endpoint is missing, telemetry is disabled/unavailable (never silently fake exporting).
    In Dev/Test:
    - Allows in_memory exporter.
    """
    global _tracer_provider, _span_processor, _in_memory_exporter
    global _is_configured_and_available, _active_exporter_mode

    env = (environment or os.environ.get("ENV") or os.environ.get("ENVIRONMENT") or settings.ENV or "development").lower()
    endpoint = otlp_endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    headers = otlp_headers or _parse_otlp_headers()
    resource = Resource.create({"service.name": service_name, "service.namespace": "studiotower"})

    if use_in_memory and env in ("test", "development", "local"):
        _in_memory_exporter = InMemorySpanExporter()
        underlying: SpanExporter = _in_memory_exporter
        _active_exporter_mode = "in_memory"
        _is_configured_and_available = True
    elif endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            exporter_kwargs: Dict[str, Any] = {
                "endpoint": _otlp_signal_url(endpoint, "traces"),
                "timeout": int(os.environ.get("OTEL_EXPORTER_OTLP_TIMEOUT", "10")),
            }
            if headers:
                exporter_kwargs["headers"] = headers
            underlying = OTLPSpanExporter(**exporter_kwargs)
            _active_exporter_mode = "otlp"
            _is_configured_and_available = True
        except Exception as exc:
            logger.error("Failed to configure OTLPSpanExporter: %s", exc)
            _active_exporter_mode = "disabled"
            _is_configured_and_available = False
            _init_meter_provider(resource, None, None)
            return False
    else:
        # In production without endpoint: explicitly unavailable!
        _active_exporter_mode = "disabled"
        _is_configured_and_available = False
        _tracer_provider = None
        logger.info("OTel endpoint not configured. Telemetry status set to unavailable.")
        _init_meter_provider(resource, None, None)
        return False

    _init_meter_provider(resource, endpoint if _active_exporter_mode == "otlp" else None, headers if _active_exporter_mode == "otlp" else None)
    provider = TracerProvider(resource=resource)

    # Cloud Run only allocates CPU during a request. Batch export after the
    # response returns never leaves the instance. Export each span immediately.
    resilient = ResilientSpanExporter(underlying)
    _span_processor = SimpleSpanProcessor(resilient)
    provider.add_span_processor(_span_processor)

    _tracer_provider = provider
    try:
        trace.set_tracer_provider(provider)
    except Exception:
        pass
    return True


def get_meter(name: str = "studiotower"):
    from opentelemetry import metrics as otel_metrics

    if _meter_provider is not None:
        return _meter_provider.get_meter(name)
    return otel_metrics.get_meter(name)


def is_telemetry_available() -> bool:
    return _is_configured_and_available


def get_initial_telemetry_status() -> str:
    """
    Returns initial status for newly dispatched runs:
    - 'exporting' if OTel backend is configured
    - 'unavailable' if OTel endpoint is unconfigured / disabled
    """
    if _is_configured_and_available:
        return TelemetryStatus.EXPORTING.value
    return TelemetryStatus.UNAVAILABLE.value


def get_tracer(name: str = "studiotower") -> trace.Tracer:
    global _tracer_provider
    env = (os.environ.get("ENV") or os.environ.get("ENVIRONMENT") or settings.ENV or "development").lower()
    if env in ("production", "prod", "staging"):
        if _tracer_provider is not None and _active_exporter_mode == "otlp":
            return _tracer_provider.get_tracer(name)
        # Fail-closed in production: do not lazily initialize InMemory exporter, and return NoOp if not OTLP
        return trace.NoOpTracer()

    if _tracer_provider is not None:
        return _tracer_provider.get_tracer(name)

    initialize_otel(use_in_memory=True, environment=env)
    if _tracer_provider is not None:
        return _tracer_provider.get_tracer(name)
    return trace.NoOpTracer()


def flush_telemetry(timeout_millis: int = 2000) -> bool:
    global _tracer_provider
    if _tracer_provider:
        try:
            return bool(_tracer_provider.force_flush(timeout_millis=timeout_millis))
        except Exception as exc:
            logger.warning("OTel force_flush failed: %s", exc)
            return False
    return True


def flush_metrics(timeout_millis: int = 2000) -> bool:
    global _meter_provider
    if _meter_provider is None:
        return True
    try:
        return bool(_meter_provider.force_flush(timeout_millis=timeout_millis))
    except Exception as exc:
        logger.warning("OTel metrics force_flush failed: %s", exc)
        return False


def shutdown_otel(timeout_millis: int = 2000):
    global _tracer_provider, _span_processor, _in_memory_exporter, _is_configured_and_available, _active_exporter_mode, _meter_provider
    flush_telemetry(timeout_millis=timeout_millis)
    flush_metrics(timeout_millis=timeout_millis)
    if _span_processor:
        try:
            _span_processor.shutdown()
        except Exception as e:
            logger.warning("Error during OTel span processor shutdown: %s", e)
    if _tracer_provider:
        try:
            _tracer_provider.shutdown()
        except Exception as e:
            logger.warning("Error during TracerProvider shutdown: %s", e)
    if _meter_provider is not None:
        try:
            _meter_provider.shutdown()
        except Exception as e:
            logger.warning("Error during MeterProvider shutdown: %s", e)
    _span_processor = None
    _tracer_provider = None
    _in_memory_exporter = None
    _meter_provider = None
    _is_configured_and_available = False
    _active_exporter_mode = "uninitialized"


def reset_otel_for_testing():
    """Clean reset for pytest fixture isolation."""
    global _tracer_provider, _span_processor, _in_memory_exporter
    global _DROPPED_SPANS_COUNT, _EXPORTED_SPANS_COUNT, _EXPORTER_FAILURE_COUNT
    flush_telemetry(timeout_millis=500)
    if _in_memory_exporter:
        _in_memory_exporter.clear()
    with _METRICS_LOCK:
        _DROPPED_SPANS_COUNT = 0
        _EXPORTED_SPANS_COUNT = 0
        _EXPORTER_FAILURE_COUNT = 0


def get_in_memory_spans() -> List[ReadableSpan]:
    global _in_memory_exporter
    if _in_memory_exporter:
        return list(_in_memory_exporter.get_finished_spans())
    return []


def clear_in_memory_spans():
    global _in_memory_exporter
    if _in_memory_exporter:
        _in_memory_exporter.clear()


def get_telemetry_exporter_metrics() -> Dict[str, int]:
    with _METRICS_LOCK:
        return {
            "exported_spans": _EXPORTED_SPANS_COUNT,
            "dropped_spans": _DROPPED_SPANS_COUNT,
            "exporter_failures": _EXPORTER_FAILURE_COUNT,
        }


def reset_telemetry_exporter_metrics():
    reset_otel_for_testing()


@contextmanager
def trace_stage(
    name: str,
    space_id_hash: str,
    run_id: str,
    stage: str,
    action_type: str = "general",
    trace_id_hex: Optional[str] = None,
    parent_context: Optional[SpanContext] = None,
    attributes: Optional[Dict[str, Any]] = None,
) -> Iterator[Dict[str, Any]]:
    """
    Context manager wrapping real pipeline executions (E1.5 Requirement 5 & 6).
    - Captures authentic start/end timestamps and true non-zero duration.
    - Establishes genuine parent-child hierarchy in OpenTelemetry context.
    - Standardizes exceptions into TelemetryErrorCode enums without leaking raw exceptions/stacks.
    """
    tracer = get_tracer()
    raw_attrs = {
        "space_id_hash": space_id_hash,
        "run_id": run_id,
        "stage": stage,
        "action_type": action_type,
        "status": "ok",
        **(attributes or {}),
    }

    # Determine context: explicit remote parent_context or current active context
    # Never create fake synthetic parents
    ctx = None
    if parent_context and parent_context.is_valid:
        ctx = trace.set_span_in_context(trace.NonRecordingSpan(parent_context))

    stage_result: Dict[str, Any] = {"error_code": None, "status": "ok"}
    clean_attrs = sanitize_span_attributes(raw_attrs)

    with tracer.start_as_current_span(
        name=name,
        context=ctx,
        attributes=clean_attrs,
        kind=SpanKind.INTERNAL,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        sc = span.get_span_context()
        stage_result["trace_id_hex"] = f"{sc.trace_id:032x}"
        stage_result["span_id_hex"] = f"{sc.span_id:016x}"
        try:
            yield stage_result
            if stage_result.get("status") == "error":
                raw_err = stage_result.get("error_code")
                valid_error_codes = {c.value for c in TelemetryErrorCode}
                if raw_err in valid_error_codes:
                    err = raw_err
                elif isinstance(raw_err, TelemetryErrorCode):
                    err = raw_err.value
                else:
                    err = TelemetryErrorCode.UNKNOWN_INTERNAL_ERROR.value
                stage_result["error_code"] = err
                span.set_attribute("status", "error")
                span.set_attribute("error_code", err)
                span.set_status(Status(StatusCode.ERROR, description=err))
            else:
                span.set_status(Status(StatusCode.OK))
        except Exception as exc:
            err_enum = TelemetryErrorCode.UNKNOWN_INTERNAL_ERROR.value
            exc_type = type(exc).__name__
            if "Conflict" in exc_type or "Lock" in exc_type:
                err_enum = TelemetryErrorCode.EXCLUSIVE_TECH_LOCK_VIOLATION.value
            elif "Timeout" in exc_type:
                err_enum = TelemetryErrorCode.STORAGE_COMMIT_TIMEOUT.value
            elif "Extraction" in exc_type or "Ingestion" in exc_type:
                err_enum = TelemetryErrorCode.INGESTION_EXTRACTION_FAILURE.value
            elif "PDX" in exc_type:
                err_enum = TelemetryErrorCode.PDX_EXECUTION_FAILURE.value

            span.set_attribute("status", "error")
            span.set_attribute("error_code", err_enum)
            span.set_status(Status(StatusCode.ERROR, description=err_enum))
            stage_result["status"] = "error"
            stage_result["error_code"] = err_enum
            raise


def record_pipeline_span(
    name: str,
    space_id_hash: str,
    run_id: str,
    trace_id_str: str,
    stage: str,
    action_type: str = "general",
    status: str = "ok",
    error_code: Optional[str] = None,
    attributes: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Convenience method to synchronously record a pipeline span.
    """
    attrs = dict(attributes or {})
    if error_code:
        attrs["error_code"] = error_code
    with trace_stage(
        name=name,
        space_id_hash=space_id_hash,
        run_id=run_id,
        stage=stage,
        action_type=action_type,
        trace_id_hex=trace_id_str,
        attributes=attrs,
    ) as stage_ctx:
        if status == "error":
            stage_ctx["status"] = "error"
            stage_ctx["error_code"] = error_code
