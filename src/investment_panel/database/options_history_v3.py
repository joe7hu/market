"""Persistence and deterministic replay for history-v3 price-shape evidence."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from math import sqrt
from statistics import mean, pstdev
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from investment_panel.analysis.history_v3 import MODEL_REVISION, analyze_group
from investment_panel.core.option_underwriting import (
    conservative_entry,
    conservative_mark,
    historical_payoff_statistics,
    paper_state,
    permitted_structures,
    thesis_v2_blocker,
)
from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE
from investment_panel.database.options_history_v3_shadows import cohort_legs, latest_available_at
from investment_panel.database.options_history_v3_evidence import quote_package


class OptionHistoryV3Materializer:
    """Bulk materialize an immutable capture generation into a separate analysis run."""

    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime
        self.analysis = AnalysisRepository(runtime)

    def materialize(
        self,
        *,
        snapshot_id: int,
        capture_generation_id: int,
        model_revision: str = MODEL_REVISION,
        code_version: str = "history-v3",
    ) -> dict[str, Any]:
        metadata, rows = self._generation_rows(snapshot_id, capture_generation_id)
        if metadata is None:
            raise ValueError("capture generation does not belong to snapshot")
        if metadata["capture_state"] != "complete":
            raise ValueError("only complete capture generations can be materialized")
        run_id = self.analysis.start_run(
            "option_history_v3",
            input_cutoff=metadata["available_at"],
            code_version=code_version,
            inputs={
                "snapshot_id": snapshot_id,
                "capture_generation_id": capture_generation_id,
                "model_revision": model_revision,
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
            )
            publication_id = self._publish_decision_system(run_id, result)
            result["publication_id"] = str(publication_id)
            return {"analysis_run_id": str(run_id), **result}
        except Exception as exc:
            self.analysis.finish_run(run_id, "failed", {"error": f"{type(exc).__name__}: {exc}"})
            raise

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
                       contract.strike::double precision AS strike,
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
    ) -> dict[str, Any]:
        grouped: dict[tuple[Any, str], list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault((row["expiration"], str(row["option_type"])), []).append(row)
        summary_count, relative_count, failures = 0, 0, 0
        eligible_groups, fit_attempts, succeeded_groups, decision_count = 0, 0, 0, 0
        persisted: list[dict[str, Any]] = []
        with self.runtime.transaction(JOB_PROFILE) as connection:
            theses = {
                int(row["instrument_id"]): {"id": int(row["id"]), **dict(row["thesis"])}
                for row in connection.execute(
                    "SELECT id, instrument_id, thesis FROM app.thesis WHERE status = 'current'"
                ).fetchall()
            }
            bars_by_instrument = self._confirmed_bars(
                connection,
                {int(row["instrument_id"]) for row in rows},
                metadata["available_at"],
            )
            for (expiration, option_type), group in grouped.items():
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
                connection.execute(
                    """
                    INSERT INTO analysis.option_surface_summary
                        (snapshot_id, expiration, option_type, feature_version, dte, atm_iv, delta_25_iv,
                         skew_25, smile_slope, smile_curvature, term_slope, average_spread_pct,
                         liquidity_score, metrics, analysis_run_id, capture_generation_id, fit_method,
                         fit_status, eligible_point_count, group_duration_seconds,
                         max_quote_age_seconds, fit_rmse, candidate_count)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, NULL, NULL, NULL, NULL, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        snapshot_id, expiration, option_type, model_revision, summary["dte"], summary["atm_iv"],
                        summary["delta_25_iv"], summary["average_spread_pct"], summary["liquidity_score"],
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
                        )
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
            "surface_summaries": summary_count,
            "relative_values": relative_count,
            "solver_failures": failures,
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
        current_complete_generation: bool,
    ) -> int:
        """Persist thesis-led or research-only candidates from immutable v3 evidence."""

        long_structure = "long_call" if quote["option_type"] == "call" else "long_put"
        lane = "anomaly"
        if thesis_v2_blocker(thesis) is None and long_structure in permitted_structures(str(thesis.get("direction"))):
            lane = "thesis"
        specs: list[tuple[str, list[dict[str, Any]]]] = [(long_structure, [_candidate_leg(quote, "long")])]
        if lane == "thesis":
            spread_structure = "call_debit_spread" if quote["option_type"] == "call" else "put_debit_spread"
            short_leg = _spread_short_leg(group, quote)
            if short_leg is not None:
                specs.append((spread_structure, [_candidate_leg(quote, "long"), _candidate_leg(short_leg, "short")]))
        created = 0
        for structure, legs in specs:
            scenario = historical_payoff_statistics(
                spot=float(quote["underlying_price"]), legs=legs,
                terminal_returns=_non_overlapping_returns(bars, int(quote.get("dte") or 0)),
                seed=_candidate_seed(generation_id, relative_value_id, structure),
            )
            calibration = self._calibration(connection, structure, _market_regime(bars), MODEL_REVISION)
            blockers = [*value["blockers"], *scenario["blockers"]]
            state = paper_state(
                structure=structure, lane=lane, thesis=thesis, fit_status="succeeded", blockers=blockers,
                scenario_count=int(scenario["scenario_count"]), expected_value=scenario["expected_value"],
                lower_95_expected_value=scenario["lower_95_expected_value"], max_loss=scenario["max_loss"],
                data_confidence=value["confidence"], execution_confidence=_execution_confidence(legs),
                calibration=calibration, current_complete_generation=current_complete_generation,
            )
            self._store_candidate(
                connection, run_id=run_id, snapshot_id=snapshot_id, generation_id=generation_id,
                relative_value_id=relative_value_id, value=value, quote=quote, thesis=thesis,
                structure=structure, legs=legs, lane=lane, state=state, scenario=scenario,
                market_regime=_market_regime(bars), calibration=calibration, as_of=as_of,
            )
            created += 1
        return created

    def _store_candidate(
        self, connection: Any, *, run_id: UUID, snapshot_id: int, generation_id: int,
        relative_value_id: int, value: dict[str, Any], quote: dict[str, Any], thesis: dict[str, Any] | None,
        structure: str, legs: list[dict[str, Any]], lane: str, state: dict[str, Any],
        scenario: dict[str, Any], market_regime: str, calibration: dict[str, Any], as_of: datetime,
    ) -> None:
        decision_key = f"history-v3:{relative_value_id}:{structure}"
        decision = connection.execute(
            """
            INSERT INTO analysis.decision
                (run_id, decision_key, kind, instrument_id, as_of, state, score, quality_status,
                 reasons, blockers, input_hash)
            VALUES (%s, %s, 'option', %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id, decision_key) DO UPDATE
            SET state = EXCLUDED.state, score = EXCLUDED.score, reasons = EXCLUDED.reasons,
                blockers = EXCLUDED.blockers, input_hash = EXCLUDED.input_hash
            RETURNING id
            """,
            [run_id, decision_key, quote["instrument_id"], quote["quote_observed_at"],
             _decision_state(state["paper_state"]), value.get("modeled_net_edge"), value["quality_status"],
             state["reasons"], state["blockers"], _hash({"value": value, "structure": structure, "scenario": scenario})],
        ).fetchone()
        decision_id = decision["id"]
        execution_confidence = _execution_confidence(legs)
        details = {
            "paper_only": True, "relative_value_evidence": value.get("evidence"),
            "historical_paths": scenario, "calibration": calibration, "as_of": as_of.isoformat(),
            "thesis": {
                "id": thesis.get("id") if thesis else None,
                "revision": thesis.get("revision") if thesis else None,
                "invalidation": thesis.get("invalidation") if thesis else None,
                "horizon_date": thesis.get("horizon_date") if thesis else None,
            },
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
                MODEL_REVISION, market_regime, value.get("fair_low"), value.get("fair_high"), value.get("modeled_net_edge"),
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
        if state["paper_state"] in {"WATCH", "PAPER_READY"}:
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
        if not instrument_ids:
            return {}
        rows = connection.execute(
            """
            SELECT DISTINCT ON (instrument_id, trading_date) instrument_id, trading_date, close
            FROM raw.confirmed_price_bar
            WHERE instrument_id = ANY(%s) AND interval = '1d' AND close > 0
              AND observed_at <= %s AND available_at <= %s
            ORDER BY instrument_id, trading_date, available_at DESC
            """,
            [sorted(instrument_ids), as_of, as_of],
        ).fetchall()
        bars: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            bars.setdefault(int(row["instrument_id"]), []).append(dict(row))
        return bars

    def _calibration(self, connection: Any, structure: str, market_regime: str, model_version: str) -> dict[str, Any]:
        rows = connection.execute(
            """
            SELECT option_decision.market_regime, option_decision.probability_profit, outcome.current_return
            FROM analysis.option_outcome outcome
            JOIN analysis.decision decision ON decision.id = outcome.decision_id
            JOIN analysis.option_decision option_decision ON option_decision.decision_id = decision.id
            JOIN analysis.shadow_trade shadow ON shadow.decision_id = decision.id
            WHERE shadow.source_kind = 'options_history_v3' AND option_decision.structure = %s
              AND option_decision.model_version = %s AND outcome.maturity_state IN ('mature', 'expired')
              AND outcome.current_return IS NOT NULL
            """,
            [structure, model_version],
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
            "lower_95_expectancy": mean(returns) - 1.96 * standard_error if standard_error is not None else None,
            "brier_score": mean((prediction - actual) ** 2 for prediction, actual in predictions) if predictions else None,
            "other_regime_monitoring_count": sum(1 for row in rows if row["market_regime"] != market_regime),
        }

    def _publish_decision_system(self, run_id: UUID, summary: dict[str, Any]) -> UUID:
        with self.runtime.read(JOB_PROFILE) as connection:
            rows = connection.execute(
                """
                SELECT decision.id::text AS decision_id, decision.instrument_id, decision.as_of,
                       option_decision.paper_state, option_decision.discovery_lane, option_decision.structure,
                       option_decision.entry_price, option_decision.max_loss, option_decision.expected_value,
                       option_decision.data_confidence, option_decision.execution_confidence,
                       option_decision.market_regime, option_decision.model_version,
                       option_decision.relative_value_id, option_decision.modeled_net_edge,
                       option_decision.quote_observed_at, option_decision.synthetic_legs,
                       contract.expiration, contract.strike, contract.option_type
                FROM analysis.decision decision
                JOIN analysis.option_decision option_decision ON option_decision.decision_id = decision.id
                JOIN catalog.option_contract contract ON contract.id = option_decision.contract_id
                WHERE decision.run_id = %s AND option_decision.paper_state IS NOT NULL
                ORDER BY option_decision.paper_state = 'PAPER_READY' DESC,
                         option_decision.modeled_net_edge DESC NULLS LAST, decision.id
                """,
                [run_id],
            ).fetchall()
        candidates = [
            {
                "stable_key": str(row["decision_id"]), "decision_id": str(row["decision_id"]),
                "instrument_id": int(row["instrument_id"]), "as_of": row["as_of"],
                "paper_state": row["paper_state"], "discovery_lane": row["discovery_lane"],
                "structure": row["structure"], "entry_price": row["entry_price"], "max_loss": row["max_loss"],
                "expected_value": row["expected_value"], "data_confidence": row["data_confidence"],
                "execution_confidence": row["execution_confidence"], "market_regime": row["market_regime"],
                "model_version": row["model_version"], "relative_value_id": row["relative_value_id"],
                "modeled_net_edge": row["modeled_net_edge"], "quote_observed_at": row["quote_observed_at"],
                "leg_quotes": list(row["synthetic_legs"] or []),
                "expiration": row["expiration"], "strike": row["strike"], "option_type": row["option_type"],
                "execution_ready": row["paper_state"] == "PAPER_READY", "paper_only": True,
            }
            for row in rows
        ]
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
            connection.execute(
                """
                INSERT INTO analysis.option_outcome (decision_id, maturity_state, observed_through, current_return)
                VALUES (%s, 'observing', %s, %s)
                ON CONFLICT (decision_id) DO UPDATE
                SET maturity_state = EXCLUDED.maturity_state, observed_through = EXCLUDED.observed_through,
                    current_return = EXCLUDED.current_return, updated_at = now()
                """,
                [row["decision_id"], latest_available_at(legs) or datetime.now(UTC),
                 (mark / entry - 1.0) if mark is not None and entry > 0 else None],
            )


def _capture_group_quality(rows: list[dict[str, Any]]) -> tuple[float | None, list[str]]:
    """Return the only capture-wide rejection reasons.

    Individual quote freshness, status, completeness, and liquidity problems are
    preserved as row-level evidence by ``analyze_group``.  Only an incoherent
    underlying or an overlong capture makes the whole expiry/type unusable.
    """

    if not rows:
        return None, ["empty_capture_group"]
    started = min((row.get("group_started_at") for row in rows if row.get("group_started_at")), default=None)
    finished = max((row.get("group_finished_at") for row in rows if row.get("group_finished_at")), default=None)
    blockers: list[str] = []
    if started is None or finished is None:
        blockers.append("missing_group_timestamps")
    elif (finished - started).total_seconds() > 60:
        blockers.append("group_duration_stale")
    values = [float(row["underlying_price"]) for row in rows if row.get("underlying_price") is not None]
    observed_at = [row.get("underlying_observed_at") for row in rows]
    if len(values) != len(rows) or any(value is None for value in observed_at):
        blockers.append("missing_aligned_underlying")
    elif len({round(value, 8) for value in values}) != 1 or len({value for value in observed_at}) != 1:
        blockers.append("inconsistent_aligned_underlying")
    return (values[0] if not blockers and values else None), sorted(set(blockers))


def is_later_capture_cohort(pending_slot: datetime | None, current_slot: datetime | None) -> bool:
    """True only when a candidate can enter after its own evidence cohort."""

    return pending_slot is not None and current_slot is not None and current_slot > pending_slot


def _summary(rows: list[dict[str, Any]], result: dict[str, Any], spot: float | None) -> dict[str, Any]:
    nearest = min(rows, key=lambda row: abs(float(row["strike"]) - (spot or float(row["strike"]))))
    spreads = [float(row["ask"] - row["bid"]) / float(row["mid"]) for row in rows if row.get("mid") and row.get("ask") is not None and row.get("bid") is not None]
    started = min((row.get("group_started_at") for row in rows if row.get("group_started_at")), default=None)
    finished = max((row.get("group_finished_at") for row in rows if row.get("group_finished_at")), default=None)
    ages = [(finished - row["provider_observed_at"]).total_seconds() for row in rows if finished and row.get("provider_observed_at")]
    candidate_count = sum(value["classification"] != "rejected" for value in result["relative_values"])
    return {
        "dte": int(nearest["dte"]), "atm_iv": nearest.get("provider_iv"),
        "delta_25_iv": _nearest_delta_iv(rows),
        "average_spread_pct": sum(spreads) / len(spreads) if spreads else None,
        "liquidity_score": float(len(rows)),
        "group_duration_seconds": (finished - started).total_seconds() if started and finished else None,
        "max_quote_age_seconds": max(ages) if ages else None,
        "candidate_count": candidate_count,
        "metrics": {
            "blockers": result["blockers"], "static_arbitrage": result["static_findings"],
            "fit": result["fit"].diagnostics, "row_metrics": result["row_metrics"],
        },
    }


def _nearest_delta_iv(rows: list[dict[str, Any]]) -> float | None:
    eligible = [row for row in rows if row.get("provider_delta") is not None and row.get("provider_iv") is not None]
    if not eligible:
        return None
    return float(min(eligible, key=lambda row: abs(abs(float(row["provider_delta"])) - 0.25))["provider_iv"])


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _long_delta_eligible(quote: dict[str, Any]) -> bool:
    """Long candidates require a fresh provider delta in the release-1 band."""

    value = quote.get("provider_delta")
    observed = quote.get("provider_observed_at")
    available = quote.get("available_at")
    try:
        delta = abs(float(value))
    except (TypeError, ValueError):
        return False
    if not 0.35 <= delta <= 0.65:
        return False
    return observed is not None and available is not None and (available - observed).total_seconds() <= 180


def _candidate_leg(quote: dict[str, Any], side: str) -> dict[str, Any]:
    bid_size, ask_size = quote.get("bid_size"), quote.get("ask_size")
    size_available = (bid_size is None or bid_size >= 1) and (ask_size is None or ask_size >= 1)
    observed_at = quote.get("provider_observed_at")
    return {
        "contract_id": int(quote["contract_id"]), "option_type": quote["option_type"], "side": side,
        "strike": float(quote["strike"]), "bid": quote.get("bid"), "ask": quote.get("ask"),
        # Synthetic legs are JSONB evidence.  Keep the timestamp explicit but
        # JSON-safe so a valid live capture cannot fail after collection.
        "observed_at": observed_at.isoformat() if observed_at is not None else None,
        "size_available": size_available,
        "bid_size": bid_size, "ask_size": ask_size, "open_interest": quote.get("open_interest"),
        "volume": quote.get("volume"), "provider_iv": quote.get("provider_iv"),
        "provider_delta": quote.get("provider_delta"),
    }


def _spread_short_leg(group: list[dict[str, Any]], long_leg: dict[str, Any]) -> dict[str, Any] | None:
    long_delta = abs(float(long_leg.get("provider_delta") or 0))
    if not 0.35 <= long_delta <= 0.65:
        return None
    candidates = [
        row for row in group
        if row.get("provider_delta") is not None and 0.15 <= abs(float(row["provider_delta"])) <= 0.40
        and ((long_leg["option_type"] == "call" and float(row["strike"]) > float(long_leg["strike"]))
             or (long_leg["option_type"] == "put" and float(row["strike"]) < float(long_leg["strike"])))
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda row: abs(abs(float(row["provider_delta"])) - 0.25))


def _non_overlapping_returns(bars: list[dict[str, Any]], dte: int) -> tuple[float, ...]:
    horizon = max(1, min(dte, 120))
    closes = [float(row["close"]) for row in bars if row.get("close") is not None and float(row["close"]) > 0]
    return tuple(closes[index + horizon] / closes[index] - 1.0 for index in range(0, len(closes) - horizon, horizon))


def _market_regime(bars: list[dict[str, Any]]) -> str:
    closes = [float(row["close"]) for row in bars if row.get("close") is not None and float(row["close"]) > 0]
    if len(closes) < 200:
        return "unavailable"
    recent_returns = [closes[index] / closes[index - 1] - 1.0 for index in range(len(closes) - 19, len(closes))]
    realized_vol = pstdev(recent_returns) * sqrt(252) if len(recent_returns) > 1 else 0.0
    bucket = "low" if realized_vol < 0.15 else "normal" if realized_vol < 0.30 else "high"
    trend = "above_200d" if closes[-1] >= mean(closes[-200:]) else "below_200d"
    return f"{trend}:{bucket}"


def _execution_confidence(legs: list[dict[str, Any]]) -> float:
    scores = []
    for leg in legs:
        bid, ask = leg.get("bid"), leg.get("ask")
        if bid is None or ask is None or float(ask) < float(bid):
            return 0.0
        midpoint = (float(bid) + float(ask)) / 2.0
        if midpoint <= 0 or leg.get("size_available") is False:
            return 0.0
        scores.append(max(0.0, min(1.0, 1.0 - (float(ask) - float(bid)) / midpoint)))
    return min(scores, default=0.0)


def _candidate_seed(generation_id: int, relative_value_id: int, structure: str) -> int:
    digest = hashlib.sha256(f"{generation_id}:{relative_value_id}:{structure}".encode()).hexdigest()
    return int(digest[:16], 16)


def _decision_state(value: str) -> str:
    return "READY" if value == "PAPER_READY" else "REJECTED" if value == "REJECT" else "WATCH"
