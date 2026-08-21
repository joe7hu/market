"""Broker sync orchestration and policy checks."""

from __future__ import annotations
from typing import Any
from investment_panel.core.config import load_config

from investment_panel.core.brokers.constants import ADVISORY_AUTHORITY
from investment_panel.core.brokers.types import BrokerSnapshot, ProviderStatus
from investment_panel.core.brokers.ibkr import IBKRProvider
from investment_panel.core.brokers.moomoo import MoomooProvider
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.brokers import BrokerRepository
from investment_panel.database.ingestion import IngestionRepository



def run(config_path: str | None = None) -> dict[str, Any]:
    config = load_config(config_path)
    runtime = runtime_for_config(config)
    symbols = IngestionRepository(runtime).option_universe(config.watchlist)[:250]
    providers = [IBKRProvider(config.data_sources.brokers.ibkr), MoomooProvider(config.data_sources.brokers.moomoo)]
    repository = BrokerRepository(runtime)
    provider_results = []
    for provider in providers:
        try:
            snapshot = provider.collect(symbols)
        except Exception as exc:  # pragma: no cover - provider boundary
            snapshot = BrokerSnapshot(ProviderStatus(getattr(provider, "name", "unknown"), "session_failure", str(exc)))
        provider_results.append(repository.sync_snapshot(snapshot))
    return {
        "status": "ok" if any(row["status"] == "ok" for row in provider_results) else "degraded",
        "providers": provider_results,
        "recommendations": 0,
        "recommendation_owner": "options-radar",
        "authority": ADVISORY_AUTHORITY,
        "database": "postgresql",
    }
