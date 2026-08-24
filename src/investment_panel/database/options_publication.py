"""Build the compact, versioned professional options-radar publication."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE
from investment_panel.core.option_trade_ticket import build_option_trade_ticket, calibrated_cohort_ready, ticket_recommendation_fields
from investment_panel.core.event_scout import build_options_decision_truth
from investment_panel.database.options_risk_context import option_risk_contexts
from investment_panel.database.source_health import source_health_blockers

__all__ = [
    "add_contract_fields",
    "as_datetime",
    "contract_readiness",
    "publication_models",
    "publish_degraded_if_needed",
    "shortlist",
    "summary_state",
]


def candidate_set_changes(
    current: list[dict[str, Any]],
    previous: list[dict[str, Any]],
) -> dict[str, list[str]]:
    current_symbols = {str(row.get("ticker") or row.get("symbol") or "") for row in current}
    previous_symbols = {str(row.get("ticker") or row.get("symbol") or "") for row in previous}
    current_symbols.discard("")
    previous_symbols.discard("")
    return {
        "new": sorted(current_symbols - previous_symbols),
        "retained": sorted(current_symbols & previous_symbols),
        "removed": sorted(previous_symbols - current_symbols),
    }


def research_priority(row: dict[str, Any]) -> dict[str, Any]:
    momentum_20d = _number(row.get("momentum_20d"))
    momentum_5d = _number(row.get("momentum_5d"))
    relative_20d = _number(row.get("relative_strength_20d"))
    relative_60d = _number(row.get("relative_strength_60d"))
    efficiency = _number(row.get("kaufman_er_20d"))
    if momentum_20d is None:
        return {"direction_pool": "unavailable", "research_priority_score": None, "why_ticker": "Daily momentum is unavailable"}
    if momentum_20d == 0:
        return {"direction_pool": "neutral", "research_priority_score": 0.0, "why_ticker": "20-day momentum is neutral"}
    direction = 1.0 if momentum_20d > 0 else -1.0
    acceleration = direction * ((momentum_5d or 0.0) - momentum_20d / 4.0)
    score = acceleration + direction * (relative_20d or 0.0) + 0.5 * direction * (relative_60d or 0.0) + 0.25 * (efficiency or 0.0)
    pool = "bullish" if direction > 0 else "bearish"
    return {
        "direction_pool": pool,
        "research_priority_score": round(score, 8),
        "why_ticker": f"{pool.title()} pool: 20-day momentum, 5-day acceleration, relative strength, and trend efficiency",
    }


def publish_degraded_if_needed(repository: Any, code_version: str, feature_version: str, _strategy_key: str) -> dict[str, Any]:
    """Replace an incompatible legacy fallback when no usable quoted publication exists."""
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
    options_risk_sleeve_capital: float | None = None,
    calibration: list[dict[str, Any]] | None = None,
    market_regime: dict[str, Any] | None = None,
    previous_opportunities: list[dict[str, Any]] | None = None,
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
                   option_decision.route_version, option_decision.strategy_route,
                   option_decision.market_regime_detail, option_decision.event_state,
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
                   symbol_feature.momentum_5d, symbol_feature.momentum_20d,
                   symbol_feature.relative_strength_20d,
                   symbol_feature.relative_strength_60d,
                   symbol_feature.kaufman_er_20d, symbol_feature.kaufman_er_60d,
                   symbol_feature.kama_fast, symbol_feature.kama_slow,
                   symbol_feature.kama_fast_slope, symbol_feature.kama_slow_slope,
                   symbol_feature.trend_state, symbol_feature.trend_confidence,
                   symbol_feature.volatility_state,
                   symbol_feature.data_quality_status AS trend_quality_status,
                   symbol_feature.reason_codes AS trend_reason_codes,
                   active_thesis.thesis AS thesis_payload,
                   active_thesis.revision_id::text AS thesis_revision_id,
                   active_thesis.revision AS thesis_revision,
                   active_thesis.author_kind AS thesis_author_kind,
                   expression.id::text AS thesis_expression_id,
                   expression.structure AS thesis_expression,
                   expression.entry_logic AS thesis_expression_entry_logic,
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
            LEFT JOIN analysis.symbol_feature symbol_feature
              ON symbol_feature.run_id = decision.run_id
             AND symbol_feature.instrument_id = decision.instrument_id
             AND symbol_feature.feature_set = 'daily_trend'
            LEFT JOIN LATERAL (
                SELECT thesis.id AS revision_id, thesis.revision, thesis.author_kind, thesis.thesis
                FROM app.thesis thesis
                WHERE thesis.instrument_id = instrument.id
                  AND thesis.status = 'current'
                ORDER BY thesis.updated_at DESC, thesis.id DESC
                LIMIT 1
            ) active_thesis ON true
            LEFT JOIN LATERAL (
                SELECT expression.id, expression.structure, expression.entry_logic
                FROM app.thesis_expression expression
                WHERE expression.thesis_revision_id = active_thesis.revision_id
                  AND expression.expression_kind = 'option'
                  AND expression.status = 'active'
                ORDER BY expression.updated_at DESC, expression.id DESC
                LIMIT 1
            ) expression ON true
            LEFT JOIN LATERAL (
                SELECT jsonb_agg(jsonb_build_object(
                    'contract_id', leg_quote.contract_id::text,
                    'observed_at', leg_quote.observed_at,
                    'captured_at', leg_quote.captured_at,
                    'last_trade_at', leg_quote.last_trade_at,
                    'market_data_status', leg_quote.market_data_status,
                    'option_type', leg_contract.option_type,
                    'strike', leg_contract.strike,
                    'bid', leg_quote.bid,
                    'ask', leg_quote.ask,
                    'bid_size', leg_quote.bid_size,
                    'ask_size', leg_quote.ask_size,
                    'open_interest', leg_quote.open_interest,
                    'volume', leg_quote.volume
                )) AS quotes
                FROM jsonb_array_elements(option_decision.synthetic_legs) leg
                JOIN raw.option_quote leg_quote
                  ON leg_quote.snapshot_id = option_decision.snapshot_id
                 AND leg_quote.contract_id = (leg->>'contract_id')::bigint
                 AND leg_quote.observed_at = option_decision.quote_observed_at
                JOIN catalog.option_contract leg_contract ON leg_contract.id = leg_quote.contract_id
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
    for row in all_rows:
        row["strategy_route"] = dict(row.get("strategy_route") or {})
        row["market_regime_detail"] = dict(row.get("market_regime_detail") or {})
        row.update(research_priority(row))
    discovery_by_ticker = {str(row["ticker"]): row for row in discovery_rows}
    readiness_evaluated_at = discovery_run["started_at"] if discovery_run else datetime.now(UTC)
    risk_contexts = option_risk_contexts(
        runtime,
        {str(row.get("ticker") or "") for row in all_rows},
        evaluated_at=readiness_evaluated_at,
    )
    source_ids = {
        str(row.get("data_source") or "").strip()
        for row in all_rows
        if str(row.get("data_source") or "").strip()
    }
    health_blockers = source_health_blockers(
        runtime,
        sorted(source_ids),
        evaluated_at=readiness_evaluated_at,
    )
    for row in all_rows:
        discovery = discovery_by_ticker.get(str(row.get("ticker"))) or {}
        for key in (
            "stage", "primary_edge", "source_root_count", "evidence_completeness",
            "data_readiness", "execution_ready", "catalyst_start", "catalyst_end",
            "timeliness", "next_evidence",
        ):
            row[key] = discovery.get(key)
        row["data_readiness"] = _contract_readiness(row, readiness_evaluated_at)
        data_source = str(row.get("data_source") or "").strip()
        row["source_health_blockers"] = list(health_blockers.get(data_source, []))
        if not data_source:
            row["source_health_blockers"] = ["source_identity_missing"]
        if row["source_health_blockers"]:
            row["data_readiness"] = "D"
            row["blockers"] = sorted({
                *list(row.get("blockers") or []),
                "active_source_health_blocked",
            })
        if row["data_readiness"] != "A":
            row["blockers"] = sorted({
                *list(row.get("blockers") or []),
                "execution_data_not_grade_a",
            })
    _add_contract_fields(
        all_rows,
        feature_version,
        strategy_revision,
        options_risk_sleeve_capital=options_risk_sleeve_capital,
        evaluated_at=readiness_evaluated_at,
        risk_contexts=risk_contexts,
        calibration=calibration,
    )
    for row in all_rows:
        row["execution_ready"] = bool(
            row.get("state") == "READY"
            and row.get("data_readiness") == "A"
            and not row.get("blockers")
            and row.get("portfolio_context_status") == "complete"
        )
        row.update(ticket_recommendation_fields(row))
        row["decision_truth"] = build_options_decision_truth(row)
    actionable = _shortlist(_prefer_current_data([
        row for row in all_rows if row.get("state") != "REJECTED"
    ]))
    candidate_changes = candidate_set_changes(actionable, previous_opportunities or [])
    primary_by_ticker: dict[str, dict[str, Any]] = {}
    for row in actionable:
        ticker = str(row.get("ticker") or "")
        current = primary_by_ticker.get(ticker)
        if current is None or _rank_key(row) > _rank_key(current):
            primary_by_ticker[ticker] = row
    for row in actionable:
        row["candidate_change"] = (
            "New" if str(row.get("ticker")) in candidate_changes["new"] else "Retained"
        )
        row["is_primary_structure"] = primary_by_ticker.get(str(row.get("ticker") or "")) is row
    primary_actionable = [row for row in actionable if row["is_primary_structure"]]
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
    latest_row = max(
        (row for row in all_rows if row.get("snapshot_time") is not None),
        key=lambda row: row["snapshot_time"],
        default=None,
    )
    latest = latest_row.get("snapshot_time") if latest_row else None
    global_summary = [{
        "stable_key": "global",
        "contract_version": 3,
        "feature_version": feature_version,
        "strategy_revision": strategy_revision,
        "publication_cutoff": (discovery_run["manifest"] or {}).get("cutoff") if discovery_run else latest,
        "latest_complete_quote_time": latest,
        "source": latest_row.get("data_source") if latest_row else (discovery_run["provider"] if discovery_run else None),
        "market_session": latest_row.get("market_session") if latest_row else (discovery_run["market_session"] if discovery_run else None),
        "scanned_contracts": scanned_contracts,
        "symbols_considered": int(discovery_run["symbols_considered"]) if discovery_run else 0,
        "symbols_with_chains": int(discovery_run["symbols_with_chains"]) if discovery_run else 0,
        "contracts_evaluated": int(discovery_run["contracts_evaluated"]) if discovery_run else scanned_contracts,
        "universe_hash": discovery_run["universe_hash"] if discovery_run else None,
        "eligible_contracts": sum(row.get("state") != "REJECTED" for row in all_rows),
        "shortlist_count": len(primary_actionable),
        "cash_secured_put_count": sum(row.get("structure") == "cash_secured_put" for row in actionable),
        "ready_count": sum(_summary_state(row) == "READY" for row in primary_actionable),
        "setup_count": sum(_summary_state(row) == "SETUP" for row in primary_actionable),
        "watch_count": sum(_summary_state(row) == "WATCH" for row in primary_actionable),
        "learning_coverage": 1.0 if all_rows else 0.0,
        "shadow_only": True,
        "market_state": (market_regime or {}).get("trend_state", "unavailable"),
        "market_state_as_of": (market_regime or {}).get("as_of"),
        "market_state_quality": (market_regime or {}).get("quality_status", "unavailable"),
        "market_trend_confidence": (market_regime or {}).get("trend_confidence", 0.0),
        "market_kaufman_er_20d": (market_regime or {}).get("kaufman_er_20d"),
        "breadth_state": (market_regime or {}).get("breadth_state", "unavailable"),
        "breadth_up_fraction": (market_regime or {}).get("breadth_up_fraction"),
        "breadth_down_fraction": (market_regime or {}).get("breadth_down_fraction"),
        "breadth_denominator": (market_regime or {}).get("breadth_denominator", 0),
        "volatility_state": (market_regime or {}).get("volatility_state", "unstable"),
        "candidate_changes": candidate_changes,
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
    *,
    options_risk_sleeve_capital: float | None = None,
    evaluated_at: datetime | None = None,
    risk_contexts: dict[str, dict[str, Any]] | None = None,
    calibration: list[dict[str, Any]] | None = None,
) -> None:
    calibration_by_structure = {
        str(profile.get("structure")): profile for profile in calibration or []
    }
    for row in rows:
        profile = calibration_by_structure.get(str(row.get("structure") or "")) or {}
        calibrated = calibrated_cohort_ready(profile)
        row.update({
            "decision_id": row["candidate_event_id"],
            "rank_score": row.get("score"),
            "calibrated_probability": row.get("probability_profit") if calibrated else None,
            "contract_version": 3,
            "feature_version": feature_version,
            "strategy_revision": strategy_revision,
            "analysis_cutoff": row.get("snapshot_time"),
            "quote_observed_at": row.get("snapshot_time"),
            "probability_semantics": (
                "calibrated_structure_cohort"
                if calibrated
                else (row.get("details") or {}).get(
                    "probability_semantics", "provisional_uncalibrated"
                )
            ),
            "probability_sample_size": (
                profile.get("sample_size")
                if calibrated
                else (row.get("details") or {}).get("scenario_count")
            ),
            "conservative_expected_value": (row.get("details") or {}).get("conservative_expected_value"),
            "optimistic_expected_value": (row.get("details") or {}).get("optimistic_expected_value"),
            "lower_95_expected_value": (row.get("details") or {}).get("lower_95_expected_value"),
        })
        if not row.get("structure"):
            row["structure"] = "long_call" if row.get("option_type") == "call" else "long_put"
        legs = _complete_ticket_legs(row)
        details = dict(row.get("details") or {})
        thesis = dict(row.get("thesis_payload") or {})
        expression = dict(row.get("thesis_expression") or {})
        preferred_structures = {
            str(item) for item in expression.get("preferred_structures") or [] if str(item)
        }
        expression_blockers: list[str] = []
        if not row.get("thesis_expression_id"):
            expression_blockers.append("thesis_expression_required")
        elif not preferred_structures:
            expression_blockers.append("thesis_expression_structures_required")
        elif str(row["structure"]) not in preferred_structures:
            expression_blockers.append("thesis_expression_structure_mismatch")
        lower_expected_value = details.get("lower_95_expected_value")
        if lower_expected_value is None and calibrated:
            unit_risk = _number(row.get("secured_cash") or row.get("max_loss"))
            cohort_lower = _number(profile.get("lower_95_expectancy"))
            if unit_risk is not None and cohort_lower is not None:
                lower_expected_value = unit_risk * cohort_lower
        ticket = build_option_trade_ticket(
            decision_id=str(row["decision_id"]),
            symbol=str(row.get("ticker") or row.get("symbol") or ""),
            structure=str(row["structure"]),
            expiration=row.get("expiration"),
            legs=legs,
            entry_price=_number(row.get("entry_price") or row.get("premium_mid")),
            one_unit_max_loss=_number(row.get("max_loss")),
            secured_cash=_number(row.get("secured_cash")),
            state=str(row.get("state") or "WATCH"),
            blockers=[*list(row.get("blockers") or []), *expression_blockers],
            evaluated_at=evaluated_at,
            market_session=str(row.get("market_session") or ""),
            sleeve_capital=options_risk_sleeve_capital,
            **(risk_contexts or {}).get(str(row.get("ticker") or ""), {}),
            assignment_policy=(row.get("strategy_route") or {}).get("assignment_policy")
            or details.get("assignment_policy"),
            thesis={
                "summary": thesis.get("core_thesis") or thesis.get("thesis") or details.get("thesis"),
                "catalyst": details.get("catalyst"),
                "direction": expression.get("direction") or thesis.get("direction"),
                "invalidation": thesis.get("invalidation") or details.get("invalidation"),
                "invalidation_rules": thesis.get("invalidation_rules"),
            },
            forecast={
                "expected_value": row.get("expected_value"),
                "lower_95_expected_value": lower_expected_value,
                "probability_profit": row.get("probability_profit"),
                "probability_semantics": row.get("probability_semantics"),
                "effective_sample_size": row.get("probability_sample_size"),
                "tail_loss": row.get("tail_cvar"),
            },
            provenance={
                "publication_cutoff": row.get("analysis_cutoff"),
                "quote_source": row.get("data_source"),
                "thesis": {
                    "revision_id": row.get("thesis_revision_id"),
                    "revision": row.get("thesis_revision"),
                    "author_kind": row.get("thesis_author_kind"),
                    "expression_id": row.get("thesis_expression_id"),
                    "option_agent_task_id": (thesis.get("provenance") or {}).get("option_agent_task_id"),
                    "option_agent_run_id": (thesis.get("provenance") or {}).get("option_agent_run_id"),
                },
                "revisions": {
                    "contract": row.get("contract_version"),
                    "feature": feature_version,
                    "strategy": strategy_revision,
                    "thesis": row.get("thesis_revision"),
                    "expression": row.get("thesis_expression_id"),
                },
            },
        )
        row["ticket"] = ticket
        row["policy_version"] = ticket["policy_version"]
        row["decision_revision"] = ticket["decision_revision"]
        row["risk_budget"] = ticket["risk"]["available_risk_budget"]
        row["advisory_max_contracts"] = ticket["risk"]["recommended_quantity"]
        row["portfolio_context_status"] = (
            "complete"
            if options_risk_sleeve_capital is not None
            and ticket["risk"].get("broker_available_capital") is not None
            else "missing_or_stale_options_risk_context"
        )
        row["lower_confidence_expectancy_per_max_risk"] = ticket[
            "lower_confidence_expectancy_per_max_risk"
        ]
        row["blockers"] = ticket["blockers"]


def _shortlist(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row["ticker"]), str(row["structure"]))
        current = best.get(key)
        if current is None or _rank_key(row) > _rank_key(current):
            best[key] = row
    primary_by_ticker: dict[str, dict[str, Any]] = {}
    for row in best.values():
        ticker = str(row["ticker"])
        current = primary_by_ticker.get(ticker)
        if current is None or _rank_key(row) > _rank_key(current):
            primary_by_ticker[ticker] = row
    ranked = sorted(
        primary_by_ticker.values(),
        key=lambda row: (
            -_rank_key(row)[0],
            -_rank_key(row)[1],
            str(row.get("ticker") or ""),
        ),
    )
    selected: list[dict[str, Any]] = []
    family_counts: dict[str, int] = {}
    for row in ranked:
        family = _structure_family(str(row.get("structure") or "unknown"))
        if family_counts.get(family, 0) >= 4:
            continue
        selected.append(row)
        family_counts[family] = family_counts.get(family, 0) + 1
        if len(selected) >= 10:
            break
    selected_tickers = {str(row["ticker"]) for row in selected}
    secondary_counts: dict[str, int] = {}
    secondary = []
    for row in sorted(best.values(), key=_rank_key, reverse=True):
        ticker = str(row["ticker"])
        if ticker not in selected_tickers or primary_by_ticker[ticker] is row:
            continue
        if secondary_counts.get(ticker, 0) >= 3:
            continue
        secondary.append(row)
        secondary_counts[ticker] = secondary_counts.get(ticker, 0) + 1
    return selected + secondary


def _structure_family(structure: str) -> str:
    if structure in {"long_call", "long_put"}:
        return "long_option"
    if structure in {"call_debit_spread", "put_debit_spread"}:
        return "debit_spread"
    if structure == "cash_secured_put":
        return "cash_secured_put"
    return structure


def _prefer_current_data(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Do not mix long-stale fallback rows into a current radar shortlist.

    The last-good fallback still protects the page when no current option chain
    is available. Once the provider has supplied any A/B/C-quality quote,
    D-quality rows add no current decision value and make a fresh panel look
    stale.
    """

    current = [
        row
        for row in rows
        if str(row.get("data_readiness") or "D") in {"A", "B", "C"}
    ]
    return current or rows


def _summary_state(row: dict[str, Any]) -> str:
    if (row.get("ticket") or {}).get("state") == "READY":
        return "READY"
    state = str(row.get("state") or "WATCH")
    return state if state in {"SETUP", "WATCH"} else "WATCH"


def _rank_key(row: dict[str, Any]) -> tuple[int, float]:
    value = row.get("lower_confidence_expectancy_per_max_risk")
    if value is not None:
        return (1, float(value))
    fallback = row.get("risk_adjusted_expectancy")
    if fallback is None:
        fallback = row.get("score")
    return (0, float(fallback) if fallback is not None else -1_000_000.0)


def _complete_ticket_legs(row: dict[str, Any]) -> list[dict[str, Any]]:
    legs = [dict(leg) for leg in row.get("synthetic_legs") or []]
    if not legs:
        legs = [{
            "contract_id": row.get("contract_id"),
            "option_type": row.get("option_type"),
            "side": "short" if row.get("structure") == "cash_secured_put" else "long",
            "strike": row.get("strike"),
            "bid": row.get("bid"),
            "ask": row.get("ask"),
            "bid_size": row.get("bid_size"),
            "ask_size": row.get("ask_size"),
            "observed_at": row.get("captured_at") or row.get("snapshot_time"),
            "open_interest": row.get("open_interest"),
            "volume": row.get("volume"),
        }]
    depth = {str(item.get("contract_id")): dict(item) for item in row.get("leg_quotes") or []}
    return [{**leg, **depth.get(str(leg.get("contract_id")), {})} for leg in legs]


def _contract_readiness(row: dict[str, Any], evaluated_at: datetime) -> str:
    quote_at = _as_datetime(row.get("captured_at") or row.get("quote_observed_at") or row.get("snapshot_time"))
    last_trade_at = _as_datetime(row.get("last_trade_at"))
    quote_age = _age_minutes(evaluated_at, quote_at)
    trade_age = _age_minutes(evaluated_at, last_trade_at)
    live_ibkr_depth = row.get("data_source") == "ibkr" and row.get("market_data_status") == "live"
    if row.get("structure") in {"call_debit_spread", "put_debit_spread"}:
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


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


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


contract_readiness = _contract_readiness
as_datetime = _as_datetime
add_contract_fields = _add_contract_fields
shortlist = _shortlist
summary_state = _summary_state
