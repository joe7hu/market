"""Refresh point-in-time stock decision outcomes; never stage stock orders."""

from __future__ import annotations

import argparse
import json
from typing import Any

from investment_panel.core.config import load_config
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.symbol_decision_outcomes import SymbolDecisionOutcomeRepository


def run(config_path: str | None = "config.yaml") -> dict[str, Any]:
    config = load_config(config_path)
    return SymbolDecisionOutcomeRepository(runtime_for_config(config)).refresh()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args(argv)
    print(json.dumps(run(args.config), default=str))


if __name__ == "__main__":  # pragma: no cover
    main()
