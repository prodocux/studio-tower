import json
import logging
import re
import threading
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

HOSTED_GRAFANA_CLOUD_MCP_ENDPOINT = "https://mcp.grafana.com/mcp"
LEGACY_GRAFANA_MCP_ENDPOINTS = frozenset(
    {
        "https://grafana.com/api/mcp",
        "http://grafana.com/api/mcp",
    }
)
MCP_PROTOCOL_VERSION = "2025-03-26"
PREFERRED_READ_TOOLS = (
    "search_dashboards",
    "list_datasources",
    "search_folders",
    "list_incidents",
)
_SECRET_RE = re.compile(
    r"(glc_[A-Za-z0-9._-]+|glsa_[A-Za-z0-9._-]+|Bearer\s+\S+)",
    re.IGNORECASE,
)


def redact_mcp_text(value: str) -> str:
    return _SECRET_RE.sub("[redacted]", value)


def normalize_grafana_mcp_endpoint(raw: str | None) -> str:
    endpoint = (raw or "").strip()
    if not endpoint:
        return ""
    stripped = endpoint.rstrip("/")
    if stripped in LEGACY_GRAFANA_MCP_ENDPOINTS:
        logger.warning(
            "Rewriting deprecated GRAFANA_MCP_ENDPOINT %s to hosted Grafana Cloud MCP %s",
            stripped,
            HOSTED_GRAFANA_CLOUD_MCP_ENDPOINT,
        )
        return HOSTED_GRAFANA_CLOUD_MCP_ENDPOINT
    return endpoint


class GrafanaMCPError(Exception):
    """Fail-closed Grafana MCP protocol or transport error. Never includes credentials."""


class GrafanaMCPRuntimeResult(BaseModel):
    connected: bool = False
    endpoint: str = ""
    tools_listed: list[str] = Field(default_factory=list)
    tools_called: list[str] = Field(default_factory=list)
    summaries: list[str] = Field(default_factory=list)
    error: str | None = None


class TraceSpan(BaseModel):
    span_id: str
    trace_id: str
    name: str
    start_time: datetime = Field(default_factory=lambda: datetime.now(UTC))
    duration_ms: int = 0
    status: str = "ok"  # "ok", "error", "warning"
    attributes: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None


class GrafanaTraceBundle(BaseModel):
    trace_id: str
    run_id: str
    space_id: str
    service_name: str = "studiotower-api"
    total_spans: int
    spans: list[TraceSpan]
    grafana_dashboard_url: str | None = None   # None when Grafana not configured or no real trace
    has_real_telemetry: bool = False            # False until verified by TelemetryService
    is_local_diagnostic: bool = False           # True when unverified local diagnostic spans exist


class GrafanaMCPClient:
    """
    Grafana Cloud MCP client.

    Local span recording remains fail-closed and in-process. When
    GRAFANA_MCP_ENDPOINT and a Grafana token are configured, diagnosis also
    speaks MCP Streamable HTTP JSON-RPC (initialize, tools/list, tools/call)
    against grafana/mcp-grafana or the hosted Grafana Cloud MCP endpoint.
    """

    def __init__(self):
        self._traces: dict[str, list[TraceSpan]] = {}
        self._simulated_trace_ids: set[str] = set()
        self._rpc_lock = threading.RLock()
        self._rpc_id = 0
        self._mcp_session_id: str | None = None
        self._mcp_initialized = False
        self._http_transport: httpx.BaseTransport | None = None

    def reset_runtime_state(self) -> None:
        """Drop MCP session state. Does not clear recorded local spans."""
        with self._rpc_lock:
            self._rpc_id = 0
            self._mcp_session_id = None
            self._mcp_initialized = False
            self._http_transport = None

    @property
    def endpoint(self) -> str:
        from app.core.config import settings

        return normalize_grafana_mcp_endpoint(
            settings.GRAFANA_MCP_ENDPOINT or ""
        )

    @property
    def api_key(self) -> str:
        return self._mcp_token()

    def _mcp_token(self) -> str:
        from app.core.config import settings

        for value in (
            settings.GRAFANA_MCP_ACCESS_TOKEN,
            settings.GRAFANA_SERVICE_ACCOUNT_TOKEN,
            settings.GRAFANA_CLOUD_API_KEY,
        ):
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _grafana_url_header(self) -> str:
        from app.core.config import settings

        return (settings.GRAFANA_BASE_URL or "").strip().rstrip("/")

    def _timeout_seconds(self) -> float:
        from app.core.config import settings

        return float(settings.GRAFANA_MCP_TIMEOUT_SECONDS)

    @property
    def is_mcp_configured(self) -> bool:
        return bool(self.endpoint and self._mcp_token())

    def record_span(
        self,
        trace_id: str,
        span_id: str,
        name: str,
        duration_ms: int,
        status: str = "ok",
        attributes: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> TraceSpan:
        span = TraceSpan(
            span_id=span_id,
            trace_id=trace_id,
            name=name,
            duration_ms=duration_ms,
            status=status,
            attributes=attributes or {},
            error_code=error_code,
            error_message=error_message,
        )
        if trace_id not in self._traces:
            self._traces[trace_id] = []
        self._traces[trace_id].append(span)
        return span

    def query_trace(self, trace_id: str, run_id: str = "", space_id: str = "") -> GrafanaTraceBundle:
        """
        Query execution trace.
        Validates Space/Run/Trace association and queries TelemetryService for authentic telemetry.
        Note: query_trace does not receive caller user identity; caller API endpoints must
        enforce space membership and access authorization prior to invoking this method.

        has_real_telemetry is strictly False by default.
        It is ONLY promoted to True if TelemetryService.get_run_trace_bundle() validates
        Space tenancy, Run existence, backend trace contract, and telemetry_status == 'available'.
        Local unverified spans are retained for diagnostic inspection but marked has_real_telemetry=False.
        """
        spans = list(self._traces.get(trace_id, []))
        is_simulated = (trace_id in self._simulated_trace_ids)
        has_real = False
        dashboard_url = None
        is_local = len(spans) > 0 and not is_simulated

        # Authoritative deep-link & telemetry verification:
        # MCP never crafts Grafana URLs or asserts has_real_telemetry on its own.
        # Authentic status is only derived through TelemetryService.get_run_trace_bundle() which validates
        # Space tenancy, run existence, genuine execution nodes, and telemetry_status == 'available'.
        if space_id and run_id and not is_simulated:
            try:
                from app.services.telemetry_service import TelemetryService
                verified_bundle = TelemetryService.get_run_trace_bundle(space_id=space_id, run_id=run_id)
                if verified_bundle and verified_bundle.trace_id == trace_id:
                    has_real = verified_bundle.has_real_telemetry
                    dashboard_url = verified_bundle.grafana_dashboard_url
                    if not spans and verified_bundle.spans:
                        spans = [
                            TraceSpan(
                                span_id=n.span_id,
                                trace_id=verified_bundle.trace_id,
                                name=n.name,
                                duration_ms=n.duration_ms,
                                status="error" if n.status == "error" else "ok",
                                attributes=n.attributes,
                                error_message=n.error_message,
                                error_code=n.error_code or (n.attributes.get("error_code") if isinstance(n.attributes, dict) else None),
                            )
                            for n in verified_bundle.spans
                        ]
            except Exception as e:
                logger.debug("TelemetryService trace bundle check skipped/failed: %s", e)
                has_real = False
                dashboard_url = None

        return GrafanaTraceBundle(
            trace_id=trace_id,
            run_id=run_id,
            space_id=space_id,
            total_spans=len(spans),
            spans=spans,
            grafana_dashboard_url=dashboard_url,
            has_real_telemetry=has_real,
            is_local_diagnostic=is_local and not has_real,
        )

    def record_simulated_failure_trace(self, trace_id: str, run_id: str, space_id: str) -> GrafanaTraceBundle:
        """
        Record a controlled failure trace for the simulate-failure demo path.
        These spans are genuinely recorded (not fabricated query results) and represent
        a real resource constraint conflict detected by the PDX engine.
        """
        from prodocux_kernel import __version__ as kernel_version

        self._simulated_trace_ids.add(trace_id)
        spans = [
            TraceSpan(
                span_id=f"spn_auth_{trace_id[-4:]}",
                trace_id=trace_id,
                name="firebase_auth_verify",
                duration_ms=20,
                status="ok",
                attributes={"auth.mode": "token"},
            ),
            TraceSpan(
                span_id=f"spn_prodocux_{trace_id[-4:]}",
                trace_id=trace_id,
                name="prodocux_pdf_extract",
                duration_ms=90,
                status="ok",
                attributes={"prodocux.version": f"prodocux=={kernel_version}"},
            ),
            TraceSpan(
                span_id=f"spn_gemini_{trace_id[-4:]}",
                trace_id=trace_id,
                name="gemini_genai_reasoning",
                duration_ms=750,
                status="ok",
                attributes={"ai.model": "gemini-3.6-flash", "ai.sdk": "google-genai"},
            ),
            TraceSpan(
                span_id=f"spn_pdx_gate_{trace_id[-4:]}",
                trace_id=trace_id,
                name="pdx_resource_constraint_evaluator",
                duration_ms=180,
                status="error",
                attributes={
                    "conflict.asset": "Sacrificial Test Aircraft",
                    "conflict.unit_1": "Disaster Perimeter Unit",
                    "conflict.unit_2": "Hangar Destructive Rigging",
                    "gate.rule_id": "EXCLUSIVE_TECH_LOCK_VIOLATION",
                },
                error_message=(
                    "Resource Conflict: 'Sacrificial Test Aircraft' is simultaneously "
                    "allocated to Unit 1 (flood drop) and Unit 2 (engine burn) "
                    "in overlapping call windows."
                ),
            ),
        ]
        self._traces[trace_id] = spans

        return GrafanaTraceBundle(
            trace_id=trace_id,
            run_id=run_id,
            space_id=space_id,
            total_spans=len(spans),
            spans=spans,
            grafana_dashboard_url=None,
            has_real_telemetry=False,
        )

    def warmup(self) -> GrafanaMCPRuntimeResult:
        """Startup probe. Never raises; production boot stays fail-closed."""
        if not self.is_mcp_configured:
            logger.info("Grafana Cloud MCP is not configured; diagnosis will use local telemetry only")
            return GrafanaMCPRuntimeResult(connected=False)
        try:
            tools = self.list_mcp_tools()
            logger.info("Grafana Cloud MCP connected endpoint=%s tools=%s", self.endpoint, len(tools))
            return GrafanaMCPRuntimeResult(
                connected=True,
                endpoint=self.endpoint,
                tools_listed=tools,
            )
        except GrafanaMCPError as exc:
            logger.warning("Grafana Cloud MCP warmup failed (fail-closed): %s", exc)
            return GrafanaMCPRuntimeResult(connected=False, endpoint=self.endpoint, error=str(exc))

    def list_mcp_tools(self) -> list[str]:
        self._ensure_initialized()
        payload = self._rpc("tools/list", {})
        tools = payload.get("tools") or []
        names: list[str] = []
        for tool in tools:
            if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                names.append(tool["name"])
        return names

    def call_mcp_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        if not name or not isinstance(name, str):
            raise GrafanaMCPError("MCP tool name is required")
        self._ensure_initialized()
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        if not isinstance(result, dict):
            raise GrafanaMCPError("MCP tools/call returned a non-object result")
        if result.get("isError") is True:
            raise GrafanaMCPError(f"MCP tool {name} returned isError")
        return result

    def query_runtime_observability(
        self,
        run_id: str = "",
        trace_id: str = "",
        space_id: str = "",
    ) -> GrafanaMCPRuntimeResult:
        """
        Call Grafana Cloud MCP at runtime. Unconfigured deployments make zero HTTP calls.
        Configured deployments must issue initialize + tools/list + tools/call.
        Fail closed: never invent Grafana data when the MCP server is unreachable.
        """
        if not self.is_mcp_configured:
            return GrafanaMCPRuntimeResult(connected=False)

        endpoint = self.endpoint
        try:
            tools = self.list_mcp_tools()
            selected = [tool for tool in PREFERRED_READ_TOOLS if tool in tools][:2]
            if not selected and tools:
                selected = [tools[0]]
            if not selected:
                raise GrafanaMCPError("Grafana MCP tools/list returned no tools")

            called: list[str] = []
            summaries: list[str] = []
            for tool_name in selected:
                arguments = self._arguments_for_tool(tool_name, run_id=run_id, trace_id=trace_id, space_id=space_id)
                raw = self.call_mcp_tool(tool_name, arguments)
                called.append(tool_name)
                summaries.append(self._summarize_tool_result(tool_name, raw))

            return GrafanaMCPRuntimeResult(
                connected=True,
                endpoint=endpoint,
                tools_listed=tools,
                tools_called=called,
                summaries=summaries,
            )
        except GrafanaMCPError as exc:
            logger.warning("Grafana Cloud MCP runtime query failed (fail-closed): %s", exc)
            return GrafanaMCPRuntimeResult(connected=False, endpoint=endpoint, error=str(exc))
        except httpx.HTTPError as exc:
            logger.warning("Grafana Cloud MCP transport failed (fail-closed): %s", type(exc).__name__)
            return GrafanaMCPRuntimeResult(
                connected=False,
                endpoint=endpoint,
                error="Grafana MCP HTTP transport failed",
            )

    def _arguments_for_tool(
        self,
        tool_name: str,
        run_id: str,
        trace_id: str,
        space_id: str,
    ) -> dict[str, Any]:
        if tool_name == "search_dashboards":
            return {"query": "studiotower"}
        if tool_name == "search_folders":
            return {"query": "studiotower"}
        if tool_name == "list_datasources":
            return {}
        if tool_name == "list_incidents":
            return {}
        return {"query": " ".join(part for part in (run_id, trace_id, space_id) if part).strip() or "studiotower"}

    def _summarize_tool_result(self, tool_name: str, raw: dict[str, Any]) -> str:
        text = self._extract_text_content(raw)
        compact = redact_mcp_text(text).replace("\n", " ").strip()
        if len(compact) > 240:
            compact = compact[:237] + "..."
        if not compact:
            compact = "empty result"
        return f"Grafana Cloud MCP tools/call {tool_name}: {compact}"

    def _extract_text_content(self, raw: dict[str, Any]) -> str:
        content = raw.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") in (None, "text"):
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                elif isinstance(item, str):
                    parts.append(item)
            return "\n".join(parts)
        return json.dumps(raw, ensure_ascii=False, default=str)

    def _ensure_initialized(self) -> None:
        if not self.is_mcp_configured:
            raise GrafanaMCPError("Grafana MCP endpoint or token is not configured")
        if self._mcp_initialized:
            return
        with self._rpc_lock:
            if self._mcp_initialized:
                return
            result = self._rpc(
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "studiotower", "version": "0.1.0"},
                },
                record_session=True,
            )
            if not isinstance(result, dict):
                raise GrafanaMCPError("Grafana MCP initialize returned a non-object result")
            self._rpc("notifications/initialized", {}, notification=True)
            self._mcp_initialized = True

    def _next_rpc_id(self) -> int:
        with self._rpc_lock:
            self._rpc_id += 1
            return self._rpc_id

    def _rpc(
        self,
        method: str,
        params: dict[str, Any],
        *,
        notification: bool = False,
        record_session: bool = False,
    ) -> dict[str, Any]:
        endpoint = self.endpoint
        token = self._mcp_token()
        if not endpoint or not token:
            raise GrafanaMCPError("Grafana MCP endpoint or token is not configured")

        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            payload["id"] = self._next_rpc_id()

        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        }
        grafana_url = self._grafana_url_header()
        if grafana_url:
            headers["X-Grafana-URL"] = grafana_url
        if self._mcp_session_id:
            headers["Mcp-Session-Id"] = self._mcp_session_id

        try:
            with httpx.Client(
                timeout=self._timeout_seconds(),
                follow_redirects=True,
                transport=self._http_transport,
            ) as client:
                response = client.post(endpoint, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise GrafanaMCPError(f"Grafana MCP request failed ({type(exc).__name__})") from exc

        session_id = response.headers.get("mcp-session-id") or response.headers.get("Mcp-Session-Id")
        if record_session and session_id:
            self._mcp_session_id = session_id

        if response.status_code in (401, 403):
            raise GrafanaMCPError("Grafana MCP authentication failed")
        if response.status_code >= 400:
            raise GrafanaMCPError(f"Grafana MCP HTTP {response.status_code}")

        if notification and not response.content:
            return {}

        body = _parse_mcp_http_body(response)
        if "error" in body:
            message = body["error"]
            if isinstance(message, dict):
                message = message.get("message") or "JSON-RPC error"
            raise GrafanaMCPError(redact_mcp_text(str(message)))
        result = body.get("result")
        if notification:
            return result if isinstance(result, dict) else {}
        if not isinstance(result, dict):
            raise GrafanaMCPError(f"Grafana MCP {method} returned no JSON-RPC result")
        return result


def _parse_mcp_http_body(response: httpx.Response) -> dict[str, Any]:
    content_type = (response.headers.get("content-type") or "").lower()
    raw = response.text or ""
    if "text/event-stream" in content_type or raw.lstrip().startswith("event:"):
        parsed: dict[str, Any] | None = None
        for line in raw.splitlines():
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if not chunk or chunk == "[DONE]":
                continue
            try:
                obj = json.loads(chunk)
            except json.JSONDecodeError as exc:
                raise GrafanaMCPError("Grafana MCP SSE payload is not JSON") from exc
            if isinstance(obj, dict) and ("result" in obj or "error" in obj):
                parsed = obj
        if parsed is None:
            raise GrafanaMCPError("Grafana MCP SSE stream contained no JSON-RPC message")
        return parsed
    if not raw.strip():
        return {}
    try:
        obj = response.json()
    except json.JSONDecodeError as exc:
        raise GrafanaMCPError("Grafana MCP response is not JSON") from exc
    if not isinstance(obj, dict):
        raise GrafanaMCPError("Grafana MCP response is not a JSON object")
    return obj


grafana_mcp = GrafanaMCPClient()
