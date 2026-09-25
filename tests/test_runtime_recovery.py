"""Regression failures listed in docs/runtime-recovery-20260925.md."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from investment_panel.infrastructure import scheduler
from investment_panel.jobs import refresh_symbol_decision_outcomes as outcome_job


def test_critical_dispatch_runs_while_long_priority_work_waits(monkeypatch):
    async def exercise():
        monkeypatch.setattr(scheduler, "_scheduler_semaphore", asyncio.Semaphore(2))
        monkeypatch.setattr(scheduler, "_slow_job_semaphore", asyncio.Semaphore(1))
        monkeypatch.setattr(scheduler, "_active_jobs", {})
        monkeypatch.setattr(scheduler, "_deferred_jobs", 0)
        started = {name: asyncio.Event() for name in (
            "refresh_symbol_decision_outcomes", "update_market_data", "process_options_paper_orders",
        )}
        release = asyncio.Event()
        peak = 0

        async def execute(job, *_args, **_kwargs):
            nonlocal peak
            peak = max(peak, scheduler.scheduler_runtime_health()["active_count"])
            started[job].set()
            if job != "process_options_paper_orders":
                await release.wait()
            return {"status": "succeeded"}

        monkeypatch.setattr(scheduler, "_dispatch_once", execute)
        tasks = []
        try:
            tasks.append(asyncio.create_task(scheduler._dispatch("refresh_symbol_decision_outcomes", "db", "config")))
            await asyncio.wait_for(started["refresh_symbol_decision_outcomes"].wait(), 1)
            tasks.append(asyncio.create_task(scheduler._dispatch("update_market_data", "db", "config")))
            await asyncio.sleep(0)
            tasks.append(asyncio.create_task(scheduler._dispatch("process_options_paper_orders", "db", "config")))
            await asyncio.wait_for(started["process_options_paper_orders"].wait(), 1)
            assert not started["update_market_data"].is_set()
            queued = scheduler.scheduler_runtime_health()["queued_jobs"]
            assert len(queued) == 1
            assert queued[0]["job_name"] == "update_market_data"
            assert queued[0]["waiting_for"] == "background_capacity"
            assert queued[0]["wait_seconds"] >= 0
            assert peak <= 2
            tasks[1].cancel()
            await asyncio.gather(tasks[1], return_exceptions=True)
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
        assert scheduler.scheduler_runtime_health()["active_count"] == 0
        assert scheduler.scheduler_runtime_health()["deferred_job_count"] == 0
        assert scheduler.scheduler_runtime_health()["queued_jobs"] == []
        assert scheduler._scheduler_semaphore._value == 2
        assert scheduler._slow_job_semaphore._value == 1

    asyncio.run(exercise())


def test_new_resolved_evidence_publishes_despite_other_immature_horizons(monkeypatch):
    published = []
    monkeypatch.setattr(outcome_job, "load_config", lambda _: object())
    monkeypatch.setattr(outcome_job, "runtime_for_config", lambda _: object())
    monkeypatch.setattr(outcome_job, "SymbolDecisionOutcomeRepository", lambda _: SimpleNamespace(
        refresh=lambda: {"status": "ok", "resolved": 0},
    ))
    monkeypatch.setattr(outcome_job, "TickerDecisionRepository", lambda _: SimpleNamespace(
        refresh_outcomes=lambda **_: {"evaluated": 1, "updated": 6, "resolved": 1},
        has_pending_outcome_attributions=lambda: True,
        publish_outcome_attributions=lambda: published.append(True) or {"status": "ok"},
    ))
    result = outcome_job.run()
    assert published == [True]
    assert result["ticker_outcome_attribution"]["status"] == "ok"
