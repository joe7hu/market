from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_api_routes_return_json() -> None:
    client = TestClient(app)
    for path in [
        "/api/status",
        "/api/dashboard",
        "/api/panel-snapshot?scope=dashboard",
        "/api/decision-readiness",
        "/api/signals",
        "/api/opportunities-ranked",
        "/api/opportunity-sources",
        "/api/candidates",
        "/api/portfolio",
        "/api/theses",
        "/api/trader-twins",
        "/api/catalysts",
        "/api/fundamentals",
        "/api/disclosures",
        "/api/quotes",
        "/api/screener",
        "/api/options-expiries",
        "/api/options-chain",
        "/api/options-payoff-scenarios",
        "/api/news",
        "/api/tradingview-symbol-search",
        "/api/tradingview-watchlists",
        "/api/tradingview-alerts",
        "/api/tradingview-chart-state",
        "/api/sepa",
        "/api/liquidity",
        "/api/correlations",
        "/api/etf-premiums",
        "/api/analyst-estimates",
        "/api/earnings",
        "/api/earnings-setups",
        "/api/valuations",
        "/api/technicals",
        "/api/research-packets",
        "/api/provider-runs",
        "/api/source-health",
        "/api/refresh-jobs",
        "/api/settings",
        "/api/tickers/TSLA",
    ]:
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")


def test_refresh_job_launcher_rejects_unallowlisted_job() -> None:
    client = TestClient(app)
    response = client.post("/api/refresh-jobs/not-a-real-job")
    assert response.status_code == 400
    assert "allowlisted" in response.text
