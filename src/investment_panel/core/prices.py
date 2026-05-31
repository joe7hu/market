"""Daily price ingestion with online providers."""

from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Any

import httpx
import pandas as pd


COINGECKO_IDS = {"BTC-USD": "bitcoin", "ETH-USD": "ethereum", "SOL-USD": "solana"}
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
    if symbol in COINGECKO_IDS:
        return fetch_coingecko_ohlc(symbol, lookback_days)
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
    rows = []
    for index, ts in enumerate(timestamps):
        close = value_at(quote.get("close"), index)
        if close is None:
            continue
        rows.append(
            {
                "symbol": symbol,
                "date": pd.to_datetime(ts, unit="s").date(),
                "open": value_at(quote.get("open"), index) or close,
                "high": value_at(quote.get("high"), index) or close,
                "low": value_at(quote.get("low"), index) or close,
                "close": close,
                "volume": value_at(quote.get("volume"), index) or 0.0,
                "source": f"yahoo-chart:{provider_symbol}" if provider_symbol != symbol else "yahoo-chart",
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


def fetch_coingecko_market_chart(coin_id: str, days: int = 365) -> dict[str, Any]:
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
    with httpx.Client(timeout=20.0) as client:
        response = client.get(url, params={"vs_currency": "usd", "days": days, "interval": "daily"})
        response.raise_for_status()
        return response.json()


def fetch_coingecko_ohlc(symbol: str, lookback_days: int = 260) -> pd.DataFrame:
    coin_id = COINGECKO_IDS[symbol]
    chart = fetch_coingecko_market_chart(coin_id, days=min(max(lookback_days, 30), 365))
    prices = chart.get("prices") or []
    volumes = {pd.to_datetime(row[0], unit="ms").date(): row[1] for row in chart.get("total_volumes", [])}
    rows = []
    previous = None
    for ts_ms, close in prices:
        day = pd.to_datetime(ts_ms, unit="ms").date()
        open_ = previous if previous is not None else close
        high = max(open_, close)
        low = min(open_, close)
        rows.append(
            {
                "symbol": symbol,
                "date": day,
                "open": float(open_),
                "high": float(high),
                "low": float(low),
                "close": float(close),
                "volume": float(volumes.get(day, 0.0)),
                "source": "coingecko-market-chart",
            }
        )
        previous = close
    if not rows:
        raise ValueError(f"No CoinGecko chart rows for {symbol}")
    return pd.DataFrame(rows).tail(lookback_days)


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
