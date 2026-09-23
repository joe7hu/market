"""CLI entrypoint for the ticker decision workflow."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json

from investment_panel.settings import load_config
from investment_panel.infrastructure.postgres.authority import runtime_for_config
from investment_panel.workflows.market import terminal_bar_retry
from investment_panel.workflows.ticker_decisions import publish


def run(
    config_path: str | None = "config.yaml",
    *,
    tickers: list[str] | None = None,
    limit: int = 2_000,
) -> dict[str, object]:
    config = load_config(config_path)
    retry = terminal_bar_retry(runtime_for_config(config), config.watchlist, datetime.now(UTC))
    if retry is not None:
        return {
            **retry,
            "ticker_decisions": {"status": "skipped", "reason": retry["reason"]},
        }
    return publish(config_path, symbols=tickers, limit=limit)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--ticker", "--tickers", action="append", dest="tickers")
    parser.add_argument("--limit", type=int, default=2_000)
    args = parser.parse_args(argv)
    print(json.dumps(run(args.config, tickers=args.tickers, limit=args.limit), indent=2, default=str))


__all__ = ["main", "run"]
