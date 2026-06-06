"""Daily deterministic candidate screen."""

from __future__ import annotations

import argparse
from typing import Any

from investment_panel.analysis import run_all_analyses
from investment_panel.core.arco import flatten_arco_items, ingest_arco_theses, load_arco_context
from investment_panel.core.config import load_config
from investment_panel.core.crypto import fetch_coingecko_markets, upsert_crypto_fundamentals
from investment_panel.core.db import db, init_db, upsert_instrument
from investment_panel.core.decision import refresh_decision_read_models
from investment_panel.core.fundamentals import update_equity_fundamentals
from investment_panel.core.instruments import universe_from_config_and_arco
from investment_panel.core.portfolio import ensure_portfolio_instruments, import_portfolio_csv, portfolio_instruments, seed_empty_theses_for_portfolio
from investment_panel.core.prices import fetch_prices, upsert_prices
from investment_panel.core.scoring import score_and_store
from investment_panel.core.source_ingestion.raw_sources import sync_private_raw_sources
from investment_panel.core.sources import lightweight_online_check, record_verified_sources
from investment_panel.core.status import snapshot_duckdb, write_source_status
from investment_panel.core.technicals import compute_and_store


def run(config_path: str | None = None, online_check: bool = False) -> dict[str, Any]:
    config = load_config(config_path)
    init_db(config.database.duckdb_path)
    arco_context = load_arco_context(config.arco)
    arco_items = flatten_arco_items(arco_context)
    universe = universe_from_config_and_arco(config.watchlist, arco_items)
    with db(config.database.duckdb_path) as con:
        record_verified_sources(con)
        if online_check:
            lightweight_online_check(con, config.market_data.user_agent)
        for instrument in universe:
            upsert_instrument(con, instrument)
        portfolio_rows = import_portfolio_csv(con, config.portfolio_csv)
        seed_empty_theses_for_portfolio(con)
        ensure_portfolio_instruments(con)
        universe = merge_universe(universe, portfolio_instruments(con))
        fundamental_rows = update_equity_fundamentals(con, universe, config.market_data.user_agent)
        thesis_rows = ingest_arco_theses(con, arco_context)
        raw_source_result = sync_private_raw_sources(con, config.nas.source_root)
        price_rows = 0
        price_errors: dict[str, str] = {}
        feature_rows = 0
        for instrument in universe:
            symbol = instrument["symbol"]
            try:
                frame = fetch_prices(
                    symbol,
                    lookback_days=config.market_data.lookback_days,
                    mode=config.market_data.mode,
                )
            except Exception as exc:
                price_errors[symbol] = f"{type(exc).__name__}: {exc}"
            else:
                price_rows += upsert_prices(con, frame)
            if compute_and_store(con, instrument["symbol"]):
                feature_rows += 1
        crypto_fundamental_rows = 0
        crypto_symbols = [row["symbol"] for row in universe if row.get("asset_class") == "crypto"]
        if config.market_data.mode == "online" and crypto_symbols:
            try:
                crypto_fundamental_rows = upsert_crypto_fundamentals(con, fetch_coingecko_markets(crypto_symbols))
            except Exception:
                crypto_fundamental_rows = 0
        candidates = score_and_store(con, [row["symbol"] for row in universe], config.scoring.weights)
        analysis_result = run_all_analyses(con, config)
        decision_result = refresh_decision_read_models(con, config.watchlist)
    result = {
        "database": str(config.database.duckdb_path),
        "instruments": len(universe),
        "portfolio_rows": portfolio_rows,
        "arco_thesis_rows": thesis_rows,
        "price_rows": price_rows,
        "price_errors": price_errors,
        "feature_rows": feature_rows,
        "fundamental_rows": fundamental_rows,
        "raw_sources": raw_source_result,
        "crypto_fundamental_rows": crypto_fundamental_rows,
        "candidates": len(candidates),
        "analysis": analysis_result,
        "decision_models": decision_result,
        "top_candidates": candidates[:10],
    }
    snapshot_path = snapshot_duckdb(config, "market-daily-screen")
    status_path = write_source_status(
        config,
        "mini-market-ingest",
        {
            "source": "market-mini",
            "job": "daily_screen",
            "origin": "autonomous_collector",
            "duckdbSnapshot": str(snapshot_path) if snapshot_path else None,
            **result,
        },
    )
    return {**result, "status_path": str(status_path), "duckdb_snapshot": str(snapshot_path) if snapshot_path else None}


def merge_universe(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for group in groups:
        for item in group:
            symbol = str(item.get("symbol") or "").upper()
            if not symbol:
                continue
            existing = merged.get(symbol, {})
            merged[symbol] = {**item, **{key: value for key, value in existing.items() if value not in (None, "")}}
    return sorted(merged.values(), key=lambda row: row["symbol"])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--online-check", action="store_true")
    args = parser.parse_args()
    import json

    print(json.dumps(run(args.config, online_check=args.online_check), indent=2, default=str))


if __name__ == "__main__":
    main()
