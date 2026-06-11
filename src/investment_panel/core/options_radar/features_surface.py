"""Vol-surface features and per-underlying stock features."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from investment_panel.core.db import (json_dumps, query_rows)
from investment_panel.core.options_intelligence import _atm_strike as _surface_atm_strike, _closest_by_delta as _surface_closest_by_delta
from investment_panel.core.options_radar.coerce import (_average, _coalesce_number, _integer, _iso, _json, _normalize_symbol, _number)
from investment_panel.core.options_radar.dbutil import (_source_filter, _symbol_filter)
from investment_panel.core.options_radar.indicators import (_atr_pct, _base_length_days, _realized_vol, _relative_strength, _volume_ratio)

def _expiry_atm_iv_and_skew(chain_rows: list[dict[str, Any]], spot: float | None) -> tuple[float | None, float | None]:
    """ATM IV and 25-delta put-call IV skew for one expiry, reusing the
    options_intelligence skew helpers (no parallel skew system)."""

    rows = [r for r in chain_rows if _number(r.get("strike")) is not None]
    if not rows:
        return None, None
    atm_strike = _surface_atm_strike(rows, spot)
    if atm_strike is None:
        return None, None
    atm_iv = _average([_number(r.get("iv")) for r in rows if _number(r.get("strike")) == atm_strike])
    calls = [r for r in rows if str(r.get("option_type") or "").lower() == "call"]
    puts = [r for r in rows if str(r.get("option_type") or "").lower() == "put"]
    call_25 = _surface_closest_by_delta(calls, 0.25)
    put_25 = _surface_closest_by_delta(puts, -0.25)
    call_iv = _number((call_25 or {}).get("iv"))
    put_iv = _number((put_25 or {}).get("iv"))
    skew = round(put_iv - call_iv, 6) if call_iv is not None and put_iv is not None else None
    return atm_iv, skew


def _iv_percentile_252d(value: float | None, history: list[float | None]) -> tuple[float | None, str]:
    """Percentile of ATM-IV at matched (leap) tenor over trailing 252 observations.

    Fixes the old cross-sectional pool (mixed strikes/expiries). Falls back to no
    percentile until >=20 observations accrue — the candidate keeps its existing
    cross-sectional iv_percentile in the meantime."""

    if value is None:
        return None, "unavailable"
    hist = [h for h in history if h is not None][-252:]
    if len(hist) < 20:
        return None, "insufficient_history"
    pct = sum(1 for h in hist if h <= value) / len(hist) * 100
    return round(pct, 2), "matched_tenor_252d"


def build_vol_surface_feature(
    ticker: str,
    snapshot_time: str,
    per_expiry: list[tuple[int | None, float | None, float | None]],
    *,
    rv_20d: float | None,
    rv_60d: float | None,
    iv_leap_history: list[float | None],
    skew_5d_ago: float | None,
) -> dict[str, Any] | None:
    """Vol-surface features from per-expiry (dte, atm_iv, skew_25d) tuples.

    Term slope < 0 = inverted front (event anticipation); negative put-call skew =
    call/upside demand. ``iv_rv_ratio`` is the cheap-convexity test (IV vs realized).
    """

    usable = [(dte, iv, sk) for dte, iv, sk in per_expiry if dte is not None]
    if not usable:
        return None

    def _nearest_iv(target: int) -> float | None:
        cands = [(abs(dte - target), iv) for dte, iv, _sk in usable if iv is not None]
        return min(cands)[1] if cands else None

    atm_iv_30d = _nearest_iv(30)
    atm_iv_90d = _nearest_iv(90)
    leap_iv = sorted([(dte, iv) for dte, iv, _sk in usable if iv is not None and dte >= 300], reverse=True)
    atm_iv_leap = leap_iv[0][1] if leap_iv else _nearest_iv(365)
    term_slope = round(atm_iv_leap - atm_iv_30d, 6) if atm_iv_leap is not None and atm_iv_30d is not None else None
    leap_skew_rows = sorted([(dte, sk) for dte, _iv, sk in usable if sk is not None], reverse=True)
    put_call_skew_25d = leap_skew_rows[0][1] if leap_skew_rows else None
    skew_change_5d = round(put_call_skew_25d - skew_5d_ago, 6) if put_call_skew_25d is not None and skew_5d_ago is not None else None
    iv_rv_ratio = round(atm_iv_leap / rv_60d, 4) if atm_iv_leap is not None and rv_60d and rv_60d > 0 else None
    iv_percentile_252d, basis = _iv_percentile_252d(atm_iv_leap, iv_leap_history)
    return {
        "snapshot_time": snapshot_time,
        "ticker": _normalize_symbol(ticker),
        "atm_iv_30d": atm_iv_30d,
        "atm_iv_90d": atm_iv_90d,
        "atm_iv_leap": atm_iv_leap,
        "term_slope": term_slope,
        "put_call_skew_25d": put_call_skew_25d,
        "skew_change_5d": skew_change_5d,
        "rv_20d": rv_20d,
        "rv_60d": rv_60d,
        "iv_rv_ratio": iv_rv_ratio,
        "iv_percentile_252d": iv_percentile_252d,
        "iv_percentile_basis": basis,
        "raw": {"expiries": len(usable)},
    }


def refresh_vol_surface_features(con: Any, symbols: list[str] | None = None, *, source: str | None = None) -> int:
    """Materialize ``vol_surface_features`` (term structure + skew + IV/RV) per ticker
    from the latest option snapshot batch across the collected expiries."""

    symbol_filter = _symbol_filter(symbols, table_alias="s", column="ticker")
    source_filter = _source_filter(source, table_alias="s", column="data_source")
    rows = query_rows(
        con,
        f"""
        SELECT s.ticker, s.snapshot_time, s.expiration, s.strike, s.option_type,
               s.iv, s.delta, s.dte, s.underlying_price
        FROM option_snapshot s
        WHERE 1 = 1 {source_filter["sql"]} {symbol_filter["sql"]}
        QUALIFY dense_rank() OVER (PARTITION BY s.ticker ORDER BY s.snapshot_time DESC) = 1
        """,
        [*source_filter["params"], *symbol_filter["params"]],
    )
    by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_ticker[_normalize_symbol(row.get("ticker"))].append(row)

    count = 0
    for ticker, ticker_rows in by_ticker.items():
        snapshot_time = _iso(max(str(r.get("snapshot_time")) for r in ticker_rows))
        spot = _coalesce_number(ticker_rows[0], "underlying_price")
        by_expiry: dict[str, list[dict[str, Any]]] = defaultdict(list)
        dte_of: dict[str, int | None] = {}
        for row in ticker_rows:
            exp = str(row.get("expiration"))
            by_expiry[exp].append(row)
            dte_of[exp] = _integer(row.get("dte"))
        per_expiry: list[tuple[int | None, float | None, float | None]] = []
        for exp, chain_rows in by_expiry.items():
            atm_iv, skew = _expiry_atm_iv_and_skew(chain_rows, spot)
            per_expiry.append((dte_of.get(exp), atm_iv, skew))

        stock_raw = _latest_stock_features_raw(con, ticker)
        rv_20d = _number(stock_raw.get("rv_20d"))
        rv_60d = _number(stock_raw.get("rv_60d"))
        history = query_rows(
            con,
            """
            SELECT atm_iv_leap, put_call_skew_25d
            FROM vol_surface_features
            WHERE ticker = ? AND snapshot_time < ?
            ORDER BY snapshot_time
            """,
            [ticker, snapshot_time],
        )
        iv_leap_history = [_number(h.get("atm_iv_leap")) for h in history]
        skew_history = [_number(h.get("put_call_skew_25d")) for h in history]
        skew_5d_ago = skew_history[-5] if len(skew_history) >= 5 else (skew_history[0] if skew_history else None)

        feature = build_vol_surface_feature(
            ticker,
            snapshot_time,
            per_expiry,
            rv_20d=rv_20d,
            rv_60d=rv_60d,
            iv_leap_history=iv_leap_history,
            skew_5d_ago=skew_5d_ago,
        )
        if not feature:
            continue
        con.execute(
            """
            INSERT OR REPLACE INTO vol_surface_features
            (snapshot_time, ticker, atm_iv_30d, atm_iv_90d, atm_iv_leap, term_slope,
             put_call_skew_25d, skew_change_5d, rv_20d, rv_60d, iv_rv_ratio,
             iv_percentile_252d, iv_percentile_basis, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                feature["snapshot_time"],
                feature["ticker"],
                feature["atm_iv_30d"],
                feature["atm_iv_90d"],
                feature["atm_iv_leap"],
                feature["term_slope"],
                feature["put_call_skew_25d"],
                feature["skew_change_5d"],
                feature["rv_20d"],
                feature["rv_60d"],
                feature["iv_rv_ratio"],
                feature["iv_percentile_252d"],
                feature["iv_percentile_basis"],
                json_dumps(feature["raw"]),
            ],
        )
        count += 1
    return count


def _latest_stock_features_raw(con: Any, ticker: str) -> dict[str, Any]:
    rows = query_rows(
        con,
        "SELECT raw FROM stock_features WHERE ticker = ? ORDER BY snapshot_time DESC LIMIT 1",
        [_normalize_symbol(ticker)],
    )
    return _json(rows[0].get("raw")) if rows else {}


def refresh_stock_features_for_option_snapshots(con: Any, symbols: list[str] | None = None, *, source: str | None = None) -> int:
    symbol_filter = _symbol_filter(symbols, table_alias="s", column="ticker")
    source_filter = _source_filter(source, table_alias="s", column="data_source")
    rows = query_rows(
        con,
        f"""
        SELECT DISTINCT s.ticker, s.snapshot_time
        FROM option_snapshot s
        WHERE 1 = 1 {source_filter["sql"]} {symbol_filter["sql"]}
        ORDER BY s.snapshot_time, s.ticker
        """,
        [*source_filter["params"], *symbol_filter["params"]],
    )
    count = 0
    for row in rows:
        if compute_stock_feature(con, str(row["ticker"]), _iso(row["snapshot_time"])):
            count += 1
    return count


def compute_stock_feature(con: Any, ticker: str, snapshot_time: str) -> dict[str, Any] | None:
    ticker = _normalize_symbol(ticker)
    prices = query_rows(
        con,
        """
        SELECT date, open, high, low, close, volume
        FROM prices_daily
        WHERE symbol = ? AND date <= TRY_CAST(? AS DATE)
        ORDER BY date
        """,
        [ticker, snapshot_time],
    )
    if not prices:
        return None
    qqq_prices = query_rows(
        con,
        """
        SELECT date, close
        FROM prices_daily
        WHERE symbol = 'QQQ' AND date <= TRY_CAST(? AS DATE)
        ORDER BY date
        """,
        [snapshot_time],
    )
    closes = [_number(row.get("close")) for row in prices]
    highs = [_number(row.get("high")) for row in prices]
    lows = [_number(row.get("low")) for row in prices]
    volumes = [_number(row.get("volume")) for row in prices]
    close_values = [value for value in closes if value is not None]
    if not close_values:
        return None
    price = close_values[-1]
    high_values = [value for value in highs if value is not None]
    high_252 = max(high_values[-252:]) if high_values else price
    feature = {
        "snapshot_time": snapshot_time,
        "ticker": ticker,
        "price": price,
        "ma_20": _average(close_values[-20:]) if len(close_values) >= 20 else None,
        "ma_50": _average(close_values[-50:]) if len(close_values) >= 50 else None,
        "ma_200": _average(close_values[-200:]) if len(close_values) >= 200 else None,
        "rs_vs_qqq_20d": _relative_strength(close_values, [_number(row.get("close")) for row in qqq_prices], 20),
        "rs_vs_qqq_60d": _relative_strength(close_values, [_number(row.get("close")) for row in qqq_prices], 60),
        "atr_pct": _atr_pct(prices),
        "volume_ratio": _volume_ratio([value for value in volumes if value is not None]),
        "distance_from_52w_high": (price / high_252 - 1) if high_252 else None,
        "base_length_days": _base_length_days(close_values, high_252),
        "breakout_level": max(high_values[-60:-1]) if len(high_values) > 1 else high_252,
        "raw": {
            "price_rows": len(prices),
            "qqq_rows": len(qqq_prices),
            "source": "prices_daily",
            # Realized vol carried in raw (no schema migration): the EV engine and the
            # iv_rv cheap-convexity test read rv_60d from here.
            "rv_20d": _realized_vol(close_values, 20),
            "rv_60d": _realized_vol(close_values, 60),
        },
    }
    con.execute(
        """
        INSERT OR REPLACE INTO stock_features
        (snapshot_time, ticker, price, ma_20, ma_50, ma_200, rs_vs_qqq_20d,
         rs_vs_qqq_60d, atr_pct, volume_ratio, distance_from_52w_high,
         base_length_days, breakout_level, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            feature["snapshot_time"],
            feature["ticker"],
            feature["price"],
            feature["ma_20"],
            feature["ma_50"],
            feature["ma_200"],
            feature["rs_vs_qqq_20d"],
            feature["rs_vs_qqq_60d"],
            feature["atr_pct"],
            feature["volume_ratio"],
            feature["distance_from_52w_high"],
            feature["base_length_days"],
            feature["breakout_level"],
            json_dumps(feature["raw"]),
        ],
    )
    return feature
