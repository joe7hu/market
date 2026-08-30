from __future__ import annotations

from datetime import UTC, datetime

from investment_panel.core.decision import (
    build_ticker_decision,
    build_trade_plan,
    bind_trade_plan,
    trade_expression_identity,
)
from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.database.ticker_decisions import TickerDecisionRepository


def test_options_and_crypto_share_episode_rank_impact_resolution_plan(
    migrated_postgres_dsn: str,
) -> None:
    """BIG-A12: cross-asset candidates remain one canonical decision graph."""
    cutoff = datetime(2026, 8, 22, 14, tzinfo=UTC)
    symbol = "CROSS"
    tables = {
        "quotes": [{
            "symbol": symbol, "price": 100, "available_at": cutoff, "confirmed": True,
        }],
        "portfolio_summary": [{
            "symbol": symbol, "net_liquidation": 100_000, "available_at": cutoff,
        }],
        "decision_queue": [{
            "symbol": symbol, "stance": "BULLISH", "entry_low": 99, "entry_high": 101,
            "target_low": 110, "target_high": 120, "invalidation_price": 90,
            "available_at": cutoff,
        }],
        "options_payoff_scenarios": [{
            "symbol": symbol, "structure": "long_call", "max_loss": 250,
            "entry_price": 2, "available_at": cutoff,
            "legs": [{
                "bid": 2, "ask": 2.2, "bid_size": 10, "ask_size": 10,
                "quote_time": cutoff,
            }],
            "delta": .4, "gamma": .1, "vega": .2, "theta": -.03,
            "skew": .02, "term_structure": {"slope": .1},
            "event_gap_scenarios": {"gap_down": -1},
            "assignment": {"status": "none"}, "collateral": {"required": 250},
            "slippage": .01, "days_to_exit": 3, "capacity": 100,
            "multi_leg_liquidity": {"status": "available"},
        }],
        "crypto_spot_quotes": [{
            "symbol": symbol, "kind": "spot", "price": 100, "expected_value": 1,
            "max_loss": 1, "planned_loss": 1, "available_at": cutoff,
            "source_id": "crypto-test", "status": "available",
            "btc_beta": 1, "eth_beta": .5, "funding": .001, "basis": .002,
            "open_interest": 100, "liquidation_regime": "normal",
            "venue_risk": "low", "counterparty_risk": "low",
            "stablecoin_liquidity": "deep", "ttl_seconds": 86_400,
        }],
    }
    decision = build_ticker_decision(
        symbol, tables, as_of=cutoff, portfolio_impacts={},
    )
    assert decision.opportunity_episode is not None
    episode = decision.opportunity_episode
    assert {kind.value for kind in episode.expressions} >= {"CALL", "CRYPTO_SPOT", "CASH"}
    assert trade_expression_identity(decision.expressions["CALL"]) != trade_expression_identity(
        decision.expressions["CRYPTO_SPOT"]
    )

    cash = decision.selected_expression
    assert cash is not None and cash.kind.value == "CASH"
    cash_impact = decision.portfolio_impacts[cash.kind]
    rank = {
        "rank_id": "rank:CROSS:phase6",
        "ticker": symbol,
        "opportunity_episode_id": decision.opportunity_episode_id,
        "decision_revision": decision.decision_revision,
        "policy_version": decision.policy_version,
        "selected_expression_identity": trade_expression_identity(cash),
        "selected_expression_kind": "CASH",
        "portfolio_impact_id": cash_impact.impact_id,
        "alpha_signal_id": "alpha:CROSS:phase6",
        "trade_rank": None,
        "trade_utility": None,
        "evaluated_universe_complete": False,
    }
    plan = build_trade_plan(
        decision=decision, rank=rank,
        alpha_signal={"signal_id": "alpha:CROSS:phase6"},
    )
    bound = bind_trade_plan(decision, plan).model_copy(update={"opportunity_rank": rank})
    assert plan.opportunity_episode_id == episode.episode_id
    assert plan.selected_expression_identity == trade_expression_identity(bound.selected_expression)
    assert plan.portfolio_impact_id == cash_impact.impact_id
    assert bound.resolution is not None
    assert bound.resolution.trade_plan_id == plan.trade_plan_id

    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        with runtime.transaction() as connection:
            connection.execute(
                "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES (%s, %s, %s)",
                [symbol, "Cross Asset Test", "equity"],
            )
        repository = TickerDecisionRepository(runtime)
        repository.publish(bound)
        replay = repository.latest(symbol)
        assert replay is not None
        assert replay.opportunity_episode_id == episode.episode_id
        assert replay.trade_plan is not None
        assert replay.trade_plan.trade_plan_id == plan.trade_plan_id
        assert replay.resolution is not None
        assert replay.resolution.trade_plan_id == replay.trade_plan.trade_plan_id
        assert replay.opportunity_rank is not None
        assert replay.opportunity_rank["opportunity_episode_id"] == replay.opportunity_episode_id
        assert replay.opportunity_rank["selected_expression_identity"] == replay.trade_plan.selected_expression_identity
    finally:
        runtime.close()
