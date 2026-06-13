"""Broker provider protocol and snapshot/status dataclasses."""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol



@dataclass(frozen=True)
class ProviderStatus:
    provider: str
    status: str
    detail: str
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    account_id: str | None = None
    account_mode: str = "unknown"
    session_started_at: datetime | None = None
    last_data_at: datetime | None = None
    latency_ms: float | None = None
    capabilities: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def health(self) -> str:
        return "ok" if self.status == "ok" else "degraded" if self.status not in {"disabled", "missing"} else self.status




@dataclass(frozen=True)
class BrokerSnapshot:
    status: ProviderStatus
    accounts: list[dict[str, Any]] = field(default_factory=list)
    positions: list[dict[str, Any]] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)
    fills: list[dict[str, Any]] = field(default_factory=list)
    market_snapshots: list[dict[str, Any]] = field(default_factory=list)
    scanner_signals: list[dict[str, Any]] = field(default_factory=list)




class BrokerProvider(Protocol):
    name: str

    def collect(self, symbols: list[str]) -> BrokerSnapshot:
        """Collect account/market/scanner state from a broker or data gateway."""
