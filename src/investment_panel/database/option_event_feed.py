"""Enrollment, strip selection, and point-in-time detector inputs for events."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable

from investment_panel.core.options_event_tape import EventObservation, StripSelection, select_event_strip, trigger_reason
from investment_panel.database.confirmed_daily_prices import latest_completed_references
from investment_panel.database.option_event_support import number, option_quote_session_cutoffs
from investment_panel.database.options_recovery_cohorts import CURRENT_OBJECTIVE_VERSION, MAX_QUOTE_AGE_MINUTES
from investment_panel.database.runtime import JOB_PROFILE


EVENT_EXPIRY_DAYS = 20


class OptionEventFeed:
    """Mixin for current-cohort event enrollment and confirmed quote inputs."""

    def enroll_symbol(self, event_id: str) -> dict[str, Any]:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            row = connection.execute(
                """
                SELECT event.id, instrument.symbol, event.started_at, event.status
                FROM analysis.option_event event
                JOIN catalog.instrument instrument ON instrument.id = event.instrument_id
                WHERE event.id = %s AND event.data_quality_status = 'valid'
                  AND event.cohort_id = (SELECT id FROM analysis.option_recovery_cohort
                                         WHERE objective_version = %s
                                           AND status IN ('collecting', 'qualified')
                                         ORDER BY started_at DESC LIMIT 1)
                FOR UPDATE
                """,
                [event_id, CURRENT_OBJECTIVE_VERSION],
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
        existing, original_contract_keys, retired_contract_keys = self._event_contracts(event_id)
        selection = select_event_strip(
            captured.get("rows") or [], as_of=as_of.date(), existing=existing,
            original_contract_keys=original_contract_keys,
            retired_contract_keys=retired_contract_keys,
        )
        expected = len(selection.expected_slot_keys)
        received = len(selection.rows)
        normalized = {
            **captured,
            "rows": [dict(row) for row in selection.rows],
            "expected_contract_count": expected,
            "received_contract_count": received,
            "completeness": received / expected if expected else 0.0,
            # A full-chain problem outside the frozen strip must not make the
            # strip appear incomplete. Its original diagnostics remain saved.
            "errors": [] if received else ["event_strip_no_executable_contracts"],
            "event_strip_diagnostics": {
                "expected_slot_keys": list(selection.expected_slot_keys),
                "expected_contract_keys": list(selection.expected_contract_keys),
                "replacements": selection.replacements,
                "provider_errors": list(captured.get("errors") or []),
            },
        }
        return normalized, selection

    def detector_observations(
        self,
        reference: datetime,
        *,
        provider_run_id: str | None = None,
        symbols: Iterable[str] | None = None,
    ) -> tuple[list[EventObservation], dict[str, Any]]:
        """Build only confirmed, run-scoped quote/reference observations."""

        requested_symbols = sorted({
            str(symbol).strip().upper().rstrip(".")
            for symbol in (symbols or ())
            if str(symbol).strip()
        })
        universe = (
            """
            SELECT DISTINCT ON (canonical_symbol) instrument_id
            FROM (
                SELECT instrument.id AS instrument_id,
                       regexp_replace(upper(instrument.symbol), '[.]+$', '') AS canonical_symbol,
                       instrument.updated_at
                FROM catalog.instrument instrument
                WHERE regexp_replace(upper(instrument.symbol), '[.]+$', '') = ANY(%s)
            ) requested
            ORDER BY canonical_symbol, updated_at DESC, instrument_id
            """
            if requested_symbols
            else """
            SELECT instrument_id FROM app.watchlist_item WHERE watch_state <> 'excluded'
            UNION
            SELECT instrument_id FROM app.portfolio_position
            UNION
            SELECT instrument_id FROM analysis.decision
            WHERE kind = 'option' AND as_of >= %s - interval '14 days'
            """
        )
        parameters: list[Any] = [requested_symbols] if requested_symbols else [reference]
        one_session_start, five_session_start = option_quote_session_cutoffs(reference)
        parameters.extend([
            reference.date(), reference.date(), reference, one_session_start,
            reference.date(), reference.date(), reference, five_session_start,
            reference.date(), reference.date(), reference, reference,
            reference, reference, reference, provider_run_id, provider_run_id,
        ])
        with self.runtime.read(JOB_PROFILE) as connection:
            rows = [dict(row) for row in connection.execute(
                f"""
                WITH universe AS (
                    {universe}
                )
                SELECT instrument.id, instrument.symbol, quote.price, quote.observed_at,
                       quote.available_at, quote.source_id,
                       (SELECT count(DISTINCT contract.id)
                        FROM catalog.option_contract contract
                        JOIN raw.option_quote option_quote ON option_quote.contract_id = contract.id
                        WHERE contract.underlying_instrument_id = instrument.id
                          AND contract.expiration BETWEEN (%s::date + 7) AND (%s::date + 45)
                          AND option_quote.available_at <= %s
                          AND option_quote.available_at >= %s) AS quoted_one_session,
                       (SELECT count(DISTINCT contract.id)
                        FROM catalog.option_contract contract
                        JOIN raw.option_quote option_quote ON option_quote.contract_id = contract.id
                        WHERE contract.underlying_instrument_id = instrument.id
                          AND contract.expiration BETWEEN (%s::date + 7) AND (%s::date + 45)
                          AND option_quote.available_at <= %s
                          AND option_quote.available_at >= %s) AS quoted_five_sessions,
                       (SELECT count(*) FROM catalog.option_contract contract
                        WHERE contract.underlying_instrument_id = instrument.id
                          AND contract.expiration BETWEEN (%s::date + 7) AND (%s::date + 45)) AS catalogued_contracts,
                       EXISTS (SELECT 1 FROM app.portfolio_position position
                               WHERE position.instrument_id = instrument.id) AS owned,
                       EXISTS (SELECT 1 FROM app.watchlist_item watch
                               WHERE watch.instrument_id = instrument.id AND watch.watch_state <> 'excluded') AS watched,
                       EXISTS (SELECT 1 FROM analysis.decision decision
                               WHERE decision.instrument_id = instrument.id AND decision.kind = 'option'
                                 AND decision.as_of >= %s - interval '14 days') AS recent_radar,
                       (SELECT count(DISTINCT signal.content_item_id)
                        FROM analysis.source_signal signal
                        JOIN raw.content_item item ON item.id = signal.content_item_id
                        WHERE signal.instrument_id = instrument.id
                          AND signal.observed_at <= %s
                          AND signal.observed_at >= %s - interval '1 day') AS material_count
                FROM universe
                JOIN catalog.instrument instrument ON instrument.id = universe.instrument_id
                LEFT JOIN LATERAL (
                    SELECT price, observed_at, available_at, source_id
                    FROM raw.confirmed_quote quote
                    WHERE quote.instrument_id = instrument.id AND quote.source_id = 'robinhood'
                      AND quote.observed_at <= %s AND quote.available_at <= %s
                      AND (
                        %s::uuid IS NULL OR EXISTS (
                          SELECT 1 FROM raw.quote_confirmation confirmation
                          WHERE confirmation.fact_id = quote.id
                            AND confirmation.fact_available_at = quote.available_at
                            AND confirmation.ingest_run_id = %s::uuid
                        )
                      )
                    ORDER BY observed_at DESC, available_at DESC LIMIT 1
                ) quote ON true
                """,
                parameters,
            ).fetchall()]
            observations: list[EventObservation] = []
            exclusions: list[dict[str, str]] = []
            fresh_quote_ages: list[float] = []
            usable_provider_symbols = 0
            triggering_quote_count = 0
            fresh_triggering_quote_count = 0
            stale_triggering_symbols: list[str] = []
            critical_reference_symbols: list[str] = []
            for row in rows:
                if row.get("source_id") is None:
                    exclusions.append({"symbol": str(row["symbol"]), "reason": "missing_current_quote"})
                    continue
                price = number(row.get("price"))
                if price is None or price <= 0:
                    exclusions.append({"symbol": str(row["symbol"]), "reason": "non_positive_current_quote"})
                    continue
                observed_at = row.get("observed_at")
                available_at = row.get("available_at")
                if not isinstance(observed_at, datetime) or not isinstance(available_at, datetime):
                    exclusions.append({"symbol": str(row["symbol"]), "reason": "provider_unconfirmed"})
                    continue
                usable_provider_symbols += 1
                age = (reference - observed_at).total_seconds() / 60.0
                references = latest_completed_references(connection, int(row["id"]), as_of=reference)
                if references is None:
                    exclusions.append({"symbol": str(row["symbol"]), "reason": "invalid_reference_bar"})
                    if 0 <= age <= MAX_QUOTE_AGE_MINUTES:
                        critical_reference_symbols.append(str(row["symbol"]))
                    continue
                one_day = price / references[0].close - 1.0
                three_sessions = price / references[2].close - 1.0
                provisional = EventObservation(
                    symbol=str(row["symbol"]), observed_at=observed_at, price=price,
                    one_day_pct=one_day, intraday_pct=None, three_session_pct=three_sessions,
                )
                reason = trigger_reason(provisional)
                if reason is not None:
                    triggering_quote_count += 1
                if age < 0 or age > MAX_QUOTE_AGE_MINUTES:
                    exclusions.append({"symbol": str(row["symbol"]), "reason": "stale_quote"})
                    if reason is not None:
                        stale_triggering_symbols.append(str(row["symbol"]))
                    continue
                fresh_quote_ages.append(age)
                if reason is not None:
                    fresh_triggering_quote_count += 1
                selected_reference = references[2] if reason == "three_session_down_10pct" else references[0]
                optionability = (
                    25.0 if int(row.get("quoted_one_session") or 0) > 0 else
                    15.0 if int(row.get("quoted_five_sessions") or 0) > 0 else
                    5.0 if int(row.get("catalogued_contracts") or 0) > 0 else 0.0
                )
                observations.append(EventObservation(
                    symbol=str(row["symbol"]), observed_at=observed_at, price=price,
                    one_day_pct=one_day, intraday_pct=None, three_session_pct=three_sessions,
                    reference_price=selected_reference.close, liquidity_score=optionability,
                    optionability_score=optionability, material_evidence_count=int(row.get("material_count") or 0),
                    instrument_id=int(row["id"]), source_id=str(row.get("source_id") or "robinhood"),
                    quote_available_at=available_at,
                    reference_trading_date=selected_reference.trading_date,
                    reference_source_id=selected_reference.source_id,
                    reference_available_at=selected_reference.available_at,
                    quote_age_minutes=age, data_quality_status="valid",
                    owned=bool(row.get("owned")), watched=bool(row.get("watched")),
                    recent_radar=bool(row.get("recent_radar")),
                ))
        return observations, {
            "exclusions": exclusions,
            # Count only facts usable by the detector.  A provider payload
            # row with a missing timestamp or non-positive price must not
            # inflate the program-wide response-coverage gate.
            "received_symbols": usable_provider_symbols,
            "fresh_symbols": len(fresh_quote_ages), "fresh_quote_ages": fresh_quote_ages,
            "triggering_quote_count": triggering_quote_count,
            "fresh_triggering_quote_count": fresh_triggering_quote_count,
            "stale_triggering_symbols": stale_triggering_symbols,
            "critical_reference_symbols": critical_reference_symbols,
        }

    def _detector_observations(self, reference: datetime) -> list[EventObservation]:
        return self.detector_observations(reference)[0]
