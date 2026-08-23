from __future__ import annotations

from pathlib import Path

from investment_panel.core.config import load_config


def test_deployed_config_enables_paper_signal_learning_without_live_brokerage() -> None:
    config = load_config(Path(__file__).parents[1] / "config.yaml")
    settings = config.analysis.options_decision_system

    assert settings.mode == "paper"
    assert settings.ticker_paper_actions_enabled is True
    assert settings.stock_paper_actions_enabled is True
    assert settings.options_paper_actions_enabled is True
    assert settings.radar_paper_actions_enabled is True
    assert settings.qqq_paper_actions_enabled is True
    assert settings.strategy_auto_promotion_enabled is True
    assert settings.telegram_notifications_enabled is True
    assert settings.telegram_notifications_dry_run is False
    assert config.data_sources.brokers.advisory_only is True
    assert config.data_sources.brokers.ibkr.paper_only is True
    assert config.data_sources.brokers.moomoo.paper_only is True
