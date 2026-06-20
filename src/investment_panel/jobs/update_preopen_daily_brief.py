"""Generate and persist the pre-open macro / QQQ daily brief."""

from __future__ import annotations

import argparse
import json
from typing import Any

from investment_panel.core.config import load_config
from investment_panel.core.db import db, init_db
from investment_panel.core.preopen_brief import refresh_preopen_daily_brief, should_run_scheduled_preopen_brief
from investment_panel.core.status import write_source_status


def run(config_path: str | None = None, *, scheduled: bool = False) -> dict[str, Any]:
    config = load_config(config_path)
    init_db(config.database.duckdb_path)
    with db(config.database.duckdb_path, read_only=False) as con:
        if scheduled:
            should_run, gate = should_run_scheduled_preopen_brief(con)
            if not should_run:
                return {
                    "database": str(config.database.duckdb_path),
                    "status": "skipped",
                    "ok": True,
                    "scheduled": True,
                    **gate,
                }
        brief = refresh_preopen_daily_brief(con)
    result = {
        "database": str(config.database.duckdb_path),
        "brief_date": brief["brief_date"],
        "status": brief["status"],
        "model_name": brief["model_name"],
        "model_version": brief["model_version"],
        "qqq_forecast_status": brief["qqq_forecast"].get("status"),
        "event_count": len(brief["key_events"]),
        "source_models": brief["source_models"],
        "error": brief.get("error") or "",
        "scheduled": scheduled,
        "ok": brief["status"] in {"ok", "deterministic_fallback"},
    }
    status_path = write_source_status(
        config,
        "mini-market-preopen-daily-brief",
        {
            "source": "market-mini",
            "job": "update_preopen_daily_brief",
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
