from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.actions.event_scout import process_signal
from investment_panel.core.event_scout import build_event_decision_packet
from investment_panel.core.event_replays import replay_mrna
from investment_panel.database.authority import runtime_for_url
from investment_panel.database.event_scout import (
    decision_truth_rows,
    event_decision_packet_rows,
    event_scout_rows,
    persist_event_packet,
)


def test_postgres_event_packet_and_shared_truth_are_atomic(migrated_postgres_dsn: str) -> None:
    runtime = runtime_for_url(migrated_postgres_dsn)
    packet = replay_mrna()
    persist_event_packet(
        runtime,
        packet,
        {
            "symbol": "MRNA",
            "trigger_type": "formal_announcement",
            "observed_at": packet["as_of"],
            "source_url": packet["source_url"],
            "source_kind": packet["source_kind"],
            "status": "replay",
            "collection_status": {"replay": "fixture_only"},
        },
    )
    packets = event_decision_packet_rows(runtime, symbol="MRNA")
    truth = decision_truth_rows(runtime, symbol="MRNA")
    events = event_scout_rows(runtime, symbol="MRNA")
    assert packets[0]["decision_truth"]["route_verdict"] == "NO_TRADE"
    assert truth[0]["primary_blocker"] == "max_loss_required"
    assert events[0]["status"] == "replay"


def _scout_event(packet: dict[str, object], *, status: str = "accepted") -> dict[str, object]:
    return {
        "symbol": packet["symbol"],
        "trigger_type": packet["trigger_type"],
        "observed_at": packet["as_of"],
        "source_url": packet.get("source_url"),
        "source_kind": packet.get("source_kind"),
        "status": status,
        "collection_status": {},
    }


def test_postgres_shared_truth_does_not_regress_to_an_older_packet(migrated_postgres_dsn: str) -> None:
    runtime = runtime_for_url(migrated_postgres_dsn)
    current = build_event_decision_packet("MRNA", as_of="2026-08-20T15:00:00Z", event_id="current-event")
    older = build_event_decision_packet("MRNA", as_of="2026-08-20T14:00:00Z", event_id="older-event")
    persist_event_packet(runtime, current, _scout_event(current))
    persist_event_packet(runtime, older, _scout_event(older))

    truth = decision_truth_rows(runtime, symbol="MRNA")
    assert truth[0]["event_id"] == "current-event"
    assert truth[0]["as_of"].astimezone(UTC).isoformat().startswith("2026-08-20T15:00:00")


def test_postgres_event_scout_cooldown_reservation_is_atomic(migrated_postgres_dsn: str) -> None:
    runtime = runtime_for_url(migrated_postgres_dsn)
    base = datetime(2026, 8, 20, 15, 0, tzinfo=UTC)
    first = build_event_decision_packet("MRNA", as_of=base, event_id="first-event")
    second = build_event_decision_packet("MRNA", as_of=base + timedelta(minutes=1), event_id="second-event")
    assert persist_event_packet(
        runtime, first, _scout_event(first), enforce_cooldown=True, reference_at=base
    ) is not None
    assert persist_event_packet(
        runtime, second, _scout_event(second), enforce_cooldown=True, reference_at=base + timedelta(minutes=1)
    ) is None
    assert len(event_scout_rows(runtime, symbol="MRNA")) == 1


def test_event_scout_rejects_client_timestamps_beyond_clock_skew() -> None:
    base = datetime(2026, 8, 20, 15, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="too far in the future"):
        process_signal(
            {},
            {"symbol": "MRNA", "trigger_type": "abnormal_volume", "observed_at": base + timedelta(minutes=6)},
            now=base,
        )
