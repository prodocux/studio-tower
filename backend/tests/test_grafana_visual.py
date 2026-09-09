import json

import httpx
import pytest
from app.core.config import settings
from app.integrations.grafana_visual import GrafanaTempoTrace, _hydrate_trace_from_payload, build_lineage_flow, load_grafana_visual_board
from app.main import app
from app.models.space import MembershipRole, Space, SpaceKind
from app.models.user import User
from app.services.storage import store
from fastapi.testclient import TestClient

client = TestClient(app)
ALICE = {"Authorization": "Bearer dev:alice_01:alice@example.com:Alice"}


def test_build_lineage_flow_orders_file_before_run():
    steps = build_lineage_flow(
        [
            GrafanaTempoTrace(
                trace_id="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                event_type="run.started",
                resource_id="run_2",
                started_unix_ms=200,
            ),
            GrafanaTempoTrace(
                trace_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                event_type="file.uploaded",
                resource_id="file_1",
                started_unix_ms=100,
            ),
        ]
    )
    assert [step.title for step in steps] == ["File ingested", "AI run started"]
    assert [step.stage for step in steps] == ["file", "run"]


def test_build_lineage_flow_uses_asset_name_not_raw_id():
    steps = build_lineage_flow(
        [
            GrafanaTempoTrace(
                trace_id="cccccccccccccccccccccccccccccccc",
                event_type="run.completed",
                resource_type="artifact",
                resource_id="art_fa6a4caee3e23a5c",
                resource_name="shoot_schedule.csv",
                run_id="run_9",
                file_id="file_1",
                started_unix_ms=50,
            )
        ]
    )
    assert steps[0].title == "shoot_schedule.csv"
    assert steps[0].detail == "AI run completed"
    assert steps[0].stage == "artifact"
    assert steps[0].resource_id == "art_fa6a4caee3e23a5c"
    assert steps[0].resource_name == "shoot_schedule.csv"
    assert steps[0].run_id == "run_9"
    assert steps[0].file_id == "file_1"
    assert steps[0].artifact_id == "art_fa6a4caee3e23a5c"


def test_build_lineage_flow_labels_catalog_artifact(monkeypatch):
    class FakeArt:
        filename = "call_sheet.csv"

    monkeypatch.setattr(
        "app.services.storage.store.get_artifact",
        lambda space_id, aid: FakeArt() if aid.startswith("art_") else None,
    )
    steps = build_lineage_flow(
        [
            GrafanaTempoTrace(
                trace_id="dddddddddddddddddddddddddddddddd",
                event_type="run.completed",
                resource_type="artifact",
                resource_id="art_oldspan",
                started_unix_ms=1,
            )
        ],
        space_id="spc_corr",
    )
    assert steps[0].title == "call_sheet.csv"
    assert steps[0].detail == "AI run completed"
    assert steps[0].resource_id == "art_oldspan"


def test_build_lineage_flow_labels_file_art_when_artifact_record_missing(monkeypatch):
    class FakeFile:
        space_id = "spc_corr"
        filename = "Act_III_Scene_Breakdown.pdf"

    monkeypatch.setattr("app.services.storage.store.get_artifact", lambda space_id, aid: None)
    monkeypatch.setattr(
        "app.services.storage.store.get_file",
        lambda fid: FakeFile() if fid == "file_art_5a0f8d3b93c6af81" else None,
    )
    steps = build_lineage_flow(
        [
            GrafanaTempoTrace(
                trace_id="ffffffffffffffffffffffffffffffff",
                event_type="run.completed",
                resource_id="art_5a0f8d3b93c6af81",
                started_unix_ms=1,
            )
        ],
        space_id="spc_corr",
    )
    assert steps[0].title == "Act_III_Scene_Breakdown.pdf"
    assert steps[0].detail == "AI run completed"
    assert steps[0].stage == "artifact"


def test_hydrate_trace_copies_correlation_attributes():
    item = GrafanaTempoTrace(trace_id="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee")
    payload = {
        "batches": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "spanID": "abc123",
                                "name": "studiotower.space.event",
                                "attributes": [
                                    {"key": "event_type", "value": {"stringValue": "run.completed"}},
                                    {"key": "resource_type", "value": {"stringValue": "artifact"}},
                                    {"key": "resource_id", "value": {"stringValue": "art_1"}},
                                    {"key": "resource_name", "value": {"stringValue": "shoot_schedule.csv"}},
                                    {"key": "run_id", "value": {"stringValue": "run_9"}},
                                    {"key": "file_id", "value": {"stringValue": "file_1"}},
                                    {"key": "artifact_id", "value": {"stringValue": "art_1"}},
                                ],
                            }
                        ]
                    }
                ]
            }
        ]
    }
    out = _hydrate_trace_from_payload(item, payload)
    assert out.event_type == "run.completed"
    assert out.resource_type == "artifact"
    assert out.resource_id == "art_1"
    assert out.resource_name == "shoot_schedule.csv"
    assert out.run_id == "run_9"
    assert out.file_id == "file_1"
    assert out.artifact_id == "art_1"


@pytest.fixture(autouse=True)
def _clean():
    store.clear()
    yield
    store.clear()


def _enable_grafana(monkeypatch) -> None:
    monkeypatch.setattr(settings, "GRAFANA_BASE_URL", "https://loftyladybug3305.grafana.net")
    monkeypatch.setattr(settings, "GRAFANA_ALLOWED_HOSTS", ["loftyladybug3305.grafana.net"])
    monkeypatch.setattr(settings, "GRAFANA_SERVICE_ACCOUNT_TOKEN", "glsa_test_visual_token")
    monkeypatch.setattr(settings, "GRAFANA_DASHBOARD_ID", "studiotower-monitor")


def _grafana_http(request: httpx.Request) -> httpx.Response:
    assert "glsa_" not in str(request.url)
    auth = request.headers.get("authorization", "")
    assert auth.startswith("Bearer glsa_")
    path = request.url.path
    if path == "/api/org":
        return httpx.Response(200, json={"name": "Main Org."})
    if path == "/api/datasources":
        return httpx.Response(
            200,
            json=[
                {
                    "name": "Grafana Cloud Billing/Usage",
                    "type": "prometheus",
                    "uid": "grafanacloud-usage",
                    "url": "https://billing.grafana.net",
                },
                {
                    "name": "grafanacloud-prom",
                    "type": "prometheus",
                    "uid": "prom",
                    "url": "https://prometheus-prod-36-prod-us-central-0.grafana.net/api/prom",
                    "user": "123456",
                },
            ],
        )
    if path == "/api/dashboards/db":
        return httpx.Response(200, json={"status": "success", "uid": "studiotower-monitor"})
    if path == "/api/search":
        return httpx.Response(
            200,
            json=[
                {
                    "uid": "studiotower-monitor",
                    "title": "StudioTower Runtime",
                    "type": "dash-db",
                    "url": "/d/studiotower-monitor/studiotower-runtime",
                }
            ],
        )
    if path == "/api/dashboards/uid/studiotower-monitor":
        return httpx.Response(
            200,
            json={
                "meta": {"url": "/d/studiotower-monitor/studiotower-runtime"},
                "dashboard": {
                    "title": "StudioTower Runtime",
                    "uid": "studiotower-monitor",
                    "panels": [
                        {
                            "id": 2,
                            "title": "Request rate",
                            "type": "timeseries",
                            "datasource": {"type": "prometheus", "uid": "prom"},
                            "targets": [
                                {
                                    "refId": "A",
                                    "expr": "sum(rate(http_requests_total[5m]))",
                                    "datasource": {"uid": "prom", "type": "prometheus"},
                                }
                            ],
                        },
                        {
                            "id": 3,
                            "title": "Highest cardinality metrics",
                            "type": "table",
                            "datasource": {"type": "prometheus", "uid": "prom"},
                            "targets": [
                                {
                                    "refId": "B",
                                    "expr": "topk(5, count by (__name__)({__name__=~\".+\"}))",
                                    "format": "table",
                                    "instant": True,
                                    "datasource": {"uid": "prom", "type": "prometheus"},
                                }
                            ],
                        },
                    ],
                },
            },
        )
    if path == "/api/ds/query":
        body = json.loads(request.content.decode("utf-8"))
        query = body["queries"][0]
        if query.get("queryType") == "traceql":
            return httpx.Response(200, json={"results": {"A": {"frames": []}}})
        expr = str(query.get("expr") or "")
        if "topk" in expr:
            return httpx.Response(
                200,
                json={
                    "results": {
                        "B": {
                            "frames": [
                                {
                                    "schema": {
                                        "fields": [
                                            {"name": "Metric", "type": "string"},
                                            {"name": "Value", "type": "number"},
                                        ]
                                    },
                                    "data": {"values": [["http_requests_total", "up"], [12.0, 3.0]]},
                                }
                            ]
                        }
                    }
                },
            )
        series_name = "requests" if "http_requests_total" in expr else "events"
        return httpx.Response(
            200,
            json={
                "results": {
                    "A": {
                        "frames": [
                            {
                                "schema": {
                                    "fields": [
                                        {"name": "Time", "type": "time"},
                                        {"name": series_name, "type": "number"},
                                    ]
                                },
                                "data": {"values": [[1.0, 2.0, 3.0], [10.0, 20.0, 15.0]]},
                            }
                        ]
                    }
                }
            },
        )
    return httpx.Response(404, json={"message": path})


def _patch_client(monkeypatch) -> None:
    transport = httpx.MockTransport(_grafana_http)
    real_client = httpx.Client
    monkeypatch.setattr(
        "app.integrations.grafana_visual.httpx.Client",
        lambda **kwargs: real_client(
            transport=transport,
            **{key: value for key, value in kwargs.items() if key != "transport"},
        ),
    )


def test_load_grafana_visual_board_queries_grafana_http_api(monkeypatch):
    _enable_grafana(monkeypatch)
    _patch_client(monkeypatch)
    board = load_grafana_visual_board()
    assert board.connected is True
    assert board.source == "grafana-http-api"
    assert board.dashboards[0].uid == "studiotower-monitor"
    assert board.dashboards[0].panels[0].title == "Request rate"
    assert board.dashboards[0].panels[0].series[0].points == [[1.0, 10.0], [2.0, 20.0], [3.0, 15.0]]
    assert board.dashboards[0].panels[1].title == "Highest cardinality metrics"
    assert board.dashboards[0].panels[1].table_rows[0] == ["Metric", "Value"]
    assert board.dashboards[0].panels[1].table_rows[1][0] == "http_requests_total"
    assert "glsa_" not in board.model_dump_json()


def test_load_grafana_visual_board_fail_closed_without_token(monkeypatch):
    monkeypatch.setattr(settings, "GRAFANA_BASE_URL", "https://loftyladybug3305.grafana.net")
    monkeypatch.setattr(settings, "GRAFANA_ALLOWED_HOSTS", ["loftyladybug3305.grafana.net"])
    monkeypatch.setattr(settings, "GRAFANA_SERVICE_ACCOUNT_TOKEN", None)
    monkeypatch.setattr(settings, "GRAFANA_MCP_ACCESS_TOKEN", None)
    monkeypatch.setattr(settings, "GRAFANA_CLOUD_API_KEY", None)
    board = load_grafana_visual_board()
    assert board.connected is False
    assert board.dashboards == []


def test_grafana_visual_route_requires_space_member(monkeypatch):
    _enable_grafana(monkeypatch)
    _patch_client(monkeypatch)
    user = User(uid="alice_01", email="alice@example.com", display_name="Alice")
    space = Space(space_id="spc_graf", name="Graf", created_by="alice_01", kind=SpaceKind.SHARED_SPACE)
    store.save_user(user)
    store.create_space(space, creator_uid="alice_01")
    store.add_member("spc_graf", "alice_01", MembershipRole.OWNER)

    res = client.get("/v1/spaces/spc_graf/grafana/visual", headers=ALICE)
    assert res.status_code == 200
    body = res.json()
    assert body["connected"] is True
    assert body["dashboards"][0]["title"] == "StudioTower Runtime"
    assert "glsa_" not in res.text
    assert "Billing" not in body["dashboards"][0]["title"]
    assert not body.get("base_url")
    assert not body.get("org_name")


def test_load_grafana_visual_board_queries_without_dashboard_write(monkeypatch):
    from app.integrations.grafana_otlp import set_ingest_status

    set_ingest_status("exporting", "StudioTower Space events export to Grafana Cloud OTLP")
    _enable_grafana(monkeypatch)

    def _readonly_http(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/org":
            return httpx.Response(200, json={"name": "Main Org."})
        if path == "/api/datasources":
            return httpx.Response(
                200,
                json=[
                    {
                        "name": "grafanacloud-prom",
                        "type": "prometheus",
                        "uid": "prom",
                        "url": "https://prometheus-prod-10-prod-us-east-0.grafana.net/api/prom",
                    }
                ],
            )
        if path == "/api/search":
            return httpx.Response(200, json=[])
        if path == "/api/dashboards/uid/studiotower-monitor":
            return httpx.Response(404, json={"message": "Dashboard not found"})
        if path == "/api/dashboards/db":
            return httpx.Response(403, json={"message": "Access denied"})
        if path == "/api/ds/query":
            return httpx.Response(
                200,
                json={
                    "results": {
                        "A": {
                            "frames": [
                                {
                                    "schema": {
                                        "fields": [
                                            {"name": "Time", "type": "time"},
                                            {"name": "events", "type": "number"},
                                        ]
                                    },
                                    "data": {"values": [[1.0, 2.0], [4.0, 6.0]]},
                                }
                            ]
                        }
                    }
                },
            )
        return httpx.Response(404, json={"message": path})

    transport = httpx.MockTransport(_readonly_http)
    real_client = httpx.Client
    monkeypatch.setattr(
        "app.integrations.grafana_visual.httpx.Client",
        lambda **kwargs: real_client(
            transport=transport,
            **{key: value for key, value in kwargs.items() if key != "transport"},
        ),
    )
    monkeypatch.setattr(
        "app.integrations.grafana_otlp.httpx.Client",
        lambda **kwargs: real_client(
            transport=transport,
            **{key: value for key, value in kwargs.items() if key != "transport"},
        ),
    )
    board = load_grafana_visual_board(space_id="spc_graf")
    assert board.connected is True
    assert board.error != "Grafana service account cannot create the StudioTower dashboard"
    assert board.dashboards
    assert board.dashboards[0].uid == "studiotower-monitor"
    assert board.dashboards[0].panels
    assert "cannot create" not in (board.error or "")
    from app.integrations.grafana_visual import _expand_promql

    expr = 'sum(rate(studiotower_space_events_total{space_id="$space_id"}[5m]))'
    out = _expand_promql(expr, space_id="spc_graf")
    assert 'space_id="spc_graf"' in out
    assert "$space_id" not in out


def test_load_grafana_visual_board_queries_tempo_lineage(monkeypatch):
    from app.integrations.grafana_otlp import set_ingest_status

    set_ingest_status("exporting", "StudioTower Space events export to Grafana Cloud OTLP")
    _enable_grafana(monkeypatch)

    def _tempo_http(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/org":
            return httpx.Response(200, json={"name": "Main Org."})
        if path == "/api/datasources":
            return httpx.Response(
                200,
                json=[
                    {
                        "name": "grafanacloud-prom",
                        "type": "prometheus",
                        "uid": "prom",
                        "url": "https://prometheus-prod-10-prod-us-east-0.grafana.net/api/prom",
                    },
                    {
                        "name": "grafanacloud-traces",
                        "type": "tempo",
                        "uid": "grafanacloud-traces",
                    },
                ],
            )
        if path == "/api/search":
            return httpx.Response(200, json=[])
        if path == "/api/dashboards/uid/studiotower-monitor":
            return httpx.Response(404, json={"message": "Dashboard not found"})
        if path == "/api/dashboards/db":
            return httpx.Response(403, json={"message": "Access denied"})
        if path == "/api/ds/query":
            body = json.loads(request.content.decode("utf-8"))
            query = body["queries"][0]
            if query.get("queryType") == "traceql":
                assert "spc_graf" in query["query"]
                return httpx.Response(
                    200,
                    json={
                        "results": {
                            "A": {
                                "frames": [
                                    {
                                        "schema": {
                                            "fields": [
                                                {"name": "traceID", "type": "string"},
                                                {"name": "name", "type": "string"},
                                                {"name": "resource_id", "type": "string"},
                                                {"name": "event_type", "type": "string"},
                                                {"name": "startTimeUnixMs", "type": "number"},
                                            ]
                                        },
                                        "data": {
                                            "values": [
                                                ["abc123def"],
                                                ["studiotower.space.event"],
                                                ["run_act_91c6c00dcf1d"],
                                                ["run.started"],
                                                [200],
                                            ]
                                        },
                                    }
                                ]
                            }
                        }
                    },
                )
            return httpx.Response(
                200,
                json={
                    "results": {
                        "A": {
                            "frames": [
                                {
                                    "schema": {
                                        "fields": [
                                            {"name": "Time", "type": "time"},
                                            {"name": "events", "type": "number"},
                                        ]
                                    },
                                    "data": {"values": [[1.0, 2.0], [4.0, 6.0]]},
                                }
                            ]
                        }
                    }
                },
            )
        return httpx.Response(404, json={"message": path})

    transport = httpx.MockTransport(_tempo_http)
    real_client = httpx.Client
    monkeypatch.setattr(
        "app.integrations.grafana_visual.httpx.Client",
        lambda **kwargs: real_client(
            transport=transport,
            **{key: value for key, value in kwargs.items() if key != "transport"},
        ),
    )
    monkeypatch.setattr(
        "app.integrations.grafana_otlp.httpx.Client",
        lambda **kwargs: real_client(
            transport=transport,
            **{key: value for key, value in kwargs.items() if key != "transport"},
        ),
    )
    board = load_grafana_visual_board(space_id="spc_graf")
    assert board.connected is True
    titles = [panel.title for dash in board.dashboards for panel in dash.panels]
    assert any("Tempo" in title or "lineage" in title.lower() for title in titles)
    tempo_panel = next(panel for dash in board.dashboards for panel in dash.panels if "Tempo" in panel.title)
    assert tempo_panel.table_rows
    assert tempo_panel.table_rows[1][0] == "abc123def"
    assert tempo_panel.table_rows[1][2] == "run_act_91c6c00dcf1d"
    assert board.lineage_flow
    assert board.lineage_flow[0].title == "AI run started"
    assert board.lineage_flow[0].stage == "run"
    assert not board.base_url
    assert not board.org_name


def test_load_grafana_tempo_lineage_uses_traceql(monkeypatch):
    from app.integrations.grafana_visual import load_grafana_tempo_lineage

    _enable_grafana(monkeypatch)

    def _tempo_http(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/datasources":
            return httpx.Response(
                200,
                json=[{"name": "grafanacloud-traces", "type": "tempo", "uid": "grafanacloud-traces"}],
            )
        if path == "/api/ds/query":
            body = json.loads(request.content.decode("utf-8"))
            assert body["queries"][0]["queryType"] == "traceql"
            assert "spc_lineage" in body["queries"][0]["query"]
            return httpx.Response(
                200,
                json={
                    "results": {
                        "A": {
                            "frames": [
                                {
                                    "schema": {"fields": [{"name": "traceID", "type": "string"}]},
                                    "data": {"values": [["trace-one"]]},
                                }
                            ]
                        }
                    }
                },
            )
        return httpx.Response(404, json={"message": path})

    transport = httpx.MockTransport(_tempo_http)
    real_client = httpx.Client
    monkeypatch.setattr(
        "app.integrations.grafana_visual.httpx.Client",
        lambda **kwargs: real_client(
            transport=transport,
            **{key: value for key, value in kwargs.items() if key != "transport"},
        ),
    )
    payload = load_grafana_tempo_lineage("spc_lineage")
    assert payload.connected is True
    assert payload.trace_count == 1
    assert payload.traces[0].trace_id == "trace-one"
