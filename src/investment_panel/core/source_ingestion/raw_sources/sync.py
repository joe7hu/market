"""Auto-split from raw_sources.py — see ARCHITECTURE.md."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from investment_panel.core.db import query_rows
from investment_panel.core.instruments import normalize_symbol, symbols_from_text
from investment_panel.core.source_ingestion.canonical import record_source_run, store_item_with_signals
from investment_panel.core.source_ingestion.registry import ensure_source_registry
from investment_panel.core.source_ingestion.utils import parse_json, stable_id

from investment_panel.core.source_ingestion.raw_sources.browser import browser_capture_source_item, browser_signal_thesis, merge_capture_record, normalize_web_capture
from investment_panel.core.source_ingestion.raw_sources.coerce import add_ref_symbol, origin_ref_if_exact, redacted_url, urls_from_value
from investment_panel.core.source_ingestion.raw_sources.constants import BIRDCLAW_TWEETS_SOURCE_ID, BROWSER_CAPTURES_SOURCE_ID
from investment_panel.core.source_ingestion.raw_sources.io import load_profiles, read_json_snapshot, read_jsonl, snapshot_capture_items
from investment_panel.core.source_ingestion.raw_sources.tweets import merge_tweet_record, normalize_bookmark_row, normalize_canonical_tweet, normalize_observation_row, tweet_id_from_url, tweet_signal_thesis, tweet_source_item


def sync_private_raw_sources(con: Any, source_root: Path) -> dict[str, Any]:
    """Materialize mounted Birdclaw/browser raw exports into source_items.

    Arco brief beliefs remain the interpreted thesis layer. Raw items that are
    already referenced by an Arco thesis are still stored for traceability, but
    do not emit duplicate ticker signals.
    """

    ensure_source_registry(con)
    analysis_refs = analysis_reference_index(con)
    birdclaw = sync_birdclaw_primary(con, source_root / "birdclaw-primary" / "exports", analysis_refs)
    browser = sync_browser_primary(con, source_root / "browser-primary" / "captures", analysis_refs)
    return {
        "status": "private_raw_sources_synced",
        "source_root": str(source_root),
        "birdclaw_primary": birdclaw,
        "browser_primary": browser,
        "items": int(birdclaw.get("items") or 0) + int(browser.get("items") or 0),
        "signals": int(birdclaw.get("signals") or 0) + int(browser.get("signals") or 0),
        "analysis_overlap_items": int(birdclaw.get("analysis_overlap_items") or 0)
        + int(browser.get("analysis_overlap_items") or 0),
    }


def sync_birdclaw_primary(con: Any, export_root: Path, analysis_refs: dict[str, Any]) -> dict[str, Any]:
    data_dir = export_root / "data"
    result = {
        "source_id": BIRDCLAW_TWEETS_SOURCE_ID,
        "path": str(data_dir),
        "path_exists": data_dir.exists(),
        "raw_counts": {},
        "items": 0,
        "signals": 0,
        "analysis_overlap_items": 0,
    }
    if not data_dir.exists():
        record_raw_source_run(con, BIRDCLAW_TWEETS_SOURCE_ID, result, "failed", 0, 0)
        return result

    read_errors: list[str] = []
    profile_rows, errors = read_jsonl(data_dir / "profiles.jsonl")
    read_errors.extend(errors)
    profiles = load_profiles(profile_rows)
    tweets: dict[str, dict[str, Any]] = {}
    tweet_rows = 0
    for path in sorted((data_dir / "tweets").glob("*.jsonl")):
        rows, errors = read_jsonl(path)
        read_errors.extend(errors)
        for row in rows:
            tweet_rows += 1
            merge_tweet_record(tweets, normalize_canonical_tweet(row, profiles))

    bookmark_rows = 0
    bookmark_records, errors = read_jsonl(data_dir / "collections" / "bookmarks.jsonl")
    read_errors.extend(errors)
    for row in bookmark_records:
        bookmark_rows += 1
        merge_tweet_record(tweets, normalize_bookmark_row(row, profiles))

    observation_rows = 0
    observation_records, errors = read_jsonl(data_dir / "observations" / "tweets.jsonl")
    read_errors.extend(errors)
    for row in observation_records:
        observation_rows += 1
        merge_tweet_record(tweets, normalize_observation_row(row, profiles))

    raw_counts = {
        "tweets": tweet_rows,
        "bookmarks": bookmark_rows,
        "observations": observation_rows,
        "profiles": len(profile_rows),
    }
    if read_errors:
        result.update(
            {
                "raw_counts": {**raw_counts, "read_errors": len(read_errors)},
                "items": 0,
                "signals": 0,
                "analysis_overlap_items": 0,
                "read_errors": read_errors,
            }
        )
        record_raw_source_run(
            con,
            BIRDCLAW_TWEETS_SOURCE_ID,
            result,
            "failed",
            0,
            0,
            failure_detail="; ".join(read_errors),
        )
        return result

    replacement_items = []
    for tweet in sorted(tweets.values(), key=lambda item: str(item.get("id") or "")):
        if not tweet.get("id"):
            continue
        replacement_items.append(tweet_source_item(tweet, analysis_refs))

    item_count = 0
    signal_count = 0
    overlap_count = 0
    try:
        con.execute("BEGIN TRANSACTION")
        clear_source(con, BIRDCLAW_TWEETS_SOURCE_ID)
        for item in replacement_items:
            overlap = bool(item["raw"].get("analysis_overlap"))
            overlap_count += 1 if overlap else 0
            stored_items, stored_signals = store_item_with_signals(
                con,
                item,
                signal_type=item["source_kind"],
                thesis=tweet_signal_thesis(item),
                evidence_refs=item["evidence_refs"],
            )
            if overlap:
                stored_signals = suppress_overlap_signals(con, item, stored_signals)
            item_count += stored_items
            signal_count += stored_signals
        con.execute("COMMIT")
    except Exception as exc:
        rollback_quietly(con)
        failure_detail = f"source replacement failed: {exc}"
        result.update(
            {
                "raw_counts": raw_counts,
                "items": 0,
                "signals": 0,
                "analysis_overlap_items": 0,
                "write_error": failure_detail,
            }
        )
        record_raw_source_run(con, BIRDCLAW_TWEETS_SOURCE_ID, result, "failed", 0, 0, failure_detail=failure_detail)
        return result

    result.update(
        {
            "raw_counts": raw_counts,
            "items": item_count,
            "signals": signal_count,
            "analysis_overlap_items": overlap_count,
        }
    )
    record_raw_source_run(con, BIRDCLAW_TWEETS_SOURCE_ID, result, "ok", item_count, signal_count)
    return result


def sync_browser_primary(con: Any, captures_root: Path, analysis_refs: dict[str, Any]) -> dict[str, Any]:
    result = {
        "source_id": BROWSER_CAPTURES_SOURCE_ID,
        "path": str(captures_root),
        "path_exists": captures_root.exists(),
        "raw_counts": {},
        "items": 0,
        "signals": 0,
        "analysis_overlap_items": 0,
    }
    if not captures_root.exists():
        record_raw_source_run(con, BROWSER_CAPTURES_SOURCE_ID, result, "failed", 0, 0)
        return result

    captures: dict[str, dict[str, Any]] = {}
    snapshot_rows = 0
    read_errors: list[str] = []
    for path in sorted((captures_root / "snapshots").glob("web-captures-*.json")):
        snapshot, read_error = read_json_snapshot(path)
        if read_error:
            read_errors.append(read_error)
            continue
        snapshot_items, schema_errors = snapshot_capture_items(snapshot)
        read_errors.extend(f"{path}: {error}" for error in schema_errors)
        for item in snapshot_items:
            snapshot_rows += 1
            merge_capture_record(captures, normalize_web_capture(item, snapshot_path=path))

    event_rows = 0
    for path in sorted((captures_root / "events").glob("*.jsonl")):
        rows, errors = read_jsonl(path)
        read_errors.extend(errors)
        for row in rows:
            event_rows += 1
            merge_capture_record(captures, normalize_web_capture(row, event_path=path))

    if read_errors:
        result.update(
            {
                "raw_counts": {"snapshot_items": snapshot_rows, "events": event_rows, "read_errors": len(read_errors)},
                "items": 0,
                "signals": 0,
                "analysis_overlap_items": 0,
                "read_errors": read_errors,
            }
        )
        record_raw_source_run(
            con,
            BROWSER_CAPTURES_SOURCE_ID,
            result,
            "failed",
            0,
            0,
            failure_detail="; ".join(read_errors),
        )
        return result

    replacement_items = [
        browser_capture_source_item(capture, analysis_refs)
        for capture in sorted(captures.values(), key=lambda item: str(item.get("url") or item.get("id") or ""))
    ]
    item_count = 0
    signal_count = 0
    overlap_count = 0
    try:
        con.execute("BEGIN TRANSACTION")
        clear_source(con, BROWSER_CAPTURES_SOURCE_ID)
        for item in replacement_items:
            overlap = bool(item["raw"].get("analysis_overlap"))
            overlap_count += 1 if overlap else 0
            stored_items, stored_signals = store_item_with_signals(
                con,
                item,
                signal_type=item["source_kind"],
                thesis=browser_signal_thesis(item),
                evidence_refs=item["evidence_refs"],
            )
            if overlap:
                stored_signals = suppress_overlap_signals(con, item, stored_signals)
            item_count += stored_items
            signal_count += stored_signals
        con.execute("COMMIT")
    except Exception as exc:
        rollback_quietly(con)
        failure_detail = f"source replacement failed: {exc}"
        result.update(
            {
                "raw_counts": {"snapshot_items": snapshot_rows, "events": event_rows, "read_errors": len(read_errors)},
                "items": 0,
                "signals": 0,
                "analysis_overlap_items": 0,
                "read_errors": read_errors,
                "write_error": failure_detail,
            }
        )
        record_raw_source_run(con, BROWSER_CAPTURES_SOURCE_ID, result, "failed", 0, 0, failure_detail=failure_detail)
        return result

    result.update(
        {
            "raw_counts": {"snapshot_items": snapshot_rows, "events": event_rows, "read_errors": len(read_errors)},
            "items": item_count,
            "signals": signal_count,
            "analysis_overlap_items": overlap_count,
            "read_errors": read_errors,
        }
    )
    status = "failed" if read_errors else "ok"
    record_raw_source_run(
        con,
        BROWSER_CAPTURES_SOURCE_ID,
        result,
        status,
        item_count,
        signal_count,
        failure_detail="; ".join(read_errors),
    )
    return result


def analysis_reference_index(con: Any) -> dict[str, Any]:
    urls: set[str] = set()
    tweet_ids: set[str] = set()
    url_symbols: dict[str, set[str]] = {}
    tweet_symbols: dict[str, set[str]] = {}
    for row in query_rows(con, "SELECT id, symbol, source_url, claims FROM birdclaw_theses"):
        symbol = normalize_symbol(str(row.get("symbol") or ""))
        refs = urls_from_value({"source_url": row.get("source_url"), "claims": parse_json(row.get("claims"))})
        urls.update(refs)
        redacted_refs = {redacted for ref in refs for redacted in [redacted_url(ref)] if redacted}
        public_refs = {public for ref in refs for public in [origin_ref_if_exact(ref)] if public}
        urls.update(redacted_refs)
        urls.update(public_refs)
        for ref in refs:
            if symbol:
                add_ref_symbol(url_symbols, ref, symbol)
                redacted = redacted_url(ref)
                public = origin_ref_if_exact(ref)
                if redacted:
                    add_ref_symbol(url_symbols, redacted, symbol)
                if public:
                    add_ref_symbol(url_symbols, public, symbol)
            tweet_id = tweet_id_from_url(ref)
            if tweet_id:
                tweet_ids.add(tweet_id)
                if symbol:
                    add_ref_symbol(tweet_symbols, tweet_id, symbol)
    return {"urls": urls, "tweet_ids": tweet_ids, "url_symbols": url_symbols, "tweet_symbols": tweet_symbols}


def clear_source(con: Any, source_id: str) -> None:
    con.execute("DELETE FROM ticker_source_signals WHERE source_id = ?", [source_id])
    con.execute("DELETE FROM source_items WHERE source_id = ?", [source_id])


def rollback_quietly(con: Any) -> None:
    try:
        con.execute("ROLLBACK")
    except Exception:
        pass


def suppress_overlap_signals(con: Any, item: dict[str, Any], stored_signals: int) -> int:
    overlap_symbols = [str(symbol) for symbol in item.get("raw", {}).get("analysis_overlap_symbols") or [] if symbol]
    if not overlap_symbols:
        return stored_signals
    placeholders = ",".join("?" for _ in overlap_symbols)
    params = [item["id"], *overlap_symbols]
    rows = query_rows(
        con,
        f"SELECT count(*) AS count FROM ticker_source_signals WHERE source_item_id = ? AND symbol IN ({placeholders})",
        params,
    )
    deleted_count = int(rows[0]["count"] or 0) if rows else 0
    con.execute(f"DELETE FROM ticker_source_signals WHERE source_item_id = ? AND symbol IN ({placeholders})", params)
    return max(0, stored_signals - deleted_count)


def record_raw_source_run(
    con: Any,
    source_id: str,
    raw: dict[str, Any],
    status: str,
    item_count: int,
    ticker_count: int,
    failure_detail: str | None = None,
) -> None:
    now = datetime.now(UTC).isoformat()
    if failure_detail is None:
        failure_detail = "" if status == "ok" else f"{raw.get('path')} is not available"
    record_source_run(
        con,
        source_id=source_id,
        run_id=f"{source_id}:{now}",
        capability="private_raw_source_sync",
        started_at=now,
        finished_at=now,
        status=status,
        item_count=item_count,
        ticker_count=ticker_count,
        failure_detail=failure_detail,
        raw=raw,
    )
