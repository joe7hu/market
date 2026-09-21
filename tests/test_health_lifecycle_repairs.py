"""Focused regressions for Health lifecycle classification."""

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from conftest import typed_config
from investment_panel.domain.decision import is_us_market_day
from investment_panel.domain.decision.reference_signal import build_reference_signal
from investment_panel.domain.decision.service_health import decision_service_health
from investment_panel.domain.factors.trend_features import compute_trend_feature
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime
from investment_panel.jobs import update_company_financials


SUNDAY = datetime(2026, 9, 20, 21, 30, tzinfo=UTC)
FRIDAY_CLOSE = datetime(2026, 9, 18, 20, tzinfo=UTC)


def _bars(values: list[float]) -> list[dict[str, object]]:
    day = date(2025, 1, 2)
    rows = []
    for value in values:
        while not is_us_market_day(day):
            day += timedelta(days=1)
        rows.append({"trading_date": day, "close": value})
        day += timedelta(days=1)
    return rows


def test_unused_broker_account_blocker_is_not_a_health_outage() -> None:
    signal = build_reference_signal(
        "TEST",
        quote={"price": 100, "observed_at": FRIDAY_CLOSE, "available_at": SUNDAY},
        feature={
            "price": 100,
            "atr_pct": 0.02,
            "as_of": SUNDAY - timedelta(minutes=2),
            "available_at": SUNDAY - timedelta(minutes=1),
            "revision": "feature-1",
            "metrics": {"as_of_date": "2026-09-18"},
            "trend_state": "range",
            "data_quality_status": "complete",
        },
        now=SUNDAY,
    )
    result = decision_service_health([{
        "symbol": "TEST",
        "asset_class": "equity",
        "quote": {"price": 100, "observed_at": FRIDAY_CLOSE, "available_at": SUNDAY},
        "feature": {
            "price": 100,
            "atr_pct": 0.02,
            "as_of": SUNDAY - timedelta(minutes=2),
            "available_at": SUNDAY - timedelta(minutes=1),
            "revision": "feature-1",
            "metrics": {"as_of_date": "2026-09-18"},
            "trend_state": "range",
            "data_quality_status": "complete",
        },
        "decision": {
            "reference_signal": signal.model_dump(),
            "opportunity_rank": {"rank_id": "rank"},
            "trade_plan": {"eligibility": "BLOCKED", "selected_expression": "CASH"},
            "risk_policy_snapshot": {"blockers": ["fresh_postgres_account_facts_required"]},
        },
    }], now=SUNDAY, account_required=False)

    assert result["status"] == "available"
    assert result["instruments"][0]["capital_status"] == "available"


def test_maturing_history_is_wait_not_a_producer_failure() -> None:
    signal = build_reference_signal(
        "NEW",
        quote={"price": 100, "observed_at": FRIDAY_CLOSE, "available_at": SUNDAY},
        feature={
            "as_of_date": FRIDAY_CLOSE.date(),
            "revision": "feature-1",
            "data_quality_status": "unavailable",
            "reason_codes": ["insufficient_price_history"],
        },
        now=SUNDAY,
    )

    assert signal.action == "WAIT"
    assert signal.failure_code is None


def test_large_equity_gain_is_not_assumed_to_be_a_corporate_action() -> None:
    values = [100 + index / 10 for index in range(220)]
    for index in range(210, len(values)):
        values[index] *= 1.5

    feature = compute_trend_feature(_bars(values), _bars([100.0] * len(values)))

    assert feature.data_quality_status == "complete"


def test_sec_coverage_gap_is_not_a_source_outage(migrated_postgres_dsn: str, monkeypatch) -> None:
    symbol = f"GAP{uuid4().hex[:8].upper()}"
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        with runtime.transaction() as connection:
            connection.execute(
                "INSERT INTO catalog.instrument (symbol, name, asset_class) VALUES (%s, %s, 'equity')",
                [symbol, symbol],
            )
        monkeypatch.setattr(update_company_financials, "load_config", lambda _path=None: typed_config(migrated_postgres_dsn, raw={}))
        monkeypatch.setattr(update_company_financials.sec, "company_tickers", lambda _user_agent: {
            "0": {"ticker": symbol, "cik_str": 1},
        })
        monkeypatch.setattr(update_company_financials.sec, "company_submissions", lambda *_args: {"filings": {"recent": {}}})
        monkeypatch.setattr(update_company_financials.sec, "company_facts", lambda *_args: {"facts": {}})

        result = update_company_financials.run(symbols=[symbol], config_path=None)

        assert result["status"] == result["source_status"] == "ok"
    finally:
        runtime.close()
