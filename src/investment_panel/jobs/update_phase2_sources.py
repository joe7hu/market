"""Credential-safe Phase 2 source producers.

The producers keep provider transport separate from the Phase 2 parsers.  A
provider response is never treated as a fact unless it carries an explicit
source availability clock.  The optional ``fetcher`` is intentionally small so
tests can supply mock payloads without credentials or network access.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import httpx

from investment_panel.core.config import AppConfig, load_config
from investment_panel.core.phase2 import (
    AdapterResult,
    Phase2Status,
    assess_option_oi_volume_sla,
    parse_coinmetrics_derivatives,
    parse_option_history,
    parse_sec_positioning,
    parse_corporate_expectations,
    parse_event_consensus,
    parse_fred_alfred,
    parse_treasury_yield_curve,
    source_contracts,
)
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.payload_archive import provider_archive_path
from investment_panel.database.phase2 import Phase2Repository


Fetcher = Callable[[str, Mapping[str, str], Mapping[str, str]], Mapping[str, Any]]


def _rows_from_body(body: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    rows = body.get("observations", body.get("data", body.get("results", ())))
    return rows if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)) else ()

_URLS = {
    "fred": "https://api.stlouisfed.org/fred/series/observations",
    "treasury": "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml",
    "trading_economics": "https://api.tradingeconomics.com/calendar/country/United%20States",
    "alphavantage": "https://www.alphavantage.co/query",
    "coinmetrics": "https://api.coinmetrics.io/v4/timeseries/market-metrics",
}


def _credential(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def _http_fetch(url: str, headers: Mapping[str, str], params: Mapping[str, str]) -> Mapping[str, Any]:
    response = httpx.get(url, headers=dict(headers), params=dict(params), timeout=30.0, follow_redirects=True)
    response.raise_for_status()
    body = response.json()
    if isinstance(body, Mapping):
        return body
    if isinstance(body, list):
        return {"data": body}
    raise ValueError("Phase 2 provider response must be an object or row list")


def _payload_for(source_id: str, *, fetcher: Fetcher) -> Mapping[str, Any]:
    if source_id not in _URLS:
        # Existing broker and SEC seams are dispatched explicitly below.  An
        # absent seam is a truthful missing-history result, never a KeyError.
        return {}
    url = _URLS[source_id]
    headers = {"Accept": "application/json", "User-Agent": "market-phase2-source-producer/1"}
    params: dict[str, str] = {}
    if source_id == "fred":
        key = _credential("FRED_API_KEY")
        params.update({"api_key": key or "", "file_type": "json", "series_id": os.environ.get("MARKET_FRED_SERIES_IDS", "GDP,CPIAUCSL,UNRATE")})
    elif source_id == "trading_economics":
        params["c"] = _credential("TRADING_ECONOMICS_API_KEY") or ""
    elif source_id == "alphavantage":
        params.update({"apikey": _credential("ALPHAVANTAGE_API_KEY") or "", "function": "EARNINGS", "symbol": os.environ.get("MARKET_ALPHAVANTAGE_SYMBOL", "SPY")})
    elif source_id == "coinmetrics":
        headers["Authorization"] = f"Bearer {_credential('COINMETRICS_API_KEY') or ''}"
    return fetcher(url, headers, params)


def adapt_source_payload(source_id: str, payload: Mapping[str, Any], *, env: Mapping[str, str] | None = None) -> AdapterResult:
    if source_id == "fred":
        return parse_fred_alfred(payload, env=env)
    if source_id == "treasury":
        return parse_treasury_yield_curve(payload)
    if source_id == "trading_economics":
        return parse_event_consensus(payload, env=env)
    if source_id == "alphavantage":
        return parse_corporate_expectations(payload, env=env)
    if source_id == "coinmetrics":
        return parse_coinmetrics_derivatives(payload, env=env)
    if source_id in {"robinhood_history_full", "ibkr_options"}:
        return parse_option_history(source_id, payload)
    if source_id == "sec_13f":
        return parse_sec_positioning(payload)
    return AdapterResult(source_id=source_id, status=Phase2Status.MISSING_HISTORY, reason="existing source seam has no Phase 2 payload")


def _source_definition(source_id: str) -> dict[str, Any]:
    contract = next(item for item in source_contracts() if item.source_id == source_id)
    return {
        "name": contract.authority,
        "family": "phase2",
        "kind": "external_adapter",
        "origin": _URLS.get(source_id) or f"existing:{source_id}",
        "capabilities": {"phase2": True, "phase2_capabilities": list(contract.capabilities)},
        "operational_state": "active",
        "health_owner": "update_phase2_sources",
        "freshness_seconds": 86400,
    }


def _archive_payload(app_config: AppConfig, source_id: str, run_id: Any, payload: Mapping[str, Any]) -> Path:
    path = provider_archive_path(app_config, source_id, datetime.now(UTC).strftime("%Y/%m/%d"), f"{run_id}.json")
    path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    return path


def run(
    config_path: str | None = None,
    *,
    source_ids: Sequence[str] | None = None,
    payloads: Mapping[str, Mapping[str, Any]] | None = None,
    fetcher: Fetcher | None = None,
    runtime: Any | None = None,
) -> dict[str, Any]:
    """Run authorized Phase 2 producers and record explicit source status."""

    config = load_config(config_path)
    db = runtime or runtime_for_config(config)
    ingestion = IngestionRepository(db)
    phase2 = Phase2Repository(db)
    selected = tuple(source_ids or ("fred", "treasury", "trading_economics", "alphavantage", "coinmetrics", "sec_13f", "robinhood_history_full", "ibkr_options"))
    results: dict[str, dict[str, Any]] = {}
    for source_id in selected:
        definition = _source_definition(source_id)
        contract = next((item for item in source_contracts() if item.source_id == source_id), None)
        credential_missing = contract is not None and contract.credential_env and not _credential(contract.credential_env)
        with db.read() as connection:
            existing_source = connection.execute(
                "SELECT family, kind, origin, enabled, operational_state FROM ingest.source WHERE id = %s",
                [source_id],
            ).fetchone()
        definition["enabled"] = bool(existing_source["enabled"]) if existing_source is not None else True
        definition["operational_state"] = str(existing_source["operational_state"]) if existing_source is not None else "active"
        if existing_source is not None:
            definition["family"] = str(existing_source["family"])
            definition["kind"] = str(existing_source["kind"])
            definition["origin"] = existing_source["origin"]
        definition["capabilities"] = {**definition["capabilities"], "phase2_status": Phase2Status.MISSING_SOURCE.value if credential_missing else "PENDING"}
        ingestion.register_source(source_id, **definition)
        with ingestion.run(source_id, "phase2") as source_run:
            body: Mapping[str, Any] = {}
            if not definition["enabled"] or definition["operational_state"] != "active":
                result = AdapterResult(source_id=source_id, status=Phase2Status.MISSING_SOURCE, reason="source is disabled by lifecycle policy")
            elif credential_missing:
                result = AdapterResult(source_id=source_id, status=Phase2Status.MISSING_SOURCE, reason=f"{contract.credential_env} is not configured")
            else:
                try:
                    body = (payloads or {}).get(source_id) if payloads is not None else _payload_for(source_id, fetcher=fetcher or _http_fetch)
                    result = adapt_source_payload(source_id, body or {}, env=os.environ)
                except (httpx.HTTPError, OSError, TypeError, ValueError) as exc:
                    result = AdapterResult(source_id=source_id, status=Phase2Status.MISSING_SOURCE, reason=f"provider request failed: {type(exc).__name__}")
            stored = 0
            payload_id = None
            if body and (result.observations or source_id in {"robinhood_history_full", "ibkr_options"}):
                archive = _archive_payload(config, source_id, source_run.id, body)
                payload_id = ingestion.record_payload_file(source_run.id, archive, phase2_source=source_id)
                if source_id in {"robinhood_history_full", "ibkr_options"}:
                    phase2.record_option_liquidity_sla(as_of=datetime.now(UTC), source_id=source_id, payload={"assessment": assess_option_oi_volume_sla(_rows_from_body(body))}, ingest_run_id=str(source_run.id), payload_id=payload_id)
            if result.observations:
                if payload_id is None:
                    archive = _archive_payload(config, source_id, source_run.id, body)
                    payload_id = ingestion.record_payload_file(source_run.id, archive, phase2_source=source_id)
                observations = tuple(item.model_copy(update={"ingest_run_id": str(source_run.id), "payload_id": payload_id}) for item in result.observations)
                stored = phase2.record_observations(observations, ingest_run_id=str(source_run.id), payload_id=payload_id)
            terminal = "succeeded" if result.status is Phase2Status.AVAILABLE else "skipped"
            definition["capabilities"] = {**definition["capabilities"], "phase2_status": result.status.value}
            ingestion.register_source(source_id, **definition)
            source_run.finish(terminal, item_count=stored, summary={"phase2_status": result.status.value, "reason": result.reason, "content_hash": result.content_hash})
            results[source_id] = {"status": result.status.value, "reason": result.reason, "stored": stored, "run_id": str(source_run.id), "content_hash": result.content_hash}
    return {"status": "ok", "database": "postgresql", "sources": results}


__all__ = ["adapt_source_payload", "run"]
