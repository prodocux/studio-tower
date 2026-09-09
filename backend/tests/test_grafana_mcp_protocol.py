import json

import httpx
import pytest
from app.agent.brain import AgentBrain
from app.core.config import settings
from app.integrations.grafana_mcp import HOSTED_GRAFANA_CLOUD_MCP_ENDPOINT, grafana_mcp
from app.models.run import Run, RunStatus
from app.services.storage import store

FAKE_TOKEN = "glc_test_token_should_not_leak"


def _mcp_handler(captured: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == HOSTED_GRAFANA_CLOUD_MCP_ENDPOINT
        assert request.headers.get("authorization") == f"Bearer {FAKE_TOKEN}"
        body = json.loads(request.content.decode("utf-8"))
        captured.append(body)
        method = body.get("method")
        if method == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "result": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "mcp-grafana", "version": "test"},
                    },
                },
                headers={"Mcp-Session-Id": "sess-test-1"},
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "result": {
                        "tools": [
                            {"name": "search_dashboards", "description": "Search Grafana dashboards"},
                            {"name": "list_datasources", "description": "List Grafana datasources"},
                        ]
                    },
                },
            )
        if method == "tools/call":
            name = (body.get("params") or {}).get("name")
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps({"tool": name, "query": "studiotower", "count": 1}),
                            }
                        ]
                    },
                },
            )
        return httpx.Response(400, json={"jsonrpc": "2.0", "error": {"message": "unexpected method"}})

    return handler


def _enable_mcp(monkeypatch) -> None:
    monkeypatch.setattr(settings, "GRAFANA_MCP_ENDPOINT", HOSTED_GRAFANA_CLOUD_MCP_ENDPOINT)
    monkeypatch.setattr(settings, "GRAFANA_CLOUD_API_KEY", FAKE_TOKEN)
    monkeypatch.setattr(settings, "GRAFANA_BASE_URL", "https://example.grafana.net")


@pytest.fixture(autouse=True)
def _clean_store():
    store.clear()
    yield
    store.clear()


def test_grafana_allowed_hosts_parses_plain_hostname(monkeypatch):
    from app.core.config import Settings

    assert Settings.parse_grafana_allowed_hosts("loftyladybug3305.grafana.net") == [
        "loftyladybug3305.grafana.net"
    ]
    monkeypatch.setenv("GRAFANA_ALLOWED_HOSTS", "loftyladybug3305.grafana.net")
    loaded = Settings()
    assert loaded.GRAFANA_ALLOWED_HOSTS == ["loftyladybug3305.grafana.net"]


def test_unconfigured_mcp_makes_zero_http_calls():
    called = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(str(request.url))
        return httpx.Response(500)

    grafana_mcp._http_transport = httpx.MockTransport(handler)
    result = grafana_mcp.query_runtime_observability(run_id="run_x", trace_id="trc_x", space_id="spc_x")
    assert result.connected is False
    assert result.tools_called == []
    assert called == []


def test_legacy_grafana_com_api_mcp_endpoint_is_rewritten(monkeypatch):
    monkeypatch.setattr(settings, "GRAFANA_MCP_ENDPOINT", "https://grafana.com/api/mcp")
    monkeypatch.setattr(settings, "GRAFANA_CLOUD_API_KEY", FAKE_TOKEN)
    assert grafana_mcp.endpoint == HOSTED_GRAFANA_CLOUD_MCP_ENDPOINT


def test_mcp_initialize_tools_list_and_tools_call(monkeypatch):
    captured: list[dict] = []
    _enable_mcp(monkeypatch)
    grafana_mcp._http_transport = httpx.MockTransport(_mcp_handler(captured))

    result = grafana_mcp.query_runtime_observability(
        run_id="run_mcp_001",
        trace_id="trc_mcp_001",
        space_id="spc_mcp_001",
    )
    methods = [item.get("method") for item in captured]
    assert methods[:3] == ["initialize", "notifications/initialized", "tools/list"]
    assert "tools/call" in methods
    called_names = [
        (item.get("params") or {}).get("name")
        for item in captured
        if item.get("method") == "tools/call"
    ]
    assert "search_dashboards" in called_names
    assert result.connected is True
    assert "search_dashboards" in result.tools_called
    assert FAKE_TOKEN not in json.dumps(result.model_dump())


def test_mcp_parses_streamable_http_sse(monkeypatch):
    _enable_mcp(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        method = body.get("method")
        rpc_id = body.get("id")
        if method == "initialize":
            sse = (
                "event: message\n"
                f'data: {{"jsonrpc":"2.0","id":{rpc_id},"result":{{"protocolVersion":"2025-03-26","capabilities":{{}},"serverInfo":{{"name":"mcp-grafana"}}}}}}\n\n'
            )
            return httpx.Response(
                200,
                content=sse.encode("utf-8"),
                headers={"content-type": "text/event-stream", "Mcp-Session-Id": "sess-sse"},
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            sse = (
                "event: message\n"
                f'data: {{"jsonrpc":"2.0","id":{rpc_id},"result":{{"tools":[{{"name":"search_dashboards"}}]}}}}\n\n'
            )
            return httpx.Response(200, content=sse.encode("utf-8"), headers={"content-type": "text/event-stream"})
        if method == "tools/call":
            sse = (
                "event: message\n"
                f'data: {{"jsonrpc":"2.0","id":{rpc_id},"result":{{"content":[{{"type":"text","text":"dashboards: 1"}}]}}}}\n\n'
            )
            return httpx.Response(200, content=sse.encode("utf-8"), headers={"content-type": "text/event-stream"})
        return httpx.Response(400)

    grafana_mcp._http_transport = httpx.MockTransport(handler)
    result = grafana_mcp.query_runtime_observability(run_id="run_sse", trace_id="trc_sse", space_id="spc_sse")
    assert result.connected is True
    assert result.tools_called == ["search_dashboards"]


def test_mcp_401_fail_closed_does_not_invent_grafana_data(monkeypatch):
    _enable_mcp(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    grafana_mcp._http_transport = httpx.MockTransport(handler)
    result = grafana_mcp.query_runtime_observability(run_id="run_401", trace_id="trc_401", space_id="spc_401")
    assert result.connected is False
    assert result.tools_called == []
    assert result.error == "Grafana MCP authentication failed"


def test_diagnosis_pipeline_issues_grafana_mcp_tools_call(monkeypatch):
    captured: list[dict] = []
    _enable_mcp(monkeypatch)
    grafana_mcp._http_transport = httpx.MockTransport(_mcp_handler(captured))

    space_id = "space_mcp_diag_001"
    run = Run(
        space_id=space_id,
        project_tag="stunts",
        status=RunStatus.FAILED,
        prompt="Execute aircraft test",
        created_by="alice_01",
    )
    store.save_run(run)
    grafana_mcp.record_simulated_failure_trace(run.trace_id, run.run_id, space_id)

    diagnosis = AgentBrain.diagnose_run_with_grafana(run, space_id)
    assert "🚨 **Grafana MCP Telemetry Diagnosis" in diagnosis
    assert "pdx_resource_constraint_evaluator" in diagnosis
    assert "Grafana Cloud MCP**: connected" in diagnosis
    assert FAKE_TOKEN not in diagnosis
    methods = [item.get("method") for item in captured]
    assert "initialize" in methods
    assert "tools/list" in methods
    assert any(
        item.get("method") == "tools/call" and (item.get("params") or {}).get("name") == "search_dashboards"
        for item in captured
    )
    tool_calls = [item for item in captured if item.get("method") == "tools/call"]
    assert tool_calls[0]["params"]["arguments"]["query"] == "studiotower"
