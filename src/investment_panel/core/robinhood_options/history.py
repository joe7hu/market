"""Full, source-faithful Robinhood option-chain collection for historical data."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from investment_panel.core.config import RobinhoodConfig
from investment_panel.core.coercion import to_finite_float as as_float
from investment_panel.core.robinhood_options.collector import (
    DEFAULT_MAX_COLLECTION_SECONDS,
    DEFAULT_MAX_RESPONSE_BYTES,
    RobinhoodClient,
    RobinhoodMcpClient,
    batches as _batches,
    cursor_from_next as _cursor_from_next,
    payload_data as _payload_data,
    payload_list as _payload_list,
    option_quote_row,
)
from investment_panel.core.robinhood_options.auth import load_robinhood_access_token
from investment_panel.core.robinhood_options.contract_terms import attach_chain_metadata

# Robinhood can return a short ``results`` page even for otherwise valid quote
# requests.  Four total attempts keeps the retry budget below a second per
# batch while giving the final stragglers a chance to arrive before a snapshot
# is marked partial.
MAX_QUOTE_RESULT_ATTEMPTS = 4


def collect_robinhood_full_option_chain(
    config: RobinhoodConfig,
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
    groups: dict[str, list[dict[str, Any]]] = {}
    errors: list[str] = []
    for chain in _payload_list(client.get_option_chains(symbol), "chains"):
        chain_id = str(chain.get("id") or "")
        for expiry in chain.get("expiration_dates") or []:
            for option_type in ("call", "put"):
                try:
                    key = f"{chain_id}:{expiry}:{option_type}"
                    groups[key] = attach_chain_metadata(
                        _all_instruments(client, chain_id, str(expiry), option_type, deadline),
                        chain,
                        symbol,
                    )
                except Exception as exc:  # keep independently auditable partial captures
                    errors.append(f"{expiry}:{option_type}:{type(exc).__name__}: {exc}")
                if time.monotonic() > deadline:
                    errors.append("collection_timeout")
                    break
            if time.monotonic() > deadline:
                break
        if time.monotonic() > deadline:
            break
    groups = {
        key: [row for row in instruments if str(row.get("tradability") or "tradable").lower() == "tradable"]
        for key, instruments in groups.items()
    }
    by_id = {
        str(row.get("id")): (key, row)
        for key, instruments in groups.items()
        for row in instruments
        if row.get("id")
    }
    rows_by_id: dict[str, dict[str, Any]] = {}
    malformed_quote_ids: set[str] = set()
    unmatched_quote_results = 0
    quote_attempts = 0
    initial_quote_attempts = 0
    quote_batch_errors = 0
    group_diagnostics: dict[str, dict[str, Any]] = {}
    underlying_payload: dict[str, Any] = {}
    # Each expiry/type is a coherent capture group: get the underlying once,
    # then retrieve every quote batch before advancing to the next group.  Do
    # not attach a shared end timestamp after fetching the whole chain.
    for group_key, instruments in sorted(groups.items()):
        group_started_at = datetime.now(UTC)
        group_ids = {str(row.get("id")) for row in instruments if row.get("id")}
        diagnostic: dict[str, Any] = {
            "expected_contract_count": len(group_ids),
            "received_contract_count": 0,
            "started_at": group_started_at,
        }
        group_diagnostics[group_key] = diagnostic
        try:
            equity = _equity_quote(client, symbol, deadline)
            spot = as_float(equity.get("last_trade_price")) or as_float(equity.get("adjusted_mark_price"))
        except Exception as exc:
            errors.append(f"{group_key}:underlying:{type(exc).__name__}: {exc}")
            diagnostic["error"] = "underlying_quote_failed"
            diagnostic["finished_at"] = datetime.now(UTC)
            continue
        underlying_payload = underlying_payload or equity
        diagnostic["underlying_observed_at"] = equity.get("updated_at") or group_started_at
        for batch in _batches(sorted(group_ids), batch_size):
            pending = set(batch)
            for attempt in range(MAX_QUOTE_RESULT_ATTEMPTS):
                if not pending:
                    break
                if time.monotonic() > deadline:
                    errors.append("collection_timeout")
                    break
                quote_attempts += 1
                if attempt == 0:
                    initial_quote_attempts += 1
                try:
                    payload = client.get_option_quotes(sorted(pending))
                except Exception:
                    quote_batch_errors += 1
                    continue
                for result in _payload_list(payload, "results"):
                    quote = dict(result.get("quote") or {})
                    instrument_id = str(quote.get("instrument_id") or result.get("instrument_id") or "")
                    if instrument_id not in pending:
                        unmatched_quote_results += 1
                        continue
                    _row_group, instrument = by_id[instrument_id]
                    row = option_quote_row(instrument, quote)
                    if row is None:
                        malformed_quote_ids.add(instrument_id)
                        continue
                    row["underlying_symbol"] = symbol
                    row["underlying_price"] = spot
                    # Keep the unmodified provider payload alongside the normalized
                    # tradability state.  A later status-policy change can replay
                    # this evidence without guessing which provider value we saw.
                    row["provider_payload"] = {
                        "instrument": {
                            key: value for key, value in instrument.items() if not key.startswith("_")
                        },
                        "quote": quote,
                        "underlying": equity,
                        "provider_market_data_status": quote.get("market_data_status") or quote.get("data_status"),
                        "normalized_market_data_status": row.get("market_data_status"),
                    }
                    row["previous_close"] = row.get("close")
                    row["provider_updated_at"] = quote.get("updated_at")
                    row["provider_observed_at"] = quote.get("updated_at")
                    row["underlying_observed_at"] = equity.get("updated_at") or group_started_at
                    row["underlying_available_at"] = group_started_at
                    row["capture_group_key"] = group_key
                    row["group_started_at"] = group_started_at
                    rows_by_id[instrument_id] = row
                    pending.remove(instrument_id)
                    diagnostic["received_contract_count"] += 1
                if pending and attempt + 1 < MAX_QUOTE_RESULT_ATTEMPTS:
                    time.sleep(0.05 * (attempt + 1))
            if time.monotonic() > deadline:
                break
        group_finished_at = datetime.now(UTC)
        for instrument_id in group_ids:
            if instrument_id in rows_by_id:
                rows_by_id[instrument_id]["group_finished_at"] = group_finished_at
                rows_by_id[instrument_id]["available_at"] = group_finished_at
        diagnostic["finished_at"] = group_finished_at
        if time.monotonic() > deadline:
            break
    if malformed_quote_ids:
        errors.append(f"malformed_quote_results:{len(malformed_quote_ids)}")
    finished_at = datetime.now(UTC)
    expected = len(by_id)
    rows = list(rows_by_id.values())
    received = len(rows)
    missing_quote_count = max(0, expected - received)
    if missing_quote_count:
        errors.append(f"missing_quote_results:{missing_quote_count}")
    return {
        "symbol": symbol,
        "rows": rows,
        "expected_contract_count": expected,
        "received_contract_count": received,
        "completeness": (received / expected) if expected else 0.0,
        "errors": errors,
        "quote_diagnostics": {
            "attempts": quote_attempts,
            "retries": max(0, quote_attempts - initial_quote_attempts),
            "batch_errors": quote_batch_errors,
            "missing_quote_count": missing_quote_count,
            "malformed_quote_count": len(malformed_quote_ids),
            "unmatched_result_count": unmatched_quote_results,
            "groups": group_diagnostics,
        },
        "capture_started_at": started_at,
        "capture_finished_at": finished_at,
        "underlying_payload": underlying_payload,
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
