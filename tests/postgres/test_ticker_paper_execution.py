from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from psycopg.types.json import Jsonb

from investment_panel.core.decision import (
    InputLineage,
    bind_trade_plan,
    build_trade_plan,
    build_ticker_decision,
    rank_opportunities,
    trade_expression_identity,
)
from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.portfolio_ledger import replay_portfolio_at
from investment_panel.database.runtime import DatabaseRuntime, RuntimeProfile
from investment_panel.database.ticker_decisions import (
    HORIZON_SESSIONS,
    TickerDecisionRepository,
    paper_execution_for_plan,
)
from investment_panel.database.ticker_execution import TickerPaperExecutionRepository
from investment_panel.jobs.ticker_decisions import portfolio_impacts
from conftest import typed_config


_LEGACY_PEER_RETURN_QUERY = """
WITH entry_prices AS (
    SELECT DISTINCT ON (bar.instrument_id)
           bar.instrument_id, bar.close
    FROM raw.confirmed_price_bar bar
    JOIN catalog.instrument instrument ON instrument.id = bar.instrument_id
    WHERE instrument.symbol = ANY(%s)
      AND bar.interval = '1d'
      AND bar.trading_date <= %s
      AND bar.available_at <= %s
    ORDER BY bar.instrument_id, bar.trading_date DESC, bar.available_at DESC
), mark_prices AS (
    SELECT DISTINCT ON (bar.instrument_id)
           bar.instrument_id, bar.close
    FROM raw.confirmed_price_bar bar
    JOIN catalog.instrument instrument ON instrument.id = bar.instrument_id
    WHERE instrument.symbol = ANY(%s)
      AND bar.interval = '1d'
      AND bar.trading_date = %s
      AND bar.available_at <= %s
    ORDER BY bar.instrument_id, bar.available_at DESC
)
SELECT avg(mark_prices.close / entry_prices.close - 1) AS return
FROM entry_prices JOIN mark_prices USING (instrument_id)
WHERE entry_prices.close > 0
"""


def _plan(query_result: object) -> dict[str, object]:
    plan = query_result
    if isinstance(plan, str):
        plan = json.loads(plan)
    assert isinstance(plan, list)
    root = plan[0]
    assert isinstance(root, dict)
    result = root["Plan"]
    assert isinstance(result, dict)
    return result


def _relation_count(plan: object, relation: str) -> int:
    if isinstance(plan, list):
        return sum(_relation_count(node, relation) for node in plan)
    if not isinstance(plan, dict):
        return 0
    count = int(plan.get("Relation Name") == relation)
    return count + sum(_relation_count(value, relation) for value in plan.values())


def _actionable_tables(
    symbol: str,
    as_of: datetime,
    *,
    legs: list[dict[str, object]],
    structure: str = "long_call",
    option_lower_expectancy: float = 0.01,
) -> dict[str, list[dict[str, object]]]:
    available_at = (as_of - timedelta(minutes=5)).isoformat()
    return {
        "quotes": [{"symbol": symbol, "price": 100, "available_at": available_at, "confirmed": True}],
        "portfolio_summary": [{"symbol": symbol, "net_liquidation": 100_000, "available_at": available_at}],
        "decision_queue": [{
            "symbol": symbol, "stance": "BULLISH", "action": "BUY",
            "entry_low": 99, "entry_high": 101, "target_low": 110, "target_high": 120,
            "invalidation_price": 90, "conviction_tier": "STANDARD", "available_at": available_at,
            "scenarios": {
                "bear": {"probability": 0.2}, "base": {"probability": 0.5}, "bull": {"probability": 0.3},
            },
        }],
        "valuations": [{"symbol": symbol, "upside_pct": 0.01, "available_at": available_at}],
        "options_payoff_scenarios": [{
            "symbol": symbol, "structure": structure, "entry_price": 2.2, "max_loss": 220,
            "lower_confidence_expectancy": option_lower_expectancy,
            "net_expected_value_per_loss_dollar": option_lower_expectancy,
            "liquidity_score": 0.9, "fill_probability": 0.9, "expiration": "2026-10-16",
            "legs": legs, "available_at": available_at,
        }],
        "fundamentals": [{"symbol": symbol, "source": "sec_companyfacts", "available_at": available_at}],
        "earnings": [{"symbol": symbol, "available_at": available_at}],
        "ticker_benchmark_snapshot": [{"symbol": symbol, "available_at": available_at}],
        "macro": [{"symbol": symbol, "available_at": available_at}],
        "disclosures": [{"symbol": symbol, "available_at": available_at}],
        "short_interest": [{"symbol": symbol, "available_at": available_at}],
    }


def _publish_context(
    runtime: DatabaseRuntime,
    config,
    symbol: str,
    tables: dict[str, list[dict[str, object]]],
    as_of: datetime,
):
    seed = build_ticker_decision(symbol, tables, as_of=as_of)
    lineage = InputLineage(
        field="market_test", source_id="market-test", source_version="1",
        event_at=as_of - timedelta(minutes=5), available_at=as_of - timedelta(minutes=5), cutoff=as_of,
    )
    snapshot = seed.market_state_snapshot.model_copy(update={
        "snapshot_id": f"market-test:{symbol}",
        "publication_id": None,
        "as_of": as_of,
        "input_cutoff": as_of,
        "input_lineage": (lineage,),
        "availability": "available",
        "blockers": (),
    })
    analysis = AnalysisRepository(runtime)
    run_id = analysis.start_run(
        "market", input_cutoff=as_of, code_version=f"market-test-{symbol}",
        inputs={"source_lineage": [lineage.model_dump(mode="json")]},
        feature_versions={"market_state": "test"},
    )
    publication_id = analysis.publish(
        run_id, "market", {"market_state_snapshot": [snapshot.model_dump(mode="json")]},
        complete_run_summary={"snapshot_id": snapshot.snapshot_id},
    )
    with runtime.transaction() as connection:
        connection.execute(
            "UPDATE app.publication SET published_at = %s WHERE id = %s",
            [as_of, publication_id],
        )
    snapshot = snapshot.model_copy(update={"publication_id": str(publication_id)})
    replay = replay_portfolio_at(config, as_of)
    impacts = portfolio_impacts(seed, snapshot, str(publication_id), replay)
    decision = build_ticker_decision(
        symbol, tables, as_of=as_of, market_state_snapshot=snapshot,
        portfolio_impacts=impacts, risk_policy_snapshot=seed.risk_policy_snapshot,
        portfolio_replay=replay,
    )
    selected = decision.selected_expression
    impact = decision.portfolio_impacts[selected.kind]
    signal = {
        "signal_id": f"test-signal:{symbol}",
        "ticker": symbol,
        "opportunity_episode_id": decision.opportunity_episode_id,
        "decision_revision": decision.decision_revision,
        "instrument_state_snapshot_id": snapshot.snapshot_id,
        "target": "expected_return",
        "horizon": selected.horizon.value,
        "direction": selected.stance.value,
        "forecast_value": 0.1,
        "cohort_id": "test-cohort",
        "calibration_state": "calibrated_exact_cohort",
        "model_version": "test-model",
        "evaluation_stage": "out_of_sample",
        "as_of": as_of,
        "input_cutoff": as_of,
        "input_lineage": decision.input_lineage,
    }
    signal_payload = {
        **signal,
        "input_lineage": [item.model_dump(mode="json") for item in decision.input_lineage],
    }
    rank = rank_opportunities([{
        "ticker": symbol,
        "opportunity_episode_id": decision.opportunity_episode_id,
        "decision_revision": decision.decision_revision,
        "policy_version": decision.policy_version,
        "selected_expression_identity": trade_expression_identity(selected),
        "selected_expression_kind": selected.kind.value,
        "portfolio_impact_id": impact.impact_id,
        "risk_policy_version": decision.policy_version,
        "alpha_signal_id": signal["signal_id"],
        "alpha_signal": signal,
        "instrument_state_snapshot_id": snapshot.snapshot_id,
        "market_snapshot_id": snapshot.snapshot_id,
        "market_state_publication_id": str(publication_id),
        "cutoff": as_of,
        "input_lineage": decision.input_lineage,
        "expression": selected.model_dump(mode="json"),
        "portfolio_impact": impact.model_dump(mode="json"),
        "risk_policy_snapshot": decision.risk_policy_snapshot.model_dump(mode="json"),
        "execution_feasible": True,
        "lower_confidence_expected_gross_pnl": 1000.0,
        "expected_transaction_costs": 10.0,
        "tail_risk_penalty": 0.0,
        "portfolio_overlap_penalty": 0.0,
        "diversification_benefit": 0.0,
        "capital_at_risk": 1000.0,
    }], evaluated_universe_complete=True)
    plan = build_trade_plan(
        decision=decision,
        rank=rank[0],
        alpha_signal=signal,
    )
    rank_run = analysis.start_run(
        "ticker-opportunity-ranking", input_cutoff=as_of, code_version=f"rank-test-{symbol}",
        inputs={"ticker": symbol, "decision_revision": decision.decision_revision},
        feature_versions={"ranking": "test"},
    )
    rank_publication = analysis.publish(
        rank_run, "ticker-opportunity-ranking",
        {
            "opportunity_rank": [rank[0].model_dump(mode="json")],
            "alpha_signal": [signal_payload],
            "trade_plan": [plan.model_dump(mode="json")],
        },
        complete_run_summary={"ticker": symbol},
    )
    with runtime.transaction() as connection:
        connection.execute(
            "UPDATE app.publication SET published_at = %s WHERE id = %s",
            [as_of, rank_publication],
        )
    plan = plan.model_copy(update={"publication_id": str(rank_publication)})
    decision = bind_trade_plan(decision, plan)
    TickerDecisionRepository(runtime).publish(decision)
    return decision


def test_outcome_attribution_publication_is_full_and_replayable(
    migrated_postgres_dsn: str,
    monkeypatch,
) -> None:
    from investment_panel.jobs import ticker_decisions

    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        symbol = "ATTR"
        observed = datetime(2026, 8, 22, 14, tzinfo=UTC)
        config = typed_config(migrated_postgres_dsn)
        with runtime.transaction() as connection:
            connection.execute(
                "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES (%s, %s, 'equity')",
                [symbol, symbol],
            )
        decision = _publish_context(
            runtime,
            config,
            symbol,
            _actionable_tables(
                symbol,
                observed,
                legs=[{
                    "contract_id": 1, "option_type": "call", "side": "long", "strike": 105,
                    "bid": 2.0, "ask": 2.2, "bid_size": 10, "ask_size": 10,
                    "quote_time": observed, "expiration": "2026-10-16",
                }],
            ),
            observed,
        )
        assert decision.trade_plan is not None
        plan = decision.trade_plan
        assert plan.publication_id
        rank = {
            "ranking_publication_id": plan.publication_id,
            "rank_id": plan.rank_id,
            "alpha_signal_id": plan.alpha_signal_id,
            "portfolio_impact_id": plan.portfolio_impact_id,
            "market_snapshot_id": plan.market_snapshot_id,
            "market_state_publication_id": plan.market_state_publication_id,
            "selected_expression_kind": plan.selected_expression_kind.value,
            "selected_expression_identity": plan.selected_expression_identity,
            "opportunity_episode_id": plan.opportunity_episode_id,
            "decision_revision": plan.decision_revision,
            "policy_version": plan.policy_version,
        }
        with runtime.transaction() as connection:
            row = connection.execute(
                "SELECT id::text, input_manifest FROM analysis.ticker_decision WHERE decision_revision = %s",
                [decision.decision_revision],
            ).fetchone()
            assert row is not None
            manifest = dict(row["input_manifest"] or {})
            manifest.update({"opportunity_rank": rank, "alpha_signals": [{"signal_id": plan.alpha_signal_id}]})
            connection.execute(
                "UPDATE analysis.ticker_decision SET input_manifest = %s::jsonb WHERE id = %s::uuid",
                [json.dumps(manifest, default=str), row["id"]],
            )
            mark_at = observed + timedelta(days=1)
            metadata = {
                "selected_expression": plan.selected_expression_kind.value,
                "selected_expression_identity": plan.selected_expression_identity,
                "alternate_expression": "CASH",
                "expression_returns": {"STOCK": 0.10, "CASH": 0.0},
                "expression_marks": {
                    "STOCK": {
                        "status": "estimated", "gross_return": 0.10,
                        "cost_adjusted_return": 0.09, "evidence_state": "ESTIMATED",
                        "observed_at": mark_at.isoformat(), "available_at": mark_at.isoformat(),
                    },
                    "CASH": {
                        "status": "measured", "gross_return": 0.0,
                        "cost_adjusted_return": 0.0, "evidence_state": "DERIVED",
                    },
                },
                "cost_adjusted_selected_return": 0.09,
                "cost_adjusted_stock_counterfactual_return": 0.09,
                "cost_adjusted_cash_return": 0.0,
                "trend_counterfactual_return": 0.02,
                "sample": "historical",
                "sample_start": observed.date().isoformat(),
                "sample_end": observed.date().isoformat(),
                "purge_embargo_verified": True,
                "delistings_handled": True,
                "sector_slice": "technology",
                "regime_slice": "risk_on",
                "multiple_trial_correction": "single-policy",
            }
            for horizon, sessions in HORIZON_SESSIONS.items():
                for horizon_sessions in sessions:
                    connection.execute(
                        """
                        INSERT INTO analysis.ticker_outcome (
                            ticker_decision_id, horizon, horizon_sessions, state,
                            measured_through, selected_expression, selected_return,
                            stock_counterfactual_return, alternate_counterfactual_return,
                            cash_return, sector_return, market_return, available_at, metadata
                        ) VALUES (%s::uuid, %s, %s, 'resolved', %s, %s, 0.10, 0.10, 0.0, 0.0, 0.02, 0.03, %s, %s)
                        """,
                        [
                            row["id"], horizon.value, horizon_sessions, mark_at,
                            plan.selected_expression_kind.value, mark_at, Jsonb(metadata),
                        ],
                    )

        repository = TickerDecisionRepository(runtime)
        first = repository.publish_outcome_attributions(now=observed + timedelta(days=2))
        replay = repository.publish_outcome_attributions(now=observed + timedelta(days=2))
        if plan.eligibility == "BLOCKED":
            assert first["status"] == "blocked"
            assert replay["status"] == "blocked"
            return
        assert first["status"] == "ok", first
        assert first["published_count"] == 6
        assert first["paper_orders"] == 0
        assert first["attribution_publication_id"] == replay["attribution_publication_id"]
        rows = AnalysisRepository(runtime).publication_rows(
            "ticker-outcome-attribution", "outcome_attribution", include_lineage=True,
        )
        assert len(rows) == 6
        assert len({row["stable_unit_key"] for row in rows}) == 6
        assert all(row["trade_plan_id"] == plan.trade_plan_id for row in rows)
        assert all(row["publication_id"] == first["attribution_publication_id"] for row in rows)

        monkeypatch.setattr(ticker_decisions, "load_config", lambda _path: config)

        def fake_evaluate(repository, decision, horizon, sessions, reference):
            return {
                "state": "resolved", "available_at": reference,
                "selected_return": 0.1, "stock_return": 0.1,
                "alternate_counterfactual_return": 0.0, "cash_return": 0.0,
                "sector_return": 0.0, "market_return": 0.0,
                "error_type": None, "mistake_card": {}, "learning_metadata": {},
            }

        monkeypatch.setattr(TickerDecisionRepository, "_evaluate", fake_evaluate)
        revised_result = ticker_decisions.publish(
            "config.yaml", symbols=[symbol], as_of=observed + timedelta(days=1),
        )
        assert revised_result["published_count"] == 1, revised_result
        revised = repository.latest(symbol)
        assert revised is not None and revised.trade_plan is not None
        assert revised.trade_plan.trade_plan_id != plan.trade_plan_id
        latest = repository.latest(symbol)
        assert latest is not None and latest.trade_plan is not None
        assert latest.trade_plan.trade_plan_id == revised.trade_plan.trade_plan_id
        selected_refresh = repository.refresh_outcomes(
            now=observed + timedelta(days=3), symbols={symbol},
        )
        assert selected_refresh["evaluated"] == 2
        preserved = AnalysisRepository(runtime).publication_rows(
            "ticker-outcome-attribution", "outcome_attribution", include_lineage=True,
        )
        assert len(preserved) == 6
        assert len({row["stable_unit_key"] for row in preserved}) == 6
        assert all(row["publication_id"] == first["attribution_publication_id"] for row in preserved)
        for field in (
            "trade_plan_id", "opportunity_episode_id", "decision_revision",
            "selected_expression_identity",
        ):
            assert all(row[field] == getattr(plan, field) for row in preserved)
            assert getattr(revised.trade_plan, field) != getattr(plan, field)
        assert all(row["trade_plan_publication_id"] == plan.publication_id for row in preserved)
        learning = repository.learning_surface(symbol)
        assert learning["outcome_attributions"] == []
        assert learning["strategy_learning"]["metrics"]["canonical_attribution_rows"] == 0
    finally:
        runtime.close()


def test_option_paper_attribution_uses_contract_cash_units_for_fees() -> None:
    execution, blocker = paper_execution_for_plan(
        [{
            "trade_plan_id": "plan-option",
            "paper_order_id": "order-option",
            "status": "exited",
            "paper_only": True,
            "expression_kind": "CALL",
            "structure": "long_call",
            "filled_quantity": 1,
            "exited_quantity": 1,
            "actual_fill_price": 2.0,
            "exit_price": 3.0,
            "fees": 1.30,
            "contract_multiplier": 100,
            "entry_fill_count": 1,
            "exit_fill_count": 1,
            "filled_at": datetime(2026, 8, 23, 14, tzinfo=UTC),
            "exit_at": datetime(2026, 8, 24, 14, tzinfo=UTC),
            "updated_at": datetime(2026, 8, 24, 15, tzinfo=UTC),
        }],
        datetime(2026, 8, 24, 16, tzinfo=UTC),
    )

    assert blocker is None
    assert execution is not None
    assert execution.realized_gross_return == 0.5
    assert execution.realized_net_return == (100.0 - 1.30) / 200.0


def test_portfolio_replay_uses_execution_and_created_at_cutoffs(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        config = typed_config(migrated_postgres_dsn)
        cutoff = datetime(2026, 8, 22, 14, tzinfo=UTC)
        later = cutoff + timedelta(hours=3)
        with runtime.transaction() as connection:
            instrument = connection.execute(
                "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES ('REPLAY', 'Replay', 'equity') RETURNING id"
            ).fetchone()["id"]
            initial = connection.execute(
                """
                INSERT INTO app.portfolio_transaction
                    (instrument_id, transaction_type, quantity, price, amount, executed_at, created_at, idempotency_key)
                VALUES (%s, 'opening_balance', 10, 100, 1000, %s, %s, 'replay-initial')
                RETURNING id
                """,
                [instrument, cutoff - timedelta(hours=1), cutoff - timedelta(hours=1)],
            ).fetchone()["id"]
            connection.execute(
                """
                INSERT INTO app.portfolio_transaction
                    (instrument_id, transaction_type, quantity, price, amount, executed_at, created_at, idempotency_key)
                VALUES (%s, 'buy', 2, 120, 240, %s, %s, 'replay-created-late')
                """,
                [instrument, cutoff - timedelta(minutes=10), later],
            )
            connection.execute(
                """
                INSERT INTO app.portfolio_transaction
                    (instrument_id, transaction_type, quantity, price, amount, executed_at, created_at,
                     idempotency_key, reverses_transaction_id)
                VALUES (%s, 'opening_balance', 10, 100, 1000, %s, %s, 'replay-reversal', %s)
                """,
                [instrument, later, later, initial],
            )

        at_cutoff = replay_portfolio_at(config, cutoff)
        after_reversal = replay_portfolio_at(config, later)
        assert at_cutoff["transaction_count"] == 1
        assert at_cutoff["positions"][0]["quantity"] == 10
        assert at_cutoff["portfolio_value"] is None
        assert at_cutoff["valuation_complete"] is False
        assert at_cutoff["book_identity"].startswith("portfolio-book:")
        assert after_reversal["transaction_count"] == 1
        assert after_reversal["positions"][0]["quantity"] == 2
    finally:
        runtime.close()


def test_portfolio_replay_freezes_sector_when_current_metadata_changes(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        config = typed_config(migrated_postgres_dsn)
        cutoff = datetime(2026, 8, 22, 14, tzinfo=UTC)
        with runtime.transaction() as connection:
            instrument = connection.execute(
                """
                INSERT INTO catalog.instrument (symbol, name, asset_class, sector)
                VALUES ('REPLAYSECTOR', 'Replay Sector', 'equity', 'old-sector')
                RETURNING id
                """
            ).fetchone()["id"]
            transaction = connection.execute(
                """
                INSERT INTO app.portfolio_transaction
                    (instrument_id, transaction_type, quantity, price, amount,
                     executed_at, created_at, idempotency_key)
                VALUES (%s, 'opening_balance', 10, 100, 1000, %s, %s, 'replay-sector-initial')
                RETURNING id
                """
                , [instrument, cutoff - timedelta(hours=1), cutoff - timedelta(hours=1)]
            ).fetchone()["id"]
            stored = connection.execute(
                "SELECT instrument_sector FROM app.portfolio_transaction WHERE id = %s",
                [transaction],
            ).fetchone()
            assert stored["instrument_sector"] == "old-sector"

        before_metadata_change = replay_portfolio_at(config, cutoff)
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE catalog.instrument SET sector = 'new-sector' WHERE id = %s",
                [instrument],
            )
        after_metadata_change = replay_portfolio_at(config, cutoff)

        assert before_metadata_change["positions"][0]["sector"] == "old-sector"
        assert after_metadata_change["positions"][0]["sector"] == "old-sector"
        assert after_metadata_change["book_identity"] == before_metadata_change["book_identity"]
    finally:
        runtime.close()


def test_stock_paper_entry_uses_shared_ticker_loss_budget_and_is_idempotent(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        with runtime.transaction() as connection:
            instrument = connection.execute(
                "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES ('ACME', 'Acme', 'equity') RETURNING id"
            ).fetchone()
        observed = datetime(2026, 8, 22, 14, tzinfo=UTC)
        config = typed_config(
            migrated_postgres_dsn,
            raw={"analysis": {"options_decision_system": {
                "mode": "paper",
                "ticker_paper_actions_enabled": True,
                "stock_paper_actions_enabled": True,
            }}},
        )
        decision = _publish_context(
            runtime, config, "ACME",
            _actionable_tables(
                "ACME", observed,
                legs=[{
                    "contract_id": 1, "option_type": "call", "side": "long", "strike": 105,
                    "bid": 2.0, "ask": 2.2, "bid_size": 10, "ask_size": 10,
                    "quote_time": observed, "expiration": "2026-10-16",
                }],
            ),
            observed,
        )
        repository = TickerPaperExecutionRepository(runtime, config)
        assert decision.trade_plan is not None
        if decision.trade_plan.eligibility == "BLOCKED":
            with pytest.raises(ValueError, match="blocked"):
                repository.stage(
                    ticker="ACME", decision=decision,
                    expression_kind=decision.trade_plan.selected_expression_kind.value,
                    idempotency_key="acme-entry-1", trade_plan_id=decision.trade_plan.trade_plan_id,
                )
            return
        result = repository.stage(
            ticker="ACME",
            decision=decision,
            expression_kind=decision.trade_plan.selected_expression_kind.value,
            idempotency_key="acme-entry-1",
            quantity=decision.trade_plan.quantity,
            limit_price=decision.trade_plan.entry_limit,
            trade_plan_id=decision.trade_plan.trade_plan_id,
        )
        replay = repository.stage(
            ticker="ACME",
            decision=decision,
            expression_kind=decision.trade_plan.selected_expression_kind.value,
            idempotency_key="acme-entry-1",
            quantity=decision.trade_plan.quantity,
            limit_price=decision.trade_plan.entry_limit,
            trade_plan_id=decision.trade_plan.trade_plan_id,
        )

        assert result["status"] == "staged"
        assert result["paper_only"] is True
        assert result["live_order_submission"] is False
        assert replay["idempotent_replay"] is True
        with runtime.read() as connection:
            row = connection.execute(
                "SELECT lane, expression_kind, planned_loss, paper_only, idempotency_key, policy_result "
                "FROM app.paper_order WHERE policy_result->>'caller_idempotency_key' = 'acme-entry-1'"
            ).fetchone()
        assert row["lane"] == "ticker"
        assert row["expression_kind"] == "STOCK"
        assert float(row["planned_loss"]) == decision.trade_plan.planned_loss
        assert row["idempotency_key"] == decision.trade_plan.trade_plan_id
        assert row["policy_result"]["caller_idempotency_key"] == "acme-entry-1"
        assert row["paper_only"] is True
    finally:
        runtime.close()


def test_ticker_paper_lifecycle_supports_partial_fill_and_invalidation_exit(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        with runtime.transaction() as connection:
            instrument = connection.execute(
                "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES ('LIFE', 'Lifecycle', 'equity') RETURNING id"
            ).fetchone()
            source_id = "ticker-paper-lifecycle"
            connection.execute(
                "INSERT INTO ingest.source (id, name, family, kind) VALUES (%s, %s, %s, %s)",
                [source_id, "Ticker paper lifecycle", "test", "quote"],
            )
            run_id = connection.execute(
                """
                INSERT INTO ingest.run (source_id, capability, started_at, finished_at, status)
                VALUES (%s, 'quotes', %s, %s, 'succeeded')
                RETURNING id
                """,
                [source_id, datetime(2026, 8, 22, 14, tzinfo=UTC), datetime(2026, 8, 22, 14, tzinfo=UTC)],
            ).fetchone()["id"]
            quote_id = connection.execute(
                """
                INSERT INTO raw.quote (
                    instrument_id, source_id, ingest_run_id, observed_at, available_at, price
                ) VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                [
                    instrument["id"], source_id, run_id,
                    datetime(2026, 8, 22, 14, tzinfo=UTC),
                    datetime(2026, 8, 22, 14, tzinfo=UTC), 99.0,
                ],
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO raw.quote_confirmation (fact_id, fact_available_at, ingest_run_id) VALUES (%s, %s, %s)",
                [quote_id, datetime(2026, 8, 22, 14, tzinfo=UTC), run_id],
            )
        observed = datetime(2026, 8, 22, 14, tzinfo=UTC)
        config = typed_config(
            migrated_postgres_dsn,
            raw={"analysis": {"options_decision_system": {
                "mode": "paper",
                "ticker_paper_actions_enabled": True,
                "stock_paper_actions_enabled": True,
            }}},
        )
        decision = _publish_context(
            runtime, config, "LIFE",
            _actionable_tables(
                "LIFE", observed,
                legs=[{
                    "contract_id": 1, "option_type": "call", "side": "long", "strike": 105,
                    "bid": 2.0, "ask": 2.2, "bid_size": 10, "ask_size": 10,
                    "quote_time": observed, "expiration": "2026-10-16",
                }],
            ),
            observed,
        )
        repository = TickerPaperExecutionRepository(runtime, config)
        assert decision.trade_plan is not None
        if decision.trade_plan.eligibility == "BLOCKED":
            with pytest.raises(ValueError, match="blocked"):
                repository.stage(
                    ticker="LIFE", decision=decision,
                    expression_kind=decision.trade_plan.selected_expression_kind.value,
                    idempotency_key="life-entry-1", trade_plan_id=decision.trade_plan.trade_plan_id,
                )
            return
        staged = repository.stage(
            ticker="LIFE", decision=decision,
            expression_kind=decision.trade_plan.selected_expression_kind.value,
            idempotency_key="life-entry-1", quantity=decision.trade_plan.quantity,
            limit_price=decision.trade_plan.entry_limit,
            trade_plan_id=decision.trade_plan.trade_plan_id,
        )
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE app.paper_order SET policy_result = policy_result || %s::jsonb WHERE id = %s::uuid",
                ['{"available_quantity": 2, "fee_per_unit": 0.01}', staged["paper_order_id"]],
            )
        partial = repository.process(now=datetime(2026, 8, 22, 14, 5, tzinfo=UTC))
        assert partial["managed"][0]["status"] == "partial"
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE app.paper_order SET policy_result = policy_result || %s::jsonb WHERE id = %s::uuid",
                    [
                        json.dumps({"available_quantity": decision.trade_plan.quantity}),
                        staged["paper_order_id"],
                    ],
            )
        filled = repository.process(now=datetime(2026, 8, 22, 14, 6, tzinfo=UTC))
        assert filled["managed"][0]["event_status"] == "entered", filled

        with runtime.transaction() as connection:
            run_id = connection.execute(
                """
                INSERT INTO ingest.run (source_id, capability, started_at, finished_at, status)
                VALUES (%s, 'quotes', %s, %s, 'succeeded')
                RETURNING id
                """,
                [source_id, datetime(2026, 8, 22, 15, tzinfo=UTC), datetime(2026, 8, 22, 15, tzinfo=UTC)],
            ).fetchone()["id"]
            quote_id = connection.execute(
                """
                INSERT INTO raw.quote (
                    instrument_id, source_id, ingest_run_id, observed_at, available_at, price
                ) SELECT id, %s, %s, %s, %s, %s
                FROM catalog.instrument WHERE symbol = 'LIFE'
                RETURNING id
                """,
                [source_id, run_id, datetime(2026, 8, 22, 15, tzinfo=UTC), datetime(2026, 8, 22, 15, tzinfo=UTC), 89.0],
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO raw.quote_confirmation (fact_id, fact_available_at, ingest_run_id) VALUES (%s, %s, %s)",
                [quote_id, datetime(2026, 8, 22, 15, tzinfo=UTC), run_id],
            )
        exited = repository.process(now=datetime(2026, 8, 22, 15, 5, tzinfo=UTC))
        assert exited["managed"][0]["reason"] == "invalidation"
        with runtime.read() as connection:
            status = connection.execute(
                "SELECT status, filled_quantity, exited_quantity, fees FROM app.paper_order WHERE id = %s::uuid",
                [staged["paper_order_id"]],
            ).fetchone()
        assert status["status"] == "exited"
        assert float(status["filled_quantity"]) == decision.trade_plan.quantity
        assert float(status["exited_quantity"]) == decision.trade_plan.quantity
        assert float(status["fees"]) > 0
    finally:
        runtime.close()


def test_multi_leg_option_paper_lifecycle_uses_shared_quote_and_risk_ledger(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        source_id = "ticker-paper-options"
        observed = datetime(2026, 8, 22, 14, tzinfo=UTC)
        later_quote = datetime(2026, 8, 22, 14, 10, tzinfo=UTC)
        exit_quote = datetime(2026, 8, 22, 15, tzinfo=UTC)
        with runtime.transaction() as connection:
            instrument = connection.execute(
                "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES ('SPRD', 'Spread', 'equity') RETURNING id"
            ).fetchone()
            connection.execute(
                "INSERT INTO ingest.source (id, name, family, kind) VALUES (%s, %s, %s, %s)",
                [source_id, "Ticker option paper", "test", "options"],
            )
            contracts = [
                connection.execute(
                    """
                    INSERT INTO catalog.option_contract
                        (underlying_instrument_id, expiration, strike, option_type, deliverable_key)
                    VALUES (%s, '2026-09-18', %s, 'call', %s)
                    RETURNING id
                    """,
                    [instrument["id"], strike, f"sprd-{strike}"],
                ).fetchone()["id"]
                for strike in (100, 105)
            ]

            def add_option_snapshot(
                at: datetime,
                prices: tuple[tuple[float, float], tuple[float, float]],
                sizes: tuple[int, int],
            ) -> None:
                run_id = connection.execute(
                    """
                    INSERT INTO ingest.run (source_id, capability, started_at, finished_at, status)
                    VALUES (%s, 'options', %s, %s, 'succeeded')
                    RETURNING id
                    """,
                    [source_id, at, at],
                ).fetchone()["id"]
                snapshot_id = connection.execute(
                    """
                    INSERT INTO raw.option_snapshot
                        (source_id, ingest_run_id, observed_at, trading_date, market_session,
                         universe, completeness, contract_count, capture_state)
                    VALUES (%s, %s, %s, %s, 'regular', 'SPRD', 1, 2, 'complete')
                    RETURNING id
                    """,
                    [source_id, run_id, at, at.date()],
                ).fetchone()["id"]
                for contract_id, (bid, ask) in zip(contracts, prices):
                    connection.execute(
                        """
                        INSERT INTO raw.option_quote
                            (observed_at, snapshot_id, contract_id, underlying_price,
                             bid, ask, bid_size, ask_size, mid, volume, open_interest, available_at)
                        VALUES (%s, %s, %s, 100, %s, %s, %s, %s, (%s + %s) / 2, 20, 100, %s)
                        """,
                        [at, snapshot_id, contract_id, bid, ask, sizes[0], sizes[1], bid, ask, at],
                    )

            add_option_snapshot(observed, ((3.0, 3.2), (1.0, 1.1)), (1, 1))
        add_option_snapshot(later_quote, ((3.0, 3.2), (1.0, 1.1)), (4, 4))
        add_option_snapshot(exit_quote, ((2.4, 2.6), (0.8, 1.0)), (4, 4))

        legs = [
            {
                "contract_id": contracts[0], "option_type": "call", "side": "long",
                "strike": 100, "bid": 3.0, "ask": 3.2, "bid_size": 1, "ask_size": 1,
                "quote_time": observed, "expiration": "2026-09-18",
            },
            {
                "contract_id": contracts[1], "option_type": "call", "side": "short",
                "strike": 105, "bid": 1.0, "ask": 1.1, "bid_size": 1, "ask_size": 1,
                "quote_time": observed, "expiration": "2026-09-18",
            },
        ]
        config = typed_config(
            migrated_postgres_dsn,
            raw={"analysis": {"options_decision_system": {
                "mode": "paper",
                "ticker_paper_actions_enabled": True,
                "stock_paper_actions_enabled": True,
                "options_paper_actions_enabled": True,
            }}},
        )
        decision = _publish_context(
            runtime, config, "SPRD",
            _actionable_tables(
                "SPRD", observed, legs=legs, structure="debit_spread", option_lower_expectancy=1.0,
            ),
            observed,
        )
        repository = TickerPaperExecutionRepository(runtime, config)
        assert decision.trade_plan is not None
        if decision.trade_plan.eligibility == "BLOCKED":
            with pytest.raises(ValueError, match="blocked"):
                repository.stage(
                    ticker="SPRD", decision=decision,
                    expression_kind=decision.trade_plan.selected_expression_kind.value,
                    idempotency_key="sprd-spread-1", trade_plan_id=decision.trade_plan.trade_plan_id,
                )
            return
        staged = repository.stage(
            ticker="SPRD", decision=decision,
            expression_kind=decision.trade_plan.selected_expression_kind.value,
            idempotency_key="sprd-spread-1", quantity=decision.trade_plan.quantity,
            limit_price=decision.trade_plan.entry_limit,
            trade_plan_id=decision.trade_plan.trade_plan_id,
        )
        with runtime.read() as connection:
            assert connection.execute(
                "SELECT count(*) FROM app.paper_order_leg WHERE paper_order_id = %s::uuid",
                [staged["paper_order_id"]],
            ).fetchone()["count"] == 2

        partial = repository.process(now=datetime(2026, 8, 22, 14, 5, tzinfo=UTC))
        assert partial["managed"][0]["status"] == "partial"
        filled = repository.process(now=datetime(2026, 8, 22, 14, 11, tzinfo=UTC))
        assert filled["managed"][0].get("event_status") == "entered", filled

        with runtime.transaction() as connection:
            run_id = connection.execute(
                """
                INSERT INTO ingest.run (source_id, capability, started_at, finished_at, status)
                VALUES (%s, 'quotes', %s, %s, 'succeeded')
                RETURNING id
                """,
                [source_id, exit_quote, exit_quote],
            ).fetchone()["id"]
            quote_id = connection.execute(
                """
                INSERT INTO raw.quote
                    (instrument_id, source_id, ingest_run_id, observed_at, available_at, price)
                VALUES (%s, %s, %s, %s, %s, 89)
                RETURNING id
                """,
                [instrument["id"], source_id, run_id, exit_quote, exit_quote],
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO raw.quote_confirmation (fact_id, fact_available_at, ingest_run_id) VALUES (%s, %s, %s)",
                [quote_id, exit_quote, run_id],
            )

        exited = repository.process(now=datetime(2026, 8, 22, 15, 5, tzinfo=UTC))
        assert exited["managed"][0]["reason"] == "invalidation"
        with runtime.read() as connection:
            row = connection.execute(
                """
                SELECT status, filled_quantity, exited_quantity, fees, exit_price, paper_only
                FROM app.paper_order WHERE id = %s::uuid
                """,
                [staged["paper_order_id"]],
            ).fetchone()
        assert row["status"] == "exited"
        assert float(row["filled_quantity"]) == decision.trade_plan.quantity
        assert float(row["exited_quantity"]) == decision.trade_plan.quantity
        assert float(row["fees"]) > 0
        assert float(row["exit_price"]) == 1.4
        assert row["paper_only"] is True
    finally:
        runtime.close()


def test_ticker_publisher_persists_immutable_revision_and_pit_manifest(
    migrated_postgres_dsn: str,
    monkeypatch,
) -> None:
    from investment_panel.jobs import ticker_decisions

    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        with runtime.transaction() as connection:
            for symbol in ("PITX", "OTHER"):
                connection.execute(
                    "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES (%s, %s, 'equity')",
                    [symbol, symbol],
                )
        config = typed_config(migrated_postgres_dsn)
        monkeypatch.setattr(ticker_decisions, "load_config", lambda _path: config)
        replay_calls = 0
        replay_portfolio = ticker_decisions.replay_portfolio_at

        def counted_replay(*args, **kwargs):
            nonlocal replay_calls
            replay_calls += 1
            return replay_portfolio(*args, **kwargs)

        monkeypatch.setattr(ticker_decisions, "replay_portfolio_at", counted_replay)
        historical = datetime(2026, 7, 1, 14, tzinfo=UTC)
        for symbol in ("PITX", "OTHER"):
            TickerDecisionRepository(runtime).publish(
                build_ticker_decision(
                    symbol,
                    _actionable_tables(
                        symbol,
                        historical,
                        legs=[{
                            "contract_id": 1, "option_type": "call", "side": "long", "strike": 105,
                            "bid": 2.0, "ask": 2.2, "bid_size": 10, "ask_size": 10,
                            "quote_time": historical, "expiration": "2026-10-16",
                        }],
                    ),
                    as_of=historical,
                )
            )
        outcome_scopes: list[list[str]] = []
        evaluated: list[tuple[str, datetime]] = []
        refresh_outcomes = TickerDecisionRepository.refresh_outcomes

        def scoped_refresh(repository, **kwargs):
            outcome_scopes.append(list(kwargs["symbols"]))
            return refresh_outcomes(repository, **kwargs)

        monkeypatch.setattr(TickerDecisionRepository, "refresh_outcomes", scoped_refresh)

        def fake_evaluate(repository, decision, horizon, sessions, reference):
            if sessions == 1:
                evaluated.append((decision["ticker"], decision["as_of"]))
            return {
                "state": "resolved", "available_at": reference,
                "selected_return": 0.1, "stock_return": 0.1,
                "alternate_counterfactual_return": 0.0, "cash_return": 0.0,
                "sector_return": 0.0, "market_return": 0.0,
                "error_type": None, "mistake_card": {}, "learning_metadata": {},
            }

        monkeypatch.setattr(TickerDecisionRepository, "_evaluate", fake_evaluate)
        observed = datetime(2026, 8, 22, 14, tzinfo=UTC)
        result = ticker_decisions.publish(
            "config.yaml", symbols=["PITX"], as_of=observed,
        )
        assert replay_calls == 1
        replay = ticker_decisions.publish(
            "config.yaml", symbols=["PITX"], as_of=observed,
        )

        assert result["published_count"] == 1, result
        assert replay["published_count"] == 0
        assert replay["skipped_count"] == 1
        assert outcome_scopes == [["PITX"], ["PITX"]]
        assert result["outcomes"]["evaluated"] == 2
        assert replay["outcomes"]["evaluated"] == 2
        assert evaluated.count(("PITX", historical)) == 2
        assert evaluated.count(("PITX", observed)) == 2
        assert ("OTHER", historical) not in evaluated
        with runtime.read() as connection:
            rank_publication = connection.execute(
                """
                SELECT publication.published_at, run.input_cutoff
                FROM app.publication publication
                JOIN analysis.run run ON run.id = publication.analysis_run_id
                WHERE publication.id = %s::uuid
                """,
                [result["ranking_publication_id"]],
            ).fetchone()
        decision = TickerDecisionRepository(runtime).latest("PITX")
        assert decision is not None
        assert decision.as_of == observed
        assert rank_publication["published_at"] > observed
        assert rank_publication["input_cutoff"] == observed
        assert len(decision.input_manifest.input_hash) == 64
        with runtime.read() as connection:
            manifest = connection.execute(
                "SELECT count(*) FROM analysis.ticker_input_manifest manifest "
                "JOIN analysis.ticker_decision decision ON decision.id = manifest.ticker_decision_id"
            ).fetchone()["count"]
            outcomes = connection.execute(
                "SELECT count(*) FROM analysis.ticker_outcome outcome "
                "JOIN analysis.ticker_decision decision ON decision.id = outcome.ticker_decision_id"
            ).fetchone()["count"]
            mature_outcomes = connection.execute(
                "SELECT count(*) FROM analysis.ticker_outcome outcome "
                "JOIN analysis.ticker_decision decision ON decision.id = outcome.ticker_decision_id "
                "JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id "
                "WHERE instrument.symbol = 'PITX' AND decision.as_of = %s AND outcome.state = 'resolved'",
                [historical],
            ).fetchone()["count"]
            benchmark = connection.execute(
                """
                SELECT benchmark_key, member_count, membership_hash,
                       exact_membership, coverage
                FROM analysis.ticker_benchmark_snapshot
                WHERE benchmark_key = 'market-equity-etf' AND as_of = %s
                """,
                [observed],
            ).fetchone()
        assert manifest >= 1
        assert outcomes == 12
        assert mature_outcomes == 6
        assert benchmark["benchmark_key"] == "market-equity-etf"
        assert benchmark["member_count"] == len(benchmark["exact_membership"])
        assert "PITX" in benchmark["exact_membership"]
        assert len(benchmark["membership_hash"]) == 64
        assert benchmark["coverage"]["options_availability_affects_breadth"] is False

        future = observed + timedelta(days=1)
        TickerDecisionRepository(runtime).publish(
            build_ticker_decision(
                "PITX",
                _actionable_tables(
                    "PITX",
                    future,
                    legs=[{
                        "contract_id": 1, "option_type": "call", "side": "long", "strike": 105,
                        "bid": 2.0, "ask": 2.2, "bid_size": 10, "ask_size": 10,
                        "quote_time": future, "expiration": "2026-10-16",
                    }],
                ),
                as_of=future,
            )
        )
        before_future_refresh = len(evaluated)
        future_refresh = refresh_outcomes(
            TickerDecisionRepository(runtime), now=observed, symbols=["PITX"],
        )
        assert future_refresh["evaluated"] == 2
        assert ("PITX", future) not in evaluated[before_future_refresh:]
    finally:
        runtime.close()


def test_peer_return_materializes_large_confirmed_peer_set_once(
    migrated_postgres_dsn: str,
    monkeypatch,
) -> None:
    import investment_panel.database.ticker_decisions as ticker_decision_module

    monkeypatch.setattr(
        ticker_decision_module,
        "JOB_PROFILE",
        RuntimeProfile(statement_timeout_ms=3_000),
    )
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        source_id = "ticker-peer-performance"
        peer_count = 640
        entry_date = datetime(2026, 4, 1, tzinfo=UTC).date()
        mark_date = datetime(2026, 4, 3, tzinfo=UTC).date()
        as_of = datetime(2026, 4, 1, 22, tzinfo=UTC)
        reference = datetime(2026, 4, 4, 22, tzinfo=UTC)
        ingestion = IngestionRepository(runtime)
        ingestion.register_source(source_id, name="Ticker peer performance", family="test", kind="daily_bars")
        run_id = ingestion.start_run(source_id, "price_bars", started_at=as_of)
        with runtime.transaction() as connection:
            connection.execute(
                """
                INSERT INTO catalog.instrument (symbol, name, asset_class, sector)
                SELECT 'PEER' || lpad(number::text, 4, '0'),
                       'Peer ' || number::text, 'equity', 'performance-test'
                FROM generate_series(1, %s) AS numbers(number)
                """,
                [peer_count],
            )
            connection.execute(
                """
                WITH trading_days AS (
                    SELECT day::date AS trading_date
                    FROM generate_series(%s::date - 180, %s::date + 180, interval '1 day') AS days(day)
                    WHERE extract(isodow FROM day) < 6
                )
                INSERT INTO raw.price_bar (
                    instrument_id, source_id, ingest_run_id, interval,
                    trading_date, observed_at, close, available_at
                )
                SELECT instrument.id, %s, %s, '1d', trading_days.trading_date,
                       ((trading_days.trading_date + time '20:00') AT TIME ZONE 'UTC'),
                       CASE
                           WHEN trading_days.trading_date = %s::date
                               THEN 110.0 + right(instrument.symbol, 4)::integer
                           ELSE 100.0 + right(instrument.symbol, 4)::integer
                                + (trading_days.trading_date - %s::date) * 0.01
                       END,
                       CASE
                           WHEN instrument.symbol = 'PEER0001'
                            AND trading_days.trading_date = %s::date
                               THEN %s::timestamptz + interval '1 hour'
                           ELSE ((trading_days.trading_date + time '21:00') AT TIME ZONE 'UTC')
                       END
                FROM catalog.instrument instrument
                CROSS JOIN trading_days
                WHERE instrument.symbol LIKE 'PEER' || chr(37)
                """,
                [entry_date, entry_date, source_id, run_id, mark_date, entry_date, mark_date, reference],
            )
            connection.execute(
                """
                INSERT INTO raw.price_bar_fact_availability (fact_id, fact_available_at, ingest_run_id)
                SELECT id, available_at, ingest_run_id
                FROM raw.price_bar
                WHERE ingest_run_id = %s
                """,
                [run_id],
            )
            connection.execute("ANALYZE raw.price_bar")
            connection.execute("ANALYZE raw.price_bar_fact_availability")
            symbols = [
                str(row["symbol"])
                for row in connection.execute(
                    "SELECT symbol FROM catalog.instrument WHERE symbol LIKE 'PEER' || chr(37) ORDER BY symbol"
                ).fetchall()
            ]
        ingestion.finish_run(run_id, "succeeded")

        parameters = [symbols, entry_date, as_of, symbols, mark_date, reference]
        with runtime.read() as connection:
            legacy_plan = _plan(
                connection.execute(
                    "EXPLAIN (FORMAT JSON) " + _LEGACY_PEER_RETURN_QUERY,
                    parameters,
                ).fetchone()["QUERY PLAN"]
            )
            comparison_symbols = symbols[:8]
            comparison_parameters = [
                comparison_symbols, entry_date, as_of,
                comparison_symbols, mark_date, reference,
            ]
            legacy_observed = connection.execute(
                _LEGACY_PEER_RETURN_QUERY,
                comparison_parameters,
            ).fetchone()["return"]

        repaired_observed = TickerDecisionRepository(runtime)._peer_return(
            comparison_symbols, entry_date, mark_date, as_of, reference,
        )
        observed = TickerDecisionRepository(runtime)._peer_return(
            symbols, entry_date, mark_date, as_of, reference,
        )
        expected = sum(
            (110.0 + number) / (100.0 + number) - 1.0
            for number in range(2, peer_count + 1)
        ) / (peer_count - 1)
        expected_comparison = sum(
            (110.0 + number) / (100.0 + number) - 1.0
            for number in range(2, 9)
        ) / 7

        assert _relation_count(legacy_plan, "price_bar_fact_availability") == 2
        assert legacy_observed is not None
        assert repaired_observed is not None
        assert observed is not None
        assert abs(legacy_observed - repaired_observed) < 1e-12
        assert abs(repaired_observed - expected_comparison) < 1e-12
        assert abs(observed - expected) < 1e-12
    finally:
        runtime.close()


def test_ticker_outcome_refresh_persists_costs_and_learning_metadata(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        ticker = "OUTC"
        as_of = datetime(2026, 7, 1, 21, tzinfo=UTC)
        available_at = datetime(2026, 7, 1, 20, 30, tzinfo=UTC)
        reference = datetime(2026, 8, 20, 21, tzinfo=UTC)
        ingestion = IngestionRepository(runtime)
        with runtime.transaction() as connection:
            connection.execute(
                "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES (%s, %s, 'equity')",
                [ticker, "Outcome Test"],
            )
        source_id = "ticker-outcome-bars"
        ingestion.register_source(source_id, name="Ticker outcome bars", family="test", kind="daily_bars")
        run_id = ingestion.start_run(source_id, "price_bars", started_at=available_at)
        rows = [{"symbol": ticker, "date": as_of.date().isoformat(), "close": 100}]
        cursor = as_of.date() + timedelta(days=1)
        while len(rows) < 30:
            if cursor.weekday() < 5:
                rows.append({"symbol": ticker, "date": cursor.isoformat(), "close": 100 + len(rows)})
            cursor += timedelta(days=1)
        ingestion.store_price_bars(run_id, source_id, rows, asset_classes={ticker: "equity"})
        ingestion.finish_run(run_id, "succeeded")
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE ingest.run SET started_at = %s, finished_at = %s WHERE id = %s",
                [available_at, available_at, run_id],
            )
            connection.execute(
                "UPDATE raw.price_bar SET available_at = %s WHERE ingest_run_id = %s",
                [available_at, run_id],
            )
            connection.execute(
                "UPDATE raw.price_bar_confirmation SET fact_available_at = %s WHERE ingest_run_id = %s",
                [available_at, run_id],
            )
            connection.execute(
                "UPDATE raw.price_bar_fact_availability SET fact_available_at = %s WHERE ingest_run_id = %s",
                [available_at, run_id],
            )

        decision = build_ticker_decision(
            ticker,
            {
                "quotes": [{"symbol": ticker, "price": 100, "available_at": available_at, "confirmed": True}],
                "portfolio_summary": [{"net_liquidation": 100_000, "available_at": available_at}],
                "decision_queue": [{
                    "symbol": ticker, "stance": "BULLISH", "action": "BUY",
                    "entry_low": 99, "entry_high": 101, "invalidation_price": 90,
                    "conviction_tier": "STANDARD", "available_at": available_at,
                }],
            },
            as_of=as_of,
        )
        repository = TickerDecisionRepository(runtime)
        published = repository.publish(decision)
        outcome_result = repository.refresh_outcomes(now=reference, symbols={ticker})

        assert outcome_result["resolved"] == 3
        with runtime.read() as connection:
            row = connection.execute(
                """
                SELECT state, stock_counterfactual_return, metadata
                FROM analysis.ticker_outcome
                WHERE ticker_decision_id = %s::uuid
                  AND horizon = 'TACTICAL' AND horizon_sessions = 20
                """,
                [published["ticker_decision_id"]],
            ).fetchone()
        assert row["state"] == "resolved"
        assert row["stock_counterfactual_return"] is not None
        assert row["metadata"]["cost_adjusted_stock_counterfactual_return"] is not None
        assert row["metadata"]["sample"] in {"historical", "forward", "canary"}
        assert row["metadata"]["purge_embargo_verified"] is True
        assert row["metadata"]["multiple_trial_correction"] == "single-policy-no-trial-selection-v1"
        assert row["metadata"]["expression_marks"]["STOCK"]["evidence_state"] == "ESTIMATED"
    finally:
        runtime.close()
