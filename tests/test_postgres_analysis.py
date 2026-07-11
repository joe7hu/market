from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import deps
from app.routers.options import router as options_router
from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.migrations import upgrade_database
from investment_panel.database.options_analysis import published_options_radar_rows, refresh_options_radar
from investment_panel.database.runtime import DatabaseRuntime


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
        market_session="premarket",
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
    assert opportunity["state"] == "FIRE"
    assert opportunity["tier"] == "Exceptional"
    assert opportunity["quality_status"] == "complete"
    assert opportunity["spread_pct"] == pytest.approx(0.08)
    assert opportunity["raw"]["feature_version"] == "option-core-v1"
    assert published_options_radar_rows(runtime, "option_radar_summary") == [
        {"symbol": "NVDA", "ticker": "NVDA", "fire_count": 1, "setup_count": 0, "watch_count": 0, "reject_count": 0}
    ]


def test_incremental_refresh_preserves_older_symbols_in_complete_publication(analysis_context) -> None:
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
            market_session="premarket",
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
    assert features.json()["rows"][0]["raw"]["feature_version"] == "option-core-v1"
    assert candidates.json()["rows"][0]["state"] == "FIRE"
