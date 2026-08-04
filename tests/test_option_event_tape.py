from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from investment_panel.core.options_event_tape import (
    DELTA_LADDER,
    EventObservation,
    FrozenContract,
    scheduled_event_slots,
    select_event_strip,
    trigger_reason,
)
from investment_panel.core.options_recovery_config import OptionsDecisionSystemConfig
from investment_panel.core.options_recovery_paper import recovery_risk_policy
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.instruments import reconcile_instrument
from investment_panel.database.option_events import OptionEventRepository
from investment_panel.database.options_recovery_execution import RecoveryExecutionRepository
from investment_panel.database.options_recovery_read import RecoveryReadRepository
from investment_panel.database.options_recovery_cohorts import ProgramEligibility
from investment_panel.database.options_history import OptionHistoryRepository
from investment_panel.database.options_history_policy import EVENT_PROFILE, OptionHistoryPolicyRepository
from investment_panel.database.runtime import DatabaseRuntime


def _strip_rows(symbol: str = "NVDA", *, as_of: date = date(2026, 8, 3)) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for expiry_offset in (10, 17, 31):
        expiry = (as_of + timedelta(days=expiry_offset)).isoformat()
        for option_type, sign in (("call", 1), ("put", -1)):
            for index, target in enumerate(DELTA_LADDER):
                rows.append({
                    "underlying_symbol": symbol,
                    "contract_symbol": f"{symbol}-{expiry}-{option_type}-{index}",
                    "expiration": expiry,
                    "option_type": option_type,
                    "strike": 100 + index,
                    "delta": sign * target,
                    "bid": 2.0,
                    "ask": 2.2,
                    "bid_size": 10,
                    "ask_size": 10,
                    "open_interest": 200,
                })
    return rows


def test_event_trigger_uses_decimal_returns() -> None:
    observation = EventObservation(
        symbol="NVDA",
        observed_at=datetime(2026, 8, 3, 15, tzinfo=UTC),
        price=100.0,
        one_day_pct=-0.061,
    )
    assert trigger_reason(observation) == "one_day_down_6pct"


def test_event_capture_slots_stop_at_an_early_market_close() -> None:
    slots = scheduled_event_slots(
        datetime(2026, 11, 27, 14, 30, tzinfo=UTC),
        datetime(2026, 11, 27, 21, 0, tzinfo=UTC),
    )

    assert len(slots) == 15
    assert slots[0] == datetime(2026, 11, 27, 14, 30, tzinfo=UTC)
    assert slots[-1] == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)


def test_frozen_strip_preserves_original_contracts_and_records_replacements() -> None:
    as_of = date(2026, 8, 3)
    rows = _strip_rows(as_of=as_of)
    first = select_event_strip(rows, as_of=as_of)
    assert len(first.rows) == 36
    assert len(first.expected_contract_keys) == 36
    frozen = [
        FrozenContract(
            contract_key=str(row["contract_symbol"]),
            option_type=str(row["option_type"]),
            expiration=date.fromisoformat(str(row["expiration"])),
            target_delta=float(row["_event_target_delta"]),
            is_initial=True,
        )
        for row in first.rows
    ]
    missing = str(first.rows[0]["contract_symbol"])
    next_rows = [row for row in rows if row["contract_symbol"] != missing]
    replacement = {**dict(first.rows[0]), "contract_symbol": f"{missing}-replacement", "delta": 0.26}
    next_rows.append(replacement)
    second = select_event_strip(next_rows, as_of=as_of, existing=frozen)
    assert missing in second.expected_contract_keys
    assert second.replacements[f"{missing}-replacement"] == missing
    assert f"{missing}-replacement" in {row["contract_symbol"] for row in second.rows}


def test_three_successive_slot_replacements_keep_fixed_denominator_and_lineage() -> None:
    as_of = date(2026, 8, 3)
    rows = _strip_rows(as_of=as_of)
    first = select_event_strip(rows, as_of=as_of)
    original_key = str(first.rows[0]["contract_symbol"])
    slot = str(first.rows[0]["_event_ladder_slot_key"])

    def frozen(selection):
        return [
            FrozenContract(
                contract_key=str(row["contract_symbol"]),
                option_type=str(row["option_type"]),
                expiration=date.fromisoformat(str(row["expiration"])),
                target_delta=float(row["_event_target_delta"]),
                is_initial=bool(row["_event_initial"]),
                ladder_slot_key=str(row["_event_ladder_slot_key"]),
            )
            for row in selection.rows
        ]

    successor_one = {**dict(first.rows[0]), "contract_symbol": f"{original_key}-one", "delta": 0.26}
    second_rows = [row for row in rows if row["contract_symbol"] != original_key] + [successor_one]
    second = select_event_strip(
        second_rows,
        as_of=as_of,
        existing=frozen(first),
        original_contract_keys=first.expected_contract_keys,
    )

    successor_two = {**successor_one, "contract_symbol": f"{original_key}-two", "delta": 0.27}
    third_rows = [row for row in second_rows if row["contract_symbol"] != successor_one["contract_symbol"]] + [successor_two]
    third = select_event_strip(
        third_rows,
        as_of=as_of,
        existing=frozen(second),
        original_contract_keys=first.expected_contract_keys,
        retired_contract_keys=second.retire_contract_keys,
    )

    successor_three = {**successor_two, "contract_symbol": f"{original_key}-three", "delta": 0.28}
    fourth_rows = [row for row in third_rows if row["contract_symbol"] != successor_two["contract_symbol"]] + [successor_three]
    fourth = select_event_strip(
        fourth_rows,
        as_of=as_of,
        existing=frozen(third),
        original_contract_keys=first.expected_contract_keys,
        retired_contract_keys=(*second.retire_contract_keys, *third.retire_contract_keys),
    )

    assert second.replacements[str(successor_one["contract_symbol"])] == original_key
    assert third.replacements[str(successor_two["contract_symbol"])] == str(successor_one["contract_symbol"])
    assert fourth.replacements[str(successor_three["contract_symbol"])] == str(successor_two["contract_symbol"])
    assert len(fourth.rows) == len(first.rows) == 36
    assert len(fourth.expected_slot_keys) == len(first.expected_slot_keys) == 36
    assert len(fourth.expected_contract_keys) == len(first.expected_contract_keys) == 36
    assert sum(row["_event_ladder_slot_key"] == slot for row in fourth.rows) == 1


def test_missing_slot_never_reuses_another_active_slot_member() -> None:
    as_of = date(2026, 8, 3)
    first = select_event_strip(_strip_rows(as_of=as_of), as_of=as_of)
    missing = dict(first.rows[0])
    neighbor = dict(first.rows[1])
    frozen = [
        FrozenContract(
            contract_key=str(row["contract_symbol"]),
            option_type=str(row["option_type"]),
            expiration=date.fromisoformat(str(row["expiration"])),
            target_delta=float(row["_event_target_delta"]),
            is_initial=bool(row["_event_initial"]),
            ladder_slot_key=str(row["_event_ladder_slot_key"]),
        )
        for row in first.rows
    ]
    successor = {**missing, "contract_symbol": "NVDA-successor", "delta": 0.60}
    rows = [row for row in _strip_rows(as_of=as_of) if row["contract_symbol"] != missing["contract_symbol"]]
    rows.append(successor)

    next_strip = select_event_strip(
        rows,
        as_of=as_of,
        existing=frozen,
        original_contract_keys=first.expected_contract_keys,
    )

    selected = {str(row["contract_symbol"]): str(row["_event_ladder_slot_key"]) for row in next_strip.rows}
    assert next_strip.replacements["NVDA-successor"] == str(missing["contract_symbol"])
    assert selected[str(neighbor["contract_symbol"])] == str(neighbor["_event_ladder_slot_key"])


def test_event_detection_enforces_two_symbol_capacity_without_duplicate_deferred_rows(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        with runtime.transaction() as connection:
            ids = {
                symbol: reconcile_instrument(connection, symbol, asset_class="equity", category="test")
                for symbol in ("NVDA", "AMD", "TSLA")
            }
        observed = datetime(2026, 8, 3, 15, tzinfo=UTC)
        observations = [
            EventObservation(symbol="NVDA", instrument_id=ids["NVDA"], observed_at=observed, price=100, one_day_pct=-0.12, liquidity_score=20),
            EventObservation(symbol="AMD", instrument_id=ids["AMD"], observed_at=observed, price=100, one_day_pct=-0.09, liquidity_score=10),
            EventObservation(symbol="TSLA", instrument_id=ids["TSLA"], observed_at=observed, price=100, one_day_pct=-0.07),
        ]
        repository = OptionEventRepository(runtime)
        first = repository.detect_events(observations, now=observed)
        assert first["detected"] == 2
        assert first["deferred_capacity"] == 1
        second = repository.detect_events(observations, now=observed + timedelta(minutes=5))
        assert second["detected"] == 0
        with runtime.read() as connection:
            rows = connection.execute(
                "SELECT status, count(*) AS count FROM analysis.option_event GROUP BY status ORDER BY status"
            ).fetchall()
        assert {row["status"]: row["count"] for row in rows} == {"active": 2, "deferred_capacity": 1}
    finally:
        runtime.close()


def test_existing_event_keeps_its_original_reference_price_and_provenance(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        first_seen = datetime(2026, 8, 3, 15, tzinfo=UTC)
        with runtime.transaction() as connection:
            instrument_id = reconcile_instrument(connection, "NVDA", asset_class="equity", category="test")
        repository = OptionEventRepository(runtime)
        event_id = repository.detect_events([
            EventObservation(
                "NVDA", first_seen, 90.0, one_day_pct=-0.10, instrument_id=instrument_id,
                reference_price=120.0, reference_trading_date=date(2026, 7, 31),
                reference_source_id="polygon", reference_available_at=first_seen,
            ),
        ], now=first_seen)["active_events"][0]["event_id"]
        repository.detect_events([
            EventObservation(
                "NVDA", first_seen + timedelta(minutes=5), 80.0, one_day_pct=-0.20,
                instrument_id=instrument_id, reference_price=110.0,
                reference_trading_date=date(2026, 7, 31), reference_source_id="yahoo_chart",
                reference_available_at=first_seen + timedelta(minutes=5),
            ),
        ], now=first_seen + timedelta(minutes=5))

        with runtime.read() as connection:
            event = connection.execute(
                """
                SELECT reference_price, reference_trading_date, reference_source_id,
                       trigger_one_day_pct, event_low
                FROM analysis.option_event WHERE id = %s
                """,
                [event_id],
            ).fetchone()
        assert event["reference_price"] == 120.0
        assert event["reference_trading_date"] == date(2026, 7, 31)
        assert event["reference_source_id"] == "polygon"
        assert event["trigger_one_day_pct"] == -0.10
        assert event["event_low"] == 80.0
    finally:
        runtime.close()


def test_capture_health_excludes_capacity_deferred_events_from_scheduled_slot_denominator(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        start = datetime(2026, 8, 3, 14, 30, tzinfo=UTC)
        reference = datetime(2026, 8, 3, 15, 0, tzinfo=UTC)
        with runtime.transaction() as connection:
            instrument_ids = [
                reconcile_instrument(connection, symbol, asset_class="equity", category="test")
                for symbol in ("NVDA", "AMD", "TSLA")
            ]
            for instrument_id, status, event_start, enrolled_at in (
                (instrument_ids[0], "active", start, start),
                (instrument_ids[1], "active", start, start),
                # This event was observed but was never admitted to either
                # Robinhood lease, so it must not count as an uncollected slot.
                (instrument_ids[2], "deferred_capacity", start - timedelta(days=5), None),
            ):
                connection.execute(
                    """
                    INSERT INTO analysis.option_event
                        (instrument_id, detected_at, started_at, enrolled_at,
                         reference_price, event_low, severity_score, status)
                    VALUES (%s, %s, %s, %s, 100, 90, 1, %s)
                    """,
                    [instrument_id, start, event_start, enrolled_at, status],
                )

        health = OptionEventRepository(runtime).capture_health(now=reference)
        expected = 2 * len(scheduled_event_slots(start, reference))

        assert health["scheduled_slots"] == expected
        assert health["active_events"] == 2
    finally:
        runtime.close()


def test_capture_health_counts_initial_slot_that_precedes_enrollment_commit(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        initial_slot = datetime(2026, 8, 3, 14, 30, tzinfo=UTC)
        enrolled_at = initial_slot + timedelta(minutes=15)
        reference = initial_slot + timedelta(minutes=30)
        with runtime.transaction() as connection:
            instrument_id = reconcile_instrument(connection, "NVDA", asset_class="equity", category="test")
            event = connection.execute(
                """
                INSERT INTO analysis.option_event
                    (instrument_id, detected_at, started_at, enrolled_at,
                     reference_price, event_low, severity_score, status)
                VALUES (%s, %s, %s, %s, 100, 90, 1, 'active')
                RETURNING id
                """,
                [instrument_id, initial_slot, initial_slot, enrolled_at],
            ).fetchone()
            connection.execute(
                """
                INSERT INTO analysis.option_event_capture (event_id, scheduled_at, status)
                VALUES (%s, %s, 'complete')
                """,
                [event["id"], initial_slot],
            )

        health = OptionEventRepository(runtime).capture_health(now=reference)

        assert health["scheduled_slots"] == len(scheduled_event_slots(initial_slot, reference))
        assert health["covered_slots"] == 1
    finally:
        runtime.close()


def test_detector_handles_an_empty_effective_universe(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        result = OptionEventRepository(runtime).detect_events(now=datetime(2026, 8, 3, 15, tzinfo=UTC))
        assert result["status"] == "ok"
        assert result["detected"] == 0
    finally:
        runtime.close()


def test_recovery_read_models_separate_radar_evidence_from_health_diagnostics(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        observed = datetime(2026, 8, 3, 15, tzinfo=UTC)
        with runtime.transaction() as connection:
            instrument_id = reconcile_instrument(connection, "NVDA", asset_class="equity", category="test")
        event_id = OptionEventRepository(runtime).detect_events(
            [EventObservation("NVDA", observed, 100.0, one_day_pct=-0.08, instrument_id=instrument_id)],
            now=observed,
        )["active_events"][0]["event_id"]

        read = RecoveryReadRepository(runtime)
        event = read.events(limit=1)[0]
        detail = read.event_detail(event_id)
        radar = read.panel_models({"option_recovery_funnel", "option_recovery_event", "option_recovery_family_performance"})
        health = read.panel_models({"option_recovery_health"})["option_recovery_health"][0]

        assert event["event_id"] == event_id
        assert detail is not None and detail["event"]["event_id"] == event_id
        assert set(radar) == {"option_recovery_funnel", "option_recovery_event", "option_recovery_family_performance"}
        assert "capture" not in radar["option_recovery_event"][0]
        assert health["capture"]["active_events"] == 1
        assert health["capture"]["active_robinhood_leases"] <= 2
        assert health["agent_telemetry"]["batches"] == 0
    finally:
        runtime.close()


def test_recovery_event_and_health_routes_expose_only_their_bounded_surfaces(
    migrated_postgres_dsn: str,
    monkeypatch,
) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app import deps
    from app.routers.options import router as options_router

    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        observed = datetime(2026, 8, 3, 15, tzinfo=UTC)
        with runtime.transaction() as connection:
            instrument_id = reconcile_instrument(connection, "NVDA", asset_class="equity", category="test")
        event_id = OptionEventRepository(runtime).detect_events(
            [EventObservation("NVDA", observed, 100.0, one_day_pct=-0.08, instrument_id=instrument_id)],
            now=observed,
        )["active_events"][0]["event_id"]
        with runtime.transaction() as connection:
            legacy_instrument = reconcile_instrument(connection, "AAOI", asset_class="equity", category="test")
        legacy_event_id = OptionEventRepository(runtime).detect_events(
            [EventObservation("AAOI", observed, 20.0, one_day_pct=-0.08, instrument_id=legacy_instrument)],
            now=observed,
        )["active_events"][0]["event_id"]
        with runtime.transaction() as connection:
            connection.execute(
                """
                UPDATE analysis.option_event
                SET cohort_id = (SELECT id FROM analysis.option_recovery_cohort WHERE objective_version = 'short_horizon_convex_v1'),
                    status = 'invalidated', data_quality_status = 'invalid_reference_bar',
                    invalidation_reason = 'invalid_reference_bar', invalidated_at = now()
                WHERE id = %s
                """,
                [legacy_event_id],
            )
        monkeypatch.setattr(deps, "load_config", lambda: {"database": {"url": migrated_postgres_dsn}})
        application = FastAPI()
        application.include_router(options_router)

        with TestClient(application) as client:
            events = client.get("/api/options/events")
            audit = client.get("/api/options/events?cohort=short_horizon_convex_v1&include_invalidated=true")
            audit_status = client.get("/api/options/events?cohort=short_horizon_convex_v1&status=invalidated")
            detail = client.get(f"/api/options/events/{event_id}")
            hidden_legacy_detail = client.get(f"/api/options/events/{legacy_event_id}")
            audit_detail = client.get(
                f"/api/options/events/{legacy_event_id}?cohort=short_horizon_convex_v1&include_invalidated=true"
            )
            health = client.get("/api/health/options-recovery")

        assert events.status_code == 200
        assert events.json()["events"][0]["event_id"] == event_id
        assert [row["event_id"] for row in events.json()["events"]] == [event_id]
        assert "capture" not in events.json()["events"][0]
        assert audit.status_code == 200
        assert [row["event_id"] for row in audit.json()["events"]] == [legacy_event_id]
        assert audit.json()["events"][0]["status"] == "invalidated"
        assert audit_status.status_code == 200
        assert [row["event_id"] for row in audit_status.json()["events"]] == [legacy_event_id]
        assert audit_status.json()["events"][0]["status"] == "invalidated"
        assert detail.status_code == 200
        assert detail.json()["event"]["event_id"] == event_id
        assert hidden_legacy_detail.status_code == 404
        assert audit_detail.status_code == 200
        assert audit_detail.json()["event"]["event_id"] == legacy_event_id
        assert health.status_code == 200
        assert "capture" in health.json() and "scheduler" in health.json()
        assert "agent_telemetry" in health.json()
    finally:
        runtime.close()


def test_open_recovery_paper_order_keeps_event_tape_active_past_event_age(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        started = datetime(2026, 8, 3, 15, tzinfo=UTC)
        with runtime.transaction() as connection:
            instrument_id = reconcile_instrument(connection, "LATE", asset_class="equity", category="test")
        events = OptionEventRepository(runtime)
        event_id = events.detect_events(
            [EventObservation("LATE", started, 100.0, one_day_pct=-0.08, instrument_id=instrument_id)],
            now=started,
        )["active_events"][0]["event_id"]
        with runtime.transaction() as connection:
            cohort_id = connection.execute(
                "SELECT cohort_id FROM analysis.option_event WHERE id = %s", [event_id]
            ).fetchone()["cohort_id"]
            connection.execute(
                """
                INSERT INTO app.paper_order
                    (instrument_id, side, quantity, status, event_id, strategy_family, cohort_id)
                VALUES (%s, 'buy', 1, 'staged', %s, 'late_fill_test', %s)
                """,
                [instrument_id, event_id, cohort_id],
            )

        closed = events.close_events(
            now=datetime(2026, 8, 18, 15, tzinfo=UTC),
            current_prices={instrument_id: 100.0},
        )

        assert closed == 0
        with runtime.read() as connection:
            event = connection.execute(
                "SELECT status FROM analysis.option_event WHERE id = %s", [event_id]
            ).fetchone()
            policy = connection.execute(
                "SELECT requested_state FROM app.option_history_policy WHERE event_id = %s AND profile = 'event_strip'",
                [event_id],
            ).fetchone()
        assert event["status"] == "active"
        assert policy["requested_state"] == "on"
    finally:
        runtime.close()


def test_close_events_ignores_quotes_confirmed_after_its_reference_time(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        started = datetime(2026, 7, 30, 15, tzinfo=UTC)
        reference = datetime(2026, 8, 3, 15, tzinfo=UTC)
        with runtime.transaction() as connection:
            instrument_id = reconcile_instrument(connection, "LOOKAHEAD", asset_class="equity", category="test")
        events = OptionEventRepository(runtime)
        event_id = events.detect_events(
            [EventObservation("LOOKAHEAD", started, 100.0, one_day_pct=-0.10, instrument_id=instrument_id)],
            now=started,
        )["active_events"][0]["event_id"]
        ingestion = IngestionRepository(runtime)
        ingestion.register_source("lookahead-quote", name="Lookahead", family="market_data", kind="quote")
        with ingestion.run("lookahead-quote", "quote") as run:
            assert ingestion.store_quotes(
                run.id,
                "lookahead-quote",
                [{"symbol": "LOOKAHEAD", "observed_at": reference - timedelta(minutes=1), "price": 112.0}],
            ) == 1
            run.finish("succeeded")
        future_available_at = reference + timedelta(minutes=1)
        with runtime.transaction() as connection:
            quote = connection.execute(
                "SELECT id, ingest_run_id FROM raw.quote WHERE instrument_id = %s", [instrument_id],
            ).fetchone()
            connection.execute("UPDATE raw.quote SET available_at = %s WHERE id = %s", [future_available_at, quote["id"]])
            connection.execute("DELETE FROM raw.quote_confirmation WHERE fact_id = %s", [quote["id"]])
            connection.execute(
                """
                INSERT INTO raw.quote_confirmation (fact_id, fact_available_at, ingest_run_id)
                VALUES (%s, %s, %s)
                """,
                [quote["id"], future_available_at, quote["ingest_run_id"]],
            )

        assert events.close_events(now=reference) == 0
        with runtime.read() as connection:
            event = connection.execute("SELECT status FROM analysis.option_event WHERE id = %s", [event_id]).fetchone()
        assert event["status"] == "active"
    finally:
        runtime.close()


def test_event_profile_writes_an_isolated_capture_and_contract_cohort(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        observed = datetime(2026, 8, 3, 15, tzinfo=UTC)
        with runtime.transaction() as connection:
            instrument_id = reconcile_instrument(connection, "NVDA", asset_class="equity", category="test")
        events = OptionEventRepository(runtime)
        detected = events.detect_events(
            [EventObservation("NVDA", observed, 100.0, one_day_pct=-0.08, instrument_id=instrument_id)],
            now=observed,
        )
        event_id = detected["active_events"][0]["event_id"]
        policy = OptionHistoryPolicyRepository(runtime).policy_for_symbol("NVDA", profile=EVENT_PROFILE)
        assert policy is not None
        assert policy["normalized_retention_days"] == 365
        assert policy["provider_payload_retention_days"] == 30

        rows = _strip_rows(as_of=observed.date())[:1]
        captured, selection = events.filter_event_strip(
            event_id,
            {
                "rows": rows,
                "expected_contract_count": 1,
                "received_contract_count": 1,
                "capture_started_at": observed,
                "capture_finished_at": observed + timedelta(seconds=2),
            },
            as_of=observed,
        )
        ingestion = IngestionRepository(runtime)
        ingestion.register_source("robinhood", name="Robinhood", family="broker", kind="option_chain")
        with ingestion.run("robinhood", "option_event_strip") as run:
            history = OptionHistoryRepository(runtime)
            universe = f"event-strip:{event_id}"
            assert history.claim_slot(
                source_id="robinhood",
                symbol="NVDA",
                slot_at=observed,
                run_id=run.id,
                collection_profile=EVENT_PROFILE,
                universe=universe,
            )
            stored = history.store_capture(
                run_id=run.id,
                source_id="robinhood",
                symbol="NVDA",
                slot_at=observed,
                captured=captured,
                collection_profile=EVENT_PROFILE,
                universe=universe,
                materialize=False,
            )
            run.finish("succeeded", summary=stored)
        recorded = events.record_capture(event_id, stored=stored, selection=selection)
        assert recorded["status"] == "complete"
        with runtime.read() as connection:
            snapshot = connection.execute(
                "SELECT collection_profile, universe FROM raw.option_snapshot WHERE id = %s", [stored["snapshot_id"]]
            ).fetchone()
            contracts = connection.execute(
                "SELECT count(*) AS count FROM analysis.option_event_contract WHERE event_id = %s", [event_id]
            ).fetchone()
        assert snapshot["collection_profile"] == EVENT_PROFILE
        assert snapshot["universe"] == f"event-strip:{event_id}"
        assert contracts["count"] == 1
    finally:
        runtime.close()


def test_event_capture_creates_at_most_two_typed_forward_shadow_tickets(
    migrated_postgres_dsn: str,
    monkeypatch,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        observed = datetime(2026, 8, 3, 15, tzinfo=UTC)
        finished = observed + timedelta(minutes=90)
        event_started = observed - timedelta(days=7)
        with runtime.transaction() as connection:
            instrument_id = reconcile_instrument(connection, "NVDA", asset_class="equity", category="test")
        events = OptionEventRepository(runtime)
        event_id = events.detect_events(
            [EventObservation("NVDA", event_started, 100.0, one_day_pct=-0.08, instrument_id=instrument_id)],
            now=event_started,
        )["active_events"][0]["event_id"]
        with runtime.transaction() as connection:
            for index, price in enumerate((100.0, 100.4, 101.1, 101.9, 102.2, 103.0)):
                at = observed + timedelta(minutes=index * 15)
                connection.execute(
                    """
                    INSERT INTO analysis.option_event_spot (event_id, observed_at, available_at, price)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (event_id, observed_at) DO UPDATE SET price = EXCLUDED.price
                    """,
                    [event_id, at, at, price],
                )
        row = next(item for item in _strip_rows(as_of=observed.date()) if item["option_type"] == "call" and item["delta"] == 0.45)
        captured, selection = events.filter_event_strip(
            event_id,
            {
                "rows": [row],
                "expected_contract_count": 1,
                "received_contract_count": 1,
                "capture_started_at": observed,
                "capture_finished_at": finished,
            },
            as_of=observed,
        )
        ingestion = IngestionRepository(runtime)
        ingestion.register_source("robinhood", name="Robinhood", family="broker", kind="option_chain")
        with ingestion.run("robinhood", "option_event_strip") as run:
            history = OptionHistoryRepository(runtime)
            universe = f"event-strip:{event_id}"
            assert history.claim_slot(
                source_id="robinhood", symbol="NVDA", slot_at=observed, run_id=run.id,
                collection_profile=EVENT_PROFILE, universe=universe,
            )
            stored = history.store_capture(
                run_id=run.id, source_id="robinhood", symbol="NVDA", slot_at=observed,
                captured=captured, collection_profile=EVENT_PROFILE, universe=universe, materialize=False,
            )
            run.finish("succeeded", summary=stored)
        recorded = events.record_capture(event_id, stored=stored, selection=selection)
        result = RecoveryExecutionRepository(runtime).evaluate_capture(
            event_id,
            capture_id=recorded["event_capture_id"],
            now=finished + timedelta(seconds=1),
        )
        assert result["status"] == "ok"
        assert len(result["selected"]) <= 2
        assert result["selected"][0]["family"] == "shock_reversal_call_v1"
        with runtime.read() as connection:
            signal = connection.execute(
                "SELECT decision_id, status, ticket->>'ticket_version' AS version FROM analysis.option_event_signal"
            ).fetchone()
            orders = connection.execute("SELECT count(*) AS count FROM app.paper_order").fetchone()
            denominator = connection.execute(
                "SELECT strategy_key, selection_stage, miss_reason FROM analysis.option_opportunity_observation ORDER BY strategy_key"
            ).fetchall()
        assert signal["status"] == "shadow"
        assert signal["version"] == "4"
        ticket = RecoveryReadRepository(runtime).ticket(str(signal["decision_id"]))
        assert ticket is not None
        assert ticket["ticket_version"] == 4
        assert ticket["objective_version"] == "short_horizon_convex_v2"
        from app.options_history_contracts import RecoveryOptionTradeTicketV4

        assert RecoveryOptionTradeTicketV4.model_validate(ticket).ticket_version == 4
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app import deps
        from app.routers.options import router as options_router

        monkeypatch.setattr(deps, "load_config", lambda: {"database": {"url": migrated_postgres_dsn}})
        application = FastAPI()
        application.include_router(options_router)
        with TestClient(application) as client:
            api_ticket = client.get(f"/api/options/tickets/{signal['decision_id']}")
        assert api_ticket.status_code == 200
        assert api_ticket.json()["ticket_version"] == 4
        assert api_ticket.json()["legs"][0]["occ_symbol"] == ticket["legs"][0]["occ_symbol"]
        assert orders["count"] == 0
        assert len(denominator) == 2
        by_family = {row["strategy_key"]: row for row in denominator}
        assert by_family["shock_reversal_call_v1"]["selection_stage"] == "published"
        assert by_family["shock_continuation_put_v1"]["miss_reason"] == "not_featured"
        staged = RecoveryExecutionRepository(runtime).stage_qualified_orders(
            event_id,
            now=finished + timedelta(seconds=1),
            enabled=True,
        )
        assert staged["status"] == "blocked"
        assert staged["orders"] == []
        assert "program_canary_not_qualified" in staged["blockers"]
        with runtime.read() as connection:
            order_count = connection.execute("SELECT count(*) AS count FROM app.paper_order").fetchone()
        assert order_count["count"] == 0

        # The staging boundary uses a current executable quote, not the
        # shadow ticket's old premium.  Supply the otherwise independent
        # global-canary result here to isolate that boundary.
        with runtime.transaction() as connection:
            connection.execute(
                """
                UPDATE analysis.option_event
                SET quote_age_minutes = 1.0,
                    reference_source_id = 'test-confirmed',
                    reference_available_at = %s
                WHERE id = %s
                """,
                [finished, event_id],
            )
        staging = RecoveryExecutionRepository(
            runtime,
            risk_policy=recovery_risk_policy(OptionsDecisionSystemConfig(options_risk_sleeve_capital=25_000.0)),
        )
        cohort = staging.cohorts.current()
        assert cohort is not None
        monkeypatch.setattr(
            staging.cohorts,
            "program_eligibility",
            lambda **_: ProgramEligibility(True, (), cohort, 5, 5, {}),
        )
        current_stage = staging.stage_qualified_orders(
            event_id,
            now=finished + timedelta(seconds=1),
            enabled=True,
        )
        assert current_stage["orders"] and current_stage["orders"][0]["status"] == "staged"
        with runtime.read() as connection:
            paper = connection.execute(
                "SELECT ticket_snapshot FROM app.paper_order WHERE event_id = %s", [event_id]
            ).fetchone()
        assert paper is not None
        assert paper["ticket_snapshot"]["entry"]["limit_price"] == 2.22
        assert paper["ticket_snapshot"]["legs"][0]["quote_time"] == finished.isoformat()
    finally:
        runtime.close()
