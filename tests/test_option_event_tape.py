from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from investment_panel.core.options_event_tape import (
    DELTA_LADDER,
    EventObservation,
    FrozenContract,
    select_event_strip,
    trigger_reason,
)
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.instruments import reconcile_instrument
from investment_panel.database.option_events import OptionEventRepository
from investment_panel.database.options_recovery_execution import RecoveryExecutionRepository
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


def test_detector_handles_an_empty_effective_universe(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        result = OptionEventRepository(runtime).detect_events(now=datetime(2026, 8, 3, 15, tzinfo=UTC))
        assert result["status"] == "ok"
        assert result["detected"] == 0
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
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        observed = datetime(2026, 8, 3, 15, tzinfo=UTC)
        finished = observed + timedelta(minutes=90)
        with runtime.transaction() as connection:
            instrument_id = reconcile_instrument(connection, "NVDA", asset_class="equity", category="test")
        events = OptionEventRepository(runtime)
        event_id = events.detect_events(
            [EventObservation("NVDA", observed, 100.0, one_day_pct=-0.08, instrument_id=instrument_id)],
            now=observed,
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
                "SELECT status, ticket->>'ticket_version' AS version FROM analysis.option_event_signal"
            ).fetchone()
            orders = connection.execute("SELECT count(*) AS count FROM app.paper_order").fetchone()
        assert signal["status"] == "shadow"
        assert signal["version"] == "4"
        assert orders["count"] == 0
    finally:
        runtime.close()
