"""Live blog/memo ingestion via opencli (substack publication, web RSS)."""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import unescape
import re
from typing import Any
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from investment_panel.providers.opencli import OpenCliRateLimitError, OpenCliRunner, ensure_list
from investment_panel.core.source_ingestion.canonical import store_item_with_signals
from investment_panel.core.source_ingestion.live.common import (
    LiveFetchResult,
    extract_symbols,
    normalize_published,
    record_live_run,
)
from investment_panel.core.source_ingestion.utils import slug, stable_id


def fetch_substack(con: Any, runner: OpenCliRunner, url: str, *, known: set[str] | None = None) -> LiveFetchResult:
    source_id = slug(f"blog_{_host(url)}")
    result = LiveFetchResult(source_id=source_id)
    if not url:
        result.status = "skipped"
        result.detail = "empty substack url"
        return result
    try:
        payload = runner.read_json(["substack", "publication", url])
    except OpenCliRateLimitError as exc:
        return _record(result, "rate_limited", exc, con, capability="substack", run_key=url, rate_limited=True)
    except Exception as exc:  # noqa: BLE001
        return _record(result, "failed", exc, con, capability="substack", run_key=url)

    _ingest_posts(con, ensure_list(payload), result, source_id=source_id, source_url=url, known=known)
    record_live_run(con, result, capability="substack", run_key=url)
    return result


def fetch_web_rss(con: Any, runner: OpenCliRunner, url: str, *, known: set[str] | None = None) -> LiveFetchResult:
    """Fetch and parse a generic RSS/Atom feed URL."""

    source_id = slug(f"blog_{_host(url)}")
    result = LiveFetchResult(source_id=source_id)
    if not url:
        result.status = "skipped"
        result.detail = "empty rss url"
        return result
    try:
        posts = _fetch_feed_posts(url)
    except Exception as exc:  # noqa: BLE001
        return _record(result, "failed", exc, con, capability="rss", run_key=url)
    _ingest_posts(con, posts, result, source_id=source_id, source_url=url, known=known)
    record_live_run(con, result, capability="rss", run_key=url)
    return result


def _ingest_posts(
    con: Any,
    rows: list[dict[str, Any]],
    result: LiveFetchResult,
    *,
    source_id: str,
    source_url: str,
    known: set[str] | None,
) -> None:
    now = datetime.now(UTC).isoformat()
    for row in rows:
        title = row.get("title") or row.get("headline")
        if not title:
            continue
        link = row.get("link") or row.get("url") or source_url
        summary = row.get("summary") or row.get("description") or row.get("subtitle") or ""
        symbols = extract_symbols(f"{title} {summary}", known)
        item_id = f"blog:{stable_id(source_id, title, link)}"
        stored_items, stored_signals = store_item_with_signals(
            con,
            {
                "id": item_id,
                "source_id": source_id,
                "source_kind": "blog",
                "title": title,
                "url": link,
                "author": row.get("author") or _host(source_url),
                "published_at": normalize_published(
                    row.get("published") or row.get("published_at") or row.get("date"), fallback_iso=now
                ),
                "observed_at": now,
                "summary": summary or title,
                "tickers": symbols,
                "evidence_refs": [link] if link else [],
                "raw": {**row, "source_family": "blog"},
                "license_status": "provider_link_only",
            },
            signal_type="blog",
            thesis=title,
            evidence_refs=[link] if link else [],
        )
        result.items += stored_items
        result.signals += stored_signals


def _host(url: str) -> str:
    try:
        netloc = urlparse(url).netloc or url
    except Exception:
        netloc = url
    return netloc.replace("www.", "") or "blog"


def _fetch_feed_posts(url: str) -> list[dict[str, Any]]:
    request = Request(url, headers={"User-Agent": "joehu-market-panel/0.1 contact:local"})
    try:
        with urlopen(request, timeout=25) as response:  # noqa: S310 - configured user source URL
            body = response.read()
    except URLError as exc:
        raise RuntimeError(str(exc)) from exc
    root = ET.fromstring(body)
    rows = _rss_items(root)
    if not rows:
        rows = _atom_entries(root)
    return rows


def _rss_items(root: ET.Element) -> list[dict[str, Any]]:
    rows = []
    for item in root.findall(".//item"):
        title = _child_text(item, "title")
        link = _child_text(item, "link") or _child_text(item, "guid")
        summary = _child_text(item, "description") or _child_text(item, "{http://purl.org/rss/1.0/modules/content/}encoded")
        published = _normalize_feed_date(_child_text(item, "pubDate") or _child_text(item, "{http://purl.org/dc/elements/1.1/}date"))
        author = _child_text(item, "{http://purl.org/dc/elements/1.1/}creator") or _child_text(item, "author")
        rows.append({"title": title, "link": link, "summary": _plain_text(summary), "published": published, "author": author})
    return [row for row in rows if row.get("title")]


def _atom_entries(root: ET.Element) -> list[dict[str, Any]]:
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    rows = []
    for entry in root.findall(".//atom:entry", ns):
        link = ""
        for link_node in entry.findall("atom:link", ns):
            if link_node.get("href"):
                link = str(link_node.get("href"))
                break
        rows.append(
            {
                "title": _child_text(entry, "{http://www.w3.org/2005/Atom}title"),
                "link": link,
                "summary": _plain_text(
                    _child_text(entry, "{http://www.w3.org/2005/Atom}summary")
                    or _child_text(entry, "{http://www.w3.org/2005/Atom}content")
                ),
                "published": _normalize_feed_date(
                    _child_text(entry, "{http://www.w3.org/2005/Atom}published")
                    or _child_text(entry, "{http://www.w3.org/2005/Atom}updated")
                ),
                "author": _child_text(entry, "{http://www.w3.org/2005/Atom}author"),
            }
        )
    return [row for row in rows if row.get("title")]


def _child_text(node: ET.Element, name: str) -> str:
    child = node.find(name)
    if child is None or child.text is None:
        return ""
    return child.text.strip()


def _plain_text(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _normalize_feed_date(value: str) -> str:
    if not value:
        return ""
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError):
        return value


def _record(
    result: LiveFetchResult,
    status: str,
    exc: Exception,
    con: Any,
    *,
    capability: str,
    run_key: Any,
    rate_limited: bool = False,
) -> LiveFetchResult:
    result.status = status
    result.error = str(exc)
    result.rate_limited = rate_limited
    record_live_run(con, result, capability=capability, run_key=run_key)
    return result
