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
    monkeypatch.setattr(full_market_refresh.update_broker_sources, "run", fake_run("broker_sources"))
    monkeypatch.setattr(full_market_refresh.update_disclosures, "run", fake_run("disclosures"))
    monkeypatch.setattr(full_market_refresh.update_event_calendar, "run", fake_run("event_calendar"))
    monkeypatch.setattr(full_market_refresh.snapshot_database, "run", fake_run("database_snapshot"))

    result = full_market_refresh.run(str(config_path), online_check=True, max_filings=2, fetch_holdings=False)

    assert calls == [
        "arco_import",
        "daily_screen",
        "free_sources_and_analyses",
        "broker_sources",
        "disclosures",
        "event_calendar",
        "database_snapshot",
    ]
    assert result["ok"] is True
    assert [step["name"] for step in result["steps"]] == calls
    assert all(step["ok"] for step in result["steps"])
    status = json.loads(Path(result["status_path"]).read_text(encoding="utf-8"))
    assert status["job"] == "full_market_refresh"
    assert status["ok"] is True


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
