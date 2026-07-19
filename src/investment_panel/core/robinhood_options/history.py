"""Full, source-faithful Robinhood option-chain collection for historical data."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from investment_panel.core.coercion import to_finite_float as as_float
from investment_panel.core.robinhood_options.collector import (
    DEFAULT_MAX_COLLECTION_SECONDS,
    DEFAULT_MAX_RESPONSE_BYTES,
    RobinhoodClient,
    RobinhoodMcpClient,
    _batches,
    _cursor_from_next,
    _payload_data,
    _payload_list,
    option_quote_row,
)
from investment_panel.core.robinhood_options.auth import load_robinhood_access_token


def collect_robinhood_full_option_chain(
    config: Any,
    symbol: str,
    *,
    client: RobinhoodClient | None = None,
    quote_batch_size: int | None = None,
) -> dict[str, Any]:
    """Collect every returned tradable call and put without sampling strikes or expiry.

    The returned rows retain the complete instrument and quote response under
    ``provider_payload`` so normalized fields never become the only record.
    """

    started_at = datetime.now(UTC)
    symbol = str(symbol).upper().strip()
    if not symbol:
        raise ValueError("symbol is required")
    if client is None:
        client = RobinhoodMcpClient(
            str(getattr(config, "mcp_url", "https://agent.robinhood.com/mcp/trading")),
            auth_token=load_robinhood_access_token(config),
            timeout_seconds=int(getattr(config, "timeout_seconds", 30)),
            max_response_bytes=int(getattr(config, "max_response_bytes", DEFAULT_MAX_RESPONSE_BYTES)),
        )
    batch_size = max(1, min(50, int(quote_batch_size or getattr(config, "quote_batch_size", 20))))
    deadline = time.monotonic() + max(1, int(getattr(config, "max_collection_seconds", DEFAULT_MAX_COLLECTION_SECONDS)))
    equity = _equity_quote(client, symbol, deadline)
    spot = as_float(equity.get("last_trade_price")) or as_float(equity.get("adjusted_mark_price"))
    instruments: list[dict[str, Any]] = []
    errors: list[str] = []
    for chain in _payload_list(client.get_option_chains(symbol), "chains"):
        chain_id = str(chain.get("id") or "")
        for expiry in chain.get("expiration_dates") or []:
            for option_type in ("call", "put"):
                try:
                    instruments.extend(_all_instruments(client, chain_id, str(expiry), option_type, deadline))
                except Exception as exc:  # keep independently auditable partial captures
                    errors.append(f"{expiry}:{option_type}:{type(exc).__name__}: {exc}")
                if time.monotonic() > deadline:
                    errors.append("collection_timeout")
                    break
            if time.monotonic() > deadline:
                break
        if time.monotonic() > deadline:
            break
    instruments = [row for row in instruments if str(row.get("tradability") or "tradable").lower() == "tradable"]
    by_id = {str(row.get("id")): row for row in instruments if row.get("id")}
    rows: list[dict[str, Any]] = []
    for batch in _batches(list(by_id), batch_size):
        if time.monotonic() > deadline:
            errors.append("collection_timeout")
            break
        try:
            payload = client.get_option_quotes(batch)
        except Exception as exc:
            errors.append(f"quote_batch:{type(exc).__name__}: {exc}")
            continue
        for result in _payload_list(payload, "results"):
            quote = dict(result.get("quote") or {})
            instrument_id = str(quote.get("instrument_id") or result.get("instrument_id") or "")
            instrument = by_id.get(instrument_id)
            if not instrument:
                continue
            row = option_quote_row(instrument, quote)
            if row is None:
                continue
            row["underlying_symbol"] = symbol
            row["underlying_price"] = spot
            row["provider_payload"] = {"instrument": instrument, "quote": quote}
            row["previous_close"] = row.get("close")
            row["provider_updated_at"] = quote.get("updated_at")
            rows.append(row)
    finished_at = datetime.now(UTC)
    expected = len(by_id)
    received = len(rows)
    return {
        "symbol": symbol,
        "rows": rows,
        "expected_contract_count": expected,
        "received_contract_count": received,
        "completeness": (received / expected) if expected else 0.0,
        "errors": errors,
        "capture_started_at": started_at,
        "capture_finished_at": finished_at,
        "underlying_payload": equity,
        "timed_out": "collection_timeout" in errors,
    }


def _equity_quote(client: RobinhoodClient, symbol: str, deadline: float) -> dict[str, Any]:
    if time.monotonic() > deadline:
        raise TimeoutError("collection_timeout")
    results = _payload_list(client.get_equity_quotes([symbol]), "results")
    return dict(results[0].get("quote") or {}) if results else {}


def _all_instruments(
    client: RobinhoodClient,
    chain_id: str,
    expiry: str,
    option_type: str,
    deadline: float,
) -> list[dict[str, Any]]:
    cursor: str | None = None
    rows: list[dict[str, Any]] = []
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError("collection_timeout")
        payload = client.get_option_instruments(
            chain_id=chain_id,
            expiration_dates=expiry,
            option_type=option_type,
            cursor=cursor,
        )
        rows.extend(dict(row) for row in _payload_list(payload, "instruments"))
        cursor = _cursor_from_next(_payload_data(payload).get("next"))
        if not cursor:
            return rows
