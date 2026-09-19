"""The funded-book manager must precede optional experiment research."""
from types import SimpleNamespace

import pytest

from investment_panel.jobs import options_paper_execution as job


def setup_tick(monkeypatch, *, enabled=True, research_error=False, execution_error=False):
    calls = []
    settings = SimpleNamespace(options_paper_actions_enabled=enabled,
        radar_paper_actions_enabled=True, qqq_paper_actions_enabled=False,
        options_risk_sleeve_capital=100000, daily_loss_halt_pct=0.02,
        max_recovery_open_positions=5, decision_inbox_enabled=False)
    config = SimpleNamespace(analysis=SimpleNamespace(options_decision_system=settings))
    monkeypatch.setattr(job, "load_config", lambda _path: config)
    runtime = object()
    monkeypatch.setattr(job, "runtime_for_config", lambda _config: runtime)

    def process(**kwargs):
        calls.append(("process", kwargs))
        if execution_error:
            raise RuntimeError("execution failed")
        return {"status": "ok", "managed": [{"paper_order_id": "existing"}], "staged": []}

    def research(_runtime, _config):
        calls.append(("research", {}))
        if research_error:
            raise RuntimeError("research failed")
        return {"status": "ok"}

    monkeypatch.setattr(job, "OptionsPaperExecutionRepository", lambda _runtime: SimpleNamespace(process=process))
    monkeypatch.setattr(job, "run_experiments", research)
    return calls


def test_order_management_precedes_research(monkeypatch):
    calls = setup_tick(monkeypatch)
    result = job.run()
    assert [name for name, _ in calls] == ["process", "research"]
    assert calls[0][1]["enabled_lanes"] == ["radar"]
    assert result["managed"] == [{"paper_order_id": "existing"}]
    assert result["paper_only"] is True
    assert result["live_brokerage_submission"] is False


def test_research_failure_does_not_skip_order_processing(monkeypatch):
    calls = setup_tick(monkeypatch, research_error=True)
    result = job.run()
    assert [name for name, _ in calls] == ["process", "research"]
    assert result["status"] == "partial"
    assert result["managed"] == [{"paper_order_id": "existing"}]
    assert result["experiments"]["error"] == "research failed"


def test_entry_switch_off_still_manages_existing_book(monkeypatch):
    calls = setup_tick(monkeypatch, enabled=False)
    result = job.run()
    assert calls[0][0] == "process"
    assert calls[0][1]["enabled_lanes"] == []
    assert result["managed"]


def test_execution_failure_does_not_start_more_experiment_work(monkeypatch):
    calls = setup_tick(monkeypatch, execution_error=True)
    with pytest.raises(RuntimeError, match="execution failed"):
        job.run()
    assert [name for name, _ in calls] == ["process"]
