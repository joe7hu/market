"""PostgreSQL owner for forward-only sell-off event evidence tapes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from statistics import quantiles
from typing import Any, Iterable

from psycopg.types.json import Jsonb

from investment_panel.core.options_event_tape import (
    EVENT_MAX_ACTIVE_SYMBOLS,
    EventObservation,
    FrozenContract,
    StripSelection,
    event_reference_price,
    event_severity,
    scheduled_event_slots,
    select_event_strip,
    trading_sessions_between,
    trigger_reason,
)
from investment_panel.database.options_history_policy import OptionHistoryPolicyRepository
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


EVENT_PROFILE = "event_strip"
EVENT_EXPIRY_DAYS = 20


class OptionEventRepository:
    """Detect, enroll, and audit two-concurrent-symbol option event strips."""

    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime
        self.policy = OptionHistoryPolicyRepository(runtime)

    def detect_events(
        self,
        observations: Iterable[EventObservation] | None = None,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        reference = now or datetime.now(UTC)
        source = list(observations) if observations is not None else self._detector_observations(reference)
        candidates = [observation for observation in source if trigger_reason(observation) is not None]
        candidates.sort(key=event_severity, reverse=True)
        enrolled: list[dict[str, Any]] = []
        deferred: list[dict[str, Any]] = []
        newly_admitted: list[str] = []
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtext('market-option-event-detection'))")
            active = connection.execute(
                """
                SELECT id, instrument_id, status
                FROM analysis.option_event
                WHERE status IN ('active', 'deferred_capacity')
                FOR UPDATE
                """
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
                        (instrument_id, detected_at, started_at, reference_price, event_low,
                         trigger_intraday_pct, trigger_one_day_pct, trigger_three_session_pct,
                         severity_score, event_rank, material_evidence_count, status, provenance)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, status
                    """,
                    [
                        observation.instrument_id, reference, observation.observed_at,
                        event_reference_price(observation), observation.price, observation.intraday_pct,
                        observation.one_day_pct, observation.three_session_pct, event_severity(observation),
                        len(enrolled) + len(deferred) + 1, observation.material_evidence_count,
                        status,
                        Jsonb({
                            "trigger_reason": trigger_reason(observation), "source_id": observation.source_id,
                            "liquidity_score": observation.liquidity_score,
                            "relevance_score": observation.relevance_score,
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
        closed = self.close_events(now=reference)
        return {
            "status": "ok", "detected": len(newly_admitted), "deferred_capacity": len(deferred),
            "active_events": enrolled, "deferred_events": deferred, "closed": closed,
        }

    def enroll_symbol(self, event_id: str) -> dict[str, Any]:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            row = connection.execute(
                """
                SELECT event.id, instrument.symbol, event.started_at, event.status
                FROM analysis.option_event event
                JOIN catalog.instrument instrument ON instrument.id = event.instrument_id
                WHERE event.id = %s FOR UPDATE
                """,
                [event_id],
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown option event: {event_id}")
            if row["status"] != "active":
                return {"event_id": str(row["id"]), "symbol": row["symbol"], "status": row["status"]}
            expires_at = row["started_at"] + timedelta(days=EVENT_EXPIRY_DAYS)
            connection.execute(
                "UPDATE analysis.option_event SET enrolled_at = coalesce(enrolled_at, now()), updated_at = now() WHERE id = %s",
                [row["id"]],
            )
        policy = self.policy.enroll_event_profile(
            event_id=row["id"], symbol=str(row["symbol"]), expires_at=expires_at,
            reason=f"selloff event {row['id']} admitted to two-symbol event-strip capacity",
        )
        return {"event_id": str(row["id"]), "symbol": str(row["symbol"]), "status": "active", "policy": policy}

    def filter_event_strip(
        self,
        event_id: str,
        captured: dict[str, Any],
        *,
        as_of: datetime,
    ) -> tuple[dict[str, Any], StripSelection]:
        existing = self._event_contracts(event_id)
        selection = select_event_strip(captured.get("rows") or [], as_of=as_of.date(), existing=existing)
        expected = len(selection.expected_contract_keys)
        received = len(selection.rows)
        normalized = {
            **captured,
            "rows": [dict(row) for row in selection.rows],
            "expected_contract_count": expected,
            "received_contract_count": received,
            "completeness": received / expected if expected else 0.0,
            # A full-chain problem outside the frozen strip must not make the
            # strip appear incomplete.  Its original diagnostics remain saved.
            "errors": [] if received else ["event_strip_no_executable_contracts"],
            "event_strip_diagnostics": {
                "expected_contract_keys": list(selection.expected_contract_keys),
                "replacements": selection.replacements,
                "provider_errors": list(captured.get("errors") or []),
            },
        }
        return normalized, selection

    def record_capture(
        self,
        event_id: str,
        *,
        stored: dict[str, Any],
        selection: StripSelection,
    ) -> dict[str, Any]:
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
            continuity = len(initial & seen) / len(initial) if initial else 1.0
            status = "complete" if stored.get("capture_state") == "complete" else "partial"
            row = connection.execute(
                """
                INSERT INTO analysis.option_event_capture
                    (event_id, snapshot_id, capture_generation_id, scheduled_at, started_at,
                     finished_at, status, expected_contract_count, received_contract_count,
                     completeness, continuity_pct, details)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (event_id, scheduled_at) DO UPDATE
                SET snapshot_id = EXCLUDED.snapshot_id, capture_generation_id = EXCLUDED.capture_generation_id,
                    started_at = EXCLUDED.started_at, finished_at = EXCLUDED.finished_at,
                    status = EXCLUDED.status, expected_contract_count = EXCLUDED.expected_contract_count,
                    received_contract_count = EXCLUDED.received_contract_count,
                    completeness = EXCLUDED.completeness, continuity_pct = EXCLUDED.continuity_pct,
                    details = EXCLUDED.details
                RETURNING id
                """,
                [
                    event_id, snapshot_id, generation_id, stored["slot_at"],
                    stored.get("capture_started_at"), stored.get("capture_finished_at"), status,
                    int(stored.get("expected_contract_count") or 0), int(stored.get("received_contract_count") or 0),
                    stored.get("completeness"), continuity,
                    Jsonb({
                        "expected_contract_keys": list(selection.expected_contract_keys),
                        "received_contract_keys": sorted(seen), "replacements": selection.replacements,
                        "provider_errors": list((stored.get("quote_diagnostics") or {}).get("provider_errors") or []),
                    }),
                ],
            ).fetchone()
        return {"event_capture_id": str(row["id"]), "continuity_pct": continuity, "status": status}

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

    def capture_health(self, *, now: datetime | None = None) -> dict[str, Any]:
        reference = now or datetime.now(UTC)
        with self.runtime.read(JOB_PROFILE) as connection:
            events = [dict(row) for row in connection.execute(
                "SELECT id, started_at, enrolled_at, closed_at, status FROM analysis.option_event ORDER BY started_at DESC"
            ).fetchall()]
            captures = [dict(row) for row in connection.execute(
                "SELECT event_id, scheduled_at, started_at, finished_at, status, completeness, continuity_pct "
                "FROM analysis.option_event_capture"
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
            start = event.get("enrolled_at") or event["started_at"]
            slots = scheduled_event_slots(start, end)
            expected_slots += len(slots)
            captured_slots = {row["scheduled_at"] for row in by_event.get(str(event["id"]), [])}
            covered_slots += sum(slot in captured_slots for slot in slots)
            for row in by_event.get(str(event["id"]), []):
                if row.get("completeness") is not None:
                    completeness.append(float(row["completeness"]))
                if row.get("continuity_pct") is not None:
                    continuity.append(float(row["continuity_pct"]))
                if row.get("finished_at") and row.get("scheduled_at"):
                    latencies.append((row["finished_at"] - row["scheduled_at"]).total_seconds() / 60.0)
        coverage = covered_slots / expected_slots if expected_slots else 1.0
        p95 = _p95(latencies)
        return {
            "scheduled_slots": expected_slots,
            "covered_slots": covered_slots,
            "slot_coverage": coverage,
            "contract_completeness": sum(completeness) / len(completeness) if completeness else 1.0,
            "same_contract_continuity": sum(continuity) / len(continuity) if continuity else 1.0,
            "capture_p95_minutes": p95,
            "active_robinhood_leases": leases,
            "active_events": sum(event["status"] == "active" for event in events),
            "gates": {
                "slot_coverage": coverage >= 0.95,
                "contract_completeness": (sum(completeness) / len(completeness) if completeness else 1.0) >= 0.98,
                "same_contract_continuity": (sum(continuity) / len(continuity) if continuity else 1.0) >= 0.90,
                "capture_p95": p95 is None or p95 < 12.0,
                "provider_leases": leases <= 2,
            },
        }

    def events(self, *, event_id: str | None = None) -> list[dict[str, Any]]:
        filters = ["true"]
        parameters: list[Any] = []
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
                ORDER BY CASE event.status WHEN 'active' THEN 0 WHEN 'deferred_capacity' THEN 1 ELSE 2 END,
                         event.started_at DESC
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def close_events(self, *, now: datetime | None = None) -> int:
        reference = now or datetime.now(UTC)
        closed = 0
        with self.runtime.transaction(JOB_PROFILE) as connection:
            rows = connection.execute(
                """
                SELECT event.*, instrument.symbol,
                       quote.price AS latest_price, quote.observed_at AS latest_at
                FROM analysis.option_event event
                JOIN catalog.instrument instrument ON instrument.id = event.instrument_id
                LEFT JOIN LATERAL (
                    SELECT price, observed_at FROM raw.quote
                    WHERE instrument_id = event.instrument_id AND observed_at <= %s
                    ORDER BY observed_at DESC LIMIT 1
                ) quote ON true
                WHERE event.status IN ('active', 'deferred_capacity') FOR UPDATE OF event
                """,
                [reference],
            ).fetchall()
            for raw in rows:
                event = dict(raw)
                sessions = trading_sessions_between(event["started_at"], reference)
                latest = event.get("latest_price")
                retraced = bool(
                    latest is not None
                    and float(latest) >= float(event["event_low"]) + 0.80 * (float(event["reference_price"]) - float(event["event_low"]))
                    and sessions >= 2
                    and event.get("last_signal_at") is None
                )
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

    def _event_contracts(self, event_id: str) -> list[FrozenContract]:
        with self.runtime.read(JOB_PROFILE) as connection:
            rows = connection.execute(
                """
                SELECT contract_key, option_type, expiration, target_delta, is_initial, retired_at
                FROM analysis.option_event_contract WHERE event_id = %s ORDER BY id
                """,
                [event_id],
            ).fetchall()
        return [FrozenContract(**dict(row)) for row in rows]

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
            str(row["contract_key"]): int(row["id"])
            for row in connection.execute(
                "SELECT id, contract_key FROM analysis.option_event_contract WHERE event_id = %s",
                [event_id],
            ).fetchall()
        }
        for selected in selection.rows:
            key = str(selected.get("contract_symbol") or selected.get("contract_key"))
            replaces_key = selected.get("_event_replaces_contract_key")
            connection.execute(
                """
                INSERT INTO analysis.option_event_contract
                    (event_id, contract_id, contract_key, option_type, expiration, target_delta,
                     is_initial, replaces_contract_id, initial_capture_generation_id, reason)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (event_id, contract_key) DO UPDATE
                SET contract_id = coalesce(analysis.option_event_contract.contract_id, EXCLUDED.contract_id)
                """,
                [
                    event_id, contracts.get(key), key,
                    str(selected.get("option_type") or selected.get("type") or "").lower(),
                    str(selected.get("expiration") or selected.get("expiry"))[:10],
                    float(selected["_event_target_delta"]), bool(selected.get("_event_initial")),
                    prior.get(str(replaces_key)) if replaces_key else None,
                    generation_id if bool(selected.get("_event_initial")) else None,
                    "initial_frozen_ladder" if bool(selected.get("_event_initial")) else "replacement_or_continuity_quote",
                ],
            )

    def _update_open_event(self, connection: Any, event_id: Any, observation: EventObservation) -> None:
        connection.execute(
            """
            UPDATE analysis.option_event
            SET event_low = least(event_low, %s), severity_score = greatest(severity_score, %s),
                material_evidence_count = greatest(material_evidence_count, %s), updated_at = now()
            WHERE id = %s
            """,
            [observation.price, event_severity(observation), observation.material_evidence_count, event_id],
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
                event_id, observation.observed_at, observation.observed_at, observation.price,
                observation.source_id, observation.one_day_pct, observation.three_session_pct,
            ],
        )

    def _detector_observations(self, reference: datetime) -> list[EventObservation]:
        with self.runtime.read(JOB_PROFILE) as connection:
            rows = connection.execute(
                """
                WITH universe AS (
                    SELECT instrument_id FROM app.watchlist_item WHERE watch_state <> 'excluded'
                    UNION
                    SELECT instrument_id FROM app.portfolio_position
                    UNION
                    SELECT instrument_id FROM analysis.decision
                    WHERE kind = 'option' AND as_of >= %s - interval '14 days'
                )
                SELECT instrument.id, instrument.symbol, quote.price, quote.change_pct, quote.observed_at,
                       quote.source_id, previous.close AS three_sessions_ago,
                       (SELECT count(*) FROM raw.option_quote option_quote
                        JOIN catalog.option_contract contract ON contract.id = option_quote.contract_id
                        WHERE contract.underlying_instrument_id = instrument.id
                          AND option_quote.observed_at >= %s - interval '1 day') AS option_liquidity,
                       ((watch.instrument_id IS NOT NULL)::int + (position.instrument_id IS NOT NULL)::int * 2) AS relevance,
                       (SELECT count(*) FROM analysis.source_signal signal
                        WHERE signal.instrument_id = instrument.id AND signal.observed_at >= %s - interval '1 day') AS material_count
                FROM universe
                JOIN catalog.instrument instrument ON instrument.id = universe.instrument_id
                JOIN LATERAL (
                    SELECT price, change_pct, observed_at, source_id FROM raw.quote
                    WHERE instrument_id = instrument.id AND observed_at <= %s
                    ORDER BY observed_at DESC LIMIT 1
                ) quote ON true
                LEFT JOIN LATERAL (
                    SELECT close FROM raw.price_bar
                    WHERE instrument_id = instrument.id AND interval = '1d' AND observed_at <= %s
                    ORDER BY trading_date DESC OFFSET 2 LIMIT 1
                ) previous ON true
                LEFT JOIN app.watchlist_item watch ON watch.instrument_id = instrument.id
                LEFT JOIN app.portfolio_position position ON position.instrument_id = instrument.id
                """,
                [reference, reference, reference, reference, reference],
            ).fetchall()
        observations: list[EventObservation] = []
        for raw in rows:
            row = dict(raw)
            price = _float(row.get("price"))
            if price is None or price <= 0:
                continue
            previous = _float(row.get("three_sessions_ago"))
            observations.append(EventObservation(
                symbol=str(row["symbol"]), observed_at=row["observed_at"], price=price,
                # ``raw.quote.change_pct`` is stored in percentage points;
                # event rules use decimal returns (for example -0.06).
                one_day_pct=_fraction(row.get("change_pct")), intraday_pct=_fraction(row.get("change_pct")),
                three_session_pct=(price / previous - 1.0) if previous and previous > 0 else None,
                reference_price=previous, liquidity_score=float(row.get("option_liquidity") or 0),
                relevance_score=float(row.get("relevance") or 0),
                material_evidence_count=int(row.get("material_count") or 0),
                instrument_id=int(row["id"]), source_id=str(row.get("source_id") or ""),
            ))
        return observations


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return quantiles(values, n=100, method="inclusive")[94]


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _fraction(value: Any) -> float | None:
    numeric = _float(value)
    return numeric / 100.0 if numeric is not None else None
