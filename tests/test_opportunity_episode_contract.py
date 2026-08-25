from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.data_access.payloads import option_decision_adapter
from investment_panel.core.decision import (
    ExpressionKind,
    Horizon,
    InputLineage,
    OpportunityEpisode,
    Stance,
    TradeExpression,
    build_ticker_decision,
)


CUTOFF = datetime(2026, 8, 22, 14, 0, tzinfo=UTC)


def _expression(kind: ExpressionKind, *, selected: bool = False) -> TradeExpression:
    return TradeExpression(
        kind=kind,
        ticker="ACME",
        horizon=Horizon.TACTICAL,
        thesis_revision="thesis-1",
        stance=Stance.BULLISH,
        scenarios=[
            {"name": "bear", "probability": 0.2, "description": "bear"},
            {"name": "base", "probability": 0.5, "description": "base"},
            {"name": "bull", "probability": 0.3, "description": "bull"},
        ],
        status="eligible",
        selected=selected,
        rationale="shared ticker thesis",
    )


def _lineage(**updates: object) -> InputLineage:
    values = {
        "field": "quote",
        "source_id": "confirmed-quotes",
        "source_version": "quotes-1",
        "available_at": CUTOFF - timedelta(minutes=5),
        "opportunity_episode_id": "episode-1",
        "decision_revision": "decision-1",
        "policy_version": "policy-1",
        "cutoff": CUTOFF,
    }
    values.update(updates)
    return InputLineage(
        **values,
    )


def _episode(**updates: object) -> OpportunityEpisode:
    expressions = {
        ExpressionKind.STOCK: _expression(ExpressionKind.STOCK),
        ExpressionKind.CALL: _expression(ExpressionKind.CALL, selected=True),
    }
    values = {
        "episode_id": "episode-1",
        "ticker": "ACME",
        "decision_revision": "decision-1",
        "policy_version": "policy-1",
        "cutoff": CUTOFF,
        "input_lineage": [_lineage()],
        "expressions": expressions,
        "selected_expression": expressions[ExpressionKind.CALL],
    }
    values.update(updates)
    return OpportunityEpisode(
        **values,
    )


def test_episode_has_multiple_trade_expressions_and_one_selection() -> None:
    episode = _episode()

    assert isinstance(episode.expressions[ExpressionKind.CALL], TradeExpression)
    assert episode.selected_expression is not None
    assert episode.selected_expression.kind is ExpressionKind.CALL
    assert sum(expression.selected for expression in episode.expressions.values()) == 1
    assert episode.input_lineage[0].opportunity_episode_id == episode.episode_id


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"input_lineage": []}, "lineage"),
        ({"input_lineage": [_lineage(available_at=CUTOFF + timedelta(seconds=1))]}, "newer"),
        ({"input_lineage": [_lineage(decision_revision="other")]}, "revision"),
        ({"input_lineage": [_lineage(policy_version="other")]}, "policy"),
        ({"input_lineage": [_lineage(cutoff=CUTOFF - timedelta(seconds=1))]}, "cutoff"),
    ],
)
def test_episode_fails_closed_on_invalid_lineage(change: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _episode(**change)


def test_episode_rejects_duplicate_lineage_and_multiple_selected_expressions() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        _episode(input_lineage=[_lineage(), _lineage()])

    expressions = {
        ExpressionKind.STOCK: _expression(ExpressionKind.STOCK, selected=True),
        ExpressionKind.CALL: _expression(ExpressionKind.CALL, selected=True),
    }
    with pytest.raises(ValueError, match="at most one"):
        OpportunityEpisode(
            episode_id="episode-1",
            ticker="ACME",
            decision_revision="decision-1",
            policy_version="policy-1",
            cutoff=CUTOFF,
            input_lineage=[_lineage()],
            expressions=expressions,
        )


def test_ticker_builder_publishes_one_episode_for_stock_and_option_paths() -> None:
    decision = build_ticker_decision(
        "ACME",
        {
            "quotes": [{
                "symbol": "ACME", "price": 100,
                "available_at": "2026-08-22T13:55:00Z", "confirmed": True,
            }],
            "portfolio_summary": [{
                "net_liquidation": 100_000,
                "available_at": "2026-08-22T13:55:00Z",
            }],
            "decision_queue": [{
                "symbol": "ACME", "stance": "BULLISH", "entry_low": 99,
                "entry_high": 101, "invalidation_price": 90,
                "available_at": "2026-08-22T13:55:00Z",
            }],
            "options_payoff_scenarios": [{
                "symbol": "ACME", "structure": "long_call", "max_loss": 250,
                "lower_confidence_expectancy": 0.2,
                "expiration": "2026-10-16",
                "available_at": "2026-08-22T13:55:00Z",
                "legs": [{
                    "contract_id": 1, "option_type": "call", "side": "long",
                    "strike": 105, "bid": 2, "ask": 2.2,
                    "bid_size": 10, "ask_size": 10,
                    "quote_time": "2026-08-22T13:55:00Z",
                }],
            }],
        },
        as_of=CUTOFF,
    )

    episode = decision.opportunity_episode
    assert episode is not None
    assert episode.ticker == decision.ticker
    assert episode.decision_revision == decision.decision_revision
    assert episode.policy_version == decision.policy_version
    assert episode.cutoff == decision.as_of
    assert len(episode.expressions) >= 2
    assert sum(expression.selected for expression in episode.expressions.values()) <= 1
    assert all(lineage.available_at <= episode.cutoff for lineage in episode.input_lineage)


def test_options_adapter_carries_episode_identity_and_fails_closed_on_future_lineage() -> None:
    decision = build_ticker_decision(
        "ACME",
        {"decision_queue": [{"symbol": "ACME", "stance": "BULLISH", "available_at": "2026-08-22T13:55:00Z"}]},
        as_of=CUTOFF,
    )
    raw = decision.model_dump(mode="json")
    adapted = option_decision_adapter(raw, {})

    assert adapted["opportunity_episode_id"] == decision.opportunity_episode_id
    assert adapted["decision_truth"]["decision_revision"] == decision.decision_revision
    assert adapted["decision_truth"]["input_lineage"]

    raw["opportunity_episode"]["input_lineage"][0]["available_at"] = "2026-08-22T15:00:00Z"
    blocked = option_decision_adapter(raw, {})
    assert blocked["state"] == "WATCH"
    assert blocked["decision_truth"]["primary_blocker"] == "opportunity_lineage_invalid"
