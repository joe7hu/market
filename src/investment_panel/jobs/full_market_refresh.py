"""Compatibility entrypoint for the PostgreSQL daily refresh composition."""

from __future__ import annotations

import argparse
import json
from typing import Any

from investment_panel.jobs import postgres_refresh


def run(
    config_path: str | None = None,
    *,
    online_check: bool = False,
    max_filings: int = 3,
    fetch_holdings: bool = False,
    continue_on_error: bool = False,
) -> dict[str, Any]:
    del online_check, max_filings, fetch_holdings
    return postgres_refresh.full(config_path, continue_on_error=continue_on_error)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--continue-on-error", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.config, continue_on_error=args.continue_on_error), indent=2, default=str))


if __name__ == "__main__":
    main()
