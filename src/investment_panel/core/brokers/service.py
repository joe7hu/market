"""Broker sync orchestration and policy checks."""

from __future__ import annotations
from typing import Any, Protocol
from investment_panel.core.config import AppConfig, BrokerPolicyConfig, load_config
from investment_panel.core.db import db, init_db, json_dumps, query_rows
from investment_panel.core.decision import refresh_decision_read_models
from investment_panel.core.instruments import infer_asset_class, normalize_symbol

from investment_panel.core.brokers.constants import ADVISORY_AUTHORITY
from investment_panel.core.brokers.types import BrokerSnapshot, ProviderStatus
from investment_panel.core.brokers.ibkr import IBKRProvider
from investment_panel.core.brokers.moomoo import MoomooProvider
from investment_panel.core.brokers.persistence import persist_broker_snapshot
from investment_panel.core.brokers.recommendations import build_and_persist_agent_recommendations



def run(config_path: str | None = None) -> dict[str, Any]:
    config = load_config(config_path)
    init_db(config.database.duckdb_path)
    with db(config.database.duckdb_path, read_only=False) as con:
        return update_broker_sources(con, config)




def update_broker_sources(con: Any, config: AppConfig, providers: list[BrokerProvider] | None = None) -> dict[str, Any]:
    symbols = broker_sync_symbols(con, config)
    active_providers = providers or [IBKRProvider(config.data_sources.brokers.ibkr), MoomooProvider(config.data_sources.brokers.moomoo)]
    provider_results: list[dict[str, Any]] = []
    for provider in active_providers:
        try:
            snapshot = provider.collect(symbols)
        except Exception as exc:  # pragma: no cover - defensive provider boundary
            snapshot = BrokerSnapshot(ProviderStatus(getattr(provider, "name", "unknown"), "session_failure", str(exc)))
        persist_broker_snapshot(con, snapshot)
        provider_results.append(
            {
                "provider": snapshot.status.provider,
                "status": snapshot.status.status,
                "accounts": len(snapshot.accounts),
                "positions": len(snapshot.positions),
                "market_snapshots": len(snapshot.market_snapshots),
                "scanner_signals": len(snapshot.scanner_signals),
            }
        )
    refresh_decision_read_models(con, config.watchlist)
    recommendations = build_and_persist_agent_recommendations(con, config.data_sources.brokers.policy)
    return {
        "status": "ok" if any(row["status"] == "ok" for row in provider_results) else "degraded",
        "providers": provider_results,
        "recommendations": len(recommendations),
        "authority": ADVISORY_AUTHORITY,
    }




def broker_sync_symbols(con: Any, config: AppConfig) -> list[str]:
    symbols = {str(item.get("symbol") or "").upper() for item in config.watchlist if item.get("symbol")}
    for row in query_rows(
        con,
        """
        SELECT symbol FROM instruments
        UNION SELECT symbol FROM portfolio_positions
        UNION SELECT symbol FROM decision_queue
        ORDER BY symbol
        LIMIT 250
        """,
    ):
        symbol = normalize_symbol(str(row.get("symbol") or ""))
        if symbol:
            symbols.add(symbol)
    return sorted(symbols)
