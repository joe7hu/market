"""Refresh official macro schedules into PostgreSQL event facts."""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime, time, timedelta
import hashlib
import gzip
import json
import re
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from investment_panel.core.config import load_config
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.source_facts import SourceFactRepository


SOURCE_ID = "official-event-calendar"
BLS_SCHEDULES = (
    ("https://www.bls.gov/schedule/news_release/cpi.htm", "Consumer Price Index", "inflation"),
    ("https://www.bls.gov/schedule/news_release/empsit.htm", "Employment Situation", "labor"),
    ("https://www.bls.gov/schedule/news_release/ppi.htm", "Producer Price Index", "inflation"),
    ("https://www.bls.gov/schedule/news_release/jolts.htm", "Job Openings and Labor Turnover Survey", "labor"),
)
ROW_RE = re.compile(
    r"(?P<month>[A-Z][a-z]+ \d{4})\s+(?P<date>[A-Z][a-z]{2}\.?\s+\d{1,2},\s+\d{4})\s+(?P<time>\d{2}:\d{2}\s+[AP]M)"
)


def run(config_path: str | None = None) -> dict[str, Any]:
    config = load_config(config_path)
    if not config.event_sources.enabled:
        return {"status": "disabled", "database": "postgresql", "events": 0}
    runtime = runtime_for_config(config)
    repository = IngestionRepository(runtime)
    repository.register_source(
        SOURCE_ID,
        name="Official event calendar",
        family="events",
        kind="calendar",
        origin="BLS, DOL, and Federal Reserve",
        capabilities={"macro_events": True},
    )
    run_id = repository.start_run(SOURCE_ID, "macro_events")
    events: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    payloads: list[dict[str, str]] = []
    if config.event_sources.bls_enabled:
        fetched, errors, payloads = _bls_events(config.market_data.user_agent)
        events.extend(fetched)
    if config.event_sources.dol_enabled:
        events.extend(_weekly_events("dol", "Weekly unemployment insurance claims", time(8, 30), "labor"))
    if config.event_sources.federal_reserve_enabled:
        events.extend(_weekly_events("federal_reserve", "Federal Reserve H.4.1 balance sheet release", time(16, 30), "central_bank"))
    try:
        payload_id = None
        if payloads:
            archive = _archive_payload(config, run_id, payloads)
            payload_id = repository.record_payload_file(run_id, archive, source_pages=len(payloads))
        count = SourceFactRepository(runtime).store_market_events(run_id, SOURCE_ID, events, payload_id=payload_id)
        status = "partial" if errors else "succeeded"
        repository.finish_run(
            run_id,
            status,
            item_count=count,
            failure_detail="; ".join(row["error"] for row in errors[:10]) or None,
            summary={"source_errors": errors},
        )
    except Exception as exc:
        repository.finish_run(run_id, "failed", failure_detail=f"{type(exc).__name__}: {exc}")
        raise
    return {
        "status": "partial" if errors else "ok",
        "database": "postgresql",
        "run_id": str(run_id),
        "events": count,
        "source_errors": errors,
    }


def _bls_events(user_agent: str) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[dict[str, str]]]:
    events: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    payloads: list[dict[str, str]] = []
    for url, release, kind in BLS_SCHEDULES:
        try:
            response = httpx.get(url, headers={"User-Agent": user_agent}, timeout=15, follow_redirects=True)
            response.raise_for_status()
            payloads.append({"url": url, "body": response.text})
            events.extend(_parse_bls(response.text, release, kind, url))
        except Exception as exc:
            errors.append({"source": url, "error": f"{type(exc).__name__}: {exc}"})
    return events, errors, payloads


def _archive_payload(config: Any, run_id: Any, payloads: list[dict[str, str]]) -> Any:
    from pathlib import Path

    preferred = Path(config.nas.market_dir) / "provider-payloads"
    root = preferred if preferred.parent.exists() else Path(config.report_dir).parent / "provider-payloads"
    path = root / SOURCE_ID / datetime.now(UTC).strftime("%Y/%m/%d") / f"{run_id}.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(payloads, handle, ensure_ascii=False, separators=(",", ":"))
    return path


def _parse_bls(html: str, release: str, kind: str, url: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for match in ROW_RE.finditer(html):
        try:
            day = datetime.strptime(match.group("date").replace(".", ""), "%b %d, %Y").date()
            clock = datetime.strptime(match.group("time"), "%I:%M %p").time()
        except ValueError:
            continue
        title = f"{match.group('month')} {release} release"
        starts = datetime.combine(day, clock, tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)
        events.append(_event("bls", title, starts, kind, url, "confirmed"))
    return events


def _weekly_events(source: str, title: str, clock: time, kind: str, *, weeks: int = 8) -> list[dict[str, Any]]:
    today = date.today()
    first = today + timedelta(days=(3 - today.weekday()) % 7)
    return [
        _event(
            source,
            title,
            datetime.combine(first + timedelta(days=7 * index), clock, tzinfo=ZoneInfo("America/New_York")).astimezone(UTC),
            kind,
            "https://oui.doleta.gov/unemploy/claims.asp" if source == "dol" else "https://www.federalreserve.gov/releases/h41/",
            "scheduled",
        )
        for index in range(weeks)
    ]


def _event(source: str, title: str, starts_at: datetime, kind: str, url: str, verification: str) -> dict[str, Any]:
    key = hashlib.sha256(f"{source}|{starts_at.date()}|{title}".encode()).hexdigest()
    return {
        "source_key": key,
        "event_scope": "macro",
        "event_kind": kind,
        "title": title,
        "starts_at": starts_at,
        "importance": "high",
        "verification_status": verification,
        "source_url": url,
        "expected_impact": f"{title}; review rates, liquidity, and portfolio exposure.",
        "details": {"official_source": source},
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    print(json.dumps(run(args.config), indent=2, default=str))


if __name__ == "__main__":
    main()
