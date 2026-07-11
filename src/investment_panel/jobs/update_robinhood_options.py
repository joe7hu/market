"""Collect Robinhood option chains for the 10x radar.

Read-only: calls quote/chain/instrument MCP tools only, never account or order
tools. Persists chains with source='robinhood' so the radar can consume live
bid/ask, IV, Greeks, open interest, and volume without involving an agent turn.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import json
import os
from typing import Any

from investment_panel.core.config import load_config
from investment_panel.core.robinhood_options import (
    RobinhoodAuthRequired,
    RobinhoodClient,
    authorize_robinhood_mcp,
    collect_robinhood_option_chains,
    load_robinhood_access_token,
)
from investment_panel.core.status import write_source_status
from investment_panel.database.options import incremental_option_symbols, option_universe, persist_collected_option_chains


MIN_QUOTED_FRACTION = 0.2
DEFAULT_INCREMENTAL_SYMBOLS = 20
DEFAULT_STALE_MINUTES = 60
_TRUTHY_OFF = {"0", "false", "off", "no"}


def _max_symbols(config_value: int) -> int:
    raw = os.environ.get("MARKET_ROBINHOOD_MAX_SYMBOLS")
    try:
        value = int((raw or "").strip())
        return value if value > 0 else config_value
    except (TypeError, ValueError):
        return config_value


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        value = int((raw or "").strip())
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


def _incremental_enabled() -> bool:
    return os.environ.get("MARKET_ROBINHOOD_INCREMENTAL", "1").strip().lower() not in _TRUTHY_OFF


def _robinhood_status(errors: list[Any], stored: int) -> str:
    if errors and not stored:
        return "error"
    if errors:
        return "partial"
    return "ok"


def run(
    config_path: str | None = None,
    symbols: list[str] | None = None,
    *,
    client: RobinhoodClient | None = None,
    full: bool = False,
) -> dict[str, Any]:
    config = load_config(config_path)
    provider = config.data_sources.brokers.robinhood
    if not config.data_sources.brokers.enabled or not provider.enabled:
        return {"status": "disabled", "provider": "robinhood"}
    if not provider.readonly:
        return {"status": "unsafe_config", "provider": "robinhood", "error": "robinhood provider must remain readonly"}
    if client is None and not _robinhood_auth_available(provider):
        result = {
            "provider": "robinhood",
            "status": "auth_required",
            "auth_command": "market-update-robinhood-options --auth",
            "auth_token_env": provider.auth_token_env,
            "token_path": os.path.expanduser(os.path.expandvars(provider.token_path)),
            "database": "postgresql",
        }
        status_path = write_source_status(
            config,
            "mini-market-robinhood-options",
            {"source": "market-mini", "job": "update_robinhood_options", "origin": "autonomous_collector", **result},
        )
        return {**result, "status_path": str(status_path) if status_path else None}

    universe = symbols or option_universe(config, limit=_max_symbols(provider.max_symbols))
    target = universe
    if not symbols and not full and _incremental_enabled():
        target = incremental_option_symbols(
            config,
            "robinhood",
            universe,
            limit=min(len(universe), _env_int("MARKET_ROBINHOOD_INCREMENTAL_SYMBOLS", DEFAULT_INCREMENTAL_SYMBOLS)),
            stale_before=datetime.now(UTC) - timedelta(minutes=_env_int("MARKET_ROBINHOOD_STALE_MINUTES", DEFAULT_STALE_MINUTES)),
        )

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
            "database": "postgresql",
        }
        status_path = write_source_status(
            config,
            "mini-market-robinhood-options",
            {"source": "market-mini", "job": "update_robinhood_options", "origin": "autonomous_collector", **result},
        )
        return {**result, "status_path": str(status_path) if status_path else None}
    except Exception as exc:  # noqa: BLE001 - provider/network failures should become job status, not hung jobs
        result = {
            "provider": "robinhood",
            "status": "error",
            "error": str(exc),
            "database": "postgresql",
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
            "database": "postgresql",
        }
        status_path = write_source_status(
            config,
            "mini-market-robinhood-options",
            {"source": "market-mini", "job": "update_robinhood_options", "origin": "autonomous_collector", **result},
        )
        return {**result, "status_path": str(status_path) if status_path else None}

    persisted = persist_collected_option_chains(config, "robinhood", collected)
    stored = int(persisted["contract_count"])

    result = {
        "provider": "robinhood",
        "status": _robinhood_status(collected["errors"], stored),
        "market_data": collected["market_data"],
        "symbols_requested": len(target),
        "symbols_considered": len(universe),
        "symbols": target,
        "incremental": bool(not symbols and not full and _incremental_enabled()),
        "symbols_with_chains": len(collected["rows"]),
        "chain_rows": stored,
        "quoted_rows": quoted_rows,
        "observed_at": collected["observed_at"],
        "errors": collected["errors"][:25],
        "database": "postgresql",
        "ingest_run_id": persisted["run_id"],
        "snapshot_id": persisted["snapshot_id"],
    }
    status_path = write_source_status(
        config,
        "mini-market-robinhood-options",
        {"source": "market-mini", "job": "update_robinhood_options", "origin": "autonomous_collector", **result},
    )
    return {**result, "status_path": str(status_path) if status_path else None}


def _incremental_robinhood_symbols(con: Any, symbols: list[str]) -> list[str]:
    normalized = []
    seen = set()
    for symbol in symbols:
        upper = str(symbol or "").upper()
        if upper and upper not in seen:
            seen.add(upper)
            normalized.append(upper)
    if not normalized:
        return []

    limit = min(len(normalized), _env_int("MARKET_ROBINHOOD_INCREMENTAL_SYMBOLS", DEFAULT_INCREMENTAL_SYMBOLS))
    stale_minutes = _env_int("MARKET_ROBINHOOD_STALE_MINUTES", DEFAULT_STALE_MINUTES)
    cutoff = datetime.now(UTC) - timedelta(minutes=stale_minutes)
    latest_by_symbol = _latest_robinhood_chain_observed_at(con, normalized)

    ranked: list[tuple[int, datetime, int, str]] = []
    for index, symbol in enumerate(normalized):
        latest = latest_by_symbol.get(symbol)
        if latest is not None and latest >= cutoff:
            continue
        bucket = 0 if latest is not None else 1
        ranked.append((bucket, latest or datetime.min.replace(tzinfo=UTC), index, symbol))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [symbol for _bucket, _latest, _index, symbol in ranked[:limit]]


def _latest_robinhood_chain_observed_at(con: Any, symbols: list[str]) -> dict[str, datetime]:
    from investment_panel.core.db import query_rows

    if not symbols:
        return {}
    placeholders = ", ".join(["?"] * len(symbols))
    rows = query_rows(
        con,
        f"""
        SELECT symbol, max(observed_at) AS latest_observed_at
        FROM options_chain
        WHERE source = 'robinhood'
          AND symbol IN ({placeholders})
        GROUP BY symbol
        """,
        symbols,
    )
    out: dict[str, datetime] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        observed = _coerce_utc_datetime(row.get("latest_observed_at"))
        if symbol and observed is not None:
            out[symbol] = observed
    return out


def _coerce_utc_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif value:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _robinhood_auth_available(provider: Any) -> bool:
    try:
        return bool(load_robinhood_access_token(provider))
    except RobinhoodAuthRequired:
        return False


def _upsert_robinhood_quote(con: Any, row: dict[str, Any]) -> None:
    from investment_panel.core.db import json_dumps

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
    parser.add_argument("--full", action="store_true", help="refresh the full configured Robinhood option universe")
    parser.add_argument("--auth", action="store_true", help="run OAuth setup and cache a Robinhood MCP token")
    args = parser.parse_args()
    if args.auth:
        config = load_config(args.config)
        print(json.dumps(authorize_robinhood_mcp(config.data_sources.brokers.robinhood), indent=2, default=str))
    else:
        print(json.dumps(run(args.config, symbols=args.symbols, full=args.full), indent=2, default=str))


if __name__ == "__main__":
    main()
