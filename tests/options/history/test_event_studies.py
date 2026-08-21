from __future__ import annotations

import pytest

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import investment_panel.database.event_studies as event_studies
from investment_panel.database.event_studies import (
    bootstrap_median_interval, bounded_event_cohort, bounded_event_targets,
    deduplicate_logical_events, event_session, event_move, latest_completed_market_date,
    minimum_event_expiration, canonical_event_bars, paired_atm_straddle_cost,
    percentile, summarize_actual_moves,
)
from investment_panel.database.confirmed_daily_prices import confirmed_daily_bars


def test_less_than_twenty_samples_fails_closed() -> None:
    result = summarize_actual_moves([0.01] * 19)
    assert result == {"sample_size": 19, "evidence_state": "insufficient_event_evidence"}


def test_median_p75_p90_and_bootstrap_are_deterministic() -> None:
    values = [float(value) for value in range(1, 21)]
    result = summarize_actual_moves(values)
    assert result["actual_move_median"] == pytest.approx(10.5)
    assert result["actual_move_p75"] == pytest.approx(15.25)
    assert result["actual_move_p90"] == pytest.approx(18.1)
    assert result["bootstrap_low"] <= result["actual_move_median"] <= result["bootstrap_high"]
    assert bootstrap_median_interval(values) == bootstrap_median_interval(values)
    assert percentile(values, 0.5) == pytest.approx(10.5)


def test_atm_cost_uses_full_call_plus_put_not_half_straddle() -> None:
    rows = [
        {"expiration": "2026-09-18", "strike": 100, "option_type": "call", "bid": 2.9, "ask": 3.1, "mid": 3.0},
        {"expiration": "2026-09-18", "strike": 100, "option_type": "put", "bid": 1.9, "ask": 2.1, "mid": 2.0},
    ]
    assert paired_atm_straddle_cost(rows, underlying_price=100) == pytest.approx(5.0)


def test_atm_cost_requires_same_expiry_and_complete_legs() -> None:
    mismatch = [
        {"expiration": "2026-09-18", "strike": 100, "option_type": "call", "bid": 2.9, "ask": 3.1, "mid": 3.0},
        {"expiration": "2026-09-25", "strike": 100, "option_type": "put", "bid": 1.9, "ask": 2.1, "mid": 2.0},
    ]
    incomplete = [{"expiration": "2026-09-18", "strike": 100, "option_type": "call", "bid": 2.9, "ask": 3.1, "mid": 3.0}]
    assert paired_atm_straddle_cost(mismatch, underlying_price=100) is None
    assert paired_atm_straddle_cost(incomplete, underlying_price=100) is None


def test_atm_cost_selects_nearest_expiry_before_nearest_strike() -> None:
    rows = [
        {"expiration": "2026-09-18", "strike": 101, "option_type": "call", "bid": 2, "ask": 2.2},
        {"expiration": "2026-09-18", "strike": 101, "option_type": "put", "bid": 2.5, "ask": 2.7},
        {"expiration": "2026-10-16", "strike": 100, "option_type": "call", "bid": 5, "ask": 5.2},
        {"expiration": "2026-10-16", "strike": 100, "option_type": "put", "bid": 5.5, "ask": 5.7},
    ]
    assert paired_atm_straddle_cost(rows, underlying_price=100) == pytest.approx(4.7)


def test_event_session_uses_new_york_market_boundaries() -> None:
    assert event_session(datetime(2026, 8, 19, 12, 30, tzinfo=UTC)) == "pre_market"
    assert event_session(datetime(2026, 8, 19, 14, 0, tzinfo=UTC)) == "regular"
    assert event_session(datetime(2026, 8, 19, 20, 0, tzinfo=UTC)) == "post_market"
    assert event_session(datetime(2026, 11, 27, 18, 30, tzinfo=UTC)) == "post_market"


def test_event_regime_cutoff_includes_only_completed_new_york_session() -> None:
    assert latest_completed_market_date(datetime(2026, 8, 19, 19, 59, tzinfo=UTC)).isoformat() == "2026-08-18"
    assert latest_completed_market_date(datetime(2026, 8, 19, 20, 1, tzinfo=UTC)).isoformat() == "2026-08-19"


def test_event_regime_trend_input_is_strictly_window_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    reference = datetime(2026, 8, 19, 21, tzinfo=UTC)
    rows = [
        {
            "trading_date": (reference - timedelta(days=1000 - index)).date(),
            "close": 100 + index, "source_id": "polygon",
            "observed_at": reference - timedelta(days=1),
            "available_at": reference - timedelta(days=1),
            "confirmed_at": reference - timedelta(days=1),
        }
        for index in range(1000)
    ]
    lengths = []

    def fake_feature(bars, benchmark, **_kwargs):
        lengths.append((len(bars), len(benchmark)))
        return SimpleNamespace(trend_state="transition")

    monkeypatch.setattr(event_studies, "compute_trend_feature", fake_feature)
    event = {
        "instrument_id": 1, "benchmark_instrument_id": 2, "starts_at": reference,
    }

    assert event_studies._pre_event_regime(event, {1: rows, 2: rows}, cutoff=reference) == "transition"
    assert lengths == [(event_studies.EVENT_TREND_WINDOW, event_studies.EVENT_TREND_WINDOW)]


def test_confirmed_bar_version_overflow_fails_closed() -> None:
    class Result:
        def fetchall(self):
            return [
                {"instrument_id": 1, "trading_date": datetime(2026, 8, 18).date(),
                 "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1,
                 "source_id": "polygon", "observed_at": datetime(2026, 8, 19, tzinfo=UTC),
                 "available_at": datetime(2026, 8, 19, tzinfo=UTC),
                 "confirmed_at": datetime(2026, 8, 19, tzinfo=UTC), "fact_rank": rank}
                for rank in (1, 3)
            ]

    class Connection:
        def execute(self, _query, params):
            assert params[-2:] == [2, 2]
            return Result()

    rows = confirmed_daily_bars(
        Connection(), [1], as_of=datetime(2026, 8, 19, tzinfo=UTC),
        max_bars=10, include_versions=True, max_fact_versions=2,
    )

    assert rows == {1: []}


def test_post_market_event_requires_an_expiry_after_the_event_date() -> None:
    assert minimum_event_expiration(datetime(2026, 8, 19, 19, 0, tzinfo=UTC)).isoformat() == "2026-08-19"
    assert minimum_event_expiration(datetime(2026, 8, 19, 21, 0, tzinfo=UTC)).isoformat() == "2026-08-20"


def test_historical_event_uses_the_bar_version_visible_at_its_cutoff() -> None:
    trading_date = datetime(2026, 8, 18).date()
    early = datetime(2026, 8, 19, 1, tzinfo=UTC)
    correction = datetime(2026, 8, 20, 1, tzinfo=UTC)
    rows = [
        {"trading_date": trading_date, "close": 100, "source_id": "polygon",
         "observed_at": early, "available_at": early, "confirmed_at": early},
        {"trading_date": trading_date, "close": 101, "source_id": "polygon",
         "observed_at": early, "available_at": correction, "confirmed_at": correction},
    ]
    selected = canonical_event_bars(
        rows, reference=early + timedelta(hours=1), completed_date=trading_date,
    )
    assert [row["close"] for row in selected] == [100]


def test_event_move_uses_new_york_date_and_exact_next_session() -> None:
    bars = [
        {"trading_date": datetime(2026, 8, 18).date(), "close": 100},
        {"trading_date": datetime(2026, 8, 19).date(), "close": 101},
        {"trading_date": datetime(2026, 8, 20).date(), "close": 103},
    ]
    starts_at = datetime(2026, 8, 20, 0, 30, tzinfo=UTC)
    assert event_move(bars, starts_at, 1, session="post_market") == pytest.approx(103 / 101 - 1)
    assert event_move([bars[0], bars[2]], starts_at, 1, session="post_market") is None
    assert event_move(bars, starts_at, 1, session="regular") is None


def test_duplicate_feed_events_count_as_one_logical_occurrence() -> None:
    starts_at = datetime(2026, 8, 19, 12, 30, tzinfo=UTC)
    events = [
        {"id": 2, "instrument_id": 1, "event_kind": "cpi", "starts_at": starts_at,
         "available_at": datetime(2026, 8, 10, tzinfo=UTC)},
        {"id": 1, "instrument_id": 1, "event_kind": "cpi", "starts_at": starts_at,
         "available_at": datetime(2026, 8, 9, tzinfo=UTC)},
    ]
    assert [row["id"] for row in deduplicate_logical_events(events)] == [1]


def test_target_limit_is_applied_per_instrument_and_event_kind() -> None:
    cutoff = datetime(2026, 8, 19, tzinfo=UTC)
    events = [
        {"id": 1, "instrument_id": 1, "event_kind": "cpi", "starts_at": cutoff},
        {"id": 2, "instrument_id": 1, "event_kind": "fomc", "starts_at": cutoff},
        {"id": 3, "instrument_id": 2, "event_kind": "earnings", "starts_at": cutoff},
    ]
    selected = bounded_event_targets(events, cutoff=cutoff, per_group=1)
    assert {row["id"] for row in selected} == {1, 2, 3}


def test_target_limit_is_also_bounded_for_the_whole_run() -> None:
    cutoff = datetime(2026, 8, 19, tzinfo=UTC)
    events = [
        {"id": value, "instrument_id": value, "event_kind": "earnings",
         "starts_at": cutoff + timedelta(days=value)}
        for value in range(1, 21)
    ]
    selected = bounded_event_targets(events, cutoff=cutoff, per_group=3, total_limit=10)
    assert [row["id"] for row in selected] == list(range(1, 11))


def test_event_cohort_is_bounded_before_regime_work() -> None:
    cutoff = datetime(2026, 8, 19, 12, 30, tzinfo=UTC)
    target = {"id": 999, "instrument_id": 1, "event_kind": "cpi", "starts_at": cutoff}
    peers = [
        {"id": value, "instrument_id": 1, "event_kind": "cpi",
         "starts_at": cutoff.replace(year=2025) - timedelta(days=value)}
        for value in range(150)
    ]
    assert len(bounded_event_cohort(peers + [target], [target], cutoff=cutoff, max_peers=100)) == 101


def test_event_cohort_has_a_strict_shared_budget() -> None:
    cutoff = datetime(2026, 8, 19, 12, 30, tzinfo=UTC)
    targets = [
        {"id": 900 + instrument, "instrument_id": instrument,
         "event_kind": "earnings", "starts_at": cutoff}
        for instrument in range(1, 11)
    ]
    peers = [
        {"id": instrument * 1000 + age, "instrument_id": instrument,
         "event_kind": "earnings", "starts_at": cutoff - timedelta(days=age + 1)}
        for instrument in range(1, 11) for age in range(100)
    ]
    cohort = bounded_event_cohort(
        peers + targets, targets, cutoff=cutoff, max_peers=100, max_total=250,
    )
    assert len(cohort) == 250
    assert {row["instrument_id"] for row in cohort} == set(range(1, 11))
