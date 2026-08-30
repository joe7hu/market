from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
import hashlib
import json
from threading import Barrier
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from investment_panel.core.decision import (
    ExpressionDecision,
    ExpressionKind,
    Horizon,
    InputLineage,
    Invalidation,
    PortfolioImpact,
    PriceRange,
    Stance,
    TradePlan,
    trade_expression_identity,
)
from investment_panel.database import decision_inbox as decision_inbox_module
from investment_panel.database.decision_inbox import DecisionInboxRepository, telegram_message
from investment_panel.database.instruments import reconcile_instrument
from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.jobs import decision_inbox as decision_inbox_job


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


def _paper_plan(ticker: str, cutoff: datetime) -> TradePlan:
    lineage = InputLineage(
        field="decision", source_id="decision-inbox-test",
        available_at=cutoff - timedelta(minutes=5), cutoff=cutoff,
    )
    expression = ExpressionDecision(
        kind=ExpressionKind.STOCK, ticker=ticker, horizon=Horizon.TACTICAL,
        thesis_revision=f"thesis:{ticker}", stance=Stance.BULLISH,
        entry_range=PriceRange(low=99, high=101),
        target_range=PriceRange(low=110, high=120),
        invalidation=Invalidation(kind="price", value=90, statement="below 90"),
        quantity=2, loss_budget=20, max_loss_per_unit=10, planned_loss=20,
        status="eligible", selected=True, rationale="test plan",
    )
    identity = trade_expression_identity(expression)
    impact = PortfolioImpact(
        impact_id=f"impact:{ticker}", ticker=ticker, opportunity_episode_id=f"episode:{ticker}",
        expression_kind=ExpressionKind.STOCK, expression_identity=identity,
        decision_revision=f"revision:{ticker}", risk_policy_version=f"policy:{ticker}",
        market_snapshot_id=f"snapshot:{ticker}",
        market_state_publication_id=f"market-publication:{ticker}", cutoff=cutoff,
        input_lineage=(lineage,), availability="unavailable", blockers=("test-only",),
    )
    values: dict[str, Any] = {
        "contract_version": "trade-plan.v1", "publication_id": f"publication:{ticker}",
        "ticker": ticker, "opportunity_episode_id": f"episode:{ticker}",
        "decision_revision": f"revision:{ticker}", "policy_version": f"policy:{ticker}",
        "cutoff": cutoff, "input_lineage": (lineage,),
        "selected_expression_kind": ExpressionKind.STOCK,
        "selected_expression_identity": identity, "selected_expression": expression,
        "rank_id": f"rank:{ticker}", "alpha_signal_id": f"signal:{ticker}",
        "portfolio_impact_id": impact.impact_id, "market_snapshot_id": f"snapshot:{ticker}",
        "market_state_publication_id": f"market-publication:{ticker}",
        "action": "BUY", "eligibility": "ACTIONABLE", "authorization_mode": "PAPER",
        "data_quality": "FRESH", "rationale": "test plan", "primary_blocker": None,
        "blockers": (),
        "next_action": "observe", "entry": expression.entry_range, "entry_limit": 100.0,
        "quantity": 2, "max_loss_per_unit": 10.0, "planned_loss": 20.0,
        "invalidation": expression.invalidation, "profit_exit": expression.target_range,
        "expiry": cutoff.date() + timedelta(days=10), "portfolio_impact": impact,
    }
    values["trade_plan_id"] = _paper_plan_id({
        key: value for key, value in values.items() if key != "publication_id"
    })
    return TradePlan.model_validate(values)


def _paper_plan_id(value: Any) -> str:
    def jsonable(item: Any) -> Any:
        if hasattr(item, "model_dump"):
            return item.model_dump(mode="json")
        if isinstance(item, dict):
            return {str(key): jsonable(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [jsonable(child) for child in item]
        if isinstance(item, (datetime, date)):
            return item.isoformat().replace("+00:00", "Z") if isinstance(item, datetime) else item.isoformat()
        if hasattr(item, "value"):
            return item.value
        return item

    encoded = json.dumps(jsonable(value), sort_keys=True, separators=(",", ":"))
    return "trade-plan.v1:" + hashlib.sha256(encoded.encode()).hexdigest()


def _paper_policy(plan: TradePlan) -> dict[str, Any]:
    return {
        "owner": "ticker-first", "decision_revision": plan.decision_revision,
        "policy_version": plan.policy_version, "expression_kind": plan.selected_expression_kind.value,
        "trade_plan_id": plan.trade_plan_id,
        "trade_plan_publication_id": plan.publication_id,
        "opportunity_rank": {
            "ticker": plan.ticker, "opportunity_episode_id": plan.opportunity_episode_id,
            "selected_expression_identity": plan.selected_expression_identity,
        },
        "trade_plan": plan.model_dump(mode="json"),
    }


def _insert_ticker_paper_order(
    runtime: DatabaseRuntime,
    plan: TradePlan,
    *,
    created_at: datetime,
    instrument_symbol: str | None = None,
    lane: str = "ticker",
    paper_only: bool = True,
    status: str = "staged",
    filled_quantity: float | None = None,
    filled_at: datetime | None = None,
    actual_fill_price: float | None = None,
    exited_quantity: float = 0,
    exit_at: datetime | None = None,
    exit_price: float | None = None,
    fees: float = 0,
    unfilled_reason: str | None = None,
    policy_result: dict[str, Any] | None = None,
) -> str:
    policy = policy_result or _paper_policy(plan)
    snapshot = {"trade_plan": plan.model_dump(mode="json"), "ticker": plan.ticker}
    symbol = instrument_symbol or plan.ticker
    with runtime.transaction() as connection:
        instrument_id = reconcile_instrument(
            connection, symbol, name=symbol, asset_class="equity", category="test",
        )
        row = connection.execute(
            """
            INSERT INTO app.paper_order (
                instrument_id, created_at, side, quantity, limit_price, status,
                policy_result, policy_snapshot, lane, ticker_decision_revision,
                expression_kind, thesis_snapshot, ticket_snapshot, paper_only,
                filled_quantity, filled_at, actual_fill_price, exited_quantity,
                exit_at, exit_price, fees, unfilled_reason
            ) VALUES (
                %s, %s, 'buy', 2, 100, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            RETURNING id::text
            """,
            [
                instrument_id, created_at, status, Jsonb(policy), Jsonb(policy), lane,
                plan.decision_revision, plan.selected_expression_kind.value,
                Jsonb(snapshot), Jsonb(snapshot), paper_only, filled_quantity, filled_at,
                actual_fill_price, exited_quantity, exit_at, exit_price, fees, unfilled_reason,
            ],
        ).fetchone()
    assert row is not None
    return str(row["id"])


def _zero_paper_transitions() -> dict[str, int]:
    return {"paper_staged": 0, "paper_filled": 0, "paper_exited": 0}


def _zero_portfolio_risk() -> dict[str, int]:
    return {"breached": 0, "resolved": 0}


def _risk_summary(reference: datetime) -> dict[str, Any]:
    return {
        "as_of": reference.isoformat(),
        "available_at": reference.isoformat(),
        "portfolio_value": 100_000.0,
    }


def _risk_card(
    card_id: str = "largest-position", *, severity: str = "critical",
) -> dict[str, Any]:
    risk_type = {
        "largest-position": "concentration",
        "portfolio-drawdown": "drawdown",
        "stale-owned-quotes": "data_freshness",
    }.get(card_id, "correlation" if card_id.startswith("correlation:") else "concentration")
    return {
        "card_id": card_id,
        "risk_type": risk_type,
        "severity": severity,
        "score": 90,
        "title": "TSLA dominates the portfolio",
        "summary": "One position dominates portfolio outcomes.",
        "symbols": ["tsla", "TSLA"],
        "impact": "72.0% of current value",
        "next_step": "Review the maximum intended weight.",
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


def test_decision_inbox_hides_duplicate_active_episode_answers(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    inbox = DecisionInboxRepository(runtime)
    try:
        for revision, dedupe_key in (("revision-1", "duplicate-inbox-1"), ("revision-2", "duplicate-inbox-2")):
            inbox.emit(
                event_type="ready",
                lane="ticker",
                enqueue_telegram=False,
                dedupe_key=dedupe_key,
                payload={
                    "ticker": "DUPINBOX",
                    "symbol": "DUPINBOX",
                    "opportunity_episode_id": "episode:duplicate-inbox",
                    "decision_revision": revision,
                    "state_transition": "newly_actionable",
                },
            )

        assert inbox.rows()["items"] == []
    finally:
        runtime.close()


def test_decision_inbox_hides_active_canonical_answer_without_episode_identity(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    inbox = DecisionInboxRepository(runtime)
    try:
        inbox.emit(
            event_type="ready",
            lane="ticker",
            enqueue_telegram=False,
            dedupe_key="missing-canonical-episode",
            payload={
                "ticker": "LEGACY",
                "state": "ACTIONABLE",
                "state_transition": "newly_actionable",
            },
        )
        inbox.emit(
            event_type="paper_filled",
            lane="ticker",
            enqueue_telegram=False,
            dedupe_key="lifecycle-without-episode",
            payload={"ticker": "LEGACY", "state_transition": "paper_filled"},
        )

        result = inbox.rows()

        assert [item["payload"]["state_transition"] for item in result["items"]] == ["paper_filled"]
        assert result["authority"] == {
            "status": "unavailable",
            "reason": "canonical_transition_authority_missing_episode",
            "missing_episode_count": 1,
            "duplicate_episode_count": 0,
        }
    finally:
        runtime.close()


def test_ticker_paper_lifecycle_postgres_activation_filtering_and_dedupe(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    inbox = DecisionInboxRepository(runtime)
    reference = datetime(2026, 8, 12, 15, 30, tzinfo=UTC)
    try:
        assert inbox.sync_ticker_paper_lifecycle(now=reference) == _zero_paper_transitions()
        old_plan = _paper_plan("OLD", reference - timedelta(minutes=2))
        _insert_ticker_paper_order(
            runtime, old_plan, created_at=reference - timedelta(minutes=1),
        )
        current_plan = _paper_plan("TSLA", reference)
        current_order_id = _insert_ticker_paper_order(
            runtime, current_plan, created_at=reference + timedelta(minutes=1),
        )
        _insert_ticker_paper_order(
            runtime, current_plan, created_at=reference + timedelta(minutes=1), lane="radar",
        )
        _insert_ticker_paper_order(
            runtime, current_plan, created_at=reference + timedelta(minutes=1), paper_only=False,
        )
        invalid_policy = _paper_policy(current_plan)
        invalid_policy["trade_plan"] = {
            **invalid_policy["trade_plan"], "ticker": "WRONG",
        }
        _insert_ticker_paper_order(
            runtime, current_plan, created_at=reference + timedelta(minutes=1),
            policy_result=invalid_policy,
        )

        result = inbox.sync_ticker_paper_lifecycle(now=reference + timedelta(minutes=2))
        assert result == {"paper_staged": 1, "paper_filled": 0, "paper_exited": 0}
        assert inbox.sync_ticker_paper_lifecycle(now=reference + timedelta(minutes=3)) == _zero_paper_transitions()
        with runtime.read() as connection:
            rows = connection.execute(
                """
                SELECT paper_order_id::text, dedupe_key, payload, status
                FROM app.decision_inbox_item
                WHERE lane = 'ticker'
                ORDER BY created_at, id
                """
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["paper_order_id"] == current_order_id
        assert rows[0]["dedupe_key"] == decision_inbox_module._ticker_paper_dedupe_key(
            current_order_id, current_plan.trade_plan_id,
            current_plan.decision_revision, current_plan.policy_version, "paper_staged",
            episode_id=current_plan.opportunity_episode_id,
        )
        assert rows[0]["payload"]["paper_only"] is True
    finally:
        runtime.close()


def test_ticker_paper_lifecycle_postgres_missed_poll_resolves_current_item(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    inbox = DecisionInboxRepository(runtime)
    reference = datetime(2026, 8, 12, 15, 30, tzinfo=UTC)
    plan = _paper_plan("TSLA", reference)
    try:
        assert inbox.sync_ticker_paper_lifecycle(now=reference) == _zero_paper_transitions()
        order_id = _insert_ticker_paper_order(
            runtime, plan, created_at=reference + timedelta(minutes=1), status="exited",
            filled_quantity=2, filled_at=reference + timedelta(minutes=2), actual_fill_price=101.25,
            exited_quantity=2, exit_at=reference + timedelta(minutes=3), exit_price=105.5,
            fees=0.25, unfilled_reason="missed poll",
        )
        inbox.emit(
            event_type="ready", lane="ticker",
            payload={
                "ticker": plan.ticker, "opportunity_episode_id": plan.opportunity_episode_id,
                "trade_plan_id": plan.trade_plan_id, "decision_revision": plan.decision_revision,
                "policy_version": plan.policy_version, "state_transition": "newly_actionable",
            },
        )

        result = inbox.sync_ticker_paper_lifecycle(now=reference + timedelta(minutes=4))
        assert result == {"paper_staged": 1, "paper_filled": 1, "paper_exited": 1}
        with runtime.read() as connection:
            rows = connection.execute(
                """
                SELECT event_type, paper_order_id::text, payload, status, dedupe_key
                FROM app.decision_inbox_item
                WHERE lane = 'ticker'
                ORDER BY created_at, id
                """
            ).fetchall()
        by_transition = {row["payload"]["state_transition"]: row for row in rows}
        assert by_transition["newly_actionable"]["status"] == "resolved"
        assert by_transition["paper_staged"]["status"] == "resolved"
        assert by_transition["paper_filled"]["status"] == "resolved"
        assert by_transition["paper_exited"]["status"] == "active"
        for transition in ("paper_staged", "paper_filled", "paper_exited"):
            row = by_transition[transition]
            assert row["paper_order_id"] == order_id
            assert row["dedupe_key"] == decision_inbox_module._ticker_paper_dedupe_key(
                order_id, plan.trade_plan_id, plan.decision_revision, plan.policy_version, transition,
                episode_id=plan.opportunity_episode_id,
            )
            assert row["payload"]["paper_only"] is True
            assert "trade_plan" not in row["payload"]
            assert "evidence" not in row["payload"]
        assert by_transition["paper_exited"]["payload"]["reason"] == "missed poll"
        assert by_transition["paper_filled"]["payload"]["fill_price"] == 101.25
        assert by_transition["paper_exited"]["payload"]["exit_price"] == 105.5
    finally:
        runtime.close()


def test_ticker_paper_lifecycle_postgres_rejects_bad_sequence_identity_ticker_and_future_plan(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    inbox = DecisionInboxRepository(runtime)
    reference = datetime(2026, 8, 12, 15, 30, tzinfo=UTC)
    try:
        assert inbox.sync_ticker_paper_lifecycle(now=reference) == _zero_paper_transitions()
        sequence_plan = _paper_plan("SEQUENCE", reference)
        _insert_ticker_paper_order(
            runtime, sequence_plan, created_at=reference + timedelta(minutes=3), status="exited",
            filled_quantity=2, filled_at=reference + timedelta(minutes=2),
            exited_quantity=2, exit_at=reference + timedelta(minutes=4),
        )
        mismatch_plan = _paper_plan("PLAN", reference)
        _insert_ticker_paper_order(
            runtime, mismatch_plan, created_at=reference + timedelta(minutes=1),
            instrument_symbol="INSTRUMENT",
        )
        future_plan = _paper_plan("FUTURE", reference + timedelta(minutes=10))
        _insert_ticker_paper_order(
            runtime, future_plan, created_at=reference + timedelta(minutes=20),
        )

        result = inbox.sync_ticker_paper_lifecycle(now=reference + timedelta(minutes=5))
        assert result == {"paper_staged": 1, "paper_filled": 0, "paper_exited": 0}
        assert len(inbox.rows()["items"]) == 1
    finally:
        runtime.close()


def test_ticker_paper_lifecycle_postgres_advisory_lock_dedupes_overlap(
    migrated_postgres_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    inbox = DecisionInboxRepository(runtime)
    reference = datetime(2026, 8, 12, 15, 30, tzinfo=UTC)
    plan = _paper_plan("OVERLAP", reference)
    try:
        inbox.sync_ticker_paper_lifecycle(now=reference)
        _insert_ticker_paper_order(
            runtime, plan, created_at=reference + timedelta(minutes=1),
        )
        barrier = Barrier(2)
        original_activation = DecisionInboxRepository._paper_lifecycle_activation

        def synchronized_activation(
            repository: DecisionInboxRepository, value: datetime,
        ) -> tuple[datetime, bool]:
            result = original_activation(repository, value)
            barrier.wait(timeout=5)
            return result

        monkeypatch.setattr(DecisionInboxRepository, "_paper_lifecycle_activation", synchronized_activation)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    DecisionInboxRepository(runtime).sync_ticker_paper_lifecycle,
                    now=reference + timedelta(minutes=2),
                )
                for _ in range(2)
            ]
            results = [future.result(timeout=10) for future in futures]
        assert sum(result["paper_staged"] for result in results) == 1
        with runtime.read() as connection:
            assert connection.execute(
                "SELECT count(*) AS count FROM app.decision_inbox_item WHERE lane = 'ticker'"
            ).fetchone()["count"] == 1
    finally:
        runtime.close()


def test_ticker_paper_lifecycle_postgres_rolls_back_emit_and_retries(
    migrated_postgres_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    inbox = DecisionInboxRepository(runtime)
    reference = datetime(2026, 8, 12, 15, 30, tzinfo=UTC)
    plan = _paper_plan("ROLLBACK", reference)
    try:
        inbox.sync_ticker_paper_lifecycle(now=reference)
        _insert_ticker_paper_order(
            runtime, plan, created_at=reference + timedelta(minutes=1),
        )

        original_resolution = DecisionInboxRepository._resolve_ticker_paper_items

        def fail_resolution(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("resolution failed")

        monkeypatch.setattr(DecisionInboxRepository, "_resolve_ticker_paper_items", fail_resolution)
        with pytest.raises(RuntimeError, match="resolution failed"):
            inbox.sync_ticker_paper_lifecycle(now=reference + timedelta(minutes=2))
        with runtime.read() as connection:
            assert connection.execute("SELECT count(*) AS count FROM app.decision_inbox_item").fetchone()["count"] == 0
            assert connection.execute("SELECT count(*) AS count FROM app.notification_outbox").fetchone()["count"] == 0

        monkeypatch.setattr(DecisionInboxRepository, "_resolve_ticker_paper_items", original_resolution)
        assert inbox.sync_ticker_paper_lifecycle(now=reference + timedelta(minutes=2))["paper_staged"] == 1
    finally:
        runtime.close()


def test_decision_inbox_job_calls_paper_lifecycle_once_after_decisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    loaded_tables: list[tuple[str, ...]] = []
    settings = SimpleNamespace(
        decision_inbox_enabled=True, telegram_notifications_enabled=False,
    )
    config = SimpleNamespace(analysis=SimpleNamespace(options_decision_system=settings))

    class FakeRepository:
        def __init__(self, _runtime: object) -> None:
            pass

        def sync_current_decisions(self, rows: list[object]) -> dict[str, int]:
            calls.append("decisions")
            assert rows == []
            return {"newly_actionable": 0}

        def sync_ticker_paper_lifecycle(self) -> dict[str, int]:
            calls.append("paper_lifecycle")
            return _zero_paper_transitions()

        def sync_current_portfolio_risk(
            self, cards: list[object] | None, summary: dict[str, object] | None,
        ) -> dict[str, int]:
            calls.append("portfolio_risk")
            assert cards == []
            assert summary is None
            return _zero_portfolio_risk()

    monkeypatch.setattr(decision_inbox_job, "load_config", lambda _path: config)

    def fake_load_postgres_tables(_config: object, table_names: tuple[str, ...], **_kwargs: Any) -> tuple[dict[str, list[object]], dict[str, list[str]]]:
        loaded_tables.append(table_names)
        return {name: [] for name in table_names}, {"unavailable_models": []}  # type: ignore[return-value]

    monkeypatch.setattr(
        decision_inbox_job,
        "load_postgres_tables",
        fake_load_postgres_tables,
    )
    monkeypatch.setattr(decision_inbox_job, "runtime_for_config", lambda _config: object())
    monkeypatch.setattr(decision_inbox_job, "DecisionInboxRepository", FakeRepository)

    result = decision_inbox_job.run()

    assert loaded_tables == [
        ("ticker_decisions", "portfolio_summary", "portfolio_performance", "correlation_edges", "portfolio_risk_cards"),
    ]
    assert calls == ["decisions", "paper_lifecycle", "portfolio_risk"]
    assert result["paper_lifecycle"] == _zero_paper_transitions()
    assert result["portfolio_risk"] == _zero_portfolio_risk()
    assert result["delivery"]["skipped"] == 1


def test_decision_inbox_job_produces_portfolio_risk_before_enabled_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    settings = SimpleNamespace(
        decision_inbox_enabled=True,
        telegram_notifications_enabled=True,
        telegram_notifications_dry_run=True,
    )
    config = SimpleNamespace(analysis=SimpleNamespace(options_decision_system=settings))

    class FakeRepository:
        def __init__(self, _runtime: object) -> None:
            pass

        def sync_current_decisions(self, rows: list[object]) -> dict[str, int]:
            calls.append("decisions")
            assert rows == []
            return {"newly_actionable": 0}

        def sync_ticker_paper_lifecycle(self) -> dict[str, int]:
            calls.append("paper_lifecycle")
            return _zero_paper_transitions()

        def sync_current_portfolio_risk(
            self, cards: list[object] | None, summary: dict[str, object] | None,
        ) -> dict[str, int]:
            calls.append("portfolio_risk")
            assert cards == []
            assert summary is None
            return _zero_portfolio_risk()

        def deliver_outbox(
            self, *, sender: object, dry_run: bool,
        ) -> dict[str, int]:
            calls.append("delivery")
            assert sender is None
            assert dry_run is True
            return {"sent": 0, "failed": 0, "dry_run": 0}

    monkeypatch.setattr(decision_inbox_job, "load_config", lambda _path: config)
    monkeypatch.setattr(
        decision_inbox_job,
        "load_postgres_tables",
        lambda _config, table_names, **_kwargs: (
            {name: [] for name in table_names}, {"unavailable_models": []}
        ),
    )
    monkeypatch.setattr(decision_inbox_job, "runtime_for_config", lambda _config: object())
    monkeypatch.setattr(decision_inbox_job, "DecisionInboxRepository", FakeRepository)

    result = decision_inbox_job.run()

    assert calls == ["decisions", "paper_lifecycle", "portfolio_risk", "delivery"]
    assert result["portfolio_risk"] == _zero_portfolio_risk()
    assert result["delivery"] == {"sent": 0, "failed": 0, "dry_run": 0}


def test_current_portfolio_risk_notifies_once_and_tracks_recurrence(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    inbox = DecisionInboxRepository(runtime)
    reference = datetime(2026, 8, 12, 15, 30, tzinfo=UTC)
    summary = _risk_summary(reference)
    card = _risk_card()
    try:
        assert inbox.sync_current_portfolio_risk([card], summary, now=reference) == {"breached": 1, "resolved": 0}
        assert inbox.sync_current_portfolio_risk([card], summary, now=reference + timedelta(minutes=1)) == _zero_portfolio_risk()
        assert inbox.sync_current_portfolio_risk([_risk_card(severity="watch")], summary, now=reference + timedelta(minutes=2)) == {"breached": 0, "resolved": 1}
        assert inbox.sync_current_portfolio_risk([card], summary, now=reference + timedelta(minutes=3)) == {"breached": 1, "resolved": 0}

        with runtime.read() as connection:
            rows = connection.execute(
                """
                SELECT payload, status
                FROM app.decision_inbox_item
                WHERE event_type = 'portfolio_critical'
                ORDER BY created_at, id
                """
            ).fetchall()
            outbox_count = connection.execute(
                "SELECT count(*) AS count FROM app.notification_outbox"
            ).fetchone()["count"]
        assert len(rows) == outbox_count == 2
        assert [row["status"] for row in rows] == ["resolved", "active"]
        assert rows[0]["payload"]["breach_sequence"] == 1
        assert rows[1]["payload"]["breach_sequence"] == 2
        assert set(rows[1]["payload"]) == {
            "card_id", "risk_type", "severity", "title", "summary", "symbols",
            "impact", "next_action", "as_of", "available_at", "breach_sequence",
            "state_transition", "governance_transition", "detail_url",
        }
        assert rows[1]["payload"]["symbols"] == ["TSLA"]
        message = telegram_message(rows[1]["payload"])
        assert "PORTFOLIO RISK · CRITICAL" in message
        assert "TSLA dominates the portfolio" in message
        assert "Symbols: TSLA" in message
        assert "Impact: 72.0% of current value" in message
        assert "Next: Review the maximum intended weight." in message
        assert message.endswith("/portfolio")
        assert "BUY" not in message and "SELL" not in message and "ORDER" not in message
    finally:
        runtime.close()


def test_current_portfolio_risk_requires_critical_canonical_cards_and_resolves_absence(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    inbox = DecisionInboxRepository(runtime)
    reference = datetime(2026, 8, 12, 15, 30, tzinfo=UTC)
    summary = _risk_summary(reference)
    try:
        for advisory in (
            _risk_card(severity="info"),
            _risk_card(severity="watch"),
            _risk_card("stale-owned-quotes", severity="info"),
            _risk_card("stale-owned-quotes", severity="watch"),
            _risk_card("stale-owned-quotes"),
        ):
            assert inbox.sync_current_portfolio_risk([advisory], summary, now=reference) == _zero_portfolio_risk()

        cards = [
            _risk_card(),
            _risk_card("portfolio-drawdown"),
        ]
        correlation_card = _risk_card("correlation:TSLA:QQQ")
        correlation_card["symbols"] = ["TSLA", "QQQ"]
        cards.append(correlation_card)
        assert inbox.sync_current_portfolio_risk(cards, summary, now=reference) == {"breached": 3, "resolved": 0}
        assert inbox.sync_current_portfolio_risk(cards[:2], summary, now=reference) == {"breached": 0, "resolved": 1}
        assert inbox.sync_current_portfolio_risk([], summary, now=reference) == {"breached": 0, "resolved": 2}

        with runtime.read() as connection:
            rows = connection.execute(
                "SELECT status FROM app.decision_inbox_item WHERE event_type = 'portfolio_critical'"
            ).fetchall()
            outbox_count = connection.execute(
                "SELECT count(*) AS count FROM app.notification_outbox"
            ).fetchone()["count"]
        assert len(rows) == outbox_count == 3
        assert {row["status"] for row in rows} == {"resolved"}
    finally:
        runtime.close()


def test_current_portfolio_risk_rejects_ambiguous_input_and_rolls_back(
    migrated_postgres_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    inbox = DecisionInboxRepository(runtime)
    reference = datetime(2026, 8, 12, 15, 30, tzinfo=UTC)
    summary = _risk_summary(reference)
    card = _risk_card()
    try:
        assert inbox.sync_current_portfolio_risk([card], summary, now=reference) == {"breached": 1, "resolved": 0}

        malformed = _risk_card()
        malformed.pop("title")
        duplicate = [_risk_card(), _risk_card()]
        nonfinite = _risk_card()
        nonfinite["score"] = float("nan")
        future = _risk_card()
        future["updated_at"] = reference + timedelta(minutes=1)
        future_summary = _risk_summary(reference)
        future_summary["available_at"] = reference + timedelta(minutes=1)
        unknown = _risk_card("provider-failure")
        malformed_correlation = _risk_card("correlation:TSLA")
        repeated_correlation = _risk_card("correlation:TSLA:TSLA")
        whitespace_correlation = _risk_card("correlation:TSLA: QQQ")
        whitespace_correlation["symbols"] = ["TSLA", "QQQ"]
        mismatched_correlation = _risk_card("correlation:TSLA:QQQ")
        mismatched_correlation["symbols"] = ["TSLA", "SPY"]
        mismatched = _risk_card()
        mismatched["risk_type"] = "drawdown"
        naive_summary = _risk_summary(reference)
        naive_summary["as_of"] = reference.replace(tzinfo=None).isoformat()
        reversed_summary = _risk_summary(reference)
        reversed_summary["available_at"] = reference - timedelta(minutes=1)
        naive_card = _risk_card()
        naive_card["updated_at"] = reference.replace(tzinfo=None).isoformat()
        reversed_card = _risk_card()
        reversed_card["as_of"] = reference.isoformat()
        reversed_card["available_at"] = (reference - timedelta(minutes=1)).isoformat()
        for invalid_cards, invalid_summary in (
            ([malformed], summary), (duplicate, summary), (nonfinite, summary), ([future], summary),
            ([card], future_summary), ([unknown], summary), ([malformed_correlation], summary),
            ([repeated_correlation], summary), ([whitespace_correlation], summary),
            ([mismatched_correlation], summary), ([mismatched], summary),
            ([card], naive_summary), ([card], reversed_summary), ([naive_card], summary),
            ([reversed_card], summary),
        ):
            assert inbox.sync_current_portfolio_risk(
                invalid_cards, invalid_summary, now=reference,
            ) == _zero_portfolio_risk()

        replacement = _risk_card("portfolio-drawdown")
        def fail_resolution(*_args: Any, **_kwargs: Any) -> int:
            raise RuntimeError("risk resolution failed")

        monkeypatch.setattr(inbox, "_resolve_portfolio_risk_items", fail_resolution)
        with pytest.raises(RuntimeError, match="risk resolution failed"):
            inbox.sync_current_portfolio_risk([replacement], summary, now=reference)

        with runtime.read() as connection:
            rows = connection.execute(
                "SELECT payload, status FROM app.decision_inbox_item WHERE event_type = 'portfolio_critical'"
            ).fetchall()
            outbox_count = connection.execute(
                "SELECT count(*) AS count FROM app.notification_outbox"
            ).fetchone()["count"]
        assert len(rows) == outbox_count == 1
        assert rows[0]["payload"]["card_id"] == "largest-position"
        assert rows[0]["status"] == "active"
    finally:
        runtime.close()


def test_current_portfolio_risk_advisory_lock_dedupes_overlapping_runs(
    migrated_postgres_dsn: str,
) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    inbox = DecisionInboxRepository(runtime)
    reference = datetime(2026, 8, 12, 15, 30, tzinfo=UTC)
    card = _risk_card("portfolio-drawdown")
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    DecisionInboxRepository(runtime).sync_current_portfolio_risk,
                    [card], _risk_summary(reference), now=reference,
                )
                for _ in range(2)
            ]
            results = [future.result(timeout=10) for future in futures]
        assert sum(result["breached"] for result in results) == 1
        with runtime.read() as connection:
            assert connection.execute(
                "SELECT count(*) AS count FROM app.decision_inbox_item WHERE event_type = 'portfolio_critical'"
            ).fetchone()["count"] == 1
            assert connection.execute(
                "SELECT count(*) AS count FROM app.decision_inbox_item WHERE event_type = 'portfolio_critical' AND status = 'active'"
            ).fetchone()["count"] == 1
    finally:
        runtime.close()


def test_ticker_paper_lifecycle_projection_is_bounded_and_ordered() -> None:
    reference = datetime(2026, 8, 12, 15, 30, tzinfo=UTC)
    plan = SimpleNamespace(
        ticker="TSLA", trade_plan_id="trade-plan:tsla", publication_id="publication:tsla",
        opportunity_episode_id="episode-tsla", decision_revision="revision-tsla",
        policy_version="risk-policy.v2:tsla",
        cutoff=reference,
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
