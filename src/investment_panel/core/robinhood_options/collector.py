"""Read-only Robinhood option-chain collector for the options radar.

The collector talks to Robinhood through an MCP endpoint, but it normalizes the
result into the same ``store_options_chain`` row shape used by IBKR and free
sources. No account, order-review, order-placement, or cancellation tools are
called from this module. Authentication is delegated entirely to :mod:`auth`.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, date, datetime
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

import httpx

from investment_panel.core.config import RobinhoodConfig
from investment_panel.core.coercion import to_finite_float as as_float
from investment_panel.core.coercion import to_int_or_none as as_int
from investment_panel.core.ibkr_options import select_leap_call_strikes, select_leap_put_strikes
from investment_panel.core.option_scan import RADAR_MAX_DTE, RADAR_MIN_DTE
from investment_panel.core.robinhood_options.auth import load_robinhood_access_token
from investment_panel.core.robinhood_options.contract_terms import (
    attach_chain_metadata,
    verified_robinhood_contract_terms,
)


DEFAULT_MAX_COLLECTION_SECONDS = 600
DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def _fetch_equity_quotes(
    client: RobinhoodClient,
    symbols: list[str],
    *,
    deadline: float | None = None,
    regular_session_only: bool = False,
) -> list[dict[str, Any]]:
    """Fetch bounded equity quote batches through the existing client seam."""

    rows: list[dict[str, Any]] = []
    values = [symbol.upper() for symbol in symbols if symbol]
    for start in range(0, len(values), 20):
        if deadline is not None and time.monotonic() > deadline:
            return rows
        payload = client.get_equity_quotes(values[start : start + 20])
        for result in _payload_list(payload, "results"):
            quote = dict(result.get("quote") or {})
            symbol = str(quote.get("symbol") or "").upper()
            if not symbol:
                continue
            price = as_float(quote.get("last_trade_price")) if regular_session_only else _latest_equity_price(quote)
            observed_at = quote.get("venue_last_trade_time") if regular_session_only else (
                quote.get("venue_last_non_reg_trade_time") or quote.get("venue_last_trade_time")
            )
            rows.append({
                "symbol": symbol,
                "time": observed_at,
                "close": price,
                "option_spot": as_float(quote.get("last_trade_price")) or price,
                "change": _equity_change_pct(quote, price),
                "change_abs": _equity_change_abs(quote, price),
                "currency": "USD",
                "source": "robinhood",
                "raw": quote,
            })
    return rows


def _latest_equity_price(quote: dict[str, Any]) -> float | None:
    regular = as_float(quote.get("last_trade_price"))
    extended = as_float(quote.get("last_non_reg_trade_price"))
    if extended is None:
        return regular
    if regular is None:
        return extended
    return extended if str(quote.get("venue_last_non_reg_trade_time") or "") > str(quote.get("venue_last_trade_time") or "") else regular


def _equity_change_abs(quote: dict[str, Any], current: float | None) -> float | None:
    previous = as_float(quote.get("adjusted_previous_close") or quote.get("previous_close"))
    return current - previous if current is not None and previous is not None else None


def _equity_change_pct(quote: dict[str, Any], current: float | None) -> float | None:
    previous = as_float(quote.get("adjusted_previous_close") or quote.get("previous_close"))
    if current is None or previous is None or previous == 0:
        return None
    return (current - previous) / previous * 100


class RobinhoodClient(Protocol):
    def get_equity_quotes(self, symbols: list[str]) -> dict[str, Any]: ...

    def get_option_chains(self, underlying_symbol: str) -> dict[str, Any]: ...

    def get_option_instruments(
        self,
        *,
        chain_id: str | None = None,
        chain_symbol: str | None = None,
        expiration_dates: str | None = None,
        option_type: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]: ...

    def get_option_quotes(self, instrument_ids: list[str]) -> dict[str, Any]: ...


class RobinhoodMcpClient:
    """Minimal streamable-HTTP MCP client for the Robinhood trading server."""

    def __init__(
        self,
        url: str,
        *,
        auth_token: str | None = None,
        timeout_seconds: int = 30,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        deadline: float | None = None,
    ) -> None:
        self.url = url
        self.auth_token = auth_token
        self.timeout = timeout_seconds
        self.max_response_bytes = max(1024, int(max_response_bytes))
        self.deadline = deadline
        self._session_id: str | None = None
        self._next_id = 1
        self._initialized = False

    def get_equity_quotes(self, symbols: list[str]) -> dict[str, Any]:
        return self._call_tool("get_equity_quotes", {"symbols": symbols})

    def get_option_chains(self, underlying_symbol: str) -> dict[str, Any]:
        return self._call_tool("get_option_chains", {"underlying_symbol": underlying_symbol})

    def get_option_instruments(
        self,
        *,
        chain_id: str | None = None,
        chain_symbol: str | None = None,
        expiration_dates: str | None = None,
        option_type: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"tradability": "tradable"}
        if chain_id:
            args["chain_id"] = chain_id
        if chain_symbol:
            args["chain_symbol"] = chain_symbol
        if expiration_dates:
            args["expiration_dates"] = expiration_dates
        if option_type:
            args["type"] = option_type
        if cursor:
            args["cursor"] = cursor
        return self._call_tool("get_option_instruments", args)

    def get_option_quotes(self, instrument_ids: list[str]) -> dict[str, Any]:
        return self._call_tool("get_option_quotes", {"instrument_ids": instrument_ids})

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self._initialized:
            self._initialize()
        payload = self._request("tools/call", {"name": name, "arguments": arguments})
        return _extract_tool_payload(payload)

    def _initialize(self) -> None:
        self._request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "market-robinhood-provider", "version": "0.1.0"},
            },
        )
        try:
            self._request("notifications/initialized", None, expect_response=False)
        finally:
            self._initialized = True

    def _request(self, method: str, params: dict[str, Any] | None, *, expect_response: bool = True) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if expect_response:
            payload["id"] = request_id
        if params is not None:
            payload["params"] = params
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        response = self._post(payload, headers)
        response.raise_for_status()
        session_id = response.headers.get("Mcp-Session-Id") or response.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id
        if not expect_response or not response.content:
            return {}
        data = _decode_mcp_response(response)
        if data.get("error"):
            raise RuntimeError(data["error"])
        return dict(data.get("result") or data)

    def _post(self, payload: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
        request_seconds = max(1.0, float(self.timeout))
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Robinhood MCP collection deadline expired")
            request_seconds = min(request_seconds, remaining)
        deadline = time.monotonic() + request_seconds
        timeout = httpx.Timeout(
            timeout=max(0.1, request_seconds),
            connect=min(10.0, max(0.1, request_seconds)),
            read=min(10.0, max(0.1, request_seconds)),
            write=min(10.0, max(0.1, request_seconds)),
            pool=min(5.0, max(0.1, request_seconds)),
        )
        chunks: list[bytes] = []
        total = 0
        with httpx.stream("POST", self.url, headers=headers, json=payload, timeout=timeout) as response:
            for chunk in response.iter_bytes():
                if time.monotonic() > deadline:
                    raise TimeoutError(f"Robinhood MCP request timed out after {self.timeout}s")
                total += len(chunk)
                if total > self.max_response_bytes:
                    raise RuntimeError(
                        f"Robinhood MCP response exceeded {self.max_response_bytes} bytes for {payload.get('method')}"
                    )
                chunks.append(chunk)
            return httpx.Response(
                status_code=response.status_code,
                headers=response.headers,
                content=b"".join(chunks),
                request=response.request,
            )


def collect_robinhood_option_chains(
    config: RobinhoodConfig,
    symbols: list[str],
    *,
    client: RobinhoodClient | None = None,
    min_dte: int = RADAR_MIN_DTE,
    max_dte: int = RADAR_MAX_DTE,
    max_expiries: int | None = None,
    strikes_around_spot: int | None = None,
    collect_puts: bool | None = None,
    quote_batch_size: int | None = None,
    near_term_dte: int | None = None,
) -> dict[str, Any]:
    """Collect option rows from Robinhood for Market's radar universe."""

    collected_at = datetime.now(UTC).isoformat()
    result: dict[str, Any] = {
        "rows": {},
        "quotes": [],
        "errors": [],
        "observed_at": collected_at,
        "collected_at": collected_at,
        "market_data": "robinhood",
    }
    if not symbols:
        return result
    if client is None:
        token = load_robinhood_access_token(config)
        client = RobinhoodMcpClient(
            str(getattr(config, "mcp_url", "https://agent.robinhood.com/mcp/trading")),
            auth_token=token,
            timeout_seconds=int(getattr(config, "timeout_seconds", 30)),
            max_response_bytes=int(getattr(config, "max_response_bytes", DEFAULT_MAX_RESPONSE_BYTES)),
        )

    max_expiries = max(1, int(max_expiries if max_expiries is not None else getattr(config, "max_expiries", 2)))
    strikes_around_spot = max(1, int(strikes_around_spot if strikes_around_spot is not None else getattr(config, "strikes_around_spot", 12)))
    collect_puts = bool(collect_puts if collect_puts is not None else getattr(config, "collect_puts", False))
    quote_batch_size = max(1, min(20, int(quote_batch_size if quote_batch_size is not None else getattr(config, "quote_batch_size", 20))))
    near_term_dte = int(near_term_dte if near_term_dte is not None else getattr(config, "near_term_dte", 35))
    max_collection_seconds = max(1, int(getattr(config, "max_collection_seconds", DEFAULT_MAX_COLLECTION_SECONDS)))
    deadline = time.monotonic() + max_collection_seconds

    quote_rows = _fetch_equity_quotes(client, symbols, deadline=deadline)
    result["quotes"] = quote_rows
    spot_by_symbol = {
        str(row.get("symbol") or "").upper(): as_float(row.get("option_spot"))
        for row in quote_rows
    }
    today = _observed_date(collected_at)
    for symbol in [s.upper() for s in symbols if s]:
        if time.monotonic() > deadline:
            result["errors"].append(f"collection_timeout:exceeded {max_collection_seconds}s before {symbol}")
            result["timed_out"] = True
            break
        try:
            rows = _collect_symbol(
                client,
                symbol,
                spot_by_symbol.get(symbol),
                today=today,
                min_dte=min_dte,
                max_dte=max_dte,
                max_expiries=max_expiries,
                strikes_around_spot=strikes_around_spot,
                collect_puts=collect_puts,
                quote_batch_size=quote_batch_size,
                near_term_dte=near_term_dte,
                deadline=deadline,
            )
        except Exception as exc:  # noqa: BLE001 - keep the rest of the universe moving
            result["errors"].append(f"{symbol}:{exc}")
            continue
        if rows:
            result["rows"][symbol] = rows
        if time.monotonic() > deadline:
            result["errors"].append(f"collection_timeout:exceeded {max_collection_seconds}s after {symbol}")
            result["timed_out"] = True
            break
    effective_observed_at = _latest_option_quote_time(result["rows"])
    if effective_observed_at is not None:
        result["observed_at"] = effective_observed_at.isoformat()
    return result


def option_quote_row(instrument: dict[str, Any], quote: dict[str, Any]) -> dict[str, Any] | None:
    """Convert one Robinhood instrument+quote pair into ``store_options_chain`` shape."""

    expiry = instrument.get("expiration_date")
    strike = as_float(instrument.get("strike_price"))
    option_type = str(instrument.get("type") or "").lower()
    instrument_id = str(instrument.get("id") or quote.get("instrument_id") or "")
    if not expiry or strike is None or option_type not in {"call", "put"} or not instrument_id:
        return None
    bid = as_float(quote.get("bid_price"))
    ask = as_float(quote.get("ask_price"))
    mark = as_float(quote.get("mark_price") if quote.get("mark_price") is not None else quote.get("adjusted_mark_price"))
    mid = mark if mark is not None else ((bid + ask) / 2 if bid is not None and ask is not None else None)
    # Preserve Robinhood's native status when supplied.  The history analysis
    # owns the shared tradability policy (including ``live``); this capture
    # layer only supplies a stable normalized token for replay.  Older quote
    # payloads omit the field, and Robinhood's full-chain endpoint is live in
    # that case.
    provider_status = quote.get("market_data_status") or quote.get("data_status")
    normalized_status = str(provider_status).strip().lower() if provider_status is not None else "live"
    contract_terms = verified_robinhood_contract_terms(instrument)
    return {
        "expiry": str(expiry),
        "strike": strike,
        "type": option_type,
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "last": as_float(quote.get("last_trade_price")),
        "bid_size": as_int(quote.get("bid_size")),
        "ask_size": as_int(quote.get("ask_size")),
        "last_trade_at": quote.get("venue_last_trade_time"),
        "captured_at": quote.get("updated_at"),
        "market_data_status": normalized_status,
        "close": as_float(quote.get("previous_close_price")),
        "iv": as_float(quote.get("implied_volatility")),
        "delta": as_float(quote.get("delta")),
        "gamma": as_float(quote.get("gamma")),
        "theta": as_float(quote.get("theta")),
        "vega": as_float(quote.get("vega")),
        "rho": as_float(quote.get("rho")),
        "open_interest": as_int(quote.get("open_interest")),
        "volume": as_int(quote.get("volume")),
        "contract_symbol": instrument_id,
        "robinhood_instrument_id": instrument_id,
        "chain_id": instrument.get("chain_id"),
        "deliverable_key": contract_terms["deliverable_key"],
        "standard_contract_verified": contract_terms["standard_contract_verified"],
        "chain_symbol": instrument.get("chain_symbol"),
        "underlying_type": instrument.get("underlying_type"),
        "style": contract_terms["style"],
        "settlement": contract_terms["settlement"],
        "tradability": instrument.get("tradability"),
        "state": instrument.get("state"),
        "updated_at": quote.get("updated_at"),
        "previous_close_date": quote.get("previous_close_date"),
        "chance_of_profit_long": as_float(quote.get("chance_of_profit_long")),
        "chance_of_profit_short": as_float(quote.get("chance_of_profit_short")),
        "market_data": "robinhood",
    }


def select_robinhood_expiries(
    expiration_dates: list[str],
    *,
    today: date,
    min_dte: int,
    max_dte: int,
    max_per_symbol: int,
) -> list[str]:
    candidates: list[tuple[int, str]] = []
    for raw in expiration_dates:
        try:
            expiry = date.fromisoformat(str(raw)[:10])
        except (TypeError, ValueError):
            continue
        dte = (expiry - today).days
        if min_dte <= dte <= max_dte:
            candidates.append((dte, str(raw)[:10]))
    candidates.sort()
    return [expiry for _dte, expiry in candidates[:max_per_symbol]]


def select_near_term_expiry(
    expiration_dates: list[str],
    *,
    today: date,
    target_dte: int,
    min_dte: int = 14,
    max_dte: int = 90,
) -> str | None:
    """Pick the one expiry closest to ``target_dte`` within a near-term window.

    The radar pulls LEAP expiries; the watchlist's expected-move/skew read is far
    more useful from a near-term expiry, so we add a single ~monthly expiry."""

    candidates: list[tuple[int, str]] = []
    for raw in expiration_dates:
        try:
            dte = (date.fromisoformat(str(raw)[:10]) - today).days
        except (TypeError, ValueError):
            continue
        if min_dte <= dte <= max_dte:
            candidates.append((abs(dte - target_dte), str(raw)[:10]))
    if not candidates:
        return None
    return min(candidates)[1]


def _collect_symbol(
    client: RobinhoodClient,
    symbol: str,
    spot: float | None,
    *,
    today: date,
    min_dte: int,
    max_dte: int,
    max_expiries: int,
    strikes_around_spot: int,
    collect_puts: bool,
    quote_batch_size: int,
    near_term_dte: int = 0,
    deadline: float | None = None,
) -> list[dict[str, Any]]:
    chains = _payload_list(client.get_option_chains(symbol), "chains")
    rows: list[dict[str, Any]] = []
    for chain in chains:
        if _deadline_expired(deadline):
            return rows
        chain_id = str(chain.get("id") or "")
        expiration_dates = [str(expiry) for expiry in chain.get("expiration_dates") or []]
        expiries = select_robinhood_expiries(
            expiration_dates,
            today=today,
            min_dte=min_dte,
            max_dte=max_dte,
            max_per_symbol=max_expiries,
        )
        if near_term_dte > 0:
            near_term = select_near_term_expiry(expiration_dates, today=today, target_dte=near_term_dte)
            if near_term and near_term not in expiries:
                expiries = [near_term, *expiries]
        for expiry in expiries:
            for option_type in (["call", "put"] if collect_puts else ["call"]):
                if _deadline_expired(deadline):
                    return rows
                instruments = attach_chain_metadata(
                    _fetch_instruments(
                        client, chain_id=chain_id, expiration=expiry,
                        option_type=option_type, deadline=deadline,
                    ),
                    chain,
                    symbol,
                )
                selected = _select_instruments(instruments, spot, option_type=option_type, count=strikes_around_spot)
                quoted = _quote_instruments(client, selected, quote_batch_size=quote_batch_size, deadline=deadline)
                for row in quoted:
                    row["underlying_price"] = spot
                rows.extend(quoted)
    return rows


def _latest_option_quote_time(rows_by_symbol: dict[str, list[dict[str, Any]]]) -> datetime | None:
    observed: list[datetime] = []
    for rows in rows_by_symbol.values():
        for row in rows:
            value = row.get("updated_at")
            if not value:
                continue
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                continue
            observed.append(parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC))
    return max(observed, default=None)


def collect_robinhood_equity_quotes(
    config: RobinhoodConfig,
    symbols: list[str],
    *,
    client: RobinhoodClient | None = None,
    deadline: float | None = None,
    regular_session_only: bool = False,
) -> dict[str, Any]:
    """Collect the lightweight 20-symbol equity batches used by recovery.

    Unlike the option-chain collector this intentionally performs no chain or
    instrument call.  Rows without a provider timestamp are returned as
    explicitly unconfirmed rather than being stamped with local wall-clock
    time, so a provider outage cannot become a fresh-looking price fact.
    """

    requested = list(dict.fromkeys(str(symbol).upper() for symbol in symbols if str(symbol).strip()))
    if client is None:
        token = load_robinhood_access_token(config)
        client = RobinhoodMcpClient(
            str(getattr(config, "mcp_url", "https://agent.robinhood.com/mcp/trading")),
            auth_token=token,
            timeout_seconds=int(getattr(config, "timeout_seconds", 30)),
            max_response_bytes=int(getattr(config, "max_response_bytes", DEFAULT_MAX_RESPONSE_BYTES)),
            deadline=deadline,
        )
    try:
        fetched = _fetch_equity_quotes(
            client, requested, deadline=deadline, regular_session_only=regular_session_only,
        )
    except Exception as exc:
        return {"rows": [], "requested_symbols": requested, "received_symbols": [], "errors": [f"provider_error:{type(exc).__name__}:{exc}"]}
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    provider_symbols: list[str] = []
    received: list[str] = []
    for source in fetched:
        symbol = str(source.get("symbol") or "").upper()
        if not symbol:
            continue
        provider_symbols.append(symbol)
        raw = dict(source.get("raw") or {})
        observed_at = _provider_quote_time(source.get("time"), raw)
        price = as_float(source.get("close"))
        if observed_at is None:
            errors.append(f"{symbol}:provider_timestamp_missing")
            continue
        if price is None or price <= 0:
            errors.append(f"{symbol}:non_positive_quote")
            continue
        received.append(symbol)
        rows.append({
            "symbol": symbol,
            "observed_at": observed_at,
            "price": price,
            "change_abs": as_float(source.get("change_abs")),
            "change_pct": as_float(source.get("change")),
            "currency": str(source.get("currency") or "USD"),
            "asset_class": "equity",
            "provider_payload": raw,
        })
    missing = sorted(set(requested) - set(provider_symbols))
    errors.extend(f"{symbol}:missing_provider_quote" for symbol in missing)
    if _deadline_expired(deadline):
        errors.append("collector_deadline_exceeded")
    return {
        "rows": rows,
        "requested_symbols": requested,
        # These are valid, timestamped, positive-price provider facts—not
        # merely symbols which appeared in a malformed payload.
        "received_symbols": sorted(set(received)),
        "errors": errors,
    }


def _provider_quote_time(primary: Any, raw: dict[str, Any]) -> datetime | None:
    candidates = (
        primary,
        raw.get("updated_at"), raw.get("last_trade_price_timestamp"),
        raw.get("venue_last_trade_time"), raw.get("venue_last_non_reg_trade_time"),
    )
    for value in candidates:
        if not value:
            continue
        if isinstance(value, datetime):
            return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            continue
        return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _fetch_instruments(
    client: RobinhoodClient,
    *,
    chain_id: str,
    expiration: str,
    option_type: str,
    deadline: float | None = None,
) -> list[dict[str, Any]]:
    instruments: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        if _deadline_expired(deadline):
            return instruments
        payload = client.get_option_instruments(
            chain_id=chain_id,
            expiration_dates=expiration,
            option_type=option_type,
            cursor=cursor,
        )
        instruments.extend(dict(row) for row in _payload_list(payload, "instruments"))
        next_url = _payload_data(payload).get("next")
        cursor = _cursor_from_next(next_url)
        if not cursor:
            return instruments


def _select_instruments(instruments: list[dict[str, Any]], spot: float | None, *, option_type: str, count: int) -> list[dict[str, Any]]:
    by_strike = {as_float(row.get("strike_price")): row for row in instruments if as_float(row.get("strike_price")) is not None}
    strikes = [strike for strike in by_strike if strike is not None]
    if option_type == "put":
        selected = select_leap_put_strikes(strikes, spot, count)
    else:
        selected = select_leap_call_strikes(strikes, spot, count)
    return [by_strike[strike] for strike in selected if strike in by_strike]


def _quote_instruments(
    client: RobinhoodClient,
    instruments: list[dict[str, Any]],
    *,
    quote_batch_size: int,
    deadline: float | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    by_id = {str(row.get("id")): row for row in instruments if row.get("id")}
    for batch in _batches(list(by_id), quote_batch_size):
        if _deadline_expired(deadline):
            return rows
        payload = client.get_option_quotes(batch)
        for result in _payload_list(payload, "results"):
            quote = dict(result.get("quote") or {})
            instrument_id = str(quote.get("instrument_id") or result.get("instrument_id") or "")
            row = option_quote_row(by_id.get(instrument_id, {}), quote)
            if row:
                rows.append(row)
    return rows


def _deadline_expired(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() > deadline


def _extract_tool_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if "structuredContent" in payload and isinstance(payload["structuredContent"], dict):
        return dict(payload["structuredContent"])
    if "data" in payload:
        return payload
    content = payload.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "text":
                continue
            text = item.get("text")
            if not isinstance(text, str):
                continue
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                return decoded
    return payload


def _decode_mcp_response(response: httpx.Response) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" not in content_type:
        decoded = response.json()
        return dict(decoded) if isinstance(decoded, dict) else {}
    for line in response.text.splitlines():
        if not line.startswith("data:"):
            continue
        data = line.removeprefix("data:").strip()
        if not data:
            continue
        decoded = json.loads(data)
        if isinstance(decoded, dict):
            return decoded
    return {}


def _payload_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    return dict(data) if isinstance(data, dict) else payload


def _payload_list(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    data = _payload_data(payload)
    rows = data.get(key)
    return [dict(row) for row in rows] if isinstance(rows, list) else []


def _cursor_from_next(next_url: Any) -> str | None:
    if not next_url:
        return None
    parsed = urlparse(str(next_url))
    values = parse_qs(parsed.query).get("cursor")
    return values[0] if values else None


def _observed_date(observed_at: str) -> date:
    return datetime.fromisoformat(observed_at.replace("Z", "+00:00")).date()

def _batches(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


batches = _batches
cursor_from_next = _cursor_from_next
payload_data = _payload_data
payload_list = _payload_list


__all__ = [
    "RobinhoodClient", "RobinhoodMcpClient", "batches", "cursor_from_next",
    "option_quote_row", "payload_data", "payload_list",
]
