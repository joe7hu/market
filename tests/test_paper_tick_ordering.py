"""Paper management stays independent of expensive candidate research."""
from types import SimpleNamespace

import pytest

from investment_panel.jobs import options_paper_execution as job


def setup_tick(monkeypatch, *, enabled=True, observation_error=False, execution_error=False):
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

    def observe(_runtime, **kwargs):
        calls.append(("observe", kwargs))
        if observation_error:
            raise RuntimeError("observation failed")
        return {"closed": 1}

    monkeypatch.setattr(job, "OptionsPaperExecutionRepository", lambda _runtime: SimpleNamespace(process=process))
    monkeypatch.setattr(job, "advance_experiment_shadows", observe)
    monkeypatch.setattr(job, "run_experiments", lambda *_args, **_kwargs: pytest.fail("research in fast tick"))
    return calls


def test_order_management_precedes_bounded_observations(monkeypatch):
    calls = setup_tick(monkeypatch)
    result = job.run()
    assert [name for name, _ in calls] == ["process", "observe"]
    assert calls[0][1]["enabled_lanes"] == ["radar"]
    assert result["managed"] == [{"paper_order_id": "existing"}]
    assert result["observations"] == {"closed": 1}
    assert result["research_job"] == "run_option_paper_experiments"
    assert result["paper_only"] is True
    assert result["live_brokerage_submission"] is False


def test_observation_failure_does_not_skip_order_processing(monkeypatch):
    calls = setup_tick(monkeypatch, observation_error=True)
    result = job.run()
    assert [name for name, _ in calls] == ["process", "observe"]
    assert result["status"] == "partial"
    assert result["managed"] == [{"paper_order_id": "existing"}]
    assert result["observations"]["error"] == "observation failed"


def test_entry_switch_off_still_manages_existing_book(monkeypatch):
    calls = setup_tick(monkeypatch, enabled=False)
    result = job.run()
    assert calls[0][1]["enabled_lanes"] == []
    assert result["managed"]


def test_execution_failure_does_not_start_more_observation_work(monkeypatch):
    calls = setup_tick(monkeypatch, execution_error=True)
    with pytest.raises(RuntimeError, match="execution failed"):
        job.run()
    assert [name for name, _ in calls] == ["process"]


def test_research_has_a_separate_configured_entrypoint(monkeypatch):
    config, runtime = object(), object()
    monkeypatch.setattr(job, "load_config", lambda path: config if path == "selected.yaml" else None)
    monkeypatch.setattr(job, "runtime_for_config", lambda value: runtime if value is config else None)
    def research(active_runtime, active_config):
        assert active_runtime is runtime and active_config is config
        return {"status": "ok", "candidate_revision_id": 123}
    monkeypatch.setattr(job, "run_experiments", research)
    result = job.run_research("selected.yaml")
    assert result["candidate_revision_id"] == 123
    assert result["live_brokerage_submission"] is False
