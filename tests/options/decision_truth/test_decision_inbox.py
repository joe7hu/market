from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from investment_panel.database.decision_inbox import DecisionInboxRepository, telegram_message
from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.runtime import DatabaseRuntime


def test_decision_inbox_dedupes_actionable_ticket_events_and_dry_runs_delivery(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    repository = DecisionInboxRepository(runtime)
    decision_id = str(uuid4())
    try:
        first = repository.emit(
            event_type="ready",
            opportunity_id=decision_id,
            ticket_version=4,
            lane="radar",
            payload={
                "symbol": "TSLA", "structure": "long_option", "lane": "radar",
                "state": "READY", "entry": 1.2, "max_risk": 120,
                "evidence": {"large": "must not persist"},
            },
        )
        second = repository.emit(
            event_type="ready",
            opportunity_id=decision_id,
            ticket_version=4,
            lane="radar",
            payload={"symbol": "TSLA", "state": "READY"},
        )
        page = repository.rows()
        delivery = repository.deliver_outbox(sender=None, dry_run=True)
        assert first["created"] is True
        assert second["created"] is False
        assert page["count"] == 1
        assert page["items"][0]["payload"]["symbol"] == "TSLA"
        assert "evidence" not in page["items"][0]["payload"]
        assert delivery == {"sent": 0, "failed": 0, "dry_run": 1}
        assert repository.rows()["items"][0]["delivery_status"] == "dry_run"
    finally:
        runtime.close()


def test_decision_inbox_retries_only_the_compact_fixed_owner_message(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    repository = DecisionInboxRepository(runtime)
    try:
        repository.emit(
            event_type="portfolio_critical",
            payload={"symbol": "NVDA", "state": "CRITICAL", "reason": "concentration", "raw": {"secret": "drop"}},
            severity="critical",
        )
        result = repository.deliver_outbox(sender=lambda _message: (_ for _ in ()).throw(RuntimeError("relay unavailable")), dry_run=False)
        row = repository.rows()["items"][0]
        assert result == {"sent": 0, "failed": 1, "dry_run": 0}
        assert row["delivery_status"] == "failed"
        assert "RuntimeError" in str(row["last_error"])
        message = telegram_message({"symbol": "NVDA", "state": "CRITICAL", "evidence": {"do_not": "send"}})
        assert "evidence" not in message.lower()
    finally:
        runtime.close()


def test_decision_inbox_rejects_operational_noise() -> None:
    class _Runtime:
        pass

    repository = DecisionInboxRepository(_Runtime())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unsupported decision Inbox event type"):
        repository.emit(event_type="provider_failure", payload={})


def test_decision_inbox_does_not_read_a_future_publication(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    analysis = AnalysisRepository(runtime)
    inbox = DecisionInboxRepository(runtime)
    decision_id = str(uuid4())
    reference = datetime(2026, 8, 12, 15, 30, tzinfo=UTC)
    try:
        run_id = analysis.start_run(
            "inbox-pit-test", input_cutoff=reference, code_version="test",
            inputs={"reference": reference.isoformat()},
        )
        analysis.finish_run(run_id, "succeeded")
        publication_id = analysis.publish(
            run_id,
            "options-radar",
            {
                "option_radar_opportunity": [{
                    "decision_id": decision_id,
                    "symbol": "TSLA",
                    "ticket": {
                        "decision_id": decision_id,
                        "ticket_version": 4,
                        "lane": "radar",
                        "state": "READY",
                        "blockers": [],
                        "expires_at": (reference + timedelta(hours=1)).isoformat(),
                    },
                }],
            },
        )
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE app.publication SET published_at = %s WHERE id = %s::uuid",
                [reference + timedelta(minutes=1), publication_id],
            )

        before = inbox.sync_current_tickets(now=reference)
        after = inbox.sync_current_tickets(now=reference + timedelta(minutes=2))

        assert before["ready"] == 0
        assert after["ready"] == 1
    finally:
        runtime.close()


def test_decision_inbox_persists_high_priority_research_without_notification(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    analysis = AnalysisRepository(runtime)
    inbox = DecisionInboxRepository(runtime)
    decision_id = str(uuid4())
    reference = datetime(2026, 8, 12, 15, 30, tzinfo=UTC)
    try:
        # Arm the lifecycle watermark before the new publication arrives.
        assert inbox.sync_current_tickets(now=reference)["high_priority_research"] == 0
        run_id = analysis.start_run(
            "inbox-research-test", input_cutoff=reference, code_version="test",
            inputs={"reference": reference.isoformat()},
        )
        analysis.finish_run(run_id, "succeeded")
        publication_id = analysis.publish(
            run_id,
            "options-radar",
            {
                "option_radar_opportunity": [{
                    "decision_id": decision_id,
                    "symbol": "NBIS",
                    "state": "SETUP",
                    "tier": "Research",
                    "ticket": {
                        "decision_id": decision_id,
                        "ticket_version": 8,
                        "lane": "radar",
                        "state": "RESEARCH",
                        "blockers": ["calibrated_probability_required"],
                        "expires_at": (reference + timedelta(minutes=2)).isoformat(),
                    },
                }],
            },
        )
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE app.publication SET published_at = %s WHERE id = %s::uuid",
                [reference + timedelta(minutes=1), publication_id],
            )

        created = inbox.sync_current_tickets(now=reference + timedelta(minutes=2))
        page = inbox.rows()
        with runtime.read() as connection:
            outbox_count = connection.execute("SELECT count(*) AS count FROM app.notification_outbox").fetchone()["count"]

        assert created["high_priority_research"] == 1
        assert created["ready"] == 0
        assert page["items"][0]["event_type"] == "high_priority_research"
        assert page["items"][0]["payload"]["state"] == "HIGH_PRIORITY_RESEARCH"
        assert outbox_count == 0
    finally:
        runtime.close()


def test_decision_inbox_arms_without_replaying_existing_ticket(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    analysis = AnalysisRepository(runtime)
    inbox = DecisionInboxRepository(runtime)
    decision_id = str(uuid4())
    reference = datetime(2026, 8, 12, 15, 30, tzinfo=UTC)
    try:
        run_id = analysis.start_run(
            "inbox-bootstrap-test", input_cutoff=reference, code_version="test",
            inputs={"reference": reference.isoformat()},
        )
        analysis.finish_run(run_id, "succeeded")
        publication_id = analysis.publish(
            run_id,
            "options-radar",
            {
                "option_radar_opportunity": [{
                    "decision_id": decision_id,
                    "symbol": "TSLA",
                    "ticket": {
                        "decision_id": decision_id,
                        "ticket_version": 4,
                        "lane": "radar",
                        "state": "READY",
                        "blockers": [],
                        "expires_at": (reference + timedelta(hours=1)).isoformat(),
                    },
                }],
            },
        )
        with runtime.transaction() as connection:
            connection.execute(
                "UPDATE app.publication SET published_at = %s WHERE id = %s::uuid",
                [reference - timedelta(minutes=1), publication_id],
            )

        result = inbox.sync_current_tickets(now=reference)

        assert result == {"ready": 0, "revoked": 0, "expired": 0, "high_priority_research": 0}
        assert inbox.rows()["items"] == []
    finally:
        runtime.close()
