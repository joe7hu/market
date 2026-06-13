"""Persist option (and synthetic spread) snapshots from ingested chains."""

from __future__ import annotations

import math
from typing import Any

from investment_panel.core.db import (json_dumps, query_rows)
from investment_panel.core.options_radar.coerce import (_coalesce_number, _days_to_expiration, _integer, _iso, _json, _normalize_symbol, _number)
from investment_panel.core.options_radar.constants import (DEFAULT_OPTION_RISK_FREE_RATE)
from investment_panel.core.options_radar.dbutil import (_contract_id, _source_filter, _symbol_filter)
from investment_panel.core.options_radar.greeks import (_option_model_dte, _option_model_iv, _resolve_option_greeks)
from investment_panel.core.options_radar.indicators import (_premium_mid, _spread_pct)

def persist_option_snapshots(
    con: Any,
    symbols: list[str] | None = None,
    *,
    source: str | None = None,
    snapshot_time: str | None = None,
) -> int:
    """Copy raw chain rows into the event-sourced radar snapshot table."""

    symbol_filter = _symbol_filter(symbols, table_alias="oc")
    source_filter = _source_filter(source, table_alias="oc")
    observed_filter = "AND oc.observed_at = TRY_CAST(? AS TIMESTAMP)" if snapshot_time else ""
    params: list[Any] = [*source_filter["params"], *symbol_filter["params"]]
    if snapshot_time:
        params.append(snapshot_time)
    rows = query_rows(
        con,
        f"""
        SELECT
            oc.symbol,
            oc.expiry,
            oc.strike,
            oc.option_type,
            oc.bid,
            oc.ask,
            oc.mid,
            oc.iv,
            oc.delta,
            oc.gamma,
            oc.theta,
            oc.vega,
            oc.contract_symbol,
            oc.observed_at,
            oc.source,
            oc.raw,
            (
                SELECT tv.delta
                FROM options_chain tv
                WHERE tv.symbol = oc.symbol
                  AND tv.expiry = oc.expiry
                  AND tv.strike = oc.strike
                  AND tv.option_type = oc.option_type
                  AND tv.source = 'tradingview'
                  AND tv.observed_at <= oc.observed_at
                  AND tv.delta IS NOT NULL
                ORDER BY tv.observed_at DESC
                LIMIT 1
            ) AS tradingview_delta,
            (
                SELECT tv.gamma
                FROM options_chain tv
                WHERE tv.symbol = oc.symbol
                  AND tv.expiry = oc.expiry
                  AND tv.strike = oc.strike
                  AND tv.option_type = oc.option_type
                  AND tv.source = 'tradingview'
                  AND tv.observed_at <= oc.observed_at
                  AND tv.gamma IS NOT NULL
                ORDER BY tv.observed_at DESC
                LIMIT 1
            ) AS tradingview_gamma,
            (
                SELECT tv.theta
                FROM options_chain tv
                WHERE tv.symbol = oc.symbol
                  AND tv.expiry = oc.expiry
                  AND tv.strike = oc.strike
                  AND tv.option_type = oc.option_type
                  AND tv.source = 'tradingview'
                  AND tv.observed_at <= oc.observed_at
                  AND tv.theta IS NOT NULL
                ORDER BY tv.observed_at DESC
                LIMIT 1
            ) AS tradingview_theta,
            (
                SELECT tv.vega
                FROM options_chain tv
                WHERE tv.symbol = oc.symbol
                  AND tv.expiry = oc.expiry
                  AND tv.strike = oc.strike
                  AND tv.option_type = oc.option_type
                  AND tv.source = 'tradingview'
                  AND tv.observed_at <= oc.observed_at
                  AND tv.vega IS NOT NULL
                ORDER BY tv.observed_at DESC
                LIMIT 1
            ) AS tradingview_vega,
            (
                SELECT q.price
                FROM quotes_intraday q
                WHERE q.symbol = oc.symbol AND q.observed_at <= oc.observed_at
                ORDER BY q.observed_at DESC
                LIMIT 1
            ) AS underlying_price
        FROM options_chain oc
        WHERE 1 = 1 {source_filter["sql"]} {symbol_filter["sql"]} {observed_filter}
        ORDER BY oc.observed_at, oc.symbol, oc.expiry, oc.strike, oc.option_type
        """,
        params,
    )
    count = 0
    for row in rows:
        raw = _json(row.get("raw"))
        ticker = _normalize_symbol(row.get("symbol"))
        snapshot_at = _iso(row.get("observed_at"))
        expiration = row.get("expiry")
        strike = _number(row.get("strike"))
        option_type = str(row.get("option_type") or raw.get("type") or "").lower()
        mid = _premium_mid(row, raw)
        bid = _number(row.get("bid"))
        ask = _number(row.get("ask"))
        contract_id = _contract_id(ticker, expiration, strike, option_type, row.get("contract_symbol") or raw.get("symbol"))
        data_source = str(row.get("source") or source or "unknown")
        underlying_price = _number(row.get("underlying_price"))
        iv = _number(row.get("iv"))
        dte = _integer(raw.get("dte")) or _days_to_expiration(expiration, snapshot_at)
        greek_resolution = _resolve_option_greeks(row, option_type=option_type, underlying_price=underlying_price, strike=strike, dte=dte, iv=iv)
        if greek_resolution["source"] != "provider":
            raw["greeks_source"] = greek_resolution["source"]
            if greek_resolution["source"] in {"black_scholes_model", "mixed_fallback"}:
                raw["greeks_model"] = {
                    "method": "black_scholes_from_iv",
                    "risk_free_rate": DEFAULT_OPTION_RISK_FREE_RATE,
                    "iv": iv,
                    "dte": dte,
                    "effective_iv": _option_model_iv(iv),
                    "effective_dte": _option_model_dte(dte),
                }
        con.execute(
            """
            INSERT OR REPLACE INTO option_snapshot
            (snapshot_time, ticker, underlying_price, expiration, strike, option_type, bid, ask, mid,
             last, volume, open_interest, iv, delta, gamma, theta, vega, dte, spread_pct,
             data_source, contract_id, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                snapshot_at,
                ticker,
                underlying_price,
                expiration,
                strike,
                option_type,
                bid,
                ask,
                mid,
                _coalesce_number(raw, "last", "last_price", "close"),
                _coalesce_number(raw, "volume", "vol"),
                _coalesce_number(raw, "open_interest", "openInterest", "oi"),
                iv,
                greek_resolution["delta"],
                greek_resolution["gamma"],
                greek_resolution["theta"],
                greek_resolution["vega"],
                dte,
                _spread_pct(bid, ask, mid),
                data_source,
                contract_id,
                json_dumps(raw),
            ],
        )
        count += 1
    return count


def _spread_contract_id(ticker: str, expiration: Any, long_strike: float, short_strike: float) -> str:
    """Deterministic id for a synthetic call debit spread. Stable across refreshes so the
    mark/shadow-trade pipeline (which re-reads option_snapshot by contract_id over later
    snapshot_times) re-prices the same structure from its legs."""

    return f"{ticker}:{expiration}:{long_strike:g}-{short_strike:g}:call_spread"


def _net_leg(long_value: Any, short_value: Any) -> float | None:
    """Net of a long-minus-short leg attribute (premium or greek). ``None`` if either
    side is missing so we never fabricate a half-known net."""

    long_num = _number(long_value)
    short_num = _number(short_value)
    if long_num is None or short_num is None:
        return None
    return long_num - short_num


def build_spread_snapshot_row(long_leg: dict[str, Any], short_leg: dict[str, Any]) -> dict[str, Any] | None:
    """Synthesize a call debit spread as a single option_snapshot row priced at the net
    debit (long_mid - short_mid). Greeks are netted (long - short); the synthetic strike is
    the long strike and the synthetic option_type is ``call_spread``. Returns ``None`` when
    the structure is not a real debit (mid <= 0) or the net mid is unpriceable."""

    long_mid = _number(long_leg.get("mid"))
    short_mid = _number(short_leg.get("mid"))
    if long_mid is None or short_mid is None:
        return None
    net_debit = long_mid - short_mid
    if net_debit <= 0:
        return None
    long_strike = _number(long_leg.get("strike"))
    short_strike = _number(short_leg.get("strike"))
    if long_strike is None or short_strike is None or short_strike <= long_strike:
        return None
    ticker = _normalize_symbol(long_leg.get("ticker"))
    expiration = long_leg.get("expiration")
    snapshot_time = _iso(long_leg.get("snapshot_time"))
    net_bid = _net_leg(long_leg.get("bid"), short_leg.get("ask"))
    if net_bid is not None:
        net_bid = max(0.0, net_bid)
    net_ask = _net_leg(long_leg.get("ask"), short_leg.get("bid"))
    long_volume = _number(long_leg.get("volume"))
    short_volume = _number(short_leg.get("volume"))
    net_volume = min(long_volume, short_volume) if long_volume is not None and short_volume is not None else None
    long_oi = _number(long_leg.get("open_interest"))
    short_oi = _number(short_leg.get("open_interest"))
    net_oi = min(long_oi, short_oi) if long_oi is not None and short_oi is not None else None
    contract_id = _spread_contract_id(ticker, expiration, long_strike, short_strike)
    raw = {
        "structure": "call_debit_spread",
        "long_strike": long_strike,
        "short_strike": short_strike,
        "width": short_strike - long_strike,
        "long_mid": long_mid,
        "short_mid": short_mid,
        "net_debit": net_debit,
        "long_contract_id": str(long_leg.get("contract_id")),
        "short_contract_id": str(short_leg.get("contract_id")),
    }
    return {
        "snapshot_time": snapshot_time,
        "ticker": ticker,
        "underlying_price": _number(long_leg.get("underlying_price")),
        "expiration": expiration,
        "strike": long_strike,
        "option_type": "call_spread",
        "bid": net_bid,
        "ask": net_ask,
        "mid": net_debit,
        "last": _net_leg(long_leg.get("last"), short_leg.get("last")),
        "volume": net_volume,
        "open_interest": net_oi,
        "iv": _number(long_leg.get("iv")),
        "delta": _net_leg(long_leg.get("delta"), short_leg.get("delta")),
        "gamma": _net_leg(long_leg.get("gamma"), short_leg.get("gamma")),
        "theta": _net_leg(long_leg.get("theta"), short_leg.get("theta")),
        "vega": _net_leg(long_leg.get("vega"), short_leg.get("vega")),
        "dte": _integer(long_leg.get("dte")),
        "spread_pct": _spread_pct(net_bid, net_ask, net_debit),
        "data_source": str(long_leg.get("data_source") or "unknown"),
        "contract_id": contract_id,
        "raw": raw,
    }


def persist_spread_snapshots(
    con: Any,
    symbols: list[str] | None = None,
    *,
    source: str | None = None,
    snapshot_time: str | None = None,
) -> int:
    """Synthesize call debit spread rows into option_snapshot from the single-leg call
    snapshots persist_option_snapshots just wrote. For each (snapshot_time, ticker,
    data_source, expiration) cohort of calls, pairs consecutive strikes into a long-lower /
    short-higher debit vertical. Idempotent (INSERT OR REPLACE on the deterministic
    contract_id) — re-running a snapshot re-prices the same spreads."""

    symbol_filter = _symbol_filter(symbols, table_alias="s", column="ticker")
    source_filter = _source_filter(source, table_alias="s", column="data_source")
    observed_filter = "AND s.snapshot_time = TRY_CAST(? AS TIMESTAMP)" if snapshot_time else ""
    params: list[Any] = [*source_filter["params"], *symbol_filter["params"]]
    if snapshot_time:
        params.append(snapshot_time)
    rows = query_rows(
        con,
        f"""
        SELECT *
        FROM option_snapshot s
        WHERE s.option_type = 'call' {source_filter["sql"]} {symbol_filter["sql"]} {observed_filter}
        ORDER BY s.snapshot_time, s.ticker, s.data_source, s.expiration, s.strike
        """,
        params,
    )
    cohorts: dict[tuple[Any, Any, Any, Any], list[dict[str, Any]]] = {}
    for row in rows:
        key = (row.get("snapshot_time"), row.get("ticker"), row.get("data_source"), row.get("expiration"))
        cohorts.setdefault(key, []).append(row)
    count = 0
    for legs in cohorts.values():
        legs.sort(key=lambda r: _number(r.get("strike")) if _number(r.get("strike")) is not None else math.inf)
        for long_leg, short_leg in zip(legs, legs[1:]):
            spread = build_spread_snapshot_row(long_leg, short_leg)
            if spread is None:
                continue
            con.execute(
                """
                INSERT OR REPLACE INTO option_snapshot
                (snapshot_time, ticker, underlying_price, expiration, strike, option_type, bid, ask, mid,
                 last, volume, open_interest, iv, delta, gamma, theta, vega, dte, spread_pct,
                 data_source, contract_id, raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    spread["snapshot_time"],
                    spread["ticker"],
                    spread["underlying_price"],
                    spread["expiration"],
                    spread["strike"],
                    spread["option_type"],
                    spread["bid"],
                    spread["ask"],
                    spread["mid"],
                    spread["last"],
                    spread["volume"],
                    spread["open_interest"],
                    spread["iv"],
                    spread["delta"],
                    spread["gamma"],
                    spread["theta"],
                    spread["vega"],
                    spread["dte"],
                    spread["spread_pct"],
                    spread["data_source"],
                    spread["contract_id"],
                    json_dumps(spread["raw"]),
                ],
            )
            count += 1
    return count
