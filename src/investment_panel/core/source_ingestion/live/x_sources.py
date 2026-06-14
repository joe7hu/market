"""Live X/Twitter ingestion via opencli (list primary, per-account fallback).

opencli twitter JSON lands in the *same* ``source_items`` shape as Birdclaw
exports by reusing the shapers in ``raw_sources/tweets.py`` — so the social graph
stays unified. The X list call (one request) is the primary; per-account fetches
are the staggered, capped fallback.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from investment_panel.providers.opencli import OpenCliRateLimitError, OpenCliRunner, ensure_list
from investment_panel.core.source_ingestion.raw_sources.coerce import normalize_timestamp
from investment_panel.core.source_ingestion.canonical import store_item_with_signals
from investment_panel.core.source_ingestion.live.common import (
    LiveFetchResult,
    existing_source_item_ids,
    extract_symbols,
    record_live_run,
)
from investment_panel.core.source_ingestion.raw_sources.constants import BIRDCLAW_TWEETS_SOURCE_ID
from investment_panel.core.source_ingestion.raw_sources.tweets import (
    tweet_signal_thesis,
    tweet_source_item,
)


def fetch_x_list(
    con: Any,
    runner: OpenCliRunner,
    list_id: str,
    *,
    limit: int = 30,
    known: set[str] | None = None,
) -> LiveFetchResult:
    """Primary X fetch: one request covers a curated list."""

    result = LiveFetchResult(source_id=BIRDCLAW_TWEETS_SOURCE_ID)
    if not list_id:
        result.status = "skipped"
        result.detail = "no x list_id configured"
        return result
    try:
        payload = runner.read_json(["twitter", "list-tweets", str(list_id), "--limit", str(limit)])
    except OpenCliRateLimitError as exc:
        return _rate_limited(result, exc, con, capability="x_list", run_key=list_id)
    except Exception as exc:  # noqa: BLE001 - any fetch failure records a failed run
        return _failed(result, exc, con, capability="x_list", run_key=list_id)

    _ingest_tweets(con, ensure_list(payload), result, known=known)
    record_live_run(con, result, capability="x_list", run_key=list_id)
    return result


def fetch_x_account(
    con: Any,
    runner: OpenCliRunner,
    handle: str,
    *,
    limit: int = 30,
    known: set[str] | None = None,
) -> LiveFetchResult:
    """Fallback X fetch: per-account timeline for a priority handle."""

    result = LiveFetchResult(source_id=BIRDCLAW_TWEETS_SOURCE_ID)
    clean_handle = str(handle or "").lstrip("@").strip()
    if not clean_handle:
        result.status = "skipped"
        result.detail = "empty handle"
        return result
    try:
        payload = runner.read_json(["twitter", "tweets", clean_handle, "--limit", str(limit)])
    except OpenCliRateLimitError as exc:
        return _rate_limited(result, exc, con, capability="x_account", run_key=clean_handle)
    except Exception as exc:  # noqa: BLE001
        return _failed(result, exc, con, capability="x_account", run_key=clean_handle)

    _ingest_tweets(con, ensure_list(payload), result, known=known)
    record_live_run(con, result, capability="x_account", run_key=clean_handle)
    return result


def _ingest_tweets(con: Any, rows: list[dict[str, Any]], result: LiveFetchResult, *, known: set[str] | None) -> None:
    tweets = [_normalize_opencli_tweet(row) for row in rows]
    tweets = [tweet for tweet in tweets if tweet.get("id")]
    item_ids = [f"x_tweet:{tweet['id']}" for tweet in tweets]
    existing = existing_source_item_ids(con, item_ids)
    for tweet in tweets:
        item_id = f"x_tweet:{tweet['id']}"
        if item_id in existing:
            result.skipped += 1
            continue
        item = tweet_source_item(tweet, analysis_refs={})
        # Layer in DB-known bare symbol mentions on top of the cashtag tickers the
        # shaper already extracted, then re-derive signals deterministically.
        symbols = extract_symbols(_tweet_text(tweet), known)
        item["tickers"] = sorted(set(item.get("tickers") or []) | set(symbols))
        stored_items, stored_signals = store_item_with_signals(
            con,
            item,
            signal_type="tweet",
            thesis=tweet_signal_thesis(item),
            evidence_refs=item.get("evidence_refs") or [],
        )
        result.items += stored_items
        result.signals += stored_signals


def _normalize_opencli_tweet(row: dict[str, Any]) -> dict[str, Any]:
    """Map opencli twitter JSON into the tweet dict ``tweet_source_item`` expects."""

    quoted = row.get("quoted_tweet") if isinstance(row.get("quoted_tweet"), dict) else None
    return {
        "id": str(row.get("id") or ""),
        "url": row.get("url"),
        "text": row.get("text") or "",
        "created_at": _normalize_tweet_time(row.get("created_at")),
        "kind": "x_retweet" if row.get("is_retweet") else "x_tweet",
        "author": {
            "handle": row.get("author"),
            "displayName": row.get("name") or row.get("author"),
        },
        "metrics": {
            "likeCount": row.get("likes"),
            "retweetCount": row.get("retweets"),
            "replyCount": row.get("replies"),
            "viewCount": row.get("views"),
        },
        "media": row.get("media_urls") or [],
        "quoted_tweet": quoted,
        "raw_sources": ["opencli_twitter"],
    }


def _normalize_tweet_time(value: Any) -> Any:
    """Normalize opencli/Twitter timestamps to ISO for DuckDB storage.

    Falls back to the X-style ``"Tue Jun 02 04:12:44 +0000 2026"`` format that
    the shared ``normalize_timestamp`` (ISO + RFC-2822 only) does not handle.
    """

    if value in (None, ""):
        return value
    normalized = normalize_timestamp(value)
    if normalized != value:
        return normalized
    try:
        return datetime.strptime(str(value), "%a %b %d %H:%M:%S %z %Y").isoformat()
    except ValueError:
        return value


def _tweet_text(tweet: dict[str, Any]) -> str:
    parts = [str(tweet.get("text") or "")]
    quoted = tweet.get("quoted_tweet")
    if isinstance(quoted, dict):
        parts.append(str(quoted.get("text") or ""))
    return " ".join(part for part in parts if part)


def _rate_limited(result: LiveFetchResult, exc: Exception, con: Any, *, capability: str, run_key: Any) -> LiveFetchResult:
    result.status = "rate_limited"
    result.rate_limited = True
    result.error = str(exc)
    record_live_run(con, result, capability=capability, run_key=run_key)
    return result


def _failed(result: LiveFetchResult, exc: Exception, con: Any, *, capability: str, run_key: Any) -> LiveFetchResult:
    result.status = "failed"
    result.error = str(exc)
    record_live_run(con, result, capability=capability, run_key=run_key)
    return result
