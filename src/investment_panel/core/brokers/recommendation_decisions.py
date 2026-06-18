"""Broker recommendation decision packet construction."""

from __future__ import annotations

from typing import Any

from investment_panel.core.config import BrokerPolicyConfig

from investment_panel.core.brokers.constants import ADVISORY_AUTHORITY
from investment_panel.core.brokers.coerce import parse_json


def build_recommendation_decision(
    row: dict[str, Any],
    *,
    basis: dict[str, Any],
    source_counts: dict[str, Any],
    base_blockers: list[str],
    policy_checks: list[dict[str, str]],
    health: dict[str, Any],
    account: dict[str, Any],
    position: dict[str, Any] | None,
    policy: BrokerPolicyConfig,
) -> dict[str, Any]:
    """Build the trade-decision fields for an agent recommendation row."""

    price = usable_quote(row)
    checks = list(policy_checks)
    if price is None and recommendation_can_stage(row):
        checks.append(
            {
                "name": "missing_usable_quote",
                "status": "blocked",
                "detail": "A positive latest quote is required before paper staging.",
            }
        )
    blockers = sorted(set([*base_blockers, *[check["name"] for check in checks if check["status"] == "blocked"]]))
    buying_power = float(account.get("buying_power") or account.get("cash") or 0)
    max_notional = recommendation_max_notional(account, policy)
    quantity = round(max_notional / price, 4) if price else 0.0
    status = recommendation_status(row, blockers)
    action = recommendation_action(status, blockers)
    return {
        "action": action,
        "status": status,
        "setup_type": setup_type_for(row, source_counts),
        "entry_trigger": entry_trigger_for(row, price, blockers),
        "target": target_for(price),
        "risk_reward": risk_reward_for(price, blockers),
        "sizing": {"side": "BUY", "quantity": quantity, "basis": "policy_max_notional", "buying_power": buying_power},
        "max_notional": round(max_notional, 2),
        "portfolio_impact": recommendation_portfolio_impact(account, position, max_notional),
        "evidence": recommendation_evidence(row, basis, source_counts),
        "blockers": blockers,
        "data_freshness": recommendation_data_freshness(row, health, policy),
        "paper_order_preview": paper_order_preview(price, quantity),
        "policy_checks": checks,
        "authority": ADVISORY_AUTHORITY,
    }


def recommendation_can_stage(row: dict[str, Any]) -> bool:
    return row.get("action_grade") in {"Act", "Research"}


def usable_quote(row: dict[str, Any]) -> float | None:
    try:
        price = float(row.get("latest_quote") or 0)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def recommendation_status(row: dict[str, Any], blockers: list[str]) -> str:
    if blockers:
        return "blocked"
    return "paper_ready" if recommendation_can_stage(row) else "monitor"


def recommendation_action(status: str, blockers: list[str]) -> str:
    if blockers:
        return "block"
    return "stage_paper_buy" if status == "paper_ready" else "monitor"


def recommendation_max_notional(account: dict[str, Any], policy: BrokerPolicyConfig) -> float:
    buying_power = float(account.get("buying_power") or account.get("cash") or 0)
    return min(policy.max_trade_notional, buying_power * 0.05 if buying_power > 0 else policy.max_trade_notional)


def recommendation_data_freshness(row: dict[str, Any], health: dict[str, Any], policy: BrokerPolicyConfig) -> dict[str, Any]:
    return {
        "quote": row.get("quote_freshness"),
        "daily_analysis": row.get("daily_analysis_freshness"),
        "filing": row.get("filing_freshness"),
        "thesis": row.get("thesis_freshness"),
        "broker_account": health["status"],
        "account_required": policy.require_account_for_recommendations,
    }


def paper_order_preview(price: float | None, quantity: float) -> dict[str, Any]:
    return {
        "provider": "paper",
        "broker_source_of_truth": "ibkr",
        "side": "BUY",
        "order_type": "limit",
        "limit_price": price,
        "quantity": quantity,
        "notional": round(quantity * price, 2) if price else 0,
        "live_trading": False,
    }


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


def entry_trigger_for(row: dict[str, Any], price: float | None, blockers: list[str]) -> str:
    if blockers:
        return "Blocked until the listed market-data, evidence, or sizing gates clear."
    return f"Paper-stage only after price confirms near {price:.2f} with fresh broker quote." if price else "Paper-stage only after a fresh broker quote is loaded."


def target_for(price: float | None) -> str:
    return f"{price * 1.08:.2f} first target / {price * 1.15:.2f} stretch target" if price else "Needs fresh quote before target can be computed."


def risk_reward_for(price: float | None, blockers: list[str]) -> str:
    if blockers:
        return "Not applicable while blocked."
    return "Plan requires at least 2:1 reward/risk before staging paper order." if price else "Needs quote before reward/risk can be computed."
