from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.data_access.loaders import load_table_panel_data
from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.options_analysis import refresh_options_radar
from investment_panel.database.outcomes import OutcomeRepository
from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.database.strategy_learning import StrategyLearningRepository
from psycopg.types.json import Jsonb


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
        with runtime.transaction() as connection:
            decision_id = connection.execute("SELECT id FROM analysis.decision").fetchone()["id"]
            postmortem_id = connection.execute(
                "INSERT INTO analysis.agent_task (decision_id, task_kind, status, request) "
                "VALUES (%s, 'option_postmortem', 'completed', %s) RETURNING id",
                [decision_id, Jsonb({"source": "immature-outcome-test"})],
            ).fetchone()["id"]
        StrategyLearningRepository(runtime).materialize_postmortem(
            str(postmortem_id), {"proposed_parameter_changes": {"min_dte": 30}}
        )
        with runtime.read() as connection:
            evaluation = connection.execute(
                "SELECT metrics FROM analysis.strategy_evaluation "
                "WHERE evaluation_type = 'backtest'"
            ).fetchone()
        assert evaluation["metrics"]["baseline"]["sample_size"] == 0

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


def test_generic_outcome_refresh_skips_options_history_v3_shadows(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    ingestion = IngestionRepository(runtime)
    analysis = AnalysisRepository(runtime)
    ingestion.register_source("v3-outcome-test", name="V3 Outcome", family="test", kind="option_chain")
    try:
        run_id = ingestion.start_run("v3-outcome-test", "option_quotes")
        snapshot = ingestion.store_option_snapshot(
            run_id,
            source_id="v3-outcome-test",
            observed_at=datetime(2026, 7, 11, 12, tzinfo=UTC),
            market_session="regular",
            universe="test",
            rows=[_row(5.0, bid=4.8, ask=5.2, open_interest=1500)],
        )
        ingestion.finish_run(run_id, "succeeded")
        with runtime.read() as connection:
            ids = connection.execute(
                """
                SELECT decision_instrument.id AS instrument_id, quote.contract_id
                FROM raw.option_quote quote
                JOIN catalog.option_contract contract ON contract.id = quote.contract_id
                JOIN catalog.instrument decision_instrument ON decision_instrument.id = contract.underlying_instrument_id
                WHERE quote.snapshot_id = %s
                LIMIT 1
                """,
                [snapshot["snapshot_id"]],
            ).fetchone()
        analysis_run = analysis.start_run(
            "option_history_v3",
            input_cutoff=datetime(2026, 7, 11, 12, tzinfo=UTC),
            code_version="test",
            inputs={"test": "v3-outcome"},
        )
        decision_id = analysis.store_option_decision(
            analysis_run,
            decision_key="v3-outcome",
            instrument_id=ids["instrument_id"],
            contract_id=ids["contract_id"],
            snapshot_id=snapshot["snapshot_id"],
            quote_observed_at=datetime(2026, 7, 11, 12, tzinfo=UTC),
            state="WATCH",
            score=1.0,
            rank=1,
            inputs={"test": "v3"},
            details={"structure": "long_call", "premium_mid": 5.0, "quality_status": "ok"},
        )
        with runtime.transaction() as connection:
            connection.execute(
                """
                UPDATE analysis.option_decision
                SET model_version = 'history-v3-price-shape-r3', paper_state = 'WATCH'
                WHERE decision_id = %s
                """,
                [decision_id],
            )
            connection.execute(
                """
                INSERT INTO analysis.shadow_trade
                    (decision_id, status, pending_entry_reason, entry_cohort_id, structure, source_kind)
                VALUES (%s, 'pending', 'test_pending', NULL, 'long_call', 'options_history_v3')
                """,
                [decision_id],
            )
        _snapshot(ingestion, datetime(2026, 7, 12, 12, tzinfo=UTC), 6.0, source_id="v3-outcome-test")
        result = OutcomeRepository(runtime).refresh(now=datetime(2026, 7, 13, 12, tzinfo=UTC))
        assert result["outcomes_updated"] == 0
        with runtime.read() as connection:
            assert connection.execute(
                "SELECT count(*) FROM analysis.option_outcome WHERE decision_id = %s",
                [decision_id],
            ).fetchone()["count"] == 0
    finally:
        runtime.close()


def test_rejected_contracts_are_aggregated_and_near_misses_retained(migrated_postgres_dsn: str) -> None:
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
            market_session="regular",
            universe="test",
            rows=[
                _row(5.0, bid=4.8, ask=5.2, open_interest=1500),
                {**_row(0.15, bid=0.1, ask=0.2, open_interest=1), "strike": 250, "contract_symbol": "NVDA260821C00250000"},
            ],
        )
        ingestion.finish_run(run_id, "succeeded")
        result = refresh_options_radar(runtime, source_id="reject-test", code_version="compact-test")
        assert result["decisions"] == 1
        with runtime.read() as connection:
            counts = connection.execute(
                """
                SELECT (SELECT count(*) FROM analysis.decision) AS decisions,
                       (SELECT count(*) FROM analysis.decision WHERE state <> 'REJECTED') AS actionable,
                       (SELECT count(*) FROM analysis.option_feature) AS features,
                       (SELECT sum(reject_count) FROM analysis.reject_summary) AS rejects
                """
            ).fetchone()
        assert counts["decisions"] == 2
        assert counts["actionable"] == 1
        assert counts["features"] == 2
        assert counts["rejects"] >= 1
    finally:
        runtime.close()


def _snapshot(repository: IngestionRepository, observed_at: datetime, mid: float, *, source_id: str = "outcome-test") -> None:
    run_id = repository.start_run(source_id, "option_quotes")
    repository.store_option_snapshot(
        run_id,
        source_id=source_id,
        observed_at=observed_at,
        market_session="regular",
        universe="test",
        rows=[_row(mid, bid=mid - 0.2, ask=mid + 0.2, open_interest=1500)],
    )
    repository.finish_run(run_id, "succeeded")


def _row(mid: float, *, bid: float, ask: float, open_interest: int) -> dict[str, object]:
    return {
        "symbol": "NVDA", "expiration": "2026-08-21", "strike": 180,
        "option_type": "call", "contract_symbol": "NVDA260821C00180000",
        "style": "american", "settlement": "physical",
        "deliverable_key": "nvda-standard", "standard_contract_verified": True,
        "underlying_price": 175, "bid": bid, "ask": ask, "mid": mid,
        "volume": 120, "open_interest": open_interest, "iv": 0.4, "delta": 0.4,
    }
