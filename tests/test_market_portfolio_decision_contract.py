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


def _decision(ticker: str = "ACME", **context):
    context.setdefault("portfolio_replay", _complete_replay())
    return build_ticker_decision(
        ticker,
        {
            "quotes": [{"symbol": ticker, "price": 100, "available_at": "2026-08-22T13:55:00Z", "confirmed": True}],
            "portfolio_summary": [{"net_liquidation": 100_000, "available_at": "2026-08-22T13:55:00Z"}],
            "decision_queue": [{
                "symbol": ticker, "stance": "BULLISH", "action": "BUY",
                "entry_low": 99, "entry_high": 101, "invalidation_price": 90,
                "conviction_tier": "STANDARD", "available_at": "2026-08-22T13:55:00Z",
            }],
        },
        as_of=AS_OF,
        **context,
    )


def _valued_replay(*, stock_evidence: dict[str, object]) -> dict[str, object]:
    replay = _complete_replay(book_identity="portfolio-book:valued")
    replay.update({
        "portfolio_value": 100_000.0,
        "positions": [{
            "instrument_id": 1,
            "symbol": "ACME",
            "sector": "Technology",
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
        "stock_evidence": stock_evidence,
    })
    return replay


def _complete_stock_evidence(*, include_btc: bool = False) -> dict[str, object]:
    scenarios: dict[str, object] = {
        "SPY": {
            "pnl_by_shock": {"-5": -500.0, "-10": -1_000.0},
            "shock_pct": [-5.0, -10.0],
        },
        "QQQ": {
            "pnl_by_shock": {"-5": -450.0, "-10": -900.0},
            "shock_pct": [-5.0, -10.0],
        },
        "sector": {"pnl": -800.0, "shock_pct": -10.0},
        "symbol": {
            "pnl_by_shock": {"-20": -1_200.0, "-30": -1_800.0},
            "shock_pct": [-20.0, -30.0],
        },
        "earnings-gap": {
            "pnl": -1_500.0,
            "largest_holding": "ACME",
            "earnings_gap": True,
        },
        "liquidity": {
            "pnl": -500.0,
            "spread_multiplier": 2.0,
            "slippage_multiplier": 2.0,
            "adv_haircut_pct": 50.0,
        },
    }
    if include_btc:
        scenarios["BTC"] = {"pnl": -700.0, "shock_pct": -15.0}
    return {
        "sector": "Technology",
        "beta": 1.1,
        "avg_dollar_volume": 1_000_000.0,
        "correlation_cluster_delta": 0.01,
        "adv_participation_limit": 0.10,
        "stress_scenarios": scenarios,
        "risk_budget": {"available": 2_000.0, "consumed": 1_000.0},
        "cash_comparator": {"status": "available", "expected_return": 0.0},
        "top_alternative": "QQQ",
        "funding_source_or_position_to_trim": "cash",
    }


def _crypto_replay(*, stock_evidence: dict[str, object]) -> dict[str, object]:
    replay = _valued_replay(stock_evidence=stock_evidence)
    replay["positions"] = [
        *replay["positions"],
        {
            "instrument_id": 2,
            "symbol": "BTC-USD",
            "asset_class": "crypto",
            "sector": "Crypto",
            "quantity": 1.0,
            "avg_cost": 500.0,
            "price": 500.0,
            "market_value": 500.0,
            "source_id": "test",
            "currency": "USD",
            "source_kind": "daily_bars",
            "trading_date": "2026-08-22",
            "observed_at": AS_OF,
            "available_at": AS_OF,
            "valuation_status": "market_quotes",
        },
    ]
    replay["eligible_position_count"] = 2
    replay["valued_position_count"] = 2
    return replay


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
    replay = _valued_replay(stock_evidence=_complete_stock_evidence())
    decision = _decision(portfolio_replay=replay)
    impact = decision.portfolio_impacts[ExpressionKind.STOCK]

    assert impact.impact_method == "stock_portfolio_impact.v1:first_order"
    assert impact.position_weight_after is not None
    assert impact.position_weight_after > impact.position_weight_before
    assert impact.gross_exposure_after > impact.gross_exposure_before
    assert impact.sector_concentration_delta == pytest.approx(impact.symbol_concentration_delta)
    assert impact.beta_delta == pytest.approx(1.1 * impact.symbol_concentration_delta)
    assert impact.adv_participation == pytest.approx(impact.symbol_concentration_delta * 100_000 / 1_000_000)
    assert impact.days_to_exit == pytest.approx(0.11)
    assert impact.scenario_pnl == replay["stock_evidence"]["stress_scenarios"]
    assert impact.risk_budget_consumed == pytest.approx(1_000.0)
    assert impact.liquidity["adv_participation_limit"] == pytest.approx(0.10)
    assert impact.cash_comparator == {"status": "available", "expected_return": 0.0}
    assert impact.top_alternative == "QQQ"
    assert impact.funding_source_or_position_to_trim == "cash"
    assert impact.availability == "available"


@pytest.mark.parametrize("location", ["impact", "portfolio_before", "portfolio_after"])
def test_stock_impact_rejects_conflicting_target_identity_aliases(location: str) -> None:
    impact = _decision(
        portfolio_replay=_valued_replay(stock_evidence=_complete_stock_evidence()),
    ).portfolio_impacts[ExpressionKind.STOCK]
    payload = impact.model_dump(mode="python")
    if location == "impact":
        payload["symbol"] = "OTHER"
    else:
        source = dict(payload[location])
        source["symbol"] = "OTHER"
        payload[location] = source

    with pytest.raises(ValueError, match="conflicting"):
        type(impact).model_validate(payload)


def test_stock_impact_rejects_cross_container_target_identity_conflicts() -> None:
    impact = _decision(
        portfolio_replay=_valued_replay(stock_evidence=_complete_stock_evidence()),
    ).portfolio_impacts[ExpressionKind.STOCK]
    payload = impact.model_dump(mode="python")
    after = dict(payload["portfolio_after"])
    after.pop("ticker", None)
    after["symbol"] = "OTHER"
    payload["portfolio_after"] = after

    with pytest.raises(ValueError, match="conflicting"):
        type(impact).model_validate(payload)


def test_generic_bear_base_bull_scenarios_do_not_unlock_stock_impact() -> None:
    evidence = _complete_stock_evidence()
    evidence["stress_scenarios"] = {
        "bear": {"pnl": -1_000.0},
        "base": {"pnl": 100.0},
        "bull": {"pnl": 1_500.0},
    }
    impact = _decision(portfolio_replay=_valued_replay(stock_evidence=evidence)).portfolio_impacts[ExpressionKind.STOCK]

    assert impact.availability == "unavailable"
    assert "stock_stress_scenarios_missing" in impact.blockers
    assert impact.scenario_pnl is None


@pytest.mark.parametrize(
    ("scenario", "replacement"),
    [
        (
            "SPY",
            {"pnl_by_shock": {"-5": -500.0, "-9": -1_000.0}, "shock_pct": [-5.0, -10.0]},
        ),
        (
            "QQQ",
            {"pnl_by_shock": {"-5": -450.0, "-9": -900.0}, "shock_pct": [-5.0, -10.0]},
        ),
        ("sector", {"pnl": -800.0, "shock_pct": -9.0}),
        (
            "symbol",
            {"pnl_by_shock": {"-20": -1_200.0, "-29": -1_800.0}, "shock_pct": [-20.0, -30.0]},
        ),
        ("earnings-gap", {"pnl": -1_500.0, "largest_holding": "OTHER", "earnings_gap": True}),
        (
            "liquidity",
            {"pnl": -500.0, "spread_multiplier": 2.0, "slippage_multiplier": 1.0, "adv_haircut_pct": 50.0},
        ),
    ],
)
def test_stock_impact_requires_the_exact_stress_scenario_matrix(
    scenario: str,
    replacement: dict[str, object],
) -> None:
    evidence = _complete_stock_evidence()
    scenarios = dict(evidence["stress_scenarios"])
    scenarios[scenario] = replacement
    evidence["stress_scenarios"] = scenarios

    impact = _decision(
        portfolio_replay=_valued_replay(stock_evidence=evidence),
    ).portfolio_impacts[ExpressionKind.STOCK]

    assert impact.availability == "unavailable"
    assert "stock_stress_scenarios_missing" in impact.blockers
    assert impact.scenario_pnl is None


@pytest.mark.parametrize("scenario", ["SPY", "QQQ", "symbol"])
def test_multi_magnitude_scenarios_reject_one_aggregate_pnl(scenario: str) -> None:
    evidence = _complete_stock_evidence()
    scenarios = dict(evidence["stress_scenarios"])
    scenarios[scenario] = {
        "pnl": -1_000.0,
        "shock_pct": [-5.0, -10.0] if scenario != "symbol" else [-20.0, -30.0],
    }
    evidence["stress_scenarios"] = scenarios

    impact = _decision(
        portfolio_replay=_valued_replay(stock_evidence=evidence),
    ).portfolio_impacts[ExpressionKind.STOCK]

    assert impact.availability == "unavailable"
    assert "stock_stress_scenarios_missing" in impact.blockers
    assert impact.scenario_pnl is None


@pytest.mark.parametrize(
    "untrusted_alternative",
    ["ZZZZ", {"ticker": "ZZZZ", "verified": True}],
)
def test_stock_impact_requires_a_catalog_backed_top_alternative(untrusted_alternative: object) -> None:
    evidence = _complete_stock_evidence()
    evidence["top_alternative"] = untrusted_alternative
    evidence["verified_tickers"] = ["ZZZZ"]
    evidence["instrument_catalog"] = [{"id": 999, "symbol": "ZZZZ"}]
    impact = _decision(portfolio_replay=_valued_replay(stock_evidence=evidence)).portfolio_impacts[ExpressionKind.STOCK]

    assert impact.availability == "unavailable"
    assert "stock_top_alternative_missing" in impact.blockers
    assert impact.top_alternative is None


def test_stock_impact_accepts_catalog_backed_top_alternative() -> None:
    evidence = _complete_stock_evidence()
    evidence["top_alternative"] = "ALFA"
    replay = _valued_replay(stock_evidence=evidence)
    replay["positions"] = [
        *replay["positions"],
        {
            "instrument_id": 99,
            "symbol": "ALFA",
            "sector": "Technology",
            "quantity": 1.0,
            "avg_cost": 10.0,
            "price": 10.0,
            "market_value": 10.0,
            "source_id": "test",
            "currency": "USD",
            "source_kind": "daily_bars",
            "trading_date": "2026-08-22",
            "observed_at": AS_OF,
            "available_at": AS_OF,
            "valuation_status": "market_quotes",
        },
    ]
    replay["eligible_position_count"] = 2
    replay["valued_position_count"] = 2

    impact = _decision(portfolio_replay=replay).portfolio_impacts[ExpressionKind.STOCK]

    assert impact.availability == "available"
    assert impact.top_alternative == "ALFA"


def test_stock_impact_requires_btc_scenario_when_btc_is_applicable() -> None:
    evidence = _complete_stock_evidence(include_btc=True)
    evidence["btc_scenarios_applicable"] = False
    scenarios = dict(evidence["stress_scenarios"])
    scenarios.pop("BTC")
    evidence["stress_scenarios"] = scenarios
    replay = _crypto_replay(stock_evidence=evidence)
    impact = _decision(portfolio_replay=replay).portfolio_impacts[ExpressionKind.STOCK]

    assert impact.availability == "unavailable"
    assert "stock_stress_scenarios_missing" in impact.blockers

    evidence = _complete_stock_evidence(include_btc=True)
    evidence["btc_scenarios_applicable"] = False
    scenarios = dict(evidence["stress_scenarios"])
    scenarios["BTC"] = {"pnl": -700.0, "shock_pct": -14.0}
    evidence["stress_scenarios"] = scenarios
    wrong_magnitude = _decision(
        portfolio_replay=_crypto_replay(stock_evidence=evidence),
    ).portfolio_impacts[ExpressionKind.STOCK]
    assert wrong_magnitude.availability == "unavailable"
    assert "stock_stress_scenarios_missing" in wrong_magnitude.blockers

    complete = _decision(
        portfolio_replay=_crypto_replay(stock_evidence=_complete_stock_evidence(include_btc=True))
    ).portfolio_impacts[ExpressionKind.STOCK]
    assert complete.availability == "available"
    assert "BTC" in complete.scenario_pnl


def test_stock_impact_derives_btc_requirement_from_crypto_sensitive_equity() -> None:
    evidence = _complete_stock_evidence()
    replay = _valued_replay(stock_evidence=evidence)
    replay["positions"][0] = {
        **replay["positions"][0],
        "asset_class": "equity",
        "category": "crypto-infrastructure",
    }

    impact = _decision(portfolio_replay=replay).portfolio_impacts[ExpressionKind.STOCK]

    assert impact.availability == "unavailable"
    assert "stock_stress_scenarios_missing" in impact.blockers


def test_stock_impact_rechecks_btc_without_optional_nested_ticker() -> None:
    evidence = _complete_stock_evidence(include_btc=True)
    evidence["btc_scenarios_applicable"] = False
    replay = _valued_replay(stock_evidence=evidence)
    impact = _decision(
        ticker="COIN",
        portfolio_replay=replay,
    ).portfolio_impacts[ExpressionKind.STOCK]
    assert impact.availability == "available"

    payload = impact.model_dump(mode="python")
    before = dict(payload["portfolio_before"])
    before.pop("stock_impact")
    without_nested_identity = dict(payload)
    without_nested_identity["portfolio_before"] = before
    rebuilt = type(impact).model_validate(without_nested_identity)
    assert rebuilt.availability == "available"

    mismatched_target = dict(without_nested_identity)
    mismatched_target["ticker"] = "ACME"
    with pytest.raises(ValueError, match="matching target ticker"):
        type(impact).model_validate(mismatched_target)


    without_target_identity = dict(without_nested_identity)
    identity_free_before = dict(before)
    identity_free_before.pop("ticker", None)
    identity_free_after = dict(payload["portfolio_after"])
    identity_free_after.pop("ticker", None)
    identity_free_after.pop("stock_impact", None)
    without_target_identity["portfolio_before"] = identity_free_before
    without_target_identity["portfolio_after"] = identity_free_after
    without_target_identity.pop("ticker", None)
    with pytest.raises(ValueError, match="ticker"):
        type(impact).model_validate(without_target_identity)

    bad_before = dict(before)
    bad_evidence = dict(bad_before["stock_evidence"])
    bad_scenarios = dict(bad_evidence["stress_scenarios"])
    bad_scenarios.pop("BTC")
    bad_evidence["stress_scenarios"] = bad_scenarios
    bad_before["stock_evidence"] = bad_evidence
    bad_payload = dict(without_nested_identity)
    bad_payload["portfolio_before"] = bad_before
    bad_payload["scenario_pnl"] = bad_scenarios | {
        key: value for key, value in payload["scenario_pnl"].items() if key != "BTC"
    }

    with pytest.raises(ValueError, match="complete stress scenarios"):
        type(impact).model_validate(bad_payload)


@pytest.mark.parametrize(
    ("container", "alias"),
    [
        ("evidence", "symbol"),
        ("evidence", "instrument_symbol"),
        ("instrument", "instrument_symbol"),
        ("target_instrument", "symbol"),
    ],
)
def test_stock_impact_derives_btc_from_every_evidence_identity_alias(
    container: str,
    alias: str,
) -> None:
    evidence = _complete_stock_evidence()
    if container == "evidence":
        evidence.update({"ticker": "ACME", alias: "BTC-USD"})
    else:
        evidence[container] = {"ticker": "ACME", alias: "BTC-USD"}
    impact = _decision(
        portfolio_replay=_valued_replay(stock_evidence=evidence),
    ).portfolio_impacts[ExpressionKind.STOCK]

    assert impact.availability == "unavailable"
    assert "stock_stress_scenarios_missing" in impact.blockers


def test_stock_impact_requires_btc_for_ticker_only_crypto_position_identity() -> None:
    replay = _valued_replay(stock_evidence=_complete_stock_evidence())
    replay["positions"] = [
        *replay["positions"],
        {
            "instrument_id": 2,
            "ticker": "BTC-USD",
            "sector": "Technology",
            "quantity": 1.0,
            "avg_cost": 500.0,
            "price": 500.0,
            "market_value": 500.0,
            "source_id": "test",
            "currency": "USD",
            "source_kind": "daily_bars",
            "trading_date": "2026-08-22",
            "observed_at": AS_OF,
            "available_at": AS_OF,
            "valuation_status": "market_quotes",
        },
    ]
    replay["eligible_position_count"] = 2
    replay["valued_position_count"] = 2

    impact = _decision(portfolio_replay=replay).portfolio_impacts[ExpressionKind.STOCK]

    assert impact.availability == "unavailable"
    assert "stock_stress_scenarios_missing" in impact.blockers


@pytest.mark.parametrize("btc_alias", ["symbol", "instrument_symbol"])
def test_stock_impact_requires_btc_when_any_position_identity_alias_is_btc(btc_alias: str) -> None:
    replay = _valued_replay(stock_evidence=_complete_stock_evidence())
    position = {
        "instrument_id": 2,
        "ticker": "NOT-BTC",
        btc_alias: "BTC-USD",
        "sector": "Technology",
        "quantity": 1.0,
        "avg_cost": 500.0,
        "price": 500.0,
        "market_value": 500.0,
        "source_id": "test",
        "currency": "USD",
        "source_kind": "daily_bars",
        "trading_date": "2026-08-22",
        "observed_at": AS_OF,
        "available_at": AS_OF,
        "valuation_status": "market_quotes",
    }
    replay["positions"] = [*replay["positions"], position]
    replay["eligible_position_count"] = 2
    replay["valued_position_count"] = 2

    impact = _decision(portfolio_replay=replay).portfolio_impacts[ExpressionKind.STOCK]

    assert impact.availability == "unavailable"
    assert "stock_stress_scenarios_missing" in impact.blockers


def test_stock_impact_missing_institutional_evidence_stays_unavailable() -> None:
    replay = _valued_replay(stock_evidence={
        "sector": "Technology",
        "beta": 1.1,
        "avg_dollar_volume": 1_000_000.0,
        "correlation_cluster_delta": 0.01,
    })
    impact = _decision(portfolio_replay=replay).portfolio_impacts[ExpressionKind.STOCK]

    assert impact.availability == "unavailable"
    assert {
        "stock_stress_scenarios_missing",
        "stock_risk_budget_evidence_missing",
        "stock_adv_participation_limit_missing",
        "stock_cash_comparator_missing",
        "stock_top_alternative_missing",
        "stock_funding_evidence_missing",
    } <= set(impact.blockers)
    assert impact.scenario_pnl is None
    assert impact.risk_budget_consumed is None
    assert impact.liquidity is None
    assert impact.cash_comparator is None
    assert impact.funding_source_or_position_to_trim is None


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
