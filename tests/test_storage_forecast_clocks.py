"""Forecast scenarios specified before changing storage accounting."""

from datetime import UTC, datetime, timedelta

from investment_panel.domain.storage_forecast import forecast_growth

GIB = 1024**3


def samples(times, free, database=None):
    return [
        {
            "sample_day": at.date(),
            "sampled_at": at,
            "volume_free_bytes": available,
            "database_bytes": (database or [100 * GIB] * len(times))[index],
            "archived_bytes": 0,
            "logical_evidence_bytes": 10 * GIB,
        }
        for index, (at, available) in enumerate(zip(times, free))
    ]


def test_two_days_and_midnight_slivers_remain_provisional():
    now = datetime(2026, 9, 28, 0, 1, tzinfo=UTC)
    result = forecast_growth(
        samples([now - timedelta(minutes=2), now], [31 * GIB, 30 * GIB])
    )
    assert result["forecast_confidence"] == "provisional"
    assert result["forecast_growth_bytes_per_day"] == int(0.7 * GIB)
    assert result["measured_growth_bytes_per_day"] is None


def test_elapsed_time_not_calendar_date_controls_measured_rate():
    now = datetime(2026, 9, 28, 23, tzinfo=UTC)
    data = samples(
        [now - timedelta(hours=71), now - timedelta(hours=25), now],
        [33 * GIB, 31 * GIB, 30 * GIB],
    )
    result = forecast_growth(data)
    assert result["forecast_confidence"] == "measured"
    assert result["sample_span_seconds"] == 71 * 3600
    assert result["forecast_growth_bytes_per_day"] >= int(3 * GIB / (71 / 24))


def test_recent_growth_and_database_growth_cannot_hide_behind_long_flat_endpoint():
    now = datetime(2026, 9, 28, tzinfo=UTC)
    data = samples(
        [now - timedelta(days=7), now - timedelta(days=1), now],
        [30 * GIB, 35 * GIB, 30 * GIB],
        [100 * GIB, 100 * GIB, 106 * GIB],
    )
    result = forecast_growth(data)
    assert result["forecast_growth_bytes_per_day"] >= 6 * GIB
    assert result["forecast_confidence"] == "measured"


def test_no_negative_growth_credit_or_fake_daily_sample_multiplication():
    now = datetime(2026, 9, 28, tzinfo=UTC)
    data = samples(
        [now - timedelta(days=2), now - timedelta(days=1), now],
        [30 * GIB, 31 * GIB, 33 * GIB],
    )
    assert forecast_growth(data)["forecast_growth_bytes_per_day"] == 0
    assert forecast_growth([data[0]] * 3)["forecast_confidence"] == "provisional"
