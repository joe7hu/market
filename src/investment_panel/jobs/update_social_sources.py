"""Pull live X/social sources via opencli (list primary, per-account fallback).

Rate-limit discipline: the curated X list is fetched first (one request). Only if
that succeeds do we fall back to a small, staggered, capped set of per-account
timelines. An ``OpenCliRateLimitError`` (surfaced as a ``rate_limited`` result)
short-circuits the cycle so we do not hammer the limiter.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from investment_panel.core.config import load_config
from investment_panel.core.db import db, init_db
from investment_panel.providers.opencli import OpenCliRunner
from investment_panel.core.source_ingestion.live import fetch_x_account, fetch_x_list, known_symbols


def run(config_path: str | None = None) -> dict[str, Any]:
    config = load_config(config_path)
    x_config = config.research_sources.x
    if not x_config.enabled:
        return {"status": "disabled", "source": "x", "runs": []}

    init_db(config.database.duckdb_path)
    runner = OpenCliRunner(
        command=config.data_sources.opencli.command,
        timeout_seconds=config.data_sources.opencli.timeout_seconds,
    )
    runs: list[dict[str, Any]] = []
    with db(config.database.duckdb_path, read_only=False) as con:
        known = known_symbols(con)
        list_result = fetch_x_list(con, runner, x_config.list_id, limit=x_config.limit, known=known)
        runs.append(list_result.as_dict())

        # Short-circuit per-account fallback if the list call was rate limited.
        if not list_result.rate_limited:
            cap = max(0, int(x_config.account_fetch_cap))
            for handle in x_config.priority_handles[:cap]:
                account_result = fetch_x_account(con, runner, handle, limit=x_config.limit, known=known)
                runs.append(account_result.as_dict())
                if account_result.rate_limited:
                    break

    items = sum(int(run.get("items") or 0) for run in runs)
    signals = sum(int(run.get("signals") or 0) for run in runs)
    rate_limited = any(run.get("rate_limited") for run in runs)
    return {
        "status": "rate_limited" if rate_limited else "ok",
        "source": "x",
        "database": str(config.database.duckdb_path),
        "items": items,
        "signals": signals,
        "rate_limited": rate_limited,
        "runs": runs,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    print(json.dumps(run(args.config), indent=2, default=str))


if __name__ == "__main__":
    main()
