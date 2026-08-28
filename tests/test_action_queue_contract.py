from datetime import UTC, datetime

from app.routers.panel import dedupe_queue, decision_inbox_queue, research_queue


def test_action_queue_keeps_current_transitions_and_dedupes_exact_source_identity() -> None:
    reference = datetime(2026, 8, 27, 15, tzinfo=UTC)
    rows = [
        {"id": "inbox-1", "event_type": "ready", "status": "active", "created_at": reference.isoformat(), "payload": {"symbol": "AAA"}},
        {"id": "inbox-1", "event_type": "ready", "status": "active", "created_at": reference.isoformat(), "payload": {"symbol": "AAA"}},
        {"id": "inbox-2", "event_type": "expired", "status": "active", "created_at": reference.isoformat(), "payload": {"symbol": "AAA"}},
        {"id": "inbox-3", "event_type": "ready", "status": "resolved", "created_at": reference.isoformat(), "payload": {"symbol": "AAA"}},
    ]

    items = dedupe_queue(decision_inbox_queue(rows, now=reference))

    assert [item["projection_identity"] for item in items] == ["inbox:decision-inbox:inbox-1", "inbox:decision-inbox:inbox-2"]
    assert items[1]["lifecycle_state"] == "expired"
    assert all(item["action"] == "NO_TRADE" for item in items)


def test_research_queue_preserves_source_order_without_cross_source_scoring() -> None:
    rows = [
        {"id": "research-1", "source_family": "research", "title": "First", "date": "2026-08-27T14:00:00Z"},
        {"id": "noise", "source_family": "news", "priority": "low", "title": "Skip"},
        {"id": "research-2", "source_family": "news", "decision_blocking": True, "title": "Second", "date": "2026-08-27T13:00:00Z"},
    ]

    items = research_queue(rows, now=datetime(2026, 8, 27, 15, tzinfo=UTC))

    assert [item["title"] for item in items] == ["First", "Second"]
    assert items[1]["primary_blocker"] == "research_decision_blocked"
    assert [item["next_action"] for item in items] == ["Review the source evidence.", "Review the source evidence."]


def test_missing_inbox_identity_is_explicit_and_non_actionable() -> None:
    item = decision_inbox_queue([{"status": "active", "payload": {}}])[0]

    assert item["source_authority"] == "decision-inbox:missing"
    assert item["lifecycle_state"] == "unavailable"
    assert item["primary_blocker"] == "decision_inbox_identity_missing"
    assert item["action"] == "NO_TRADE"
