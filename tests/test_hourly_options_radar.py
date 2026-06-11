from __future__ import annotations

import json
from pathlib import Path

from investment_panel.jobs import hourly_options_radar


def test_hourly_options_radar_recomputes_radar_without_provider_refresh_or_agent_work(tmp_path: Path, monkeypatch) -> None:
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
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_refresh_options_radar(config_path_arg, **kwargs):
        calls.append(("radar", kwargs))
        return {"job": "refresh_options_radar", "config_path": config_path_arg, "agent_work": "skipped", **kwargs}

    monkeypatch.setattr(hourly_options_radar.refresh_options_radar, "run_signal_only", fake_refresh_options_radar)
    monkeypatch.setattr(hourly_options_radar, "app_is_serving_database", lambda _db_path: False)

    result = hourly_options_radar.run(str(config_path), symbols=["TSLA"], lock_path=tmp_path / "hourly.lock")

    assert [call[0] for call in calls] == ["radar"]
    assert calls[0][1] == {"symbols": ["TSLA"]}
    assert result["cadence"] == "hourly_deterministic"
    assert result["agent_workers"] == "daily_premarket_only"
    assert result["source_refresh"] == "skipped_hourly_to_avoid_app_db_lock"
    status = json.loads((tmp_path / "status" / "mini-market-hourly-options-radar.json").read_text(encoding="utf-8"))
    assert status["job"] == "hourly_options_radar"
    assert status["options_radar"]["agent_work"] == "skipped"
    assert status["source_refresh"] == "skipped_hourly_to_avoid_app_db_lock"


def test_hourly_options_radar_skips_when_previous_run_is_locked(tmp_path: Path, monkeypatch) -> None:
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
    monkeypatch.setattr(hourly_options_radar.refresh_options_radar, "run_signal_only", lambda *_args, **_kwargs: calls.append("radar") or {})
    monkeypatch.setattr(hourly_options_radar, "app_is_serving_database", lambda _db_path: False)

    lock_path = tmp_path / "hourly.lock"
    with hourly_options_radar.hourly_lock(lock_path) as acquired:
        assert acquired is True
        result = hourly_options_radar.run(str(config_path), lock_path=lock_path)

    assert result["status"] == "skipped_running"
    assert result["agent_workers"] == "daily_premarket_only"
    assert calls == []
    status = json.loads((tmp_path / "status" / "mini-market-hourly-options-radar.json").read_text(encoding="utf-8"))
    assert status["status"] == "skipped_running"


def test_hourly_options_radar_skips_when_app_is_serving_same_database(tmp_path: Path, monkeypatch) -> None:
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
    monkeypatch.setattr(hourly_options_radar.refresh_options_radar, "run_signal_only", lambda *_args, **_kwargs: calls.append("radar") or {})
    monkeypatch.setattr(hourly_options_radar, "app_is_serving_database", lambda _db_path: True)

    result = hourly_options_radar.run(str(config_path), lock_path=tmp_path / "hourly.lock")

    assert result["status"] == "skipped_app_active"
    assert result["source_refresh"] == "skipped_hourly_to_keep_app_responsive"
    assert calls == []
    status = json.loads((tmp_path / "status" / "mini-market-hourly-options-radar.json").read_text(encoding="utf-8"))
    assert status["status"] == "skipped_app_active"


def test_hourly_app_probe_timeout_means_app_is_active(monkeypatch) -> None:
    def timeout(*_args, **_kwargs):
        raise TimeoutError("slow app response")

    monkeypatch.setattr(hourly_options_radar, "urlopen", timeout)

    assert hourly_options_radar.app_is_serving_database("/tmp/investment.duckdb") is True
