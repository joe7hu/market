from datetime import UTC, datetime
from fastapi.testclient import TestClient
import psycopg
from psycopg.types.json import Jsonb
from uuid import uuid4

from investment_panel.api import dependencies
from investment_panel.api.main import app
from conftest import typed_config


def test_research_workbench_read_routes_are_bounded_on_empty_database(migrated_postgres_dsn, monkeypatch):
    monkeypatch.setitem(
        app.dependency_overrides,
        dependencies.get_config,
        lambda: typed_config(migrated_postgres_dsn),
    )
    client = TestClient(app)

    routes = [
        "/api/research/strategies",
        "/api/research/predictions",
        "/api/research/prompts",
        "/api/research/experiments",
        "/api/research/events",
    ]
    responses = [client.get(route) for route in routes]

    assert all(response.status_code == 200 for response in responses)
    assert responses[0].json()["rows"] == []
    assert responses[1].json()["rows"] == []
    assert responses[2].json()["rows"] == []
    assert responses[3].json()["rows"] == []
    assert responses[4].json()["rows"] == []
    assert client.get("/api/research/strategies/1").status_code == 404
    assert client.get("/api/research/predictions/not-a-claim").status_code == 404
    assert client.get("/api/research/prompts/missing").status_code == 404
    assert client.get("/api/research/experiments/strategy-missing").status_code == 404


def test_research_workbench_reads_registered_revision_and_prompt_history(
    migrated_postgres_dsn, monkeypatch
):
    with psycopg.connect(migrated_postgres_dsn) as connection:
        revision_id = connection.execute(
            """
            INSERT INTO analysis.strategy_revision
                (strategy_key, revision, name, status, parameters, authority_group, artifact_id)
            VALUES (%s, 1, %s, 'candidate', %s, %s, %s)
            RETURNING id
            """,
            [
                "workbench_test_strategy",
                "Workbench test strategy",
                Jsonb({"threshold": 0.2}),
                "workbench-test",
                "workbench-test-artifact",
            ],
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO analysis.continuous_advisor_prompt_version
                (version, template, mutation_rationale)
            VALUES (%s, %s, %s)
            """,
            [
                "workbench-test-root",
                Jsonb({"base_instruction": "use the evidence cutoff", "forecast_instruction": "state the target event"}),
                "test prompt root",
            ],
        )
        connection.execute(
            """
            INSERT INTO analysis.continuous_advisor_prompt_version
                (version, parent_version, template, mutation_rationale)
            VALUES (%s, %s, %s, %s)
            """,
            [
                "workbench-test-prompt",
                "workbench-test-root",
                Jsonb({"forecast_instruction": "state the changed target event"}),
                "test prompt history",
            ],
        )
        connection.commit()

    monkeypatch.setitem(
        app.dependency_overrides,
        dependencies.get_config,
        lambda: typed_config(migrated_postgres_dsn),
    )
    client = TestClient(app)

    strategies = client.get("/api/research/strategies")
    strategy = client.get(f"/api/research/strategies/{revision_id}")
    prompts = client.get("/api/research/prompts")
    prompt = client.get("/api/research/prompts/workbench-test-prompt")
    artifact = client.get("/api/research/artifacts/workbench-test-artifact")

    assert strategies.status_code == 200
    assert strategies.json()["rows"][0]["strategy_key"] == "workbench_test_strategy"
    assert strategies.json()["rows"][0]["paper_order_count"] == 0
    assert strategy.status_code == 200
    assert strategy.json()["strategy_revision_id"] == revision_id
    assert strategy.json()["evaluations"] == []
    assert prompts.status_code == 200
    assert prompts.json()["rows"][0]["semantic_effective_hash"]
    assert prompt.status_code == 200
    assert prompt.json()["effective_template"]["base_instruction"] == "use the evidence cutoff"
    assert prompt.json()["effective_diff"] == {"forecast_instruction": {"before": "state the target event", "after": "state the changed target event"}}
    assert artifact.status_code == 200
    assert artifact.json()["content_available"] is False
    assert artifact.json()["records"][0]["source_relation"] == "analysis.strategy_revision"


def test_research_overview_exposes_evidence_backed_diagnostics(
    migrated_postgres_dsn, monkeypatch
):
    instrument_id = None
    decision_id = uuid4()
    now = datetime.now(UTC)
    with psycopg.connect(migrated_postgres_dsn) as connection:
        instrument_id = connection.execute(
            """
            INSERT INTO catalog.instrument (symbol, name, asset_class)
            VALUES (%s, %s, 'equity')
            RETURNING id
            """,
            [f"DIA{uuid4().hex[:6].upper()}", "Diagnostics test"],
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO analysis.ticker_decision (
                id, instrument_id, decision_revision, contract_version, as_of,
                published_at, input_hash, code_version, experiment_id,
                tactical, fundamental, capital_action, risk_policy,
                selected_expression
            )
            VALUES (%s, %s, 'diagnostics-v1', 'ticker-decision.v1', %s, %s, %s,
                    'test', 'diagnostics-test', %s, %s, %s, %s, %s)
            """,
            [
                decision_id,
                instrument_id,
                now,
                now,
                "d" * 64,
                Jsonb({"stance": "BULLISH"}),
                Jsonb({"stance": "BULLISH"}),
                Jsonb({"action": "BUY"}),
                Jsonb({"max_loss": 100}),
                Jsonb({"kind": "STOCK"}),
            ],
        )
        connection.execute(
            """
            INSERT INTO analysis.ticker_outcome (
                ticker_decision_id, horizon, horizon_sessions, state,
                measured_through, selected_expression, selected_return,
                stock_counterfactual_return, cash_return, market_return,
                available_at, metadata
            )
            VALUES (%s, 'TACTICAL', 20, 'resolved', %s, 'STOCK', 0.08,
                    0.03, 0.0, 0.01, %s, %s)
            """,
            [
                decision_id,
                now,
                now,
                Jsonb({
                    "plan_authority": "canonical",
                    "regime_slice": "risk_on",
                    "sector_slice": "technology",
                }),
            ],
        )
        connection.execute(
            """
            INSERT INTO analysis.ticker_outcome (
                ticker_decision_id, horizon, horizon_sessions, state,
                measured_through, selected_expression, selected_return,
                stock_counterfactual_return, cash_return, market_return,
                available_at, metadata
            )
            VALUES (%s, 'TACTICAL', 5, 'resolved', %s, 'STOCK', 0.90,
                    0.01, 0.0, 0.01, %s, %s)
                """,
                [
                decision_id,
                now,
                now,
                Jsonb({
                    "plan_authority": "canonical",
                    "plan_blocker": "ranking_publication_mismatch",
                }),
            ],
        )
        connection.execute(
            """
            INSERT INTO analysis.ticker_outcome (
                ticker_decision_id, horizon, horizon_sessions, state,
                measured_through, selected_expression, selected_return,
                stock_counterfactual_return, cash_return, market_return,
                available_at, metadata
            )
            VALUES (%s, 'TACTICAL', 1, 'resolved', %s, 'STOCK', 0.70,
                    0.02, 0.0, 0.01, %s, %s)
            """,
            [decision_id, now, now, Jsonb({"plan_authority": "legacy_or_invalid"})],
        )
        connection.execute(
            """
            INSERT INTO analysis.agent_run (
                provider, model, trigger, started_at, finished_at, input_tokens,
                output_tokens, cost_usd, status
            )
            VALUES ('test', 'test-model', 'test', %s, %s, 100, 50, 0.25, 'succeeded')
            """,
            [now, now],
        )
        connection.commit()

    monkeypatch.setitem(
        app.dependency_overrides,
        dependencies.get_config,
        lambda: typed_config(migrated_postgres_dsn),
    )
    payload = TestClient(app).get("/api/research/overview")

    assert payload.status_code == 200
    diagnostics = payload.json()["diagnostics"]
    assert diagnostics["quality_status"] == "partial"
    assert diagnostics["decision_quality"]["counts"]["comparable_counterfactual_count"] == 1
    assert diagnostics["decision_quality"]["mean_selection_delta"] == 0.05
    assert diagnostics["decision_quality"]["cases"][0]["assessment"] == "selected_outperformed_counterfactual"
    assert diagnostics["research_efficiency"]["agent_runs"]["run_count"] == 1
    assert diagnostics["drift_concentration"]["independent_episode_count"] == 1
