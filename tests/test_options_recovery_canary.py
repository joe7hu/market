from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from investment_panel.database.instruments import reconcile_instrument
from investment_panel.database.option_events import OptionEventRepository
from investment_panel.database.options_recovery_cohorts import (
    RecoveryCohortRepository,
    program_qualification_reasons,
    scheduled_detector_slots,
)
from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.jobs.detect_option_events import (
    detector_collection_deadline,
    detector_symbol_limit,
    detector_universe,
    post_ingestion_reference,
    detector_slot,
)


def _insert_green_detector_runs(connection, cohort_id: object, trading_date: date) -> None:
    slots = scheduled_detector_slots(trading_date)
    assert slots
    connection.execute(
        """
        INSERT INTO analysis.option_event_detector_run
            (cohort_id, scheduled_at, started_at, finished_at, expected_symbols,
             received_symbols, fresh_symbols, quote_age_p95_minutes, status, details)
        SELECT %s, slot, slot, slot, 1, 1, 1, 1.0, 'succeeded',
               '{"triggering_quote_count": 0, "fresh_triggering_quote_count": 0}'::jsonb
        FROM generate_series(%s::timestamptz, %s::timestamptz, interval '5 minutes') AS slot
        """,
        [cohort_id, slots[0], slots[-1]],
    )


def test_detector_uses_a_post_ingestion_cutoff_for_newly_confirmed_quotes() -> None:
    scheduled = datetime(2026, 8, 3, 19, 55, tzinfo=UTC)
    completed = scheduled + timedelta(seconds=4)

    assert post_ingestion_reference(scheduled, completed) == completed
    assert post_ingestion_reference(completed, scheduled) == completed


def test_detector_collection_deadline_reserves_the_post_collection_tail(monkeypatch) -> None:
    monkeypatch.setattr("investment_panel.jobs.detect_option_events.time.monotonic", lambda: 100.0)
    monkeypatch.setattr("investment_panel.jobs.detect_option_events.job_timeout_seconds", lambda _: 90)

    assert detector_collection_deadline() == 175.0


def test_detector_universe_is_bounded_and_keeps_current_events_first(monkeypatch) -> None:
    class Ingestion:
        def __init__(self) -> None:
            self.configured: list[dict[str, str]] | None = None
            self.limit: int | None = None

        def option_universe(self, configured, *, limit):
            self.configured = list(configured)
            self.limit = limit
            return ["MSFT", "NVDA", "AAPL"][:limit]

    class Events:
        def current_event_symbols(self, *, limit):
            assert limit == 3
            return ["TSLA", "SNDK"]

    ingestion = Ingestion()
    symbols, active = detector_universe(
        ingestion,  # type: ignore[arg-type]
        Events(),  # type: ignore[arg-type]
        configured=[{"symbol": "NVDA"}],
        limit=3,
    )

    assert active == ["TSLA", "SNDK"]
    assert symbols == ["TSLA", "SNDK", "MSFT"]
    assert ingestion.configured == [{"symbol": "TSLA"}, {"symbol": "SNDK"}, {"symbol": "NVDA"}]
    assert ingestion.limit == 3
    monkeypatch.setenv("MARKET_ROBINHOOD_MAX_SYMBOLS", "2")
    assert detector_symbol_limit(80) == 2


def test_detector_denominator_stops_at_an_early_market_close() -> None:
    early_close = date(2026, 11, 27)
    slots = scheduled_detector_slots(early_close)

    assert len(slots) == 43
    assert slots[0] == datetime(2026, 11, 27, 14, 30, tzinfo=UTC)
    assert slots[-1] == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)
    assert detector_slot(datetime(2026, 11, 27, 18, 0, tzinfo=UTC)) == slots[-1]
    assert detector_slot(datetime(2026, 11, 27, 18, 1, tzinfo=UTC)) is None


def test_program_canary_qualifies_five_distinct_dates_and_requires_current_health_and_switch(
    migrated_postgres_dsn: str,
    monkeypatch,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        repository = RecoveryCohortRepository(runtime)
        start = datetime(2026, 7, 20, 14, tzinfo=UTC)
        with runtime.transaction() as connection:
            cohort = repository.current(connection)
            assert cohort is not None
            connection.execute(
                "UPDATE analysis.option_recovery_cohort SET started_at = %s WHERE id = %s",
                [start, cohort["id"]],
            )
            instrument_id = reconcile_instrument(connection, "CANARY", asset_class="equity", category="test")
            event_id = connection.execute(
                """
                INSERT INTO analysis.option_event
                    (instrument_id, detected_at, started_at, enrolled_at,
                     reference_price, event_low, severity_score, status)
                VALUES (%s, %s, %s, %s, 100, 90, 25, 'active')
                RETURNING id
                """,
                [instrument_id, start, start, start],
            ).fetchone()["id"]
            cohort = repository.current(connection)
            assert cohort is not None
            for day in (date(2026, 7, 27), date(2026, 7, 28), date(2026, 7, 29), date(2026, 7, 30), date(2026, 7, 31)):
                _insert_green_detector_runs(connection, cohort["id"], day)
                connection.execute(
                    """
                    INSERT INTO analysis.option_recovery_event_session_quality
                        (cohort_id, event_id, trading_date, scheduled_slots, usable_slots,
                         complete_slots, contract_completeness, canonical_continuity,
                         original_continuity, capture_p95_latency_minutes,
                         qualification_result, qualification_reasons)
                    VALUES (%s, %s, %s, 1, 1, 1, 1.0, 1.0, 1.0, 1.0, true, '[]'::jsonb)
                    """,
                    [cohort["id"], event_id, day],
                )

        # This test supplies complete persisted quality projections.  The
        # event-slot collector itself is separately tested; suppressing its
        # live refresh here isolates the five-date state transition.
        monkeypatch.setattr(repository, "refresh_current_event_session_quality", lambda **_: 0)
        for day in (date(2026, 7, 27), date(2026, 7, 28), date(2026, 7, 29), date(2026, 7, 30), date(2026, 7, 31)):
            repository.refresh_program_session(
                trading_date=day,
                now=datetime.combine(day, time(21), tzinfo=UTC),
            )

        # Recomputing a date is idempotent: it does not fabricate a sixth
        # canary date or change the cohort identity.
        duplicate = repository.refresh_program_session(
            trading_date=date(2026, 7, 31),
            now=datetime(2026, 7, 31, 21, 5, tzinfo=UTC),
        )
        assert duplicate is not None and duplicate["qualified_dates"] == 5
        qualified = repository.current()
        assert qualified is not None and qualified["status"] == "qualified"

        now = datetime(2026, 7, 31, 21, tzinfo=UTC)
        disabled = repository.program_eligibility(recovery_paper_actions_enabled=False, now=now)
        enabled = repository.program_eligibility(recovery_paper_actions_enabled=True, now=now)
        assert disabled.eligible is False
        assert "recovery_paper_actions_disabled" in disabled.blockers
        assert enabled.eligible is True

        # A date can satisfy its 95% detector ratio before the final 16:00
        # slot.  It becomes staging-safe only after a projection is computed
        # at or after the cash close.
        with runtime.transaction() as connection:
            connection.execute(
                """
                UPDATE analysis.option_recovery_program_session
                SET computed_at = %s
                WHERE cohort_id = %s AND trading_date = %s
                """,
                [datetime(2026, 7, 31, 19, 59, tzinfo=UTC), qualified["id"], date(2026, 7, 31)],
            )
        pre_close_projection = repository.program_eligibility(recovery_paper_actions_enabled=True, now=now)
        assert pre_close_projection.eligible is False
        assert "program_health_not_green" in pre_close_projection.blockers
        with runtime.transaction() as connection:
            connection.execute(
                """
                UPDATE analysis.option_recovery_program_session
                SET computed_at = %s
                WHERE cohort_id = %s AND trading_date = %s
                """,
                [now, qualified["id"], date(2026, 7, 31)],
            )
        assert repository.program_eligibility(recovery_paper_actions_enabled=True, now=now).eligible is True

        # A previously green Friday cannot be carried forward when its actual
        # current completed-session projection becomes unhealthy.
        with runtime.transaction() as connection:
            connection.execute(
                """
                UPDATE analysis.option_recovery_program_session
                SET qualification_result = false,
                    qualification_reasons = '["synthetic_failed_current_date"]'::jsonb
                WHERE cohort_id = %s AND trading_date = %s
                """,
                [qualified["id"], date(2026, 7, 31)],
            )
        unhealthy = repository.program_eligibility(recovery_paper_actions_enabled=True, now=now)
        assert unhealthy.eligible is False
        assert "program_health_not_green" in unhealthy.blockers
    finally:
        runtime.close()


def test_program_qualification_never_defaults_missing_evidence_to_green() -> None:
    reasons = program_qualification_reasons({
        "after_start": True,
        "active_event_count": 1,
        "detector_coverage": 1.0,
        "provider_coverage": 1.0,
        "triggering_quote_count": 1,
        "fresh_triggering_quote_count": 1,
        "slot_coverage": None,
        "contract_completeness": None,
        "canonical_continuity": None,
        "capture_p95_latency_minutes": None,
        "critical_defects": [],
    })
    assert {
        "event_capture_slot_coverage_below_95pct",
        "contract_completeness_below_98pct",
        "canonical_continuity_below_90pct",
        "capture_p95_latency_not_under_12_minutes",
    }.issubset(reasons)


def test_detector_excludes_stale_and_missing_production_shape_without_creating_events(
    migrated_postgres_dsn: str,
) -> None:
    """AAOI/SNDK-style bad inputs remain explicit exclusions, not zero returns."""

    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        reference = datetime(2026, 8, 3, 19, tzinfo=UTC)
        with runtime.transaction() as connection:
            aaoI_id = reconcile_instrument(connection, "AAOI", asset_class="equity", category="test")
            reconcile_instrument(connection, "SNDK", asset_class="equity", category="test")
            connection.execute(
                """
                INSERT INTO ingest.source (id, name, family, kind)
                VALUES ('polygon', 'Polygon', 'market_data', 'daily_bars'),
                       ('robinhood', 'Robinhood', 'broker', 'quote')
                """
            )
            daily_run = connection.execute(
                """
                INSERT INTO ingest.run (source_id, capability, started_at, finished_at, status)
                VALUES ('polygon', 'daily_bars', %s, %s, 'succeeded') RETURNING id
                """,
                [reference - timedelta(days=6), reference - timedelta(minutes=30)],
            ).fetchone()["id"]
            for trading_date in (date(2026, 7, 29), date(2026, 7, 30), date(2026, 7, 31)):
                observed_at = datetime.combine(trading_date, time(20), tzinfo=UTC)
                bar = connection.execute(
                    """
                    INSERT INTO raw.price_bar
                        (instrument_id, source_id, ingest_run_id, interval, trading_date,
                         observed_at, close, available_at)
                    VALUES (%s, 'polygon', %s, '1d', %s, %s, 25, %s)
                    RETURNING id, available_at
                    """,
                    [aaoI_id, daily_run, trading_date, observed_at, reference - timedelta(days=1)],
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO raw.price_bar_confirmation (fact_id, fact_available_at, ingest_run_id)
                    VALUES (%s, %s, %s)
                    """,
                    [bar["id"], bar["available_at"], daily_run],
                )
            quote_run = connection.execute(
                """
                INSERT INTO ingest.run (source_id, capability, started_at, finished_at, status)
                VALUES ('robinhood', 'equity_quotes', %s, %s, 'succeeded') RETURNING id
                """,
                [reference - timedelta(minutes=20), reference - timedelta(minutes=11)],
            ).fetchone()["id"]
            quote = connection.execute(
                """
                INSERT INTO raw.quote
                    (instrument_id, source_id, ingest_run_id, observed_at, price, available_at)
                VALUES (%s, 'robinhood', %s, %s, 20, %s) RETURNING id, available_at
                """,
                [aaoI_id, quote_run, reference - timedelta(minutes=11), reference - timedelta(minutes=11)],
            ).fetchone()
            connection.execute(
                """
                INSERT INTO raw.quote_confirmation (fact_id, fact_available_at, ingest_run_id)
                VALUES (%s, %s, %s)
                """,
                [quote["id"], quote["available_at"], quote_run],
            )

        events = OptionEventRepository(runtime)
        observations, report = events.detector_observations(
            reference,
            symbols=["AAOI", "SNDK"],
        )
        detected = events.detect_events(observations, now=reference, require_valid_reference=True)

        reasons = {(row["symbol"], row["reason"]) for row in report["exclusions"]}
        assert observations == []
        assert reasons >= {("AAOI", "stale_quote"), ("SNDK", "missing_current_quote")}
        assert report["triggering_quote_count"] == 1
        assert report["fresh_triggering_quote_count"] == 0
        assert report["stale_triggering_symbols"] == ["AAOI"]
        assert detected["detected"] == 0
        assert detected["active_events"] == []
    finally:
        runtime.close()


def test_detector_accepts_an_unchanged_quote_confirmed_by_its_current_run(
    migrated_postgres_dsn: str,
) -> None:
    """Repeated provider facts remain visible to their own ingestion run."""

    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        reference = datetime(2026, 8, 3, 19, 55, tzinfo=UTC)
        with runtime.transaction() as connection:
            instrument_id = reconcile_instrument(connection, "NVDA", asset_class="equity", category="test")
            connection.execute(
                """
                INSERT INTO ingest.source (id, name, family, kind)
                VALUES ('polygon', 'Polygon', 'market_data', 'daily_bars'),
                       ('robinhood', 'Robinhood', 'broker', 'quote')
                """
            )
            bar_run = connection.execute(
                """
                INSERT INTO ingest.run (source_id, capability, started_at, finished_at, status)
                VALUES ('polygon', 'daily_bars', %s, %s, 'succeeded') RETURNING id
                """,
                [reference - timedelta(days=5), reference - timedelta(days=1)],
            ).fetchone()["id"]
            for trading_date in (date(2026, 7, 29), date(2026, 7, 30), date(2026, 7, 31)):
                observed_at = datetime.combine(trading_date, time(20), tzinfo=UTC)
                bar = connection.execute(
                    """
                    INSERT INTO raw.price_bar
                        (instrument_id, source_id, ingest_run_id, interval, trading_date,
                         observed_at, close, available_at)
                    VALUES (%s, 'polygon', %s, '1d', %s, %s, 100, %s)
                    RETURNING id, available_at
                    """,
                    [instrument_id, bar_run, trading_date, observed_at, reference - timedelta(days=1)],
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO raw.price_bar_confirmation (fact_id, fact_available_at, ingest_run_id)
                    VALUES (%s, %s, %s)
                    """,
                    [bar["id"], bar["available_at"], bar_run],
                )
            first_run = connection.execute(
                """
                INSERT INTO ingest.run (source_id, capability, started_at, finished_at, status)
                VALUES ('robinhood', 'equity_quotes', %s, %s, 'succeeded') RETURNING id
                """,
                [reference - timedelta(minutes=4), reference - timedelta(minutes=3)],
            ).fetchone()["id"]
            quote = connection.execute(
                """
                INSERT INTO raw.quote
                    (instrument_id, source_id, ingest_run_id, observed_at, price, available_at)
                VALUES (%s, 'robinhood', %s, %s, 100, %s)
                RETURNING id, available_at
                """,
                [instrument_id, first_run, reference - timedelta(minutes=2), reference - timedelta(minutes=2)],
            ).fetchone()
            connection.execute(
                """
                INSERT INTO raw.quote_confirmation (fact_id, fact_available_at, ingest_run_id)
                VALUES (%s, %s, %s)
                """,
                [quote["id"], quote["available_at"], first_run],
            )
            repeat_run = connection.execute(
                """
                INSERT INTO ingest.run (source_id, capability, started_at, finished_at, status)
                VALUES ('robinhood', 'equity_quotes', %s, %s, 'succeeded') RETURNING id
                """,
                [reference - timedelta(minutes=1), reference],
            ).fetchone()["id"]
            connection.execute(
                """
                INSERT INTO raw.quote_confirmation (fact_id, fact_available_at, ingest_run_id)
                VALUES (%s, %s, %s)
                """,
                [quote["id"], quote["available_at"], repeat_run],
            )

        observations, report = OptionEventRepository(runtime).detector_observations(
            reference, provider_run_id=str(repeat_run), symbols=["NVDA"],
        )

        assert len(observations) == 1
        assert report["received_symbols"] == 1
        assert report["fresh_symbols"] == 1
    finally:
        runtime.close()
