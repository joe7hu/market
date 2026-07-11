from __future__ import annotations

from investment_panel.jobs import hourly_options_radar


def test_hourly_radar_uses_postgresql_signal_publication(monkeypatch) -> None:
    monkeypatch.setattr(
        hourly_options_radar.refresh_options_radar,
        "run_signal_only",
        lambda path, symbols=None: {"status": "ok", "database": "postgresql:///market", "symbols": symbols},
    )
    result = hourly_options_radar.run("config.yaml", symbols=["NVDA"])
    assert result["status"] == "ok"
    assert result["symbols"] == ["NVDA"]
    assert result["cadence"] == "hourly_deterministic"
