from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from investment_panel.database import decision_inbox as decision_inbox_module
from investment_panel.database.decision_inbox import DecisionInboxRepository, telegram_message
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


def _plan(
    action: str = "BUY", *, eligibility: str = "ACTIONABLE", blocker: str | None = None,
) -> SimpleNamespace:
    actionable = eligibility == "ACTIONABLE"
    return SimpleNamespace(
        action=action,
        eligibility=eligibility,
        authorization_mode="PAPER" if actionable else "NONE",
        data_quality="FRESH" if actionable else "INCOMPLETE",
        trade_plan_id=f"trade-plan:{action.lower()}",
        selected_expression_kind=SimpleNamespace(value="STOCK"),
        selected_expression_identity=f"STOCK:{action}",
        expiry=datetime(2026, 8, 20, tzinfo=UTC).date() if actionable else None,
        rationale=f"{action} rationale",
        primary_blocker=blocker,
        next_action="Refresh the decision authority.",
    )


def _row(
    published_at: datetime,
    *,
    ticker: str = "TSLA",
    episode_id: str = "episode-1",
    revision: str = "revision-1",
    policy_version: str = "risk-policy.v2:test",
    plan: SimpleNamespace | None = None,
    plan_blocker: str | None = None,
    lifecycle: str = "ACTIVE",
    as_of: datetime | None = None,
) -> dict[str, object]:
    return {
        "ticker": ticker,
        "symbol": ticker,
        "status": "published",
        "contract_version": "ticker-decision.v1",
        "as_of": as_of or published_at - timedelta(minutes=1),
        "published_at": published_at,
        "decision_revision": revision,
        "policy_version": policy_version,
        "opportunity_episode_id": episode_id,
        "resolution": {"lifecycle": lifecycle},
        "input_manifest": {},
        "_plan": plan or _plan(),
        "_plan_blocker": plan_blocker,
    }


def _stub_plan_authority(row: dict[str, object]) -> tuple[object | None, str | None]:
    return row.get("_plan"), row.get("_plan_blocker")  # type: ignore[return-value]


def _zero_transitions() -> dict[str, int]:
    return {
        "newly_actionable": 0,
        "action_changed": 0,
        "thesis_invalidated": 0,
        "decision_authority_degraded": 0,
    }


def test_decision_inbox_canonical_activation_does_not_replay_existing_decisions(
    migrated_postgres_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(decision_inbox_module, "plan_authority", _stub_plan_authority)
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    inbox = DecisionInboxRepository(runtime)
    reference = datetime(2026, 8, 12, 15, 30, tzinfo=UTC)
    try:
        old = _row(reference - timedelta(minutes=1), plan=_plan())
        assert inbox.sync_current_decisions([old], now=reference) == _zero_transitions()
        assert inbox.sync_current_decisions([old], now=reference + timedelta(minutes=2)) == _zero_transitions()
        current = _row(reference + timedelta(minutes=1), plan=_plan())
        result = inbox.sync_current_decisions([current], now=reference + timedelta(minutes=2))
        assert result["newly_actionable"] == 1
    finally:
        runtime.close()


def test_decision_inbox_canonical_actionable_transition_emits_one_dry_run_outbox(
    migrated_postgres_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(decision_inbox_module, "plan_authority", _stub_plan_authority)
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    inbox = DecisionInboxRepository(runtime)
    reference = datetime(2026, 8, 12, 15, 30, tzinfo=UTC)
    try:
        assert inbox.sync_current_decisions([], now=reference) == _zero_transitions()
        row = _row(reference + timedelta(minutes=1), plan=_plan())
        assert inbox.sync_current_decisions([row], now=reference + timedelta(minutes=2))["newly_actionable"] == 1
        assert inbox.sync_current_decisions([row], now=reference + timedelta(minutes=3)) == _zero_transitions()
        item = inbox.rows()["items"][0]
        assert item["event_type"] == "ready"
        assert item["payload"]["state_transition"] == "newly_actionable"
        assert item["payload"]["trade_plan_id"] == "trade-plan:buy"
        assert item["payload"]["authorization_mode"] == "PAPER"
        assert "entry" not in item["payload"]
        with runtime.read() as connection:
            assert connection.execute("SELECT count(*) AS count FROM app.notification_outbox").fetchone()["count"] == 1
        assert inbox.deliver_outbox(sender=None, dry_run=True) == {"sent": 0, "failed": 0, "dry_run": 1}
    finally:
        runtime.close()


def test_decision_inbox_action_change_resolves_prior_actionable_item(
    migrated_postgres_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(decision_inbox_module, "plan_authority", _stub_plan_authority)
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    inbox = DecisionInboxRepository(runtime)
    reference = datetime(2026, 8, 12, 15, 30, tzinfo=UTC)
    try:
        inbox.sync_current_decisions([], now=reference)
        first = _row(reference + timedelta(minutes=1), plan=_plan("BUY"))
        second = _row(
            reference + timedelta(minutes=2),
            revision="revision-2", plan=_plan("HOLD"),
        )
        inbox.sync_current_decisions([first], now=reference + timedelta(minutes=3))
        result = inbox.sync_current_decisions([second], now=reference + timedelta(minutes=3))
        assert result["action_changed"] == 1
        with runtime.read() as connection:
            statuses = connection.execute(
                "SELECT payload->>'state_transition' AS transition, status FROM app.decision_inbox_item ORDER BY created_at"
            ).fetchall()
        assert [(row["transition"], row["status"]) for row in statuses] == [
            ("newly_actionable", "resolved"), ("action_changed", "active"),
        ]
    finally:
        runtime.close()


def test_decision_inbox_overlapping_action_changes_keep_replacement_active(
    migrated_postgres_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(decision_inbox_module, "plan_authority", _stub_plan_authority)
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    inbox = DecisionInboxRepository(runtime)
    reference = datetime(2026, 8, 12, 15, 30, tzinfo=UTC)
    try:
        inbox.sync_current_decisions([], now=reference)
        actionable = _row(reference + timedelta(minutes=1), plan=_plan("BUY"))
        replacement = _row(
            reference + timedelta(minutes=2),
            revision="revision-2", plan=_plan("HOLD"),
        )
        inbox.sync_current_decisions([actionable], now=reference + timedelta(minutes=3))
        replacement_key = decision_inbox_module._canonical_dedupe_key(
            "episode-1", "revision-2", "action_changed", "risk-policy.v2:test",
        )
        barrier = Barrier(2)
        original_emit = DecisionInboxRepository._emit_in_transaction

        def synchronized_emit(
            repository: DecisionInboxRepository,
            connection: Any,
            **kwargs: Any,
        ) -> dict[str, Any]:
            if kwargs.get("dedupe_key") == replacement_key:
                barrier.wait(timeout=5)
            return original_emit(repository, connection, **kwargs)

        monkeypatch.setattr(DecisionInboxRepository, "_emit_in_transaction", synchronized_emit)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    DecisionInboxRepository(runtime).sync_current_decisions,
                    [replacement], now=reference + timedelta(minutes=3),
                )
                for _ in range(2)
            ]
            results = [future.result(timeout=10) for future in futures]
        assert sum(result["action_changed"] for result in results) == 1
        items = inbox.rows()["items"]
        by_transition = {item["payload"]["state_transition"]: item for item in items}
        assert by_transition["newly_actionable"]["status"] == "resolved"
        assert by_transition["action_changed"]["status"] == "active"
        with runtime.read() as connection:
            assert connection.execute("SELECT count(*) AS count FROM app.notification_outbox").fetchone()["count"] == 2
    finally:
        runtime.close()


def test_decision_inbox_emit_failure_preserves_prior_actionable_item(
    migrated_postgres_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(decision_inbox_module, "plan_authority", _stub_plan_authority)
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    inbox = DecisionInboxRepository(runtime)
    reference = datetime(2026, 8, 12, 15, 30, tzinfo=UTC)
    try:
        inbox.sync_current_decisions([], now=reference)
        actionable = _row(reference + timedelta(minutes=1), plan=_plan("BUY"))
        replacement = _row(
            reference + timedelta(minutes=2),
            revision="revision-2", plan=_plan("HOLD"),
        )
        inbox.sync_current_decisions([actionable], now=reference + timedelta(minutes=3))

        def fail_emit(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("replacement emit failed")

        monkeypatch.setattr(inbox, "_emit_in_transaction", fail_emit)
        with pytest.raises(RuntimeError, match="replacement emit failed"):
            inbox.sync_current_decisions([replacement], now=reference + timedelta(minutes=3))
        item = inbox.rows()["items"][0]
        assert item["payload"]["state_transition"] == "newly_actionable"
        assert item["status"] == "active"
        with runtime.read() as connection:
            assert connection.execute("SELECT count(*) AS count FROM app.notification_outbox").fetchone()["count"] == 1
    finally:
        runtime.close()


def test_decision_inbox_invalidated_transition_is_no_trade_and_resolves_prior(
    migrated_postgres_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(decision_inbox_module, "plan_authority", _stub_plan_authority)
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    inbox = DecisionInboxRepository(runtime)
    reference = datetime(2026, 8, 12, 15, 30, tzinfo=UTC)
    try:
        inbox.sync_current_decisions([], now=reference)
        actionable = _row(reference + timedelta(minutes=1), plan=_plan("BUY"))
        invalidated = _row(
            reference + timedelta(minutes=2), revision="revision-2", lifecycle="INVALIDATED",
            plan=_plan("NO_TRADE", eligibility="BLOCKED", blocker="thesis_invalidated"),
        )
        inbox.sync_current_decisions([actionable], now=reference + timedelta(minutes=3))
        result = inbox.sync_current_decisions([invalidated], now=reference + timedelta(minutes=3))
        assert result["thesis_invalidated"] == 1
        item = inbox.rows()["items"][0]
        assert item["event_type"] == "revoked"
        assert item["payload"]["action"] == "NO_TRADE"
        assert item["payload"]["state"] == "NO_TRADE"
        assert item["payload"]["lifecycle"] == "INVALIDATED"
        assert "selected_expression_kind" not in item["payload"]
        assert [row["status"] for row in inbox.rows()["items"]] == ["active", "resolved"]
    finally:
        runtime.close()


def test_decision_inbox_lost_plan_authority_degrades_fail_closed(
    migrated_postgres_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(decision_inbox_module, "plan_authority", _stub_plan_authority)
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    inbox = DecisionInboxRepository(runtime)
    reference = datetime(2026, 8, 12, 15, 30, tzinfo=UTC)
    try:
        inbox.sync_current_decisions([], now=reference)
        actionable = _row(reference + timedelta(minutes=1), plan=_plan("BUY"))
        degraded = _row(
            reference + timedelta(minutes=2), revision="revision-2", plan=_plan("BUY"),
            plan_blocker="ranking_publication_mismatch",
        )
        inbox.sync_current_decisions([actionable], now=reference + timedelta(minutes=3))
        result = inbox.sync_current_decisions([degraded], now=reference + timedelta(minutes=3))
        assert result["decision_authority_degraded"] == 1
        assert inbox.sync_current_decisions([degraded], now=reference + timedelta(minutes=4)) == _zero_transitions()
        item = inbox.rows()["items"][0]
        payload = item["payload"]
        assert item["event_type"] == "revoked"
        assert payload["action"] == "NO_TRADE"
        assert payload["authorization_mode"] == "NONE"
        assert payload["primary_blocker"] == "ranking_publication_mismatch"
        assert payload["blockers"] == ["ranking_publication_mismatch"]
        assert payload["next_action"]
        assert "trade_plan_id" not in payload
        assert "selected_expression_kind" not in payload
        assert "entry" not in payload
        assert [row["status"] for row in inbox.rows()["items"]] == ["active", "resolved"]
    finally:
        runtime.close()


def test_decision_inbox_blocked_episode_without_prior_actionability_is_silent(
    migrated_postgres_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(decision_inbox_module, "plan_authority", _stub_plan_authority)
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    inbox = DecisionInboxRepository(runtime)
    reference = datetime(2026, 8, 12, 15, 30, tzinfo=UTC)
    try:
        inbox.sync_current_decisions([], now=reference)
        blocked = _row(
            reference + timedelta(minutes=1),
            plan=_plan("NO_TRADE", eligibility="BLOCKED", blocker="current_price"),
        )
        assert inbox.sync_current_decisions([blocked], now=reference + timedelta(minutes=2)) == _zero_transitions()
        assert inbox.rows()["items"] == []
    finally:
        runtime.close()


def test_decision_inbox_rejects_invalid_current_rows_and_duplicate_authority(
    migrated_postgres_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(decision_inbox_module, "plan_authority", _stub_plan_authority)
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    inbox = DecisionInboxRepository(runtime)
    reference = datetime(2026, 8, 12, 15, 30, tzinfo=UTC)
    try:
        inbox.sync_current_decisions([], now=reference)
        published = reference + timedelta(minutes=1)
        invalid = [
            _row(published, ticker="MISSING_EPISODE", episode_id=""),
            _row(published, ticker="MISSING_REVISION", revision=""),
            _row(published, ticker="MISSING_POLICY", policy_version=""),
            _row(published + timedelta(minutes=5), ticker="FUTURE", as_of=published + timedelta(minutes=5)),
            _row(published, ticker="MALFORMED", plan=_plan()),
        ]
        invalid[-1]["resolution"] = "not-an-object"
        duplicate = _row(published, ticker="DUPLICATE", episode_id="episode-a", plan=_plan())
        duplicate_other = _row(published, ticker="DUPLICATE", episode_id="episode-b", plan=_plan())
        result = inbox.sync_current_decisions([*invalid, duplicate, duplicate_other], now=reference + timedelta(minutes=2))
        assert result == _zero_transitions()
        assert inbox.rows()["items"] == []
    finally:
        runtime.close()


def test_ticker_paper_lifecycle_projection_is_bounded_and_ordered() -> None:
    reference = datetime(2026, 8, 12, 15, 30, tzinfo=UTC)
    plan = SimpleNamespace(
        ticker="TSLA", trade_plan_id="trade-plan:tsla", publication_id="publication:tsla",
        opportunity_episode_id="episode-tsla", decision_revision="revision-tsla",
        policy_version="risk-policy.v2:tsla",
        selected_expression_kind=SimpleNamespace(value="STOCK"),
        selected_expression_identity="STOCK:tsla",
    )
    order = {
        "id": str(uuid4()), "_trade_plan": plan, "quantity": 2,
        "created_at": reference + timedelta(minutes=1),
        "filled_quantity": 2, "filled_at": reference + timedelta(minutes=2),
        "exited_quantity": 2, "exit_at": reference + timedelta(minutes=3),
        "status": "exited", "actual_fill_price": 101.25, "exit_price": 105.5,
        "fees": 0.25, "unfilled_reason": None,
    }

    events = decision_inbox_module._ticker_paper_events(
        order, reference, reference + timedelta(minutes=4),
    )

    assert [event["state_transition"] for event in events] == [
        "paper_staged", "paper_filled", "paper_exited",
    ]
    assert events[0]["quantity"] == 2.0
    assert events[1]["filled_quantity"] == 2.0
    assert events[2]["exited_quantity"] == 2.0
    assert events[2]["transition_at"] == (reference + timedelta(minutes=3)).isoformat()


def test_ticker_paper_message_is_paper_only_and_excludes_untrusted_fields() -> None:
    message = telegram_message({
        "paper_only": True, "ticker": "TSLA", "state_transition": "paper_filled",
        "paper_order_id": str(uuid4()), "trade_plan_id": "trade-plan:tsla",
        "quantity": 2, "filled_quantity": 2, "fill_price": 101.25, "fees": 0.25,
        "detail_url": "/tickers/TSLA", "evidence": {"secret": "drop"},
    })

    assert "PAPER ONLY" in message
    assert "paper_filled" in message
    assert "LIVE" not in message
    assert "evidence" not in message.lower()
