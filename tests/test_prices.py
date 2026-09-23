from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
import sys

from investment_panel.core import prices


def test_yahoo_alias_fetch_keeps_market_symbol(monkeypatch) -> None:
    requested_urls: list[str] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "chart": {
                    "result": [
                        {
                            "timestamp": [1_700_000_000, 1_700_086_400],
                            "indicators": {
                                "quote": [
                                    {
                                        "open": [10, 11],
                                        "high": [12, 13],
                                        "low": [9, 10],
                                        "close": [11, 12],
                                        "volume": [100, 120],
                                    }
                                ]
                            },
                        }
                    ]
                }
            }

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            return None

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args) -> None:
            return None

        def get(self, url: str, params: dict) -> FakeResponse:
            requested_urls.append(url)
            return FakeResponse()

    monkeypatch.setattr(prices.httpx, "Client", FakeClient)

    frame = prices.fetch_yahoo_chart("SQ", lookback_days=2)

    assert requested_urls == ["https://query1.finance.yahoo.com/v8/finance/chart/XYZ"]
    assert frame["symbol"].unique().tolist() == ["SQ"]
    assert frame["source"].unique().tolist() == ["yahoo-chart:XYZ"]


def test_yahoo_current_daily_row_is_marked_provisional_until_session_close(monkeypatch) -> None:
    now = int(datetime.now(UTC).timestamp())

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "chart": {"result": [{
                    "meta": {
                        "exchangeTimezoneName": "America/New_York",
                        "currentTradingPeriod": {"regular": {"end": now + 3_600}},
                    },
                    "timestamp": [now],
                    "indicators": {"quote": [{
                        "open": [10], "high": [12], "low": [9], "close": [11], "volume": [100],
                    }]},
                }]},
            }

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            return None

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args) -> None:
            return None

        def get(self, *_args, **_kwargs) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(prices.httpx, "Client", FakeClient)

    frame = prices.fetch_yahoo_chart("QQQ", lookback_days=1)

    assert not bool(frame.iloc[0]["is_complete"])


def test_yahoo_uses_yfinance_when_a_closed_terminal_bar_is_missing(monkeypatch) -> None:
    now = int(datetime.now(UTC).timestamp())
    market_date = datetime.fromtimestamp(now, UTC).date()

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "chart": {"result": [{
                    "meta": {
                        "exchangeTimezoneName": "UTC",
                        "currentTradingPeriod": {"regular": {"end": now - 1}},
                    },
                    "timestamp": [now - 86_400],
                    "indicators": {"quote": [{
                        "open": [10], "high": [12], "low": [9], "close": [11], "volume": [100],
                    }]},
                }]},
            }

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            return None

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args) -> None:
            return None

        def get(self, *_args, **_kwargs) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(prices.httpx, "Client", FakeClient)
    monkeypatch.setattr(prices, "fetch_yfinance", lambda *_args, **_kwargs: prices.pd.DataFrame({
        "symbol": ["UNH"], "date": [market_date], "open": [10], "high": [12], "low": [9],
        "close": [11], "volume": [100], "source": ["yfinance"],
    }))

    frame = prices.fetch_yahoo_chart("UNH", lookback_days=2)

    assert frame["date"].tolist() == [market_date - timedelta(days=1), market_date]
    assert frame["source"].tolist() == ["yahoo-chart", "yfinance"]
    assert bool(frame.iloc[-1]["is_complete"])

    monkeypatch.setattr(prices, "fetch_yfinance", lambda *_args, **_kwargs: prices.pd.DataFrame({
        "symbol": ["UNH"], "date": [market_date], "open": [10], "high": [10], "low": [9],
        "close": [11], "volume": [100], "source": ["yfinance"],
    }))

    unchanged = prices.fetch_yahoo_chart("UNH", lookback_days=2)

    assert unchanged["date"].tolist() == [market_date - timedelta(days=1)]


def test_yahoo_uses_completed_intraday_bar_when_daily_chart_is_stale(monkeypatch) -> None:
    session_start = int(datetime(2026, 9, 22, 13, 30, tzinfo=UTC).timestamp())
    session_end = int(datetime(2026, 9, 22, 20, tzinfo=UTC).timestamp())
    minute_timestamps = list(range(session_start, session_end, 60))

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self.payload

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            return None

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args) -> None:
            return None

        def get(self, _url: str, params: dict) -> FakeResponse:
            metadata = {
                "exchangeTimezoneName": "UTC",
                "currentTradingPeriod": {"regular": {"start": session_start, "end": session_end}},
            }
            if params["interval"] == "1m":
                return FakeResponse({"chart": {"result": [{
                    "meta": metadata,
                    "timestamp": minute_timestamps,
                    "indicators": {"quote": [{
                        "open": [10, *([11] * (len(minute_timestamps) - 1))],
                        "high": [*([12] * (len(minute_timestamps) - 1)), 13],
                        "low": [9] * len(minute_timestamps),
                        "close": [*([11] * (len(minute_timestamps) - 1)), 12],
                        "volume": [100] * len(minute_timestamps),
                    }]},
                }]}})
            return FakeResponse({"chart": {"result": [{
                "meta": metadata,
                "timestamp": [session_start - 86_400],
                "indicators": {"quote": [{
                    "open": [10], "high": [12], "low": [9], "close": [11], "volume": [100],
                }]},
            }]}})

    monkeypatch.setattr(prices.httpx, "Client", FakeClient)
    monkeypatch.setattr(prices.time, "time", lambda: session_end + 60)
    monkeypatch.setattr(
        prices,
        "fetch_yfinance",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("intraday terminal bar should win")),
    )

    frame = prices.fetch_yahoo_chart("UNH", lookback_days=2)

    terminal = frame.iloc[-1]
    assert terminal["date"] == date(2026, 9, 22)
    assert terminal["source"] == "yahoo-chart-intraday"
    assert (terminal["open"], terminal["high"], terminal["low"], terminal["close"], terminal["volume"]) == (10, 13, 9, 12, 39_000)
    assert bool(terminal["is_complete"])


def test_yahoo_intraday_terminal_rejects_incomplete_regular_session(monkeypatch) -> None:
    session_start = int(datetime(2026, 9, 22, 13, 30, tzinfo=UTC).timestamp())
    session_end = int(datetime(2026, 9, 22, 20, tzinfo=UTC).timestamp())
    timestamps = list(range(session_start, session_end, 60))
    del timestamps[1]

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"chart": {"result": [{
                "meta": {
                    "exchangeTimezoneName": "UTC",
                    "currentTradingPeriod": {"regular": {"start": session_start, "end": session_end}},
                },
                "timestamp": timestamps,
                "indicators": {"quote": [{
                    "open": [10] * len(timestamps), "high": [12] * len(timestamps),
                    "low": [9] * len(timestamps), "close": [11] * len(timestamps),
                    "volume": [100] * len(timestamps),
                }]},
            }]}}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            return None

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args) -> None:
            return None

        def get(self, *_args, **_kwargs) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(prices.httpx, "Client", FakeClient)

    terminal = prices.fetch_yahoo_intraday_terminal(
        "UNH", "UNH", market_date=date(2026, 9, 22), regular_session_end=session_end,
    )

    assert terminal.empty


def test_normalize_price_frame_keeps_raw_close_when_adjusted_close_is_present() -> None:
    frame = prices.pd.DataFrame({
        "date": ["2026-09-22"], "open": [10], "high": [12], "low": [9], "close": [11],
        "adj_close": [10], "volume": [100],
    })

    normalized = prices.normalize_price_frame("UNH", frame, "yfinance")

    assert normalized.columns.tolist() == ["symbol", "date", "open", "high", "low", "close", "volume", "source"]
    assert normalized.iloc[0]["close"] == 11


def test_yfinance_uses_the_yahoo_alias_and_exchange_market_date(monkeypatch) -> None:
    requested: dict[str, str] = {}

    def download(symbol: str, *, start: str, end: str, **_kwargs) -> prices.pd.DataFrame:
        requested.update(symbol=symbol, start=start, end=end)
        return prices.pd.DataFrame({
            "Date": ["2026-09-23"], "Open": [10], "High": [12], "Low": [9], "Close": [11],
            "Adj Close": [10], "Volume": [100],
        })

    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(download=download))

    frame = prices.fetch_yfinance("SPX", 1, market_date=date(2026, 9, 23))

    assert requested == {"symbol": "^GSPC", "start": "2026-09-22", "end": "2026-09-24"}
    assert frame.iloc[0]["symbol"] == "SPX"
