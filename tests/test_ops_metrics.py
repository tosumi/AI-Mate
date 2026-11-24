"""運営者のダッシュボード(Azure のメトリック。本体仕様書 §15.1 の M9)。"""

import logging
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from mycompanion.api.app import AppDeps, create_app
from mycompanion.api.auth import AuthError, OperatorPrincipal
from mycompanion.config import ConfigError, OperatorMonitorConfig
from mycompanion.ops.audit import InMemoryOperatorAuditLog
from mycompanion.ops.metrics import PANELS, SystemMetrics
from mycompanion.ops.roles import Role

NOW = datetime(2026, 11, 24, 12, 34, tzinfo=UTC)
END = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
SUB = "/subscriptions/00000000-1111-2222-3333-444444444444/resourceGroups/rg-secret/providers"
RESOURCES = {
    "api": f"{SUB}/Microsoft.App/containerApps/app-secret",
    "cosmos": f"{SUB}/Microsoft.DocumentDB/databaseAccounts/cosmos-secret",
    "logs": f"{SUB}/Microsoft.OperationalInsights/workspaces/log-secret",
}


def _at(hours_before_end: int) -> str:
    return (END - timedelta(hours=hours_before_end)).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeMonitor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail: set[str] = set()

    async def metrics(self, resource_id: str, params: dict) -> dict:
        self.calls.append((resource_id, params))
        if resource_id in self.fail:
            raise RuntimeError(f"boom {resource_id}")
        names = params["metricnames"].split(",")
        value = []
        for name in names:
            if name == "Requests" and params.get("$filter"):
                value.append({"name": {"value": name}, "timeseries": []})  # 5xx はなかった
            elif name in ("DataUsage", "IndexUsage"):
                # コンテナごと(split)。時刻ごとに足すと、全体の大きさ
                value.append({"name": {"value": name}, "timeseries": [
                    {"data": [{"timeStamp": _at(2), "maximum": 100.0}, {"timeStamp": _at(1), "maximum": 150.0}]},
                    {"data": [{"timeStamp": _at(2), "maximum": 10.0}, {"timeStamp": _at(1), "maximum": None}]},
                ]})  # fmt: skip
            else:
                value.append({"name": {"value": name}, "timeseries": [
                    {"data": [{"timeStamp": _at(2), "average": 1.5, "maximum": 2.0, "total": 7.0}]}
                ]})  # fmt: skip
        return {"value": value}

    async def log_query(self, resource_id: str, query: str, timespan: str) -> dict:
        self.calls.append((resource_id, {"query": query, "timespan": timespan}))
        return {"tables": [{"columns": [{"name": "TimeGenerated"}, {"name": "value"}],
                            "rows": [[_at(3), 0.9], [_at(1), 0.4]]}]}  # fmt: skip


def _metrics(monitor=None, clock=lambda: NOW) -> tuple[SystemMetrics, FakeMonitor]:
    monitor = monitor or FakeMonitor()
    return SystemMetrics(OperatorMonitorConfig(dict(RESOURCES)), monitor, clock), monitor


def _panel(result: dict, panel_id: str) -> dict:
    return next(p for p in result["panels"] if p["id"] == panel_id)


def _series(panel: dict, series_id: str) -> dict:
    return next(s for s in panel["series"] if s["id"] == series_id)


async def test_all_panels_are_returned_and_unconfigured_ones_are_marked():
    metrics, _ = _metrics()
    result = await metrics.read("24h")
    assert [p["id"] for p in result["panels"]] == [p.id for p in PANELS]
    assert result["end"] == END.isoformat() and result["interval"] == "PT1H"
    for panel_id in ("worker", "postgres", "openai"):
        panel = _panel(result, panel_id)
        assert (panel["configured"], panel["available"], panel["series"]) == (False, False, [])


async def test_split_metrics_are_summed_per_time_and_totals_are_zero_filled():
    metrics, monitor = _metrics()
    result = await metrics.read("24h")
    cosmos = _panel(result, "cosmos")
    assert _series(cosmos, "data")["points"] == [[_iso(2), 110.0], [_iso(1), 150.0]]
    api = _panel(result, "api")
    errors = _series(api, "errors")["points"]
    assert len(errors) == 24 and all(v == 0.0 for _, v in errors)  # 5xx がなかった時間は 0
    assert dict(_series(api, "requests")["points"])[_iso(2)] == 7.0
    assert _series(api, "replicas")["points"] == [[_iso(2), 2.0]]
    # 絞り込み・分け方が同じものは、まとめて1回で読む(API: ふつう + 5xx、Cosmos: 分ける + ふつう)
    api_calls = [p for rid, p in monitor.calls if rid == RESOURCES["api"]]
    assert len(api_calls) == 2 and {"statusCodeCategory eq '5xx'"} == {p.get("$filter") for p in api_calls} - {None}
    cosmos_calls = [p for rid, p in monitor.calls if rid == RESOURCES["cosmos"]]
    assert {p.get("$filter") for p in cosmos_calls} == {"CollectionName eq '*'", None}


async def test_log_ingestion_comes_from_the_usage_table():
    metrics, monitor = _metrics()
    result = await metrics.read("7d")
    points = dict(_series(_panel(result, "logs"), "ingestion")["points"])
    assert result["interval"] == "PT6H" and len(points) == 28
    (query,) = [p["query"] for rid, p in monitor.calls if rid == RESOURCES["logs"]]
    assert "bin_at(TimeGenerated, 6h, datetime(2026-11-17T12:00:00Z))" in query


def _iso(hours_before_end: int) -> str:
    return (END - timedelta(hours=hours_before_end)).isoformat()


async def test_a_failing_resource_does_not_hide_the_others_or_leak_ids(caplog):
    monitor = FakeMonitor()
    monitor.fail.add(RESOURCES["api"])
    metrics, _ = _metrics(monitor)
    with caplog.at_level(logging.WARNING):
        result = await metrics.read("24h")
    assert _panel(result, "api")["available"] is False and _panel(result, "api")["configured"] is True
    assert _panel(result, "cosmos")["available"] is True
    text = str(result) + caplog.text
    assert "secret" not in text and "00000000-1111" not in text


async def test_results_are_cached_for_five_minutes():
    now = [NOW]
    metrics, monitor = _metrics(clock=lambda: now[0])
    await metrics.read("24h")
    calls = len(monitor.calls)
    now[0] += timedelta(minutes=4)
    await metrics.read("24h")
    assert len(monitor.calls) == calls
    now[0] += timedelta(minutes=2)
    await metrics.read("24h")
    assert len(monitor.calls) == calls * 2


def test_monitor_config_is_optional_and_checked():
    assert OperatorMonitorConfig.from_env({}) is None
    config = OperatorMonitorConfig.from_env({"OPS_MONITOR_API_APP_ID": RESOURCES["api"], "OPS_MONITOR_COSMOS_ID": " "})
    assert config and config.resources == {"api": RESOURCES["api"]}
    with pytest.raises(ConfigError) as error:
        OperatorMonitorConfig.from_env({"OPS_MONITOR_POSTGRES_ID": "pg-secret"})
    assert "pg-secret" not in str(error.value)


# --- API ----------------------------------------------------------------------------------------------

OPERATORS = {f"tok-{r.value}": OperatorPrincipal(f"oid-{r.value}", "t", frozenset({r.value})) for r in Role}


class FakeOperatorVerifier:
    async def verify(self, token: str) -> OperatorPrincipal:
        if token not in OPERATORS:
            raise AuthError("bad")
        return OPERATORS[token]


class NoUsers:
    async def verify(self, token: str):
        raise AuthError("bad")

    async def resolve_user(self, idp, subject):
        raise AssertionError


def _client(metrics: SystemMetrics | None) -> TestClient:
    deps = AppDeps(
        verifier=NoUsers(), users=NoUsers(), ops_verifier=FakeOperatorVerifier(),
        ops_audit=InMemoryOperatorAuditLog(), ops_metrics=metrics,
    )  # fmt: skip
    return TestClient(create_app(deps))


@pytest.mark.parametrize("role", list(Role))
def test_only_admins_see_the_dashboard(role):
    client = _client(_metrics()[0])
    response = client.get("/ops/v1/system/metrics", headers={"Authorization": f"Bearer tok-{role.value}"})
    assert response.status_code == (200 if role == Role.ADMIN else 403)
    if role == Role.ADMIN:
        assert response.json()["period"] == "24h" and "secret" not in response.text


def test_dashboard_rejects_unknown_periods_and_needs_configuration():
    admin = {"Authorization": "Bearer tok-admin"}
    assert _client(_metrics()[0]).get("/ops/v1/system/metrics", params={"period": "1y"}, headers=admin).status_code == 422
    assert _client(None).get("/ops/v1/system/metrics", headers=admin).status_code == 503
