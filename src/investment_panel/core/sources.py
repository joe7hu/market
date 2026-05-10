"""Source health checks and verified data-source notes."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx


VERIFIED_SOURCES = [
    {
        "source": "sec_edgar",
        "source_url": "https://www.sec.gov/search-filings/edgar-application-programming-interfaces",
        "detail": "Free server-side JSON APIs; use declared user-agent; SEC fair-access max is 10 requests/sec total.",
    },
    {
        "source": "sec_13f",
        "source_url": "https://www.sec.gov/files/form_13f.pdf",
        "detail": "Quarterly official ZIP datasets from May 2013 forward; tab-delimited flat files; as-filed caveats apply.",
    },
    {
        "source": "coingecko",
        "source_url": "https://docs.coingecko.com/reference/coins-markets",
        "detail": "Demo/free REST API uses api.coingecko.com; 30 calls/min and 10k/month; categories update about every 5 minutes.",
    },
    {
        "source": "defillama",
        "source_url": "https://defillama.com/docs/api",
        "detail": "Free unauthenticated API for protocols, TVL, fees/revenue; yields currently use yields.llama.fi.",
    },
    {
        "source": "yfinance",
        "source_url": "https://pypi.org/project/yfinance/",
        "detail": "Unofficial Yahoo Finance wrapper intended for research/education and personal use; cache/fallback required.",
    },
    {
        "source": "stooq",
        "source_url": "https://pydata.github.io/pandas-datareader/readers/stooq.html",
        "detail": "Available through pandas-datareader StooqDailyReader; website-backed, not a contracted API.",
    },
    {
        "source": "opencli",
        "source_url": "https://github.com/jackwener/opencli",
        "detail": "Local CLI adapter registry used read-only for research sources; Market allowlists commands through provider adapters.",
    },
    {
        "source": "tradingview_opencli",
        "source_url": "https://github.com/himself65/finance-skills/tree/main/opencli-plugins/tradingview",
        "detail": "Read-only TradingView desktop adapter for quotes, screeners, news, watchlists, alerts, chart state, and options chains.",
    },
]


def record_verified_sources(con: Any) -> None:
    for source in VERIFIED_SOURCES:
        con.execute(
            """
            INSERT OR REPLACE INTO source_health
            (source, checked_at, status, detail, source_url)
            VALUES (?, ?, ?, ?, ?)
            """,
            [source["source"], datetime.utcnow().isoformat(), "verified_docs", source["detail"], source["source_url"]],
        )


def lightweight_online_check(con: Any, user_agent: str) -> None:
    checks = [
        ("sec_edgar", "https://data.sec.gov/submissions/CIK0000320193.json"),
        ("coingecko", "https://api.coingecko.com/api/v3/ping"),
        ("defillama", "https://api.llama.fi/protocols"),
    ]
    with httpx.Client(timeout=8.0, headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}) as client:
        for source, url in checks:
            status = "unreachable"
            detail = ""
            try:
                response = client.get(url)
                status = "ok" if response.status_code < 400 else f"http_{response.status_code}"
                detail = f"HTTP {response.status_code}"
            except Exception as exc:
                detail = str(exc)
            con.execute(
                """
                INSERT OR REPLACE INTO source_health
                (source, checked_at, status, detail, source_url)
                VALUES (?, ?, ?, ?, ?)
                """,
                [source, datetime.utcnow().isoformat(), status, detail, url],
            )
