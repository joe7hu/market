"""Run the deterministic Radar/QQQ paper-only execution loop."""

from __future__ import annotations

import argparse
import json
from typing import Any

from investment_panel.core.config import load_config
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.options_paper_execution import OptionsPaperExecutionRepository


def run(config_path: str | None = "config.yaml") -> dict[str, Any]:
    config = load_config(config_path)
    settings = config.analysis.options_decision_system
    lanes = [
        lane
        for lane, enabled in (
            ("radar", settings.radar_paper_actions_enabled),
            ("qqq", settings.qqq_paper_actions_enabled),
        )
        if settings.options_paper_actions_enabled and enabled
    ]
    repository = OptionsPaperExecutionRepository(runtime_for_config(config))
    # The switches are entry gates only.  ``process`` always manages existing
    # Radar and QQQ orders, including when one or both creation lanes are off.
    result = repository.process(
        enabled_lanes=lanes,
        sleeve_capital=settings.options_risk_sleeve_capital,
        daily_loss_halt_pct=settings.daily_loss_halt_pct,
        max_open_positions=settings.max_recovery_open_positions,
        decision_inbox_enabled=settings.decision_inbox_enabled,
    )
    return {**result, "paper_only": True, "live_brokerage_submission": False}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args(argv)
    print(json.dumps(run(args.config), default=str))


if __name__ == "__main__":  # pragma: no cover
    main()
