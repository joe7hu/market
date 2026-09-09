from __future__ import annotations

from investment_panel.workflows.portfolio import PortfolioActions
from conftest import typed_config


def test_portfolio_action_owns_transaction_response_reload(monkeypatch) -> None:
    stored = []

    def record(_config, transaction):
        stored.append(transaction)
        return {"id": "tx-1", **transaction}

    from investment_panel.workflows import portfolio

    monkeypatch.setattr(portfolio, "record_transaction_owner", record)
    monkeypatch.setattr(portfolio, "portfolio_rows_owner", lambda _config: [{"symbol": "NVDA"}])
    actions = PortfolioActions(typed_config())

    result = actions.record_transaction({"symbol": "NVDA", "transaction_type": "buy"})

    assert stored == [{"symbol": "NVDA", "transaction_type": "buy"}]
    assert result["transaction"]["id"] == "tx-1"
    assert result["portfolio"] == {"rows": [{"symbol": "NVDA"}], "count": 1}


def test_background_dispatch_only_schedules_new_jobs():
    from fastapi import BackgroundTasks
    from investment_panel.api import job_control

    tasks = BackgroundTasks()
    job_control.schedule_created_job(tasks, {"created": False, "id": "existing"}, "refresh_decision_models", "postgresql:///fixture")
    assert not tasks.tasks
    job_control.schedule_created_job(tasks, {"created": True, "id": "new"}, "refresh_decision_models", "postgresql:///fixture")
    assert len(tasks.tasks) == 1
    assert tasks.tasks[0].func is job_control.execute_background_refresh_job
    assert tasks.tasks[0].args == ("new", "refresh_decision_models", "postgresql:///fixture")


def test_option_history_health_preserves_configured_mode(monkeypatch):
    from types import SimpleNamespace
    from investment_panel.api.routers.options import historical_option_health

    config = typed_config(raw={"analysis": {"options_decision_system": {"mode": "paper"}}})
    history = SimpleNamespace(health=lambda **filters: filters)
    assert historical_option_health(symbol="QQQ", actions=history, config=config) == {"symbol": "QQQ", "mode": "paper"}
    from investment_panel.api.routers import panel
    monkeypatch.setattr(panel.loaders, "load_panel_data", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(panel.payloads, "status_payload", lambda _data: {})
    assert panel.status(config=config, actions=history)["options_history"] == {"mode": "paper"}
