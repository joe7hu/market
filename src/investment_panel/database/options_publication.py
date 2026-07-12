"""Build the compact, versioned professional options-radar publication."""

from __future__ import annotations

from typing import Any

from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


def publication_models(
    runtime: DatabaseRuntime,
    run_id: Any,
    *,
    feature_version: str,
    strategy_revision: int,
) -> dict[str, list[dict[str, Any]]]:
    with runtime.read(JOB_PROFILE) as connection:
        rows = connection.execute(
            """
            SELECT decision.id::text AS opportunity_id,
                   decision.id::text AS candidate_event_id,
                   decision.id::text AS event_id, instrument.symbol,
                   instrument.symbol AS ticker, decision.state, decision.rank,
                   decision.score, option_decision.tier, option_decision.structure,
                   option_decision.entry_price, option_decision.exit_cost_estimate,
                   option_decision.secured_cash, option_decision.max_profit,
                   option_decision.max_loss, option_decision.break_even,
                   option_decision.effective_assignment_price,
                   option_decision.probability_profit,
                   option_decision.probability_assignment,
                   option_decision.probability_touch, option_decision.expected_value,
                   option_decision.risk_adjusted_expectancy,
                   option_decision.tail_cvar, option_decision.data_confidence,
                   option_decision.execution_confidence, option_decision.details,
                   quote.observed_at AS snapshot_time,
                   snapshot.source_id AS data_source, snapshot.market_session,
                   contract.id::text AS contract_id, contract.expiration,
                   contract.strike, contract.option_type, quote.underlying_price,
                   quote.bid, quote.ask, quote.mid, quote.mid AS premium_mid,
                   quote.volume, quote.open_interest, quote.provider_iv AS iv,
                   quote.provider_delta AS delta, feature.dte, feature.spread_pct,
                   feature.liquidity_score, feature.convexity_score,
                   feature.required_2x_price, feature.required_5x_price,
                   feature.required_10x_price, feature.required_move_pct,
                   option_decision.buy_under, decision.reasons AS top_reasons,
                   decision.blockers, decision.quality_status,
                   jsonb_build_object(
                       'expiration', contract.expiration,
                       'strike', contract.strike,
                       'option_type', contract.option_type,
                       'feature_version', feature.feature_version
                   ) AS raw
            FROM analysis.decision decision
            JOIN analysis.option_decision option_decision
              ON option_decision.decision_id = decision.id
            JOIN analysis.option_feature feature
              ON feature.run_id = decision.run_id
             AND feature.contract_id = option_decision.contract_id
            JOIN raw.option_quote quote
              ON quote.snapshot_id = option_decision.snapshot_id
             AND quote.contract_id = option_decision.contract_id
             AND quote.observed_at = option_decision.quote_observed_at
            JOIN raw.option_snapshot snapshot ON snapshot.id = quote.snapshot_id
            JOIN catalog.option_contract contract ON contract.id = quote.contract_id
            JOIN catalog.instrument instrument
              ON instrument.id = contract.underlying_instrument_id
            WHERE decision.run_id = %s
            ORDER BY decision.score DESC NULLS LAST,
                     decision.rank NULLS LAST, decision.id
            """,
            [run_id],
        ).fetchall()
        rejected = connection.execute(
            """
            SELECT instrument.symbol, sum(summary.reject_count) AS reject_count
            FROM analysis.reject_summary summary
            JOIN catalog.instrument instrument ON instrument.id = summary.instrument_id
            WHERE summary.run_id = %s GROUP BY instrument.symbol
            """,
            [run_id],
        ).fetchall()
        account = connection.execute(
            "SELECT net_liquidation, cash_balance, buying_power, observed_at "
            "FROM raw.broker_account_snapshot ORDER BY observed_at DESC, id DESC LIMIT 1"
        ).fetchone()
    all_rows = [dict(row) for row in rows]
    _add_contract_fields(all_rows, feature_version, strategy_revision, dict(account) if account else None)
    actionable = _shortlist([row for row in all_rows if row.get("state") != "REJECTED"])
    symbol_summaries = _symbol_summaries(all_rows, [dict(row) for row in rejected])
    snapshots = _unique_contract_rows(all_rows, (
        "snapshot_time", "ticker", "underlying_price", "expiration", "strike",
        "option_type", "bid", "ask", "mid", "volume", "open_interest", "iv",
        "delta", "dte", "spread_pct", "data_source", "contract_id", "raw",
    ))
    features = _unique_contract_rows(all_rows, (
        "snapshot_time", "contract_id", "ticker", "required_2x_price",
        "required_5x_price", "required_10x_price", "required_move_pct",
        "liquidity_score", "convexity_score", "raw",
    ))
    latest = max(
        (row.get("snapshot_time") for row in all_rows if row.get("snapshot_time") is not None),
        default=None,
    )
    rejected_count = sum(int(row.get("reject_count") or 0) for row in rejected)
    global_summary = [{
        "stable_key": "global",
        "contract_version": 2,
        "feature_version": feature_version,
        "strategy_revision": strategy_revision,
        "publication_cutoff": latest,
        "latest_complete_quote_time": latest,
        "source": all_rows[0].get("data_source") if all_rows else None,
        "market_session": all_rows[0].get("market_session") if all_rows else None,
        "scanned_contracts": len(all_rows) + rejected_count,
        "eligible_contracts": sum(row.get("state") != "REJECTED" for row in all_rows),
        "shortlist_count": len(actionable),
        "cash_secured_put_count": sum(row.get("structure") == "cash_secured_put" for row in actionable),
        "ready_count": sum(row.get("state") == "READY" for row in actionable),
        "setup_count": sum(row.get("state") == "SETUP" for row in actionable),
        "watch_count": sum(row.get("state") == "WATCH" for row in actionable),
        "learning_coverage": 1.0 if all_rows else 0.0,
        "shadow_only": True,
    }]
    return {
        "option_radar_opportunity": actionable,
        "candidate_event": all_rows,
        "option_radar_summary": global_summary,
        "option_radar_symbol_summary": symbol_summaries,
        "option_snapshot": snapshots,
        "option_features": features,
    }


def _add_contract_fields(
    rows: list[dict[str, Any]],
    feature_version: str,
    strategy_revision: int,
    account: dict[str, Any] | None,
) -> None:
    nav = float(account["net_liquidation"]) if account and account.get("net_liquidation") is not None else None
    for row in rows:
        row.update({
            "decision_id": row["candidate_event_id"],
            "rank_score": row.get("score"),
            "calibrated_probability": None,
            "contract_version": 2,
            "feature_version": feature_version,
            "strategy_revision": strategy_revision,
            "analysis_cutoff": row.get("snapshot_time"),
            "quote_observed_at": row.get("snapshot_time"),
        })
        if not row.get("structure"):
            row["structure"] = "long_call" if row.get("option_type") == "call" else "long_put"
        risk_cap = 0.05 if row["structure"] == "cash_secured_put" else 0.0025
        capital_at_risk = float(row.get("secured_cash") or row.get("max_loss") or 0)
        risk_budget = nav * risk_cap if nav is not None else None
        row["risk_budget"] = risk_budget
        row["advisory_max_contracts"] = int(risk_budget // capital_at_risk) if risk_budget is not None and capital_at_risk > 0 else 0
        row["portfolio_context_status"] = "complete" if nav is not None else "missing_nav"
        if nav is None and "missing_portfolio_value" not in row["blockers"]:
            row["blockers"] = [*row["blockers"], "missing_portfolio_value"]


def _shortlist(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        best.setdefault((str(row["ticker"]), str(row["structure"])), row)
    return sorted(
        best.values(),
        key=lambda row: (-float(row.get("score") or 0), str(row.get("ticker") or "")),
    )[:10]


def _symbol_summaries(rows: list[dict[str, Any]], rejected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries: dict[str, dict[str, Any]] = {}
    for row in rows:
        summary = summaries.setdefault(row["symbol"], _empty_symbol_summary(row["symbol"]))
        state = str(row["state"]).lower()
        if state == "rejected":
            continue
        summary[f"{state}_count"] += 1
    for row in rejected:
        summary = summaries.setdefault(row["symbol"], _empty_symbol_summary(row["symbol"]))
        summary["reject_count"] = int(row.get("reject_count") or 0)
    return list(summaries.values())


def _empty_symbol_summary(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "ticker": symbol,
        "fire_count": 0,
        "setup_count": 0,
        "watch_count": 0,
        "reject_count": 0,
    }


def _unique_contract_rows(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    return list({str(row["contract_id"]): {key: row[key] for key in keys} for row in rows}.values())
