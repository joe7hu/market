"""Refresh free/local provider sources and derived analyses."""

from __future__ import annotations

import argparse
import json
from typing import Any

from investment_panel.analysis import run_all_analyses
from investment_panel.core.config import load_config
from investment_panel.core.db import db, init_db
from investment_panel.core.decision import refresh_decision_read_models
from investment_panel.core.free_sources import update_tradingview_sources, update_yfinance_sources
from investment_panel.core.portfolio import ensure_portfolio_instruments
from investment_panel.core.status import write_source_status
from investment_panel.jobs import update_equity_data


def run(
    config_path: str | None = None,
    equity_data: bool = True,
    tradingview: bool = True,
    yfinance: bool = True,
    analyses: bool = True,
    symbols: list[str] | None = None,
) -> dict[str, Any]:
    config = load_config(config_path)
    init_db(config.database.duckdb_path)
    with db(config.database.duckdb_path) as con:
        ensure_portfolio_instruments(con)
        preflight_decision_result = refresh_decision_read_models(con, config.watchlist)
    equity_result = update_equity_data.run(config_path) if equity_data else {"status": "skipped"}
    with db(config.database.duckdb_path) as con:
        tradingview_result = update_tradingview_sources(con, config, symbols=symbols) if tradingview else {"status": "skipped"}
        post_tradingview_decision_result = refresh_decision_read_models(con, config.watchlist)
        yfinance_result = update_yfinance_sources(con, config) if yfinance else {"status": "skipped"}
        analysis_result = run_all_analyses(con, config) if analyses else {"status": "skipped"}
        decision_result = refresh_decision_read_models(con, config.watchlist)
    result = {
        "database": str(config.database.duckdb_path),
        "preflight_decision_models": preflight_decision_result,
        "equity_data": equity_result,
        "tradingview": tradingview_result,
        "post_tradingview_decision_models": post_tradingview_decision_result,
        "yfinance": yfinance_result,
        "analysis": analysis_result,
        "decision_models": decision_result,
    }
    status_path = write_source_status(
        config,
        "mini-market-free-sources",
        {
            "source": "market-mini",
            "job": "update_free_sources",
            "origin": "autonomous_collector",
            **result,
        },
    )
    return {**result, "status_path": str(status_path)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--skip-equity-data", action="store_true")
    parser.add_argument("--skip-tradingview", action="store_true")
    parser.add_argument("--skip-yfinance", action="store_true")
    parser.add_argument("--skip-analyses", action="store_true")
    parser.add_argument("--symbol", action="append", dest="symbols", default=None)
    args = parser.parse_args()
    print(
        json.dumps(
            run(
                args.config,
                equity_data=not args.skip_equity_data,
                tradingview=not args.skip_tradingview,
                yfinance=not args.skip_yfinance,
                analyses=not args.skip_analyses,
                symbols=args.symbols,
            ),
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
