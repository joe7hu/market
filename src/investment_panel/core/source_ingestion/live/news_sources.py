"""Live news ingestion via opencli (bloomberg / reuters / google-news / hackernews).

News rows land in ``news_items`` (via the existing ``store_news_rows``) and in the
canonical ``source_items`` / ``ticker_source_signals`` so the source catalog and
decision universe see them immediately. Symbols are extracted deterministically
($CASHTAG + DB-known mentions).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from investment_panel.providers.opencli import OpenCliRateLimitError, OpenCliRunner, ensure_list
from investment_panel.core.source_ingestion.canonical import store_item_with_signals
from investment_panel.core.source_ingestion.live.common import (
    LiveFetchResult,
    extract_symbols,
    normalize_published,
    record_live_fetch_failure,
    record_live_run,
)
from investment_panel.core.source_ingestion.utils import slug, stable_id

# provider -> opencli args (verified against the installed adapters). reuters has
# no `news` subcommand (search only); google news lives on the `google` adapter;
# hackernews is the always-public fallback wire.
_PROVIDER_COMMANDS: dict[str, list[str]] = {
    "bloomberg": ["bloomberg", "markets"],
    "reuters": ["reuters", "search", "stock market"],
    "google-news": ["google", "news", "stock market"],
    "hackernews": ["hackernews", "top"],
}


def fetch_news(con: Any, runner: OpenCliRunner, provider: str, *, limit: int = 30, known: set[str] | None = None) -> LiveFetchResult:
    source_id = slug(f"news_{provider}")
    result = LiveFetchResult(source_id=source_id)
    args = _PROVIDER_COMMANDS.get(provider)
    if not args:
        result.status = "skipped"
        result.detail = f"unknown news provider {provider}"
        return result
    command = [*args, "--limit", str(limit)]
    try:
        payload = runner.read_json(command)
    except OpenCliRateLimitError as exc:
        return record_live_fetch_failure(con, result, exc, capability="news", run_key=provider, status="rate_limited", rate_limited=True)
    except Exception as exc:  # noqa: BLE001
        return record_live_fetch_failure(con, result, exc, capability="news", run_key=provider)

    rows = ensure_list(payload)
    news_rows = _to_news_rows(rows, provider, known)
    if news_rows:
        # Lazy import avoids a circular import at source_ingestion package init
        # (free_sources -> decision -> sources -> source_ingestion).
        from investment_panel.core.free_sources import store_news_rows

        store_news_rows(con, news_rows, source=source_id)
    for row in news_rows:
        symbols = row.get("related_symbols") or []
        stored_items, stored_signals = store_item_with_signals(
            con,
            {
                "id": f"news:{row['id']}",
                "source_id": source_id,
                "source_kind": "news",
                "title": row.get("title"),
                "url": row.get("link"),
                "author": provider,
                "published_at": row.get("published"),
                "observed_at": row.get("published"),
                "summary": row.get("summary") or row.get("title"),
                "tickers": symbols,
                "evidence_refs": [row["link"]] if row.get("link") else [],
                "raw": row.get("raw") or row,
                "license_status": "provider_link_only",
            },
            signal_type="news",
            thesis=row.get("title"),
            evidence_refs=[row["link"]] if row.get("link") else [],
        )
        result.items += stored_items
        result.signals += stored_signals
    record_live_run(con, result, capability="news", run_key=provider)
    return result


def _to_news_rows(rows: list[dict[str, Any]], provider: str, known: set[str] | None) -> list[dict[str, Any]]:
    now = datetime.now(UTC).isoformat()
    out: list[dict[str, Any]] = []
    for row in rows:
        title = row.get("title") or row.get("headline")
        if not title:
            continue
        link = row.get("link") or row.get("url")
        summary = row.get("summary") or row.get("description") or ""
        symbols = extract_symbols(f"{title} {summary}", known)
        out.append(
            {
                "id": str(row.get("id") or stable_id(provider, title, link)),
                "title": title,
                "link": link,
                "summary": summary,
                "provider": provider,
                "published": normalize_published(
                    row.get("published") or row.get("published_at") or row.get("date"), fallback_iso=now
                ),
                "related_symbols": symbols,
                "raw": row,
            }
        )
    return out
