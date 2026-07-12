from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.data_access import load_table_panel_data
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.options_analysis import refresh_options_radar
from investment_panel.database.outcomes import OutcomeRepository
from investment_panel.database.runtime import DatabaseRuntime


def test_actionable_decision_keeps_one_incremental_outcome_without_mark_history(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    ingestion = IngestionRepository(runtime)
    ingestion.register_source("outcome-test", name="Outcome", family="test", kind="option_chain")
    try:
        _snapshot(ingestion, datetime(2026, 7, 11, 12, tzinfo=UTC), 5.0)
        radar = refresh_options_radar(runtime, source_id="outcome-test", code_version="outcome-test")
        assert radar["decisions"] == 1
        _snapshot(ingestion, datetime(2026, 7, 12, 12, tzinfo=UTC), 6.0)
        _snapshot(ingestion, datetime(2026, 7, 16, 12, tzinfo=UTC), 10.0)

        first = OutcomeRepository(runtime).refresh(now=datetime(2026, 7, 17, 12, tzinfo=UTC))
        assert first["outcomes_updated"] == 1
        with runtime.read() as connection:
            outcome = connection.execute("SELECT * FROM analysis.option_outcome").fetchone()
        assert outcome["return_1d"] == pytest.approx(0.2)
        assert outcome["return_5d"] == pytest.approx(1.0)
        assert outcome["peak_return"] == pytest.approx(1.0)
        assert outcome["time_to_2x_days"] == 5

        _snapshot(ingestion, datetime(2026, 7, 18, 12, tzinfo=UTC), 15.0)
        OutcomeRepository(runtime).refresh(now=datetime(2026, 7, 18, 13, tzinfo=UTC))
        with runtime.read() as connection:
            outcome = connection.execute("SELECT * FROM analysis.option_outcome").fetchone()
            counts = connection.execute(
                "SELECT (SELECT count(*) FROM analysis.option_outcome) AS outcomes, "
                "(SELECT count(*) FROM analysis.decision) AS decisions"
            ).fetchone()
        assert outcome["return_1d"] == pytest.approx(0.2)
        assert outcome["peak_return"] == pytest.approx(2.0)
        assert counts == {"outcomes": 1, "decisions": 1}
        mark = load_table_panel_data(
            {"database": {"url": migrated_postgres_dsn}}, "candidate_event_mark"
        ).rows("candidate_event_mark")[0]
        attribution = load_table_panel_data(
            {"database": {"url": migrated_postgres_dsn}}, "candidate_event_attribution"
        ).rows("candidate_event_attribution")[0]
        assert mark["current_return"] == pytest.approx(2.0)
        assert mark["max_return_since_alert"] == pytest.approx(2.0)
        assert attribution["label"] == "winner_2x"

        with runtime.transaction() as connection:
            connection.execute("UPDATE analysis.option_outcome SET peak_return = 5")
            connection.execute("UPDATE analysis.decision SET state = 'FIRE'")
        assert load_table_panel_data(
            {"database": {"url": migrated_postgres_dsn}}, "missed_winner_event"
        ).rows("missed_winner_event") == []
        with runtime.transaction() as connection:
            connection.execute("UPDATE analysis.decision SET state = 'WATCH'")
        missed = load_table_panel_data(
            {"database": {"url": migrated_postgres_dsn}}, "missed_winner_event"
        ).rows("missed_winner_event")
        assert missed[0]["prior_state"] == "WATCH"
        assert missed[0]["outcome_type"] == "5x"
    finally:
        runtime.close()


def test_rejected_contracts_are_aggregated_not_retained(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    ingestion = IngestionRepository(runtime)
    ingestion.register_source("reject-test", name="Reject", family="test", kind="option_chain")
    try:
        run_id = ingestion.start_run("reject-test", "option_quotes")
        ingestion.store_option_snapshot(
            run_id,
            source_id="reject-test",
            observed_at=datetime(2026, 7, 11, 12, tzinfo=UTC),
            market_session="premarket",
            universe="test",
            rows=[
                _row(5.0, bid=4.8, ask=5.2, open_interest=1500),
                {**_row(2.0, bid=0.1, ask=3.9, open_interest=1), "strike": 250, "contract_symbol": "NVDA260821C00250000"},
            ],
        )
        ingestion.finish_run(run_id, "succeeded")
        result = refresh_options_radar(runtime, source_id="reject-test", code_version="compact-test")
        assert result["decisions"] == 1
        with runtime.read() as connection:
            counts = connection.execute(
                """
                SELECT (SELECT count(*) FROM analysis.decision) AS decisions,
                       (SELECT count(*) FROM analysis.option_feature) AS features,
                       (SELECT sum(reject_count) FROM analysis.reject_summary) AS rejects
                """
            ).fetchone()
        assert counts["decisions"] == 1
        assert counts["features"] == 1
        assert counts["rejects"] >= 1
    finally:
        runtime.close()


def _snapshot(repository: IngestionRepository, observed_at: datetime, mid: float) -> None:
    run_id = repository.start_run("outcome-test", "option_quotes")
    repository.store_option_snapshot(
        run_id,
        source_id="outcome-test",
        observed_at=observed_at,
        market_session="premarket",
        universe="test",
        rows=[_row(mid, bid=mid - 0.2, ask=mid + 0.2, open_interest=1500)],
    )
    repository.finish_run(run_id, "succeeded")


def _row(mid: float, *, bid: float, ask: float, open_interest: int) -> dict[str, object]:
    return {
        "symbol": "NVDA", "expiration": "2026-08-21", "strike": 180,
        "option_type": "call", "contract_symbol": "NVDA260821C00180000",
        "underlying_price": 175, "bid": bid, "ask": ask, "mid": mid,
        "volume": 120, "open_interest": open_interest, "iv": 0.4, "delta": 0.4,
    }
