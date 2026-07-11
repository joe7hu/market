"""Arco snapshot ingestion for thesis-flow evidence."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from investment_panel.core.config import ArcoConfig
from investment_panel.core.instruments import symbols_from_text


def json_dumps(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        default=lambda item: item.isoformat() if isinstance(item, (date, datetime)) else str(item),
    )


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def latest_file(directory: Path, pattern: str) -> Path | None:
    files = sorted(directory.glob(pattern))
    return files[-1] if files else None


def load_arco_context(config: ArcoConfig) -> dict[str, Any]:
    raw_dir = config.raw_dir
    signals_path = raw_dir / config.signals_path
    beliefs_path = raw_dir / config.beliefs_path
    signals = read_json(signals_path, {"topics": [], "subtopics": []})
    beliefs = read_json(beliefs_path, {"beliefs": []})
    brief_beliefs_path = latest_file(raw_dir, config.brief_beliefs_glob)
    bookmarks_path = latest_file(raw_dir, config.birdclaw_bookmarks_glob)
    web_captures_path = latest_file(raw_dir, config.web_captures_glob)
    manifest_path = latest_file(raw_dir, config.source_manifest_glob)
    brief_beliefs = read_json(brief_beliefs_path, {"beliefs": []}) if brief_beliefs_path else {}
    bookmarks = read_json(bookmarks_path, {"canonicalItems": [], "items": []}) if bookmarks_path else {}
    manifest = read_json(manifest_path, {}) if manifest_path else {}
    source_snapshots = load_source_snapshots(raw_dir, manifest)
    manifest_snapshot_count = len([entry for entry in manifest.get("sourceSnapshots", []) or [] if isinstance(entry, dict)])
    if not source_snapshots:
        source_snapshots = [
            snapshot_record("birdclaw_bookmarks", bookmarks_path, bookmarks),
            snapshot_record("browser_captures", web_captures_path, read_json(web_captures_path, {}) if web_captures_path else {}),
        ]
        source_snapshots = [record for record in source_snapshots if record["path"]]
    return {
        "signals": signals,
        "beliefs": beliefs,
        "brief_beliefs": brief_beliefs,
        "bookmarks": bookmarks,
        "manifest": manifest,
        "source_snapshots": source_snapshots,
        "brief_beliefs_path": str(brief_beliefs_path) if brief_beliefs_path else None,
        "bookmarks_path": str(bookmarks_path) if bookmarks_path else None,
        "web_captures_path": str(web_captures_path) if web_captures_path else None,
        "manifest_path": str(manifest_path) if manifest_path else None,
        "source_status": {
            "raw_dir_exists": raw_dir.exists(),
            "signals_path": str(signals_path) if signals_path.exists() else None,
            "beliefs_path": str(beliefs_path) if beliefs_path.exists() else None,
            "brief_beliefs_path": str(brief_beliefs_path) if brief_beliefs_path else None,
            "bookmarks_path": str(bookmarks_path) if bookmarks_path else None,
            "web_captures_path": str(web_captures_path) if web_captures_path else None,
            "manifest_path": str(manifest_path) if manifest_path else None,
            "manifest_snapshot_count": manifest_snapshot_count,
            "source_snapshot_count": len(source_snapshots),
        },
    }


def load_source_snapshots(raw_dir: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for entry in manifest.get("sourceSnapshots", []) or []:
        if not isinstance(entry, dict):
            continue
        path = resolve_snapshot_path(raw_dir, entry.get("path"))
        if not path:
            continue
        snapshots.append(
            {
                "source_id": str(entry.get("sourceId") or path.stem),
                "path": str(path),
                "schema": entry.get("schema"),
                "snapshot": read_json(path, {}),
            }
        )
    return snapshots


def resolve_snapshot_path(raw_dir: Path, manifest_path: Any) -> Path | None:
    if not manifest_path:
        return None
    candidate = Path(str(manifest_path)).expanduser()
    if candidate.is_absolute():
        return candidate if candidate.exists() else None
    direct = raw_dir / candidate
    if direct.exists():
        return direct
    basename = raw_dir / candidate.name
    if basename.exists():
        return basename
    for parent in [raw_dir, *raw_dir.parents]:
        rooted = parent / candidate
        if rooted.exists():
            return rooted
    return None


def snapshot_record(source_id: str, path: Path | None, snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "path": str(path) if path else None,
        "schema": snapshot.get("schema") if isinstance(snapshot, dict) else None,
        "snapshot": snapshot,
    }


def flatten_arco_items(context: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    brief_items = flatten_brief_beliefs(context)
    if brief_items:
        return brief_items

    for signal in context.get("signals", {}).get("subtopics", []) or []:
        evidence = signal.get("examples") or signal.get("topSources") or []
        text = " ".join(
            str(value)
            for value in [
                signal.get("subtopic"),
                signal.get("topic"),
                signal.get("summary"),
                signal.get("durableTopicId"),
            ]
            if value
        )
        items.append(
            {
                "id": signal.get("id") or stable_id(text),
                "source_type": "arco_signal",
                "title": signal.get("subtopic") or signal.get("topic"),
                "text": text,
                "score": signal.get("contrarianScore") or signal.get("score"),
                "raw": signal,
                "evidence": evidence,
            }
        )
    for belief in context.get("beliefs", {}).get("beliefs", []) or []:
        text = " ".join(
            str(value)
            for value in [belief.get("title"), belief.get("claim"), belief.get("bet"), belief.get("whyNow")]
            if value
        )
        items.append(
            {
                "id": belief.get("id") or stable_id(text),
                "source_type": "arco_belief",
                "title": belief.get("title"),
                "text": text,
                "score": confidence_score(str(belief.get("confidence", ""))),
                "raw": belief,
                "evidence": belief.get("evidence", []),
            }
        )
    seen_source_ids: set[str] = set()
    for record in context.get("source_snapshots", []) or []:
        snapshot = record.get("snapshot") or {}
        source_id = str(record.get("source_id") or "arco_snapshot")
        for item in snapshot.get("canonicalItems", []) or []:
            items.append(source_item(item, source_id, 0.45))
            if item.get("id"):
                seen_source_ids.add(str(item["id"]))
        for item in snapshot.get("observedItems", []) or []:
            items.append(source_item(item, f"{source_id}_observed", 0.25))
            if item.get("id"):
                seen_source_ids.add(str(item["id"]))

    bookmark_items = context.get("bookmarks", {}).get("canonicalItems") or context.get("bookmarks", {}).get("items") or []
    for item in bookmark_items:
        if item.get("id") and str(item["id"]) in seen_source_ids:
            continue
        items.append(source_item(item, "birdclaw_bookmark", 0.45))
    return items


def flatten_brief_beliefs(context: dict[str, Any]) -> list[dict[str, Any]]:
    brief = context.get("brief_beliefs") or {}
    beliefs = brief.get("beliefs", []) or []
    if not beliefs:
        return []
    source_index = source_items_by_url(context.get("source_snapshots", []) or [])
    items: list[dict[str, Any]] = []
    for belief in beliefs:
        if not isinstance(belief, dict):
            continue
        matched_sources = matched_evidence_sources(belief, source_index)
        fields = [
            belief.get("title"),
            belief.get("topic"),
            belief.get("claim"),
            belief.get("bet"),
            belief.get("whyNow"),
            belief.get("counterSignal"),
            belief.get("confidenceRationale"),
            belief.get("nextAction"),
        ]
        text = " ".join(str(value) for value in fields if value)
        evidence_text = " ".join(item_text(source) for source in matched_sources)
        evidence = {
            "brief_evidence": belief.get("evidence", []),
            "primary_sources": belief.get("primarySources", []),
            "matched_source_items": matched_sources,
            "source_brief": belief.get("sourceBrief") or brief.get("sourceBrief"),
            "validation_warnings": belief.get("validationWarnings", []),
        }
        items.append(
            {
                "id": belief.get("id") or stable_id(text),
                "source_type": "arco_brief_belief",
                "title": belief.get("title") or belief.get("topic"),
                "text": " ".join(part for part in (text, evidence_text) if part),
                "score": confidence_score(str(belief.get("confidence", ""))),
                "raw": belief,
                "evidence": evidence,
            }
        )
    return items


def source_items_by_url(source_snapshots: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for record in source_snapshots:
        snapshot = record.get("snapshot") or {}
        for collection in ("canonicalItems", "observedItems", "items"):
            for item in snapshot.get(collection, []) or []:
                if not isinstance(item, dict):
                    continue
                for url in item_urls(item):
                    index.setdefault(url, item)
    return index


def matched_evidence_sources(belief: dict[str, Any], source_index: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    seen: set[str] = set()
    for evidence in list(belief.get("evidence", []) or []) + list(belief.get("primarySources", []) or []):
        if not isinstance(evidence, dict):
            continue
        url = evidence.get("url") or evidence.get("sourceUrl")
        if not url or url in seen:
            continue
        seen.add(url)
        if url in source_index:
            matched.append(source_index[url])
    return matched


def source_item(item: dict[str, Any], source_id: str, score: float) -> dict[str, Any]:
    text = item_text(item)
    source_type = str(item.get("sourceType") or source_id)
    return {
        "id": item.get("id") or stable_id(f"{source_type}:{text}"),
        "source_type": source_type,
        "title": item.get("title") or author_label(item),
        "text": text,
        "score": score,
        "raw": item,
        "evidence": [item],
    }


def item_text(item: dict[str, Any]) -> str:
    return " ".join(
        str(item.get(key, ""))
        for key in (
            "exactText",
            "text",
            "plainText",
            "markdown",
            "title",
            "summary",
            "description",
            "selectionText",
            "note",
            "pageText",
            "transcriptText",
        )
        if item.get(key)
    )


def item_urls(item: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for key in ("url", "sourceUrl", "canonicalUrl"):
        if item.get(key):
            urls.append(str(item[key]))
    metadata = item.get("metadata")
    if isinstance(metadata, dict):
        for key in ("canonicalUrl", "locationHref", "activeTabUrl"):
            if metadata.get(key):
                urls.append(str(metadata[key]))
    links = item.get("links")
    if isinstance(links, list):
        for link in links:
            if isinstance(link, dict):
                for key in ("expandedUrl", "originalUrl", "url"):
                    if link.get(key):
                        urls.append(str(link[key]))
    return urls


def author_label(item: dict[str, Any]) -> str | None:
    author = item.get("author")
    if isinstance(author, dict):
        return author.get("handle") or author.get("displayName")
    if author:
        return str(author)
    return None


def ingest_arco_theses(con: Any, context: dict[str, Any]) -> int:
    rows: list[list[Any]] = []
    for item in flatten_arco_items(context):
        symbols = symbols_from_text(item.get("text", ""))
        if not symbols and item["source_type"] == "arco_signal":
            symbols = symbols_from_text(json_dumps(item.get("raw", {})))
        for symbol in symbols:
            row_id = stable_id(f"{item['source_type']}:{item['id']}:{symbol}")
            rows.append(
                [
                    row_id,
                    symbol,
                    author_from_item(item, symbol),
                    timestamp_from_item(item),
                    item.get("title") or item.get("text", "")[:240],
                    json_dumps(claims_from_item(item, symbol)),
                    json_dumps({"score": item.get("score")}),
                    source_url_from_item(item, symbol),
                ]
            )
    if not should_replace_theses(context, rows):
        return 0
    con.execute("DELETE FROM birdclaw_theses")
    for row in rows:
        con.execute(
            """
            INSERT OR REPLACE INTO birdclaw_theses
            (id, symbol, author, created_at, thesis_summary, claims, engagement, source_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            row,
        )
    return len(rows)


def should_replace_theses(context: dict[str, Any], rows: list[list[Any]]) -> bool:
    if rows:
        return True
    status = context.get("source_status")
    if not isinstance(status, dict) or not status.get("raw_dir_exists"):
        return False
    manifest_snapshot_count = int(status.get("manifest_snapshot_count") or 0)
    source_snapshot_count = int(status.get("source_snapshot_count") or 0)
    if manifest_snapshot_count and source_snapshot_count < manifest_snapshot_count:
        return False
    return any(
        status.get(key)
        for key in (
            "signals_path",
            "beliefs_path",
            "brief_beliefs_path",
            "bookmarks_path",
            "web_captures_path",
            "manifest_path",
        )
    )


def stable_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def confidence_score(confidence: str) -> float:
    return {"high": 0.9, "medium": 0.65, "low": 0.35}.get(confidence.lower(), 0.5)


def claims_from_item(item: dict[str, Any], symbol: str | None = None) -> dict[str, Any]:
    evidence = item.get("evidence", [])
    if isinstance(evidence, dict) and symbol:
        symbol_sources = evidence_sources_for_symbol(item, symbol)
        symbol_refs = evidence_refs_for_symbol(item, symbol)
        if symbol_sources:
            evidence = {**evidence, "matched_source_items": symbol_sources}
        elif symbol_refs:
            evidence = {**evidence, "brief_evidence": symbol_refs, "primary_sources": symbol_refs}
        elif has_aggregate_brief_evidence(item):
            evidence = {
                "source_brief": evidence.get("source_brief"),
                "validation_warnings": evidence.get("validation_warnings", []),
                "unattributed_symbol": symbol,
            }
    return {"source_type": item["source_type"], "text": item.get("text"), "evidence": evidence}


def author_from_item(item: dict[str, Any], symbol: str | None = None) -> str | None:
    symbol_sources = evidence_sources_for_symbol(item, symbol)
    if symbol_sources:
        for source in symbol_sources:
            author = author_label(source)
            if author:
                return author
        return None
    if symbol and has_aggregate_brief_evidence(item):
        return None
    raw = item.get("raw") or {}
    author = raw.get("author")
    if isinstance(author, dict):
        return author.get("handle") or author.get("displayName")
    if author:
        return str(author)
    evidence = item.get("evidence") or []
    if isinstance(evidence, dict):
        brief_evidence = evidence.get("brief_evidence") or []
        if brief_evidence and isinstance(brief_evidence[0], dict):
            return brief_evidence[0].get("author")
        return None
    if evidence and isinstance(evidence[0], dict):
        return evidence[0].get("author")
    return None


def timestamp_from_item(item: dict[str, Any]) -> str:
    raw = item.get("raw") or {}
    for key in ("created_at", "createdAt", "date", "lastSeen", "updatedAt", "savedAt", "capturedAt", "bookmarkedAt"):
        if raw.get(key):
            return normalize_timestamp(str(raw[key]))
    return datetime.utcnow().isoformat()


def normalize_timestamp(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError):
        return datetime.utcnow().isoformat()


def source_url_from_item(item: dict[str, Any], symbol: str | None = None) -> str | None:
    for source in evidence_sources_for_symbol(item, symbol):
        url = first_url_from_item(source)
        if url:
            return url
    for ref in evidence_refs_for_symbol(item, symbol):
        url = first_url_from_item(ref)
        if url:
            return url
    if symbol and has_aggregate_brief_evidence(item):
        evidence = item.get("evidence") or {}
        if isinstance(evidence, dict) and evidence.get("source_brief"):
            return str(evidence["source_brief"])
        return None
    raw = item.get("raw") or {}
    raw_url = first_url_from_item(raw)
    if raw_url:
        return raw_url
    evidence = item.get("evidence") or []
    if isinstance(evidence, dict):
        for key in ("brief_evidence", "primary_sources"):
            values = evidence.get(key) or []
            if values and isinstance(values[0], dict):
                url = first_url_from_item(values[0])
                if url:
                    return url
        if evidence.get("source_brief"):
            return str(evidence["source_brief"])
    if evidence and isinstance(evidence[0], dict):
        return first_url_from_item(evidence[0])
    return None


def evidence_refs_for_symbol(item: dict[str, Any], symbol: str | None) -> list[dict[str, Any]]:
    if not symbol:
        return []
    evidence = item.get("evidence") or {}
    if not isinstance(evidence, dict):
        return []
    refs: list[dict[str, Any]] = []
    symbol_upper = symbol.upper()
    for key in ("brief_evidence", "primary_sources"):
        values = evidence.get(key) or []
        if not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, dict) and symbol_upper in {candidate.upper() for candidate in symbols_from_text(json_dumps(value))}:
                refs.append(value)
    return refs


def has_aggregate_brief_evidence(item: dict[str, Any]) -> bool:
    evidence = item.get("evidence") or {}
    if not isinstance(evidence, dict):
        return False
    evidence_count = 0
    for key in ("matched_source_items", "brief_evidence", "primary_sources"):
        values = evidence.get(key) or []
        if isinstance(values, list):
            evidence_count += len([value for value in values if isinstance(value, dict)])
    return item.get("source_type") == "arco_brief_belief" and evidence_count > 1


def evidence_sources_for_symbol(item: dict[str, Any], symbol: str | None) -> list[dict[str, Any]]:
    if not symbol:
        return []
    evidence = item.get("evidence") or {}
    if not isinstance(evidence, dict):
        return []
    matched_sources = evidence.get("matched_source_items") or []
    if not isinstance(matched_sources, list):
        return []
    symbol_upper = symbol.upper()
    return [
        source
        for source in matched_sources
        if isinstance(source, dict) and symbol_upper in {candidate.upper() for candidate in symbols_from_text(item_text(source))}
    ]


def first_url_from_item(raw: dict[str, Any]) -> str | None:
    for key in ("url", "sourceUrl", "canonicalUrl"):
        if raw.get(key):
            return str(raw[key])
    metadata = raw.get("metadata")
    if isinstance(metadata, dict):
        for key in ("canonicalUrl", "locationHref", "activeTabUrl"):
            if metadata.get(key):
                return str(metadata[key])
    links = raw.get("links")
    if isinstance(links, list) and links and isinstance(links[0], dict):
        return links[0].get("expandedUrl") or links[0].get("originalUrl")
    return None
