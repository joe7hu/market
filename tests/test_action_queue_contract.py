from datetime import UTC, datetime
import json
from pathlib import Path

from app.routers import panel as panel_router
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


def test_today_replay_fixture_keeps_missing_trade_plan_explicit() -> None:
    fixture = json.loads((Path(__file__).parent / "fixtures" / "today_replay.json").read_text())
    action = fixture["actions"][0]
    state = action["field_states"][0]

    assert action["projection_identity"] == "capital:ticker-decision:decision:AAA"
    assert action["action"] == "NO_TRADE"
    assert action["selected_expression"] == "CASH"
    assert state == {
        "field": "trade_plan",
        "availability_status": "missing",
        "source": "trade_plan",
        "reason": "trade_plan_missing",
        "blocking": True,
        "next_action": "Refresh the ticker decision and publish its canonical TradePlan.",
    }


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


def test_action_queue_does_not_duplicate_portfolio_risk_inbox_audit() -> None:
    reference = datetime(2026, 8, 27, 15, tzinfo=UTC)
    inbox = decision_inbox_queue([
        {
            "id": "inbox-risk-1",
            "event_type": "portfolio_critical",
            "status": "active",
            "created_at": reference.isoformat(),
            "payload": {"card_id": "largest-position", "title": "TSLA risk"},
        },
    ], now=reference)
    risk = panel_router._portfolio_risk_queue([
        {
            "card_id": "largest-position",
            "severity": "critical",
            "title": "TSLA risk",
            "symbol": "TSLA",
        },
    ], now=reference)

    assert inbox == []
    assert len(dedupe_queue([*inbox, *risk])) == 1
    assert risk[0]["source"] == "portfolio_risk"


def test_today_queue_reserves_non_capital_sources_over_global_limit() -> None:
    reference = datetime(2026, 8, 27, 15, tzinfo=UTC)
    capital = [
        {"projection_identity": f"capital:{index}", "source": "capital_action", "ticker": f"T{index:03d}"}
        for index in range(150)
    ]
    inbox = decision_inbox_queue([{
        "id": "inbox-fair",
        "event_type": "ready",
        "status": "active",
        "created_at": reference.isoformat(),
        "payload": {},
    }], now=reference)
    risk = panel_router._portfolio_risk_queue([{
        "card_id": "risk-fair",
        "severity": "critical",
        "title": "Risk",
        "updated_at": reference.isoformat(),
    }], now=reference)
    research = research_queue([{
        "id": "research-fair",
        "source_family": "research",
        "title": "Research",
        "date": reference.isoformat(),
    }], now=reference)

    queue = panel_router._bounded_today_queue(capital, inbox, risk, research)

    assert len(queue) == 10
    assert {item["source"] for item in queue} == {
        "capital_action", "decision_inbox", "portfolio_risk", "research",
    }
    assert queue[0]["projection_identity"] == "capital:0"


def test_today_queue_excludes_expired_and_superseded_current_tasks() -> None:
    queue = panel_router._bounded_today_queue(
        [{"projection_identity": "capital:1", "source": "capital_action", "lifecycle_state": "actionable"}],
        [{"projection_identity": "inbox:expired", "source": "decision_inbox", "lifecycle_state": "expired"}],
        [{"projection_identity": "risk:superseded", "source": "portfolio_risk", "lifecycle_state": "superseded"}],
        [{"projection_identity": "research:current", "source": "research", "lifecycle_state": "current"}],
    )

    assert [item["projection_identity"] for item in queue] == ["capital:1", "research:current"]
