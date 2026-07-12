from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from app import deps
from app.routers.options import router as options_router
from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.actions import ActionRepository
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.migrations import upgrade_database
from investment_panel.database.options_analysis import published_options_radar_rows, refresh_options_radar
from investment_panel.database.outcomes import OutcomeRepository
from investment_panel.database.runtime import DatabaseRuntime


def test_no_regular_snapshot_replaces_legacy_contract_with_explicit_empty_publication(postgres_dsn: str) -> None:
    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    try:
        result = refresh_options_radar(runtime, code_version="empty-contract-test")
        summary = published_options_radar_rows(runtime, "option_radar_summary")
        assert result["reason"] == "legacy_publication_replaced"
        assert len(summary) == 1
        assert summary[0]["contract_version"] == 2
        assert summary[0]["degraded_reason"] == "no_complete_regular_session_publication"
        assert published_options_radar_rows(runtime, "option_radar_opportunity") == []
    finally:
        runtime.close()


@pytest.fixture
def analysis_context(postgres_dsn: str):
    upgrade_database(postgres_dsn)
    runtime = DatabaseRuntime(postgres_dsn, min_size=1, max_size=6)
    runtime.open()
    ingestion = IngestionRepository(runtime)
    analysis = AnalysisRepository(runtime)
    ingestion.register_source("test-options", name="Test", family="test", kind="option_chain")
    ingest_run = ingestion.start_run("test-options", "option_quotes")
    observed_at = datetime(2026, 7, 11, 12, 15, tzinfo=UTC)
    snapshot = ingestion.store_option_snapshot(
        ingest_run,
        source_id="test-options",
        observed_at=observed_at,
        market_session="regular",
        universe="test",
        rows=[
            {
                "symbol": "NVDA",
                "expiration": "2026-08-21",
                "strike": 180,
                "option_type": "call",
                "contract_symbol": "NVDA260821C00180000",
                "underlying_price": 175,
                "bid": 4.8,
                "ask": 5.2,
                "mid": 5.0,
                "volume": 120,
                "open_interest": 1500,
                "iv": 0.41,
                "delta": 0.43,
            }
        ],
    )
    ingestion.finish_run(ingest_run, "succeeded")
    with runtime.read() as connection:
        identifiers = connection.execute(
            "SELECT q.contract_id, c.underlying_instrument_id "
            "FROM raw.option_quote q JOIN catalog.option_contract c ON c.id = q.contract_id"
        ).fetchone()
    try:
        yield {
            "runtime": runtime,
            "analysis": analysis,
            "snapshot_id": snapshot["snapshot_id"],
            "contract_id": int(identifiers["contract_id"]),
            "instrument_id": int(identifiers["underlying_instrument_id"]),
            "observed_at": observed_at,
        }
    finally:
        runtime.close()


def _start_run(repository: AnalysisRepository, suffix: str = "a"):
    return repository.start_run(
        "options-radar",
        input_cutoff=datetime(2026, 7, 11, 12, 15, tzinfo=UTC),
        code_version=f"test-{suffix}",
        inputs={"snapshot": suffix},
        feature_versions={"option": "v1"},
    )


def test_analysis_keeps_features_decisions_and_publication_separate(analysis_context, postgres_dsn: str) -> None:
    repository: AnalysisRepository = analysis_context["analysis"]
    run_id = _start_run(repository)
    feature_id = repository.store_option_feature(
        run_id,
        snapshot_id=analysis_context["snapshot_id"],
        contract_id=analysis_context["contract_id"],
        quote_observed_at=analysis_context["observed_at"],
        feature_version="v1",
        values={
            "dte": 41,
            "spread_pct": 0.08,
            "liquidity_score": 92,
            "convexity_score": 74,
            "required_2x_price": 185,
            "required_5x_price": 200,
            "required_10x_price": 225,
            "required_move_pct": 0.286,
            "metrics": {"breakeven": 185},
        },
    )
    decision_id = repository.store_option_decision(
        run_id,
        decision_key="NVDA:2026-08-21:180:call",
        instrument_id=analysis_context["instrument_id"],
        contract_id=analysis_context["contract_id"],
        snapshot_id=analysis_context["snapshot_id"],
        quote_observed_at=analysis_context["observed_at"],
        state="SETUP",
        score=82,
        rank=1,
        inputs={"feature_id": feature_id},
        reasons=["liquid_contract", "convexity_supported"],
        blockers=["wait_for_entry"],
        details={"premium_mid": 5, "buy_under": 4.8, "predicted_p2x": 0.31, "tier": "Strong"},
    )
    repository.finish_run(run_id, "succeeded", {"decisions": 1})
    publication_id = repository.publish(
        run_id,
        "options-radar",
        {
            "option_radar_opportunity": [
                {
                    "opportunity_id": str(decision_id),
                    "symbol": "NVDA",
                    "state": "SETUP",
                    "score": 82,
                    "premium_mid": 5,
                }
            ],
            "option_radar_summary": [{"symbol": "NVDA", "setup_count": 1, "fire_count": 0}],
        },
        validation={"row_counts_match": True},
    )
    assert repository.publication_rows("options-radar", "option_radar_opportunity") == [
        {
            "opportunity_id": str(decision_id),
            "symbol": "NVDA",
            "state": "SETUP",
            "score": 82,
            "premium_mid": 5,
        }
    ]

    with closing(psycopg.connect(postgres_dsn)) as connection:
        counts = connection.execute(
            "SELECT (SELECT count(*) FROM raw.option_quote), "
            "(SELECT count(*) FROM analysis.option_feature), "
            "(SELECT count(*) FROM analysis.option_decision), "
            "(SELECT count(*) FROM app.publication_item WHERE publication_id = %s)",
            [publication_id],
        ).fetchone()
    assert counts == (1, 1, 1, 2)


def test_publication_validation_failure_never_exposes_partial_state(analysis_context, postgres_dsn: str) -> None:
    repository: AnalysisRepository = analysis_context["analysis"]
    first_run = _start_run(repository, "first")
    repository.finish_run(first_run, "succeeded")
    first_id = repository.publish(
        first_run,
        "today",
        {"daily_brief": [{"stable_key": "brief", "headline": "First complete brief"}]},
    )
    second_run = _start_run(repository, "second")
    repository.finish_run(second_run, "succeeded")

    with pytest.raises(ValueError, match="duplicate publication key"):
        repository.publish(
            second_run,
            "today",
            {"daily_brief": [{"stable_key": "brief"}, {"stable_key": "brief"}]},
        )

    assert repository.publication_rows("today", "daily_brief") == [
        {"stable_key": "brief", "headline": "First complete brief"}
    ]
    with closing(psycopg.connect(postgres_dsn)) as connection:
        publications = connection.execute(
            "SELECT id, status FROM app.publication WHERE scope = 'today'"
        ).fetchall()
    assert publications == [(first_id, "published")]


def test_concurrent_publications_serialize_to_one_visible_snapshot(analysis_context, postgres_dsn: str) -> None:
    repository: AnalysisRepository = analysis_context["analysis"]
    runs = [_start_run(repository, suffix) for suffix in ("one", "two")]
    for run_id in runs:
        repository.finish_run(run_id, "succeeded")

    def publish(index: int) -> None:
        repository.publish(
            runs[index],
            "watchlist",
            {"universe_screen": [{"symbol": "NVDA", "generation": index}]},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(publish, range(2)))

    visible = repository.publication_rows("watchlist", "universe_screen")
    assert len(visible) == 1
    assert visible[0]["generation"] in {0, 1}
    with closing(psycopg.connect(postgres_dsn)) as connection:
        statuses = connection.execute(
            "SELECT status, count(*) FROM app.publication WHERE scope = 'watchlist' GROUP BY status"
        ).fetchall()
    assert sorted(statuses) == [("published", 1), ("superseded", 1)]


def test_postgresql_options_radar_builds_versioned_features_decisions_and_read_models(analysis_context) -> None:
    runtime: DatabaseRuntime = analysis_context["runtime"]

    result = refresh_options_radar(runtime, source_id="test-options", code_version="test-engine")

    assert result["status"] == "ok"
    assert result["option_features"] == 1
    assert result["decisions"] == 1
    assert result["actionable"] == 1
    opportunity = published_options_radar_rows(runtime, "option_radar_opportunity")[0]
    assert opportunity["symbol"] == "NVDA"
    assert opportunity["state"] == "WATCH"
    assert opportunity["tier"] == "setup"
    assert opportunity["structure"] == "long_call"
    assert opportunity["contract_version"] == 2
    assert opportunity["quality_status"] == "complete"
    assert opportunity["spread_pct"] == pytest.approx(0.08)
    assert opportunity["raw"]["feature_version"] == "option-professional-v2"
    summary = published_options_radar_rows(runtime, "option_radar_summary")
    assert len(summary) == 1
    assert summary[0]["stable_key"] == "global"
    assert summary[0]["scanned_contracts"] == result["option_features"]
    assert summary[0]["shortlist_count"] == 1
    assert summary[0]["shadow_only"] is True
    assert published_options_radar_rows(runtime, "option_radar_symbol_summary") == [
        {"symbol": "NVDA", "ticker": "NVDA", "fire_count": 0, "setup_count": 0, "watch_count": 1, "reject_count": 0}
    ]


def test_options_radar_captures_cash_secured_put_with_collateral_context(analysis_context) -> None:
    runtime: DatabaseRuntime = analysis_context["runtime"]
    ingestion = IngestionRepository(runtime)
    observed_at = datetime(2026, 7, 12, 15, 0, tzinfo=UTC)
    option_run = ingestion.start_run("test-options", "option_quotes")
    ingestion.store_option_snapshot(
        option_run,
        source_id="test-options",
        observed_at=observed_at,
        market_session="regular",
        universe="test",
        rows=[
            {
                "symbol": "NVDA",
                "expiration": "2026-08-21",
                "strike": 160,
                "option_type": "put",
                "contract_symbol": "NVDA260821P00160000",
                "underlying_price": 175,
                "bid": 3.0,
                "ask": 3.2,
                "mid": 3.1,
                "volume": 150,
                "open_interest": 2500,
                "iv": 0.38,
                "delta": -0.22,
            }
        ],
    )
    ingestion.finish_run(option_run, "succeeded")
    ingestion.register_source("test-broker", name="Broker", family="broker", kind="account")
    broker_run = ingestion.start_run("test-broker", "broker_account")
    with runtime.transaction() as connection:
        instrument_id = analysis_context["instrument_id"]
        connection.execute(
            """
            INSERT INTO raw.price_bar
                (instrument_id, source_id, ingest_run_id, interval, trading_date,
                 observed_at, close, volume)
            SELECT %s, 'test-options', %s, '1d', (%s::date - value),
                   %s - make_interval(days => value), 150 + value * 0.1, 1000000
            FROM generate_series(0, 79) value
            """,
            [instrument_id, option_run, observed_at, observed_at],
        )
        connection.execute(
            """
            INSERT INTO raw.fundamental_observation
                (instrument_id, source_id, ingest_run_id, metric_set, period_end,
                 observed_at, values)
            VALUES (%s, 'test-options', %s, 'company_quality', '2025-12-31', %s, %s)
            """,
            [instrument_id, option_run, observed_at, Jsonb({"quality_status": "acceptable"})],
        )
        connection.execute(
            """
            INSERT INTO raw.broker_account_snapshot
                (source_id, ingest_run_id, account_key, observed_at, currency,
                 net_liquidation, buying_power, cash_balance)
            VALUES ('test-broker', %s, 'paper', %s, 'USD', 500000, 100000, 100000)
            """,
            [broker_run, observed_at],
        )
    ingestion.finish_run(broker_run, "succeeded")

    result = refresh_options_radar(runtime, source_id="test-options", code_version="csp-test")

    assert result["cash_secured_puts"] == 1
    opportunities = published_options_radar_rows(runtime, "option_radar_opportunity")
    csp = next(row for row in opportunities if row["structure"] == "cash_secured_put")
    assert csp["state"] == "SETUP"
    assert csp["secured_cash"] == pytest.approx(15700.65)
    assert csp["effective_assignment_price"] == pytest.approx(157.0065)
    assert csp["probability_assignment"] == pytest.approx(0.22)
    assert csp["details"]["max_contracts"] == 1
    assert csp["blockers"] == []
    summary = published_options_radar_rows(runtime, "option_radar_summary")[0]
    assert summary["cash_secured_put_count"] == 1
    assert summary["shortlist_count"] <= 10
    detail = AnalysisRepository(runtime).option_signal_detail(csp["decision_id"])
    assert detail is not None
    assert detail["structure"] == "cash_secured_put"
    assert detail["no_trade_baseline"]["expected_value"] == 0
    staged = ActionRepository(runtime).stage_option_paper_entry(
        decision_id=csp["decision_id"],
        idempotency_key="csp-nvda-1",
        expected_contract_version=2,
        limit_price=3.1,
    )
    replay = ActionRepository(runtime).stage_option_paper_entry(
        decision_id=csp["decision_id"],
        idempotency_key="csp-nvda-1",
        expected_contract_version=2,
        limit_price=3.1,
    )
    assert staged["reserved_collateral"] == pytest.approx(15700.65)
    assert replay["paper_order_id"] == staged["paper_order_id"]
    assert replay["idempotent_replay"] is True
    mark_run = ingestion.start_run("test-options", "option_quotes")
    ingestion.store_option_snapshot(
        mark_run,
        source_id="test-options",
        observed_at=datetime(2026, 7, 17, 15, 0, tzinfo=UTC),
        market_session="regular",
        universe="test",
        rows=[
            {
                "symbol": "NVDA", "expiration": "2026-08-21", "strike": 160,
                "option_type": "put", "contract_symbol": "NVDA260821P00160000",
                "underlying_price": 180, "bid": 1.4, "ask": 1.5, "mid": 1.45,
                "volume": 100, "open_interest": 2500, "iv": 0.32, "delta": -0.15,
            }
        ],
    )
    ingestion.finish_run(mark_run, "succeeded")
    OutcomeRepository(runtime).refresh(now=datetime(2026, 7, 18, 15, 0, tzinfo=UTC))
    with runtime.read() as connection:
        outcome = connection.execute(
            "SELECT current_return, return_5d, strike_touched FROM analysis.option_outcome WHERE decision_id = %s",
            [csp["decision_id"]],
        ).fetchone()
    assert outcome["current_return"] > 0
    assert outcome["return_5d"] > 0
    assert outcome["strike_touched"] is False


def test_options_radar_builds_same_snapshot_call_debit_spread(analysis_context) -> None:
    runtime: DatabaseRuntime = analysis_context["runtime"]
    ingestion = IngestionRepository(runtime)
    observed_at = datetime(2026, 7, 12, 15, 0, tzinfo=UTC)
    run_id = ingestion.start_run("test-options", "option_quotes")
    ingestion.store_option_snapshot(
        run_id,
        source_id="test-options",
        observed_at=observed_at,
        market_session="regular",
        universe="test",
        rows=[
            {"symbol": "NVDA", "expiration": "2026-08-21", "strike": 175, "option_type": "call", "underlying_price": 180, "bid": 7.8, "ask": 8.0, "mid": 7.9, "volume": 200, "open_interest": 2000, "iv": .35, "delta": .58},
            {"symbol": "NVDA", "expiration": "2026-08-21", "strike": 190, "option_type": "call", "underlying_price": 180, "bid": 3.8, "ask": 4.0, "mid": 3.9, "volume": 180, "open_interest": 1800, "iv": .34, "delta": .35},
        ],
    )
    with runtime.transaction() as connection:
        connection.execute(
            """
            INSERT INTO raw.price_bar
                (instrument_id, source_id, ingest_run_id, interval, trading_date,
                 observed_at, close, volume)
            SELECT %s, 'test-options', %s, '1d', (%s::date - 100 + value),
                   %s - make_interval(days => 100 - value), 100 + value * 0.8, 1000000
            FROM generate_series(0, 100) value
            """,
            [analysis_context["instrument_id"], run_id, observed_at, observed_at],
        )
    ingestion.finish_run(run_id, "succeeded")

    result = refresh_options_radar(runtime, source_id="test-options", code_version="spread-test")

    assert result["empirical_long_options"] == 2
    assert result["call_debit_spreads"] >= 1
    spread = next(
        row for row in published_options_radar_rows(runtime, "option_radar_opportunity")
        if row["structure"] == "call_debit_spread"
    )
    assert spread["state"] == "SETUP"
    assert spread["max_loss"] == pytest.approx(420)
    assert spread["max_profit"] == pytest.approx(1080)
    assert spread["expected_value"] > 0
    assert spread["details"]["same_snapshot_legs"] is True
    mark_at = datetime(2026, 7, 13, 15, 0, tzinfo=UTC)
    mark_run = ingestion.start_run("test-options", "option_quotes")
    ingestion.store_option_snapshot(
        mark_run, source_id="test-options", observed_at=mark_at, market_session="regular", universe="test",
        rows=[
            {"symbol": "NVDA", "expiration": "2026-08-21", "strike": 175, "option_type": "call", "underlying_price": 182, "bid": 9.0, "ask": 9.2, "mid": 9.1, "volume": 200, "open_interest": 2000, "iv": .35, "delta": .58},
            {"symbol": "NVDA", "expiration": "2026-08-21", "strike": 190, "option_type": "call", "underlying_price": 182, "bid": 5.3, "ask": 5.5, "mid": 5.4, "volume": 180, "open_interest": 1800, "iv": .34, "delta": .35},
        ],
    )
    ingestion.finish_run(mark_run, "succeeded")
    OutcomeRepository(runtime).refresh(now=datetime(2026, 7, 13, 16, 0, tzinfo=UTC))
    with runtime.read() as connection:
        outcome = connection.execute(
            "SELECT current_return FROM analysis.option_outcome WHERE decision_id = %s",
            [spread["decision_id"]],
        ).fetchone()
    assert outcome["current_return"] == pytest.approx((9.0 - 5.5) / 4.2 - 1)


def test_options_radar_applies_promoted_strategy_parameters(analysis_context) -> None:
    runtime: DatabaseRuntime = analysis_context["runtime"]
    refresh_options_radar(runtime, source_id="test-options", code_version="base")
    with runtime.transaction() as connection:
        base = connection.execute(
            "SELECT id, parameters FROM analysis.strategy_revision "
            "WHERE strategy_key = 'options-radar-core' AND status = 'active'"
        ).fetchone()
        connection.execute(
            "UPDATE analysis.strategy_revision SET status = 'superseded' WHERE id = %s",
            [base["id"]],
        )
        candidate = connection.execute(
            """
            INSERT INTO analysis.strategy_revision
                (strategy_key, revision, name, status, parameters, supersedes_id,
                 authority_group, promoted_at)
            VALUES ('options-radar-core__agent_test', 1, 'tight spread', 'active', %s, %s,
                    'options-radar-core', now())
            RETURNING id
            """,
            [
                Jsonb({"gates": {"max_spread_pct": 0.25}, "reject_spread_pct": 0.05}),
                base["id"],
            ],
        ).fetchone()

    result = refresh_options_radar(runtime, source_id="test-options", code_version="promoted")

    assert result["decisions"] == 0
    assert published_options_radar_rows(runtime, "option_radar_opportunity") == []
    assert published_options_radar_rows(runtime, "option_radar_symbol_summary")[0]["reject_count"] == 1
    with runtime.read() as connection:
        run = connection.execute(
            "SELECT strategy_revision_id FROM analysis.run ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    assert run["strategy_revision_id"] == candidate["id"]


def test_options_publication_rejects_run_from_superseded_strategy(analysis_context) -> None:
    runtime: DatabaseRuntime = analysis_context["runtime"]
    refresh_options_radar(runtime, source_id="test-options", code_version="base")
    with runtime.transaction() as connection:
        base = connection.execute(
            "SELECT id FROM analysis.strategy_revision "
            "WHERE authority_group = 'options-radar-core' AND status = 'active'"
        ).fetchone()
    stale_run = analysis_context["analysis"].start_run(
        "options-radar",
        input_cutoff=analysis_context["observed_at"],
        code_version="stale-strategy",
        inputs={"strategy": "stale"},
        strategy_revision_id=base["id"],
    )
    with runtime.transaction() as connection:
        connection.execute(
            "UPDATE analysis.strategy_revision SET status = 'superseded' WHERE id = %s",
            [base["id"]],
        )
        connection.execute(
            "INSERT INTO analysis.strategy_revision "
            "(strategy_key, revision, name, status, parameters, supersedes_id, authority_group, promoted_at) "
            "VALUES ('options-radar-core__agent_new', 1, 'new', 'active', %s, %s, "
            "'options-radar-core', now())",
            [Jsonb({"gates": {"max_spread_pct": 0.05}}), base["id"]],
        )

    with pytest.raises(ValueError, match="strategy authority changed"):
        analysis_context["analysis"].publish(
            stale_run,
            "options-radar",
            {"option_radar_opportunity": []},
            strategy_root_key="options-radar-core",
        )


def test_incremental_refresh_preserves_older_symbols_in_complete_publication(analysis_context, postgres_dsn: str) -> None:
    runtime: DatabaseRuntime = analysis_context["runtime"]
    ingestion = IngestionRepository(runtime)
    for key, observed_at, symbol, strike in (
        ("aapl-old", datetime(2026, 7, 11, 12, 10, tzinfo=UTC), "AAPL", 220),
        ("nvda-new", datetime(2026, 7, 11, 12, 20, tzinfo=UTC), "NVDA", 185),
    ):
        ingest_run = ingestion.start_run("test-options", "option_quotes", source_run_key=key)
        ingestion.store_option_snapshot(
            ingest_run,
            source_id="test-options",
            observed_at=observed_at,
                market_session="regular",
            universe="incremental",
            rows=[{
                "symbol": symbol, "expiration": "2026-08-21", "strike": strike,
                "option_type": "call", "contract_symbol": f"{symbol}-{strike}",
                "underlying_price": strike - 5, "bid": 4.8, "ask": 5.2, "mid": 5,
                "volume": 120, "open_interest": 1500, "iv": 0.4, "delta": 0.4,
            }],
        )
        ingestion.finish_run(ingest_run, "succeeded")

    result = refresh_options_radar(
        runtime, source_id="test-options", symbols=["NVDA"], code_version="incremental-test"
    )

    assert result["status"] == "ok"
    opportunities = published_options_radar_rows(runtime, "option_radar_opportunity")
    assert {row["symbol"] for row in opportunities} == {"AAPL", "NVDA"}
    from app.data_access import load_table_panel_data

    chain = load_table_panel_data({"database": {"url": postgres_dsn}}, "options_chain").rows("options_chain")
    assert {row["symbol"] for row in chain} == {"AAPL", "NVDA"}
    assert {float(row["strike"]) for row in chain if row["symbol"] == "NVDA"} == {185.0}


def test_options_api_reads_only_published_postgresql_generation(
    analysis_context,
    postgres_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime: DatabaseRuntime = analysis_context["runtime"]
    refresh_options_radar(runtime, source_id="test-options", code_version="api-test")
    monkeypatch.setattr(deps, "load_config", lambda: {"database": {"url": postgres_dsn}})
    application = FastAPI()
    application.include_router(options_router)

    with TestClient(application) as client:
        opportunities = client.get("/api/option-radar-opportunities")
        snapshots = client.get("/api/option-snapshot")
        features = client.get("/api/option-features")
        candidates = client.get("/api/candidate-events")

    assert opportunities.status_code == 200
    assert opportunities.json()["rows"][0]["symbol"] == "NVDA"
    assert snapshots.json()["rows"][0]["contract_id"] == str(analysis_context["contract_id"])
    assert features.json()["rows"][0]["raw"]["feature_version"] == "option-professional-v2"
    assert candidates.json()["rows"][0]["state"] == "WATCH"
