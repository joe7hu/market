"""Source documentation and lightweight online health checks."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from investment_panel.core.source_ingestion.canonical import sync_canonical_sources
from investment_panel.core.source_ingestion.definitions import VERIFIED_SOURCES
from investment_panel.core.source_ingestion.registry import ensure_source_registry

def record_verified_sources(con: Any) -> None:
    ensure_source_registry(con)
    for source in VERIFIED_SOURCES:
        con.execute(
            """
            INSERT OR REPLACE INTO source_health
            (source, checked_at, status, detail, source_url)
            VALUES (?, ?, ?, ?, ?)
            """,
            [source["source"], datetime.utcnow().isoformat(), "verified_docs", source["detail"], source["source_url"]],
        )
    sync_canonical_sources(con)


def lightweight_online_check(con: Any, user_agent: str) -> None:
    ensure_source_registry(con)
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
    sync_canonical_sources(con)
