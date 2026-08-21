"""Forward-only regressions distilled from the August opportunity audit.

The original audit remains an external, immutable report.  These small fixtures
only encode the classification guarantees that future capture must preserve.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from investment_panel.core.options_recovery import ExecutableLeg, QuoteCapture, evaluate_lifecycle
from investment_panel.database.options_recovery_learning import classification as classify_observation


NOW = datetime(2026, 8, 3, 14, tzinfo=UTC)


def _capture(minutes: int, *, bid: float, ask: float, session: int, **extra: object) -> QuoteCapture:
    return QuoteCapture(
        observed_at=NOW + timedelta(minutes=minutes),
        legs=(ExecutableLeg("fixture", "buy", bid, ask, 10, 10),),
        session_number=session,
        dte=20,
        **extra,
    )


def test_nvda_205c_fixture_preserves_available_3x_and_4x_path() -> None:
    result = evaluate_lifecycle(
        published_at=NOW,
        quantity=1,
        captures=[
            _capture(15, bid=0.99, ask=1.00, session=1),
            _capture(30, bid=3.12, ask=3.18, session=3),
            _capture(45, bid=4.18, ask=4.24, session=4),
        ],
    )

    assert result.classification == "captured"
    assert result.time_to_3x_sessions == 2
    assert result.time_to_4x_sessions == 3


def test_tsla_290p_fixture_is_a_full_denominator_miss_when_not_ticketed() -> None:
    lifecycle = evaluate_lifecycle(
        published_at=NOW,
        quantity=1,
        captures=[
            _capture(15, bid=0.99, ask=1.00, session=1),
            _capture(30, bid=5.40, ask=5.50, session=3),
        ],
    )
    classification, data_status, reason = classify_observation(
        {
            "paper_status": None,
            "miss_reason": "ranked_out",
            "started_at": NOW - timedelta(days=7),
            "expiration": date(2026, 8, 21),
        },
        lifecycle,
        NOW + timedelta(minutes=31),
    )

    assert lifecycle.time_to_4x_sessions == 2
    assert (classification, data_status, reason) == ("missed", "ok", "ranked_out")


def test_july_googl_fixture_is_unmeasurable_when_exact_contract_continuity_is_missing() -> None:
    lifecycle = evaluate_lifecycle(
        published_at=NOW,
        quantity=1,
        captures=[
            _capture(15, bid=0.99, ask=1.00, session=1),
            _capture(
                30,
                bid=0.0,
                ask=0.0,
                session=2,
                continuity_ok=False,
                reason="same_contract_continuity_missing",
            ),
        ],
    )

    assert lifecycle.classification == "unmeasurable"
    assert lifecycle.unmeasurable_reason == "same_contract_continuity_missing"


def test_closed_event_horizon_never_leaves_an_observation_observing_forever() -> None:
    lifecycle = evaluate_lifecycle(
        published_at=NOW,
        quantity=1,
        captures=[_capture(15, bid=0.99, ask=1.00, session=1)],
    )

    classification, data_status, reason = classify_observation(
        {
            "paper_status": None,
            "miss_reason": None,
            "started_at": NOW - timedelta(days=10),
            "expiration": date(2026, 9, 18),
            "event_status": "closed",
        },
        lifecycle,
        NOW + timedelta(minutes=16),
    )

    assert (classification, data_status, reason) == ("unmeasurable", "continuity_missing", "unmeasurable")
