"""CLI entrypoint for the ticker decision workflow."""

from __future__ import annotations

import argparse
import json

from investment_panel.workflows.ticker_decisions import publish


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--ticker", "--tickers", action="append", dest="tickers")
    parser.add_argument("--limit", type=int, default=2_000)
    args = parser.parse_args(argv)
    print(json.dumps(publish(args.config, symbols=args.tickers, limit=args.limit), indent=2, default=str))


__all__ = ["main"]
