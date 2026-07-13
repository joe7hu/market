from __future__ import annotations

import httpx

from investment_panel.core import sec
from investment_panel.core.provider_identity import provider_user_agent


def test_sec_get_bytes_declares_headers_and_retries_transient_403(monkeypatch) -> None:
    requests: list[dict[str, str]] = []
    statuses = iter([403, 200])

    def fake_get(url: str, *, headers: dict[str, str], timeout: float, follow_redirects: bool) -> httpx.Response:
        requests.append(headers)
        request = httpx.Request("GET", url)
        status = next(statuses)
        return httpx.Response(status, request=request, content=b"ok" if status == 200 else b"blocked")

    sleeps: list[float] = []
    monkeypatch.setattr(sec.httpx, "get", fake_get)
    monkeypatch.setattr(sec.time, "sleep", sleeps.append)

    assert sec.sec_get_bytes("https://www.sec.gov/Archives/test.json", "Market App owner@example.com") == b"ok"
    assert requests == [
        {"User-Agent": "Market App owner@example.com", "Accept-Encoding": "gzip, deflate", "Host": "www.sec.gov"},
        {"User-Agent": "Market App owner@example.com", "Accept-Encoding": "gzip, deflate", "Host": "www.sec.gov"},
    ]
    assert sleeps == [0.25]


def test_regulatory_user_agents_are_loaded_from_deployment_environment(monkeypatch) -> None:
    from types import SimpleNamespace

    config = SimpleNamespace(market_data=SimpleNamespace(user_agent="generic-market-client"))
    monkeypatch.setenv("MARKET_SEC_USER_AGENT", "SEC deployment contact")
    monkeypatch.setenv("MARKET_BLS_USER_AGENT", "BLS deployment contact")

    assert provider_user_agent(config, "sec") == "SEC deployment contact"
    assert provider_user_agent(config, "bls") == "BLS deployment contact"
