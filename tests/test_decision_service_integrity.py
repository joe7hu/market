"""Regression contracts for signal completeness, venue clocks and truthful health."""
from datetime import UTC, datetime, timedelta

import pytest

from investment_panel.domain.decision.assessment import assessment_quote
from investment_panel.domain.decision.calendar import after_market_minutes, forecast_observation_end
from investment_panel.domain.decision.reference_signal import build_reference_signal, project_reference_signal
from investment_panel.domain.decision.service_health import decision_service_health
from investment_panel.infrastructure.postgres.monitored_universe import merge_monitored_universe
from investment_panel.infrastructure.providers.assessment_quotes import validated_quote

SUNDAY = datetime(2026, 9, 20, 21, 30, tzinfo=UTC)
FRIDAY_CLOSE = datetime(2026, 9, 18, 20, tzinfo=UTC)


def quote(*, observed=FRIDAY_CLOSE, available=SUNDAY, price=100):
    return {"price": price, "observed_at": observed, "available_at": available}


def feature(**changes):
    return {"price": 100, "atr_pct": .02, "as_of": SUNDAY - timedelta(minutes=2),
            "available_at": SUNDAY - timedelta(minutes=1), "revision": "feature-1",
            "metrics": {"as_of_date": "2026-09-18"}, "trend_state": "trend_up",
            "data_quality_status": "complete", **changes}


def test_closed_equity_reference_keeps_its_friday_observation_clock():
    value = assessment_quote(quote(), now=SUNDAY)
    assert value.usable and value.state == "closed_reference"
    assert value.observed_at == FRIDAY_CLOSE
    assert not assessment_quote(quote(observed=FRIDAY_CLOSE - timedelta(hours=4)), now=SUNDAY).usable


def test_weekend_never_exempts_crypto_from_the_15_minute_clock():
    assert not assessment_quote(quote(), now=SUNDAY, continuous=True).usable
    value = assessment_quote(quote(observed=SUNDAY-timedelta(minutes=5)), now=SUNDAY, continuous=True)
    assert value.usable and value.state == "live"


@pytest.mark.parametrize("changes", [
    {"observed_at": None}, {"available_at": None}, {"price": True}, {"price": float("nan")},
    {"price": float("inf")}, {"price": 0}, {"observed_at": SUNDAY+timedelta(seconds=1)},
    {"available_at": FRIDAY_CLOSE-timedelta(seconds=1)}, {"available_at": SUNDAY+timedelta(seconds=1)},
])
def test_refresh_does_not_repair_invalid_quote_evidence(changes):
    assert not assessment_quote({**quote(), **changes}, now=SUNDAY).usable


def test_monday_open_requires_a_new_quote_even_with_current_ingestion():
    monday = datetime(2026, 9, 21, 13, 30, tzinfo=UTC)
    assert not assessment_quote(quote(available=monday), now=monday).usable


def test_holiday_and_early_close_references_use_real_completed_session():
    holiday = datetime(2026, 7, 3, 18, tzinfo=UTC)
    assert assessment_quote(quote(observed=datetime(2026, 7, 2, 20, tzinfo=UTC), available=holiday), now=holiday).usable
    saturday = datetime(2026, 11, 28, 18, tzinfo=UTC)
    assert assessment_quote(quote(observed=datetime(2026, 11, 27, 18, tzinfo=UTC), available=saturday), now=saturday).usable


def test_complete_conditional_signal_uses_fractional_atr_not_percentage_points():
    signal = build_reference_signal("TEST", quote=quote(), feature=feature(), now=SUNDAY)
    assert signal.action == "BUY_SETUP"
    assert (signal.entry_low, signal.entry_high, signal.stop_price, signal.target_price) == (99.5, 100.5, 96, 108)
    assert signal.risk_per_unit == 4.5
    assert signal.source_revision == "feature-1"
    assert signal.quote_observed_at == FRIDAY_CLOSE
    assert signal.expires_at == datetime(2026, 9, 21, 13, 30, tzinfo=UTC)
    assert signal.order_authorized is False
    assert "probability" not in signal.model_dump()


@pytest.mark.parametrize(("state", "owned", "action"), [
    ("trend_up", True, "HOLD"), ("trend_down", False, "AVOID"), ("trend_down", True, "EXIT_SETUP"),
    ("range", False, "WAIT"), ("transition", True, "HOLD"),
])
def test_legitimate_no_setup_and_holding_decisions_are_complete(state, owned, action):
    signal = build_reference_signal("TEST", quote=quote(), feature=feature(trend_state=state), now=SUNDAY, owned=owned)
    assert signal.action == action and signal.failure_code is None and not signal.order_authorized


def test_crossed_invalidation_never_recommends_a_new_long():
    for owned, action in ((False, "AVOID"), (True, "EXIT_SETUP")):
        signal = build_reference_signal("TEST", quote=quote(price=95), feature=feature(), now=SUNDAY, owned=owned)
        assert signal.action == action and signal.entry_low is None


@pytest.mark.parametrize(("changes", "code"), [
    ({"data_quality_status": "unavailable"}, "trend_feature_incomplete"),
    ({"metrics": {"as_of_date": "2026-09-17"}}, "trend_feature_not_current"),
    ({"available_at": SUNDAY+timedelta(seconds=1)}, "trend_feature_not_current"),
    ({"as_of": SUNDAY+timedelta(seconds=1)}, "trend_feature_not_current"),
    ({"atr_pct": 2}, "trend_risk_inputs_invalid"), ({"atr_pct": 0}, "trend_risk_inputs_invalid"),
    ({"trend_state": "invented"}, "trend_state_invalid"),
])
def test_bad_inputs_are_named_service_failures_not_wait(changes, code):
    signal = build_reference_signal("TEST", quote=quote(), feature=feature(**changes), now=SUNDAY)
    assert signal.action == "SERVICE_FAILURE" and signal.failure_code == code
    assert signal.owner_job == "refresh_symbol_features"
    assert signal.entry_low is None and signal.target_price is None


def test_crypto_uses_utc_daily_feature_and_short_expiry_on_sunday():
    signal = build_reference_signal("BTC-USD", quote=quote(observed=SUNDAY),
        feature=feature(metrics={"as_of_date": "2026-09-19"}), now=SUNDAY, continuous=True)
    assert signal.action == "BUY_SETUP" and signal.expires_at == SUNDAY+timedelta(minutes=15)


def test_read_projection_revokes_expiry_without_mutating_evidence():
    signal = build_reference_signal("TEST", quote=quote(), feature=feature(), now=SUNDAY)
    projected = project_reference_signal(signal, now=signal.expires_at)
    assert projected.action == "SERVICE_FAILURE" and projected.owner_job == "refresh_decision_models"
    assert projected.entry_low is None and projected.stop_price is None
    assert signal.action == "BUY_SETUP" and signal.entry_low == 99.5
    assert projected.as_of == signal.as_of and projected.quote_observed_at == FRIDAY_CLOSE
    assert project_reference_signal(signal, now=SUNDAY-timedelta(seconds=1)).action == "SERVICE_FAILURE"


def health_row():
    signal = build_reference_signal("TEST", quote=quote(), feature=feature(trend_state="range"), now=SUNDAY)
    return {"symbol": "TEST", "asset_class": "equity", "quote": quote(), "feature": feature(trend_state="range"),
            "decision": {"reference_signal": signal.model_dump(), "opportunity_rank": {"rank_id": "r"},
                         "trade_plan": {"eligibility": "BLOCKED", "selected_expression": "CASH"}}}


def test_health_measures_delivery_not_profitability_or_trade_count():
    result = decision_service_health([health_row()], now=SUNDAY)
    assert result["status"] == "available" and result["ready_count"] == 1
    assert result["instruments"][0]["signal_action"] == "WAIT"
    assert decision_service_health([], now=SUNDAY)["status"] == "not_configured"


def test_green_database_does_not_mask_broken_signal_producers():
    row = health_row()
    row["quote"] = quote(observed=FRIDAY_CLOSE-timedelta(days=1))
    row["decision"] = {}
    result = decision_service_health([row], now=SUNDAY)
    assert result["status"] == "failed" and result["ready_count"] == 0
    assert result["instruments"][0]["owner_jobs"] == ["refresh_assessment_inputs", "refresh_decision_models"]
    assert {item["code"] for item in result["incidents"]} >= {"assessment_quote_invalid", "signal_not_published", "decision_contract_incomplete"}


def test_monitored_population_honors_exclusions_but_always_retains_owned_risk():
    result = merge_monitored_universe([
        {"symbol": "DROP", "watch_state": "excluded", "is_owned": False},
        {"symbol": "OWN", "watch_state": "excluded", "is_owned": True},
        {"symbol": "DB", "watch_state": "watched", "asset_class": "etf"},
    ], [{"symbol": "DROP"}, {"symbol": "BTC-USD"}, {"symbol": "DB"}])
    assert result == [{"symbol": "BTC-USD", "asset_class": "crypto"}, {"symbol": "DB", "asset_class": "etf"}, {"symbol": "OWN", "asset_class": "equity"}]


def test_reference_provider_never_invents_execution_liquidity():
    row = validated_quote("TEST", "equity", 100, FRIDAY_CLOSE, "fixture", now=SUNDAY)
    assert row["observed_at"] == FRIDAY_CLOSE and row["execution_eligible"] is False
    assert all(key not in row for key in ("bid", "ask", "bid_size", "ask_size"))
    with pytest.raises(ValueError):
        validated_quote("BTC-USD", "crypto", 100, FRIDAY_CLOSE, "fixture", now=SUNDAY)


def test_prospective_window_begins_at_admission_and_counts_trading_minutes():
    # Sunday-created observation gets thirty actual market minutes on Monday.
    assert after_market_minutes(SUNDAY, 30) == datetime(2026, 9, 21, 14, tzinfo=UTC)
    friday = datetime(2026, 9, 18, 19, 50, tzinfo=UTC)
    assert after_market_minutes(friday, 30) == datetime(2026, 9, 21, 13, 50, tzinfo=UTC)


def test_crypto_resolution_window_does_not_wait_for_monday():
    target = SUNDAY + timedelta(hours=1)
    assert forecast_observation_end(target, continuous=True) == target+timedelta(minutes=15)
    assert forecast_observation_end(target, continuous=False) == datetime(2026, 9, 21, 20, tzinfo=UTC)

@pytest.mark.parametrize("stamp", ["2026-09-18T20:00:00", datetime(2026, 9, 18, 20)])
def test_assessment_does_not_guess_timezone(stamp):
    assert not assessment_quote({"price": 100, "observed_at": stamp, "available_at": SUNDAY}, now=SUNDAY).usable


def test_sunday_advisor_packet_keeps_friday_price_but_new_weekend_news():
    from investment_panel.core.continuous_advisor import build_evidence_packet
    quote_at = datetime(2026, 9, 18, 20, tzinfo=UTC)
    context = {"context_status": {"cutoff_available": True, "cutoff": SUNDAY},
        "portfolio": {"price": 100, "quote_observed_at": quote_at, "quote_available_at": quote_at},
        "source_evidence": [{"reference": "news:sunday", "source_type": "news", "observed_at": SUNDAY, "available_at": SUNDAY, "title": "Weekend event"}]}
    packet = build_evidence_packet({"symbol": "XYZ"}, context, cutoff=SUNDAY, prompt_version="test")
    assert packet["evidence"]["prices"]["price"] == 100
    assert "price_stale" not in packet["blockers"]
    assert packet["source_references"] == ["news:sunday"]
    crypto = build_evidence_packet({"symbol": "BTC-USD", "asset_class": "crypto"}, context, cutoff=SUNDAY, prompt_version="test")
    assert crypto["evidence"]["prices"] == {}
    assert "price_stale" in crypto["blockers"]


@pytest.mark.parametrize("code", ["fresh_postgres_account_facts_required", "opportunity_rank_identity_mismatch", "market_state_publication_missing"])
def test_working_signal_does_not_hide_capital_decision_producer_failures(code):
    row = health_row()
    row["decision"]["trade_plan"]["blockers"] = [code]
    result = decision_service_health([row], now=SUNDAY)
    assert result["status"] == "failed"
    assert result["instruments"][0]["signal_action"] == "WAIT"
    assert result["instruments"][0]["conditions_status"] == "available"
    assert result["instruments"][0]["capital_status"] == "failed"
    assert result["incidents"][0]["capability"] == "Capital decisions"


def test_unvalidated_strategy_and_risk_limit_are_not_outages_but_absent_account_is():
    row = health_row()
    row["decision"]["trade_plan"]["blockers"] = ["strategy_not_validated", "stock_risk_budget_exceeded"]
    assert decision_service_health([row], now=SUNDAY)["status"] == "available"
    row["decision"]["risk_policy_snapshot"] = {"blockers": ["fresh_postgres_account_facts_required"]}
    result = decision_service_health([row], now=SUNDAY)
    assert result["status"] == "failed"
    assert result["incidents"][0]["job"] == "update_broker_account"
