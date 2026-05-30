"""Market event calendar ingestion and normalization."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any

from investment_panel.core.config import AppConfig
from investment_panel.core.db import json_dumps


@dataclass(frozen=True)
class MarketEvent:
    id: str
    event_date: str
    event: str
    expected_impact: str
    source: str
    symbol: str | None = None
    start_at: str | None = None
    end_at: str | None = None
    timezone: str = "America/New_York"
    event_scope: str = "macro"
    event_kind: str = "economic"
    importance: str = "high"
    verification_status: str = "watch"
    source_url: str | None = None
    source_name: str | None = None
    raw: dict[str, Any] | None = None


LEGACY_REQUESTED_WEEK_EVENT_IDS = (
    "macro-2026-05-11-warsh-cloture-vote",
    "macro-2026-05-12-bls-cpi-april",
    "macro-2026-05-14-fed-barr-balance-sheet-speech",
    "macro-2026-05-14-fed-h41",
    "macro-2026-05-14-trump-xi-summit",
    "macro-2026-05-15-warsh-chair-transition-deadline",
    "macro-2026-05-11-warsh-confirmation-watch",
    "macro-2026-05-13-fed-chair-speech-watch",
)


def delete_legacy_requested_week_events(con: Any) -> int:
    placeholders = ", ".join(["?"] * len(LEGACY_REQUESTED_WEEK_EVENT_IDS))
    deleted = con.execute(f"DELETE FROM catalysts WHERE id IN ({placeholders})", list(LEGACY_REQUESTED_WEEK_EVENT_IDS)).rowcount
    return max(0, int(deleted or 0))


def update_event_calendar(con: Any, config: AppConfig) -> dict[str, Any]:
    deleted = delete_legacy_requested_week_events(con)
    if not config.event_sources.enabled:
        record_calendar_health(con, "disabled", f"Event calendar ingestion disabled; removed {deleted} legacy seeded rows")
        return {"status": "disabled", "events": 0, "legacy_seed_rows_deleted": deleted}
    record_calendar_health(con, "missing", f"No live event calendar fetcher configured; removed {deleted} legacy seeded rows")
    return {
        "status": "missing",
        "events": 0,
        "legacy_seed_rows_deleted": deleted,
        "sources": {
            "bls": config.event_sources.bls_enabled,
            "federal_reserve": config.event_sources.federal_reserve_enabled,
            "treasury": config.event_sources.treasury_enabled,
            "sec": config.event_sources.sec_enabled,
            "watchlist": config.event_sources.watchlist_enabled,
        },
    }


def upsert_events(con: Any, events: list[MarketEvent]) -> int:
    count = 0
    for event in events:
        con.execute(
            """
            INSERT OR REPLACE INTO catalysts
            (id, symbol, event_date, event, expected_impact, source, start_at, end_at, timezone,
             event_scope, event_kind, importance, verification_status, source_url, source_name, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                event.id,
                event.symbol,
                event.event_date,
                event.event,
                event.expected_impact,
                event.source,
                event.start_at,
                event.end_at,
                event.timezone,
                event.event_scope,
                event.event_kind,
                event.importance,
                event.verification_status,
                event.source_url,
                event.source_name,
                json_dumps(event.raw or asdict(event)),
            ],
        )
        count += 1
    return count


def record_calendar_health(con: Any, status: str, detail: str) -> None:
    con.execute(
        """
        INSERT OR REPLACE INTO source_health (source, checked_at, status, detail, source_url)
        VALUES (?, now(), ?, ?, ?)
        """,
        ["event_calendar", status, detail, "local:event_calendar"],
    )


def event_id(source: str, event_date: str, title: str) -> str:
    digest = hashlib.sha1(f"{source}:{event_date}:{title}".encode("utf-8")).hexdigest()[:12]
    return f"macro-{digest}"


def parse_bls_schedule_rows(html: str, release_name: str = "Consumer Price Index") -> list[MarketEvent]:
    rows: list[MarketEvent] = []
    pattern = re.compile(r"(?P<month>[A-Z][a-z]+ \d{4})\s+(?P<date>[A-Z][a-z]{2}\.?\s+\d{1,2},\s+\d{4})\s+(?P<time>\d{2}:\d{2}\s+[AP]M)")
    for match in pattern.finditer(html):
        event_date = _parse_bls_date(match.group("date"))
        if not event_date:
            continue
        title = f"{match.group('month')} {release_name} release"
        rows.append(
            MarketEvent(
                id=event_id("bls", event_date, title),
                event_date=event_date,
                event=title,
                expected_impact=f"{release_name} scheduled release.",
                source="bls",
                start_at=f"{event_date}T{_to_24_hour(match.group('time'))}",
                event_kind="inflation" if "price" in release_name.lower() else "economic",
                verification_status="confirmed",
                source_name="U.S. Bureau of Labor Statistics",
                source_url="https://www.bls.gov/schedule/news_release/bls.ics",
                raw=match.groupdict(),
            )
        )
    return rows


def parse_fed_calendar_text(text: str) -> list[MarketEvent]:
    rows: list[MarketEvent] = []
    h41_match = re.search(r"(?P<time>4:30 p\.m\.)\s+H\.4\.1 - Factors Affecting Reserve Balances\s+(?P<dates>[\d,\s]+)", text)
    if h41_match:
        for day in [item.strip() for item in h41_match.group("dates").split(",") if item.strip().isdigit()]:
            event_date = f"2026-05-{int(day):02d}"
            rows.append(
                MarketEvent(
                    id=event_id("federal_reserve", event_date, "H.4.1 Factors Affecting Reserve Balances"),
                    event_date=event_date,
                    event="Federal Reserve H.4.1 balance sheet release",
                    expected_impact="Weekly Fed balance sheet update.",
                    source="federal_reserve",
                    start_at=f"{event_date}T16:30:00",
                    event_kind="central_bank",
                    verification_status="confirmed",
                    source_name="Federal Reserve",
                    source_url="https://www.federalreserve.gov/Releases/H41/default.htm",
                    raw=h41_match.groupdict(),
                )
            )
    return rows


def geopolitical_event_from_report(event_date: str, end_date: str, title: str, source_url: str, source_name: str) -> MarketEvent:
    return MarketEvent(
        id=event_id("geopolitical_watch", event_date, title),
        event_date=event_date,
        event=title,
        expected_impact="Geopolitical market-risk event.",
        source="geopolitical_watch",
        start_at=f"{event_date}T00:00:00",
        end_at=f"{end_date}T23:59:00",
        timezone="local",
        event_kind="geopolitical",
        verification_status="confirmed",
        source_url=source_url,
        source_name=source_name,
        raw={"end_date": end_date},
    )


def _parse_bls_date(value: str) -> str | None:
    cleaned = value.replace(".", "")
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(cleaned, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _to_24_hour(value: str) -> str:
    return datetime.strptime(value, "%I:%M %p").time().isoformat()
