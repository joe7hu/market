"""Collect Robinhood option chains for the 10x radar.

Read-only: calls quote/chain/instrument MCP tools only, never account or order
tools. Persists chains with source='robinhood' so the radar can consume live
bid/ask, IV, Greeks, open interest, and volume without involving an agent turn.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

from investment_panel.core.config import load_config
from investment_panel.core.db import db, init_db, json_dumps
from investment_panel.core.free_sources import option_symbols, store_options_chain
from investment_panel.core.robinhood_options import RobinhoodAuthRequired, RobinhoodClient, authorize_robinhood_mcp, collect_robinhood_option_chains
from investment_panel.core.status import write_source_status


MIN_QUOTED_FRACTION = 0.2


def _max_symbols(config_value: int) -> int:
    raw = os.environ.get("MARKET_ROBINHOOD_MAX_SYMBOLS")
    try:
        value = int((raw or "").strip())
        return value if value > 0 else config_value
    except (TypeError, ValueError):
        return config_value


def _robinhood_status(errors: list[Any], stored: int) -> str:
    if errors and not stored:
        return "error"
    if errors:
        return "partial"
    return "ok"


def run(config_path: str | None = None, symbols: list[str] | None = None, *, client: RobinhoodClient | None = None) -> dict[str, Any]:
    config = load_config(config_path)
    provider = config.data_sources.brokers.robinhood
    if not config.data_sources.brokers.enabled or not provider.enabled:
        return {"status": "disabled", "provider": "robinhood"}
    if not provider.readonly:
        return {"status": "unsafe_config", "provider": "robinhood", "error": "robinhood provider must remain readonly"}
    if client is None and not os.environ.get(provider.auth_token_env) and not os.path.exists(os.path.expanduser(os.path.expandvars(provider.token_path))):
        result = {
            "provider": "robinhood",
            "status": "auth_required",
            "auth_command": "market-update-robinhood-options --auth",
            "auth_token_env": provider.auth_token_env,
            "token_path": os.path.expanduser(os.path.expandvars(provider.token_path)),
            "database": str(config.database.duckdb_path),
        }
        status_path = write_source_status(
            config,
            "mini-market-robinhood-options",
            {"source": "market-mini", "job": "update_robinhood_options", "origin": "autonomous_collector", **result},
        )
        return {**result, "status_path": str(status_path) if status_path else None}

    init_db(config.database.duckdb_path)
    with db(config.database.duckdb_path) as con:
        target = symbols or option_symbols(con, config)[: _max_symbols(provider.max_symbols)]

    try:
        collected = collect_robinhood_option_chains(provider, target, client=client)
    except RobinhoodAuthRequired as exc:
        result = {
            "provider": "robinhood",
            "status": "auth_required",
            "auth_command": "market-update-robinhood-options --auth",
            "auth_token_env": provider.auth_token_env,
            "token_path": os.path.expanduser(os.path.expandvars(provider.token_path)),
            "error": str(exc),
            "database": str(config.database.duckdb_path),
        }
        status_path = write_source_status(
            config,
            "mini-market-robinhood-options",
            {"source": "market-mini", "job": "update_robinhood_options", "origin": "autonomous_collector", **result},
        )
        return {**result, "status_path": str(status_path) if status_path else None}
    total_rows = sum(len(rows) for rows in collected["rows"].values())
    quoted_rows = sum(
        1
        for rows in collected["rows"].values()
        for row in rows
        if (row.get("bid") or 0) > 0 or (row.get("ask") or 0) > 0
    )
    if total_rows and quoted_rows / total_rows < MIN_QUOTED_FRACTION:
        result = {
            "provider": "robinhood",
            "status": "skipped_unquoted_snapshot",
            "market_data": collected["market_data"],
            "quoted_rows": quoted_rows,
            "total_rows": total_rows,
            "observed_at": collected["observed_at"],
            "database": str(config.database.duckdb_path),
        }
        status_path = write_source_status(
            config,
            "mini-market-robinhood-options",
            {"source": "market-mini", "job": "update_robinhood_options", "origin": "autonomous_collector", **result},
        )
        return {**result, "status_path": str(status_path) if status_path else None}

    stored = 0
    with db(config.database.duckdb_path) as con:
        for quote in collected.get("quotes") or []:
            _upsert_robinhood_quote(con, quote)
        for symbol, rows in collected["rows"].items():
            stored += store_options_chain(con, symbol, collected["observed_at"], rows, source="robinhood")

    result = {
        "provider": "robinhood",
        "status": _robinhood_status(collected["errors"], stored),
        "market_data": collected["market_data"],
        "symbols_requested": len(target),
        "symbols_with_chains": len(collected["rows"]),
        "chain_rows": stored,
        "quoted_rows": quoted_rows,
        "observed_at": collected["observed_at"],
        "errors": collected["errors"][:25],
        "database": str(config.database.duckdb_path),
    }
    status_path = write_source_status(
        config,
        "mini-market-robinhood-options",
        {"source": "market-mini", "job": "update_robinhood_options", "origin": "autonomous_collector", **result},
    )
    return {**result, "status_path": str(status_path) if status_path else None}


def _upsert_robinhood_quote(con: Any, row: dict[str, Any]) -> None:
    con.execute(
        """
        INSERT OR REPLACE INTO quotes_intraday
        (symbol, observed_at, price, change_pct, change_abs, currency, source, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            row.get("symbol"),
            row.get("time"),
            row.get("close"),
            row.get("change"),
            row.get("change_abs"),
            row.get("currency") or "USD",
            "robinhood",
            json_dumps(row.get("raw") or row),
        ],
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--symbol", action="append", dest="symbols", default=None)
    parser.add_argument("--auth", action="store_true", help="run OAuth setup and cache a Robinhood MCP token")
    args = parser.parse_args()
    if args.auth:
        config = load_config(args.config)
        print(json.dumps(authorize_robinhood_mcp(config.data_sources.brokers.robinhood), indent=2, default=str))
    else:
        print(json.dumps(run(args.config, symbols=args.symbols), indent=2, default=str))


if __name__ == "__main__":
    main()
