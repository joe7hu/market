from __future__ import annotations

from investment_panel.jobs import premarket_options_intelligence


def test_premarket_intelligence_uses_postgresql_today_composition(monkeypatch) -> None:
    monkeypatch.setattr(
        premarket_options_intelligence.postgres_refresh,
        "premarket",
        lambda path: {"status": "ok", "database": "postgresql:///market", "today": {"daily_brief": 3}},
    )
    result = premarket_options_intelligence.run("config.yaml", strategy_version="v-test")
    assert result["strategy_version"] == "v-test"
    assert result["today"]["daily_brief"] == 3
