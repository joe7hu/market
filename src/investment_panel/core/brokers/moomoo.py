"""Moomoo provider."""

from __future__ import annotations
from datetime import UTC, datetime, timedelta
import importlib.util
import time
from typing import Any, Protocol

from investment_panel.core.brokers.types import BrokerSnapshot, ProviderStatus
from investment_panel.core.brokers.coerce import tcp_open



class MoomooProvider:
    """Health-first adapter for moomoo OpenD / Python SDK."""

    name = "moomoo"

    def __init__(self, config: Any):
        self.config = config

    def collect(self, symbols: list[str]) -> BrokerSnapshot:
        checked_at = datetime.now(UTC)
        if not self.config.enabled:
            return BrokerSnapshot(ProviderStatus(self.name, "disabled", "moomoo OpenD source is disabled in config.", checked_at))
        if importlib.util.find_spec("futu") is None:
            return BrokerSnapshot(
                ProviderStatus(
                    self.name,
                    "missing_dependency",
                    "Install the moomoo/futu Python SDK and run OpenD to enable supplemental sync.",
                    checked_at,
                    account_mode="paper" if self.config.paper_only else "unknown",
                    capabilities=moomoo_capabilities(),
                )
            )
        started = time.perf_counter()
        if not tcp_open(self.config.host, self.config.port, timeout=1.0):
            return BrokerSnapshot(
                ProviderStatus(
                    self.name,
                    "gateway_offline",
                    f"moomoo OpenD is not reachable at {self.config.host}:{self.config.port}.",
                    checked_at,
                    latency_ms=round((time.perf_counter() - started) * 1000, 2),
                    capabilities=moomoo_capabilities(),
                )
            )
        return BrokerSnapshot(
            ProviderStatus(
                self.name,
                "session_failure",
                "OpenD socket is reachable, but no supplemental quote/scanner session completed in this run.",
                checked_at,
                account_mode="paper" if self.config.paper_only else "unknown",
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                capabilities=moomoo_capabilities(),
            )
        )




def moomoo_capabilities() -> list[str]:
    return ["quotes", "kline", "order_book", "stock_filters", "capital_flow", "options", "simulated_paper"]
