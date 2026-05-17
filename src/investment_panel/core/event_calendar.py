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


REQUESTED_WEEK_EVENTS = [
    MarketEvent(
        id="macro-2026-05-11-warsh-cloture-vote",
        event_date="2026-05-11",
        event="Senate cloture vote on Kevin Warsh Fed nomination",
        expected_impact="Procedural vote on Warsh's Fed Governor nomination; not final confirmation or chair swearing-in.",
        source="senate_schedule",
        start_at="2026-05-11T17:30:00",
        timezone="America/New_York",
        event_kind="central_bank",
        verification_status="confirmed",
        source_url="https://www.radiotv.senate.gov/",
        source_name="U.S. Senate Radio-TV Gallery",
        raw={
            "note": "Senate schedule lists a 5:30 p.m. vote on the motion to invoke cloture on the Warsh nomination.",
            "related_executive_calendar": "Executive Calendar #728",
        },
    ),
    MarketEvent(
        id="macro-2026-05-15-warsh-chair-transition-deadline",
        event_date="2026-05-15",
        event="Warsh Fed Chair confirmation target / Powell chair term ends",
        expected_impact="Leadership transition deadline; final confirmation timing remains dependent on Senate floor action.",
        source="event_calendar_watch",
        start_at="2026-05-15T09:30:00",
        timezone="America/New_York",
        event_kind="central_bank",
        verification_status="tentative",
        source_url="https://www.federalreserve.gov/mediacenter/files/FOMCpresconf20260429.pdf",
        source_name="Federal Reserve April 29, 2026 press conference transcript",
        raw={
            "note": "Powell said his term as Chair ends on May 15; reporting says Warsh is expected to be confirmed before then.",
            "secondary_source": "https://www.kiplinger.com/investing/economy/3-ways-kevin-warsh-will-change-the-fed",
        },
    ),
    MarketEvent(
        id="macro-2026-05-12-bls-cpi-april",
        event_date="2026-05-12",
        event="April 2026 CPI report",
        expected_impact="Inflation print can reprice rates, equity duration, USD, and crypto risk.",
        source="bls",
        start_at="2026-05-12T08:30:00",
        timezone="America/New_York",
        event_kind="inflation",
        verification_status="confirmed",
        source_url="https://www.bls.gov/schedule/news_release/cpi.htm?lv=true",
        source_name="U.S. Bureau of Labor Statistics",
        raw={"reference_month": "April 2026", "release_time": "08:30 AM"},
    ),
    MarketEvent(
        id="macro-2026-05-14-fed-barr-balance-sheet-speech",
        event_date="2026-05-14",
        event="Fed Governor Barr balance sheet speech",
        expected_impact="Confirmed Fed balance-sheet speech at a Money Marketeers FOMC event; relevant to reserves, liquidity, and QT interpretation.",
        source="federal_reserve",
        start_at="2026-05-14T19:00:00",
        timezone="America/New_York",
        event_kind="central_bank",
        verification_status="confirmed",
        source_url="https://www.federalreserve.gov/newsevents/2026-may.htm",
        source_name="Federal Reserve Calendar",
        raw={
            "speaker": "Governor Michael S. Barr",
            "venue": "Money Marketeers FOMC Event, New York, N.Y.",
            "correction": "Replaces unverified May 13 Fed/FOMC chair speech watch item.",
        },
    ),
    MarketEvent(
        id="macro-2026-05-14-fed-h41",
        event_date="2026-05-14",
        event="Federal Reserve H.4.1 balance sheet release",
        expected_impact="Weekly Fed balance sheet update; relevant to reserves, liquidity, and QT/QE interpretation.",
        source="federal_reserve",
        start_at="2026-05-14T16:30:00",
        timezone="America/New_York",
        event_kind="central_bank",
        verification_status="confirmed",
        source_url="https://www.federalreserve.gov/Releases/H41/default.htm",
        source_name="Federal Reserve H.4.1",
        raw={"release": "H.4.1 Factors Affecting Reserve Balances", "release_time": "4:30 p.m."},
    ),
    MarketEvent(
        id="macro-2026-05-14-trump-xi-summit",
        event_date="2026-05-14",
        event="Trump-Xi Beijing summit",
        expected_impact="Two-day geopolitical/trade risk event with broad market, FX, rates, and China-exposed equity implications.",
        source="geopolitical_watch",
        start_at="2026-05-14T00:00:00",
        end_at="2026-05-15T23:59:00",
        timezone="Asia/Shanghai",
        event_kind="geopolitical",
        verification_status="confirmed",
        source_url="https://koreajoongangdaily.joins.com/news/2026-03-26/national/diplomacy/Delayed-TrumpXi-summit-to-take-place-in-Beijing-on-May-14-and-15-White-House/2554084",
        source_name="Korea JoongAng Daily / Yonhap",
        raw={"location": "Beijing", "reported_dates": "May 14-15, 2026"},
    ),
]


def seed_requested_week_events(con: Any, enabled: bool = True) -> int:
    if not enabled:
        return 0
    con.execute(
        """
        DELETE FROM catalysts
        WHERE id IN (
            'macro-2026-05-11-warsh-confirmation-watch',
            'macro-2026-05-13-fed-chair-speech-watch'
        )
        """
    )
    return upsert_events(con, REQUESTED_WEEK_EVENTS)


def update_event_calendar(con: Any, config: AppConfig) -> dict[str, Any]:
    if not config.event_sources.enabled:
        return {"status": "disabled", "events": 0}
    inserted = seed_requested_week_events(con, config.event_sources.seed_requested_week)
    record_calendar_health(con, "ok", f"Upserted {inserted} deterministic market calendar rows")
    return {
        "status": "ok",
        "events": inserted,
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
