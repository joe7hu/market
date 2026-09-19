"""Durable claims, actual-check metadata and entry gates under time/error budgets."""
from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace
import pytest
from investment_panel.infrastructure.postgres import options_paper_execution as owner


def repository(monkeypatch, *, fail=None, times=None):
    statements, checked = [], []
    class Connection:
        def execute(self, sql, params=None):
            statements.append((sql, params))
            return SimpleNamespace(fetchall=lambda: [{"id": str(i)} for i in range(3)])
    repo = owner.OptionsPaperExecutionRepository(SimpleNamespace(transaction=lambda *_: nullcontext(Connection())))
    def manage(order_id, now):
        checked.append(order_id)
        if order_id == fail:
            raise RuntimeError("broken order")
        return {"paper_order_id": order_id, "status": "entered"}
    monkeypatch.setattr(repo, "_manage_one", manage)
    if times is not None:
        values = iter(times)
        monkeypatch.setattr(owner.time, "monotonic", lambda: next(values))
    return repo, statements, checked


def test_one_bad_order_does_not_skip_other_claimed_positions(monkeypatch):
    repo, sql, checked = repository(monkeypatch, fail="0")
    results = repo.manage_orders(lanes=["radar"], decision_inbox_enabled=False,
        now=datetime.now(UTC), limit=3)
    assert checked == ["0", "1", "2"]
    assert results[0]["status"] == "failed" and results[1]["status"] == "entered"
    assert "SKIP LOCKED" in sql[0][0] and "last_claimed_at" in sql[0][0]
    assert "ORDER BY last_checked_at NULLS FIRST, last_claimed_at NULLS FIRST" in sql[0][0]
    claim_update = sql[0][0].split("SET execution_quote", 1)[1].split("FROM due", 1)[0]
    assert "last_checked_at" not in claim_update
    assert "updated_at" not in sql[0][0]


def test_budget_does_not_claim_unprocessed_orders_were_checked(monkeypatch):
    repo, statements, checked = repository(monkeypatch, times=[0, 0, 9])
    results = repo.manage_orders(lanes=["radar"], decision_inbox_enabled=False,
        now=datetime.now(UTC), limit=3, max_work_seconds=8)
    assert checked == ["0"]
    assert results[-1] == {"status": "deferred", "reason": "management_budget_exhausted", "remaining_claims": 2}
    writes = [params for sql, params in statements if sql.startswith("UPDATE app.paper_order")]
    assert len(writes) == 1 and writes[0][-1] == "0"
    assert "last_checked_at" in writes[0][0].obj


@pytest.mark.parametrize("outcome", [{"status": "failed"}, {"status": "deferred", "reason": "management_budget_exhausted"}])
def test_incomplete_management_blocks_new_risk(monkeypatch, outcome):
    repo, _, _ = repository(monkeypatch)
    monkeypatch.setattr(repo, "manage_orders", lambda **kw: [outcome])
    monkeypatch.setattr(repo, "stage_current_ready", lambda **kw: pytest.fail("admitted new risk"))
    result = repo.process(enabled_lanes=["radar"], sleeve_capital=100000,
        daily_loss_halt_pct=.02, max_open_positions=5, decision_inbox_enabled=False)
    assert result["status"] == "partial" and result["entry_staging"] == "blocked"
