"""Refresh deterministic 10x options radar tables."""

from __future__ import annotations

import argparse
import json
from typing import Any

from investment_panel.core.config import load_config
from investment_panel.core.db import db, init_db
from investment_panel.core.options_radar import DEFAULT_STRATEGY_VERSION, refresh_options_radar, refresh_options_radar_learning_marks


def run(config_path: str | None = None, symbols: list[str] | None = None, strategy_version: str = DEFAULT_STRATEGY_VERSION) -> dict[str, Any]:
    config = load_config(config_path)
    init_db(config.database.duckdb_path)
    with db(config.database.duckdb_path) as con:
        result = refresh_options_radar(con, symbols=symbols, strategy_version=strategy_version)
    return {"database": str(config.database.duckdb_path), "strategy_version": strategy_version, **result}


def run_deterministic_only(
    config_path: str | None = None,
    symbols: list[str] | None = None,
    strategy_version: str = DEFAULT_STRATEGY_VERSION,
) -> dict[str, Any]:
    config = load_config(config_path)
    init_db(config.database.duckdb_path)
    with db(config.database.duckdb_path) as con:
        result = refresh_options_radar(
            con,
            symbols=symbols,
            strategy_version=strategy_version,
            include_agent_work=False,
        )
    return {"database": str(config.database.duckdb_path), "strategy_version": strategy_version, "agent_work": "skipped", **result}


def run_signal_only(
    config_path: str | None = None,
    symbols: list[str] | None = None,
    strategy_version: str = DEFAULT_STRATEGY_VERSION,
    source: str | None = None,
) -> dict[str, Any]:
    """Fast fresh-signal rematerialization for the continuous scheduler: skips the
    agent work AND the heavy learning/backtest machinery so it stays cheap.
    ``source`` scopes it to one option provider (e.g. 'ibkr')."""

    config = load_config(config_path)
    init_db(config.database.duckdb_path)
    with db(config.database.duckdb_path) as con:
        result = refresh_options_radar(
            con,
            symbols=symbols,
            strategy_version=strategy_version,
            source=source,
            include_agent_work=False,
            include_learning=False,
        )
    return {"database": str(config.database.duckdb_path), "strategy_version": strategy_version, "mode": "signal_only", "source": source or "all", **result}


def run_learning_marks(
    config_path: str | None = None,
    strategy_version: str = DEFAULT_STRATEGY_VERSION,
    recent_days: int = 10,
    include_calibration: bool = False,
) -> dict[str, Any]:
    """Incremental marks/calibration refresh for short-horizon learning feedback."""

    config = load_config(config_path)
    init_db(config.database.duckdb_path)
    with db(config.database.duckdb_path) as con:
        result = refresh_options_radar_learning_marks(
            con,
            strategy_version=strategy_version,
            recent_days=recent_days,
            include_calibration=include_calibration,
        )
    return {"database": str(config.database.duckdb_path), "strategy_version": strategy_version, "mode": "learning_marks", **result}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--symbol", action="append", dest="symbols", default=None)
    parser.add_argument("--strategy-version", default=DEFAULT_STRATEGY_VERSION)
    args = parser.parse_args()
    print(json.dumps(run(args.config, symbols=args.symbols, strategy_version=args.strategy_version), indent=2, default=str))


if __name__ == "__main__":
    main()
