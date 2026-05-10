"""Update equity daily prices and technicals."""

from __future__ import annotations

import argparse
import json

from investment_panel.core.config import load_config
from investment_panel.core.db import db, init_db, query_rows
from investment_panel.core.fundamentals import update_equity_fundamentals
from investment_panel.core.prices import fetch_prices, upsert_prices
from investment_panel.core.status import write_source_status
from investment_panel.core.technicals import compute_and_store


def run(config_path: str | None = None) -> dict[str, int | str]:
    config = load_config(config_path)
    init_db(config.database.duckdb_path)
    with db(config.database.duckdb_path) as con:
        instruments = query_rows(con, "SELECT symbol, asset_class, source FROM instruments WHERE asset_class IN ('equity', 'etf') ORDER BY symbol")
        config_by_symbol = {row["symbol"].upper(): row for row in config.watchlist}
        for instrument in instruments:
            instrument["cik"] = config_by_symbol.get(instrument["symbol"], {}).get("cik")
        symbols = [row["symbol"] for row in instruments]
        fundamental_rows = update_equity_fundamentals(con, instruments, config.market_data.user_agent)
        price_rows = 0
        feature_rows = 0
        for symbol in symbols:
            frame = fetch_prices(symbol, config.market_data.lookback_days, config.market_data.mode)
            price_rows += upsert_prices(con, frame)
            if compute_and_store(con, symbol):
                feature_rows += 1
    result = {
        "database": str(config.database.duckdb_path),
        "symbols": len(symbols),
        "price_rows": price_rows,
        "feature_rows": feature_rows,
        "fundamental_rows": fundamental_rows,
    }
    status_path = write_source_status(
        config,
        "mini-market-equity",
        {
            "source": "market-mini",
            "job": "update_equity_data",
            "origin": "autonomous_collector",
            **result,
        },
    )
    return {**result, "status_path": str(status_path)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    print(json.dumps(run(args.config), indent=2))


if __name__ == "__main__":
    main()
