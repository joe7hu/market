from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from investment_panel.analysis.history_v3 import analyze_group, static_arbitrage_findings
from investment_panel.core.option_underwriting import (
    conservative_entry,
    conservative_mark,
    historical_payoff_statistics,
    paper_state,
)
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.actions import _v3_paper_readiness
from investment_panel.database.options_history import OptionHistoryRepository
from investment_panel.database.options_history_v3 import is_later_capture_cohort
from investment_panel.database.options_decision_system import OptionsDecisionSystemRepository
from investment_panel.database.runtime import DatabaseRuntime


def _rows(option_type: str = "call") -> list[dict[str, object]]:
    now = datetime(2026, 7, 20, 14, 30, tzinfo=UTC)
    return [
        {
            "contract_id": index + 1, "strike": 470 + index * 5, "option_type": option_type,
            "bid": max(0.1, 34 - index * 2.5) if option_type == "call" else 4 + index * 2.5,
            "ask": max(0.2, 34.2 - index * 2.5) if option_type == "call" else 4.2 + index * 2.5,
            "mid": max(0.15, 34.1 - index * 2.5) if option_type == "call" else 4.1 + index * 2.5,
            "open_interest": 500, "underlying_price": 500.0, "market_data_status": "open",
            "group_started_at": now, "group_finished_at": now + timedelta(seconds=4),
            "provider_observed_at": now + timedelta(seconds=3), "underlying_observed_at": now,
        }
        for index in range(12)
    ]


def test_price_shape_fit_is_deterministic_and_clean_chain_has_no_static_candidate() -> None:
    first = analyze_group(_rows(), spot=500.0, option_type="call")
    second = analyze_group(_rows(), spot=500.0, option_type="call")
    assert first["fit"].status == "succeeded"
    assert first["fit"].fitted == second["fit"].fitted
    assert not first["static_findings"]
    assert {row["classification"] for row in first["relative_values"]} <= {"rejected", "relative_cheap", "relative_rich"}


def test_static_arbitrage_uses_executable_worst_side_and_detects_bounds() -> None:
    rows = _rows()
    rows[0]["ask"] = 1.0  # substantially below the call intrinsic value of 30
    findings = static_arbitrage_findings(rows, spot=500.0, option_type="call")
    assert any(item["kind"] == "intrinsic_lower_bound" for item in findings)
    audited_wing = [
        {"contract_id": 800, "strike": 800.0, "bid": 1.00, "ask": 1.10, "mid": 1.05},
        {"contract_id": 805, "strike": 805.0, "bid": 0.95, "ask": 1.05, "mid": 1.00},
    ]
    assert not static_arbitrage_findings(audited_wing, spot=700.0, option_type="call")


def test_append_only_retry_advances_pointer_without_mixing_quotes(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    ingestion = IngestionRepository(runtime)
    history = OptionHistoryRepository(runtime)
    ingestion.register_source("robinhood", name="Robinhood", family="broker", kind="option_chain")
    slot = datetime(2026, 7, 20, 14, 30, tzinfo=UTC)
    rows = [
        {"underlying_symbol": "QQQ", "expiry": "2026-08-21", "strike": 480 + index * 5,
         "type": "call", "underlying_price": 500, "bid": 5 - index * 0.1,
         "ask": 5.2 - index * 0.1, "mid": 5.1 - index * 0.1, "open_interest": 200}
        for index in range(12)
    ]
    first_run = ingestion.start_run("robinhood", "option_history_full")
    assert history.claim_slot(source_id="robinhood", symbol="QQQ", slot_at=slot, run_id=first_run)
    partial = history.store_capture(run_id=first_run, source_id="robinhood", symbol="QQQ", slot_at=slot, captured={"rows": rows[:2], "expected_contract_count": len(rows), "received_contract_count": 2, "capture_started_at": slot, "capture_finished_at": slot})
    ingestion.finish_run(first_run, "partial", summary=partial)
    second_run = ingestion.start_run("robinhood", "option_history_full")
    assert history.claim_slot(source_id="robinhood", symbol="QQQ", slot_at=slot, run_id=second_run)
    complete = history.store_capture(run_id=second_run, source_id="robinhood", symbol="QQQ", slot_at=slot, captured={"rows": rows, "expected_contract_count": len(rows), "received_contract_count": len(rows), "capture_started_at": slot, "capture_finished_at": slot + timedelta(seconds=1)})
    ingestion.finish_run(second_run, "succeeded", summary=complete)
    assert history.chain(symbol="QQQ", snapshot=complete["snapshot_id"])["count"] == len(rows)
    groups = history.surface_groups(symbol="QQQ", snapshot=complete["snapshot_id"])
    assert groups["rows"] == [{"expiration": datetime(2026, 8, 21).date(), "option_type": "call", "dte": 32, "contract_count": len(rows)}]
    with runtime.read() as connection:
        generations = connection.execute("SELECT capture_state, received_contract_count FROM raw.option_capture_generation ORDER BY generation").fetchall()
        pointer = connection.execute("SELECT latest_complete_generation_id FROM raw.option_snapshot WHERE id = %s", [complete["snapshot_id"]]).fetchone()
    assert [row["capture_state"] for row in generations] == ["partial", "complete"]
    assert [row["received_contract_count"] for row in generations] == [2, len(rows)]
    assert pointer["latest_complete_generation_id"] == complete["capture_generation_id"]
    first_replay = history.v3.materialize(
        snapshot_id=complete["snapshot_id"], capture_generation_id=complete["capture_generation_id"], code_version="test-replay"
    )
    second_replay = history.v3.materialize(
        snapshot_id=complete["snapshot_id"], capture_generation_id=complete["capture_generation_id"], code_version="test-replay"
    )
    assert first_replay["deterministic_hash"] == second_replay["deterministic_hash"]
    assert first_replay["analysis_run_id"] != second_replay["analysis_run_id"]
    with runtime.transaction() as connection:
        candidate = connection.execute(
            "SELECT id FROM analysis.option_relative_value WHERE analysis_run_id = %s LIMIT 1",
            [second_replay["analysis_run_id"]],
        ).fetchone()
        connection.execute(
            "UPDATE analysis.option_relative_value SET classification = 'historical_static_arbitrage_candidate' WHERE id = %s",
            [candidate["id"]],
        )
        connection.execute(
            "INSERT INTO analysis.option_relative_value_verification (relative_value_id, status, blockers, evidence) VALUES (%s, 'verified', ARRAY[]::text[], '{}'::jsonb)",
            [candidate["id"]],
        )
    verified = OptionsDecisionSystemRepository(runtime).relative_values(
        symbol="QQQ", snapshot=complete["snapshot_id"], classification="verified_static_arbitrage_candidate"
    )
    assert [row["id"] for row in verified["rows"]] == [candidate["id"]]
    assert verified["rows"][0]["verification_status"] == "verified"
    runtime.close()


def test_underwriting_never_uses_midpoint_for_debit_fill_or_mark() -> None:
    now = datetime(2026, 7, 20, 14, 30, tzinfo=UTC)
    legs = [
        {"side": "long", "bid": 2.0, "ask": 2.2, "observed_at": now},
        {"side": "short", "bid": 0.8, "ask": 1.0, "observed_at": now + timedelta(seconds=2)},
    ]
    assert conservative_entry(legs, "call_debit_spread") == (1.4000000000000001, [])
    assert conservative_mark(legs, "call_debit_spread") == (1.0, [])
    result = paper_state(structure="put_debit_spread", lane="anomaly", thesis=None, fit_status="succeeded")
    assert result["paper_state"] == "WATCH"
    assert result["blockers"] == ["thesis_upgrade_required"]


def test_shadow_entry_requires_a_strictly_later_capture_cohort() -> None:
    earlier = datetime(2026, 7, 20, 14, 30, tzinfo=UTC)
    later = earlier + timedelta(minutes=15)

    assert is_later_capture_cohort(earlier, later)
    assert not is_later_capture_cohort(earlier, earlier)
    assert not is_later_capture_cohort(later, earlier)


def test_paper_state_requires_exact_gates_and_all_structures_can_watch() -> None:
    thesis = {
        "schema_version": 2, "direction": "bullish", "horizon_date": "2026-08-20",
        "invalidation": "QQQ closes below 480", "max_loss": 500,
    }
    assert paper_state(structure="long_call", lane="thesis", thesis=thesis, fit_status="fit_failed")["paper_state"] == "REJECT"
    assert paper_state(structure="call_debit_spread", lane="thesis", thesis=thesis, fit_status="succeeded")["paper_state"] == "COLLECTING"
    bearish = {**thesis, "direction": "bearish"}
    for structure, option_thesis in (("long_put", bearish), ("put_debit_spread", bearish), ("long_call", thesis), ("call_debit_spread", thesis)):
        state = paper_state(
            structure=structure, lane="thesis", thesis=option_thesis, fit_status="succeeded", scenario_count=20,
            expected_value=1, lower_95_expected_value=0.1, max_loss=100, data_confidence=0.9, execution_confidence=0.8,
        )
        assert state["paper_state"] == "WATCH"
    ready = paper_state(
        structure="long_call", lane="thesis", thesis=thesis, fit_status="succeeded", scenario_count=20,
        expected_value=1, lower_95_expected_value=0.1, max_loss=100, data_confidence=0.9, execution_confidence=0.8,
        calibration={"sample_size": 30, "lower_95_expectancy": 0.01, "brier_score": 0.2, "other_regime_monitoring_count": 5},
    )
    assert ready["paper_state"] == "PAPER_READY"


def test_historical_payoff_statistics_is_seeded_and_never_uses_midpoint_entry() -> None:
    legs = [
        {"option_type": "call", "side": "long", "strike": 100.0, "bid": 4.8, "ask": 5.0, "observed_at": datetime(2026, 7, 20, 14, 30, tzinfo=UTC)},
        {"option_type": "call", "side": "short", "strike": 105.0, "bid": 2.0, "ask": 2.2, "observed_at": datetime(2026, 7, 20, 14, 30, tzinfo=UTC)},
    ]
    returns = (-0.10, -0.02, 0.01, 0.04, 0.08) * 4

    first = historical_payoff_statistics(spot=100.0, legs=legs, terminal_returns=returns, seed=7)
    second = historical_payoff_statistics(spot=100.0, legs=legs, terminal_returns=returns, seed=7)

    assert first == second
    assert first["scenario_count"] == 20
    assert first["entry_price"] == pytest.approx(3.0)  # long ask minus short bid
    assert first["max_loss"] == pytest.approx(300.0)
    assert first["lower_95_expected_value"] is not None


def test_v3_paper_readiness_requires_a_fresh_coherent_leg_package() -> None:
    now = datetime(2026, 7, 20, 14, 30, tzinfo=UTC)
    payload = {
        "quote_observed_at": now.isoformat(),
        "leg_quotes": [{"bid": 2.0, "ask": 2.2, "size_available": True}],
    }

    assert _v3_paper_readiness(payload, now) == "A"
    assert _v3_paper_readiness(payload, now + timedelta(minutes=6)) == "C"
    assert _v3_paper_readiness({**payload, "leg_quotes": [{"bid": 2.2, "ask": 2.0}]}, now) == "C"


def test_claimed_generation_is_terminal_after_collector_failure(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    ingestion = IngestionRepository(runtime)
    history = OptionHistoryRepository(runtime)
    ingestion.register_source("robinhood", name="Robinhood", family="broker", kind="option_chain")
    slot = datetime(2026, 7, 20, 15, 0, tzinfo=UTC)
    run_id = ingestion.start_run("robinhood", "option_history_full")
    assert history.claim_slot(source_id="robinhood", symbol="QQQ", slot_at=slot, run_id=run_id)
    history.fail_capture(source_id="robinhood", symbol="QQQ", slot_at=slot, run_id=run_id, error=TimeoutError("provider timeout"))
    with runtime.read() as connection:
        row = connection.execute(
            "SELECT capture_state FROM raw.option_capture_generation WHERE ingest_run_id = %s", [run_id]
        ).fetchone()
    assert row["capture_state"] == "failed"
    runtime.close()
