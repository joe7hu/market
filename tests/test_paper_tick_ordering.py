"""Fast funded management, bounded shadows and prospective NAV; no research work."""
from types import SimpleNamespace
import pytest
from investment_panel.jobs import options_paper_execution as job


def setup_tick(monkeypatch, *, enabled=True, observation_error=False, execution_error=False, nav_error=False):
    calls = []
    settings = SimpleNamespace(options_paper_actions_enabled=enabled, radar_paper_actions_enabled=True,
        qqq_paper_actions_enabled=False, options_risk_sleeve_capital=100000,
        daily_loss_halt_pct=.02, max_recovery_open_positions=5, decision_inbox_enabled=False)
    monkeypatch.setattr(job, "load_config", lambda _: SimpleNamespace(analysis=SimpleNamespace(options_decision_system=settings)))
    monkeypatch.setattr(job, "runtime_for_config", lambda _: object())
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
    def nav():
        calls.append(("nav", {}))
        if nav_error:
            raise RuntimeError("nav failed")
        return {"status": "complete"}
    monkeypatch.setattr(job, "OptionsPaperExecutionRepository", lambda _: SimpleNamespace(process=process))
    monkeypatch.setattr(job, "PaperWorkbenchRepository", lambda _: SimpleNamespace(capture_nav=nav))
    monkeypatch.setattr(job, "advance_experiment_shadows", observe)
    monkeypatch.setattr(job, "run_experiments", lambda *a, **kw: pytest.fail("research in fast tick"))
    return calls


def test_management_then_observation_then_nav(monkeypatch):
    calls = setup_tick(monkeypatch)
    result = job.run()
    assert [name for name, _ in calls] == ["process", "observe", "nav"]
    assert calls[0][1]["enabled_lanes"] == ["radar"]
    assert result["observations"] == {"closed": 1}
    assert result["nav_observation"]["status"] == "complete"
    assert result["research_job"] == "run_option_paper_experiments"
    assert result["paper_only"] and not result["live_brokerage_submission"]


@pytest.mark.parametrize("failure", ["observation_error", "nav_error"])
def test_optional_failure_retains_funded_results(monkeypatch, failure):
    setup_tick(monkeypatch, **{failure: True})
    result = job.run()
    assert result["status"] == "partial" and result["managed"]


def test_disabled_entries_still_manage_existing_risk(monkeypatch):
    calls = setup_tick(monkeypatch, enabled=False)
    assert job.run()["managed"]
    assert calls[0][1]["enabled_lanes"] == []


def test_failed_manager_stops_other_work(monkeypatch):
    calls = setup_tick(monkeypatch, execution_error=True)
    with pytest.raises(RuntimeError, match="execution failed"):
        job.run()
    assert [name for name, _ in calls] == ["process"]


def test_research_entrypoint_uses_explicit_config(monkeypatch):
    config, runtime = object(), object()
    monkeypatch.setattr(job, "load_config", lambda path: config if path == "selected.yaml" else None)
    monkeypatch.setattr(job, "runtime_for_config", lambda value: runtime if value is config else None)
    def research(active_runtime, active_config):
        assert active_runtime is runtime and active_config is config
        return {"status": "ok", "candidate_revision_id": 123}
    monkeypatch.setattr(job, "run_experiments", research)
    result = job.run_research("selected.yaml")
    assert result["candidate_revision_id"] == 123 and not result["live_brokerage_submission"]
