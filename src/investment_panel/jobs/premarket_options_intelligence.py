"""PostgreSQL premarket option and `/today` publication entrypoint."""

from __future__ import annotations

import argparse
import json
from typing import Any

from investment_panel.database.options_constants import DEFAULT_STRATEGY_VERSION
from investment_panel.jobs import postgres_refresh


def run(config_path: str | None = None, *, strategy_version: str = DEFAULT_STRATEGY_VERSION) -> dict[str, Any]:
    return {"strategy_version": strategy_version, **postgres_refresh.premarket(config_path)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--strategy-version", default=DEFAULT_STRATEGY_VERSION)
    args = parser.parse_args()
    print(json.dumps(run(args.config, strategy_version=args.strategy_version), indent=2, default=str))


if __name__ == "__main__":
    main()
