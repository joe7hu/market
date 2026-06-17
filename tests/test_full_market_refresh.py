from __future__ import annotations

import json
from pathlib import Path

import pytest

from investment_panel.jobs import full_market_refresh


def write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
database:
  duckdb_path: {tmp_path / "investment.duckdb"}
nas:
  status_dir: {tmp_path / "status"}
  duckdb_snapshot_dir: {tmp_path / "snapshots"}
""",
        encoding="utf-8",
    )
    return config_path


def test_full_market_refresh_runs_existing_jobs_in_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    config_path = write_config(tmp_path)

    def fake_run(name: str):
        def _run(*args, **kwargs):
            calls.append(name)
            return {"job": name, "args": [str(arg) for arg in args], "kwargs": kwargs}

        return _run

    monkeypatch.setattr(full_market_refresh.update_arco_data, "run", fake_run("arco_import"))
    monkeypatch.setattr(full_market_refresh.daily_screen, "run", fake_run("daily_screen"))
    monkeypatch.setattr(full_market_refresh.update_free_sources, "run", fake_run("free_sources_and_analyses"))
    monkeypatch.setattr(full_market_refresh.refresh_options_radar, "run", fake_run("options_radar"))
    monkeypatch.setattr(full_market_refresh.run_option_agents, "run", fake_run("option_agents"))
    monkeypatch.setattr(full_market_refresh.update_broker_sources, "run", fake_run("broker_sources"))
    monkeypatch.setattr(full_market_refresh.update_disclosures, "run", fake_run("disclosures"))
    monkeypatch.setattr(full_market_refresh.update_event_calendar, "run", fake_run("event_calendar"))
    monkeypatch.setattr(full_market_refresh, "prune_operational_tables", lambda *_args, **_kwargs: calls.append("retention_prune") or {"provider_runs": 0, "source_runs": 0, "refresh_jobs": 0})
    monkeypatch.setattr(full_market_refresh.snapshot_database, "run", fake_run("database_snapshot"))

    result = full_market_refresh.run(str(config_path), online_check=True, max_filings=2, fetch_holdings=False)

    assert calls == [
        "arco_import",
        "daily_screen",
        "free_sources_and_analyses",
        "options_radar",
        "option_agents",
        "broker_sources",
        "disclosures",
        "event_calendar",
        "retention_prune",
        "database_snapshot",
    ]
    assert result["ok"] is True
    assert [step["name"] for step in result["steps"]] == calls
    assert all(step["ok"] for step in result["steps"])
    free_sources = next(step for step in result["steps"] if step["name"] == "free_sources_and_analyses")
    assert free_sources["result"]["kwargs"]["equity_data"] is True
    assert free_sources["result"]["kwargs"]["analyses"] is True
    status = json.loads(Path(result["status_path"]).read_text(encoding="utf-8"))
    assert status["job"] == "full_market_refresh"
    assert status["ok"] is True


def test_housekeeping_failure_keeps_data_fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = write_config(tmp_path)

    def ok_run(name: str):
        return lambda *_args, **_kwargs: {"job": name}

    monkeypatch.setattr(full_market_refresh.update_arco_data, "run", ok_run("arco_import"))
    monkeypatch.setattr(full_market_refresh.daily_screen, "run", ok_run("daily_screen"))
    monkeypatch.setattr(full_market_refresh.update_free_sources, "run", ok_run("free_sources_and_analyses"))
    monkeypatch.setattr(full_market_refresh.refresh_options_radar, "run", ok_run("options_radar"))
    monkeypatch.setattr(full_market_refresh.run_option_agents, "run", ok_run("option_agents"))
    monkeypatch.setattr(full_market_refresh.update_broker_sources, "run", ok_run("broker_sources"))
    monkeypatch.setattr(full_market_refresh.update_disclosures, "run", ok_run("disclosures"))
    monkeypatch.setattr(full_market_refresh.update_event_calendar, "run", ok_run("event_calendar"))
    monkeypatch.setattr(full_market_refresh, "prune_operational_tables", lambda *_args, **_kwargs: {"refresh_jobs": 0})

    def fail_snapshot(*_args, **_kwargs):
        raise RuntimeError("nas offline")

    monkeypatch.setattr(full_market_refresh.snapshot_database, "run", fail_snapshot)

    # Production runs the daily refresh with continue_on_error so the housekeeping
    # tail can fail without aborting; the overall run is failed but data is fresh.
    result = full_market_refresh.run(str(config_path), continue_on_error=True)

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["failedStep"] == "database_snapshot"
    assert result["dataOk"] is True
    assert result["dataFinishedAt"] == result["finishedAt"]
    snapshot_step = next(step for step in result["steps"] if step["name"] == "database_snapshot")
    assert snapshot_step["category"] == "housekeeping"
    assert snapshot_step["ok"] is False


def test_data_step_failure_marks_data_not_fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = write_config(tmp_path)

    def ok_run(name: str):
        return lambda *_args, **_kwargs: {"job": name}

    monkeypatch.setattr(full_market_refresh.update_arco_data, "run", ok_run("arco_import"))
    monkeypatch.setattr(full_market_refresh.update_free_sources, "run", ok_run("free_sources_and_analyses"))
    monkeypatch.setattr(full_market_refresh.refresh_options_radar, "run", ok_run("options_radar"))
    monkeypatch.setattr(full_market_refresh.run_option_agents, "run", ok_run("option_agents"))
    monkeypatch.setattr(full_market_refresh.update_broker_sources, "run", ok_run("broker_sources"))
    monkeypatch.setattr(full_market_refresh.update_disclosures, "run", ok_run("disclosures"))
    monkeypatch.setattr(full_market_refresh.update_event_calendar, "run", ok_run("event_calendar"))
    monkeypatch.setattr(full_market_refresh, "prune_operational_tables", lambda *_args, **_kwargs: {"refresh_jobs": 0})
    monkeypatch.setattr(full_market_refresh.snapshot_database, "run", ok_run("database_snapshot"))

    def fail_daily(*_args, **_kwargs):
        raise RuntimeError("screen failed")

    monkeypatch.setattr(full_market_refresh.daily_screen, "run", fail_daily)

    result = full_market_refresh.run(str(config_path), continue_on_error=True)

    assert result["dataOk"] is False
    assert result["dataFinishedAt"] is None


def test_full_market_refresh_records_failed_step_before_reraising(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = write_config(tmp_path)

    monkeypatch.setattr(full_market_refresh.update_arco_data, "run", lambda *_args, **_kwargs: {"job": "arco_import"})

    def fail_daily(*_args, **_kwargs):
        raise RuntimeError("screen failed")

    monkeypatch.setattr(full_market_refresh.daily_screen, "run", fail_daily)

    with pytest.raises(RuntimeError, match="screen failed"):
        full_market_refresh.run(str(config_path))

    status = json.loads((tmp_path / "status" / "mini-market-full-refresh.json").read_text(encoding="utf-8"))
    assert status["ok"] is False
    assert status["status"] == "failed"
    assert status["failedStep"] == "daily_screen"
    assert [step["ok"] for step in status["steps"]] == [True, False]
