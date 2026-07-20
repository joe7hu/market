"""Persistence and deterministic replay for history-v3 price-shape evidence."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from investment_panel.analysis.history_v3 import MODEL_REVISION, analyze_group, rejected_value
from investment_panel.core.option_underwriting import paper_state
from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


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
            self.analysis.finish_run(run_id, "succeeded", result)
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
                       quote.open_interest, quote.provider_delta, quote.market_data_status,
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
        eligible_groups, fit_attempts, succeeded_groups = 0, 0, 0
        persisted: list[dict[str, Any]] = []
        with self.runtime.transaction(JOB_PROFILE) as connection:
            theses = {
                int(row["instrument_id"]): {"id": int(row["id"]), **dict(row["thesis"])}
                for row in connection.execute(
                    "SELECT id, instrument_id, thesis FROM app.thesis WHERE status = 'current'"
                ).fetchall()
            }
            for (expiration, option_type), group in grouped.items():
                temporal_blockers = _temporal_blockers(group)
                spot = _group_spot(group)
                result = analyze_group(group, spot=spot, option_type=option_type, model_revision=model_revision)
                if temporal_blockers:
                    result["blockers"] = sorted(set(result["blockers"] + temporal_blockers))
                    result["relative_values"] = [
                        rejected_value(row, model_revision, result["blockers"], result["static_findings"])
                        for row in group
                    ]
                fit = result["fit"]
                group_is_eligible = result["eligible_count"] >= 12 and not temporal_blockers
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
                        self._store_research_candidate(
                            connection, run_id=run_id, snapshot_id=snapshot_id, generation_id=capture_generation_id,
                            relative_value_id=int(stored_value["id"]), value=value, quote=quote,
                            thesis=theses.get(int(quote["instrument_id"])),
                        )
            self._advance_pending_shadows(
                connection, snapshot_id=snapshot_id, generation_id=capture_generation_id,
                current_slot=metadata.get("slot_at"), symbol=str(metadata.get("symbol") or "QQQ"),
            )
            self._mark_entered_shadows(connection, generation_id=capture_generation_id, symbol=str(metadata.get("symbol") or "QQQ"))
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
            "deterministic_hash": digest,
            "diff_count": relative_count,
        }

    def _store_research_candidate(
        self, connection: Any, *, run_id: UUID, snapshot_id: int, generation_id: int,
        relative_value_id: int, value: dict[str, Any], quote: dict[str, Any], thesis: dict[str, Any] | None,
    ) -> None:
        structure = "long_call" if quote["option_type"] == "call" else "long_put"
        state = paper_state(
            structure=structure, lane="anomaly", thesis=thesis, fit_status="succeeded",
            blockers=value["blockers"], scenario_count=0, expected_value=None,
            lower_95_expected_value=None, max_loss=(quote.get("ask") or 0) * 100,
            data_confidence=value["confidence"], execution_confidence=0.0,
        )
        decision_key = f"history-v3:{relative_value_id}"
        decision = connection.execute(
            """
            INSERT INTO analysis.decision
                (run_id, decision_key, kind, instrument_id, as_of, state, score, quality_status,
                 reasons, blockers, input_hash)
            VALUES (%s, %s, 'option', %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id, decision_key) DO UPDATE
            SET state = EXCLUDED.state, score = EXCLUDED.score, reasons = EXCLUDED.reasons,
                blockers = EXCLUDED.blockers
            RETURNING id
            """,
            [run_id, decision_key, quote["instrument_id"], quote["quote_observed_at"],
             "READY" if state["paper_state"] == "PAPER_READY" else state["paper_state"].lower(),
             value.get("modeled_net_edge"), value["quality_status"], state["reasons"], state["blockers"], _hash(value)],
        ).fetchone()
        decision_id = decision["id"]
        connection.execute(
            """
            INSERT INTO analysis.option_decision
                (decision_id, contract_id, snapshot_id, quote_observed_at, premium_mid, fill_assumption,
                 structure, entry_price, max_loss, expected_value, data_confidence, execution_confidence,
                 details, paper_state, discovery_lane, thesis_id, relative_value_id, model_version,
                 fair_low, fair_high, modeled_net_edge)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, %s, %s, %s, %s, 'anomaly', %s, %s, %s, %s, %s, %s)
            ON CONFLICT (decision_id) DO UPDATE
            SET paper_state = EXCLUDED.paper_state, relative_value_id = EXCLUDED.relative_value_id,
                modeled_net_edge = EXCLUDED.modeled_net_edge, details = EXCLUDED.details
            """,
            [decision_id, quote["contract_id"], snapshot_id, quote["quote_observed_at"], quote.get("mid"), quote.get("ask"),
             structure, quote.get("ask"), (quote.get("ask") or 0) * 100, value.get("confidence"), 0.0,
             Jsonb({"paper_only": True, "relative_value_evidence": value.get("evidence")}), state["paper_state"],
             thesis.get("id") if thesis else None, relative_value_id, MODEL_REVISION,
             value.get("fair_low"), value.get("fair_high"), value.get("modeled_net_edge")],
        )
        connection.execute(
            """
            INSERT INTO analysis.shadow_trade
                (decision_id, status, pending_entry_reason, entry_cohort_id, structure, source_kind, metrics)
            VALUES (%s, 'pending', 'next_valid_cohort_required', %s, %s, 'system', %s)
            ON CONFLICT (decision_id) DO NOTHING
            """,
            [decision_id, generation_id, structure, Jsonb({"no_same_capture_entry": True, "fill_basis": "pending_next_cohort"})],
        )

    def _advance_pending_shadows(
        self, connection: Any, *, snapshot_id: int, generation_id: int, current_slot: Any, symbol: str) -> None:
        """Enter only older pending long shadows at a later cohort's ask-side mark."""

        rows = connection.execute(
            """
            SELECT shadow.id, shadow.decision_id, pending_snapshot.slot_at AS pending_slot,
                   quote.ask, quote.bid, quote.available_at, quote.provider_observed_at
            FROM analysis.shadow_trade shadow
            JOIN analysis.decision decision ON decision.id = shadow.decision_id
            JOIN analysis.option_decision option_decision ON option_decision.decision_id = decision.id
            JOIN raw.option_capture_generation pending_generation ON pending_generation.id = shadow.entry_cohort_id
            JOIN raw.option_snapshot pending_snapshot ON pending_snapshot.id = pending_generation.snapshot_id
            LEFT JOIN raw.option_quote quote
              ON quote.capture_generation_id = %s AND quote.contract_id = option_decision.contract_id
            WHERE shadow.status = 'pending' AND shadow.source_kind = 'system'
              AND shadow.entry_cohort_id <> %s AND pending_snapshot.history_symbol = %s
            FOR UPDATE OF shadow
            """,
            [generation_id, generation_id, symbol],
        ).fetchall()
        for row in rows:
            if row["ask"] is not None and row["bid"] is not None and float(row["ask"]) >= float(row["bid"]):
                connection.execute(
                    """
                    UPDATE analysis.shadow_trade
                    SET status = 'entered', entry_at = %s, entry_price = %s, entry_cohort_id = %s,
                        fill_basis = 'later_capture_long_ask'
                    WHERE id = %s AND status = 'pending'
                    """,
                    [row["available_at"] or datetime.now(UTC), row["ask"], generation_id, row["id"]],
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

    def _mark_entered_shadows(self, connection: Any, *, generation_id: int, symbol: str) -> None:
        """Persist later bid-side marks; missing bids are explicitly recorded, never synthesized."""

        rows = connection.execute(
            """
            SELECT shadow.id, shadow.decision_id, shadow.entry_price, quote.bid, quote.available_at
            FROM analysis.shadow_trade shadow
            JOIN analysis.decision decision ON decision.id = shadow.decision_id
            JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
            JOIN analysis.option_decision option_decision ON option_decision.decision_id = decision.id
            LEFT JOIN raw.option_quote quote
              ON quote.capture_generation_id = %s AND quote.contract_id = option_decision.contract_id
            WHERE shadow.status = 'entered' AND shadow.source_kind = 'system'
              AND shadow.entry_cohort_id <> %s AND instrument.symbol = %s
            FOR UPDATE OF shadow
            """,
            [generation_id, generation_id, symbol],
        ).fetchall()
        for row in rows:
            mark = float(row["bid"]) if row["bid"] is not None else None
            entry = float(row["entry_price"])
            metrics = {"mark_generation_id": generation_id, "mark_price": mark, "mark_missing": mark is None}
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
                [row["decision_id"], row["available_at"] or datetime.now(UTC), (mark / entry - 1.0) if mark is not None and entry > 0 else None],
            )


def _temporal_blockers(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["empty_capture_group"]
    started = min((row.get("group_started_at") for row in rows if row.get("group_started_at")), default=None)
    finished = max((row.get("group_finished_at") for row in rows if row.get("group_finished_at")), default=None)
    if started is None or finished is None:
        return ["missing_group_timestamps"]
    blockers: list[str] = []
    if (finished - started).total_seconds() > 60:
        blockers.append("group_duration_stale")
    ages = [
        (finished - observed).total_seconds()
        for row in rows
        for observed in [row.get("provider_observed_at")]
        if observed is not None
    ]
    if not ages or max(ages) > 180:
        blockers.append("quote_age_stale")
    if any(row.get("underlying_observed_at") is None for row in rows):
        blockers.append("missing_aligned_underlying")
    return blockers


def _group_spot(rows: list[dict[str, Any]]) -> float | None:
    values = [float(row["underlying_price"]) for row in rows if row.get("underlying_price") is not None]
    return values[0] if values and all(value == values[0] for value in values) else None


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
        "metrics": {"blockers": result["blockers"], "static_arbitrage": result["static_findings"], "fit": result["fit"].diagnostics},
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
