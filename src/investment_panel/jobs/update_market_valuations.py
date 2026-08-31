"""Refresh source-backed broad-market valuation series for the Market page."""

from __future__ import annotations

from datetime import UTC, datetime
import time
from typing import Any

import httpx

from investment_panel.core.config import load_config
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.market_analysis import refresh_market_publication
from investment_panel.jobs.update_market_data import market_benchmark_symbols


MUNGER_MARKET_METRICS_URL = "https://mungermode.com/api/v1/market/metrics"
SOURCE_ID = "mungermode-market-valuations"
SUPPORTED_METRICS = {
    "sp500_forward_pe",
    "shiller_pe",
    "sp500_pe",
    "equity_risk_premium",
    "sp500_price",
}
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
RETRY_DELAYS_SECONDS = (1.0, 3.0)


def run(config_path: str | None = None, *, url: str = MUNGER_MARKET_METRICS_URL) -> dict[str, Any]:
    """Fetch the complete public series and persist one current row per metric."""

    config = load_config(config_path)
    runtime = runtime_for_config(config)
    repository = IngestionRepository(runtime)
    repository.register_source(
        SOURCE_ID,
        name="Munger Mode market valuation metrics",
        family="market_data",
        kind="market_valuation",
        origin=url,
        capabilities={"market_valuation": True},
    )
    fetched_at = datetime.now(UTC)
    try:
        with repository.run(SOURCE_ID, "market_valuation") as ingestion_run:
            payload = _fetch_payload(url)
            rows, skipped = _valuation_rows(payload, fetched_at, url)
            if not rows:
                raise ValueError("market valuation source returned no supported series")
            stored = 0
            for row in rows:
                stored += repository.store_fundamental_observations(
                    ingestion_run.id,
                    SOURCE_ID,
                    f"market_valuation:{row['values']['metric']}",
                    [row],
                )
            ingestion_run.finish(
                item_count=stored,
                instrument_count=1,
                summary={"series": len(rows), "skipped_series": skipped, "source_url": url},
            )
    except Exception as exc:
        return {
            "status": "failed",
            "ok": False,
            "database": "postgresql",
            "source": SOURCE_ID,
            "source_status": "failed",
            "downstream_status": "not_run",
            "error": f"{type(exc).__name__}: {exc}",
        }
    try:
        publication = refresh_market_publication(
            runtime,
            benchmark_symbols=market_benchmark_symbols(runtime, config.watchlist),
        )
    except Exception as exc:
        return {
            "status": "partial",
            "ok": False,
            "database": "postgresql",
            "source": SOURCE_ID,
            "source_status": "ok",
            "downstream_status": "failed",
            "market_publication": {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            },
            "error": f"market publication failed after source success: {type(exc).__name__}: {exc}",
        }
    return {
        "status": "ok",
        "ok": True,
        "database": "postgresql",
        "source": SOURCE_ID,
        "source_status": "ok",
        "downstream_status": "ok",
        "series": len(rows),
        "rows": stored,
        "skipped_series": skipped,
        "market_publication": publication,
    }


def _fetch_payload(url: str) -> Any:
    """Fetch the public endpoint with a short, bounded retry policy."""

    for attempt in range(len(RETRY_DELAYS_SECONDS) + 1):
        try:
            response = httpx.get(
                url,
                timeout=45.0,
                follow_redirects=True,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "joehu-market-panel/0.1",
                },
            )
            status_code = int(getattr(response, "status_code", 200))
            if status_code in RETRYABLE_STATUS_CODES and attempt < len(RETRY_DELAYS_SECONDS):
                time.sleep(_retry_delay(response, attempt))
                continue
            response.raise_for_status()
            return response.json()
        except httpx.RequestError:
            if attempt >= len(RETRY_DELAYS_SECONDS):
                raise
            time.sleep(RETRY_DELAYS_SECONDS[attempt])
    raise RuntimeError("market valuation request exhausted its retry budget")


def _retry_delay(response: Any, attempt: int) -> float:
    headers = getattr(response, "headers", {})
    retry_after = headers.get("Retry-After") if hasattr(headers, "get") else None
    try:
        return min(30.0, max(0.0, float(retry_after)))
    except (TypeError, ValueError):
        return RETRY_DELAYS_SECONDS[attempt]


def _valuation_rows(payload: Any, observed_at: datetime, source_url: str) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(payload, dict):
        raise ValueError("market valuation source response must be an object")
    rows: list[dict[str, Any]] = []
    skipped: list[str] = []
    for metric, block in payload.items():
        if metric not in SUPPORTED_METRICS:
            continue
        if not isinstance(block, dict):
            skipped.append(str(metric))
            continue
        history = [
            {"date": str(point["date"])[:10], "value": float(point["value"])}
            for point in block.get("data") or []
            if isinstance(point, dict)
            and point.get("date")
            and _number(point.get("value")) is not None
        ]
        history.sort(key=lambda point: point["date"])
        if not history:
            skipped.append(str(metric))
            continue
        latest = history[-1]
        rows.append(
            {
                "symbol": "SPY",
                "name": "S&P 500",
                "asset_class": "etf",
                "observed_at": observed_at,
                "period_end": latest["date"],
                "values": {
                    "metric": metric,
                    "label": str(block.get("label") or metric.replace("_", " ").title()),
                    "latest_value": latest["value"],
                    "value": latest["value"],
                    "suffix": str(block.get("suffix") or ""),
                    "higher_is_better": bool(block.get("higher_is_better")),
                    "source_url": source_url,
                    "history": history,
                },
            }
        )
    return rows, skipped


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None
