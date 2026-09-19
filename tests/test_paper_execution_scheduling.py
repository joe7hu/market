"""Budget isolation and independent candidate-collection permissions."""
import asyncio
from dataclasses import replace

import pytest

from investment_panel.core import job_policy
from investment_panel.core.options_recovery_config import options_decision_system_config
from investment_panel.infrastructure import scheduler
from investment_panel.infrastructure.postgres.options_paper_execution import OptionsPaperExecutionRepository
from investment_panel.jobs import options_paper_execution as job
from investment_panel.settings import AppConfig


@pytest.mark.parametrize("collection", [False, True])
@pytest.mark.parametrize("promotion", [False, True])
def test_collection_and_promotion_are_independent(monkeypatch, collection, promotion):
    monkeypatch.setenv("MARKET_PAPER_EXPERIMENT_REFRESH_SECONDS", "300")
    config = AppConfig()
    settings = replace(config.analysis.options_decision_system,
        strategy_experiment_collection_enabled=collection,
        strategy_auto_promotion_enabled=promotion)
    config = replace(config, analysis=replace(config.analysis, options_decision_system=settings))
    intervals = job_policy.scheduler_intervals(config)
    assert ("run_option_paper_experiments" in intervals) is collection
    assert "process_options_paper_orders" in intervals
    assert "run_option_paper_experiments" not in scheduler.FAST_DATABASE_JOBS
    assert job_policy.job_timeout_seconds("run_option_paper_experiments") == 900


@pytest.mark.parametrize("value", [False, None, "false", "true", 0, 1])
def test_collection_permission_requires_explicit_boolean_true(value):
    config = options_decision_system_config({"strategy_experiment_collection_enabled": value}, lambda _: "shadow")
    assert config.strategy_experiment_collection_enabled is False


def test_collection_boolean_true():
    assert options_decision_system_config({"strategy_experiment_collection_enabled": True}, lambda _: "shadow").strategy_experiment_collection_enabled


def test_disabled_collection_does_not_disable_existing_observations(monkeypatch):
    config = AppConfig()
    monkeypatch.setattr(job, "advance_experiment_shadows", lambda *_args, **_kwargs: {"closed": 2})
    result = job.run_experiments(object(), config)
    assert result["status"] == "disabled"
    assert result["observations"] == {"closed": 2}


@pytest.mark.parametrize("staging_fails", [False, True])
def test_existing_orders_are_managed_before_staging(monkeypatch, staging_fails):
    repo = OptionsPaperExecutionRepository(object())
    calls = []
    def manage(**kwargs):
        calls.append("manage")
        assert set(kwargs["lanes"]) == {"radar", "qqq"}
        return [{"paper_order_id": "existing", "status": "closed"}]
    def stage(**_kwargs):
        calls.append("stage")
        if staging_fails:
            raise RuntimeError("publication unavailable")
        return [{"paper_order_id": "new"}]
    monkeypatch.setattr(repo, "manage_orders", manage)
    monkeypatch.setattr(repo, "stage_current_ready", stage)
    result = repo.process(enabled_lanes=["radar"], sleeve_capital=100000,
        daily_loss_halt_pct=0.02, max_open_positions=5, decision_inbox_enabled=False)
    assert calls == ["manage", "stage"]
    assert result["managed"][0]["status"] == "closed"
    assert result["status"] == ("partial" if staging_fails else "ok")
    assert bool(result["staging_error"]) is staging_fails


def test_management_failure_blocks_new_risk(monkeypatch):
    repo = OptionsPaperExecutionRepository(object())
    def fail(**_kwargs):
        raise RuntimeError("management unavailable")
    monkeypatch.setattr(repo, "manage_orders", fail)
    monkeypatch.setattr(repo, "stage_current_ready", lambda **_kwargs: pytest.fail("new risk admitted"))
    with pytest.raises(RuntimeError, match="management unavailable"):
        repo.process(enabled_lanes=["radar"], sleeve_capital=100000,
            daily_loss_halt_pct=0.02, max_open_positions=5, decision_inbox_enabled=False)


def test_slow_backlog_does_not_starve_paper_tick(monkeypatch):
    async def scenario():
        total, slow = asyncio.Semaphore(2), asyncio.Semaphore(1)
        monkeypatch.setattr(scheduler, "_scheduler_semaphore", total)
        monkeypatch.setattr(scheduler, "_slow_job_semaphore", slow)
        monkeypatch.setattr(scheduler, "_deferred_jobs", 0)
        monkeypatch.setattr(scheduler, "_active_jobs", {})
        first_started, release_slow, paper_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        running, peak = 0, 0
        async def execute(name, *_args, **_kwargs):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            try:
                if name == "process_options_paper_orders":
                    paper_done.set()
                else:
                    first_started.set()
                    await release_slow.wait()
            finally:
                running -= 1
        monkeypatch.setattr(scheduler, "_dispatch_once", execute)
        tasks = [asyncio.create_task(scheduler._dispatch("slow-1", "db", "config"))]
        try:
            await asyncio.wait_for(first_started.wait(), 1)
            tasks.extend(asyncio.create_task(scheduler._dispatch(f"slow-{n}", "db", "config")) for n in range(2, 6))
            await asyncio.sleep(0)
            tasks.append(asyncio.create_task(scheduler._dispatch("process_options_paper_orders", "db", "config")))
            await asyncio.wait_for(paper_done.wait(), 1)
            assert peak <= 2
            assert not release_slow.is_set()
        finally:
            release_slow.set()
            await asyncio.gather(*tasks)
        assert total._value == 2 and slow._value == 1
        assert scheduler._deferred_jobs == 0
        assert scheduler._active_jobs == {}
    asyncio.run(scenario())


def test_cancelled_waiter_does_not_leak_capacity(monkeypatch):
    async def scenario():
        total, slow = asyncio.Semaphore(2), asyncio.Semaphore(1)
        monkeypatch.setattr(scheduler, "_scheduler_semaphore", total)
        monkeypatch.setattr(scheduler, "_slow_job_semaphore", slow)
        monkeypatch.setattr(scheduler, "_deferred_jobs", 0)
        await slow.acquire()
        task = asyncio.create_task(scheduler._dispatch("research", "db", "config"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert scheduler._deferred_jobs == 0
        assert total._value == 2
        slow.release()
        assert slow._value == 1
    asyncio.run(scenario())
