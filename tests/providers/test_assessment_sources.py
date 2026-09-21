"""Provider payload tests: real OHLCV, bounded paging, explicit failures."""
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from investment_panel.core import prices
from investment_panel.infrastructure.providers import assessment_quotes


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 20, 21, 30, tzinfo=UTC)


def client_for(handler):
    class Client:
        def __init__(self, **kwargs):
            assert kwargs["timeout"] <= 20
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def get(self, url, params=None):
            return httpx.Response(200, json=handler(url, params), request=httpx.Request("GET", url))
    return Client


def test_daily_candles_are_genuine_ohlcv_paged_and_exclude_live_bucket(monkeypatch):
    calls = []
    def response(url, params):
        calls.append(params)
        start, end = (datetime.fromisoformat(params[key]) for key in ("start", "end"))
        assert (end-start).days <= 299 and params["granularity"] == 86400
        rows = []
        while start <= end:  # Includes provider's overlapping boundary / live bucket.
            rows.append([start.timestamp(), 98, 105, 100, 102, 23])
            start += timedelta(days=1)
        return rows
    monkeypatch.setattr(prices, "datetime", FrozenDatetime)
    monkeypatch.setattr(prices.httpx, "Client", client_for(response))
    bars = prices.fetch_coinbase_candles("BTC-USD", 360)
    assert len(calls) == 2 and len(bars) == 360 and bars["date"].is_unique
    assert bars.iloc[-1]["date"].isoformat() == "2026-09-19"
    assert list(bars.iloc[-1][["open", "high", "low", "close", "volume"]]) == [100, 105, 98, 102, 23]
    assert bars.iloc[-1]["observed_at"] == datetime(2026, 9, 20, tzinfo=UTC)


@pytest.mark.parametrize("bucket", [
    [1789862400, 110, 105, 100, 102, 23],
    [1789862400, 98, 105, 100, 102, -1],
    [1789862400, 98, 105, 100],
])
def test_malformed_candle_never_becomes_a_flat_synthetic_bar(monkeypatch, bucket):
    monkeypatch.setattr(prices, "datetime", FrozenDatetime)
    # Use a known in-range bucket rather than depending on an opaque epoch.
    bucket[0] = datetime(2026, 9, 19, tzinfo=UTC).timestamp()
    monkeypatch.setattr(prices.httpx, "Client", client_for(lambda *_: [bucket]))
    with pytest.raises(ValueError):
        prices.fetch_coinbase_candles("BTC-USD", 2)


def test_failed_crypto_primary_falls_back_to_real_yahoo_ohlcv(monkeypatch):
    def fail(*_):
        raise ValueError("bad primary payload")
    expected = object()
    monkeypatch.setattr(prices, "fetch_coinbase_candles", fail)
    monkeypatch.setattr(prices, "fetch_yahoo_chart", lambda *_: expected)
    assert prices.fetch_prices("BTC-USD") is expected


def test_crypto_reference_has_separate_fallback_and_original_clock(monkeypatch):
    now = datetime.now(UTC)
    calls = []
    def response(url, _):
        calls.append(url)
        if "coinbase" in url:
            return {"price": "100", "time": (now-timedelta(days=2)).isoformat()}
        return {"chart": {"result": [{"meta": {"regularMarketPrice": 102, "regularMarketTime": int(now.timestamp())}}]}}
    monkeypatch.setattr(assessment_quotes.httpx, "Client", client_for(response))
    quote = assessment_quotes.fetch_assessment_quote("BTC-USD", "crypto")
    assert len(calls) == 2 and quote["provider"] == "yahoo-chart-reference"
    assert quote["observed_at"] == now.replace(microsecond=0)
    assert quote["execution_eligible"] is False


def test_reference_collector_reports_both_provider_failures(monkeypatch):
    monkeypatch.setattr(assessment_quotes.httpx, "Client", client_for(lambda *_: {}))
    with pytest.raises(ValueError, match="Coinbase:.*Yahoo:"):
        assessment_quotes.fetch_assessment_quote("BTC-USD", "crypto")
