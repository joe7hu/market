"""PostgreSQL owner for forward-only sell-off event evidence tapes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Iterable

from psycopg.types.json import Jsonb

from investment_panel.core.decision import MARKET_TZ
from investment_panel.core.options_event_tape import (
    EVENT_MAX_ACTIVE_SYMBOLS,
    EventObservation,
    FrozenContract,
    StripSelection,
    event_priority_components,
    event_reference_price,
    event_severity,
    scheduled_event_slots,
    trading_sessions_between,
    trigger_reason,
)
from investment_panel.database.options_recovery_cohorts import (
    CURRENT_OBJECTIVE_VERSION,
    MAX_QUOTE_AGE_MINUTES,
    RecoveryCohortRepository,
)
from investment_panel.database.options_history_policy import OptionHistoryPolicyRepository
from investment_panel.database.confirmed_daily_prices import completed_trading_dates
from investment_panel.database.option_event_feed import OptionEventFeed
from investment_panel.database.option_event_support import as_datetime as _as_datetime
from investment_panel.database.option_event_support import p95 as _p95
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


EVENT_PROFILE = "event_strip"
class OptionEventRepository(OptionEventFeed):
    """Detect, enroll, and audit two-concurrent-symbol option event strips."""

    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime
        self.policy = OptionHistoryPolicyRepository(runtime)
        self.cohorts = RecoveryCohortRepository(runtime)

    def current_event_symbols(self, *, limit: int = EVENT_MAX_ACTIVE_SYMBOLS) -> list[str]:
        """Return current recovery symbols for the bounded detector scan."""

        safe_limit = max(0, int(limit))
        if safe_limit == 0:
            return []
        with self.runtime.read(JOB_PROFILE) as connection:
            rows = connection.execute(
                f"""
                SELECT regexp_replace(upper(instrument.symbol), '[.]+$', '') AS symbol
                FROM analysis.option_event event
                JOIN catalog.instrument instrument ON instrument.id = event.instrument_id
                WHERE {self.cohorts.current_event_clause(alias='event')}
                  AND event.status IN ('active', 'deferred_capacity')
                ORDER BY (event.status = 'active') DESC,
                         event.event_rank ASC NULLS LAST,
                         event.detected_at ASC,
                         instrument.symbol
                LIMIT %s
                """,
                [safe_limit],
            ).fetchall()
        return [str(row["symbol"]) for row in rows if str(row["symbol"] or "").strip()]

    def detect_events(
        self,
        observations: Iterable[EventObservation] | None = None,
        *,
        now: datetime | None = None,
        require_valid_reference: bool = False,
    ) -> dict[str, Any]:
        reference = now or datetime.now(UTC)
        source = list(observations) if observations is not None else self._detector_observations(reference)
        excluded: list[dict[str, Any]] = []
        candidates: list[EventObservation] = []
        for observation in source:
            reason = trigger_reason(observation)
            if reason is None:
                continue
            invalid_reason = self._invalid_observation_reason(observation, require_valid_reference=require_valid_reference)
            if invalid_reason:
                excluded.append({"symbol": observation.symbol, "reason": invalid_reason})
                continue
            candidates.append(observation)
        candidates.sort(key=self._opportunity_sort_key)
        enrolled: list[dict[str, Any]] = []
        deferred: list[dict[str, Any]] = []
        newly_admitted: list[str] = []
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtext('market-option-event-detection'))")
            cohort = self.cohorts.current(connection)
            if cohort is None:
                return {
                    "status": "failed", "reason": "current_recovery_cohort_missing",
                    "detected": 0, "deferred_capacity": 0, "active_events": [],
                    "deferred_events": [], "closed": 0, "excluded": excluded,
                }
            active = connection.execute(
                """
                SELECT id, instrument_id, status
                FROM analysis.option_event
                WHERE cohort_id = %s AND data_quality_status = 'valid'
                  AND status IN ('active', 'deferred_capacity')
                FOR UPDATE
                """
                , [cohort["id"]]
            ).fetchall()
            open_by_instrument = {int(row["instrument_id"]): dict(row) for row in active}
            available = max(
                0,
                EVENT_MAX_ACTIVE_SYMBOLS - sum(row["status"] == "active" for row in open_by_instrument.values()),
            )
            for observation in candidates:
                if observation.instrument_id is None:
                    continue
                existing = open_by_instrument.get(observation.instrument_id)
                if existing is not None:
                    self._update_open_event(connection, existing["id"], observation)
                    if existing["status"] == "deferred_capacity" and available > 0:
                        connection.execute(
                            """
                            UPDATE analysis.option_event
                            SET status = 'active', enrolled_at = now(), updated_at = now()
                            WHERE id = %s
                            """,
                            [existing["id"]],
                        )
                        existing["status"] = "active"
                        available -= 1
                        newly_admitted.append(str(existing["id"]))
                    item = {"event_id": str(existing["id"]), "symbol": observation.symbol, "status": str(existing["status"])}
                    (enrolled if existing["status"] == "active" else deferred).append(item)
                    continue
                status = "active" if available > 0 else "deferred_capacity"
                if status == "active":
                    available -= 1
                event = connection.execute(
                    """
                    INSERT INTO analysis.option_event
                        (cohort_id, objective_version, instrument_id, detected_at, started_at, reference_price, event_low,
                         trigger_intraday_pct, trigger_one_day_pct, trigger_three_session_pct,
                         severity_score, event_rank, material_evidence_count, status, data_quality_status,
                         trigger_reason, quote_age_minutes, reference_trading_date, reference_source_id,
                         reference_available_at, priority_components, capacity_defer_reason, provenance)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, status
                    """,
                [
                        cohort["id"], CURRENT_OBJECTIVE_VERSION, observation.instrument_id, reference,
                        observation.quote_available_at or observation.observed_at,
                        event_reference_price(observation), observation.price, observation.intraday_pct,
                        observation.one_day_pct, observation.three_session_pct, event_severity(observation),
                        len(enrolled) + len(deferred) + 1, observation.material_evidence_count,
                        status, observation.data_quality_status, trigger_reason(observation), observation.quote_age_minutes,
                        observation.reference_trading_date, observation.reference_source_id,
                        observation.reference_available_at, Jsonb(event_priority_components(observation)),
                        "two_symbol_capacity" if status == "deferred_capacity" else None,
                        Jsonb({
                            "trigger_reason": trigger_reason(observation), "source_id": observation.source_id,
                            "priority_components": event_priority_components(observation),
                            "tie_breakers": {
                                "owned": observation.owned, "watched": observation.watched,
                                "recent_radar": observation.recent_radar, "symbol": observation.symbol,
                            },
                        }),
                    ],
                ).fetchone()
                self._insert_spot(connection, event["id"], observation)
                item = {"event_id": str(event["id"]), "symbol": observation.symbol, "status": str(event["status"])}
                (enrolled if status == "active" else deferred).append(item)
                if status == "active":
                    newly_admitted.append(str(event["id"]))
        for event_id in newly_admitted:
            self.enroll_symbol(event_id)
        fresh_prices = {
            int(observation.instrument_id): observation.price
            for observation in source
            if observation.instrument_id is not None
            and self._invalid_observation_reason(observation, require_valid_reference=require_valid_reference) is None
        }
        closed = self.close_events(
            now=reference,
            current_prices=fresh_prices if require_valid_reference else None,
        )
        return {
            "status": "ok", "detected": len(newly_admitted), "deferred_capacity": len(deferred),
            "active_events": enrolled, "deferred_events": deferred, "closed": closed,
            "eligible_trigger_count": len(candidates), "excluded": excluded,
        }

    def record_capture(
        self,
        event_id: str,
        *,
        stored: dict[str, Any],
        selection: StripSelection,
    ) -> dict[str, Any]:
        slot_at = _as_datetime(stored["slot_at"])
        snapshot_id = int(stored["snapshot_id"])
        generation_id = int(stored["capture_generation_id"])
        with self.runtime.transaction(JOB_PROFILE) as connection:
            self._record_contracts(connection, event_id, snapshot_id, generation_id, selection)
            initial = {
                str(row["contract_key"])
                for row in connection.execute(
                    "SELECT contract_key FROM analysis.option_event_contract WHERE event_id = %s AND is_initial",
                    [event_id],
                ).fetchall()
            }
            seen = {str(row.get("contract_symbol") or row.get("contract_key")) for row in selection.rows}
            original_continuity = len(initial & seen) / len(initial) if initial else None
            expected_slots = set(selection.expected_slot_keys)
            seen_slots = {str(row.get("_event_ladder_slot_key") or "") for row in selection.rows}
            canonical_continuity = len(expected_slots & seen_slots) / len(expected_slots) if expected_slots else None
            status = "complete" if stored.get("capture_state") == "complete" else "partial"
            row = connection.execute(
                """
                INSERT INTO analysis.option_event_capture
                    (event_id, snapshot_id, capture_generation_id, scheduled_at, started_at,
                     finished_at, status, expected_contract_count, received_contract_count,
                     completeness, continuity_pct, canonical_continuity_pct, original_continuity_pct, details)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (event_id, scheduled_at) DO UPDATE
                SET snapshot_id = EXCLUDED.snapshot_id, capture_generation_id = EXCLUDED.capture_generation_id,
                    started_at = EXCLUDED.started_at, finished_at = EXCLUDED.finished_at,
                    status = EXCLUDED.status, expected_contract_count = EXCLUDED.expected_contract_count,
                    received_contract_count = EXCLUDED.received_contract_count,
                    completeness = EXCLUDED.completeness, continuity_pct = EXCLUDED.continuity_pct,
                    canonical_continuity_pct = EXCLUDED.canonical_continuity_pct,
                    original_continuity_pct = EXCLUDED.original_continuity_pct,
                    details = EXCLUDED.details
                RETURNING id
                """,
                [
                    event_id, snapshot_id, generation_id, slot_at,
                    stored.get("capture_started_at"), stored.get("capture_finished_at"), status,
                    int(stored.get("expected_contract_count") or 0), int(stored.get("received_contract_count") or 0),
                    stored.get("completeness"), original_continuity, canonical_continuity, original_continuity,
                    Jsonb({
                        "expected_slot_keys": list(selection.expected_slot_keys),
                        "expected_contract_keys": list(selection.expected_contract_keys),
                        "received_contract_keys": sorted(seen), "replacements": selection.replacements,
                        "provider_errors": list((stored.get("quote_diagnostics") or {}).get("provider_errors") or []),
                    }),
                ],
            ).fetchone()
        quality_at = _as_datetime(stored.get("capture_finished_at") or slot_at)
        trading_date = slot_at.astimezone(MARKET_TZ).date()
        self.cohorts.refresh_event_session_quality(event_id, trading_date=trading_date, now=quality_at)
        self.cohorts.refresh_program_session(trading_date=trading_date, now=quality_at)
        return {
            "event_capture_id": str(row["id"]), "continuity_pct": original_continuity,
            "canonical_continuity_pct": canonical_continuity, "status": status,
        }

    def record_terminal_capture(
        self,
        event_id: str,
        *,
        scheduled_at: datetime,
        status: str,
        reason: str,
    ) -> None:
        """Retain a failed/deferred event slot as explainable coverage evidence."""

        if status not in {"failed", "deferred"}:
            raise ValueError("terminal event capture must be failed or deferred")
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute(
                """
                INSERT INTO analysis.option_event_capture
                    (event_id, scheduled_at, status, details)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (event_id, scheduled_at) DO UPDATE
                SET status = EXCLUDED.status, details = EXCLUDED.details
                """,
                [event_id, scheduled_at, status, Jsonb({"reason": reason})],
            )
        quality_at = _as_datetime(scheduled_at)
        trading_date = quality_at.astimezone(MARKET_TZ).date()
        self.cohorts.refresh_event_session_quality(event_id, trading_date=trading_date, now=quality_at)
        self.cohorts.refresh_program_session(trading_date=trading_date, now=quality_at)

    def capture_health(self, *, now: datetime | None = None) -> dict[str, Any]:
        reference = now or datetime.now(UTC)
        with self.runtime.read(JOB_PROFILE) as connection:
            # A capacity-deferred event is intentionally not a scheduled
            # collection obligation.  It remains visible as a deferred
            # opportunity, but including it here would make the collection
            # coverage gate fail for slots that were never admitted.
            events = [dict(row) for row in connection.execute(
                """
                SELECT id, started_at, enrolled_at, closed_at, status
                FROM analysis.option_event
                WHERE cohort_id = (SELECT id FROM analysis.option_recovery_cohort
                                   WHERE objective_version = %s
                                     AND status IN ('collecting', 'qualified')
                                   ORDER BY started_at DESC LIMIT 1)
                  AND data_quality_status = 'valid'
                  AND status IN ('active', 'closed')
                ORDER BY started_at DESC
                """, [CURRENT_OBJECTIVE_VERSION]
            ).fetchall()]
            captures = [dict(row) for row in connection.execute(
                """
                SELECT capture.event_id, capture.scheduled_at, capture.started_at, capture.finished_at,
                       capture.status, capture.completeness, capture.continuity_pct,
                       capture.canonical_continuity_pct, capture.original_continuity_pct
                FROM analysis.option_event_capture capture
                JOIN analysis.option_event event ON event.id = capture.event_id
                WHERE event.cohort_id = (SELECT id FROM analysis.option_recovery_cohort
                                         WHERE objective_version = %s
                                           AND status IN ('collecting', 'qualified')
                                         ORDER BY started_at DESC LIMIT 1)
                  AND event.data_quality_status = 'valid'
                """, [CURRENT_OBJECTIVE_VERSION]
            ).fetchall()]
            leases = int(connection.execute(
                "SELECT count(*) AS count FROM ops.provider_lease WHERE provider = 'robinhood' AND expires_at > %s",
                [reference],
            ).fetchone()["count"])
        by_event: dict[str, list[dict[str, Any]]] = {}
        for capture in captures:
            by_event.setdefault(str(capture["event_id"]), []).append(capture)
        expected_slots = 0
        covered_slots = 0
        completeness: list[float] = []
        continuity: list[float] = []
        latencies: list[float] = []
        for event in events:
            end = event.get("closed_at") or reference
            event_captures = by_event.get(str(event["id"]), [])
            captured_slots = {row["scheduled_at"] for row in event_captures}
            covered_capture_slots = {
                row["scheduled_at"] for row in event_captures
                if row.get("status") in {"complete", "partial"}
            }
            start = event.get("enrolled_at") or event["started_at"]
            # A detector can admit an event while the already-scheduled
            # capture job is running.  The resulting initial strip belongs to
            # that job's slot even when enrollment commits a few minutes
            # later, so retain it in the coverage denominator.
            if captured_slots:
                start = min(start, min(captured_slots))
            slots = scheduled_event_slots(start, end)
            expected_slots += len(slots)
            # Failed/deferred slots remain in ``slots`` but never count as
            # covered.  A complete capture is covered; a partial one is
            # covered only as a usable partial in the cohort projection.
            covered_slots += sum(slot in covered_capture_slots for slot in slots)
            for row in event_captures:
                if row.get("completeness") is not None:
                    completeness.append(float(row["completeness"]))
                if row.get("continuity_pct") is not None:
                    continuity.append(float(row["continuity_pct"]))
                if row.get("finished_at") and row.get("scheduled_at"):
                    latencies.append((row["finished_at"] - row["scheduled_at"]).total_seconds() / 60.0)
        coverage = covered_slots / expected_slots if expected_slots else None
        p95 = _p95(latencies)
        completeness_value = sum(completeness) / len(completeness) if completeness else None
        original_value = sum(continuity) / len(continuity) if continuity else None
        canonical_values = [
            float(row["canonical_continuity_pct"])
            for rows in by_event.values() for row in rows
            if row.get("canonical_continuity_pct") is not None
        ]
        canonical_value = sum(canonical_values) / len(canonical_values) if canonical_values else None
        return {
            "scheduled_slots": expected_slots,
            "covered_slots": covered_slots,
            "slot_coverage": coverage,
            "contract_completeness": completeness_value,
            "canonical_slot_continuity": canonical_value,
            "same_contract_continuity": original_value,
            "capture_p95_minutes": p95,
            "active_robinhood_leases": leases,
            "active_events": sum(event["status"] == "active" for event in events),
            "gates": {
                "slot_coverage": coverage is not None and coverage >= 0.95,
                "contract_completeness": completeness_value is not None and completeness_value >= 0.98,
                "canonical_slot_continuity": canonical_value is not None and canonical_value >= 0.90,
                "same_contract_continuity": original_value is not None and original_value >= 0.90,
                "capture_p95": p95 is not None and p95 < 12.0,
                "provider_leases": leases <= 2,
            },
        }

    def events(
        self,
        *,
        event_id: str | None = None,
        cohort_id: str | None = None,
        include_invalidated: bool = False,
    ) -> list[dict[str, Any]]:
        filters = ["true"]
        parameters: list[Any] = []
        if cohort_id:
            filters.append("event.cohort_id = %s")
            parameters.append(cohort_id)
        elif not include_invalidated:
            filters.append(self.cohorts.current_event_clause(alias="event"))
        if not include_invalidated:
            filters.append("event.status <> 'invalidated'")
        if event_id:
            filters.append("event.id = %s")
            parameters.append(event_id)
        with self.runtime.read(JOB_PROFILE) as connection:
            rows = connection.execute(
                f"""
                SELECT event.*, instrument.symbol,
                       (SELECT count(*) FROM analysis.option_event_contract contract WHERE contract.event_id = event.id) AS contract_count,
                       (SELECT count(*) FROM analysis.option_event_capture capture WHERE capture.event_id = event.id) AS capture_count
                FROM analysis.option_event event
                JOIN catalog.instrument instrument ON instrument.id = event.instrument_id
                WHERE {' AND '.join(filters)}
                ORDER BY CASE event.status WHEN 'active' THEN 0 WHEN 'deferred_capacity' THEN 1 WHEN 'closed' THEN 2 ELSE 3 END,
                         event.started_at DESC
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def close_events(
        self,
        *,
        now: datetime | None = None,
        current_prices: dict[int, float] | None = None,
    ) -> int:
        """Close only from detector-run prices when a refresh run supplied them.

        Direct maintenance callers retain the legacy fallback for now, while
        the refresh-then-detect workflow never reads a stale raw quote to close
        an event after a provider or ingestion failure.
        """

        reference = now or datetime.now(UTC)
        closed = 0
        with self.runtime.transaction(JOB_PROFILE) as connection:
            quote_join = ""
            quote_columns = "NULL::double precision AS latest_price, NULL::timestamptz AS latest_at"
            parameters: list[Any] = [CURRENT_OBJECTIVE_VERSION]
            if current_prices is None:
                quote_join = """
                LEFT JOIN LATERAL (
                    SELECT price, observed_at FROM raw.confirmed_quote
                    WHERE instrument_id = event.instrument_id
                      AND observed_at <= %s
                      AND available_at <= %s
                    ORDER BY observed_at DESC, available_at DESC LIMIT 1
                ) quote ON true
                """
                quote_columns = "quote.price AS latest_price, quote.observed_at AS latest_at"
                parameters = [reference, reference, CURRENT_OBJECTIVE_VERSION]
            rows = connection.execute(
                f"""
                SELECT event.*, instrument.symbol, {quote_columns},
                       EXISTS (
                         SELECT 1
                         FROM app.paper_order paper
                         WHERE paper.event_id = event.id
                           AND paper.cohort_id = event.cohort_id
                           AND paper.status IN ('staged', 'entered', 'partial_exited')
                       ) AS unresolved_paper_order
                FROM analysis.option_event event
                JOIN catalog.instrument instrument ON instrument.id = event.instrument_id
                {quote_join}
                WHERE event.status IN ('active', 'deferred_capacity')
                  AND event.data_quality_status = 'valid'
                  AND event.cohort_id = (SELECT id FROM analysis.option_recovery_cohort
                                         WHERE objective_version = %s
                                           AND status IN ('collecting', 'qualified')
                                         ORDER BY started_at DESC LIMIT 1)
                FOR UPDATE OF event
                """,
                parameters,
            ).fetchall()
            for raw in rows:
                event = dict(raw)
                sessions = trading_sessions_between(event["started_at"], reference)
                latest = (
                    current_prices.get(int(event["instrument_id"]))
                    if current_prices is not None else event.get("latest_price")
                )
                if current_prices is not None and latest is None:
                    continue
                retraced = bool(
                    latest is not None
                    and float(latest) >= float(event["event_low"]) + 0.80 * (float(event["reference_price"]) - float(event["event_low"]))
                    and sessions >= 2
                    and event.get("last_signal_at") is None
                )
                # A staged order can fill on a later capture, and an entered
                # order owns a fill-relative lifecycle horizon.  Do not turn
                # off its tape merely because the original event aged out.
                if bool(event["unresolved_paper_order"]):
                    continue
                if sessions < 10 and not retraced:
                    continue
                reason = "ten_trading_sessions" if sessions >= 10 else "gap_80pct_retraced_without_signal"
                connection.execute(
                    "UPDATE analysis.option_event SET status = 'closed', closed_at = %s, close_reason = %s, updated_at = now() WHERE id = %s",
                    [reference, reason, event["id"]],
                )
                if event["status"] == "active":
                    connection.execute(
                        """
                        UPDATE app.option_history_policy
                        SET requested_state = 'off', effective_state = 'disabled', paused_at = %s,
                            reason = %s, updated_at = now(), lock_version = lock_version + 1
                        WHERE profile = %s AND event_id = %s
                        """,
                        [reference, f"event closed: {reason}", EVENT_PROFILE, event["id"]],
                    )
                closed += 1
        return closed

    @staticmethod
    def _opportunity_sort_key(observation: EventObservation) -> tuple[float, int, int, int, str]:
        """Score first, then owned/watch/radar priority, then symbol deterministically."""

        return (
            -event_severity(observation),
            -int(bool(observation.owned)),
            -int(bool(observation.watched)),
            -int(bool(observation.recent_radar)),
            observation.symbol.upper(),
        )

    @staticmethod
    def _invalid_observation_reason(
        observation: EventObservation,
        *,
        require_valid_reference: bool,
    ) -> str | None:
        if observation.data_quality_status != "valid":
            return observation.data_quality_status
        if observation.price <= 0:
            return "non_positive_current_quote"
        if not require_valid_reference:
            return None
        if observation.reference_price is None or observation.reference_price <= 0:
            return "missing_or_non_positive_reference"
        if observation.reference_trading_date is None:
            return "reference_trading_date_missing"
        if observation.reference_available_at is None:
            return "reference_availability_missing"
        if observation.reference_available_at > (observation.quote_available_at or observation.observed_at):
            return "future_available_reference"
        # A malformed provider row can carry a future trading date while still
        # claiming an earlier availability timestamp.  The shared daily-price
        # selector protects the normal detector path; retain this boundary for
        # injected/replay observations too.
        completed = completed_trading_dates(observation.quote_available_at or observation.observed_at, count=1)
        if not completed or observation.reference_trading_date > completed[0]:
            return "future_reference_trading_date"
        if observation.quote_age_minutes is None or observation.quote_age_minutes > MAX_QUOTE_AGE_MINUTES:
            return "stale_current_quote"
        return None

    def _event_contracts(self, event_id: str) -> tuple[list[FrozenContract], tuple[str, ...], tuple[str, ...]]:
        with self.runtime.read(JOB_PROFILE) as connection:
            rows = connection.execute(
                """
                SELECT contract_key, option_type, expiration, target_delta, is_initial, retired_at,
                       ladder_slot_key
                FROM analysis.option_event_contract
                WHERE event_id = %s AND retired_at IS NULL
                ORDER BY id
                """,
                [event_id],
            ).fetchall()
            originals = connection.execute(
                """
                SELECT contract_key FROM analysis.option_event_contract
                WHERE event_id = %s AND is_initial ORDER BY id
                """,
                [event_id],
            ).fetchall()
            retired = connection.execute(
                """
                SELECT contract_key FROM analysis.option_event_contract
                WHERE event_id = %s AND retired_at IS NOT NULL ORDER BY id
                """,
                [event_id],
            ).fetchall()
        return (
            [FrozenContract(**dict(row)) for row in rows],
            tuple(str(row["contract_key"]) for row in originals),
            tuple(str(row["contract_key"]) for row in retired),
        )

    def _record_contracts(
        self,
        connection: Any,
        event_id: str,
        snapshot_id: int,
        generation_id: int,
        selection: StripSelection,
    ) -> None:
        contract_rows = connection.execute(
            """
            SELECT contract.id, contract.provider_symbols ->> 'robinhood' AS provider_symbol
            FROM raw.option_quote quote
            JOIN catalog.option_contract contract ON contract.id = quote.contract_id
            WHERE quote.snapshot_id = %s AND quote.capture_generation_id = %s
            """,
            [snapshot_id, generation_id],
        ).fetchall()
        contracts = {
            str(item["provider_symbol"]): int(item["id"])
            for item in (dict(row) for row in contract_rows)
            if item.get("provider_symbol")
        }
        prior = {
            str(row["contract_key"]): dict(row)
            for row in connection.execute(
                "SELECT id, contract_key, retired_at, ladder_slot_key FROM analysis.option_event_contract WHERE event_id = %s FOR UPDATE",
                [event_id],
            ).fetchall()
        }
        for selected in selection.rows:
            key = str(selected.get("contract_symbol") or selected.get("contract_key"))
            replaces_key = selected.get("_event_replaces_contract_key")
            slot = str(selected.get("_event_ladder_slot_key") or "")
            if not slot:
                raise ValueError("event strip selection requires ladder slot key")
            predecessor = prior.get(str(replaces_key)) if replaces_key else None
            # One transaction performs the successor transition: first retire
            # the active member, then insert the immutable successor pointing
            # at that direct predecessor.  The partial unique slot index makes
            # accidental strip growth impossible.
            if predecessor is not None and predecessor.get("retired_at") is None:
                connection.execute(
                    """
                    UPDATE analysis.option_event_contract
                    SET retired_at = now(), retired_reason = 'successor_selected'
                    WHERE id = %s AND retired_at IS NULL
                    """,
                    [predecessor["id"]],
                )
            connection.execute(
                """
                INSERT INTO analysis.option_event_contract
                    (event_id, contract_id, contract_key, ladder_slot_key, option_type, expiration, target_delta,
                     is_initial, replaces_contract_id, initial_capture_generation_id, reason)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (event_id, contract_key) DO UPDATE
                SET contract_id = coalesce(analysis.option_event_contract.contract_id, EXCLUDED.contract_id)
                """,
                [
                    event_id, contracts.get(key), key, slot,
                    str(selected.get("option_type") or selected.get("type") or "").lower(),
                    str(selected.get("expiration") or selected.get("expiry"))[:10],
                    float(selected["_event_target_delta"]), bool(selected.get("_event_initial")),
                    predecessor["id"] if predecessor is not None else None,
                    generation_id if bool(selected.get("_event_initial")) else None,
                    "initial_frozen_ladder" if bool(selected.get("_event_initial")) else "replacement_or_continuity_quote",
                ],
            )

    def _update_open_event(self, connection: Any, event_id: Any, observation: EventObservation) -> None:
        connection.execute(
            """
            UPDATE analysis.option_event
            SET event_low = least(event_low, %s), severity_score = greatest(severity_score, %s),
                material_evidence_count = greatest(material_evidence_count, %s),
                priority_components = %s,
                capacity_defer_reason = CASE WHEN status = 'deferred_capacity' THEN 'two_symbol_capacity' ELSE NULL END,
                updated_at = now()
            WHERE id = %s
            """,
            [
                observation.price, event_severity(observation), observation.material_evidence_count,
                Jsonb(event_priority_components(observation)), event_id,
            ],
        )
        self._insert_spot(connection, event_id, observation)

    @staticmethod
    def _insert_spot(connection: Any, event_id: Any, observation: EventObservation) -> None:
        connection.execute(
            """
            INSERT INTO analysis.option_event_spot
                (event_id, observed_at, available_at, price, source_id, one_day_pct, three_session_pct)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (event_id, observed_at) DO NOTHING
            """,
            [
                event_id, observation.observed_at, observation.quote_available_at or observation.observed_at, observation.price,
                observation.source_id, observation.one_day_pct, observation.three_session_pct,
            ],
        )
