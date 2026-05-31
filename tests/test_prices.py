from __future__ import annotations

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
