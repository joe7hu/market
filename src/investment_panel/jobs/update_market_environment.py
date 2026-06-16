"""Refresh broad-market valuation and environment inputs for the Market page."""

from __future__ import annotations

import argparse
import json
from typing import Any

from investment_panel.analysis.market_environment import store_market_environment_sources
from investment_panel.core.config import load_config
from investment_panel.core.db import db, init_db
from investment_panel.core.status import write_source_status


def run(config_path: str | None = None) -> dict[str, Any]:
    config = load_config(config_path)
    init_db(config.database.duckdb_path)
    with db(config.database.duckdb_path) as con:
        market_environment_rows = store_market_environment_sources(con)
    result = {
        "database": str(config.database.duckdb_path),
        "market_environment_rows": market_environment_rows,
        "status": "ok",
    }
    status_path = write_source_status(
        config,
        "mini-market-environment",
        {
            "source": "market-mini",
            "job": "update_market_environment",
            "origin": "manual_or_scheduler_refresh",
            **result,
        },
    )
    return {**result, "status_path": str(status_path)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    print(json.dumps(run(args.config), indent=2, default=str))


if __name__ == "__main__":
    main()
