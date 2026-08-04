"""PostgreSQL-owned cohort and canary authority for options recovery.

The recovery program deliberately has one current cohort.  Every decision path
uses this repository instead of repeating a hand-written ``objective_version``
predicate, which makes a contaminated historical cohort auditable but unable to
reach ranking, learning, promotion, or paper staging.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from statistics import quantiles
from typing import Any, Iterable

from psycopg.types.json import Jsonb

from investment_panel.core.decision import MARKET_TZ, is_us_market_day, market_session_bounds
from investment_panel.core.options_event_tape import scheduled_event_slots
from investment_panel.database.confirmed_daily_prices import completed_trading_dates
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


CURRENT_OBJECTIVE_VERSION = "short_horizon_convex_v2"
CURRENT_CODE_VERSION = "options-recovery-v5"
RECOVERY_POLICY_VERSION = "options-recovery-canary-v2"
REQUIRED_QUALIFIED_DATES = 5
MAX_QUOTE_AGE_MINUTES = 10.0


@dataclass(frozen=True)
class ProgramEligibility:
    eligible: bool
    blockers: tuple[str, ...]
    cohort: dict[str, Any] | None
    qualified_dates: int
    required_dates: int
    latest_program_session: dict[str, Any] | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "blockers": list(self.blockers),
            "cohort": self.cohort,
            "qualified_dates": self.qualified_dates,
            "required_dates": self.required_dates,
            "latest_program_session": self.latest_program_session,
        }


class RecoveryCohortRepository:
    """Resolve current recovery state and materialize program-wide canary health."""

    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def current(self, connection: Any | None = None) -> dict[str, Any] | None:
        if connection is not None:
            return _current(connection)
        with self.runtime.read(JOB_PROFILE) as read:
            return _current(read)

    def current_id(self) -> str | None:
        cohort = self.current()
        return str(cohort["id"]) if cohort else None

    def current_event_clause(self, *, alias: str = "event") -> str:
        """Central SQL predicate for decision-safe events only."""

        return (
            f"{alias}.cohort_id = (SELECT id FROM analysis.option_recovery_cohort "
            f"WHERE objective_version = '{CURRENT_OBJECTIVE_VERSION}' "
            "AND status IN ('collecting', 'qualified') LIMIT 1) "
            f"AND {alias}.data_quality_status = 'valid' "
            f"AND {alias}.status <> 'invalidated'"
        )

    def event_is_current_valid(self, event_id: str, *, connection: Any | None = None) -> bool:
        if connection is not None:
            return self._event_is_current_valid(connection, event_id)
        with self.runtime.read(JOB_PROFILE) as read:
            return self._event_is_current_valid(read, event_id)

    def _event_is_current_valid(self, connection: Any, event_id: str) -> bool:
        row = connection.execute(
            f"""
            SELECT 1 FROM analysis.option_event event
            WHERE event.id = %s AND {self.current_event_clause()}
            """,
            [event_id],
        ).fetchone()
        return row is not None

    def record_detector_run(
        self,
        *,
        scheduled_at: datetime,
        started_at: datetime,
        finished_at: datetime | None,
        expected_symbols: int,
        received_symbols: int,
        fresh_symbols: int,
        quote_age_p95_minutes: float | None,
        provider_run_id: str | None,
        status: str,
        failure_reasons: Iterable[str] = (),
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if status not in {"succeeded", "failed", "skipped"}:
            raise ValueError("invalid recovery detector status")
        with self.runtime.transaction(JOB_PROFILE) as connection:
            cohort = _current(connection)
            if cohort is None:
                return None
            row = connection.execute(
                """
                INSERT INTO analysis.option_event_detector_run
                    (cohort_id, scheduled_at, started_at, finished_at, expected_symbols,
                     received_symbols, fresh_symbols, quote_age_p95_minutes, provider_run_id,
                     status, failure_reasons, details)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::uuid, %s, %s, %s)
                ON CONFLICT (cohort_id, scheduled_at) DO UPDATE
                SET started_at = EXCLUDED.started_at, finished_at = EXCLUDED.finished_at,
                    expected_symbols = EXCLUDED.expected_symbols,
                    received_symbols = EXCLUDED.received_symbols,
                    fresh_symbols = EXCLUDED.fresh_symbols,
                    quote_age_p95_minutes = EXCLUDED.quote_age_p95_minutes,
                    provider_run_id = EXCLUDED.provider_run_id, status = EXCLUDED.status,
                    failure_reasons = EXCLUDED.failure_reasons, details = EXCLUDED.details
                RETURNING *
                """,
                [
                    cohort["id"], _utc(scheduled_at), _utc(started_at), _utc(finished_at),
                    max(0, int(expected_symbols)), max(0, int(received_symbols)), max(0, int(fresh_symbols)),
                    quote_age_p95_minutes, provider_run_id, status,
                    Jsonb(sorted({str(reason) for reason in failure_reasons if str(reason)})),
                    Jsonb(details or {}),
                ],
            ).fetchone()
        return dict(row)

    def refresh_event_session_quality(
        self,
        event_id: str,
        *,
        trading_date: date | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        reference = _utc(now) or datetime.now(UTC)
        with self.runtime.transaction(JOB_PROFILE) as connection:
            event = connection.execute(
                f"""
                SELECT event.id, event.cohort_id, event.started_at, event.enrolled_at,
                       event.closed_at, event.status
                FROM analysis.option_event event
                WHERE event.id = %s AND {self.current_event_clause()}
                FOR UPDATE
                """,
                [event_id],
            ).fetchone()
            if event is None:
                return None
            day = trading_date or reference.astimezone(MARKET_TZ).date()
            start, end = _session_bounds(day)
            enrolled = event["enrolled_at"] or event["started_at"]
            horizon_start = max(_utc(enrolled) or start, start)
            horizon_end = min(_utc(event["closed_at"]) or end, end, reference)
            slots = scheduled_event_slots(horizon_start, horizon_end) if horizon_end >= horizon_start else []
            captures = [dict(row) for row in connection.execute(
                """
                SELECT scheduled_at, started_at, finished_at, status, expected_contract_count,
                       received_contract_count, completeness, continuity_pct,
                       canonical_continuity_pct, original_continuity_pct, details
                FROM analysis.option_event_capture
                WHERE event_id = %s AND scheduled_at >= %s AND scheduled_at < %s
                ORDER BY scheduled_at
                """,
                [event_id, start, end],
            ).fetchall()]
            by_slot = {row["scheduled_at"]: row for row in captures}
            scheduled = len(slots)
            usable_rows = [
                row for slot, row in by_slot.items()
                if slot in set(slots)
                and row["status"] in {"complete", "partial"}
                and int(row.get("received_contract_count") or 0) > 0
            ]
            complete = sum(
                row["status"] == "complete" and int(row.get("received_contract_count") or 0) > 0
                for row in usable_rows
            )
            expected_contracts = sum(max(0, int(row.get("expected_contract_count") or 0)) for row in usable_rows)
            received_contracts = sum(max(0, int(row.get("received_contract_count") or 0)) for row in usable_rows)
            completeness = received_contracts / expected_contracts if expected_contracts else None
            canonical_values = [
                _number(row.get("canonical_continuity_pct"))
                for row in usable_rows if _number(row.get("canonical_continuity_pct")) is not None
            ]
            original_values = [
                _number(row.get("original_continuity_pct"))
                if _number(row.get("original_continuity_pct")) is not None
                else _number(row.get("continuity_pct"))
                for row in usable_rows
            ]
            original_values = [value for value in original_values if value is not None]
            latencies = [
                (row["finished_at"] - row["scheduled_at"]).total_seconds() / 60.0
                for row in usable_rows
                if row.get("finished_at") is not None and row.get("scheduled_at") is not None
            ]
            defects: list[str] = []
            for slot in slots:
                row = by_slot.get(slot)
                if row is None:
                    defects.append("missing_scheduled_capture")
                elif row["status"] in {"failed", "deferred"}:
                    details = dict(row.get("details") or {})
                    defects.append(str(details.get("reason") or f"capture_{row['status']}"))
            if scheduled and not usable_rows:
                defects.append("no_usable_capture_slots")
            if completeness is None:
                defects.append("null_contract_completeness")
            if not canonical_values:
                defects.append("null_canonical_continuity")
            if not original_values:
                defects.append("null_original_continuity")
            slot_coverage = len(usable_rows) / scheduled if scheduled else None
            canonical = sum(canonical_values) / len(canonical_values) if canonical_values else None
            original = sum(original_values) / len(original_values) if original_values else None
            p95 = _p95(latencies)
            reasons = _event_quality_reasons(
                slot_coverage=slot_coverage,
                completeness=completeness,
                canonical=canonical,
                latency=p95,
                defects=defects,
            )
            row = connection.execute(
                """
                INSERT INTO analysis.option_recovery_event_session_quality
                    (cohort_id, event_id, trading_date, scheduled_slots, usable_slots, complete_slots,
                     contract_completeness, canonical_continuity, original_continuity,
                     capture_p95_latency_minutes, data_defects, qualification_result,
                     qualification_reasons, computed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (event_id, trading_date) DO UPDATE
                SET scheduled_slots = EXCLUDED.scheduled_slots, usable_slots = EXCLUDED.usable_slots,
                    complete_slots = EXCLUDED.complete_slots,
                    contract_completeness = EXCLUDED.contract_completeness,
                    canonical_continuity = EXCLUDED.canonical_continuity,
                    original_continuity = EXCLUDED.original_continuity,
                    capture_p95_latency_minutes = EXCLUDED.capture_p95_latency_minutes,
                    data_defects = EXCLUDED.data_defects,
                    qualification_result = EXCLUDED.qualification_result,
                    qualification_reasons = EXCLUDED.qualification_reasons,
                    computed_at = now()
                RETURNING *
                """,
                [
                    event["cohort_id"], event["id"], day, scheduled, len(usable_rows), int(complete),
                    completeness, canonical, original, p95, Jsonb(sorted(set(defects))),
                    not reasons, Jsonb(reasons),
                ],
            ).fetchone()
        return dict(row)

    def refresh_program_session(
        self,
        *,
        trading_date: date | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        reference = _utc(now) or datetime.now(UTC)
        day = trading_date or reference.astimezone(MARKET_TZ).date()
        # Materialize every active event's fixed-slot denominator before the
        # program roll-up.  A skipped capture must remain visible as a missing
        # obligation rather than disappearing because no capture row exists.
        self.refresh_current_event_session_quality(trading_date=day, now=reference)
        with self.runtime.transaction(JOB_PROFILE) as connection:
            cohort = _current(connection)
            if cohort is None:
                return None
            start, end = _session_bounds(day)
            detector = [dict(row) for row in connection.execute(
                """
                SELECT status, expected_symbols, received_symbols, fresh_symbols,
                       quote_age_p95_minutes, failure_reasons, details
                FROM analysis.option_event_detector_run
                WHERE cohort_id = %s AND scheduled_at >= %s AND scheduled_at < %s
                ORDER BY scheduled_at
                """,
                [cohort["id"], start, end],
            ).fetchall()]
            quality = [dict(row) for row in connection.execute(
                """
                SELECT scheduled_slots, usable_slots, contract_completeness,
                       canonical_continuity, original_continuity, capture_p95_latency_minutes,
                       data_defects
                FROM analysis.option_recovery_event_session_quality
                WHERE cohort_id = %s AND trading_date = %s
                """,
                [cohort["id"], day],
            ).fetchall()]
            active_event_count = int(connection.execute(
                """
                SELECT count(*) AS count
                FROM analysis.option_event event
                WHERE event.cohort_id = %s AND event.data_quality_status = 'valid'
                  AND event.status = 'active' AND event.started_at < %s
                  AND (event.closed_at IS NULL OR event.closed_at >= %s)
                """,
                [cohort["id"], end, start],
            ).fetchone()["count"] or 0)
            expected_runs = len(scheduled_detector_slots(day))
            succeeded = sum(row["status"] == "succeeded" for row in detector)
            provider_expected = sum(int(row.get("expected_symbols") or 0) for row in detector)
            provider_received = sum(int(row.get("received_symbols") or 0) for row in detector)
            trigger_quotes = sum(int((row.get("details") or {}).get("triggering_quote_count") or 0) for row in detector)
            fresh_triggers = sum(int((row.get("details") or {}).get("fresh_triggering_quote_count") or 0) for row in detector)
            quote_ages = [
                _number(row.get("quote_age_p95_minutes"))
                for row in detector if _number(row.get("quote_age_p95_minutes")) is not None
            ]
            event_slots = sum(int(row.get("scheduled_slots") or 0) for row in quality)
            usable_slots = sum(int(row.get("usable_slots") or 0) for row in quality)
            expected_weight = sum(int(row.get("scheduled_slots") or 0) for row in quality)
            completeness_values = [
                (_number(row.get("contract_completeness")), int(row.get("scheduled_slots") or 0))
                for row in quality
            ]
            canonical_values = [
                (_number(row.get("canonical_continuity")), int(row.get("scheduled_slots") or 0))
                for row in quality
            ]
            original_values = [
                (_number(row.get("original_continuity")), int(row.get("scheduled_slots") or 0))
                for row in quality
            ]
            latency_values = [
                _number(row.get("capture_p95_latency_minutes"))
                for row in quality if _number(row.get("capture_p95_latency_minutes")) is not None
            ]
            defects = sorted({
                str(item)
                for row in quality for item in (row.get("data_defects") or []) if str(item)
            })
            critical = sorted({
                reason for row in detector for reason in (row.get("failure_reasons") or [])
                if _is_critical_defect(str(reason))
            } | {reason for reason in defects if _is_critical_defect(reason)})
            completeness = _weighted_mean(completeness_values, expected_weight)
            canonical = _weighted_mean(canonical_values, expected_weight)
            original = _weighted_mean(original_values, expected_weight)
            latency = _p95(latency_values)
            state = {
                "after_start": day > cohort["started_at"].astimezone(MARKET_TZ).date(),
                "active_event_count": active_event_count,
                "detector_coverage": succeeded / expected_runs if expected_runs else None,
                "provider_coverage": provider_received / provider_expected if provider_expected else None,
                "triggering_quote_count": trigger_quotes,
                "fresh_triggering_quote_count": fresh_triggers,
                "slot_coverage": usable_slots / event_slots if event_slots else None,
                "contract_completeness": completeness,
                "canonical_continuity": canonical,
                "capture_p95_latency_minutes": latency,
                "critical_defects": critical,
            }
            reasons = program_qualification_reasons(state)
            row = connection.execute(
                """
                INSERT INTO analysis.option_recovery_program_session
                    (cohort_id, trading_date, active_event_count, detector_scheduled_runs,
                     detector_succeeded_runs, provider_expected_symbols, provider_received_symbols,
                     fresh_event_trigger_quotes, quote_age_p95_minutes, event_scheduled_slots,
                     event_usable_slots, contract_completeness, canonical_continuity,
                     original_continuity, capture_p95_latency_minutes, critical_defects,
                     qualification_result, qualification_reasons, policy_version, computed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, now())
                ON CONFLICT (cohort_id, trading_date) DO UPDATE
                SET active_event_count = EXCLUDED.active_event_count,
                    detector_scheduled_runs = EXCLUDED.detector_scheduled_runs,
                    detector_succeeded_runs = EXCLUDED.detector_succeeded_runs,
                    provider_expected_symbols = EXCLUDED.provider_expected_symbols,
                    provider_received_symbols = EXCLUDED.provider_received_symbols,
                    fresh_event_trigger_quotes = EXCLUDED.fresh_event_trigger_quotes,
                    quote_age_p95_minutes = EXCLUDED.quote_age_p95_minutes,
                    event_scheduled_slots = EXCLUDED.event_scheduled_slots,
                    event_usable_slots = EXCLUDED.event_usable_slots,
                    contract_completeness = EXCLUDED.contract_completeness,
                    canonical_continuity = EXCLUDED.canonical_continuity,
                    original_continuity = EXCLUDED.original_continuity,
                    capture_p95_latency_minutes = EXCLUDED.capture_p95_latency_minutes,
                    critical_defects = EXCLUDED.critical_defects,
                    qualification_result = EXCLUDED.qualification_result,
                    qualification_reasons = EXCLUDED.qualification_reasons,
                    policy_version = EXCLUDED.policy_version, computed_at = now()
                RETURNING *
                """,
                [
                    cohort["id"], day, active_event_count, expected_runs, succeeded,
                    provider_expected, provider_received, fresh_triggers,
                    _p95(quote_ages), event_slots, usable_slots, completeness, canonical,
                    original, latency, Jsonb(critical), not reasons, Jsonb(reasons),
                    RECOVERY_POLICY_VERSION,
                ],
            ).fetchone()
            qualified_dates = int(connection.execute(
                """
                SELECT count(DISTINCT trading_date) AS count
                FROM analysis.option_recovery_program_session
                WHERE cohort_id = %s AND qualification_result
                """,
                [cohort["id"]],
            ).fetchone()["count"] or 0)
            if qualified_dates >= int(cohort["required_qualified_dates"]):
                connection.execute(
                    """
                    UPDATE analysis.option_recovery_cohort
                    SET status = 'qualified', qualified_at = coalesce(qualified_at, now()),
                        blockers = '[]'::jsonb, updated_at = now()
                    WHERE id = %s AND status = 'collecting'
                    """,
                    [cohort["id"]],
                )
        return {**dict(row), "qualified_dates": qualified_dates, "required_dates": int(cohort["required_qualified_dates"])}

    def refresh_current_event_session_quality(
        self,
        *,
        trading_date: date,
        now: datetime | None = None,
    ) -> int:
        """Refresh fixed-slot quality rows for every current valid event."""

        with self.runtime.read(JOB_PROFILE) as connection:
            event_ids = [
                str(row["id"])
                for row in connection.execute(
                    f"""
                    SELECT event.id
                    FROM analysis.option_event event
                    WHERE event.status IN ('active', 'closed')
                      AND {self.current_event_clause()}
                    ORDER BY event.started_at
                    """
                ).fetchall()
            ]
        refreshed = 0
        for event_id in event_ids:
            refreshed += int(
                self.refresh_event_session_quality(
                    event_id, trading_date=trading_date, now=now,
                ) is not None
            )
        return refreshed

    def program_eligibility(
        self,
        *,
        recovery_paper_actions_enabled: bool,
        now: datetime | None = None,
    ) -> ProgramEligibility:
        reference = _utc(now) or datetime.now(UTC)
        # A still-running RTH date has denominator obligations that have not
        # happened yet.  It cannot revoke an already-qualified global canary
        # or prevent an immediately eligible new event from staging.
        completed_dates = completed_trading_dates(reference, count=1)
        latest_complete_date = completed_dates[0] if completed_dates else None
        completed_close = (
            market_session_bounds(latest_complete_date)[1]
            if latest_complete_date is not None else None
        )
        with self.runtime.read(JOB_PROFILE) as connection:
            cohort = _current(connection)
            if cohort is None:
                return ProgramEligibility(False, ("current_recovery_cohort_missing",), None, 0, REQUIRED_QUALIFIED_DATES, None)
            qualified = int(connection.execute(
                """
                SELECT count(DISTINCT trading_date) AS count
                FROM analysis.option_recovery_program_session
                WHERE cohort_id = %s AND qualification_result
                """,
                [cohort["id"]],
            ).fetchone()["count"] or 0)
            latest = connection.execute(
                """
                SELECT * FROM analysis.option_recovery_program_session
                WHERE cohort_id = %s AND trading_date = %s
                  AND computed_at >= %s
                LIMIT 1
                """,
                [cohort["id"], latest_complete_date, completed_close],
            ).fetchone()
        required = int(cohort["required_qualified_dates"])
        blockers: list[str] = []
        if cohort["status"] != "qualified" or qualified < required:
            blockers.append("program_canary_not_qualified")
        if latest is None or not bool(latest["qualification_result"]):
            blockers.append("program_health_not_green")
        if not recovery_paper_actions_enabled:
            blockers.append("recovery_paper_actions_disabled")
        return ProgramEligibility(
            not blockers, tuple(sorted(set(blockers))), cohort, qualified, required,
            dict(latest) if latest else None,
        )

    def health(self, *, recovery_paper_actions_enabled: bool = False) -> dict[str, Any]:
        eligibility = self.program_eligibility(
            recovery_paper_actions_enabled=recovery_paper_actions_enabled,
        )
        cohort = eligibility.cohort
        status = "collecting"
        if cohort and cohort.get("status") == "qualified":
            status = "paper_enabled" if eligibility.eligible else "qualified_but_disabled"
        return {
            "current_cohort": cohort,
            "qualified_dates": eligibility.qualified_dates,
            "required_qualified_dates": eligibility.required_dates,
            "program_state": status,
            "paper_staging": eligibility.as_dict(),
        }


def scheduled_detector_slots(trading_date: date) -> list[datetime]:
    """Every five-minute RTH detector obligation, including that session's close."""

    if not is_us_market_day(trading_date):
        return []
    cursor, close = market_session_bounds(trading_date)
    slots: list[datetime] = []
    while cursor <= close:
        slots.append(cursor.astimezone(UTC))
        cursor += timedelta(minutes=5)
    return slots


def program_qualification_reasons(state: dict[str, Any]) -> list[str]:
    """Pure canary policy, shared by projections and focused tests."""

    reasons: list[str] = []
    if not bool(state.get("after_start")):
        reasons.append("before_or_on_cohort_start_date")
    if int(state.get("active_event_count") or 0) < 1:
        reasons.append("no_valid_active_event")
    if _number(state.get("detector_coverage")) is None or float(state["detector_coverage"]) < 0.95:
        reasons.append("detector_run_coverage_below_95pct")
    if _number(state.get("provider_coverage")) is None or float(state["provider_coverage"]) < 0.95:
        reasons.append("provider_response_coverage_below_95pct")
    triggering = int(state.get("triggering_quote_count") or 0)
    fresh = int(state.get("fresh_triggering_quote_count") or 0)
    if triggering and fresh < triggering:
        reasons.append("stale_event_trigger_quote")
    if _number(state.get("slot_coverage")) is None or float(state["slot_coverage"]) < 0.95:
        reasons.append("event_capture_slot_coverage_below_95pct")
    if _number(state.get("contract_completeness")) is None or float(state["contract_completeness"]) < 0.98:
        reasons.append("contract_completeness_below_98pct")
    if _number(state.get("canonical_continuity")) is None or float(state["canonical_continuity"]) < 0.90:
        reasons.append("canonical_continuity_below_90pct")
    latency = _number(state.get("capture_p95_latency_minutes"))
    if latency is None or latency >= 12.0:
        reasons.append("capture_p95_latency_not_under_12_minutes")
    for defect in state.get("critical_defects") or []:
        reasons.append(f"critical_defect:{defect}")
    return sorted(set(reasons))


def _current(connection: Any) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT * FROM analysis.option_recovery_cohort
        WHERE objective_version = %s AND status IN ('collecting', 'qualified')
        ORDER BY started_at DESC, created_at DESC LIMIT 1
        """,
        [CURRENT_OBJECTIVE_VERSION],
    ).fetchone()
    return dict(row) if row else None


def _session_bounds(trading_date: date) -> tuple[datetime, datetime]:
    start = datetime.combine(trading_date, time.min, MARKET_TZ).astimezone(UTC)
    end = start + timedelta(days=1)
    return start, end


def _event_quality_reasons(
    *,
    slot_coverage: float | None,
    completeness: float | None,
    canonical: float | None,
    latency: float | None,
    defects: Iterable[str],
) -> list[str]:
    reasons: list[str] = []
    if slot_coverage is None or slot_coverage < 0.95:
        reasons.append("event_capture_slot_coverage_below_95pct")
    if completeness is None or completeness < 0.98:
        reasons.append("contract_completeness_below_98pct")
    if canonical is None or canonical < 0.90:
        reasons.append("canonical_continuity_below_90pct")
    if latency is None or latency >= 12.0:
        reasons.append("capture_p95_latency_not_under_12_minutes")
    reasons.extend(f"defect:{item}" for item in defects if _is_critical_defect(str(item)))
    return sorted(set(reasons))


def _is_critical_defect(reason: str) -> bool:
    value = reason.lower()
    return any(token in value for token in (
        "stale", "reference", "lookahead", "future_available", "source_confirmation",
        "provider_unconfirmed", "critical",
    ))


def _weighted_mean(values: Iterable[tuple[float | None, int]], denominator: int) -> float | None:
    pairs = [(value, weight) for value, weight in values if value is not None and weight > 0]
    if denominator <= 0 or not pairs or sum(weight for _, weight in pairs) != denominator:
        return None
    return sum(float(value) * weight for value, weight in pairs) / denominator


def _p95(values: Iterable[float | None]) -> float | None:
    numbers = sorted(float(value) for value in values if value is not None)
    if not numbers:
        return None
    if len(numbers) == 1:
        return numbers[0]
    return quantiles(numbers, n=100, method="inclusive")[94]


def _number(value: Any) -> float | None:
    try:
        result = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return result


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
