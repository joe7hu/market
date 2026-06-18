"""Agent recommendation building and paper-order staging."""

from __future__ import annotations
from datetime import UTC, datetime
from typing import Any
from investment_panel.core.config import BrokerPolicyConfig
from investment_panel.core.db import json_dumps, query_rows

from investment_panel.core.brokers.constants import ADVISORY_AUTHORITY
from investment_panel.core.brokers.coerce import parse_json, stable_id
from investment_panel.core.brokers.ibkr import ibkr_health
from investment_panel.core.brokers.read_models import broker_accounts, effective_portfolio_rows
from investment_panel.core.brokers.policy import manual_account_proxy, policy_checks
from investment_panel.core.brokers.recommendation_decisions import build_recommendation_decision



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
        decision = build_recommendation_decision(
            row,
            basis=basis,
            source_counts=source_counts,
            base_blockers=blockers,
            policy_checks=checks,
            health=health,
            account=account,
            position=positions.get(symbol),
            policy=policy,
        )
        recommendations.append(
            {
                "id": stable_id(f"{symbol}:{row.get('as_of')}:{row.get('action_score')}"),
                "symbol": symbol,
                "as_of": now,
                "action": decision["action"],
                "status": decision["status"],
                "actionability_score": float(row.get("action_score") or row.get("score") or 0),
                "thesis": basis.get("summary") or f"{symbol} has a backend decision queue entry.",
                "setup_type": decision["setup_type"],
                "entry_trigger": decision["entry_trigger"],
                "invalidation_stop": row.get("invalidation") or "Refresh evidence and stop if thesis or price setup is invalidated.",
                "target": decision["target"],
                "risk_reward": decision["risk_reward"],
                "sizing": decision["sizing"],
                "max_notional": decision["max_notional"],
                "portfolio_impact": decision["portfolio_impact"],
                "evidence": decision["evidence"],
                "blockers": decision["blockers"],
                "data_freshness": decision["data_freshness"],
                "paper_order_preview": decision["paper_order_preview"],
                "policy_checks": decision["policy_checks"],
                "authority": decision["authority"],
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
