from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest
from psycopg.errors import RaiseException
from psycopg.types.json import Jsonb

from app import panel_snapshot
from app.routers.panel import today
from investment_panel.core.decision import (
    AvailabilityStatus,
    CoverageMatrix,
    CoverageMatrixRow,
    EligibleUniverseSnapshot,
    ExpressionDecision,
    ExpressionKind,
    Horizon,
    InputLineage,
    Invalidation,
    MarketDimensionState,
    MarketStateSnapshot,
    PortfolioImpact,
    PriceRange,
    RiskPolicySnapshot,
    Stance,
    TickerDecision,
    build_alpha_signal,
    build_decision_resolution,
    build_opportunity_episode,
    build_trade_plan,
    rank_opportunities,
    trade_expression_identity,
)
from investment_panel.core.decision.ticker import MARKET_DIMENSIONS, MARKET_HORIZONS
from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.database.ticker_decisions import TickerDecisionRepository
from investment_panel.jobs import ticker_decisions
from conftest import typed_config


def _qualified_artifact(
    runtime: DatabaseRuntime,
    cutoff: datetime,
    *,
    verdict: str = "pass",
    metrics_update: dict[str, object] | None = None,
) -> tuple[int, dict[str, object]]:
    repository = AnalysisRepository(runtime)
    parameters = {
        "artifact_id": "ticker-stock-alpha:v1",
        "model_version": "ticker-stock-alpha.v1",
        "feature_version": "daily-trend-v1",
        "artifact_hash": "phase0-artifact-hash",
        "input_hash": "phase0-input-hash",
        "cost_model_version": "stock-cost-slippage.v1",
        "target": "expected_return",
        "cohort_id": "stock-oos-exact-v1",
        "calibration_state": "calibrated_exact_cohort",
        "expression_kind": "STOCK",
        "horizons": ["TACTICAL", "FUNDAMENTAL"],
    }
    revision_id = repository.register_strategy(
        "ticker-stock-alpha",
        1,
        name="Qualified stock alpha",
        status="active",
        parameters=parameters,
    )
    metrics = {
        "artifact_id": parameters["artifact_id"],
        "model_version": parameters["model_version"],
        "cohort_id": parameters["cohort_id"],
        "feature_version": parameters["feature_version"],
        "cost_model_version": parameters["cost_model_version"],
        "artifact_hash": "phase0-artifact-hash",
        "input_hash": "phase0-input-hash",
        "cohort_path": ["cohort:stock-oos-exact-v1"],
        "fallback_parent": "horizon:TACTICAL",
        "effective_sample_size": 40,
        "calibration_metrics": {"brier_score": 0.2, "calibration_error": 0.1},
        "lower_confidence_net_utility_after_costs": 0.02,
        "valid_through": (cutoff + timedelta(days=30)).isoformat(),
        "forecast": {"horizon": "TACTICAL", "forecast_value": 0.10},
    }
    metrics.update(metrics_update or {})
    with runtime.transaction() as connection:
        row = connection.execute(
            """
            INSERT INTO analysis.strategy_evaluation (
                strategy_revision_id, evaluation_type, evaluated_at,
                period_start, period_end, verdict, metrics, evidence
            ) VALUES (%s, 'out_of_sample', %s, %s, %s, %s, %s, %s)
            RETURNING id::text
            """,
            [
                revision_id,
                cutoff - timedelta(minutes=2),
                cutoff - timedelta(days=30),
                cutoff - timedelta(minutes=3),
                verdict,
                Jsonb(metrics),
                Jsonb([{"source": "walk-forward", "paper_only": True}]),
            ],
        ).fetchone()
        connection.execute(
            """
            INSERT INTO analysis.strategy_evaluation (
                strategy_revision_id, evaluation_type, evaluated_at,
                period_start, period_end, verdict, metrics, evidence
            ) VALUES (%s, 'paper_advisory_promotion', %s, %s, %s, 'pass', %s, %s)
            """,
            [
                revision_id,
                cutoff - timedelta(minutes=1),
                cutoff - timedelta(days=30),
                cutoff - timedelta(minutes=3),
                Jsonb({
                    "artifact_hash": metrics["artifact_hash"],
                    "input_hash": metrics["input_hash"],
                    "authorization_mode": "ADVISORY",
                }),
                Jsonb({"paper_only": True, "live_order_submission": False}),
            ],
        )
    artifact = repository.qualified_stock_alpha_artifact(cutoff=cutoff, horizon="TACTICAL")
    assert artifact["strategy_evaluation_id"] == row["id"]
    return revision_id, artifact


def _market_snapshot(cutoff: datetime) -> MarketStateSnapshot:
    lineage = InputLineage(
        field="market_fact",
        source_id="phase0-fixture",
        source_version="1",
        event_at=cutoff - timedelta(minutes=1),
        available_at=cutoff,
        cutoff=cutoff,
    )
    horizons = {
        horizon: tuple(
            MarketDimensionState(
                dimension=dimension,
                horizon=horizon,
                state="neutral",
                evidence_status="available",
                availability_status=AvailabilityStatus.AVAILABLE,
                quality="fixture",
                lineage=(lineage,),
            )
            for dimension in MARKET_DIMENSIONS
        )
        for horizon in MARKET_HORIZONS
    }
    coverage = CoverageMatrix(
        matrix_id="phase0-coverage:v1",
        as_of=cutoff,
        input_cutoff=cutoff,
        rows=tuple(
            CoverageMatrixRow(
                dimension=dimension,
                asset_class="cross-asset",
                horizon=horizon,
                provider="phase0-fixture",
                point_in_time_safe=True,
                current_status="available",
                input_cutoff=cutoff,
                input_lineage=(lineage,),
            )
            for horizon in MARKET_HORIZONS
            for dimension in MARKET_DIMENSIONS
        ),
    )
    return MarketStateSnapshot(
        snapshot_id="phase0-market-state:v1",
        as_of=cutoff,
        input_cutoff=cutoff,
        horizons=horizons,
        coverage_matrix=coverage,
        input_lineage=(lineage,),
        availability="available",
        availability_status=AvailabilityStatus.AVAILABLE,
    )


def _publish_market(runtime: DatabaseRuntime, cutoff: datetime) -> tuple[str, dict[str, object]]:
    repository = AnalysisRepository(runtime)
    snapshot = _market_snapshot(cutoff)
    run_id = repository.start_run(
        "market",
        input_cutoff=cutoff,
        code_version="phase0-market-v1",
        inputs={"source_lineage": [item.model_dump(mode="json") for item in snapshot.input_lineage]},
        feature_versions={"market_state": "phase0-market-v1"},
    )
    publication_id = repository.publish(
        run_id,
        "market",
        {"market_state_snapshot": [snapshot.model_dump(mode="json")]},
        complete_run_summary={"snapshot_id": snapshot.snapshot_id},
    )
    publication = repository.publication_by_id("market", publication_id)
    assert publication is not None
    return str(publication_id), publication


def _tables(symbol: str, cutoff: datetime) -> dict[str, list[dict[str, object]]]:
    available_at = cutoff - timedelta(minutes=5)
    common = {"symbol": symbol, "available_at": available_at.isoformat()}
    return {
        "quotes": [
            {**common, "price": 100.0, "confirmed": True},
            {"symbol": symbol, "price": 999.0, "confirmed": True,
             "available_at": (cutoff + timedelta(minutes=5)).isoformat()},
        ],
        "portfolio_summary": [{**common, "net_liquidation": 100_000.0}],
        "decision_queue": [{
            **common,
            "stance": "BULLISH",
            "action": "BUY",
            "entry_low": 99.0,
            "entry_high": 101.0,
            "target_low": 120.0,
            "target_high": 125.0,
            "invalidation_price": 90.0,
            "conviction_tier": "STANDARD",
            "scenarios": {
                "bear": {"probability": 0.2},
                "base": {"probability": 0.5},
                "bull": {"probability": 0.3},
            },
        }],
        "valuations": [{**common, "upside_pct": 0.20}],
        "fundamentals": [{**common, "source": "sec_companyfacts", "sector": "Technology"}],
        "technicals": [{**common, "source": "confirmed_prices", "trend": "up"}],
        "liquidity": [{**common, "source": "confirmed_prices", "avg_dollar_volume": 1_000_000.0}],
        "earnings": [{**common, "source": "sec_companyfacts"}],
        "ticker_benchmark_snapshot": [{**common, "source": "catalog.instrument"}],
        "macro": [{**common, "source": "market-publication"}],
        "disclosures": [{**common, "source": "sec_submissions"}],
        "short_interest": [{**common, "source": "finra"}],
        # The missing option surface is deliberate. It must not block STOCK.
        "options_payoff_scenarios": [],
    }


def _portfolio_replay(cutoff: datetime) -> dict[str, object]:
    available_at = cutoff - timedelta(minutes=5)
    return {
        "cutoff": cutoff,
        "positions": [{
            "instrument_id": 1,
            "symbol": "QQQ",
            "sector": "Technology",
            "quantity": 1.0,
            "avg_cost": 450.0,
            "price": 450.0,
            "market_value": 450.0,
            "source_id": "phase0-fixture",
            "currency": "USD",
            "source_kind": "daily_bars",
            "trading_date": cutoff.date().isoformat(),
            "observed_at": available_at,
            "available_at": available_at,
            "valuation_status": "market_quotes",
        }],
        "portfolio_value": 100_000.0,
        "transaction_count": 0,
        "eligible_position_count": 1,
        "valued_position_count": 1,
        "missing_valuation_count": 0,
        "valuation_complete": True,
        "lineage": [],
        "book_identity": "portfolio-book:phase0",
        "stock_evidence": {
            "sector": "Technology",
            "beta": 1.1,
            "avg_dollar_volume": 1_000_000.0,
            "correlation_cluster_delta": 0.01,
            "adv_participation_limit": 0.10,
            "stress_scenarios": {
                "SPY": {"pnl_by_shock": {"-5": -500.0, "-10": -1_000.0}, "shock_pct": [-5.0, -10.0]},
                "QQQ": {"pnl_by_shock": {"-5": -450.0, "-10": -900.0}, "shock_pct": [-5.0, -10.0]},
                "sector": {"pnl": -800.0, "shock_pct": -10.0},
                "symbol": {"pnl_by_shock": {"-20": -1_200.0, "-30": -1_800.0}, "shock_pct": [-20.0, -30.0]},
                "earnings-gap": {"pnl": -1_500.0, "largest_holding": "QQQ", "earnings_gap": True},
                "liquidity": {"pnl": -500.0, "spread_multiplier": 2.0, "slippage_multiplier": 2.0, "adv_haircut_pct": 50.0},
            },
            "risk_budget": {"available": 2_000.0, "consumed": 1_000.0},
            "cash_comparator": {"status": "available", "expected_return": 0.0},
            "top_alternative": "QQQ",
            "funding_source_or_position_to_trim": "cash",
            "expected_transaction_costs": 5.0,
            "tail_risk_penalty": 0.0,
            "portfolio_overlap_penalty": 0.0,
            "diversification_benefit": 0.0,
        },
    }


def test_qualified_stock_reaches_action_queue(migrated_postgres_dsn: str, monkeypatch) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        config = typed_config(migrated_postgres_dsn, raw={
            "analysis": {
                "ticker_universe_coverage_threshold": 0.5,
                "market_publication_max_age_minutes": 60,
            },
        })
        with runtime.transaction() as connection:
            for symbol in ("LANE", "BROKEN"):
                connection.execute(
                    "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES (%s, %s, 'equity')",
                    [symbol, symbol],
                )
        qualification_cutoff = datetime.now(UTC) + timedelta(seconds=5)
        _, artifact = _qualified_artifact(runtime, qualification_cutoff)
        market_cutoff = datetime.now(UTC) - timedelta(minutes=10)
        market_publication_id, publication = _publish_market(runtime, market_cutoff)
        assert _timestamp(publication["published_at"]) > _timestamp(publication["input_cutoff"])
        decision_cutoff = _timestamp(publication["published_at"])

        feature_run_id = AnalysisRepository(runtime).start_run(
            "daily-trend", input_cutoff=decision_cutoff,
            code_version="test", inputs={"symbol": "LANE"},
            feature_versions={"daily_trend": "daily-trend-v1"},
        )
        with runtime.transaction() as connection:
            connection.execute(
                """
                INSERT INTO analysis.symbol_feature (
                    run_id, instrument_id, as_of, feature_set, feature_version,
                    momentum_5d, momentum_20d, relative_strength_20d,
                    relative_strength_60d, kaufman_er_20d,
                    trend_state, trend_confidence, volatility_state,
                    data_quality_status, reason_codes
                )
                SELECT %s, id, %s, 'daily_trend', 'daily-trend-v1',
                       0.02, 0.04, 0.03, 0.06, 0.5,
                       'trend_up', 0.8, 'normal', 'complete', '{}'
                FROM catalog.instrument WHERE symbol = 'LANE'
                """,
                [feature_run_id, decision_cutoff],
            )

        monkeypatch.setattr(ticker_decisions, "load_config", lambda _path: config)
        monkeypatch.setattr(ticker_decisions, "replay_portfolio_at", lambda *_args, **_kwargs: _portfolio_replay(decision_cutoff))

        def bounded_tables(_config, _names, *, query_symbol_filter, **_kwargs):
            symbol = next(iter(query_symbol_filter))
            if symbol == "BROKEN":
                raise RuntimeError("isolated fixture failure")
            return _tables(symbol, decision_cutoff), {"available_model_count": 12, "unavailable_models": []}

        monkeypatch.setattr(ticker_decisions, "load_postgres_tables", bounded_tables)
        result = ticker_decisions.publish(
            "config.yaml",
            symbols=["LANE", "BROKEN"],
            as_of=decision_cutoff,
            market_state_publication_id=market_publication_id,
        )
        assert result["status"] == "partial"
        assert result["published_count"] == 1
        assert result["failed_count"] == 1

        decision = TickerDecisionRepository(runtime).latest("LANE")
        assert decision is not None
        assert decision.selected_expression.kind is ExpressionKind.STOCK, (
            decision.context_blockers,
            (decision.resolution.primary_blocker, decision.resolution.blockers) if decision.resolution else None,
            (decision.opportunity_rank or {}).get("trade_rank_unavailable_reason"),
            (
                decision.trade_plan.primary_blocker,
                decision.trade_plan.blockers,
                decision.trade_plan.eligibility,
            ) if decision.trade_plan else None,
            decision.expressions[ExpressionKind.STOCK].status,
            decision.portfolio_impacts[ExpressionKind.STOCK].blockers,
        )
        assert decision.market_state_publication_id == market_publication_id
        assert decision.trade_plan is not None
        assert decision.trade_plan.eligibility == "ACTIONABLE"
        assert decision.trade_plan.authorization_mode in {"ADVISORY", "PAPER"}
        assert decision.trade_plan.availability_status is AvailabilityStatus.AVAILABLE
        assert artifact["availability_status"] == "available"
        assert decision.alpha_signals[0]["strategy_revision_id"] == artifact["strategy_revision_id"]
        assert decision.alpha_signals[0]["research_score"] == pytest.approx(0.195)
        assert all(item.available_at <= decision.cutoff for item in decision.input_lineage)
        assert decision.selected_expression.entry_range == PriceRange(low=99.0, high=101.0)
        assert "999.0" not in json.dumps(decision.input_manifest.inputs, default=str)
        assert not any("CALL" in blocker or "PUT" in blocker for blocker in decision.context_blockers)

        panel_snapshot.invalidate_context_cache()
        action_queue = today(config, SimpleNamespace(decision_inbox=lambda **_kwargs: {"items": []}))
        lane_action = next(item for item in action_queue["actions"] if item.get("ticker") == "LANE")
        assert lane_action["lifecycle_state"] == "actionable"
        assert lane_action["trade_plan"]["trade_plan_id"] == decision.trade_plan.trade_plan_id

        funnel = TickerDecisionRepository(runtime).decision_funnel(
            now=datetime.now(UTC) + timedelta(seconds=1), action_queue=action_queue["actions"],
        )
        assert funnel["policy_version"] == decision.opportunity_rank["ranking_version"]
        assert lane_action["policy_version"] == decision.policy_version
        assert all(
            next(stage for stage in funnel["stages"] if stage["stage"] == name)["count"] == 1
            for name in ("qualified_stock_alpha", "trade_rank", "trade_plan")
        )
        assert next(stage for stage in funnel["stages"] if stage["stage"] == "action_queue")["count"] == 1

        current_funnel = TickerDecisionRepository(runtime).decision_funnel(now=datetime.now(UTC))
        assert next(stage for stage in current_funnel["stages"] if stage["stage"] == "stock_expression")["count"] == 1
        assert next(stage for stage in current_funnel["stages"] if stage["stage"] == "portfolio_impact")["count"] == 1
        assert next(stage for stage in current_funnel["stages"] if stage["stage"] == "decision_resolution")["count"] == 1
    finally:
        runtime.close()


def test_decision_funnel_compact_publication_read_keeps_legacy_fallback(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        cutoff = datetime.now(UTC) - timedelta(seconds=1)
        analysis = AnalysisRepository(runtime)
        run_id = analysis.start_run(
            "legacy-funnel", input_cutoff=cutoff, code_version="test",
            inputs={"fixture": "legacy-funnel"},
        )
        analysis.finish_run(run_id, "succeeded")
        with runtime.transaction() as connection:
            publication = connection.execute(
                """
                INSERT INTO app.publication (
                    scope, analysis_run_id, status, published_at
                ) VALUES (
                    'ticker-opportunity-ranking', %s, 'published', %s
                ) RETURNING id::text
                """,
                    [run_id, cutoff + timedelta(microseconds=1)],
                ).fetchone()
            for model_name, payload in (
                ("alpha_signal", {
                    "ticker": "LEGACY", "availability_status": "available", "blockers": [],
                }),
                ("opportunity_rank", {
                    "ticker": "LEGACY", "availability_status": "available", "blockers": [],
                    "trade_rank": 1, "ranking_version": "ranking:legacy",
                }),
                ("trade_plan", {
                    "ticker": "LEGACY", "availability_status": "available", "blockers": [],
                    "eligibility": "ACTIONABLE",
                }),
            ):
                connection.execute(
                    """
                    INSERT INTO app.publication_item (
                        publication_id, model_name, stable_key, rank, payload
                    ) VALUES (%s::uuid, %s, 'LEGACY', 1, %s)
                    """,
                    [publication["id"], model_name, Jsonb(payload)],
                )

        alpha_rows, rank_rows, plan_rows = (
            TickerDecisionRepository(runtime)._current_funnel_publication_rows()
        )

        assert [row["ticker"] for row in alpha_rows] == ["LEGACY"]
        assert rank_rows[0]["trade_rank"] == 1
        assert rank_rows[0]["ranking_version"] == "ranking:legacy"
        assert plan_rows[0]["eligibility"] == "ACTIONABLE"
        assert {
            alpha_rows[0]["publication_id"],
            rank_rows[0]["publication_id"],
            plan_rows[0]["publication_id"],
        } == {publication["id"]}

        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE app.publication SET status = 'superseded' WHERE id = %s::uuid",
                [publication["id"]],
            )
        historical_alpha, historical_rank, historical_plan = (
            TickerDecisionRepository(runtime)._current_funnel_publication_rows(
                reference=cutoff + timedelta(minutes=1),
            )
        )
        assert [row["ticker"] for row in historical_alpha] == ["LEGACY"]
        assert historical_rank[0]["publication_id"] == historical_plan[0]["publication_id"] == publication["id"]
    finally:
        runtime.close()


def test_trade_rank_requires_published_qualified_oos_artifact_and_positive_net_utility(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        cutoff = datetime.now(UTC) + timedelta(seconds=2)
        _, available = _qualified_artifact(runtime, cutoff)
        assert available["availability_status"] == "available"
        with runtime.transaction() as connection:
            failed = connection.execute(
                """
                INSERT INTO analysis.strategy_evaluation (
                    strategy_revision_id, evaluation_type, evaluated_at,
                    period_start, period_end, verdict, metrics, evidence
                )
                SELECT strategy_revision_id, evaluation_type, %s,
                       period_start, period_end, 'fail', metrics, evidence
                FROM analysis.strategy_evaluation
                WHERE id = %s::uuid
                RETURNING id::text
                """,
                [cutoff - timedelta(milliseconds=1), available["strategy_evaluation_id"]],
            ).fetchone()
        blocked = AnalysisRepository(runtime).qualified_stock_alpha_artifact(cutoff=cutoff, horizon="TACTICAL")
        assert blocked["strategy_evaluation_id"] == failed["id"]
        assert blocked["availability_status"] == "policy_blocked"
        assert blocked["blockers"] == ["alpha_oos_evaluation_not_passed"]
    finally:
        runtime.close()

    candidate = _rank_candidate(cutoff, gross=100.0, costs=10.0)
    rank = rank_opportunities([candidate], evaluated_universe_complete=True)[0]
    assert rank.trade_rank == 1
    assert rank.availability_status is AvailabilityStatus.AVAILABLE
    no_net = rank_opportunities([_rank_candidate(cutoff, gross=10.0, costs=10.0)], evaluated_universe_complete=True)[0]
    assert no_net.trade_rank is None
    assert no_net.trade_rank_unavailable_reason == "lower_confidence_expected_net_pnl_not_positive"


def test_late_backdated_oos_evaluation_cannot_authorize_historical_decision(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        qualification_cutoff = datetime.now(UTC) + timedelta(seconds=5)
        revision_id, original = _qualified_artifact(runtime, qualification_cutoff)
        failed_evaluated_at = datetime.now(UTC) - timedelta(seconds=2)
        with runtime.transaction() as connection:
            failed = connection.execute(
                """
                INSERT INTO analysis.strategy_evaluation (
                    strategy_revision_id, evaluation_type, evaluated_at,
                    period_start, period_end, verdict, metrics, evidence
                )
                SELECT strategy_revision_id, evaluation_type, %s,
                       period_start, period_end, 'fail', metrics, evidence
                FROM analysis.strategy_evaluation
                WHERE id = %s::uuid
                RETURNING id::text
                """,
                [failed_evaluated_at, original["strategy_evaluation_id"]],
            ).fetchone()
        decision_cutoff = datetime.now(UTC)
        with runtime.transaction() as connection:
            late = connection.execute(
                """
                INSERT INTO analysis.strategy_evaluation (
                    strategy_revision_id, evaluation_type, evaluated_at, available_at,
                    period_start, period_end, verdict, metrics, evidence
                ) VALUES (%s, 'out_of_sample', %s, %s, %s, %s, 'pass', %s, '[]'::jsonb)
                RETURNING id::text, available_at
                """,
                [
                    revision_id,
                    decision_cutoff - timedelta(seconds=1),
                    decision_cutoff - timedelta(seconds=1),
                    decision_cutoff - timedelta(days=30),
                    decision_cutoff - timedelta(seconds=1),
                    Jsonb({
                        "artifact_id": "ticker-stock-alpha:v1",
                        "model_version": "ticker-stock-alpha.v1",
                        "cohort_id": "stock-oos-exact-v1",
                        "exact_cohort": True,
                        "valid_through": (decision_cutoff + timedelta(days=30)).isoformat(),
                    }),
                ],
            ).fetchone()
        assert late["available_at"] > decision_cutoff
        with pytest.raises(RaiseException, match="evaluation authority is immutable"):
            with runtime.transaction() as connection:
                connection.execute(
                    "UPDATE analysis.strategy_evaluation SET available_at = %s WHERE id = %s::uuid",
                    [decision_cutoff - timedelta(days=1), late["id"]],
                )
        artifact = AnalysisRepository(runtime).qualified_stock_alpha_artifact(
            cutoff=decision_cutoff, horizon="TACTICAL",
        )
        assert artifact["strategy_evaluation_id"] == failed["id"]
        assert artifact["availability_status"] == "policy_blocked"
        assert artifact["blockers"] == ["alpha_oos_evaluation_not_passed"]
    finally:
        runtime.close()


def test_unavailable_alternate_does_not_block_selected_stock() -> None:
    cutoff = datetime(2026, 8, 29, 14, tzinfo=UTC)
    stock = _expression(ExpressionKind.STOCK, cutoff, selected=True)
    call = _expression(ExpressionKind.CALL, cutoff, selected=False, available=False)
    cash = _expression(ExpressionKind.CASH, cutoff, selected=False)
    snapshot = _context_snapshot(cutoff)
    impacts = {
        ExpressionKind.STOCK: _impact(stock, snapshot, available=True),
        ExpressionKind.CASH: _impact(cash, snapshot, available=True),
        ExpressionKind.CALL: _impact(call, snapshot, available=False),
    }
    decision = TickerDecision.model_construct(
        market_state_snapshot=snapshot,
        risk_policy_snapshot=RiskPolicySnapshot(policy_version="risk-policy.v2:test"),
        portfolio_impacts=impacts,
        expressions={ExpressionKind.STOCK: stock, ExpressionKind.CALL: call, ExpressionKind.CASH: cash},
    )
    assert decision.context_blockers == ()


def test_isolated_ticker_failure_does_not_block_covered_universe() -> None:
    cutoff = datetime(2026, 8, 29, 14, tzinfo=UTC)
    universe = EligibleUniverseSnapshot(
        intended=("LANE", "B", "C", "D", "BROKEN"),
        available=("LANE", "B", "C", "D"),
        excluded_reasons={"BROKEN": "isolated source failure"},
        excluded_materiality={"BROKEN": False},
        source_failures={"BROKEN": ("isolated source failure",)},
        coverage_ratio=0.8,
        threshold=0.8,
    )
    rank = rank_opportunities([_rank_candidate(cutoff)], eligible_universe=universe)[0]
    assert rank.trade_rank == 1
    assert rank.eligible_universe is not None
    assert rank.eligible_universe.excluded_reasons == {"BROKEN": "isolated source failure"}


def test_blocked_decision_keeps_all_reasons_and_one_primary() -> None:
    resolution = build_decision_resolution(
        action="BUY",
        decision_revision="decision:blockers",
        policy_version="risk-policy.v2:test",
        provenance={"as_of": datetime(2026, 8, 29, 14, tzinfo=UTC)},
        blockers=["market_state_stale", "alpha_oos_evaluation_missing", "market_state_stale"],
        blocked=True,
    )
    assert resolution.blockers == ["market_state_stale", "alpha_oos_evaluation_missing"]
    assert resolution.primary_blocker in resolution.blockers
    assert len(resolution.blockers) == 2


def test_blocked_variants_expose_typed_availability() -> None:
    cutoff = datetime(2026, 8, 29, 14, tzinfo=UTC)
    signal = build_alpha_signal(
        ticker="LANE",
        opportunity_episode_id="episode:lane",
        decision_revision="decision:lane",
        instrument_state_snapshot_id="instrument:lane",
        as_of=cutoff,
        availability_status=AvailabilityStatus.NOT_CALIBRATED,
        blockers=("alpha_oos_evaluation_missing",),
    )
    rank = rank_opportunities([{
        **_rank_candidate(cutoff),
        "alpha_signal": signal,
        "alpha_signal_id": signal.signal_id,
    }], evaluated_universe_complete=True)[0]
    expression = _expression(ExpressionKind.CALL, cutoff, selected=False, available=False)
    assert signal.availability_status is AvailabilityStatus.NOT_CALIBRATED
    assert expression.availability_status is AvailabilityStatus.UNSUPPORTED
    assert rank.availability_status is AvailabilityStatus.NOT_CALIBRATED
    assert rank.primary_blocker == "alpha_oos_evaluation_missing"


def test_publication_visibility_identity_freshness_and_lineage(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        market_cutoff = datetime.now(UTC) - timedelta(minutes=5)
        publication_id, publication = _publish_market(runtime, market_cutoff)
        published_at = _timestamp(publication["published_at"])
        assert published_at > market_cutoff
        decision_cutoff = published_at + timedelta(seconds=1)
        selected = ticker_decisions._market_snapshot_for_decision(
            publication, decision_cutoff, max_age=timedelta(hours=1),
        )
        assert selected is not None
        assert selected.publication_id == publication_id
        assert selected.input_cutoff == market_cutoff
        assert ticker_decisions._market_snapshot_for_decision(
            publication, decision_cutoff + timedelta(hours=2), max_age=timedelta(hours=1),
        ) is None
        altered = deepcopy(publication)
        altered["models"]["market_state_snapshot"][0]["publication_id"] = "wrong-publication"
        assert ticker_decisions._market_snapshot_for_decision(
            altered, decision_cutoff, max_age=timedelta(hours=1),
        ) is None
        future = dict(publication)
        future["source_lineage"] = [{
            **publication["source_lineage"][0],
            "available_at": (market_cutoff + timedelta(seconds=1)).isoformat(),
        }]
        assert ticker_decisions._market_snapshot_for_decision(
            future, decision_cutoff, max_age=timedelta(hours=1),
        ) is None
    finally:
        runtime.close()


def test_exact_market_publication_history_remains_valid_after_supersession(
    migrated_postgres_dsn: str,
    monkeypatch,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        market_cutoff = datetime.now(UTC) - timedelta(minutes=5)
        first_id, first = _publish_market(runtime, market_cutoff)
        decision_cutoff = _timestamp(first["published_at"])
        second_id, _ = _publish_market(runtime, market_cutoff + timedelta(minutes=1))
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE app.publication SET published_at = %s WHERE id = %s::uuid",
                [decision_cutoff + timedelta(seconds=1), second_id],
            )
        repository = AnalysisRepository(runtime)
        first = repository.publication_by_id("market", first_id)
        second = repository.publication_by_id("market", second_id)
        assert first is not None and first["publication_status"] == "superseded"
        assert second is not None and second["publication_status"] == "published"

        monkeypatch.setattr(AnalysisRepository, "publication_rows", lambda *_args, **_kwargs: [])
        selected_publication = {"id": first_id}
        decision_repository = TickerDecisionRepository(runtime)
        expression = _expression(ExpressionKind.STOCK, decision_cutoff, selected=True)
        episode = build_opportunity_episode(
            ticker="LANE",
            decision_revision="decision:market-history",
            policy_version="risk-policy.v2:test",
            cutoff=decision_cutoff,
            input_lineage=[InputLineage(
                field="price",
                source_id="phase0-market-history",
                available_at=decision_cutoff,
                opportunity_episode_id="episode:market-history",
                decision_revision="decision:market-history",
                policy_version="risk-policy.v2:test",
                cutoff=decision_cutoff,
            )],
            expressions={ExpressionKind.STOCK: expression},
            selected_expression=ExpressionKind.STOCK,
            episode_id="episode:market-history",
        )
        impact = PortfolioImpact(
            impact_id="impact:market-history", ticker="LANE",
            opportunity_episode_id="episode:market-history",
            expression_kind=ExpressionKind.STOCK,
            expression_identity=trade_expression_identity(expression),
            decision_revision="decision:market-history",
            risk_policy_version="risk-policy.v2:test",
            market_snapshot_id="phase0-market-state:v1",
            market_state_publication_id=first_id,
            cutoff=decision_cutoff,
            input_lineage=episode.input_lineage,
            availability="unavailable",
            blockers=("portfolio_context_missing",),
        )
        resolution = build_decision_resolution(
            action="NO_TRADE", decision_revision="decision:market-history",
            policy_version="risk-policy.v2:test", ticker="LANE",
            provenance={},
            blockers=("portfolio_context_missing",), blocked=True,
            authorization_mode="NONE", data_quality="INCOMPLETE",
        )

        def compact_rows(**_kwargs):
            impact_row = impact.model_dump(mode="json")
            impact_row["market_state_publication_id"] = selected_publication["id"]
            return [{
                "ticker": "LANE",
                "as_of": decision_cutoff,
                "published_at": decision_cutoff,
                "decision_revision": "decision:market-history",
                "policy_version": "risk-policy.v2:test",
                "opportunity_episode_id": "episode:market-history",
                "opportunity_episode": episode.model_dump(mode="json"),
                "opportunity_cutoff_match": True,
                "opportunity_expressions_match": True,
                "opportunity_selected_expression_match": True,
                "selected_expression": {"kind": "STOCK", "horizon": "TACTICAL"},
                "stock_expression": expression.model_dump(mode="json"),
                "stock_portfolio_impact": impact_row,
                "resolution": resolution.model_dump(mode="json"),
                "market_state_publication_id": selected_publication["id"],
            }]

        monkeypatch.setattr(
            decision_repository,
            "_current_funnel_rows",
            compact_rows,
        )

        funnel = decision_repository.decision_funnel(now=decision_cutoff)
        facts = next(stage for stage in funnel["stages"] if stage["stage"] == "point_in_time_facts")
        assert facts["count"] == 1

        selected_publication["id"] = second_id
        funnel = decision_repository.decision_funnel(now=decision_cutoff)
        facts = next(stage for stage in funnel["stages"] if stage["stage"] == "point_in_time_facts")
        assert facts["count"] == 0

        selected_publication["id"] = first_id
        mismatched = {**first, "publication_id": second_id}
        monkeypatch.setattr(AnalysisRepository, "publication_by_id", lambda *_args: mismatched)
        funnel = decision_repository.decision_funnel(now=decision_cutoff)
        facts = next(stage for stage in funnel["stages"] if stage["stage"] == "point_in_time_facts")
        assert facts["count"] == 0
    finally:
        runtime.close()


def test_actionable_plan_has_all_executable_fields() -> None:
    cutoff = datetime(2026, 8, 29, 14, tzinfo=UTC)
    lineage = InputLineage(field="price", source_id="phase0", available_at=cutoff, cutoff=cutoff)
    snapshot = _context_snapshot(cutoff)
    expression = _expression(ExpressionKind.STOCK, cutoff, selected=True).model_copy(update={
        "invalidation": Invalidation(kind="price", statement="Close below 90", value=90.0),
    })
    episode = build_opportunity_episode(
        ticker="LANE",
        decision_revision="decision:lane",
        policy_version="risk-policy.v2:test",
        cutoff=cutoff,
        input_lineage=(lineage,),
        expressions={ExpressionKind.STOCK: expression},
        selected_expression=expression,
        episode_id="episode:lane",
    )
    impact = PortfolioImpact.compose(
        episode=episode,
        expression=expression,
        snapshot=snapshot,
        policy_version="risk-policy.v2:test",
        portfolio_replay=_portfolio_replay(cutoff),
    )
    resolution = build_decision_resolution(
        action="BUY",
        decision_revision="decision:lane",
        policy_version="risk-policy.v2:test",
        provenance={"as_of": cutoff},
        ticker="LANE",
        entry=expression.entry_range,
        size=expression.quantity,
        invalidation=expression.invalidation,
        exit=expression.target_range,
        ttl=cutoff + timedelta(days=30),
        expires_at=cutoff + timedelta(days=30),
        portfolio_context=impact.model_dump(mode="json"),
        data_quality="FRESH",
        authorization_mode="ADVISORY",
        rationale="Phase 0 executable stock plan",
    )
    decision = TickerDecision.model_construct(
        ticker="LANE",
        as_of=cutoff,
        decision_revision="decision:lane",
        resolution=resolution,
        policy_version="risk-policy.v2:test",
        expressions={ExpressionKind.STOCK: expression},
        selected_expression=expression,
        opportunity_episode=episode,
        input_manifest=SimpleNamespace(input_hash="phase0"),
        risk_policy_snapshot=RiskPolicySnapshot(policy_version="risk-policy.v2:test"),
        market_state_publication_id="publication:lane",
        market_state_snapshot=snapshot,
        portfolio_impacts={ExpressionKind.STOCK: impact},
    )
    plan = build_trade_plan(
        decision=decision,
        rank={
            "rank_id": "rank:lane",
            "ticker": "LANE",
            "decision_revision": "decision:lane",
            "opportunity_episode_id": "episode:lane",
            "selected_expression_kind": "STOCK",
            "selected_expression_identity": trade_expression_identity(expression),
            "portfolio_impact_id": impact.impact_id,
            "alpha_signal_id": "signal:lane",
            "trade_rank": 1,
            "trade_utility": 90.0,
            "evaluated_universe_complete": True,
        },
        alpha_signal={"signal_id": "signal:lane"},
    )
    assert all(getattr(plan, field) is not None for field in (
        "quantity", "entry", "entry_limit", "max_loss_per_unit", "planned_loss",
        "invalidation", "profit_exit", "cutoff", "policy_version", "rank_id",
        "alpha_signal_id", "portfolio_impact_id", "market_snapshot_id",
        "market_state_publication_id",
    ))


def _rank_candidate(cutoff: datetime, *, gross: float = 100.0, costs: float = 10.0) -> dict[str, object]:
    lineage = InputLineage(
        field="price", source_id="phase0", available_at=cutoff, cutoff=cutoff,
    )
    signal = build_alpha_signal(
        ticker="LANE",
        opportunity_episode_id="episode:lane",
        decision_revision="decision:lane",
        instrument_state_snapshot_id="instrument:lane",
        as_of=cutoff,
        input_lineage=(lineage,),
        target="expected_return",
        horizon="TACTICAL",
        direction="BULLISH",
        forecast_value=0.10,
        cohort_id="stock-oos-exact-v1",
        calibration_state="calibrated_exact_cohort",
        model_version="ticker-stock-alpha.v1",
        feature_version="stock-features.v1",
        evaluation_stage="out_of_sample",
        availability_status=AvailabilityStatus.AVAILABLE,
        strategy_key="ticker-stock-alpha",
        strategy_revision_id=1,
        model_artifact_id="ticker-stock-alpha:v1",
        strategy_evaluation_id="evaluation:lane",
        artifact_published_at=cutoff - timedelta(minutes=2),
        evaluation_evaluated_at=cutoff - timedelta(minutes=1),
        evaluation_available_at=cutoff - timedelta(minutes=1),
        oos_period_start=cutoff - timedelta(days=30),
        oos_period_end=cutoff - timedelta(minutes=3),
        cohort_path=("cohort:stock-oos-exact-v1",),
        fallback_parent="horizon:TACTICAL",
        effective_sample_size=40,
        calibration_metrics={"brier_score": 0.2, "calibration_error": 0.1},
        research_score=0.5,
        cost_model_version="stock-cost-slippage.v1",
        promotion_stage="advisory",
        lower_confidence_net_utility_after_costs=0.02,
    )
    expression = {
        "kind": "STOCK",
        "ticker": "LANE",
        "horizon": "TACTICAL",
        "thesis_revision": "decision:lane",
        "stance": "BULLISH",
        "status": "eligible",
        "rationale": "Phase 0 stock expression",
    }
    expression_identity = trade_expression_identity(expression)
    return {
        "ticker": "LANE",
        "opportunity_episode_id": "episode:lane",
        "decision_revision": "decision:lane",
        "policy_version": "risk-policy.v2:test",
        "selected_expression_identity": expression_identity,
        "selected_expression_kind": "STOCK",
        "portfolio_impact_id": "impact:lane",
        "risk_policy_version": "risk-policy.v2:test",
        "alpha_signal_id": signal.signal_id,
        "alpha_signal": signal,
        "instrument_state_snapshot_id": "instrument:lane",
        "market_snapshot_id": "market:lane",
        "market_state_publication_id": "publication:lane",
        "cutoff": cutoff,
        "input_lineage": (lineage,),
        "portfolio_impact": {
            "impact_id": "impact:lane",
            "availability": "available",
            "blockers": [],
            "expression_kind": "STOCK",
            "expression_identity": expression_identity,
            "decision_revision": "decision:lane",
            "opportunity_episode_id": "episode:lane",
            "market_snapshot_id": "market:lane",
            "market_state_publication_id": "publication:lane",
            "risk_policy_version": "risk-policy.v2:test",
            "cutoff": cutoff,
            "input_lineage": (lineage,),
        },
        "risk_policy_snapshot": {"policy_version": "risk-policy.v2:test", "blockers": []},
        "expression": expression,
        "execution_feasible": True,
        "lower_confidence_expected_gross_pnl": gross,
        "expected_transaction_costs": costs,
        "tail_risk_penalty": 0.0,
        "portfolio_overlap_penalty": 0.0,
        "diversification_benefit": 0.0,
        "capital_at_risk": 1_000.0,
    }


def _expression(
    kind: ExpressionKind,
    cutoff: datetime,
    *,
    selected: bool,
    available: bool = True,
) -> ExpressionDecision:
    return ExpressionDecision(
        kind=kind,
        ticker="LANE",
        horizon=Horizon.TACTICAL,
        thesis_revision="thesis:lane",
        stance=Stance.BULLISH if kind is not ExpressionKind.CASH else Stance.NEUTRAL,
        entry_range=PriceRange(low=99, high=101) if kind is ExpressionKind.STOCK else None,
        target_range=PriceRange(low=120, high=125) if kind is ExpressionKind.STOCK else None,
        quantity=10 if kind is ExpressionKind.STOCK else None,
        max_loss_per_unit=100 if kind is ExpressionKind.STOCK else None,
        planned_loss=1_000 if kind is ExpressionKind.STOCK else 0 if kind is ExpressionKind.CASH else None,
        status="eligible" if available else "unavailable",
        availability_status=AvailabilityStatus.AVAILABLE if available else AvailabilityStatus.UNSUPPORTED,
        blockers=() if available else ("option_surface_unavailable",),
        selected=selected,
        rationale=f"{kind.value} fixture at {cutoff.isoformat()}",
    )


def _context_snapshot(cutoff: datetime) -> MarketStateSnapshot:
    return MarketStateSnapshot(
        snapshot_id="market:lane",
        publication_id="publication:lane",
        as_of=cutoff,
        input_cutoff=cutoff,
    ).model_copy(update={
        "availability": "available",
        "availability_status": AvailabilityStatus.AVAILABLE,
    })


def _impact(expression: ExpressionDecision, snapshot: MarketStateSnapshot, *, available: bool) -> PortfolioImpact:
    impact = PortfolioImpact(
        impact_id=f"impact:{expression.kind.value}",
        ticker="LANE",
        opportunity_episode_id="episode:lane",
        expression_kind=expression.kind,
        expression_identity=trade_expression_identity(expression),
        decision_revision="decision:lane",
        risk_policy_version="risk-policy.v2:test",
        market_snapshot_id=snapshot.snapshot_id,
        market_state_publication_id=snapshot.publication_id,
        cutoff=snapshot.input_cutoff,
        availability="unavailable",
        availability_status=AvailabilityStatus.UNSUPPORTED,
        blockers=("option_impact_unavailable",),
    )
    if not available:
        return impact
    return impact.model_copy(update={
        "availability": "available",
        "availability_status": AvailabilityStatus.AVAILABLE,
        "blockers": (),
    })


def _timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
