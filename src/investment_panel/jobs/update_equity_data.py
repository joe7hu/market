"""Update equity daily prices and technicals."""

from __future__ import annotations

import argparse
import json
from typing import Any

from investment_panel.core.config import load_config
from investment_panel.core.db import db, init_db, query_rows
from investment_panel.core.decision import refresh_decision_read_models
from investment_panel.core.fundamentals import update_equity_fundamentals
from investment_panel.core.portfolio import ensure_portfolio_instruments
from investment_panel.core.prices import fetch_prices, upsert_prices
from investment_panel.core.status import write_source_status
from investment_panel.core.technicals import compute_and_store


def run(config_path: str | None = None) -> dict[str, Any]:
    config = load_config(config_path)
    init_db(config.database.duckdb_path)
    with db(config.database.duckdb_path) as con:
        ensure_portfolio_instruments(con)
        instruments = query_rows(con, "SELECT symbol, asset_class, source FROM instruments WHERE asset_class IN ('equity', 'etf') ORDER BY symbol")
        config_by_symbol = {row["symbol"].upper(): row for row in config.watchlist}
        for instrument in instruments:
            instrument["cik"] = config_by_symbol.get(instrument["symbol"], {}).get("cik")
        symbols = [row["symbol"] for row in instruments]
        fundamental_rows = update_equity_fundamentals(con, instruments, config.market_data.user_agent)
        price_rows = 0
        price_errors: dict[str, str] = {}
        feature_rows = 0
        for symbol in symbols:
            try:
                frame = fetch_prices(symbol, config.market_data.lookback_days, config.market_data.mode)
            except Exception as exc:
                price_errors[symbol] = f"{type(exc).__name__}: {exc}"
            else:
                price_rows += upsert_prices(con, frame)
            if compute_and_store(con, symbol):
                feature_rows += 1
        decision_result = refresh_decision_read_models(con, config.watchlist)
    result = {
        "database": str(config.database.duckdb_path),
        "symbols": len(symbols),
        "price_rows": price_rows,
        "price_errors": price_errors,
        "feature_rows": feature_rows,
        "fundamental_rows": fundamental_rows,
        "decision_models": decision_result,
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
