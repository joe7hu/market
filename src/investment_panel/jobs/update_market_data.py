"""CLI entrypoint for the market-data workflow."""

from __future__ import annotations

import argparse
import json

from investment_panel.settings import load_config
from investment_panel.workflows.market_data import run_for_config


def run(
    config_path: str | None = None,
    *,
    symbols: list[str] | None = None,
    publish: bool = True,
) -> dict[str, object]:
    return run_for_config(load_config(config_path), symbols=symbols, publish=publish)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--symbol", action="append", dest="symbols", default=None)
    args = parser.parse_args()
    print(json.dumps(run(args.config, symbols=args.symbols), indent=2, default=str))


__all__ = ["main", "run"]
