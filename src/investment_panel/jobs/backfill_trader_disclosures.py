"""Backfill historical public disclosures for newly tracked traders."""

from __future__ import annotations

import argparse
import json
from typing import Any

from investment_panel.core.config import load_config
from investment_panel.core.db import db, init_db
from investment_panel.core.disclosures import backfill_trader_disclosure_history, load_tracked_traders_from_config
from investment_panel.core.status import write_source_status


def run(
    config_path: str | None = None,
    trader_name: str | None = None,
    replace: bool = True,
) -> dict[str, Any]:
    config = load_config(config_path)
    traders = load_tracked_traders_from_config(config_path)
    selected = [
        trader for trader in traders if trader_name is None or trader["trader_name"].lower() == trader_name.lower()
    ]
    if trader_name and not selected:
        raise ValueError(f"No tracked trader named {trader_name!r} in config")

    init_db(config.database.duckdb_path)
    results = []
    with db(config.database.duckdb_path) as con:
        for trader in selected:
            results.append(backfill_trader_disclosure_history(con, trader, replace=replace))

    result = {
        "database": str(config.database.duckdb_path),
        "status": "trader_disclosures_backfilled",
        "tracked_traders_configured": len(traders),
        "traders_backfilled": len(results),
        "public_disclosure_rows_ingested": sum(int(row["public_disclosure_rows_ingested"]) for row in results),
        "trader_replica_portfolios_built": sum(int(row["trader_replica_portfolios_built"]) for row in results),
        "results": results,
    }
    status_path = write_source_status(
        config,
        "mini-market-trader-backfill",
        {
            "source": "market-mini",
            "job": "backfill_trader_disclosures",
            "origin": "operator_onboarding",
            **result,
        },
    )
    return {**result, "status_path": str(status_path)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--trader", help="Backfill only this configured trader. Omit to backfill all tracked traders.")
    parser.add_argument("--no-replace", action="store_true", help="Append/upsert rows without clearing this trader's old model rows first.")
    args = parser.parse_args()
    print(json.dumps(run(args.config, args.trader, replace=not args.no_replace), indent=2))


if __name__ == "__main__":
    main()
