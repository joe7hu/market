"""Broker and advisory-agent read models.

V1 is deliberately advisory and paper-only. This module stores source-of-truth
broker/account state, supplemental discovery rows, safety checks, and paper
order previews without exposing any live-order path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import hashlib
import importlib.util
import json
import socket
import time
from typing import Any, Protocol
from uuid import uuid4

from investment_panel.core.config import AppConfig, BrokerPolicyConfig, load_config
from investment_panel.core.db import db, init_db, json_dumps, query_rows
from investment_panel.core.decision import refresh_decision_read_models
from investment_panel.core.instruments import infer_asset_class, normalize_symbol


BROKER_BLOCKING_STATUSES = {
    "gateway_offline",
    "stale_session",
    "quote_entitlement_failure",
    "rate_limited",
    "stale_data",
    "malformed_symbol",
    "session_failure",
    "missing_dependency",
    "disabled",
    "missing",
}
ADVISORY_AUTHORITY = "advisory_paper_only"


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


class IBKRProvider:
    """Health-first adapter for Interactive Brokers TWS API / IB Gateway."""

    name = "ibkr"

    def __init__(self, config: Any):
        self.config = config

    def collect(self, symbols: list[str]) -> BrokerSnapshot:
        checked_at = datetime.now(UTC)
        if not self.config.enabled:
            return BrokerSnapshot(ProviderStatus(self.name, "disabled", "IBKR source is disabled in config.", checked_at))
        if importlib.util.find_spec("ibapi") is None:
            return BrokerSnapshot(
                ProviderStatus(
                    self.name,
                    "missing_dependency",
                    "Install ibapi and run TWS or IB Gateway to enable IBKR sync.",
                    checked_at,
                    capabilities=ibkr_capabilities(),
                )
            )
        started = time.perf_counter()
        if not tcp_open(self.config.host, self.config.port, timeout=1.0):
            return BrokerSnapshot(
                ProviderStatus(
                    self.name,
                    "gateway_offline",
                    f"TWS API / IB Gateway is not reachable at {self.config.host}:{self.config.port}.",
                    checked_at,
                    latency_ms=round((time.perf_counter() - started) * 1000, 2),
                    capabilities=ibkr_capabilities(),
                )
            )
        return BrokerSnapshot(
            ProviderStatus(
                self.name,
                "session_failure",
                "TWS API socket is reachable, but no account sync session completed in this run.",
                checked_at,
                account_id=self.config.account_id,
                account_mode="paper" if self.config.paper_only else "unknown",
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                capabilities=ibkr_capabilities(),
            )
        )


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


def persist_broker_snapshot(con: Any, snapshot: BrokerSnapshot) -> None:
    status = snapshot.status
    con.execute(
        """
        INSERT OR REPLACE INTO broker_provider_status
        (provider, checked_at, status, health, detail, account_id, account_mode,
         session_started_at, last_data_at, latency_ms, capabilities, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            status.provider,
            status.checked_at,
            status.status,
            status.health,
            status.detail,
            status.account_id,
            status.account_mode,
            status.session_started_at,
            status.last_data_at,
            status.latency_ms,
            json_dumps(status.capabilities),
            json_dumps(status.raw),
        ],
    )
    record_source_health(con, f"broker:{status.provider}", status.status, status.detail)
    record_provider_run(con, status.provider, "broker_sync", status.checked_at, status.status, status.detail, status.raw)

    if status.status != "ok":
        return

    con.execute("DELETE FROM broker_accounts WHERE provider = ?", [status.provider])
    con.execute("DELETE FROM broker_positions WHERE provider = ?", [status.provider])
    con.execute("DELETE FROM broker_orders WHERE provider = ?", [status.provider])
    con.execute("DELETE FROM broker_fills WHERE provider = ?", [status.provider])

    for account in snapshot.accounts:
        con.execute(
            """
            INSERT OR REPLACE INTO broker_accounts
            (provider, account_id, account_mode, currency, cash, buying_power, net_liquidation,
             margin_requirement, excess_liquidity, day_pnl, total_pnl, updated_at, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                status.provider,
                account.get("account_id") or status.account_id or "UNKNOWN",
                account.get("account_mode") or status.account_mode,
                account.get("currency", "USD"),
                account.get("cash"),
                account.get("buying_power"),
                account.get("net_liquidation"),
                account.get("margin_requirement"),
                account.get("excess_liquidity"),
                account.get("day_pnl"),
                account.get("total_pnl"),
                account.get("updated_at") or status.last_data_at or status.checked_at,
                json_dumps(account.get("raw") or account),
            ],
        )
    for position in snapshot.positions:
        symbol = normalize_symbol(str(position.get("symbol") or ""))
        if not symbol:
            continue
        con.execute(
            """
            INSERT OR REPLACE INTO broker_positions
            (provider, account_id, symbol, asset_class, quantity, average_cost, market_price,
             market_value, unrealized_pnl, realized_pnl, updated_at, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                status.provider,
                position.get("account_id") or status.account_id or "UNKNOWN",
                symbol,
                position.get("asset_class") or infer_asset_class(symbol),
                position.get("quantity"),
                position.get("average_cost") or position.get("avg_cost"),
                position.get("market_price"),
                position.get("market_value"),
                position.get("unrealized_pnl"),
                position.get("realized_pnl"),
                position.get("updated_at") or status.last_data_at or status.checked_at,
                json_dumps(position.get("raw") or position),
            ],
        )
        con.execute(
            """
            INSERT INTO instruments (symbol, name, asset_class, sector, industry, category, source)
            SELECT ?, ?, ?, NULL, NULL, 'broker-position', ?
            WHERE NOT EXISTS (SELECT 1 FROM instruments WHERE symbol = ?)
            """,
            [symbol, position.get("name") or symbol, position.get("asset_class") or infer_asset_class(symbol), status.provider, symbol],
        )
    for order in snapshot.orders:
        con.execute(
            """
            INSERT OR REPLACE INTO broker_orders
            (provider, account_id, order_id, symbol, side, order_type, quantity, limit_price,
             status, submitted_at, updated_at, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                status.provider,
                order.get("account_id") or status.account_id or "UNKNOWN",
                str(order.get("order_id") or uuid4().hex),
                normalize_symbol(str(order.get("symbol") or "")),
                order.get("side"),
                order.get("order_type"),
                order.get("quantity"),
                order.get("limit_price"),
                order.get("status"),
                order.get("submitted_at"),
                order.get("updated_at") or status.checked_at,
                json_dumps(order.get("raw") or order),
            ],
        )
    for fill in snapshot.fills:
        con.execute(
            """
            INSERT OR REPLACE INTO broker_fills
            (provider, account_id, fill_id, order_id, symbol, side, quantity, price, filled_at, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                status.provider,
                fill.get("account_id") or status.account_id or "UNKNOWN",
                str(fill.get("fill_id") or uuid4().hex),
                str(fill.get("order_id") or ""),
                normalize_symbol(str(fill.get("symbol") or "")),
                fill.get("side"),
                fill.get("quantity"),
                fill.get("price"),
                fill.get("filled_at") or status.checked_at,
                json_dumps(fill.get("raw") or fill),
            ],
        )
    for quote in snapshot.market_snapshots:
        symbol = normalize_symbol(str(quote.get("symbol") or ""))
        if not symbol:
            continue
        con.execute(
            """
            INSERT OR REPLACE INTO broker_market_snapshots
            (provider, symbol, observed_at, bid, ask, last, close, volume, entitlement_status, data_status, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                status.provider,
                symbol,
                quote.get("observed_at") or status.last_data_at or status.checked_at,
                quote.get("bid"),
                quote.get("ask"),
                quote.get("last"),
                quote.get("close"),
                quote.get("volume"),
                quote.get("entitlement_status", "ok"),
                quote.get("data_status", "fresh"),
                json_dumps(quote.get("raw") or quote),
            ],
        )
        if quote.get("last") is not None:
            con.execute(
                """
                INSERT OR REPLACE INTO quotes_intraday
                (symbol, observed_at, price, change_pct, change_abs, currency, source, raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    symbol,
                    quote.get("observed_at") or status.checked_at,
                    quote.get("last"),
                    quote.get("change_pct"),
                    quote.get("change_abs"),
                    quote.get("currency", "USD"),
                    f"broker:{status.provider}",
                    json_dumps(quote.get("raw") or quote),
                ],
            )
    run_id = stable_id(f"{status.provider}:{status.checked_at.isoformat()}:scanner")
    for signal in snapshot.scanner_signals:
        symbol = normalize_symbol(str(signal.get("symbol") or ""))
        if not symbol:
            continue
        con.execute(
            """
            INSERT OR REPLACE INTO broker_scanner_signals
            (provider, run_id, symbol, observed_at, signal_type, rank, score, metrics, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                status.provider,
                signal.get("run_id") or run_id,
                symbol,
                signal.get("observed_at") or status.checked_at,
                signal.get("signal_type") or "scanner",
                signal.get("rank"),
                signal.get("score"),
                json_dumps(signal.get("metrics") or {}),
                json_dumps(signal.get("raw") or signal),
            ],
        )


def build_and_persist_agent_recommendations(con: Any, policy: BrokerPolicyConfig) -> list[dict[str, Any]]:
    rows = build_agent_recommendations(con, policy)
    con.execute("DELETE FROM broker_agent_recommendations")
    con.execute("DELETE FROM broker_policy_checks")
    for row in rows:
        con.execute(
            """
            INSERT OR REPLACE INTO broker_agent_recommendations
            (id, symbol, as_of, action, status, actionability_score, thesis, setup_type,
             entry_trigger, invalidation_stop, target, risk_reward, sizing, max_notional,
             portfolio_impact, evidence, blockers, data_freshness, paper_order_preview,
             policy_checks, authority)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                row["id"],
                row["symbol"],
                row["as_of"],
                row["action"],
                row["status"],
                row["actionability_score"],
                row["thesis"],
                row["setup_type"],
                row["entry_trigger"],
                row["invalidation_stop"],
                row["target"],
                row["risk_reward"],
                json_dumps(row["sizing"]),
                row["max_notional"],
                json_dumps(row["portfolio_impact"]),
                json_dumps(row["evidence"]),
                json_dumps(row["blockers"]),
                json_dumps(row["data_freshness"]),
                json_dumps(row["paper_order_preview"]),
                json_dumps(row["policy_checks"]),
                row["authority"],
            ],
        )
        for check in row["policy_checks"]:
            con.execute(
                """
                INSERT OR REPLACE INTO broker_policy_checks
                (id, recommendation_id, symbol, checked_at, check_name, status, detail, raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    stable_id(f"{row['id']}:{check['name']}"),
                    row["id"],
                    row["symbol"],
                    row["as_of"],
                    check["name"],
                    check["status"],
                    check["detail"],
                    json_dumps(check),
                ],
            )
    return rows


def build_agent_recommendations(con: Any, policy: BrokerPolicyConfig) -> list[dict[str, Any]]:
    queue = query_rows(
        con,
        """
        SELECT *
        FROM decision_queue
        ORDER BY action_score DESC NULLS LAST, rank ASC NULLS LAST
        LIMIT 75
        """,
    )
    accounts = broker_accounts(con)
    account = accounts[0] if accounts else manual_account_proxy(con, policy)
    health = ibkr_health(con)
    positions = {row["symbol"]: row for row in effective_portfolio_rows(con)}
    now = datetime.now(UTC)
    recommendations = []
    for row in queue:
        symbol = str(row.get("symbol") or "").upper()
        basis = parse_json(row.get("decision_basis"))
        source_counts = basis.get("source_counts") if isinstance(basis.get("source_counts"), dict) else {}
        blockers = [str(item) for item in parse_json(row.get("blocking_gates")) or []]
        checks = policy_checks(row, basis, health, account, positions.get(symbol), policy, source_counts)
        blockers = sorted(set([*blockers, *[check["name"] for check in checks if check["status"] == "blocked"]]))
        price = float(row.get("latest_quote") or 0)
        buying_power = float(account.get("buying_power") or account.get("cash") or 0)
        max_notional = min(policy.max_trade_notional, buying_power * 0.05 if buying_power > 0 else policy.max_trade_notional)
        quantity = round(max_notional / price, 4) if price > 0 else 0.0
        status = "blocked" if blockers else "paper_ready" if row.get("action_grade") in {"Act", "Research"} else "monitor"
        action = "block" if blockers else "stage_paper_buy" if status == "paper_ready" else "monitor"
        evidence = recommendation_evidence(row, basis, source_counts)
        data_freshness = {
            "quote": row.get("quote_freshness"),
            "daily_analysis": row.get("daily_analysis_freshness"),
            "filing": row.get("filing_freshness"),
            "thesis": row.get("thesis_freshness"),
            "broker_account": health["status"],
            "account_required": policy.require_account_for_recommendations,
        }
        recommendations.append(
            {
                "id": stable_id(f"{symbol}:{row.get('as_of')}:{row.get('action_score')}"),
                "symbol": symbol,
                "as_of": now,
                "action": action,
                "status": status,
                "actionability_score": float(row.get("action_score") or row.get("score") or 0),
                "thesis": basis.get("summary") or f"{symbol} has a backend decision queue entry.",
                "setup_type": setup_type_for(row, source_counts),
                "entry_trigger": entry_trigger_for(row, price, blockers),
                "invalidation_stop": row.get("invalidation") or "Refresh evidence and stop if thesis or price setup is invalidated.",
                "target": target_for(price),
                "risk_reward": risk_reward_for(price, blockers),
                "sizing": {"side": "BUY", "quantity": quantity, "basis": "policy_max_notional", "buying_power": buying_power},
                "max_notional": round(max_notional, 2),
                "portfolio_impact": recommendation_portfolio_impact(account, positions.get(symbol), max_notional),
                "evidence": evidence,
                "blockers": blockers,
                "data_freshness": data_freshness,
                "paper_order_preview": {
                    "provider": "paper",
                    "broker_source_of_truth": "ibkr",
                    "side": "BUY",
                    "order_type": "limit",
                    "limit_price": price or None,
                    "quantity": quantity,
                    "notional": round(quantity * price, 2) if price > 0 else 0,
                    "live_trading": False,
                },
                "policy_checks": checks,
                "authority": ADVISORY_AUTHORITY,
            }
        )
    return recommendations


def stage_paper_order(con: Any, recommendation_id: str) -> dict[str, Any]:
    rows = query_rows(con, "SELECT * FROM broker_agent_recommendations WHERE id = ? LIMIT 1", [recommendation_id])
    if not rows:
        raise ValueError(f"recommendation not found: {recommendation_id}")
    rec = rows[0]
    preview = parse_json(rec.get("paper_order_preview"))
    blockers = parse_json(rec.get("blockers")) or []
    status = "blocked" if blockers or rec.get("status") == "blocked" else "staged"
    order_id = stable_id(f"paper:{recommendation_id}:{datetime.now(UTC).isoformat()}")
    now = datetime.now(UTC)
    con.execute(
        """
        INSERT OR REPLACE INTO broker_paper_orders
        (id, recommendation_id, provider, account_id, symbol, side, order_type,
         quantity, limit_price, notional, status, authority, created_at, updated_at,
         preview, audit_trail)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            order_id,
            recommendation_id,
            "paper",
            "LOCAL-PAPER",
            rec.get("symbol"),
            preview.get("side", "BUY"),
            preview.get("order_type", "limit"),
            preview.get("quantity"),
            preview.get("limit_price"),
            preview.get("notional"),
            status,
            ADVISORY_AUTHORITY,
            now,
            now,
            json_dumps(preview),
            json_dumps(
                [
                    {"at": now.isoformat(), "event": "paper_order_stage_requested"},
                    {"at": now.isoformat(), "event": status, "blockers": blockers},
                ]
            ),
        ],
    )
    return {"id": order_id, "status": status, "symbol": rec.get("symbol"), "blockers": blockers, "preview": preview}


def effective_portfolio_rows(con: Any) -> list[dict[str, Any]]:
    health = ibkr_health(con)
    if health["usable"]:
        return [
            {
                "symbol": row.get("symbol"),
                "quantity": row.get("quantity"),
                "avg_cost": row.get("average_cost"),
                "average_cost": row.get("average_cost"),
                "market_price": row.get("market_price"),
                "market_value": row.get("market_value"),
                "unrealized_pnl": row.get("unrealized_pnl"),
                "source": "ibkr",
                "provider": row.get("provider"),
                "account_id": row.get("account_id"),
                "updated_at": row.get("updated_at"),
                "asset_class": row.get("asset_class"),
            }
            for row in query_rows(
                con,
                """
                SELECT provider, account_id, symbol, asset_class, quantity, average_cost,
                       market_price, market_value, unrealized_pnl, updated_at
                FROM broker_positions
                WHERE provider = 'ibkr'
                ORDER BY symbol
                """,
            )
        ]
    stale_rows = query_rows(
        con,
        """
        SELECT provider, account_id, symbol, asset_class, quantity, average_cost,
               market_price, market_value, unrealized_pnl, updated_at
        FROM broker_positions
        WHERE provider = 'ibkr'
        ORDER BY symbol
        """,
    )
    if stale_rows:
        return [
            {
                "symbol": row.get("symbol"),
                "quantity": row.get("quantity"),
                "avg_cost": row.get("average_cost"),
                "average_cost": row.get("average_cost"),
                "market_price": row.get("market_price"),
                "market_value": row.get("market_value"),
                "unrealized_pnl": row.get("unrealized_pnl"),
                "source": "ibkr_stale",
                "provider": row.get("provider"),
                "account_id": row.get("account_id"),
                "updated_at": row.get("updated_at"),
                "asset_class": row.get("asset_class"),
            }
            for row in stale_rows
        ]
    return [
        {**row, "source": "manual"}
        for row in query_rows(
            con,
            """
            SELECT symbol, quantity, avg_cost, avg_cost AS average_cost, purchase_date,
                   CASE
                       WHEN purchase_date IS NULL THEN NULL
                       ELSE date_diff('day', purchase_date, current_date)
                   END AS holding_days,
                   CASE
                       WHEN purchase_date IS NULL THEN 'unknown'
                       WHEN date_diff('day', purchase_date, current_date) > 365 THEN 'long_term'
                       ELSE 'short_term'
                   END AS tax_lot_term,
                   notes
            FROM portfolio_positions
            ORDER BY symbol
            """,
        )
    ]


def ibkr_health(con: Any) -> dict[str, Any]:
    rows = query_rows(con, "SELECT * FROM broker_provider_status WHERE provider = 'ibkr' LIMIT 1")
    if not rows:
        return {"provider": "ibkr", "status": "missing", "usable": False, "detail": "IBKR has not synced yet."}
    row = rows[0]
    status = str(row.get("status") or "missing")
    last_data = parse_dt(row.get("last_data_at") or row.get("checked_at"))
    stale = bool(last_data and datetime.now(UTC) - last_data > timedelta(minutes=15))
    if stale and status == "ok":
        status = "stale_data"
    return {
        "provider": "ibkr",
        "status": status,
        "usable": status == "ok" and not stale,
        "detail": row.get("detail"),
        "account_id": row.get("account_id"),
        "account_mode": row.get("account_mode"),
        "checked_at": row.get("checked_at"),
        "last_data_at": row.get("last_data_at"),
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


def policy_checks(
    row: dict[str, Any],
    basis: dict[str, Any],
    health: dict[str, Any],
    account: dict[str, Any],
    position: dict[str, Any] | None,
    policy: BrokerPolicyConfig,
    source_counts: dict[str, Any],
) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []

    def add(name: str, blocked: bool, detail: str) -> None:
        checks.append({"name": name, "status": "blocked" if blocked else "passed", "detail": detail})

    freshness = basis.get("freshness") if isinstance(basis.get("freshness"), dict) else {}
    add("stale_data", row.get("freshness_status") != "fresh" or any(value in {"stale", "failed", "missing", "unknown"} for value in [freshness.get("quote_freshness"), freshness.get("daily_analysis_freshness")]), "Quote and daily analysis must be fresh.")
    add(
        "broker_account_sync_unhealthy",
        policy.require_account_for_recommendations and not health.get("usable"),
        f"IBKR status is {health.get('status')}; account required: {policy.require_account_for_recommendations}.",
    )
    projected_notional = min(policy.max_trade_notional, float(account.get("buying_power") or policy.max_trade_notional) * 0.05 if account else policy.max_trade_notional)
    add("exceeds_max_notional", projected_notional > policy.max_trade_notional, f"Projected notional {projected_notional:.2f}; max {policy.max_trade_notional:.2f}.")
    net_liq = float(account.get("net_liquidation") or 0)
    existing_value = float((position or {}).get("market_value") or 0)
    projected_weight = ((existing_value + projected_notional) / net_liq * 100) if net_liq > 0 else 0
    add("concentration_limit", projected_weight > policy.max_position_weight_pct, f"Projected position weight {projected_weight:.2f}%.")
    total_evidence = int(basis.get("evidence_count") or 0)
    primary_evidence = int(basis.get("primary_evidence_count") or 0)
    add("required_evidence_missing", total_evidence < policy.min_total_evidence_count or primary_evidence < policy.min_primary_evidence_count, f"Evidence total {total_evidence}; primary {primary_evidence}.")
    asset_class = str(basis.get("asset_class") or "").lower()
    add("unsupported_asset_class", asset_class not in {"equity", "etf"}, f"Asset class {asset_class or 'unknown'} is not enabled for paper staging.")
    catalyst = str(basis.get("catalyst") or row.get("catalyst_window") or "")
    add("catalyst_earnings_rule", bool(catalyst and int(source_counts.get("earnings_setup") or 0) == 0 and "earnings" in catalyst.lower()), "Earnings/catalyst setups require explicit earnings setup evidence.")
    return checks


def manual_account_proxy(con: Any, policy: BrokerPolicyConfig) -> dict[str, Any]:
    """Sizing proxy for market-data-only mode when Joe provides portfolio rows manually."""

    rows = query_rows(
        con,
        """
        SELECT sum(quantity * avg_cost) AS cost_basis
        FROM portfolio_positions
        """
    )
    cost_basis = float((rows[0] if rows else {}).get("cost_basis") or 0)
    proxy_value = max(cost_basis, policy.max_trade_notional)
    return {
        "account_id": "MANUAL-PORTFOLIO",
        "account_mode": "manual_market_data_only",
        "cash": policy.max_trade_notional,
        "buying_power": policy.max_trade_notional,
        "net_liquidation": proxy_value,
        "source": "manual_proxy",
    }


def broker_status_rows(con: Any) -> list[dict[str, Any]]:
    return [_compact_empty_fields(decode_broker_row(row)) for row in query_rows(con, "SELECT * FROM broker_provider_status ORDER BY provider")]


def _compact_empty_fields(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if value not in (None, "", [], {})}


def broker_accounts(con: Any) -> list[dict[str, Any]]:
    return query_rows(con, "SELECT * FROM broker_accounts ORDER BY provider, account_id")


def broker_positions(con: Any) -> list[dict[str, Any]]:
    return query_rows(con, "SELECT * FROM broker_positions ORDER BY provider, account_id, symbol")


def broker_market_snapshots(con: Any) -> list[dict[str, Any]]:
    return query_rows(
        con,
        """
        SELECT *
        FROM broker_market_snapshots
        QUALIFY row_number() OVER (PARTITION BY provider, symbol ORDER BY observed_at DESC) = 1
        ORDER BY provider, symbol
        """,
    )


def broker_scanner_signals(con: Any) -> list[dict[str, Any]]:
    return [decode_broker_row(row) for row in query_rows(con, "SELECT * FROM broker_scanner_signals ORDER BY observed_at DESC, rank ASC NULLS LAST LIMIT 200")]


def agent_recommendations(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(con, "SELECT * FROM broker_agent_recommendations ORDER BY status DESC, actionability_score DESC LIMIT 100")
    return [decode_broker_row(row) for row in rows]


def paper_orders(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(con, "SELECT * FROM broker_paper_orders ORDER BY created_at DESC LIMIT 100")
    return [decode_broker_row(row) for row in rows]


def record_source_health(con: Any, source: str, status: str, detail: str) -> None:
    con.execute(
        """
        INSERT OR REPLACE INTO source_health (source, checked_at, status, detail, source_url)
        VALUES (?, ?, ?, ?, ?)
        """,
        [source, datetime.now(UTC), status, detail, source],
    )


def record_provider_run(con: Any, provider: str, capability: str, finished_at: datetime, status: str, detail: str, raw: dict[str, Any]) -> None:
    con.execute(
        """
        INSERT OR REPLACE INTO provider_runs
        (id, provider, capability, started_at, finished_at, status, detail, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [stable_id(f"{provider}:{capability}:{finished_at.isoformat()}"), provider, capability, finished_at, finished_at, status, detail, json_dumps(raw)],
    )


def recommendation_evidence(row: dict[str, Any], basis: dict[str, Any], source_counts: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = []
    for reason in parse_json(row.get("inclusion_reasons")) or basis.get("inclusion_reasons") or []:
        evidence.append({"type": "inclusion_reason", "detail": str(reason)})
    for source, count in sorted(source_counts.items()):
        if int(count or 0) > 0:
            evidence.append({"type": "source_count", "source": source, "count": int(count or 0)})
    return evidence[:12]


def recommendation_portfolio_impact(account: dict[str, Any], position: dict[str, Any] | None, notional: float) -> dict[str, Any]:
    net_liq = float(account.get("net_liquidation") or 0)
    current_value = float((position or {}).get("market_value") or 0)
    return {
        "owned": bool(position),
        "current_value": current_value,
        "projected_add_notional": round(notional, 2),
        "projected_weight_pct": round(((current_value + notional) / net_liq * 100), 2) if net_liq > 0 else None,
        "account_net_liquidation": net_liq or None,
    }


def setup_type_for(row: dict[str, Any], source_counts: dict[str, Any]) -> str:
    if int(source_counts.get("earnings_setup") or 0):
        return "earnings_setup"
    if int(source_counts.get("sepa") or 0):
        return "technical_breakout"
    if int(source_counts.get("arco_thesis") or 0):
        return "thesis_followup"
    return str(row.get("source_cluster") or "multi_source")


def entry_trigger_for(row: dict[str, Any], price: float, blockers: list[str]) -> str:
    if blockers:
        return "Blocked until the listed market-data, evidence, or sizing gates clear."
    return f"Paper-stage only after price confirms near {price:.2f} with fresh broker quote." if price > 0 else "Paper-stage only after a fresh broker quote is loaded."


def target_for(price: float) -> str:
    return f"{price * 1.08:.2f} first target / {price * 1.15:.2f} stretch target" if price > 0 else "Needs fresh quote before target can be computed."


def risk_reward_for(price: float, blockers: list[str]) -> str:
    if blockers:
        return "Not applicable while blocked."
    return "Plan requires at least 2:1 reward/risk before staging paper order." if price > 0 else "Needs quote before reward/risk can be computed."


def decode_broker_row(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    for key in ("capabilities", "raw", "metrics", "sizing", "portfolio_impact", "evidence", "blockers", "data_freshness", "paper_order_preview", "policy_checks", "preview", "audit_trail"):
        if key in decoded:
            decoded[key] = parse_json(decoded[key])
    return decoded


def parse_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return {}
    try:
        return json.loads(value)
    except Exception:
        return {}


def parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.astimezone(UTC)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.astimezone(UTC)


def tcp_open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def stable_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def ibkr_capabilities() -> list[str]:
    return ["positions", "cash", "buying_power", "margin_risk", "pnl", "orders", "fills", "account_mode", "market_snapshots", "options", "scanner"]


def moomoo_capabilities() -> list[str]:
    return ["quotes", "kline", "order_book", "stock_filters", "capital_flow", "options", "simulated_paper"]
