"""Broker policy checks and manual account proxy."""

from __future__ import annotations
from typing import Any, Protocol
from investment_panel.core.config import AppConfig, BrokerPolicyConfig, load_config
from investment_panel.core.db import db, init_db, json_dumps, query_rows



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
