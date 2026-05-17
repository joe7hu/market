"""Refresh persisted decision-grade read models without fetching new sources."""

from __future__ import annotations

import argparse
import json
from typing import Any

from investment_panel.core.config import load_config
from investment_panel.core.db import db, init_db
from investment_panel.core.decision import refresh_decision_read_models
from investment_panel.core.status import write_source_status


def run(config_path: str | None = None) -> dict[str, Any]:
    config = load_config(config_path)
    init_db(config.database.duckdb_path)
    with db(config.database.duckdb_path) as con:
        result = refresh_decision_read_models(con, config.watchlist)
    status_path = write_source_status(
        config,
        "mini-market-decision-models",
        {
            "source": "market-mini",
            "job": "refresh_decision_models",
            "origin": "derived_analysis",
            "database": str(config.database.duckdb_path),
            **result,
        },
    )
    return {**result, "database": str(config.database.duckdb_path), "status_path": str(status_path)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    print(json.dumps(run(args.config), indent=2, default=str))


if __name__ == "__main__":
    main()
