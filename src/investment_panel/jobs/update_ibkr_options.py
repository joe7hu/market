"""Collect IBKR option chains (price/greeks/OI/volume) for the 10x radar.

Read-only: requests contract details + delayed market-data snapshots, never
orders. Persists chains with source='ibkr' so the radar can consume a single
reliable option source instead of the rate-limited TradingView+yfinance combo.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

from investment_panel.core.config import load_config
from investment_panel.core.ibkr_options import collect_ibkr_option_chains
from investment_panel.core.status import write_source_status
from investment_panel.database.options import option_universe, persist_collected_option_chains

# Minimum fraction of collected contracts that must carry a live bid/ask for the
# snapshot to be worth persisting. Off-hours the delayed feed returns ~0% quoted, so
# this keeps quote-less snapshots from superseding the last good market-hours one.
MIN_QUOTED_FRACTION = 0.2


def _max_symbols() -> int:
    raw = os.environ.get("MARKET_IBKR_MAX_SYMBOLS")
    try:
        value = int((raw or "").strip())
        return value if value > 0 else 40
    except (TypeError, ValueError):
        return 40


def _ibkr_status(errors: list[Any], stored: int) -> str:
    """Honest job status so an offline gateway can't report green.

    A connect failure is surfaced as ``gateway_offline``; other errors with no
    stored rows are an ``error``; partial errors with data are ``partial``.
    """

    if any("ibkr_connect_failed" in str(err) for err in errors):
        return "gateway_offline"
    if errors and not stored:
        return "error"
    if errors:
        return "partial"
    return "ok"


def run(config_path: str | None = None, symbols: list[str] | None = None) -> dict[str, Any]:
    config = load_config(config_path)
    if not config.data_sources.brokers.enabled or not config.data_sources.brokers.ibkr.enabled:
        return {"status": "disabled", "provider": "ibkr"}
    target = symbols or option_universe(config, limit=_max_symbols())
    # Collect over the network WITHOUT holding the DB lock — the IBKR scan takes
    # minutes, and holding a write connection that whole time would block the radar.
    collected = collect_ibkr_option_chains(config.data_sources.brokers.ibkr, target)
    # Off-hours the delayed feed returns open interest but no bid/ask. Persisting such
    # a quote-less snapshot would supersede the last good market-hours one and poison
    # the radar with null-spread "data gap" contracts. Skip the store when the pull is
    # essentially unquoted; the radar then stays frozen on the last quoted snapshot
    # (which is also what should be shown pre-open). Data-driven, so it handles
    # holidays / early closes without a clock check.
    total_rows = sum(len(rows) for rows in collected["rows"].values())
    quoted_rows = sum(
        1
        for rows in collected["rows"].values()
        for r in rows
        if (r.get("bid") or 0) > 0 or (r.get("ask") or 0) > 0
    )
    if total_rows and quoted_rows / total_rows < MIN_QUOTED_FRACTION:
        result = {
            "provider": "ibkr",
            "status": "skipped_unquoted_snapshot",
            "market_data": collected["market_data"],
            "quoted_rows": quoted_rows,
            "total_rows": total_rows,
            "observed_at": collected["observed_at"],
            "database": config.database.url,
        }
        status_path = write_source_status(
            config,
            "mini-market-ibkr-options",
            {"source": "market-mini", "job": "update_ibkr_options", "origin": "autonomous_collector", **result},
        )
        return {**result, "status_path": str(status_path) if status_path else None}
    persisted = persist_collected_option_chains(config, "ibkr", collected)
    stored = int(persisted["contract_count"])
    result = {
        "provider": "ibkr",
        "status": _ibkr_status(collected["errors"], stored),
        "market_data": collected["market_data"],
        "symbols_with_chains": len(collected["rows"]),
        "chain_rows": stored,
        "observed_at": collected["observed_at"],
        "errors": collected["errors"][:25],
    }
    result["database"] = config.database.url
    result["ingest_run_id"] = persisted["run_id"]
    result["snapshot_id"] = persisted["snapshot_id"]
    result["symbols_requested"] = len(target)
    status_path = write_source_status(
        config,
        "mini-market-ibkr-options",
        {"source": "market-mini", "job": "update_ibkr_options", "origin": "autonomous_collector", **result},
    )
    return {**result, "status_path": str(status_path) if status_path else None}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--symbol", action="append", dest="symbols", default=None)
    args = parser.parse_args()
    print(json.dumps(run(args.config, symbols=args.symbols), indent=2, default=str))


if __name__ == "__main__":
    main()
