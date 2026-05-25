"""Refresh IBKR/moomoo broker sources and advisory paper-trade read models."""

from __future__ import annotations

import argparse
import json
from typing import Any

from investment_panel.core import brokers


def run(config_path: str | None = None) -> dict[str, Any]:
    return brokers.run(config_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    print(json.dumps(run(args.config), indent=2, default=str))


if __name__ == "__main__":
    main()
