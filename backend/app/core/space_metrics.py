"""Export real StudioTower Space activity as OpenTelemetry metrics and spans.

These signals are recorded only when a new activity event is stored.
They are not synthesized from Grafana Billing or test datasources.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.telemetry_sanitizer import sanitize_span_attributes
from app.models.activity import ActivityEvent

logger = logging.getLogger("studiotower.space_metrics")

SPACE_EVENTS_COUNTER_NAME = "studiotower.space.events"
SPACE_EVENTS_PROMQL_NAME = "studiotower_space_events_total"
SPACE_EVENT_SPAN_NAME = "studiotower.space.event"

_counter = None


def _get_counter():
    global _counter
    if _counter is None:
        from app.core.otel import get_meter

        _counter = get_meter("studiotower.space").create_counter(
            SPACE_EVENTS_COUNTER_NAME,
            unit="1",
            description="StudioTower Space activity events (chat, files, runs, gates)",
        )
    return _counter


def _safe_resource_name(raw: str) -> str:
    name = str(raw or "").replace("\\", "/").split("/")[-1].strip()
    return name.replace("..", "")[:80]


def _activity_attributes(event: ActivityEvent) -> dict[str, Any]:
    """Span correlation for Tempo. Metrics stay low-cardinality (space/event/resource type).

    Always: space_id, event_type, resource_type, resource_id.
    When known: resource_name (basename only), file_id, run_id, artifact_id.
    Chat text is never copied onto spans.
    """
    space_id = str(event.space_id or "").strip()
    event_type = getattr(event.event_type, "value", None) or str(event.event_type or "")
    resource_type = str(event.resource_type or "unknown")
    resource_id = str(event.resource_id or "").strip()
    details = event.details if isinstance(event.details, dict) else {}
    resource_name = _safe_resource_name(str(details.get("filename") or details.get("resource_name") or ""))
    run_id = str(details.get("run_id") or "").strip()
    if not run_id and resource_id.startswith("run_"):
        run_id = resource_id
    file_id = str(details.get("file_id") or "").strip()
    if not file_id and resource_id.startswith("file_"):
        file_id = resource_id
    artifact_id = str(details.get("artifact_id") or "").strip()
    if not artifact_id and resource_id.startswith("art_"):
        artifact_id = resource_id
    attributes = {
        "space_id": space_id[:80],
        "event_type": event_type[:80],
        "resource_type": resource_type[:40],
        "resource_id": resource_id[:80],
    }
    if resource_name:
        attributes["resource_name"] = resource_name
    if run_id:
        attributes["run_id"] = run_id[:80]
    if file_id:
        attributes["file_id"] = file_id[:80]
    if artifact_id:
        attributes["artifact_id"] = artifact_id[:80]
    return attributes


def _record_activity_span(attributes: dict[str, Any]) -> None:
    """Emit a Tempo-queryable span for this Space event. Never raises."""
    try:
        from app.core.otel import get_tracer

        tracer = get_tracer("studiotower.space")
        clean = sanitize_span_attributes(attributes)
        with tracer.start_as_current_span(SPACE_EVENT_SPAN_NAME, attributes=clean):
            pass
    except Exception as exc:
        logger.warning("Failed to record space activity span: %s", exc)


def record_space_activity(event: ActivityEvent) -> None:
    """Record one real Space activity event. Never raises into the request path."""
    attributes = _activity_attributes(event)
    if not attributes["space_id"] or not attributes["event_type"]:
        return
    metric_attrs = {
        "space_id": attributes["space_id"],
        "event_type": attributes["event_type"],
        "resource_type": attributes["resource_type"],
    }
    try:
        _get_counter().add(1, metric_attrs)
    except Exception as exc:
        logger.warning("Failed to record space activity metric: %s", exc)
    _record_activity_span(attributes)
    try:
        from app.core.otel import flush_metrics, flush_telemetry

        flush_telemetry(timeout_millis=2500)
        flush_metrics(timeout_millis=2500)
    except Exception as exc:
        logger.warning("Failed to flush space activity telemetry: %s", exc)
