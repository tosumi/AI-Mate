"""運営者のダッシュボード: Azure の状況のメトリック(本体仕様書 §15.1 の M9。管理者だけ)。

- Azure Monitor のメトリック(REST)と、Log Analytics の Usage 表(ログの取り込み量)を、API のマネージド ID で読む
  (リソースグループに「監視の閲覧者」を割り当てる)。どちらも Azure Resource Manager の口から読むので、トークンは1種類。
- 対象のリソース ID は環境変数(`config.OperatorMonitorConfig`)。応答・ログには、リソース ID・名前を出さない。
- 読み出しは、期間ごとに5分間キャッシュする(画面を開くたびに Azure を呼ばない)。
- ユーザーのデータは扱わない(数値だけ)。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from mycompanion.config import OperatorMonitorConfig

logger = logging.getLogger(__name__)

ARM = "https://management.azure.com"
ARM_SCOPE = "https://management.azure.com/.default"
METRICS_API_VERSION = "2023-10-01"
LOGS_API_VERSION = "2017-10-01"
CACHE_SECONDS = 300


@dataclass(frozen=True)
class Period:
    span: timedelta
    interval: str  # メトリックの間隔(ISO 8601)
    bin: str  # Log Analytics の bin()
    step: timedelta


PERIODS = {
    "24h": Period(timedelta(hours=24), "PT1H", "1h", timedelta(hours=1)),
    "7d": Period(timedelta(days=7), "PT6H", "6h", timedelta(hours=6)),
    "30d": Period(timedelta(days=30), "P1D", "1d", timedelta(days=1)),
}


@dataclass(frozen=True)
class Series:
    id: str
    label: str
    unit: str  # percent / count / bytes / cores / mb
    metric: str = ""  # Azure Monitor のメトリックの名前
    aggregation: str = "Average"  # Average / Maximum / Total
    filter: str | None = None  # 次元の絞り込み(例: 5xx だけ)
    split: str | None = None  # この次元で分けて読み、時刻ごとに足す(Cosmos DB のコンテナごとの大きさ)
    scale: float = 1.0
    log_query: str | None = None  # Log Analytics の問い合わせ(`{bin}` に間隔、`{start}` に期間の始まりが入る)


@dataclass(frozen=True)
class Panel:
    id: str
    title: str
    resource: str  # config.MONITOR_RESOURCE_ENV の名前
    series: tuple[Series, ...]


# 表示する項目(仕様書 §15.1 の M9 のダッシュボード。2026-11-24 のユーザーの決定)
PANELS: tuple[Panel, ...] = (
    Panel("api", "API(Container Apps)", "api", (
        Series("replicas", "レプリカ数", "count", "Replicas", "Maximum"),
        Series("cpu", "CPU", "percent", "CpuPercentage", "Average"),
        Series("memory", "メモリ", "percent", "MemoryPercentage", "Average"),
        Series("requests", "要求数", "count", "Requests", "Total"),
        Series("errors", "5xx の数", "count", "Requests", "Total", filter="statusCodeCategory eq '5xx'"),
    )),
    Panel("worker", "ワーカー(Container Apps のジョブ)", "worker", (
        # Executions は、実行中の数の分ごとの記録。合計すると「分の延べ」になるので、最大(同時に動いた数)を出す
        Series("executions", "実行中の数(最大)", "count", "Executions", "Maximum"),
        Series("cpu", "CPU(最大)", "cores", "UsageNanoCores", "Maximum", scale=1e-9),
        Series("memory", "メモリ(最大)", "bytes", "UsageBytes", "Maximum"),
    )),
    Panel("postgres", "PostgreSQL", "postgres", (
        Series("cpu", "CPU", "percent", "cpu_percent", "Average"),
        Series("memory", "メモリ", "percent", "memory_percent", "Average"),
        Series("storage", "ストレージの使用量", "bytes", "storage_used", "Maximum"),
        Series("connections", "接続数(最大)", "count", "active_connections", "Maximum"),
    )),
    Panel("cosmos", "Cosmos DB", "cosmos", (
        Series("data", "データの大きさ", "bytes", "DataUsage", "Maximum", split="CollectionName"),
        Series("index", "インデックスの大きさ", "bytes", "IndexUsage", "Maximum", split="CollectionName"),
        Series("ru", "RU の消費", "count", "TotalRequestUnits", "Total"),
    )),
    Panel("logs", "ログ(Log Analytics)", "logs", (
        Series("ingestion", "取り込み量", "mb", log_query=(
            # 区切りは、期間の始まりにそろえる(グラフの時刻と合わせるため)
            "Usage | summarize value = sum(Quantity) by bin_at(TimeGenerated, {bin}, datetime({start}))"
            " | order by TimeGenerated asc"
        )),
    )),
    Panel("openai", "Azure OpenAI", "openai", (
        Series("tokens", "トークン数", "count", "TotalTokens", "Total"),
        Series("requests", "要求数", "count", "AzureOpenAIRequests", "Total"),
    )),
)  # fmt: skip


class MonitorClient(Protocol):
    async def metrics(self, resource_id: str, params: dict[str, str]) -> dict[str, Any]:
        """Azure Monitor のメトリック(`{resource_id}/providers/Microsoft.Insights/metrics`)の応答。"""
        ...

    async def log_query(self, resource_id: str, query: str, timespan: str) -> dict[str, Any]:
        """Log Analytics の問い合わせ(`{resource_id}/query`)の応答。"""
        ...


class ArmMonitorClient:
    """Azure Resource Manager の口から読む(マネージド ID のトークン)。"""

    def __init__(self) -> None:
        import httpx
        from azure.identity.aio import DefaultAzureCredential

        self._credential = DefaultAzureCredential()
        self._http = httpx.AsyncClient(base_url=ARM, timeout=20.0)

    async def _headers(self) -> dict[str, str]:
        token = await self._credential.get_token(ARM_SCOPE)
        return {"Authorization": f"Bearer {token.token}"}

    async def metrics(self, resource_id: str, params: dict[str, str]) -> dict[str, Any]:
        response = await self._http.get(
            f"{resource_id}/providers/Microsoft.Insights/metrics",
            params={"api-version": METRICS_API_VERSION, **params},
            headers=await self._headers(),
        )
        response.raise_for_status()
        return response.json()

    async def log_query(self, resource_id: str, query: str, timespan: str) -> dict[str, Any]:
        response = await self._http.post(
            f"{resource_id}/query",
            params={"api-version": LOGS_API_VERSION},
            json={"query": query, "timespan": timespan},
            headers=await self._headers(),
        )
        response.raise_for_status()
        return response.json()

    async def close(self) -> None:
        await self._http.aclose()
        await self._credential.close()


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _grid(start: datetime, end: datetime, step: timedelta) -> list[datetime]:
    points, t = [], start
    while t < end:
        points.append(t)
        t += step
    return points


def _metric_points(body: dict[str, Any], series: Series) -> list[tuple[datetime, float]]:
    """1つのメトリックの時系列。分けて読んだもの(split)は、時刻ごとに足す。"""
    key = series.aggregation.lower()
    sums: dict[datetime, float] = {}
    for item in body.get("value", []):
        if item.get("name", {}).get("value") != series.metric:
            continue
        for timeseries in item.get("timeseries", []):
            for point in timeseries.get("data", []):
                value = point.get(key)
                if value is None:
                    continue
                ts = _ts(point["timeStamp"])
                sums[ts] = sums.get(ts, 0.0) + float(value) * series.scale
    return sorted(sums.items())


def _log_points(body: dict[str, Any]) -> list[tuple[datetime, float]]:
    tables = body.get("tables") or []
    if not tables:
        return []
    columns = [c["name"] for c in tables[0].get("columns", [])]
    t_index, v_index = columns.index("TimeGenerated"), columns.index("value")
    return [(_ts(row[t_index]), float(row[v_index] or 0)) for row in tables[0].get("rows", [])]


class SystemMetrics:
    def __init__(
        self,
        config: OperatorMonitorConfig | None,
        client: MonitorClient | None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._config = config
        self._client = client
        self._clock = clock
        self._cache: dict[str, tuple[datetime, dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    async def read(self, period_name: str) -> dict[str, Any]:
        period = PERIODS[period_name]
        async with self._lock:  # 同時に開いても、Azure を二重に呼ばない
            now = self._clock()
            cached = self._cache.get(period_name)
            if cached and (now - cached[0]).total_seconds() < CACHE_SECONDS:
                return cached[1]
            result = await self._read(period_name, period, now)
            self._cache[period_name] = (now, result)
            return result

    async def _read(self, name: str, period: Period, now: datetime) -> dict[str, Any]:
        end = now.replace(minute=0, second=0, microsecond=0)
        start = end - period.span
        panels = await asyncio.gather(*(self._panel(panel, period, start, end) for panel in PANELS))
        return {
            "period": name, "interval": period.interval, "start": start.isoformat(), "end": end.isoformat(),
            "generated_at": now.isoformat(), "panels": list(panels),
        }  # fmt: skip

    async def _panel(self, panel: Panel, period: Period, start: datetime, end: datetime) -> dict[str, Any]:
        head = {"id": panel.id, "title": panel.title}
        resource_id = (self._config.resources if self._config else {}).get(panel.resource)
        if resource_id is None or self._client is None:
            return {**head, "configured": False, "available": False, "series": []}
        try:
            points = await self._series_points(panel, resource_id, period, start, end)
        except Exception as exc:  # noqa: BLE001 - 1つのリソースが読めなくても、ほかは出す
            # リソース ID・応答の本文はログに出さない(サブスクリプションの ID を含みうる)
            logger.warning("メトリックを読めませんでした: %s %s", panel.id, type(exc).__name__)
            return {**head, "configured": True, "available": False, "series": []}
        grid = _grid(start, end, period.step)
        series_out = []
        for series in panel.series:
            found = points[series.id]
            # 合計の系列は、値のない時刻を 0 にする(要求・エラーがなかった時間)
            if series.aggregation == "Total" or series.log_query:
                known = dict(found)
                found = [(t, known.get(t, 0.0)) for t in grid] if grid else found
            series_out.append({
                "id": series.id, "label": series.label, "unit": series.unit,
                "points": [[t.isoformat(), round(v, 4)] for t, v in found if start <= t < end],
            })  # fmt: skip
        return {**head, "configured": True, "available": True, "series": series_out}

    async def _series_points(
        self, panel: Panel, resource_id: str, period: Period, start: datetime, end: datetime
    ) -> dict[str, list[tuple[datetime, float]]]:
        assert self._client is not None
        timespan = f"{start.strftime('%Y-%m-%dT%H:%M:%SZ')}/{end.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        out: dict[str, list[tuple[datetime, float]]] = {}
        # 絞り込み・分け方が同じメトリックは、1回で読む
        groups: dict[tuple[str | None, str | None], list[Series]] = {}
        for series in panel.series:
            if series.log_query:
                body = await self._client.log_query(resource_id, series.log_query.format(bin=period.bin, start=start.strftime("%Y-%m-%dT%H:%M:%SZ")), timespan)
                out[series.id] = _log_points(body)
            else:
                groups.setdefault((series.filter, series.split), []).append(series)
        for (flt, split), members in groups.items():
            params = {
                "timespan": timespan,
                "interval": period.interval,
                "metricnames": ",".join(dict.fromkeys(s.metric for s in members)),
                "aggregation": ",".join(dict.fromkeys(s.aggregation for s in members)),
            }
            if flt or split:
                params["$filter"] = flt or f"{split} eq '*'"
                params["top"] = "100"  # 分けたときの系列の数の上限(既定は 10)
            body = await self._client.metrics(resource_id, params)
            for series in members:
                out[series.id] = _metric_points(body, series)
        return out
