from __future__ import annotations

import json
from pathlib import Path

from investment_panel.jobs import premarket_options_intelligence


def test_premarket_options_intelligence_runs_agents_once_then_recomputes(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
database:
  duckdb_path: {tmp_path / "investment.duckdb"}
nas:
  status_dir: {tmp_path / "status"}
""",
        encoding="utf-8",
    )
    calls: list[str] = []

    def fake_refresh(config_path_arg, **kwargs):
        calls.append("refresh_before_agents")
        return {"job": "refresh_options_radar", "config_path": config_path_arg, **kwargs}

    def fake_agents(config_path_arg, **kwargs):
        calls.append("run_option_agents")
        return {"job": "run_option_agents", "config_path": config_path_arg, **kwargs}

    def fake_deterministic(config_path_arg, **kwargs):
        calls.append("refresh_after_agents")
        return {"job": "refresh_options_radar", "agent_work": "skipped", "config_path": config_path_arg, **kwargs}

    monkeypatch.setattr(premarket_options_intelligence.refresh_options_radar, "run", fake_refresh)
    monkeypatch.setattr(premarket_options_intelligence.run_option_agents, "run", fake_agents)
    monkeypatch.setattr(premarket_options_intelligence.refresh_options_radar, "run_deterministic_only", fake_deterministic)
    # The job now skips when the live app owns the DB; force the not-serving path so
    # the agent pipeline runs deterministically regardless of any local app on :8000.
    monkeypatch.setattr(premarket_options_intelligence, "app_is_serving_database", lambda _db_path: False)

    result = premarket_options_intelligence.run(str(config_path), strategy_version="test_strategy")

    assert calls == ["refresh_before_agents", "run_option_agents", "refresh_after_agents"]
    assert result["cadence"] == "daily_premarket"
    assert result["strategy_version"] == "test_strategy"
    assert result["after_agents"]["agent_work"] == "skipped"
    status = json.loads((tmp_path / "status" / "mini-market-premarket-options-intelligence.json").read_text(encoding="utf-8"))
    assert status["job"] == "premarket_options_intelligence"
    assert status["agent_workers"] == "enabled_once_per_day"
