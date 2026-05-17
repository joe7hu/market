"""Refresh local market event calendar rows."""

from __future__ import annotations

import argparse
import json
from typing import Any

from investment_panel.core.config import load_config
from investment_panel.core.db import db, init_db
from investment_panel.core.decision import refresh_decision_read_models
from investment_panel.core.event_calendar import update_event_calendar
from investment_panel.core.status import write_source_status


def run(config_path: str | None = None) -> dict[str, Any]:
    config = load_config(config_path)
    init_db(config.database.duckdb_path)
    with db(config.database.duckdb_path) as con:
        result = update_event_calendar(con, config)
        decision_result = refresh_decision_read_models(con, config.watchlist)
    result = {**result, "decision_models": decision_result}
    status_path = write_source_status(
        config,
        "mini-market-event-calendar",
        {
            "source": "market-mini",
            "job": "update_event_calendar",
            "origin": "autonomous_collector",
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
