from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from investment_panel.core.decision import (
    ExpressionKind,
    InputLineage,
    MARKET_DIMENSIONS,
    MARKET_HORIZONS,
    MarketStateSnapshot,
    build_ticker_decision,
    trade_expression_identity,
)


AS_OF = datetime(2026, 8, 22, 14, 0, tzinfo=UTC)


def _complete_replay(*, book_identity: str = "portfolio-book:test") -> dict[str, object]:
    return {
        "cutoff": AS_OF,
        "positions": [],
        "portfolio_value": 0.0,
        "transaction_count": 0,
        "eligible_position_count": 0,
        "valued_position_count": 0,
        "missing_valuation_count": 0,
        "valuation_complete": True,
        "lineage": [],
        "book_identity": book_identity,
    }


def _decision(**context):
    context.setdefault("portfolio_replay", _complete_replay())
    return build_ticker_decision(
        "ACME",
        {
            "quotes": [{"symbol": "ACME", "price": 100, "available_at": "2026-08-22T13:55:00Z", "confirmed": True}],
            "portfolio_summary": [{"net_liquidation": 100_000, "available_at": "2026-08-22T13:55:00Z"}],
            "decision_queue": [{
                "symbol": "ACME", "stance": "BULLISH", "action": "BUY",
                "entry_low": 99, "entry_high": 101, "invalidation_price": 90,
                "conviction_tier": "STANDARD", "available_at": "2026-08-22T13:55:00Z",
            }],
        },
        as_of=AS_OF,
        **context,
    )


def test_market_snapshot_has_four_horizons_and_unavailable_dimensions() -> None:
    snapshot = _decision().market_state_snapshot

    assert snapshot is not None
    assert tuple(snapshot.horizons) == MARKET_HORIZONS
    assert len(snapshot.coverage_matrix.rows) == len(MARKET_HORIZONS) * len(MARKET_DIMENSIONS)
    for horizon in MARKET_HORIZONS:
        dimensions = {item.dimension: item for item in snapshot.horizons[horizon]}
        assert set(dimensions) == set(MARKET_DIMENSIONS)
        assert all(item.input_cutoff == AS_OF for item in snapshot.coverage_matrix.rows if item.horizon == horizon)
        assert dimensions["rates"].state is None
        assert dimensions["rates"].probability is None


def test_every_expression_has_one_portfolio_impact_including_cash() -> None:
    decision = _decision()

    assert set(decision.portfolio_impacts) == set(decision.expressions) | {ExpressionKind.CASH}
    for kind, expression in decision.expressions.items():
        impact = decision.portfolio_impacts[kind]
        assert impact.expression_kind is kind
        assert impact.expression_identity == trade_expression_identity(expression)
        assert impact.opportunity_episode_id == decision.opportunity_episode_id
        assert impact.decision_revision == decision.decision_revision
        assert impact.risk_policy_version == decision.policy_version
        assert impact.market_snapshot_id == decision.market_state_snapshot.snapshot_id
        assert impact.cutoff == decision.cutoff

    cash = decision.portfolio_impacts[ExpressionKind.CASH]
    assert cash.availability == "available"
    assert cash.portfolio_before == cash.portfolio_after
    assert cash.marginal_risk == 0
    assert cash.risk_budget_consumed == 0
    non_cash = next(impact for kind, impact in decision.portfolio_impacts.items() if kind is not ExpressionKind.CASH)
    assert non_cash.availability == "unavailable"
    assert "stock_nav_missing" in non_cash.blockers


def test_book_identity_changes_every_bound_impact_and_never_unlocks_non_cash() -> None:
    first = _decision(portfolio_replay=_complete_replay(book_identity="portfolio-book:first"))
    second = _decision(portfolio_replay=_complete_replay(book_identity="portfolio-book:second"))

    for kind in first.portfolio_impacts:
        assert first.portfolio_impacts[kind].impact_id != second.portfolio_impacts[kind].impact_id
    assert second.portfolio_impacts[ExpressionKind.CASH].availability == "available"
    assert all(
        impact.availability == "unavailable"
        for kind, impact in second.portfolio_impacts.items()
        if kind is not ExpressionKind.CASH
    )


def test_stock_impact_reports_first_order_exposure_from_cutoff_book() -> None:
    replay = _complete_replay(book_identity="portfolio-book:valued")
    replay.update({
        "portfolio_value": 100_000.0,
        "positions": [{
            "instrument_id": 1,
            "symbol": "ACME",
            "quantity": 10.0,
            "avg_cost": 90.0,
            "price": 100.0,
            "market_value": 1_000.0,
            "source_id": "test",
            "currency": "USD",
            "source_kind": "daily_bars",
            "trading_date": "2026-08-22",
            "observed_at": AS_OF,
            "available_at": AS_OF,
            "valuation_status": "market_quotes",
        }],
        "eligible_position_count": 1,
        "valued_position_count": 1,
    })
    decision = _decision(portfolio_replay=replay)
    impact = decision.portfolio_impacts[ExpressionKind.STOCK]

    assert impact.impact_method == "stock_portfolio_impact.v1:first_order"
    assert impact.position_weight_after is not None
    assert impact.position_weight_after > impact.position_weight_before
    assert impact.gross_exposure_after > impact.gross_exposure_before
    assert "stock_beta_evidence_missing" in impact.blockers
    assert impact.availability == "unavailable"


def test_supplied_policy_snapshot_must_match_canonical_point_in_time_authority() -> None:
    seed = _decision()
    assert seed.risk_policy_snapshot is not None
    supplied = seed.risk_policy_snapshot.model_copy(update={"cash_balance": 1.0})

    decision = _decision(
        market_state_snapshot=seed.market_state_snapshot,
        portfolio_impacts=seed.portfolio_impacts,
        risk_policy_snapshot=supplied,
    )

    assert decision.risk_policy_snapshot is not None
    assert decision.risk_policy_snapshot.cash_balance is None
    assert "risk_policy_snapshot_mismatch" in decision.context_blockers
    assert decision.resolution is not None
    assert decision.resolution.action.value == "NO_TRADE"
    assert decision.resolution.size is None


def test_future_market_lineage_is_rejected() -> None:
    future = AS_OF + timedelta(minutes=1)
    lineage = InputLineage(field="rates", source_id="test", source_version="1", available_at=future)

    with pytest.raises(ValueError, match="newer than its cutoff"):
        MarketStateSnapshot(
            snapshot_id="future",
            as_of=AS_OF,
            input_cutoff=AS_OF,
            input_lineage=(lineage,),
        )


def test_missing_context_blocks_resolution() -> None:
    decision = _decision(
        market_state_snapshot=None,
        portfolio_impacts=None,
        risk_policy_snapshot=None,
    )

    assert decision.resolution is not None
    assert decision.resolution.is_actionable is False
    assert decision.resolution.is_blocked is True
    assert decision.capital_action.action.value == "AVOID"
    assert decision.resolution.size is None
    assert "market_state_missing" in decision.resolution.blockers
    assert "risk_policy_snapshot_missing" in decision.context_blockers
    assert decision.context_blockers
