"""Build the compact, versioned professional options-radar publication."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE
from investment_panel.analysis.option_recommendation import recommendation_fields


def publish_degraded_if_needed(repository: Any, code_version: str, feature_version: str, _strategy_key: str) -> dict[str, Any]:
    """Replace an incompatible legacy fallback when no regular-session publication exists."""
    current = repository.publication_rows("options-radar", "option_radar_summary")
    if len(current) == 1 and current[0].get("contract_version") == 3:
        return {"status": "skipped", "reason": "no_regular_session_snapshot", "option_features": 0, "decisions": 0}
    cutoff = datetime.now(UTC)
    run_id = repository.start_run(
        "options-radar", input_cutoff=cutoff, code_version=code_version,
        inputs={"reason": "no_complete_regular_session_publication"}, feature_versions={"option": feature_version},
    )
    summary = [{
        "stable_key": "global", "contract_version": 3, "feature_version": feature_version,
        "publication_cutoff": cutoff, "latest_complete_quote_time": None, "source": None,
        "market_session": "unavailable", "scanned_contracts": 0, "eligible_contracts": 0,
        "shortlist_count": 0, "cash_secured_put_count": 0, "ready_count": 0,
        "setup_count": 0, "watch_count": 0, "learning_coverage": 0.0, "shadow_only": True,
        "symbols_considered": 0, "symbols_with_chains": 0, "contracts_evaluated": 0,
        "degraded_reason": "no_complete_regular_session_publication",
    }]
    publication_id = repository.publish(
        run_id, "options-radar", {"option_radar_summary": summary, "option_radar_opportunity": [],
            "option_radar_symbol_summary": [], "candidate_event": [], "option_snapshot": [],
            "option_features": [], "option_calibration": [], "option_discovery_candidate": [],
            "option_gate_result": []},
        validation={"contract_version": 3, "degraded": True},
        complete_run_summary={"option_features": 0, "decisions": 0},
    )
    return {"status": "ok", "reason": "legacy_publication_replaced", "publication_id": str(publication_id),
            "option_features": 0, "decisions": 0}


def publication_models(
    runtime: DatabaseRuntime,
    run_id: Any,
    *,
    feature_version: str,
    strategy_revision: int,
    scanned_contracts: int,
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
                   option_decision.synthetic_legs, leg_depth.quotes AS leg_quotes,
                   quote.observed_at AS snapshot_time,
                   snapshot.source_id AS data_source, snapshot.market_session,
                   quote.market_data_status,
                   contract.id::text AS contract_id, contract.expiration,
                   contract.strike, contract.option_type, quote.underlying_price,
                   quote.bid, quote.ask, quote.mid, quote.mid AS premium_mid,
                   quote.volume, quote.open_interest, quote.provider_iv AS iv,
                   quote.bid_size, quote.ask_size, quote.last_trade_at, quote.captured_at,
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
            LEFT JOIN LATERAL (
                SELECT jsonb_agg(jsonb_build_object(
                    'contract_id', leg_quote.contract_id::text,
                    'observed_at', leg_quote.observed_at,
                    'captured_at', leg_quote.captured_at,
                    'last_trade_at', leg_quote.last_trade_at,
                    'market_data_status', leg_quote.market_data_status,
                    'bid_size', leg_quote.bid_size,
                    'ask_size', leg_quote.ask_size
                )) AS quotes
                FROM jsonb_array_elements(option_decision.synthetic_legs) leg
                JOIN raw.option_quote leg_quote
                  ON leg_quote.snapshot_id = option_decision.snapshot_id
                 AND leg_quote.contract_id = (leg->>'contract_id')::bigint
                 AND leg_quote.observed_at = option_decision.quote_observed_at
            ) leg_depth ON true
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
        discovery_rows = [dict(row) for row in connection.execute(
            """
            SELECT candidate.run_id::text AS discovery_run_id, instrument.symbol AS ticker,
                   instrument.symbol, candidate.stage, candidate.discovery_score,
                   candidate.surface_reason, candidate.primary_edge, candidate.causal_exposure,
                   candidate.catalyst_start, candidate.catalyst_end,
                   candidate.earliest_signal_at, candidate.timeliness,
                   candidate.source_root_count, candidate.evidence_completeness,
                   candidate.data_readiness, candidate.execution_ready,
                   candidate.next_evidence, candidate.details
            FROM analysis.option_discovery_candidate candidate
            JOIN catalog.instrument instrument ON instrument.id = candidate.instrument_id
            WHERE candidate.run_id = %s ORDER BY candidate.discovery_score DESC, instrument.symbol
            """,
            [run_id],
        ).fetchall()]
        gate_rows = [dict(row) for row in connection.execute(
            """
            SELECT instrument.symbol || ':' || gate.gate_code AS stable_key,
                   gate.run_id::text AS discovery_run_id, instrument.symbol AS ticker,
                   gate.gate_code, gate.passed, gate.reason, gate.evidence
            FROM analysis.option_gate_result gate
            JOIN catalog.instrument instrument ON instrument.id = gate.instrument_id
            WHERE gate.run_id = %s ORDER BY instrument.symbol, gate.gate_code
            """,
            [run_id],
        ).fetchall()]
        discovery_run = connection.execute(
            "SELECT started_at, provider, market_session, symbols_considered, symbols_with_chains, contracts_evaluated, universe_hash, manifest "
            "FROM analysis.option_discovery_run WHERE run_id = %s", [run_id]
        ).fetchone()
    all_rows = [dict(row) for row in rows]
    discovery_by_ticker = {str(row["ticker"]): row for row in discovery_rows}
    readiness_evaluated_at = discovery_run["started_at"] if discovery_run else datetime.now(UTC)
    for row in all_rows:
        discovery = discovery_by_ticker.get(str(row.get("ticker"))) or {}
        for key in (
            "stage", "primary_edge", "source_root_count", "evidence_completeness",
            "data_readiness", "execution_ready", "catalyst_start", "catalyst_end",
            "timeliness", "next_evidence",
        ):
            row[key] = discovery.get(key)
        row["data_readiness"] = _contract_readiness(row, readiness_evaluated_at)
    _add_contract_fields(all_rows, feature_version, strategy_revision, dict(account) if account else None)
    for row in all_rows:
        row["execution_ready"] = bool(
            row.get("state") == "READY"
            and row.get("data_readiness") == "A"
            and not row.get("blockers")
            and row.get("portfolio_context_status") == "complete"
        )
        if row.get("data_readiness") != "A" and "execution_data_not_grade_a" not in row["blockers"]:
            row["blockers"] = [*row["blockers"], "execution_data_not_grade_a"]
        row.update(recommendation_fields(row))
    actionable = _shortlist([row for row in all_rows if row.get("state") != "REJECTED"])
    published_tickers = {str(row["ticker"]) for row in actionable}
    for row in discovery_rows:
        ticker = str(row["ticker"])
        if ticker in published_tickers:
            row["stage"] = "PUBLISHED"
        elif row.get("stage") == "PUBLISHED":
            row["stage"] = "STRUCTURED"
        ticker_opportunities = [item for item in actionable if str(item.get("ticker")) == ticker]
        row["execution_ready"] = any(bool(item.get("execution_ready")) for item in ticker_opportunities)
    for row in all_rows:
        row["stage"] = "PUBLISHED" if str(row.get("ticker")) in published_tickers else row.get("stage")
    with runtime.transaction(JOB_PROFILE) as connection:
        connection.execute(
            """
            UPDATE analysis.option_discovery_candidate candidate
            SET stage = CASE
                WHEN instrument.symbol = ANY(%s::text[]) THEN 'PUBLISHED'
                WHEN candidate.stage = 'PUBLISHED' THEN 'STRUCTURED'
                ELSE candidate.stage END,
                execution_ready = instrument.symbol = ANY(%s::text[])
            FROM catalog.instrument instrument
            WHERE candidate.run_id = %s AND instrument.id = candidate.instrument_id
            """,
            [
                sorted(published_tickers),
                sorted(str(row["ticker"]) for row in discovery_rows if row.get("execution_ready")),
                run_id,
            ],
        )
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
    global_summary = [{
        "stable_key": "global",
        "contract_version": 3,
        "feature_version": feature_version,
        "strategy_revision": strategy_revision,
        "publication_cutoff": (discovery_run["manifest"] or {}).get("cutoff") if discovery_run else latest,
        "latest_complete_quote_time": latest,
        "source": all_rows[0].get("data_source") if all_rows else (discovery_run["provider"] if discovery_run else None),
        "market_session": all_rows[0].get("market_session") if all_rows else (discovery_run["market_session"] if discovery_run else None),
        "scanned_contracts": scanned_contracts,
        "symbols_considered": int(discovery_run["symbols_considered"]) if discovery_run else 0,
        "symbols_with_chains": int(discovery_run["symbols_with_chains"]) if discovery_run else 0,
        "contracts_evaluated": int(discovery_run["contracts_evaluated"]) if discovery_run else scanned_contracts,
        "universe_hash": discovery_run["universe_hash"] if discovery_run else None,
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
        "option_discovery_candidate": discovery_rows,
        "option_gate_result": gate_rows,
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
            "contract_version": 3,
            "feature_version": feature_version,
            "strategy_revision": strategy_revision,
            "analysis_cutoff": row.get("snapshot_time"),
            "quote_observed_at": row.get("snapshot_time"),
            "probability_semantics": (row.get("details") or {}).get("probability_semantics", "provisional_uncalibrated"),
            "probability_sample_size": (row.get("details") or {}).get("scenario_count"),
            "conservative_expected_value": (row.get("details") or {}).get("conservative_expected_value"),
            "optimistic_expected_value": (row.get("details") or {}).get("optimistic_expected_value"),
            "lower_95_expected_value": (row.get("details") or {}).get("lower_95_expected_value"),
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


def _contract_readiness(row: dict[str, Any], evaluated_at: datetime) -> str:
    quote_at = _as_datetime(row.get("captured_at") or row.get("quote_observed_at") or row.get("snapshot_time"))
    last_trade_at = _as_datetime(row.get("last_trade_at"))
    quote_age = _age_minutes(evaluated_at, quote_at)
    trade_age = _age_minutes(evaluated_at, last_trade_at)
    live_ibkr_depth = row.get("data_source") == "ibkr" and row.get("market_data_status") == "live"
    if row.get("structure") == "call_debit_spread":
        legs = list(row.get("leg_quotes") or [])
        if len(legs) < 2 or not all(
            _leg_is_grade_a(leg, evaluated_at, str(row.get("data_source") or "")) for leg in legs
        ):
            return "B" if quote_age <= 20 else "C" if quote_age <= 60 * 24 * 4 else "D"
    if (row.get("bid_size") or 0) > 0 and (row.get("ask_size") or 0) > 0 and quote_age <= 5 and (
        trade_age <= 5 or live_ibkr_depth
    ):
        return "A"
    if quote_age <= 20:
        return "B"
    if quote_age <= 60 * 24 * 4:
        return "C"
    return "D"


def _leg_is_grade_a(leg: dict[str, Any], evaluated_at: datetime, data_source: str) -> bool:
    observed_at = _as_datetime(leg.get("captured_at") or leg.get("observed_at"))
    last_trade_at = _as_datetime(leg.get("last_trade_at"))
    quote_age = _age_minutes(evaluated_at, observed_at)
    trade_age = _age_minutes(evaluated_at, last_trade_at)
    live_ibkr_depth = data_source == "ibkr" and leg.get("market_data_status") == "live"
    return bool(
        (leg.get("bid_size") or 0) > 0
        and (leg.get("ask_size") or 0) > 0
        and quote_age <= 5
        and (trade_age <= 5 or live_ibkr_depth)
    )


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_minutes(evaluated_at: datetime, observed_at: datetime | None) -> float:
    if observed_at is None:
        return float("inf")
    age = (evaluated_at - observed_at).total_seconds() / 60
    return max(0.0, age) if age >= -1 else float("inf")


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
