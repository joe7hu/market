"""Daily price ingestion with online providers."""

from __future__ import annotations

import time
from math import isfinite
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import pandas as pd


COINBASE_PRODUCTS = frozenset({"BTC-USD", "ETH-USD", "SOL-USD"})
YAHOO_SYMBOL_ALIASES = {
    "000660": "000660.KS",
    "005380": "005380.KS",
    "005930": "005930.KS",
    "373220": "373220.KS",
    "5803": "5803.T",
    "8035": "8035.T",
    "9984": "9984.T",
    "ABB": "ABBNY",
    "ABC": "COR",
    "BLL": "BALL",
    "BNBUSD": "BNB-USD",
    "BTCUSD": "BTC-USD",
    "DJI": "^DJI",
    "ETHUSD": "ETH-USD",
    "HINDALCO": "HINDALCO.NS",
    "HYPEUSD": "HYPE32196-USD",
    "HSI": "^HSI",
    "IXIC": "^IXIC",
    "KOSPI": "^KS11",
    "KNOX": "KNOX.V",
    "LPK": "LPK.DE",
    "NI225": "^N225",
    "NIFTY": "^NSEI",
    "RTN": "RTX",
    "RWE": "RWE.DE",
    "SIVE": "SIVE.ST",
    "SIVE.": "SIVE.ST",
    "SOI": "SOI.PA",
    "SPX": "^GSPC",
    "SQ": "XYZ",
    "TASI": "^TASI.SR",
    "TOPIX": "1306.T",
    "USDJPY": "JPY=X",
    "USDKRW": "KRW=X",
    "USDMYR": "MYR=X",
    "USDPHP": "PHP=X",
    "USDSGD": "SGD=X",
    "USDTHB": "THB=X",
    "XLMUSD": "XLM-USD",
    "XRPUSD": "XRP-USD",
    "ZEEL": "ZEEL.NS",
    "399300": "399300.SZ",
    "BPCL": "BPCL.NS",
    "BOURSA": "BOURSA.KW",
    "MMC": "MMCO.VI",
    "QNBK": "QNBK.QA",
}


def fetch_prices(symbol: str, lookback_days: int = 260, mode: str = "online") -> pd.DataFrame:
    if mode != "online":
        raise ValueError(f"Unsupported market_data.mode {mode!r}; use online data or inject test fixtures.")
    if symbol in COINBASE_PRODUCTS:
        try:
            return fetch_coinbase_candles(symbol, lookback_days)
        except (httpx.HTTPError, ValueError):
            # Alternate real OHLC source, never a constructed price-sample bar.
            return fetch_yahoo_chart(symbol, lookback_days)
    return fetch_yahoo_chart(symbol, lookback_days)


def fetch_yahoo_chart(symbol: str, lookback_days: int = 260) -> pd.DataFrame:
    provider_symbol = YAHOO_SYMBOL_ALIASES.get(symbol, symbol)
    end = int(time.time())
    start = end - lookback_days * 3 * 86400
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{provider_symbol}"
    with httpx.Client(timeout=20.0, headers={"User-Agent": "joehu-market-panel/0.1"}) as client:
        response = client.get(
            url,
            params={
                "period1": start,
                "period2": end,
                "interval": "1d",
                "events": "history",
                "includeAdjustedClose": "true",
            },
        )
        response.raise_for_status()
        payload = response.json()
    result = (payload.get("chart", {}).get("result") or [None])[0]
    if not result:
        raise ValueError(f"No Yahoo chart result for {symbol}")
    timestamps = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    metadata = result.get("meta") or {}
    try:
        market_timezone = ZoneInfo(str(metadata.get("exchangeTimezoneName") or "UTC"))
    except ZoneInfoNotFoundError:
        market_timezone = ZoneInfo("UTC")
    requested_at = datetime.fromtimestamp(end, UTC)
    market_date = requested_at.astimezone(market_timezone).date()
    regular_session_end = (
        ((metadata.get("currentTradingPeriod") or {}).get("regular") or {}).get("end")
    )
    rows = []
    for index, ts in enumerate(timestamps):
        close = value_at(quote.get("close"), index)
        opened, high, low, volume = (value_at(quote.get(key), index) for key in ("open", "high", "low", "volume"))
        if any(value is None or not isfinite(value) for value in (opened, high, low, close, volume)):
            continue
        if not (0 < low <= min(opened, close) <= max(opened, close) <= high) or volume < 0:
            continue
        trading_date = datetime.fromtimestamp(ts, UTC).astimezone(market_timezone).date()
        is_complete = trading_date < market_date or (
            trading_date == market_date
            and regular_session_end is not None
            and end >= int(regular_session_end)
        )
        rows.append(
            {
                "symbol": symbol,
                "date": trading_date,
                "open": opened,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "source": f"yahoo-chart:{provider_symbol}" if provider_symbol != symbol else "yahoo-chart",
                "is_complete": is_complete,
            }
        )
    if not rows:
        raise ValueError(f"No Yahoo chart rows for {symbol}")
    return pd.DataFrame(rows).tail(lookback_days)


def fetch_yfinance(symbol: str, lookback_days: int = 260) -> pd.DataFrame:
    import yfinance as yf

    end = date.today() + timedelta(days=1)
    start = end - timedelta(days=lookback_days * 2)
    frame = yf.download(symbol, start=start.isoformat(), end=end.isoformat(), progress=False, auto_adjust=False)
    if frame.empty:
        raise ValueError(f"No yfinance rows for {symbol}")
    frame = frame.reset_index()
    frame.columns = [str(column[0] if isinstance(column, tuple) else column).lower().replace(" ", "_") for column in frame.columns]
    return normalize_price_frame(symbol, frame, "yfinance").tail(lookback_days)


def fetch_coinbase_candles(symbol: str, lookback_days: int = 260) -> pd.DataFrame:
    """Completed UTC daily OHLCV buckets, paged within Coinbase's 300 limit.

    API: docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles
    No interpolation: a no-trade/missing interval remains a history gap.
    """
    if lookback_days < 1 or lookback_days > 3650:
        raise ValueError("lookback_days must be between 1 and 3650")
    end = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    earliest = end - timedelta(days=lookback_days)
    cursor = earliest
    rows: dict[date, dict[str, Any]] = {}
    with httpx.Client(timeout=20.0, headers={"User-Agent": "joehu-market-panel/0.1"}) as client:
        while cursor < end:
            boundary = min(end, cursor + timedelta(days=299))
            response = client.get(
                f"https://api.exchange.coinbase.com/products/{symbol}/candles",
                params={"granularity": 86400, "start": cursor.isoformat(), "end": boundary.isoformat()},
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError(f"Invalid candle payload for {symbol}")
            for candle in payload:
                if not isinstance(candle, list) or len(candle) != 6:
                    raise ValueError(f"Invalid OHLCV bucket for {symbol}")
                ts, low, high, opened, close, volume = map(float, candle)
                if not all(isfinite(value) for value in (ts, low, high, opened, close, volume)):
                    raise ValueError(f"Non-finite OHLCV bucket for {symbol}")
                started = datetime.fromtimestamp(ts, UTC)
                if not earliest <= started < end:
                    continue  # provider may include the previous or live bucket
                if started.hour or started.minute or started.second or not (0 < low <= min(opened, close) <= max(opened, close) <= high) or volume < 0:
                    raise ValueError(f"Inconsistent OHLCV bucket for {symbol}")
                row = {"symbol": symbol, "date": started.date(), "open": opened, "high": high,
                       "low": low, "close": close, "volume": volume, "source": "coinbase-exchange-candles",
                       "observed_at": started + timedelta(days=1), "is_complete": True}
                previous = rows.get(started.date())
                if previous is not None and previous != row:
                    raise ValueError(f"Conflicting duplicate OHLCV bucket for {symbol}")
                rows[started.date()] = row
            cursor = boundary
    if not rows:
        raise ValueError(f"No completed Coinbase candles for {symbol}")
    return pd.DataFrame([rows[day] for day in sorted(rows)])


def normalize_price_frame(symbol: str, frame: pd.DataFrame, source: str) -> pd.DataFrame:
    column_map = {
        "date": "date",
        "datetime": "date",
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "adj_close": "close",
        "volume": "volume",
    }
    normalized = frame.rename(columns={key: value for key, value in column_map.items() if key in frame.columns})
    required = ["date", "open", "high", "low", "close", "volume"]
    missing = [column for column in required if column not in normalized.columns]
    if missing:
        raise ValueError(f"Price frame missing columns for {symbol}: {missing}")
    normalized = normalized[required].copy()
    normalized["symbol"] = symbol
    normalized["source"] = source
    normalized["date"] = pd.to_datetime(normalized["date"]).dt.date
    return normalized[["symbol", "date", "open", "high", "low", "close", "volume", "source"]]


def upsert_prices(con: Any, frame: pd.DataFrame) -> int:
    if frame.empty:
        return 0
    con.register("price_frame", frame)
    con.execute(
        """
        INSERT OR REPLACE INTO prices_daily
        SELECT symbol, date, open, high, low, close, volume, source
        FROM price_frame
        """
    )
    con.unregister("price_frame")
    return len(frame)


def value_at(values: list[Any] | None, index: int) -> float | None:
    if not values or index >= len(values) or values[index] is None:
        return None
    return float(values[index])
