"""Live blog/memo ingestion via opencli (substack publication, web RSS)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

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
    """Generic web RSS is not supported by the installed opencli adapters.

    The ``web`` adapter only exposes ``read`` (single page → Markdown), not a feed
    parser, so there is no clean way to enumerate posts from an arbitrary RSS URL.
    Skip explicitly (rather than record spurious failures) until a real feed
    adapter exists; ``substack_urls`` is the supported blog path.
    """

    result = LiveFetchResult(source_id=slug(f"blog_{_host(url)}"))
    result.status = "skipped"
    result.detail = "generic web RSS unsupported by opencli; use substack_urls"
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
