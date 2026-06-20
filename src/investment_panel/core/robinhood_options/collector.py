"""Read-only Robinhood option-chain collector for the options radar.

The collector talks to Robinhood through an MCP endpoint, but it normalizes the
result into the same ``store_options_chain`` row shape used by IBKR and free
sources. No account, order-review, order-placement, or cancellation tools are
called from this module. Authentication is delegated entirely to :mod:`auth`.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

import httpx

from investment_panel.core.coercion import to_finite_float as as_float
from investment_panel.core.coercion import to_int_or_none as as_int
from investment_panel.core.ibkr_options import select_leap_call_strikes, select_leap_put_strikes
from investment_panel.core.option_scan import RADAR_MAX_DTE, RADAR_MIN_DTE
from investment_panel.core.robinhood_options.auth import load_robinhood_access_token


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

    def __init__(self, url: str, *, auth_token: str | None = None, timeout_seconds: int = 30) -> None:
        self.url = url
        self.auth_token = auth_token
        self.timeout = timeout_seconds
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
        response = httpx.post(self.url, headers=headers, json=payload, timeout=self.timeout)
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


def collect_robinhood_option_chains(
    config: Any,
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

    observed_at = datetime.now(UTC).isoformat()
    result: dict[str, Any] = {
        "rows": {},
        "quotes": [],
        "errors": [],
        "observed_at": observed_at,
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
        )

    max_expiries = max(1, int(max_expiries if max_expiries is not None else getattr(config, "max_expiries", 2)))
    strikes_around_spot = max(1, int(strikes_around_spot if strikes_around_spot is not None else getattr(config, "strikes_around_spot", 12)))
    collect_puts = bool(collect_puts if collect_puts is not None else getattr(config, "collect_puts", False))
    quote_batch_size = max(1, min(20, int(quote_batch_size if quote_batch_size is not None else getattr(config, "quote_batch_size", 20))))
    near_term_dte = int(near_term_dte if near_term_dte is not None else getattr(config, "near_term_dte", 35))

    quote_rows = _fetch_equity_quotes(client, symbols)
    result["quotes"] = quote_rows
    spot_by_symbol = {str(row.get("symbol") or "").upper(): as_float(row.get("close")) for row in quote_rows}
    today = _observed_date(observed_at)
    for symbol in [s.upper() for s in symbols if s]:
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
            )
        except Exception as exc:  # noqa: BLE001 - keep the rest of the universe moving
            result["errors"].append(f"{symbol}:{exc}")
            continue
        if rows:
            result["rows"][symbol] = rows
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
    return {
        "expiry": str(expiry),
        "strike": strike,
        "type": option_type,
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "last": as_float(quote.get("last_trade_price")),
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
        "chain_symbol": instrument.get("chain_symbol"),
        "underlying_type": instrument.get("underlying_type"),
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
) -> list[dict[str, Any]]:
    chains = _payload_list(client.get_option_chains(symbol), "chains")
    rows: list[dict[str, Any]] = []
    for chain in chains:
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
                instruments = _fetch_instruments(client, chain_id=chain_id, expiration=expiry, option_type=option_type)
                selected = _select_instruments(instruments, spot, option_type=option_type, count=strikes_around_spot)
                rows.extend(_quote_instruments(client, selected, quote_batch_size=quote_batch_size))
    return rows


def _fetch_equity_quotes(client: RobinhoodClient, symbols: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for batch in _batches([s.upper() for s in symbols if s], 20):
        payload = client.get_equity_quotes(batch)
        for result in _payload_list(payload, "results"):
            quote = dict(result.get("quote") or {})
            symbol = str(quote.get("symbol") or "").upper()
            if not symbol:
                continue
            current = _latest_equity_price(quote)
            rows.append(
                {
                    "symbol": symbol,
                    "time": quote.get("venue_last_non_reg_trade_time") or quote.get("venue_last_trade_time"),
                    "close": current,
                    "change": _quote_change_pct(quote, current),
                    "change_abs": _quote_change_abs(quote, current),
                    "currency": "USD",
                    "source": "robinhood",
                    "raw": quote,
                }
            )
    return rows


def _fetch_instruments(client: RobinhoodClient, *, chain_id: str, expiration: str, option_type: str) -> list[dict[str, Any]]:
    instruments: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
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


def _quote_instruments(client: RobinhoodClient, instruments: list[dict[str, Any]], *, quote_batch_size: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    by_id = {str(row.get("id")): row for row in instruments if row.get("id")}
    for batch in _batches(list(by_id), quote_batch_size):
        payload = client.get_option_quotes(batch)
        for result in _payload_list(payload, "results"):
            quote = dict(result.get("quote") or {})
            instrument_id = str(quote.get("instrument_id") or result.get("instrument_id") or "")
            row = option_quote_row(by_id.get(instrument_id, {}), quote)
            if row:
                rows.append(row)
    return rows


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


def _latest_equity_price(quote: dict[str, Any]) -> float | None:
    regular = as_float(quote.get("last_trade_price"))
    extended = as_float(quote.get("last_non_reg_trade_price"))
    if extended is None:
        return regular
    if regular is None:
        return extended
    regular_ts = str(quote.get("venue_last_trade_time") or "")
    extended_ts = str(quote.get("venue_last_non_reg_trade_time") or "")
    return extended if extended_ts > regular_ts else regular


def _quote_change_abs(quote: dict[str, Any], current: float | None) -> float | None:
    previous = as_float(quote.get("adjusted_previous_close") or quote.get("previous_close"))
    return current - previous if current is not None and previous is not None else None


def _quote_change_pct(quote: dict[str, Any], current: float | None) -> float | None:
    previous = as_float(quote.get("adjusted_previous_close") or quote.get("previous_close"))
    if current is None or previous is None or previous == 0:
        return None
    return (current - previous) / previous * 100


def _observed_date(observed_at: str) -> date:
    return datetime.fromisoformat(observed_at.replace("Z", "+00:00")).date()


def _batches(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]
