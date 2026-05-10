"""Disclosure update job for verified sources and configured 13F trackers."""

from __future__ import annotations

import argparse
import json

from investment_panel.core.config import load_config
from investment_panel.core.db import db, init_db
from investment_panel.core.disclosures import (
    ingest_public_disclosure_csvs,
    ensure_disclosure_symbol_prices,
    ingest_official_house_disclosures_for_trader,
    ingest_13f_trackers,
    load_13f_trackers_from_config,
    load_public_disclosure_csvs_from_config,
    load_tracked_traders_from_config,
    purge_direct_tracker_rows,
    rebuild_trader_replica_portfolios,
)
from investment_panel.core.sources import lightweight_online_check, record_verified_sources
from investment_panel.core.status import write_source_status


def run(
    config_path: str | None = None,
    online_check: bool = False,
    max_filings: int = 3,
    fetch_holdings: bool = True,
) -> dict[str, str | bool | int]:
    config = load_config(config_path)
    trackers = load_13f_trackers_from_config(config_path)
    public_disclosure_csvs = load_public_disclosure_csvs_from_config(config_path)
    tracked_traders = load_tracked_traders_from_config(config_path)
    init_db(config.database.duckdb_path)
    with db(config.database.duckdb_path) as con:
        purge_direct_tracker_rows(con)
        record_verified_sources(con)
        if online_check:
            lightweight_online_check(con, config.market_data.user_agent)
        public_disclosure_result = ingest_public_disclosure_csvs(con, public_disclosure_csvs)
        official_house_results = [ingest_official_house_disclosures_for_trader(con, trader) for trader in tracked_traders]
        price_result = ensure_disclosure_symbol_prices(con)
        ingest_result = ingest_13f_trackers(
            con,
            trackers,
            config.market_data.user_agent,
            default_max_filings=max_filings,
            fetch_holdings=fetch_holdings,
        )
        replica_result = rebuild_trader_replica_portfolios(con)
    result = {
        "database": str(config.database.duckdb_path),
        "online_check": online_check,
        "status": "disclosures_updated",
        "trackers_configured": len(trackers),
        "public_disclosure_csvs_configured": len(public_disclosure_csvs),
        "tracked_traders_configured": len(tracked_traders),
        "official_house_filings_found": sum(row["official_house_filings_found"] for row in official_house_results),
        "official_house_filings_ingested": sum(row["official_house_filings_ingested"] for row in official_house_results),
        "official_house_rows_ingested": sum(row["official_house_rows_ingested"] for row in official_house_results),
        **public_disclosure_result,
        **price_result,
        **ingest_result,
        **replica_result,
    }
    status_path = write_source_status(
        config,
        "mini-market-disclosures",
        {
            "source": "market-mini",
            "job": "update_disclosures",
            "origin": "autonomous_collector",
            **result,
        },
    )
    return {**result, "status_path": str(status_path)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--online-check", action="store_true")
    parser.add_argument("--max-filings", type=int, default=3)
    parser.add_argument("--skip-holdings", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.config, args.online_check, args.max_filings, not args.skip_holdings), indent=2))


if __name__ == "__main__":
    main()
