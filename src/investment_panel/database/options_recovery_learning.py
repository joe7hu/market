"""Full-denominator PostgreSQL learning owner for recovery strategies."""

from __future__ import annotations

from datetime import UTC, datetime
from statistics import mean
from typing import Any, Iterable

from psycopg.types.json import Jsonb

from investment_panel.core.options_event_tape import trading_sessions_between
from investment_panel.core.options_recovery import ExecutableLeg, QuoteCapture, evaluate_lifecycle
from investment_panel.core.options_recovery_metrics import (
    counterfactual_metrics,
    lower_confidence_bound,
    recovery_promotion_passes,
)
from investment_panel.core.options_recovery_registry import (
    FamilySignal,
    RankedRecoveryCandidate,
    RecoveryEventState,
    contract_gate,
    strategies,
)
from investment_panel.database.options_recovery_execution_support import (
    contract_quote,
    invalidated,
    number,
)
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


class RecoveryLearningRepository:
    """Retain counterfactuals, evaluate executable paths, and gate promotion."""

    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def record_capture(
        self,
        *,
        event: RecoveryEventState,
        capture: dict[str, Any],
        contracts: Iterable[dict[str, Any]],
        revisions: dict[str, int],
        family_signals: Iterable[FamilySignal],
    ) -> int:
        """Persist every liquid strip contract for both families before ranking."""

        active = {item.family: item.active for item in family_signals}
        stored = 0
        with self.runtime.transaction(JOB_PROFILE) as connection:
            for source in contracts:
                quote = contract_quote(source)
                if quote is None or not _liquid(quote):
                    continue
                for strategy in strategies():
                    gate = contract_gate(quote, family=strategy.key, as_of=capture["finished_at"])
                    if not active.get(strategy.key, False):
                        stage, reason = "observed", "not_featured"
                    elif not gate.eligible:
                        stage, reason = "observed", "gate_reject"
                    else:
                        stage, reason = "eligible", None
                    connection.execute(
                        """
                        INSERT INTO analysis.option_opportunity_observation
                            (event_id, capture_id, capture_generation_id, capture_generation_key,
                             event_contract_id, contract_id, strategy_key, strategy_revision_id,
                             observed_at, available_at, expiration, quote, liquid, selection_stage,
                             miss_reason, data_status)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s, %s, 'ok')
                        ON CONFLICT (event_id, capture_generation_key, contract_id, strategy_key, strategy_revision_id)
                        DO UPDATE SET quote = EXCLUDED.quote, liquid = EXCLUDED.liquid,
                                      selection_stage = EXCLUDED.selection_stage,
                                      miss_reason = EXCLUDED.miss_reason, updated_at = now()
                        """,
                        [
                            event.event_id, capture["id"], capture.get("capture_generation_id"),
                            str(capture.get("capture_generation_id") or capture["id"]), source.get("event_contract_id"),
                            quote.contract_id, strategy.key, revisions[strategy.key], quote.observed_at,
                            quote.available_at, quote.expiration, Jsonb(_quote_payload(quote, gate)), stage, reason,
                        ],
                    )
                    stored += 1
        return stored

    def mark_selection(
        self,
        *,
        event_id: str,
        capture: dict[str, Any],
        revisions: dict[str, int],
        selected: Iterable[tuple[RankedRecoveryCandidate, dict[str, Any]]],
    ) -> None:
        """Turn eligible denominator rows into explainable ranked-out/published rows."""

        selected_rows = list(selected)
        generation_key = str(capture.get("capture_generation_id") or capture["id"])
        with self.runtime.transaction(JOB_PROFILE) as connection:
            for strategy in strategies():
                connection.execute(
                    """
                    UPDATE analysis.option_opportunity_observation
                    SET selection_stage = 'ranked_out', miss_reason = 'ranked_out', updated_at = now()
                    WHERE event_id = %s AND capture_generation_key = %s AND strategy_key = %s
                      AND strategy_revision_id = %s AND selection_stage = 'eligible'
                    """,
                    [event_id, generation_key, strategy.key, revisions[strategy.key]],
                )
            for candidate, _source in selected_rows:
                connection.execute(
                    """
                    UPDATE analysis.option_opportunity_observation
                    SET selection_stage = 'published', miss_reason = NULL,
                        selection_score = %s, lower_confidence_expectancy = %s, updated_at = now()
                    WHERE event_id = %s AND capture_generation_key = %s AND contract_id = %s
                      AND strategy_key = %s AND strategy_revision_id = %s
                    """,
                    [
                        candidate.selection_score, candidate.lower_confidence_expectancy,
                        event_id, generation_key, candidate.quote.contract_id,
                        candidate.family, revisions[candidate.family],
                    ],
                )

    def link_signal(
        self,
        *,
        signal_id: str,
        event_id: str,
        capture: dict[str, Any],
        contract_id: int,
        family: str,
        strategy_revision_id: int,
    ) -> None:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute(
                """
                UPDATE analysis.option_opportunity_observation
                SET signal_id = %s, selection_stage = 'published', miss_reason = NULL, updated_at = now()
                WHERE event_id = %s AND capture_generation_key = %s AND contract_id = %s
                  AND strategy_key = %s AND strategy_revision_id = %s
                """,
                [
                    signal_id, event_id, str(capture.get("capture_generation_id") or capture["id"]),
                    contract_id, family, strategy_revision_id,
                ],
            )

    def sync_paper_lifecycle(self, event_id: str) -> int:
        """Project immutable paper-order state into the observation denominator."""

        with self.runtime.transaction(JOB_PROFILE) as connection:
            result = connection.execute(
                """
                UPDATE analysis.option_opportunity_observation observation
                SET paper_order_id = paper.id,
                    selection_stage = CASE
                      WHEN paper.status IN ('staged', 'entered') THEN 'ticketed'
                      WHEN paper.status = 'partial_exited' THEN 'filled'
                      WHEN paper.status IN ('exited', 'invalidated') THEN 'exited'
                      ELSE observation.selection_stage
                    END,
                    miss_reason = CASE
                      WHEN paper.status = 'unfilled' THEN 'unfilled'
                      WHEN paper.status = 'risk_blocked' THEN 'risk_blocked'
                      WHEN paper.status = 'unmeasurable' THEN 'unmeasurable'
                      ELSE observation.miss_reason
                    END,
                    updated_at = now()
                FROM app.paper_order paper
                WHERE paper.event_signal_id = observation.signal_id
                  AND paper.event_id = %s
                """,
                [event_id],
            )
        return result.rowcount

    def refresh_outcomes(self, *, now: datetime | None = None, limit: int = 2_000) -> dict[str, int]:
        """Refresh every forward path without converting missing continuity into a loss."""

        reference = _utc(now) or datetime.now(UTC)
        with self.runtime.read(JOB_PROFILE) as connection:
            observations = [dict(row) for row in connection.execute(
                """
                SELECT observation.*, event.started_at, event.reference_price, event.event_low,
                       signal.status AS signal_status, paper.status AS paper_status
                FROM analysis.option_opportunity_observation observation
                JOIN analysis.option_event event ON event.id = observation.event_id
                LEFT JOIN analysis.option_event_signal signal ON signal.id = observation.signal_id
                LEFT JOIN app.paper_order paper ON paper.id = observation.paper_order_id
                WHERE observation.outcome_classification = 'observing'
                  AND observation.available_at <= %s
                ORDER BY observation.available_at
                LIMIT %s
                """,
                [reference, limit],
            ).fetchall()]
        totals = {"updated": 0, "captured": 0, "missed": 0, "unfilled": 0, "unmeasurable": 0}
        for observation in observations:
            captures = self._future_captures(observation, reference)
            lifecycle = evaluate_lifecycle(
                published_at=observation["available_at"], quantity=1, captures=captures,
            )
            metrics = counterfactual_metrics(lifecycle, captures)
            classification, data_status, reason = _classification(observation, lifecycle, reference)
            if classification == "observing":
                continue
            self._store_outcome(observation, lifecycle, metrics, classification, data_status, reason, reference)
            totals["updated"] += 1
            totals[classification] += 1
        return totals

    def metrics(self, family: str) -> dict[str, Any]:
        """Compute native recovery metrics from the complete observation denominator."""

        with self.runtime.read(JOB_PROFILE) as connection:
            rows = [dict(row) for row in connection.execute(
                """
                SELECT observation.*, instrument.symbol, signal.signal_at
                FROM analysis.option_opportunity_observation observation
                JOIN analysis.option_event event ON event.id = observation.event_id
                JOIN catalog.instrument instrument ON instrument.id = event.instrument_id
                LEFT JOIN analysis.option_event_signal signal ON signal.id = observation.signal_id
                WHERE observation.strategy_key = %s
                ORDER BY observation.available_at
                """,
                [family],
            ).fetchall()]
            baseline = dict(connection.execute(
                """
                SELECT count(*) AS outcomes,
                       avg(coalesce(outcome.realized_exit_return, outcome.current_return)) AS net_expectancy,
                       avg(outcome.return_1d) AS return_1d,
                       avg(outcome.return_5d) AS return_5d
                FROM analysis.option_outcome outcome
                JOIN analysis.decision decision ON decision.id = outcome.decision_id
                JOIN analysis.run run ON run.id = decision.run_id
                WHERE run.feature_versions->>'option' = 'option-professional-v3-ticket'
                  AND outcome.promotion_eligible IS TRUE
                  AND outcome.outcome_classification = 'captured'
                """
            ).fetchone())
        resolved = [row for row in rows if row["outcome_classification"] != "observing"]
        published = [row for row in rows if row["selection_stage"] in {"published", "ticketed", "filled", "exited"}]
        paper_rows = _latest_by(rows, "paper_order_id")
        fills = [row for row in paper_rows if row.get("paper_order_id") and row.get("entry_fill_at")]
        realized = [float(row["realized_return"]) for row in fills if row.get("realized_return") is not None]
        missed = [row for row in resolved if row["outcome_classification"] == "missed"]
        successes = [row for row in published if _three_x(row)]
        gain_by_symbol: dict[str, float] = {}
        for row in fills:
            gain = max(float(row.get("realized_return") or 0.0), 0.0)
            gain_by_symbol[row["symbol"]] = gain_by_symbol.get(row["symbol"], 0.0) + gain
        total_gain = sum(gain_by_symbol.values())
        calibration_values = [row.get("lower_confidence_expectancy") for row in published if row.get("lower_confidence_expectancy") is not None]
        calibration_gap = _calibration_gap(calibration_values, published)
        return {
            "family": family,
            "observations": len(rows),
            "measurable": sum(row["outcome_classification"] != "unmeasurable" for row in resolved),
            "independent_events": len({str(row["event_id"]) for row in rows}),
            "shadow_signals": len(published),
            "paper_fills": len(fills),
            "fill_rate": len(fills) / len(published) if published else 0.0,
            "precision_at_k": len(successes) / len(published) if published else 0.0,
            "event_3x_recall": _event_recall(rows, target=3.0),
            "event_4x_recall": _event_recall(rows, target=4.0),
            "return_1_session": _mean_field(resolved, "return_1_session"),
            "return_3_session": _mean_field(resolved, "return_3_session"),
            "return_5_session": _mean_field(resolved, "return_5_session"),
            "return_10_session": _mean_field(resolved, "return_10_session"),
            "time_to_2x_sessions": _mean_field(resolved, "time_to_2x_sessions"),
            "time_to_3x_sessions": _mean_field(resolved, "time_to_3x_sessions"),
            "time_to_4x_sessions": _mean_field(resolved, "time_to_4x_sessions"),
            "executable_peak_return": _mean_field(resolved, "executable_peak_return"),
            "realized_return": mean(realized) if realized else None,
            "mae": _mean_field(resolved, "mae"),
            "giveback": _mean_field(resolved, "giveback"),
            "exit_efficiency": _mean_field(resolved, "exit_efficiency"),
            "signal_delay_minutes": _signal_delay_minutes(rows),
            "net_expectancy": mean(realized) if realized else 0.0,
            "lower_95_expectancy": lower_confidence_bound(realized) or 0.0,
            "calibration_gap": calibration_gap,
            "max_ticker_gain_concentration": max(gain_by_symbol.values()) / total_gain if total_gain > 0 else 1.0,
            "opportunity_cost": sum(max(float(row.get("realized_return") or 0.0), 0.0) for row in missed),
            "unresolved_defects": any(
                row["data_status"] != "ok" and row["outcome_classification"] != "unmeasurable"
                for row in resolved
            ),
            "outcomes": {name: sum(row["outcome_classification"] == name for row in resolved)
                         for name in ("captured", "missed", "unfilled", "unmeasurable")},
            "baseline_v3": {
                "outcomes": int(baseline["outcomes"] or 0),
                "net_expectancy": number(baseline["net_expectancy"]),
                "return_1d": number(baseline["return_1d"]),
                "return_5d": number(baseline["return_5d"]),
            },
        }

    def promotion_status(self, family: str) -> dict[str, Any]:
        metrics = self.metrics(family)
        return {"family": family, "eligible": recovery_promotion_passes(metrics), "metrics": metrics}

    def auto_promote_eligible(self, *, enabled: bool) -> int:
        if not enabled:
            return 0
        eligible = {
            strategy.key: metrics
            for strategy in strategies()
            if recovery_promotion_passes(metrics := self.metrics(strategy.key))
        }
        if not eligible:
            return 0
        promoted = 0
        with self.runtime.transaction(JOB_PROFILE) as connection:
            for strategy in strategies():
                metrics = eligible.get(strategy.key)
                if metrics is None:
                    continue
                row = connection.execute(
                    """
                    SELECT id FROM analysis.strategy_revision
                    WHERE strategy_key = %s AND authority_group = %s AND status = 'candidate'
                    ORDER BY revision DESC LIMIT 1 FOR UPDATE
                    """,
                    [strategy.key, f"options-recovery:{strategy.key}"],
                ).fetchone()
                if row is None:
                    continue
                connection.execute("UPDATE analysis.strategy_revision SET status = 'paper_ready', promoted_at = now() WHERE id = %s", [row["id"]])
                connection.execute(
                    """INSERT INTO analysis.strategy_evaluation
                         (strategy_revision_id, evaluation_type, evaluated_at, verdict, metrics, evidence)
                       VALUES (%s, 'recovery_paper_ready', now(), 'pass', %s, %s)""",
                    [row["id"], Jsonb(metrics), Jsonb([{"source": "option_opportunity_observation", "full_denominator": True}])],
                )
                promoted += 1
        return promoted

    def _future_captures(self, observation: dict[str, Any], now: datetime) -> list[QuoteCapture]:
        with self.runtime.read(JOB_PROFILE) as connection:
            rows = connection.execute(
                """
                SELECT capture.finished_at, event.reference_price, event.event_low,
                       spot.price AS spot_price, quote.bid, quote.ask, quote.bid_size, quote.ask_size,
                       quote.available_at
                FROM analysis.option_event_capture capture
                JOIN analysis.option_event event ON event.id = capture.event_id
                LEFT JOIN raw.option_quote quote
                  ON quote.snapshot_id = capture.snapshot_id AND quote.contract_id = %s
                LEFT JOIN LATERAL (
                  SELECT price FROM analysis.option_event_spot
                  WHERE event_id = event.id AND available_at <= capture.finished_at
                  ORDER BY available_at DESC LIMIT 1
                ) spot ON true
                WHERE capture.event_id = %s AND capture.status IN ('complete', 'partial')
                  AND capture.finished_at > %s AND capture.finished_at <= %s
                ORDER BY capture.finished_at
                """,
                [observation["contract_id"], observation["event_id"], observation["available_at"], now],
            ).fetchall()
        captures: list[QuoteCapture] = []
        for row in rows:
            present = row["bid"] is not None or row["ask"] is not None
            legs = () if not present else (ExecutableLeg(
                str(observation["contract_id"]), "buy", row["bid"], row["ask"], row["bid_size"], row["ask_size"],
            ),)
            captures.append(QuoteCapture(
                observed_at=row["finished_at"], legs=legs,
                session_number=trading_sessions_between(observation["started_at"], row["finished_at"]),
                dte=(observation["expiration"] - row["finished_at"].date()).days,
                invalidated=invalidated(observation["strategy_key"], row["spot_price"], row["event_low"], row["reference_price"]),
                continuity_ok=present,
                reason="same_contract_continuity_missing" if not present else None,
            ))
        return captures

    def _store_outcome(
        self,
        observation: dict[str, Any],
        lifecycle: Any,
        metrics: Any,
        classification: str,
        data_status: str,
        reason: str | None,
        now: datetime,
    ) -> None:
        last = lifecycle.exit_fills[-1] if lifecycle.exit_fills else None
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute(
                """
                UPDATE analysis.option_opportunity_observation
                SET entry_fill_at = %s, entry_fill_price = %s,
                    return_1_session = %s, return_3_session = %s, return_5_session = %s, return_10_session = %s,
                    time_to_2x_sessions = %s, time_to_3x_sessions = %s, time_to_4x_sessions = %s,
                    executable_peak_return = %s, realized_return = %s, mae = %s, giveback = %s,
                    exit_efficiency = %s, exit_fill_at = %s, exit_fill_price = %s,
                    outcome_classification = %s, data_status = %s, miss_reason = %s,
                    measured_through = %s, updated_at = now()
                WHERE id = %s
                """,
                [
                    lifecycle.entry_fill_at, lifecycle.entry_fill_price,
                    metrics.return_1_session, metrics.return_3_session, metrics.return_5_session, metrics.return_10_session,
                    lifecycle.time_to_2x_sessions, lifecycle.time_to_3x_sessions, lifecycle.time_to_4x_sessions,
                    lifecycle.executable_peak_return, metrics.realized_return, lifecycle.mae, lifecycle.giveback,
                    metrics.exit_efficiency, last.observed_at if last else None, last.executable_price if last else None,
                    classification, data_status, reason, now, observation["id"],
                ],
            )


def _liquid(quote: Any) -> bool:
    return bool(
        quote.bid is not None and quote.ask is not None and quote.bid > 0 and quote.ask >= quote.bid
        and (quote.bid_size or 0) > 0 and (quote.ask_size or 0) > 0
    )


def _quote_payload(quote: Any, gate: Any) -> dict[str, Any]:
    return {
        "occ_symbol": quote.occ_symbol, "bid": quote.bid, "ask": quote.ask,
        "bid_size": quote.bid_size, "ask_size": quote.ask_size, "open_interest": quote.open_interest,
        "delta": quote.delta, "volume": quote.volume, "gate": {"eligible": gate.eligible, "blockers": list(gate.blockers)},
    }


def _classification(observation: dict[str, Any], lifecycle: Any, now: datetime) -> tuple[str, str, str | None]:
    if lifecycle.classification == "unmeasurable":
        return "unmeasurable", "continuity_missing", "unmeasurable"
    if lifecycle.classification == "unfilled":
        return "unfilled", "ok", "unfilled"
    if lifecycle.classification == "captured":
        if observation.get("paper_status") in {"exited", "invalidated"}:
            return "captured", "ok", "captured"
        return "missed", "ok", observation.get("miss_reason") or "not_published"
    terminal = trading_sessions_between(observation["started_at"], now) >= 10 or (observation["expiration"] - now.date()).days <= 5
    if not terminal:
        return "observing", "ok", None
    # A terminal clock without enough post-observation captures is a data
    # continuity failure.  It must never turn a missing quote stream into a
    # synthetic loss or a missed-winner label.
    if lifecycle.classification == "observing":
        return "unmeasurable", "continuity_missing", "unmeasurable"
    if observation.get("paper_status") == "unfilled":
        return "unfilled", "ok", "unfilled"
    return "missed", "ok", observation.get("miss_reason") or "not_published"


def _latest_by(rows: Iterable[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        value = row.get(key)
        if value is None:
            continue
        identifier = str(value)
        if identifier not in latest or latest[identifier]["available_at"] < row["available_at"]:
            latest[identifier] = row
    return list(latest.values())


def _three_x(row: dict[str, Any]) -> bool:
    return (number(row.get("executable_peak_return")) or -1.0) >= 2.0


def _event_recall(rows: Iterable[dict[str, Any]], *, target: float) -> float:
    per_event: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        per_event.setdefault(str(row["event_id"]), []).append(row)
    opportunities = [group for group in per_event.values() if any((number(row.get("executable_peak_return")) or -1) >= target - 1 for row in group)]
    if not opportunities:
        return 0.0
    captured = sum(any(row["outcome_classification"] == "captured" for row in group) for group in opportunities)
    return captured / len(opportunities)


def _mean_field(rows: Iterable[dict[str, Any]], field: str) -> float | None:
    values = [number(row.get(field)) for row in rows]
    finite = [value for value in values if value is not None]
    return mean(finite) if finite else None


def _signal_delay_minutes(rows: Iterable[dict[str, Any]]) -> float | None:
    values = [
        (row["signal_at"] - row["available_at"]).total_seconds() / 60.0
        for row in rows
        if row.get("signal_at") is not None and row.get("signal_at") >= row["available_at"]
    ]
    return mean(values) if values else None


def _calibration_gap(predictions: list[Any], rows: list[dict[str, Any]]) -> float:
    if not predictions:
        return 1.0
    expected = mean(max(0.0, min(1.0, float(value))) for value in predictions)
    observed = sum(_three_x(row) for row in rows if row.get("lower_confidence_expectancy") is not None) / len(predictions)
    return abs(expected - observed)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
