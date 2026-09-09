"""Pull Grafana Cloud dashboard visuals into StudioTower.

This is the Grafana HTTP API (search + dashboard JSON + datasource query).
It is not the local OTel waterfall and not MCP JSON summaries.
Tokens never appear in returned payloads or log lines.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from pydantic import BaseModel, Field

from app.core.grafana_links import RE_DASHBOARD_ID, validate_grafana_base_url
from app.integrations.grafana_mcp import redact_mcp_text

logger = logging.getLogger(__name__)

MAX_DASHBOARDS = 2
MAX_PANELS = 12
MAX_SERIES_POINTS = 120
# Billing/Usage panels are monthly estimates and are not StudioTower Space activity.
# Space activity queries use a 7-day window.
QUERY_WINDOW_MS = 7 * 24 * 3600 * 1000
QUERY_PANEL_TYPES = frozenset(
    {"timeseries", "graph", "stat", "gauge", "bargauge", "heatmap", "trend", "xychart", "table"}
)
QUERY_COPY_KEYS = frozenset(
    {
        "expr",
        "query",
        "queryType",
        "legendFormat",
        "instant",
        "range",
        "format",
        "intervalMs",
        "maxDataPoints",
        "exemplar",
        "utcOffsetSec",
        "rawSql",
        "sql",
        "editorMode",
        "interval",
        "step",
        "lookbackDelta",
    }
)
SKIP_DATASOURCE_TYPES = frozenset(
    {"grafana", "datasource", "grafana-testdata-datasource", "grafana-pyroscope-datasource"}
)
SKIP_DATASOURCE_UIDS = frozenset({"grafana", "-- Grafana --", "-- Mixed --", "-- Dashboard --"})
_LABEL_TEMPLATE_VAR = re.compile(r'(=~?)\s*["\']\$\{?[A-Za-z_][\w]*\}?["\']')


class GrafanaSeries(BaseModel):
    name: str
    points: list[list[float]] = Field(default_factory=list)


class GrafanaPanelVisual(BaseModel):
    panel_id: int
    title: str
    type: str
    image_base64: str | None = None
    series: list[GrafanaSeries] = Field(default_factory=list)
    table_rows: list[list[str]] = Field(default_factory=list)
    query_error: str | None = None


class GrafanaDashboardVisual(BaseModel):
    uid: str
    title: str
    url: str
    panels: list[GrafanaPanelVisual] = Field(default_factory=list)


class GrafanaTempoTrace(BaseModel):
    trace_id: str
    span_name: str = ""
    resource_id: str = ""
    event_type: str = ""
    resource_type: str = ""
    resource_name: str = ""
    run_id: str = ""
    file_id: str = ""
    artifact_id: str = ""
    started_unix_ms: int = 0


class GrafanaLineageStep(BaseModel):
    node_id: str
    title: str
    stage: str
    detail: str = ""
    event_type: str = ""
    resource_id: str = ""
    resource_name: str = ""
    run_id: str = ""
    file_id: str = ""
    artifact_id: str = ""
    trace_id: str = ""


class GrafanaTempoLineage(BaseModel):
    connected: bool = False
    source: str = "grafana-tempo"
    trace_count: int = 0
    traces: list[GrafanaTempoTrace] = Field(default_factory=list)
    error: str | None = None


class GrafanaVisualBoard(BaseModel):
    connected: bool = False
    source: str | None = None
    base_url: str | None = None
    org_name: str | None = None
    datasource_names: list[str] = Field(default_factory=list)
    dashboard_count: int = 0
    ingest: str | None = None
    ingest_detail: str | None = None
    error: str | None = None
    dashboards: list[GrafanaDashboardVisual] = Field(default_factory=list)
    lineage_flow: list[GrafanaLineageStep] = Field(default_factory=list)


class GrafanaVisualError(Exception):
    """Fail-closed Grafana HTTP visual error. Message must not include credentials."""


def _token() -> str:
    from app.core.config import settings

    for value in (
        settings.GRAFANA_SERVICE_ACCOUNT_TOKEN,
        settings.GRAFANA_MCP_ACCESS_TOKEN,
        settings.GRAFANA_CLOUD_API_KEY,
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _validated_base() -> str:
    from app.core.config import settings

    raw = (settings.GRAFANA_BASE_URL or "").strip()
    hosts = list(settings.GRAFANA_ALLOWED_HOSTS or [])
    ok, clean, err = validate_grafana_base_url(raw, hosts)
    if not ok or not clean:
        raise GrafanaVisualError(err or "Grafana base URL is not allowlisted")
    return clean.rstrip("/")


def is_grafana_visual_configured() -> bool:
    try:
        return bool(_token() and _validated_base())
    except GrafanaVisualError:
        return False


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "StudioTower-GrafanaVisual/1.0",
    }


def _client() -> httpx.Client:
    from app.core.config import settings

    # Visual board fans out search + dashboard + ds/query. Per-request timeout
    # stays bounded; Grafana Cloud image renderer is not used (usually 404).
    timeout = max(float(settings.GRAFANA_MCP_TIMEOUT_SECONDS), 20.0)
    return httpx.Client(timeout=timeout, follow_redirects=True)


def _get_json(client: httpx.Client, url: str, token: str) -> Any:
    response = client.get(url, headers=_headers(token))
    if response.status_code == 401:
        raise GrafanaVisualError("Grafana HTTP API unauthorized")
    if response.status_code == 403:
        raise GrafanaVisualError("Grafana HTTP API forbidden")
    if response.status_code >= 400:
        raise GrafanaVisualError(f"Grafana HTTP API returned {response.status_code}")
    try:
        return response.json()
    except Exception as exc:
        raise GrafanaVisualError("Grafana HTTP API returned non-JSON") from exc


def _flatten_panels(panels: list[Any]) -> list[dict[str, Any]]:
    flat: list[dict[str, Any]] = []
    for panel in panels:
        if not isinstance(panel, dict):
            continue
        nested = panel.get("panels")
        if isinstance(nested, list) and nested:
            flat.extend(_flatten_panels(nested))
            continue
        kind = str(panel.get("type") or "")
        if kind in {"row", "text", "news", "dashlist", "livestat"}:
            continue
        panel_id = panel.get("id")
        if not isinstance(panel_id, int):
            continue
        flat.append(panel)
    return flat


def _panel_datasource(panel: dict[str, Any], dashboard_ds: Any) -> Any:
    ds = panel.get("datasource") or dashboard_ds
    if isinstance(ds, str):
        return {"uid": ds}
    return ds


def _escape_prom_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "").replace("\r", "")


def _expand_promql(expr: str, space_id: str | None = None) -> str:
    expanded = expr
    if space_id:
        escaped = _escape_prom_label(space_id)
        expanded = expanded.replace("${space_id}", escaped).replace("$space_id", escaped)
    for src, dst in (
        ("$__rate_interval", "5m"),
        ("${__rate_interval}", "5m"),
        ("$__interval_ms", "60000"),
        ("${__interval_ms}", "60000"),
        ("$__interval", "1m"),
        ("${__interval}", "1m"),
        ("$__range_s", str(QUERY_WINDOW_MS // 1000)),
        ("${__range_s}", str(QUERY_WINDOW_MS // 1000)),
        ("$__range", "7d"),
        ("${__range}", "7d"),
    ):
        expanded = expanded.replace(src, dst)
    return _LABEL_TEMPLATE_VAR.sub(r'=~".*"', expanded)


def _usable_datasource(ds: Any) -> dict[str, Any] | None:
    if isinstance(ds, str):
        ds = {"uid": ds}
    if not isinstance(ds, dict):
        return None
    uid = str(ds.get("uid") or "").strip()
    kind = str(ds.get("type") or "").strip()
    if not uid or uid in SKIP_DATASOURCE_UIDS or kind in SKIP_DATASOURCE_TYPES:
        return None
    return {"uid": uid, **({"type": kind} if kind else {})}


def _query_payload(panel: dict[str, Any], dashboard_ds: Any, space_id: str | None = None) -> dict[str, Any] | None:
    targets = [t for t in (panel.get("targets") or []) if isinstance(t, dict) and not t.get("hide")]
    if not targets:
        return None
    queries: list[dict[str, Any]] = []
    default_ds = _panel_datasource(panel, dashboard_ds)
    min_interval_ms = max(QUERY_WINDOW_MS // MAX_SERIES_POINTS, 60_000)
    for index, target in enumerate(targets[:4]):
        ds = _usable_datasource(target.get("datasource") or default_ds)
        if not ds:
            continue
        query: dict[str, Any] = {
            key: value
            for key, value in target.items()
            if key in QUERY_COPY_KEYS and value not in (None, "")
        }
        expr = query.get("expr")
        if isinstance(expr, str) and expr.strip():
            query["expr"] = _expand_promql(expr, space_id)
        loki = query.get("query")
        if isinstance(loki, str) and loki.strip() and "$" in loki:
            query["query"] = _expand_promql(loki, space_id)
        if not any(isinstance(query.get(key), str) and query.get(key).strip() for key in ("expr", "query", "rawSql", "sql")):
            continue
        if query.get("instant") and query.get("range"):
            query["range"] = False
        query["exemplar"] = False
        query["refId"] = str(target.get("refId") or chr(65 + index))
        query["datasource"] = ds
        query["maxDataPoints"] = MAX_SERIES_POINTS
        try:
            query["intervalMs"] = max(int(query.get("intervalMs") or 0), min_interval_ms)
        except (TypeError, ValueError):
            query["intervalMs"] = min_interval_ms
        if isinstance(query.get("interval"), str) and "$" in query["interval"]:
            query.pop("interval", None)
        queries.append(query)
    if not queries:
        return None
    now_ms = int(time.time() * 1000)
    return {"from": str(now_ms - QUERY_WINDOW_MS), "to": str(now_ms), "queries": queries}


def _frames_to_series(results: dict[str, Any]) -> list[GrafanaSeries]:
    series: list[GrafanaSeries] = []
    for ref_id, payload in results.items():
        if not isinstance(payload, dict):
            continue
        frames = payload.get("frames") or []
        for frame_index, frame in enumerate(frames):
            if not isinstance(frame, dict):
                continue
            fields = ((frame.get("schema") or {}).get("fields") or [])
            values = ((frame.get("data") or {}).get("values") or [])
            if not isinstance(fields, list) or not isinstance(values, list) or not values:
                continue
            time_idx = next(
                (i for i, field in enumerate(fields) if isinstance(field, dict) and field.get("type") == "time"),
                None,
            )
            for field_idx, field in enumerate(fields):
                if field_idx == time_idx or not isinstance(field, dict):
                    continue
                samples = values[field_idx] if field_idx < len(values) else []
                if not isinstance(samples, list):
                    continue
                points: list[list[float]] = []
                if time_idx is not None:
                    times = values[time_idx] if time_idx < len(values) else []
                    if not isinstance(times, list):
                        continue
                    pairs = zip(times, samples)
                else:
                    pairs = ((index, sample) for index, sample in enumerate(samples))
                for stamp, sample in pairs:
                    try:
                        points.append([float(stamp), float(sample)])
                    except (TypeError, ValueError):
                        continue
                    if len(points) >= MAX_SERIES_POINTS:
                        break
                if not points:
                    continue
                name = str(field.get("name") or ref_id or f"series-{frame_index}")
                series.append(GrafanaSeries(name=name[:80], points=points))
                if len(series) >= 4:
                    return series
    return series


def _cell_text(field: dict[str, Any], cell: Any) -> str:
    if cell is None:
        return "n/a"
    if field.get("type") == "time":
        try:
            stamp = float(cell)
            if stamp > 1e12:
                stamp = stamp / 1000.0
            return datetime.fromtimestamp(stamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        except (TypeError, ValueError, OSError, OverflowError):
            pass
    return str(cell)[:80]


def _frames_to_table(results: dict[str, Any]) -> list[list[str]]:
    for payload in results.values():
        if not isinstance(payload, dict):
            continue
        for frame in payload.get("frames") or []:
            if not isinstance(frame, dict):
                continue
            fields = ((frame.get("schema") or {}).get("fields") or [])
            values = ((frame.get("data") or {}).get("values") or [])
            if not isinstance(fields, list) or not isinstance(values, list) or not values:
                continue
            typed_fields = [field for field in fields if isinstance(field, dict)]
            names = [str(field.get("name") or f"col{index}")[:40] for index, field in enumerate(typed_fields)]
            if not names:
                continue
            row_count = max((len(col) for col in values if isinstance(col, list)), default=0)
            if row_count == 0:
                continue
            rows: list[list[str]] = [names]
            for row_idx in range(min(row_count, 20)):
                row: list[str] = []
                for col_idx, field in enumerate(typed_fields):
                    column = values[col_idx] if col_idx < len(values) and isinstance(values[col_idx], list) else []
                    cell = column[row_idx] if row_idx < len(column) else None
                    row.append(_cell_text(field, cell))
                rows.append(row)
            return rows
    return []


def _ds_query_error(response: httpx.Response) -> str:
    prefix = f"Grafana ds/query returned {response.status_code}"
    try:
        payload = response.json()
    except Exception:
        return prefix
    if not isinstance(payload, dict):
        return prefix
    message = payload.get("message")
    if isinstance(message, str) and message.strip():
        return redact_mcp_text(f"{prefix}: {message.strip()[:160]}")
    results = payload.get("results")
    if isinstance(results, dict):
        for item in results.values():
            if not isinstance(item, dict):
                continue
            err = item.get("error") or item.get("message")
            if isinstance(err, str) and err.strip():
                return redact_mcp_text(f"{prefix}: {err.strip()[:160]}")
    return prefix


def _result_frame_error(results: dict[str, Any]) -> str | None:
    for item in results.values():
        if not isinstance(item, dict):
            continue
        err = item.get("error") or item.get("message")
        if isinstance(err, str) and err.strip():
            return redact_mcp_text(err.strip()[:160])
    return None


def _query_panel(
    client: httpx.Client,
    base: str,
    token: str,
    panel: dict[str, Any],
    dashboard_ds: Any,
    space_id: str | None = None,
) -> tuple[list[GrafanaSeries], list[list[str]], str | None]:
    body = _query_payload(panel, dashboard_ds, space_id=space_id)
    if not body:
        return [], [], "Grafana panel has no queryable datasource target"
    try:
        response = client.post(
            f"{base}/api/ds/query",
            headers={**_headers(token), "Content-Type": "application/json"},
            json=body,
        )
    except httpx.HTTPError as exc:
        logger.warning("Grafana ds/query transport failed: %s", redact_mcp_text(str(exc)))
        return [], [], "Grafana ds/query is unreachable"
    if response.status_code >= 400:
        return [], [], _ds_query_error(response)
    try:
        payload = response.json()
    except Exception:
        return [], [], "Grafana ds/query returned non-JSON"
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, dict):
        return [], [], "Grafana ds/query returned no frames"
    series = _frames_to_series(results)
    table_rows = _frames_to_table(results)
    if not series and not table_rows:
        fallback_body = _space_events_fallback_query(body)
        if fallback_body:
            try:
                retry = client.post(
                    f"{base}/api/ds/query",
                    headers={**_headers(token), "Content-Type": "application/json"},
                    json=fallback_body,
                )
            except httpx.HTTPError:
                retry = None
            if retry is not None and retry.status_code < 400:
                try:
                    retry_payload = retry.json()
                except Exception:
                    retry_payload = None
                retry_results = retry_payload.get("results") if isinstance(retry_payload, dict) else None
                if isinstance(retry_results, dict):
                    series = _frames_to_series(retry_results)
                    table_rows = _frames_to_table(retry_results)
                    if series or table_rows:
                        return series, table_rows, None
        return [], [], _result_frame_error(results)
    return series, table_rows, None


def _space_events_fallback_query(body: dict[str, Any]) -> dict[str, Any] | None:
    queries = body.get("queries")
    if not isinstance(queries, list) or not queries or not isinstance(queries[0], dict):
        return None
    expr = str(queries[0].get("expr") or "")
    if "studiotower_space_events_total" not in expr:
        return None
    clone = json.loads(json.dumps(body))
    clone["queries"][0]["expr"] = expr.replace("studiotower_space_events_total", "studiotower_space_events")
    return clone


def _tempo_query_text(space_id: str) -> str:
    escaped = _escape_prom_label(space_id)
    return f'{{ span.space_id = "{escaped}" || .space_id = "{escaped}" }}'


def _traces_from_ds_results(results: dict[str, Any]) -> list[GrafanaTempoTrace]:
    traces: list[GrafanaTempoTrace] = []
    seen: set[str] = set()
    for payload in results.values():
        if not isinstance(payload, dict):
            continue
        for frame in payload.get("frames") or []:
            if not isinstance(frame, dict):
                continue
            fields = ((frame.get("schema") or {}).get("fields") or [])
            values = ((frame.get("data") or {}).get("values") or [])
            if not isinstance(fields, list) or not isinstance(values, list):
                continue
            index_by_name = {
                str(field.get("name") or "").lower(): idx
                for idx, field in enumerate(fields)
                if isinstance(field, dict)
            }
            row_count = max((len(col) for col in values if isinstance(col, list)), default=0)
            for row_idx in range(min(row_count, 30)):
                def cell(name: str) -> str:
                    idx = index_by_name.get(name)
                    if idx is None or idx >= len(values) or not isinstance(values[idx], list):
                        return ""
                    raw = values[idx][row_idx] if row_idx < len(values[idx]) else ""
                    return str(raw or "").strip()

                trace_id = cell("traceid") or cell("trace_id") or cell("trace id")
                if not trace_id or trace_id in seen:
                    continue
                seen.add(trace_id)
                started = 0
                raw_started = cell("starttimeunixnano") or cell("starttime") or cell("starttimeunixms")
                try:
                    started_int = int(raw_started or "0")
                    if started_int > 10_000_000_000:
                        started = started_int // 1_000_000
                    elif started_int > 0:
                        started = started_int
                except (TypeError, ValueError):
                    started = 0
                traces.append(
                    GrafanaTempoTrace(
                        trace_id=trace_id[:64],
                        span_name=(cell("name") or cell("spanname") or cell("roottracename"))[:120],
                        resource_id=(cell("resource_id") or cell("span.resource_id"))[:80],
                        event_type=(cell("event_type") or cell("span.event_type"))[:80],
                        resource_type=(cell("resource_type") or cell("span.resource_type"))[:40],
                        resource_name=(cell("resource_name") or cell("span.resource_name") or cell("filename"))[:80],
                        run_id=(cell("run_id") or cell("span.run_id"))[:80],
                        file_id=(cell("file_id") or cell("span.file_id"))[:80],
                        artifact_id=(cell("artifact_id") or cell("span.artifact_id"))[:80],
                        started_unix_ms=started,
                    )
                )
    return traces


def _traces_from_search_payload(payload: Any) -> list[GrafanaTempoTrace]:
    traces: list[GrafanaTempoTrace] = []
    if isinstance(payload, dict):
        items = payload.get("traces") or payload.get("data") or []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        trace_id = str(item.get("traceID") or item.get("traceId") or item.get("trace_id") or "").strip()
        if not trace_id or trace_id in seen:
            continue
        seen.add(trace_id)
        traces.append(
            GrafanaTempoTrace(
                trace_id=trace_id[:64],
                span_name=str(item.get("rootTraceName") or item.get("rootName") or "")[:120],
            )
        )
        if len(traces) >= 30:
            break
    return traces


def _traces_table(traces: list[GrafanaTempoTrace]) -> list[list[str]]:
    if not traces:
        return []
    rows = [["trace_id", "span", "resource_id", "event_type"]]
    for item in traces[:20]:
        rows.append([item.trace_id, item.span_name or "n/a", item.resource_id or "n/a", item.event_type or "n/a"])
    return rows


_FLOW_TITLES = {
    "file.uploaded": "File ingested",
    "file.ingestion_ready": "File indexed",
    "file.ingestion_partial_ocr": "File indexed (partial OCR)",
    "file.needs_ocr": "File needs OCR",
    "file.ingestion_failed": "File ingestion failed",
    "file.reindex_triggered": "File reindexed",
    "message.created": "Crew message",
    "run.started": "AI run started",
    "run.completed": "AI run completed",
    "run.failed": "AI run failed",
    "gate.requested": "Approval requested",
    "gate.approved": "Human approved",
    "gate.rejected": "Human rejected",
}
_STAGE_RANK = {"file": 0, "artifact": 1, "message": 2, "run": 3, "gate": 4, "trace": 5}


def _lineage_stage(event_type: str, resource_type: str, resource_id: str = "") -> str:
    kind = (resource_type or "").lower()
    blob = (event_type or "").lower()
    rid = resource_id or ""
    if kind == "artifact" or blob.startswith("artifact") or rid.startswith("art_"):
        return "artifact"
    if kind == "file" or blob.startswith("file") or rid.startswith("file_"):
        return "file"
    if blob.startswith("message") or kind == "message":
        return "message"
    if blob.startswith("run") or kind == "run" or rid.startswith("run_"):
        return "run"
    if blob.startswith("gate") or kind == "gate":
        return "gate"
    return "trace"


def _lookup_resource_name(space_id: str, resource_id: str, resource_type: str, file_id: str = "") -> str:
    """Label a Tempo resource id with the Space catalog name. Does not invent extra nodes."""
    if not space_id:
        return ""
    try:
        from app.services.storage import store

        if resource_type == "file" or (resource_id or "").startswith("file_"):
            rec = store.get_file(resource_id)
            if rec and rec.space_id == space_id:
                return str(rec.filename or "")[:80]
        if resource_type == "artifact" or (resource_id or "").startswith("art_"):
            art = store.get_artifact(space_id, resource_id)
            if art:
                return str(art.filename or "")[:80]
            if (resource_id or "").startswith("art_"):
                file_art = store.get_file(f"file_art_{resource_id[4:]}")
                if file_art and file_art.space_id == space_id:
                    return str(file_art.filename or "")[:80]
        if resource_type == "run" or (resource_id or "").startswith("run_"):
            run = store.get_run(resource_id)
            if run and run.space_id == space_id:
                file_id = file_id or (run.source_file_id or "")
        if file_id:
            rec = store.get_file(file_id)
            if rec and rec.space_id == space_id:
                return str(rec.filename or "")[:80]
    except Exception:
        return ""
    return ""


def build_lineage_flow(traces: list[GrafanaTempoTrace], space_id: str | None = None) -> list[GrafanaLineageStep]:
    """Turn Tempo Space-activity spans into a process that names assets, not raw ids."""
    ordered = sorted(
        traces,
        key=lambda item: (
            _STAGE_RANK.get(_lineage_stage(item.event_type, item.resource_type, item.resource_id), 9),
            item.started_unix_ms or 0,
        ),
    )
    steps: list[GrafanaLineageStep] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(ordered[:20]):
        event_type = item.event_type or ""
        resource_id = item.resource_id or ""
        key = (event_type or item.span_name or item.trace_id, resource_id)
        if key in seen:
            continue
        seen.add(key)
        stage = _lineage_stage(event_type, item.resource_type, resource_id)
        action = _FLOW_TITLES.get(event_type) or item.span_name or "Tempo activity"
        resource_name = (item.resource_name or "").strip() or _lookup_resource_name(
            space_id or "", resource_id, item.resource_type, item.file_id
        )
        title = resource_name or action
        detail = action if resource_name else (resource_id or (item.trace_id[:12] if item.trace_id else ""))
        run_id = item.run_id or (resource_id if resource_id.startswith("run_") else "")
        file_id = item.file_id or (resource_id if resource_id.startswith("file_") else "")
        artifact_id = item.artifact_id or (resource_id if resource_id.startswith("art_") else "")
        steps.append(
            GrafanaLineageStep(
                node_id=f"tempo_{index}_{item.trace_id[:12]}",
                title=title[:80],
                stage=stage,
                detail=detail[:80],
                event_type=event_type[:80],
                resource_id=resource_id[:80],
                resource_name=resource_name[:80],
                run_id=run_id[:80],
                file_id=file_id[:80],
                artifact_id=artifact_id[:80],
                trace_id=item.trace_id[:64],
            )
        )
        if len(steps) >= 12:
            break
    return steps


def _otel_attr_value(raw: Any) -> str:
    if isinstance(raw, dict):
        for key in ("stringValue", "string_value", "intValue", "int_value"):
            if raw.get(key) not in (None, ""):
                return str(raw.get(key)).strip()
        inner = raw.get("value")
        if inner is not None and inner is not raw:
            return _otel_attr_value(inner)
        return ""
    return str(raw or "").strip()


def _otel_attrs(node: Any) -> dict[str, str]:
    found: dict[str, str] = {}
    if isinstance(node, list):
        for item in node:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or item.get("name") or "").strip()
            if key:
                found[key] = _otel_attr_value(item.get("value"))
    elif isinstance(node, dict):
        for key, value in node.items():
            found[str(key)] = _otel_attr_value(value)
    return found


def _iter_tempo_span_dicts(payload: Any) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "spanID" in node or "spanId" in node or "span_id" in node:
                spans.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return spans


def _hydrate_trace_from_payload(item: GrafanaTempoTrace, payload: Any) -> GrafanaTempoTrace:
    best = item
    for span in _iter_tempo_span_dicts(payload):
        attrs = _otel_attrs(span.get("attributes") or span.get("tags") or [])
        event_type = attrs.get("event_type") or attrs.get("span.event_type") or item.event_type
        resource_id = attrs.get("resource_id") or attrs.get("span.resource_id") or item.resource_id
        resource_type = attrs.get("resource_type") or attrs.get("span.resource_type") or item.resource_type
        resource_name = attrs.get("resource_name") or attrs.get("span.resource_name") or attrs.get("filename") or item.resource_name
        run_id = attrs.get("run_id") or attrs.get("span.run_id") or item.run_id
        file_id = attrs.get("file_id") or attrs.get("span.file_id") or item.file_id
        artifact_id = attrs.get("artifact_id") or attrs.get("span.artifact_id") or item.artifact_id
        name = str(span.get("name") or span.get("operationName") or item.span_name or "")
        nano = span.get("startTimeUnixNano") or span.get("startTimeUnixNano") or span.get("startTime")
        started = item.started_unix_ms
        try:
            started_int = int(str(nano or "0"))
            if started_int > 10_000_000_000:
                started = started_int // 1_000_000
            elif started_int > 0:
                started = started_int
        except (TypeError, ValueError):
            pass
        if event_type or resource_id or name:
            best = item.model_copy(
                update={
                    "span_name": (name or item.span_name)[:120],
                    "event_type": (event_type or "")[:80],
                    "resource_id": (resource_id or "")[:80],
                    "resource_type": (resource_type or "")[:40],
                    "resource_name": (resource_name or "")[:80],
                    "run_id": (run_id or "")[:80],
                    "file_id": (file_id or "")[:80],
                    "artifact_id": (artifact_id or "")[:80],
                    "started_unix_ms": started,
                }
            )
            if event_type:
                break
    return best


def hydrate_tempo_traces(
    client: httpx.Client,
    base: str,
    token: str,
    tempo_uid: str,
    traces: list[GrafanaTempoTrace],
) -> list[GrafanaTempoTrace]:
    """Fetch span attributes for search hits. Fail-soft: search rows still render."""
    uid = (tempo_uid or "").strip()
    if not uid or not traces:
        return traces
    hydrated: list[GrafanaTempoTrace] = []
    for item in traces[:20]:
        enriched = item
        for path in (
            f"{base}/api/datasources/proxy/uid/{uid}/api/traces/{item.trace_id}",
            f"{base}/api/datasources/proxy/uid/{uid}/api/v2/traces/{item.trace_id}",
        ):
            try:
                response = client.get(path, headers=_headers(token))
            except httpx.HTTPError:
                continue
            if response.status_code >= 400:
                continue
            try:
                payload = response.json()
            except Exception:
                continue
            enriched = _hydrate_trace_from_payload(item, payload)
            if enriched.event_type or enriched.resource_id:
                break
        hydrated.append(enriched)
    return hydrated


def _is_tempo_panel(panel: dict[str, Any]) -> bool:
    ds = panel.get("datasource") if isinstance(panel.get("datasource"), dict) else {}
    kind = str(ds.get("type") or "").lower()
    if kind in {"tempo", "grafana-tempo-datasource"}:
        return True
    targets = panel.get("targets") or []
    return any(isinstance(target, dict) and str(target.get("queryType") or "") == "traceql" for target in targets)


def query_tempo_space_traces(
    client: httpx.Client,
    base: str,
    token: str,
    tempo_ds: dict[str, Any],
    space_id: str,
) -> tuple[list[GrafanaTempoTrace], str | None]:
    uid = str(tempo_ds.get("uid") or "").strip()
    if not uid or not space_id:
        return [], "Grafana Tempo datasource is missing"
    tempo_ref = {"type": str(tempo_ds.get("type") or "tempo"), "uid": uid}
    now_ms = int(time.time() * 1000)
    body = {
        "from": str(now_ms - QUERY_WINDOW_MS),
        "to": str(now_ms),
        "queries": [
            {
                "refId": "A",
                "datasource": tempo_ref,
                "queryType": "traceql",
                "limit": 30,
                "query": _tempo_query_text(space_id),
            }
        ],
    }
    try:
        response = client.post(
            f"{base}/api/ds/query",
            headers={**_headers(token), "Content-Type": "application/json"},
            json=body,
        )
    except httpx.HTTPError as exc:
        logger.warning("Grafana Tempo ds/query transport failed: %s", redact_mcp_text(str(exc)))
        response = None
    traces: list[GrafanaTempoTrace] = []
    query_error = None
    if response is not None and response.status_code < 400:
        try:
            payload = response.json()
        except Exception:
            payload = None
        results = payload.get("results") if isinstance(payload, dict) else None
        if isinstance(results, dict):
            traces = _traces_from_ds_results(results)
            query_error = _result_frame_error(results)
    elif response is not None:
        query_error = _ds_query_error(response)

    if not traces:
        try:
            search = client.get(
                f"{base}/api/datasources/proxy/uid/{uid}/api/search",
                headers=_headers(token),
                params={"limit": 30, "tags": f"space_id={space_id}"},
            )
        except httpx.HTTPError as exc:
            logger.warning("Grafana Tempo search transport failed: %s", redact_mcp_text(str(exc)))
            search = None
        if search is not None and search.status_code < 400:
            try:
                traces = _traces_from_search_payload(search.json())
                if traces:
                    query_error = None
            except Exception:
                pass
        elif search is not None and query_error is None:
            query_error = f"Grafana Tempo search returned {search.status_code}"

    if traces:
        traces = hydrate_tempo_traces(client, base, token, uid, traces)
        return traces, None
    return [], query_error


def load_grafana_tempo_lineage(space_id: str) -> GrafanaTempoLineage:
    """Read Space lineage traces from Grafana Tempo. Never uses the OTLP write token."""
    token = _token()
    if not token:
        return GrafanaTempoLineage(connected=False, error="Grafana service account token is not configured")
    try:
        base = _validated_base()
    except GrafanaVisualError as exc:
        return GrafanaTempoLineage(connected=False, error=str(exc))
    try:
        with _client() as client:
            raw_ds = _get_json(client, f"{base}/api/datasources", token)
            from app.integrations.grafana_otlp import pick_tempo_datasource

            tempo_ds = pick_tempo_datasource(raw_ds) if isinstance(raw_ds, list) else None
            if not tempo_ds:
                return GrafanaTempoLineage(
                    connected=False,
                    error="Grafana Tempo datasource is not visible to this service account",
                )
            traces, error = query_tempo_space_traces(client, base, token, tempo_ds, space_id)
            return GrafanaTempoLineage(
                connected=True,
                trace_count=len(traces),
                traces=traces,
                error=None if traces else error,
            )
    except GrafanaVisualError as exc:
        logger.warning("Grafana Tempo lineage failed closed: %s", redact_mcp_text(str(exc)))
        return GrafanaTempoLineage(connected=False, error=str(exc))
    except httpx.HTTPError as exc:
        logger.warning("Grafana Tempo lineage transport failed: %s", redact_mcp_text(str(exc)))
        return GrafanaTempoLineage(connected=False, error="Grafana HTTP API is unreachable")


def _dashboard_open_url(base: str, uid: str, slug_url: str | None) -> str:
    if slug_url and slug_url.startswith("/"):
        return f"{base}{slug_url}"
    return f"{base}/d/{uid}"


def load_grafana_visual_board(space_id: str | None = None) -> GrafanaVisualBoard:
    """Fetch the StudioTower Space activity dashboard. Does not fall back to Billing/Usage."""
    from app.integrations.grafana_otlp import (
        STUDIO_TOWER_DASHBOARD_TITLE,
        STUDIO_TOWER_DASHBOARD_UID,
        ensure_studiotower_dashboard,
        get_ingest_status,
        pick_metrics_datasource,
        pick_tempo_datasource,
        studiotower_dashboard_model,
    )

    ingest_status, ingest_detail = get_ingest_status()
    token = _token()
    if not token:
        return GrafanaVisualBoard(
            connected=False,
            ingest=ingest_status,
            ingest_detail=ingest_detail,
            error="Grafana service account token is not configured",
        )
    try:
        base = _validated_base()
    except GrafanaVisualError as exc:
        return GrafanaVisualBoard(connected=False, ingest=ingest_status, ingest_detail=ingest_detail, error=str(exc))

    from app.core.config import settings

    preferred_uid = (settings.GRAFANA_DASHBOARD_ID or STUDIO_TOWER_DASHBOARD_UID).strip() or STUDIO_TOWER_DASHBOARD_UID
    try:
        with _client() as client:
            search = _get_json(client, f"{base}/api/search?type=dash-db&limit=50", token)
            if not isinstance(search, list):
                raise GrafanaVisualError("Grafana search returned an unexpected payload")
            hits = [item for item in search if isinstance(item, dict) and item.get("uid") and item.get("type") != "dash-folder"]
            datasources: list[Any] = []
            try:
                raw_ds = _get_json(client, f"{base}/api/datasources", token)
                if isinstance(raw_ds, list):
                    datasources = raw_ds
            except GrafanaVisualError:
                datasources = []

            upsert_error = None
            metrics_ds = pick_metrics_datasource(datasources) if datasources else None
            tempo_ds = pick_tempo_datasource(datasources) if datasources else None
            if metrics_ds:
                upsert_error = ensure_studiotower_dashboard(
                    client, base, token, metrics_ds, tempo_ds=tempo_ds
                )
                if upsert_error is None and not any(str(item.get("uid") or "") == STUDIO_TOWER_DASHBOARD_UID for item in hits):
                    hits.append(
                        {
                            "uid": STUDIO_TOWER_DASHBOARD_UID,
                            "title": "StudioTower Space Activity",
                            "type": "dash-db",
                        }
                    )

            def _is_studiotower(item: dict[str, Any]) -> bool:
                uid = str(item.get("uid") or "")
                title = str(item.get("title") or "").lower()
                return uid == preferred_uid or uid == STUDIO_TOWER_DASHBOARD_UID or "studiotower" in title

            studio_hits = [item for item in hits if _is_studiotower(item)]
            if not studio_hits and preferred_uid:
                studio_hits = [{"uid": preferred_uid, "title": "StudioTower Space Activity", "type": "dash-db"}]

            def _rank(item: dict[str, Any]) -> tuple[int, str]:
                uid = str(item.get("uid") or "")
                title = str(item.get("title") or "").lower()
                if uid == preferred_uid or uid == STUDIO_TOWER_DASHBOARD_UID:
                    return (0, title)
                return (1, title)

            studio_hits.sort(key=_rank)
            dashboards: list[GrafanaDashboardVisual] = []
            empty_fallback: list[GrafanaDashboardVisual] = []
            lineage_flow: list[GrafanaLineageStep] = []
            for hit in studio_hits:
                if len(dashboards) >= MAX_DASHBOARDS:
                    break
                uid = str(hit.get("uid") or "")
                if not RE_DASHBOARD_ID.match(uid):
                    continue
                try:
                    payload = _get_json(client, f"{base}/api/dashboards/uid/{uid}", token)
                except GrafanaVisualError:
                    payload = None
                dash = (payload or {}).get("dashboard") if isinstance(payload, dict) else None
                meta = (payload or {}).get("meta") if isinstance(payload, dict) else None
                if metrics_ds and uid in {preferred_uid, STUDIO_TOWER_DASHBOARD_UID}:
                    model = studiotower_dashboard_model(metrics_ds, tempo_ds=tempo_ds)
                    if not isinstance(dash, dict):
                        dash = model
                        meta = {}
                        hit = {**hit, "title": STUDIO_TOWER_DASHBOARD_TITLE, "url": f"/d/{uid}"}
                    else:
                        existing_panels = list(dash.get("panels") or [])
                        have = {
                            str(panel.get("title") or "").strip().lower()
                            for panel in _flatten_panels(existing_panels)
                        }
                        used_ids = {
                            int(panel["id"])
                            for panel in _flatten_panels(existing_panels)
                            if isinstance(panel.get("id"), int)
                        }
                        next_id = max(used_ids or {0}) + 1
                        extras: list[dict[str, Any]] = []
                        for panel in model.get("panels") or []:
                            if str(panel.get("title") or "").strip().lower() in have:
                                continue
                            extras.append({**panel, "id": next_id})
                            next_id += 1
                        if extras:
                            dash = {**dash, "panels": existing_panels + extras}
                if not isinstance(dash, dict):
                    continue
                title = str(dash.get("title") or hit.get("title") or uid)
                open_url = _dashboard_open_url(base, uid, (meta or {}).get("url") if isinstance(meta, dict) else hit.get("url"))
                dashboard_ds = dash.get("datasource") or (metrics_ds if metrics_ds else None)
                visual_panels: list[GrafanaPanelVisual] = []
                tempo_traces: list[GrafanaTempoTrace] = []
                tempo_error = None
                if tempo_ds and space_id:
                    tempo_traces, tempo_error = query_tempo_space_traces(
                        client, base, token, tempo_ds, space_id
                    )
                    if tempo_traces and not lineage_flow:
                        lineage_flow = build_lineage_flow(tempo_traces, space_id=space_id)
                for panel in _flatten_panels(list(dash.get("panels") or []))[:MAX_PANELS]:
                    panel_id = int(panel["id"])
                    kind = str(panel.get("type") or "panel")
                    series: list[GrafanaSeries] = []
                    table_rows: list[list[str]] = []
                    query_error = None
                    if _is_tempo_panel(panel):
                        table_rows = _traces_table(tempo_traces)
                        query_error = None if table_rows else tempo_error
                    elif kind in QUERY_PANEL_TYPES or panel.get("targets"):
                        series, table_rows, query_error = _query_panel(
                            client, base, token, panel, dashboard_ds, space_id=space_id
                        )
                    visual_panels.append(
                        GrafanaPanelVisual(
                            panel_id=panel_id,
                            title=str(panel.get("title") or f"Panel {panel_id}")[:120],
                            type=kind,
                            image_base64=None,
                            series=series,
                            table_rows=table_rows,
                            query_error=query_error,
                        )
                    )
                if (
                    tempo_ds
                    and space_id
                    and len(visual_panels) < MAX_PANELS
                    and not any(_is_tempo_panel(panel) for panel in _flatten_panels(list(dash.get("panels") or [])))
                ):
                    visual_panels.append(
                        GrafanaPanelVisual(
                            panel_id=90,
                            title="Space lineage traces (Tempo)",
                            type="table",
                            table_rows=_traces_table(tempo_traces),
                            query_error=None if tempo_traces else tempo_error,
                        )
                    )
                visual = GrafanaDashboardVisual(
                    uid=uid,
                    title=title[:160],
                    url=open_url,
                    panels=visual_panels,
                )
                has_data = any(panel.series or panel.table_rows for panel in visual_panels)
                if has_data:
                    dashboards.append(visual)
                elif len(empty_fallback) < MAX_DASHBOARDS:
                    empty_fallback.append(visual)
            if not dashboards:
                dashboards = empty_fallback
            has_points = any(
                panel.series or panel.table_rows
                for dashboard in dashboards
                for panel in dashboard.panels
            ) or bool(lineage_flow)
            if has_points:
                board_error = None
            elif ingest_status != "exporting":
                board_error = ingest_detail
            elif upsert_error:
                board_error = upsert_error
            else:
                board_error = "StudioTower dashboard is connected, but this Space has no exported events in the last 7 days"
            return GrafanaVisualBoard(
                connected=True,
                source="grafana-http-api",
                dashboard_count=len(hits),
                ingest=ingest_status,
                ingest_detail=ingest_detail,
                dashboards=dashboards,
                lineage_flow=lineage_flow,
                error=board_error,
            )
    except GrafanaVisualError as exc:
        logger.warning("Grafana visual board failed closed: %s", redact_mcp_text(str(exc)))
        return GrafanaVisualBoard(connected=False, ingest=ingest_status, ingest_detail=ingest_detail, error=str(exc))
    except httpx.HTTPError as exc:
        logger.warning("Grafana visual transport failed: %s", redact_mcp_text(str(exc)))
        return GrafanaVisualBoard(
            connected=False,
            ingest=ingest_status,
            ingest_detail=ingest_detail,
            error="Grafana HTTP API is unreachable",
        )
