"""Persistence and deterministic replay for history-v3 price-shape evidence."""

from __future__ import annotations
from datetime import UTC, datetime
from math import sqrt
from statistics import mean, pstdev
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from investment_panel.analysis.history_v3 import MODEL_REVISION, analyze_group
from investment_panel.core.option_underwriting import (
    conservative_entry, conservative_mark, historical_payoff_statistics, paper_state,
    permitted_structures, thesis_blocker, underwriting_direction,
)
from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.opportunity_episodes import canonical_option_lane, option_episode_key, option_sample_eligibility, scorecard_truth_cohort
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE
from investment_panel.database.options_history_v3_candidates import (
    candidate_leg,
    candidate_seed,
    candidate_thesis_payload,
    decision_state,
    execution_confidence as calculate_execution_confidence,
    history_truth_blockers,
    market_regime,
    non_overlapping_returns,
    spread_short_leg,
)
from investment_panel.database.confirmed_daily_prices import confirmed_daily_bars
from investment_panel.database.options_history_policy import apply_publication_cap
from investment_panel.database.options_history_ticket import published_candidates
from investment_panel.database.options_history_v3_surface import surface_shape_metrics
from investment_panel.database.options_history_v3_materialization import (
    capture_group_quality as _capture_group_quality,
    deterministic_hash as _hash,
    group_verified_contract_rows,
    is_later_capture_cohort,
    long_delta_eligible as _long_delta_eligible,
    policy_for_instrument as _policy_for_instrument,
    surface_summary as _summary,
)


def cohort_legs(connection: Any, generation_id: int, shadows: list[dict[str, Any]]) -> dict[Any, list[dict[str, Any]]]:
    contract_ids = sorted({int(leg["contract_id"]) for shadow in shadows for leg in shadow.get("synthetic_legs", [])})
    if not contract_ids:
        return {}
    quotes = {int(row["contract_id"]): dict(row) for row in connection.execute(
        """SELECT contract_id, bid, ask, bid_size, ask_size, provider_observed_at, available_at
           FROM raw.option_quote WHERE capture_generation_id = %s AND contract_id = ANY(%s)""",
        [generation_id, contract_ids],
    ).fetchall()}
    result: dict[Any, list[dict[str, Any]]] = {}
    for shadow in shadows:
        legs = []
        for stored in shadow.get("synthetic_legs", []):
            if (quote := quotes.get(int(stored["contract_id"]))) is not None:
                legs.append({
                    **dict(stored),
                    "bid": quote["bid"],
                    "ask": quote["ask"],
                    "observed_at": quote["provider_observed_at"],
                    "size_available": quote["bid_size"] is not None and quote["bid_size"] >= 1
                    and quote["ask_size"] is not None and quote["ask_size"] >= 1,
                    "available_at": quote["available_at"],
                })
        result[shadow["id"]] = legs
    return result


def latest_available_at(legs: list[dict[str, Any]]) -> datetime | None:
    values = [leg["available_at"] for leg in legs if leg.get("available_at") is not None]
    return max(values) if values else None


def quote_package(legs: list[dict[str, Any]], as_of: datetime) -> dict[str, Any]:
    observed = [_quote_datetime(leg.get("observed_at")) for leg in legs]
    timestamps = [value for value in observed if value is not None]
    ages = [(as_of - value).total_seconds() for value in timestamps]
    skew = (max(timestamps) - min(timestamps)).total_seconds() if len(timestamps) > 1 else 0.0
    return {
        "max_quote_age_seconds": max(ages) if ages else None,
        "interleg_skew_seconds": skew,
        "liquidity": {
            "minimum_open_interest": min((int(leg["open_interest"]) for leg in legs if leg.get("open_interest") is not None), default=None),
            "minimum_volume": min((int(leg["volume"]) for leg in legs if leg.get("volume") is not None), default=None),
            "displayed_sizes": [
                {"contract_id": leg["contract_id"], "bid_size": leg.get("bid_size"), "ask_size": leg.get("ask_size")}
                for leg in legs
            ],
        },
    }


def _quote_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def upsert_observing_shadow_outcome(
    connection: Any,
    *,
    decision_id: str,
    shadow_trade_id: str,
    observed_through: datetime,
    current_return: float | None,
) -> None:
    """Keep an incomplete shadow mark visible but out of scorecard cohorts."""
    connection.execute(
        """
        INSERT INTO analysis.option_outcome
            (decision_id, maturity_state, observed_through, current_return, outcome_source, shadow_trade_id,
             lane, episode_key, sample_eligible, quarantine_reason, calibration_cohort)
        SELECT decision.id, 'observing', %s, %s, 'options_history_v3', %s,
               decision.lane, decision.episode_key, false,
               coalesce(decision.quarantine_reason, 'outcome_not_resolved_execution_grade'),
               decision.calibration_cohort
        FROM analysis.decision decision
        WHERE decision.id = %s
        ON CONFLICT (decision_id) DO UPDATE
        SET maturity_state = EXCLUDED.maturity_state, observed_through = EXCLUDED.observed_through,
            current_return = EXCLUDED.current_return, outcome_source = EXCLUDED.outcome_source,
            shadow_trade_id = EXCLUDED.shadow_trade_id, lane = EXCLUDED.lane,
            episode_key = EXCLUDED.episode_key, sample_eligible = false,
            quarantine_reason = EXCLUDED.quarantine_reason,
            calibration_cohort = EXCLUDED.calibration_cohort, updated_at = now()
        """,
        [observed_through, current_return, shadow_trade_id, decision_id],
    )


def surface_shift_run_summary(
    runtime: DatabaseRuntime, symbol: str, *, snapshot_id: int,
    capture_generation_id: int, analysis_run_id: Any, model_revision: str,
    mode: str, as_of: Any,
) -> dict[str, Any]:
    """Keep a research failure from invalidating the authoritative publication."""
    from investment_panel.database.options_distribution_shift import materialize_surface_shift

    try:
        result = materialize_surface_shift(
            runtime, symbol=symbol, as_of=as_of, snapshot_id=snapshot_id,
            capture_generation_id=capture_generation_id,
            current_analysis_run_id=analysis_run_id, model_revision=model_revision, mode=mode,
        )
        return {"surface_shift_state": result["evidence_state"]}
    except Exception as error:
        return {"surface_shift_state": "unavailable", "surface_shift_error": type(error).__name__}
class OptionHistoryV3Materializer:
    """Bulk materialize an immutable capture generation into a separate analysis run."""

    def __init__(self, runtime: DatabaseRuntime, *, options_risk_sleeve_capital: float | None = None) -> None:
        self.runtime = runtime
        self.analysis = AnalysisRepository(runtime)
        self.options_risk_sleeve_capital = options_risk_sleeve_capital
    def materialize(
        self,
        *,
        snapshot_id: int,
        capture_generation_id: int,
        model_revision: str = MODEL_REVISION,
        code_version: str = "history-v3",
        mode: str = "historical_evidence",
    ) -> dict[str, Any]:
        if mode not in {"historical_evidence", "live_lifecycle"}:
            raise ValueError("option history materialization mode is invalid")
        metadata, rows = self._generation_rows(snapshot_id, capture_generation_id)
        if metadata is None:
            raise ValueError("capture generation does not belong to snapshot")
        if metadata["capture_state"] != "complete":
            raise ValueError("only complete capture generations can be materialized")
        existing = self._canonical_succeeded_run(capture_generation_id, model_revision, mode)
        if existing is not None:
            existing.update(surface_shift_run_summary(self.runtime, str(metadata["symbol"]), snapshot_id=snapshot_id, capture_generation_id=capture_generation_id, analysis_run_id=existing["analysis_run_id"], model_revision=model_revision, mode=mode, as_of=metadata["available_at"]))
            return existing
        run_id = self.analysis.start_run(
            "option_history_v3",
            input_cutoff=metadata["available_at"],
            code_version=code_version,
            inputs={
                "snapshot_id": snapshot_id,
                "capture_generation_id": capture_generation_id,
                "model_revision": model_revision,
                "mode": mode,
                "available_at": metadata["available_at"].isoformat(),
                "row_count": len(rows),
            },
            feature_versions={"option_history": model_revision},
        )
        try:
            result = self._persist_run(
                run_id=run_id,
                snapshot_id=snapshot_id,
                capture_generation_id=capture_generation_id,
                metadata=metadata,
                rows=rows,
                model_revision=model_revision,
                mode=mode,
            )
            publication_id = self._publish_decision_system(run_id, result)
            result["publication_id"] = str(publication_id)
            result.update(surface_shift_run_summary(
                self.runtime, str(metadata["symbol"]),
                snapshot_id=snapshot_id, capture_generation_id=capture_generation_id, analysis_run_id=run_id, model_revision=model_revision, mode=mode, as_of=metadata["available_at"],
            ))
            return {"analysis_run_id": str(run_id), **result}
        except Exception as exc:
            self.analysis.finish_run(run_id, "failed", {"error": f"{type(exc).__name__}: {exc}"})
            raise
    def _canonical_succeeded_run(self, capture_generation_id: int, model_revision: str, mode: str) -> dict[str, Any] | None:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                [f"option-history-v3:{capture_generation_id}:{model_revision}:{mode}"],
            )
            row = connection.execute(
                """
                SELECT run.id::text AS analysis_run_id, run.summary
                FROM analysis.run run
                WHERE run.run_type = 'option_history_v3'
                  AND run.status = 'succeeded'
                  AND run.summary->>'capture_generation_id' = %s
                  AND run.summary->>'model_revision' = %s
                  AND coalesce(run.summary->>'mode', 'historical_evidence') = %s
                ORDER BY run.finished_at ASC, run.id ASC LIMIT 1
                """,
                [str(capture_generation_id), model_revision, mode],
            ).fetchone()
        if row is None:
            return None
        return {"analysis_run_id": row["analysis_run_id"], **dict(row["summary"] or {}), "idempotent_replay": True}

    def _generation_rows(self, snapshot_id: int, generation_id: int) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        with self.runtime.read(JOB_PROFILE) as connection:
            metadata = connection.execute(
                """
                SELECT generation.id, generation.capture_state, generation.capture_finished_at,
                       snapshot.history_symbol AS symbol, snapshot.slot_at,
                       snapshot.latest_complete_generation_id,
                       coalesce(generation.capture_finished_at, snapshot.observed_at) AS available_at
                FROM raw.option_capture_generation generation
                JOIN raw.option_snapshot snapshot ON snapshot.id = generation.snapshot_id
                WHERE generation.id = %s AND snapshot.id = %s
                """,
                [generation_id, snapshot_id],
            ).fetchone()
            if metadata is None:
                return None, []
            rows = connection.execute(
                """
                SELECT quote.contract_id, contract.expiration, contract.option_type,
                       snapshot.history_symbol AS symbol,
                       contract.strike::double precision AS strike,
                       contract.multiplier, quote.contract_style AS style,
                       quote.contract_settlement AS settlement,
                       quote.contract_deliverable_key AS deliverable_key,
                       quote.standard_contract_verified,
                       greatest(contract.expiration - snapshot.trading_date, 0) AS dte,
                       quote.observed_at AS quote_observed_at, contract.underlying_instrument_id AS instrument_id,
                       quote.underlying_price, quote.bid, quote.ask, quote.mid,
                       quote.bid_size, quote.ask_size, quote.open_interest, quote.provider_delta, quote.market_data_status,
                       quote.capture_group_key, quote.group_started_at, quote.group_finished_at,
                       quote.provider_observed_at, quote.available_at,
                       quote.underlying_observed_at, quote.underlying_available_at,
                       quote.provider_iv
                FROM raw.option_quote quote
                JOIN catalog.option_contract contract ON contract.id = quote.contract_id
                JOIN raw.option_snapshot snapshot ON snapshot.id = quote.snapshot_id
                WHERE quote.snapshot_id = %s AND quote.capture_generation_id = %s
                ORDER BY contract.expiration, contract.option_type, contract.strike
                """,
                [snapshot_id, generation_id],
            ).fetchall()
        return dict(metadata), [dict(row) for row in rows]

    def _persist_run(
        self,
        *,
        run_id: UUID,
        snapshot_id: int,
        capture_generation_id: int,
        metadata: dict[str, Any],
        rows: list[dict[str, Any]],
        model_revision: str,
        mode: str,
    ) -> dict[str, Any]:
        grouped, contract_term_rejections, excluded_contract_rows = (
            group_verified_contract_rows(rows)
        )
        surface_metrics = surface_shape_metrics(
            grouped, {key: _capture_group_quality(group)[0] for key, group in grouped.items()}
        )
        summary_count, relative_count, failures = 0, 0, contract_term_rejections
        eligible_groups, fit_attempts, succeeded_groups, decision_count = 0, 0, 0, 0
        persisted: list[dict[str, Any]] = []
        with self.runtime.transaction(JOB_PROFILE) as connection:
            theses = {
                int(row["instrument_id"]): {"id": int(row["id"]), **dict(row["thesis"])}
                for row in connection.execute(
                    """
                    SELECT DISTINCT ON (instrument_id) id, instrument_id, thesis
                    FROM app.thesis
                    WHERE created_at <= %s AND updated_at <= %s
                    ORDER BY instrument_id, revision DESC, updated_at DESC, id DESC
                    """,
                    [metadata["available_at"], metadata["available_at"]],
                ).fetchall()
            }
            bars_by_instrument = self._confirmed_bars(
                connection,
                {int(row["instrument_id"]) for row in rows},
                metadata["available_at"],
            )
            for (expiration, option_type), group in grouped.items():
                policy = _policy_for_instrument(connection, int(group[0]["instrument_id"]))
                spot, group_blockers = _capture_group_quality(group)
                result = analyze_group(
                    group,
                    spot=spot,
                    option_type=option_type,
                    model_revision=model_revision,
                    group_blockers=group_blockers,
                )
                fit = result["fit"]
                group_is_eligible = result["eligible_count"] >= 12 and not group_blockers
                if group_is_eligible:
                    eligible_groups += 1
                    fit_attempts += 1
                if fit.status == "succeeded":
                    succeeded_groups += 1
                elif "fit_failed" in result["blockers"]:
                    failures += 1
                summary = _summary(group, result, spot)
                summary.update(surface_metrics.get((expiration, option_type), {}))
                connection.execute(
                    """
                    INSERT INTO analysis.option_surface_summary
                        (snapshot_id, expiration, option_type, feature_version, dte, atm_iv, delta_25_iv,
                         skew_25, smile_slope, smile_curvature, term_slope, average_spread_pct,
                         liquidity_score, metrics, analysis_run_id, capture_generation_id, fit_method,
                         fit_status, eligible_point_count, group_duration_seconds,
                         max_quote_age_seconds, fit_rmse, candidate_count)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL, NULL, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        snapshot_id, expiration, option_type, model_revision, summary["dte"], summary["atm_iv"],
                        summary["delta_25_iv"], summary.get("skew_25"), summary.get("term_slope"),
                        summary["average_spread_pct"], summary["liquidity_score"],
                        Jsonb(summary["metrics"]), run_id, capture_generation_id, model_revision, fit.status,
                        result["eligible_count"], summary["group_duration_seconds"], summary["max_quote_age_seconds"],
                        fit.rmse, summary["candidate_count"],
                    ],
                )
                summary_count += 1
                for value in result["relative_values"]:
                    if value.get("contract_id") is None:
                        continue
                    stored_value = connection.execute(
                        """
                        INSERT INTO analysis.option_relative_value
                            (analysis_run_id, capture_generation_id, contract_id, model_revision,
                             classification, fair_low, fair_high, modeled_net_edge, edge_side,
                             confidence, quality_status, blockers, evidence)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        [
                            run_id, capture_generation_id, value["contract_id"], model_revision,
                            value["classification"], value["fair_low"], value["fair_high"],
                            value["modeled_net_edge"], value["edge_side"], value["confidence"],
                            value["quality_status"], value["blockers"], Jsonb(value["evidence"]),
                        ],
                    ).fetchone()
                    relative_count += 1
                    persisted.append(value)
                    quote = next(row for row in group if row["contract_id"] == value["contract_id"])
                    if value["classification"] in {"relative_cheap", "historical_static_arbitrage_candidate"} and _long_delta_eligible(quote):
                        decision_count += self._store_candidates(
                            connection, run_id=run_id, snapshot_id=snapshot_id, generation_id=capture_generation_id,
                            relative_value_id=int(stored_value["id"]), value=value, quote=quote, group=group,
                            thesis=theses.get(int(quote["instrument_id"])),
                            bars=bars_by_instrument.get(int(quote["instrument_id"]), []),
                            as_of=metadata["available_at"],
                            current_complete_generation=metadata["latest_complete_generation_id"] == capture_generation_id,
                            model_revision=model_revision,
                            lifecycle_enabled=mode == "live_lifecycle",
                            policy=policy,
                        )
            if mode == "live_lifecycle":
                self._advance_pending_shadows(
                    connection, snapshot_id=snapshot_id, generation_id=capture_generation_id,
                    current_slot=metadata.get("slot_at"), symbol=str(metadata.get("symbol") or "QQQ"),
                )
                self._mark_entered_shadows(
                    connection, generation_id=capture_generation_id, current_slot=metadata.get("slot_at"),
                    symbol=str(metadata.get("symbol") or "QQQ"),
                )
        digest = _hash(persisted)
        return {
            "snapshot_id": snapshot_id,
            "capture_generation_id": capture_generation_id,
            "model_revision": model_revision,
            "mode": mode,
            "surface_summaries": summary_count,
            "relative_values": relative_count,
            "solver_failures": failures,
            "contract_term_rejections": contract_term_rejections,
            "excluded_contract_rows": excluded_contract_rows,
            "eligible_groups": eligible_groups,
            "fit_attempts": fit_attempts,
            "succeeded_groups": succeeded_groups,
            "decision_candidates": decision_count,
            "deterministic_hash": digest,
            "diff_count": relative_count,
        }

    def _store_candidates(
        self, connection: Any, *, run_id: UUID, snapshot_id: int, generation_id: int,
        relative_value_id: int, value: dict[str, Any], quote: dict[str, Any], group: list[dict[str, Any]],
        thesis: dict[str, Any] | None, bars: list[dict[str, Any]], as_of: datetime,
        current_complete_generation: bool, model_revision: str, lifecycle_enabled: bool,
        policy: dict[str, Any] | None,
    ) -> int:
        """Persist thesis-led or research-only candidates from immutable v3 evidence."""

        long_structure = "long_call" if quote["option_type"] == "call" else "long_put"
        lane = "anomaly"
        if thesis_blocker(thesis) is None and long_structure in permitted_structures(underwriting_direction(thesis)):
            lane = "thesis"
        specs: list[tuple[str, list[dict[str, Any]]]] = [(long_structure, [candidate_leg(quote, "long")])]
        if lane == "thesis":
            spread_structure = "call_debit_spread" if quote["option_type"] == "call" else "put_debit_spread"
            short_leg = spread_short_leg(group, quote)
            if short_leg is not None:
                specs.append((spread_structure, [candidate_leg(quote, "long"), candidate_leg(short_leg, "short")]))
        created = 0
        for structure, legs in specs:
            scenario = historical_payoff_statistics(
                spot=float(quote["underlying_price"]), legs=legs,
                terminal_returns=non_overlapping_returns(bars, int(quote.get("dte") or 0)),
                seed=candidate_seed(generation_id, relative_value_id, structure),
            )
            calibration = self._calibration(
                connection, int(quote["instrument_id"]), structure, market_regime(bars), model_revision, as_of
            )
            blockers = [
                *value["blockers"],
                *scenario["blockers"],
                *history_truth_blockers(bars, as_of),
            ]
            computed_state = paper_state(
                structure=structure, lane=lane, thesis=thesis, fit_status="succeeded", blockers=blockers,
                scenario_count=int(scenario["scenario_count"]), expected_value=scenario["expected_value"],
                lower_95_expected_value=scenario["lower_95_expected_value"], max_loss=scenario["max_loss"],
                data_confidence=value["confidence"], execution_confidence=calculate_execution_confidence(legs),
                calibration=calibration, current_complete_generation=current_complete_generation,
            )
            state = apply_publication_cap(computed_state, policy)
            self._store_candidate(
                connection, run_id=run_id, snapshot_id=snapshot_id, generation_id=generation_id,
                relative_value_id=relative_value_id, value=value, quote=quote, thesis=thesis,
                structure=structure, legs=legs, lane=lane, state=state, scenario=scenario,
                market_regime=market_regime(bars), calibration=calibration, as_of=as_of,
                model_revision=model_revision, lifecycle_enabled=lifecycle_enabled,
            )
            created += 1
        return created

    def _store_candidate(
        self, connection: Any, *, run_id: UUID, snapshot_id: int, generation_id: int,
        relative_value_id: int, value: dict[str, Any], quote: dict[str, Any], thesis: dict[str, Any] | None,
        structure: str, legs: list[dict[str, Any]], lane: str, state: dict[str, Any],
        scenario: dict[str, Any], market_regime: str, calibration: dict[str, Any], as_of: datetime,
        model_revision: str, lifecycle_enabled: bool,
    ) -> None:
        decision_key = f"history-v3:{relative_value_id}:{structure}"
        scorecard_lane = canonical_option_lane("qqq", symbol=str(quote.get("symbol") or "QQQ"))
        quality_status, sample_eligible, quarantine_reason = option_sample_eligibility(
            value.get("quality_status")
        )
        if state["blockers"]:
            sample_eligible = False
            quarantine_reason = "quality_gated"
        episode_key = option_episode_key(
            lane=scorecard_lane,
            symbol=str(quote.get("symbol") or "QQQ"),
            strategy=structure,
            contract_ladder_slot=str(quote["contract_id"]),
            entry_at=quote["quote_observed_at"],
        )
        decision = connection.execute(
            """
            INSERT INTO analysis.decision
                (run_id, decision_key, kind, instrument_id, as_of, state, score, quality_status,
                 reasons, blockers, input_hash, lane, episode_key, sample_eligible,
                 quarantine_reason, calibration_cohort)
            VALUES (%s, %s, 'option', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id, decision_key) DO UPDATE
            SET state = EXCLUDED.state, score = EXCLUDED.score, reasons = EXCLUDED.reasons,
                blockers = EXCLUDED.blockers, input_hash = EXCLUDED.input_hash,
                quality_status = EXCLUDED.quality_status, lane = EXCLUDED.lane,
                episode_key = EXCLUDED.episode_key, sample_eligible = EXCLUDED.sample_eligible,
                quarantine_reason = EXCLUDED.quarantine_reason,
                calibration_cohort = EXCLUDED.calibration_cohort
            RETURNING id
            """,
            [run_id, decision_key, quote["instrument_id"], quote["quote_observed_at"],
             decision_state(state["paper_state"]), value.get("modeled_net_edge"), quality_status,
             state["reasons"], state["blockers"], _hash({"value": value, "structure": structure, "scenario": scenario}),
             scorecard_lane, episode_key, sample_eligible, quarantine_reason,
             scorecard_truth_cohort(model_revision)],
        ).fetchone()
        decision_id = decision["id"]
        execution_confidence = calculate_execution_confidence(legs)
        details = {
            "paper_only": True, "relative_value_evidence": value.get("evidence"),
            "historical_paths": scenario, "calibration": calibration, "as_of": as_of.isoformat(),
            "computed_paper_state": state.get("computed_paper_state", state["paper_state"]),
            "effective_paper_state": state["paper_state"],
            "thesis": candidate_thesis_payload(thesis),
            "quote_package": quote_package(legs, as_of),
            "reassessment_date": str(quote.get("expiration") or "") or None,
        }
        connection.execute(
            """
            INSERT INTO analysis.option_decision
                (decision_id, contract_id, snapshot_id, quote_observed_at, premium_mid, fill_assumption,
                 synthetic_legs, structure, entry_price, max_loss, probability_profit, expected_value,
                 data_confidence, execution_confidence, details, paper_state, discovery_lane, thesis_id,
                 relative_value_id, model_version, market_regime, fair_low, fair_high, modeled_net_edge)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (decision_id) DO UPDATE
            SET fill_assumption = EXCLUDED.fill_assumption, synthetic_legs = EXCLUDED.synthetic_legs,
                structure = EXCLUDED.structure, entry_price = EXCLUDED.entry_price, max_loss = EXCLUDED.max_loss,
                probability_profit = EXCLUDED.probability_profit, expected_value = EXCLUDED.expected_value,
                data_confidence = EXCLUDED.data_confidence, execution_confidence = EXCLUDED.execution_confidence,
                details = EXCLUDED.details, paper_state = EXCLUDED.paper_state, discovery_lane = EXCLUDED.discovery_lane,
                thesis_id = EXCLUDED.thesis_id, relative_value_id = EXCLUDED.relative_value_id,
                model_version = EXCLUDED.model_version, market_regime = EXCLUDED.market_regime,
                fair_low = EXCLUDED.fair_low, fair_high = EXCLUDED.fair_high,
                modeled_net_edge = EXCLUDED.modeled_net_edge
            """,
            [
                decision_id, quote["contract_id"], snapshot_id, quote["quote_observed_at"], quote.get("mid"),
                scenario["entry_price"], Jsonb(legs), structure, scenario["entry_price"], scenario["max_loss"],
                scenario["probability_profit"], scenario["expected_value"], value.get("confidence"), execution_confidence,
                Jsonb(details), state["paper_state"], lane, thesis.get("id") if thesis else None, relative_value_id,
                model_revision, market_regime, value.get("fair_low"), value.get("fair_high"), value.get("modeled_net_edge"),
            ],
        )
        connection.execute(
            """
            INSERT INTO analysis.decision_evidence (decision_id, evidence_kind, reference_key, detail)
            VALUES (%s, 'option_relative_value', %s, %s)
            ON CONFLICT (decision_id, evidence_kind, reference_key) DO UPDATE SET detail = EXCLUDED.detail
            """,
            [decision_id, str(relative_value_id), Jsonb({"capture_generation_id": generation_id, "structure": structure})],
        )
        if lifecycle_enabled and state["paper_state"] in {"WATCH", "PAPER_READY"}:
            connection.execute(
                """
                INSERT INTO analysis.shadow_trade
                    (decision_id, status, pending_entry_reason, entry_cohort_id, structure, market_regime, source_kind, metrics)
                VALUES (%s, 'pending', 'next_valid_cohort_required', %s, %s, %s, 'options_history_v3', %s)
                ON CONFLICT (decision_id) DO NOTHING
                """,
                [decision_id, generation_id, structure, market_regime,
                 Jsonb({"no_same_capture_entry": True, "fill_basis": "pending_next_cohort", "paper_only": True})],
            )

    def _confirmed_bars(self, connection: Any, instrument_ids: set[int], as_of: datetime) -> dict[int, list[dict[str, Any]]]:
        return confirmed_daily_bars(connection, instrument_ids, as_of=as_of)

    def _calibration(
        self, connection: Any, instrument_id: int, structure: str, market_regime: str,
        model_version: str, as_of: datetime,
    ) -> dict[str, Any]:
        rows = connection.execute(
            """
            SELECT option_decision.market_regime, option_decision.probability_profit, outcome.current_return
            FROM analysis.option_outcome outcome
            JOIN analysis.decision decision ON decision.id = outcome.decision_id
            JOIN analysis.option_decision option_decision ON option_decision.decision_id = decision.id
            JOIN analysis.shadow_trade shadow ON shadow.decision_id = decision.id
            WHERE shadow.source_kind = 'options_history_v3' AND option_decision.structure = %s
              AND option_decision.model_version = %s AND outcome.maturity_state IN ('mature', 'expired')
              AND outcome.current_return IS NOT NULL AND outcome.observed_through <= %s
              AND decision.instrument_id = %s
              AND decision.sample_eligible IS TRUE
              AND outcome.sample_eligible IS TRUE
              AND decision.calibration_cohort LIKE 'option-scorecard-truth-v1:%%'
              AND outcome.calibration_cohort LIKE 'option-scorecard-truth-v1:%%'
            """,
            [structure, model_version, as_of, instrument_id],
        ).fetchall()
        exact = [dict(row) for row in rows if row["market_regime"] == market_regime]
        returns = [float(row["current_return"]) for row in exact]
        predictions = [
            (float(row["probability_profit"]), 1.0 if float(row["current_return"]) > 0 else 0.0)
            for row in exact if row["probability_profit"] is not None
        ]
        standard_error = pstdev(returns) / sqrt(len(returns)) if len(returns) > 1 else None
        return {
            "sample_size": len(returns),
            "prediction_sample_size": len(predictions),
            "lower_95_expectancy": mean(returns) - 1.96 * standard_error if standard_error is not None else None,
            "brier_score": mean((prediction - actual) ** 2 for prediction, actual in predictions) if predictions else None,
            "other_regime_monitoring_count": sum(1 for row in rows if row["market_regime"] != market_regime),
        }

    def _publish_decision_system(self, run_id: UUID, summary: dict[str, Any]) -> UUID:
        with self.runtime.read(JOB_PROFILE) as connection:
            rows = connection.execute(
                """
                SELECT decision.id::text AS decision_id, decision.instrument_id, decision.as_of,
                       decision.state, decision.reasons, decision.blockers,
                       option_decision.paper_state, option_decision.discovery_lane, option_decision.structure,
                       option_decision.entry_price, option_decision.max_loss, option_decision.expected_value,
                       option_decision.data_confidence, option_decision.execution_confidence,
                       option_decision.market_regime, option_decision.model_version,
                       option_decision.relative_value_id, option_decision.modeled_net_edge,
                       option_decision.quote_observed_at, option_decision.synthetic_legs,
                       option_decision.details, contract.expiration, contract.strike, contract.option_type,
                       instrument.symbol, snapshot.market_session
                FROM analysis.decision decision
                JOIN analysis.option_decision option_decision ON option_decision.decision_id = decision.id
                JOIN catalog.option_contract contract ON contract.id = option_decision.contract_id
                JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
                JOIN raw.option_snapshot snapshot ON snapshot.id = option_decision.snapshot_id
                WHERE decision.run_id = %s AND option_decision.paper_state IS NOT NULL
                ORDER BY option_decision.paper_state = 'PAPER_READY' DESC,
                         option_decision.modeled_net_edge DESC NULLS LAST, decision.id
                """,
                [run_id],
            ).fetchall()
        candidates = published_candidates(
            self.runtime, list(rows), sleeve_capital=self.options_risk_sleeve_capital
        )
        return self.analysis.publish(
            run_id, "options-decision-system", {"options_decision_candidate": candidates},
            validation={"paper_only": True, "candidate_count": len(candidates)}, complete_run_summary=summary,
        )

    def _advance_pending_shadows(
        self, connection: Any, *, snapshot_id: int, generation_id: int, current_slot: Any, symbol: str) -> None:
        """Enter v3 shadows only from an ordered later, coherent quote cohort."""

        rows = connection.execute(
            """
            SELECT shadow.id, shadow.decision_id, pending_snapshot.slot_at AS pending_slot,
                   option_decision.structure, option_decision.synthetic_legs
            FROM analysis.shadow_trade shadow
            JOIN analysis.decision decision ON decision.id = shadow.decision_id
            JOIN analysis.option_decision option_decision ON option_decision.decision_id = decision.id
            JOIN raw.option_capture_generation pending_generation ON pending_generation.id = shadow.entry_cohort_id
            JOIN raw.option_snapshot pending_snapshot ON pending_snapshot.id = pending_generation.snapshot_id
            WHERE shadow.status = 'pending' AND shadow.source_kind = 'options_history_v3'
              AND shadow.entry_cohort_id <> %s AND pending_snapshot.history_symbol = %s
              AND pending_snapshot.slot_at < %s
            FOR UPDATE OF shadow
            """,
            [generation_id, symbol, current_slot],
        ).fetchall()
        legs_by_shadow = cohort_legs(connection, generation_id, [dict(row) for row in rows])
        for raw in rows:
            row, legs = dict(raw), legs_by_shadow.get(raw["id"], [])
            if not is_later_capture_cohort(row["pending_slot"], current_slot):
                continue
            entry, blockers = conservative_entry(legs, str(row["structure"]))
            if entry is not None:
                connection.execute(
                    """
                    UPDATE analysis.shadow_trade
                    SET status = 'entered', entry_at = %s, entry_price = %s, entry_cohort_id = %s,
                        fill_basis = 'later_capture_worst_side', metrics = metrics || %s
                    WHERE id = %s AND status = 'pending'
                    """,
                    [latest_available_at(legs) or datetime.now(UTC), entry, generation_id,
                     Jsonb({"entry_leg_count": len(legs), "entry_blockers": blockers}), row["id"]],
                )
            elif current_slot is not None and row["pending_slot"] is not None and (current_slot - row["pending_slot"]).total_seconds() >= 30 * 60:
                connection.execute(
                    """
                    UPDATE analysis.shadow_trade
                    SET status = 'unfilled', exit_at = now(), pending_entry_reason = 'no_valid_entry_within_two_capture_slots'
                    WHERE id = %s AND status = 'pending'
                    """,
                    [row["id"]],
                )

    def _mark_entered_shadows(self, connection: Any, *, generation_id: int, current_slot: Any, symbol: str) -> None:
        """Persist later worst-side package marks; missing legs are never synthesized."""

        rows = connection.execute(
            """
            SELECT shadow.id, shadow.decision_id, shadow.entry_price, option_decision.structure,
                   option_decision.synthetic_legs
            FROM analysis.shadow_trade shadow
            JOIN analysis.decision decision ON decision.id = shadow.decision_id
            JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
            JOIN analysis.option_decision option_decision ON option_decision.decision_id = decision.id
            JOIN raw.option_capture_generation entry_generation ON entry_generation.id = shadow.entry_cohort_id
            JOIN raw.option_snapshot entry_snapshot ON entry_snapshot.id = entry_generation.snapshot_id
            WHERE shadow.status = 'entered' AND shadow.source_kind = 'options_history_v3'
              AND shadow.entry_cohort_id <> %s AND instrument.symbol = %s AND entry_snapshot.slot_at < %s
            FOR UPDATE OF shadow
            """,
            [generation_id, symbol, current_slot],
        ).fetchall()
        legs_by_shadow = cohort_legs(connection, generation_id, [dict(row) for row in rows])
        for raw in rows:
            row, legs = dict(raw), legs_by_shadow.get(raw["id"], [])
            mark, blockers = conservative_mark(legs, str(row["structure"]))
            entry = float(row["entry_price"])
            metrics = {"mark_generation_id": generation_id, "mark_price": mark, "mark_missing": mark is None,
                       "mark_leg_count": len(legs), "mark_blockers": blockers}
            connection.execute(
                "UPDATE analysis.shadow_trade SET metrics = metrics || %s WHERE id = %s",
                [Jsonb(metrics), row["id"]],
            )
            upsert_observing_shadow_outcome(
                connection,
                decision_id=str(row["decision_id"]),
                shadow_trade_id=str(row["id"]),
                observed_through=latest_available_at(legs) or datetime.now(UTC),
                current_return=(mark / entry - 1.0) if mark is not None and entry > 0 else None,
            )
