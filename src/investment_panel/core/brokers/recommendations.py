"""Agent recommendation building and paper-order staging."""

from __future__ import annotations
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from investment_panel.core.config import AppConfig, BrokerPolicyConfig, load_config
from investment_panel.core.db import db, init_db, json_dumps, query_rows

from investment_panel.core.brokers.constants import ADVISORY_AUTHORITY
from investment_panel.core.brokers.coerce import parse_json, stable_id
from investment_panel.core.brokers.ibkr import ibkr_health
from investment_panel.core.brokers.read_models import broker_accounts, effective_portfolio_rows
from investment_panel.core.brokers.policy import manual_account_proxy, policy_checks



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
