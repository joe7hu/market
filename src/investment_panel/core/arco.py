"""Arco snapshot ingestion for thesis-flow evidence."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from investment_panel.core.config import ArcoConfig
from investment_panel.core.db import json_dumps
from investment_panel.core.instruments import symbols_from_text


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
    signals = read_json(raw_dir / config.signals_path, {"topics": [], "subtopics": []})
    beliefs = read_json(raw_dir / config.beliefs_path, {"beliefs": []})
    bookmarks_path = latest_file(raw_dir, config.birdclaw_bookmarks_glob)
    manifest_path = latest_file(raw_dir, config.source_manifest_glob)
    bookmarks = read_json(bookmarks_path, {"canonicalItems": [], "items": []}) if bookmarks_path else {}
    manifest = read_json(manifest_path, {}) if manifest_path else {}
    return {
        "signals": signals,
        "beliefs": beliefs,
        "bookmarks": bookmarks,
        "manifest": manifest,
        "bookmarks_path": str(bookmarks_path) if bookmarks_path else None,
        "manifest_path": str(manifest_path) if manifest_path else None,
    }


def flatten_arco_items(context: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
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
    bookmark_items = context.get("bookmarks", {}).get("canonicalItems") or context.get("bookmarks", {}).get("items") or []
    for item in bookmark_items:
        text = " ".join(str(item.get(key, "")) for key in ("text", "plainText", "markdown", "title"))
        items.append(
            {
                "id": item.get("id") or stable_id(text),
                "source_type": "birdclaw_bookmark",
                "title": item.get("title") or item.get("author", {}).get("handle"),
                "text": text,
                "score": 0.45,
                "raw": item,
                "evidence": [item],
            }
        )
    return items


def ingest_arco_theses(con: Any, context: dict[str, Any]) -> int:
    count = 0
    for item in flatten_arco_items(context):
        symbols = symbols_from_text(item.get("text", ""))
        if not symbols and item["source_type"] == "arco_signal":
            symbols = symbols_from_text(json_dumps(item.get("raw", {})))
        for symbol in symbols:
            row_id = stable_id(f"{item['source_type']}:{item['id']}:{symbol}")
            con.execute(
                """
                INSERT OR REPLACE INTO birdclaw_theses
                (id, symbol, author, created_at, thesis_summary, claims, engagement, source_url)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    row_id,
                    symbol,
                    author_from_item(item),
                    timestamp_from_item(item),
                    item.get("title") or item.get("text", "")[:240],
                    json_dumps({"source_type": item["source_type"], "text": item.get("text"), "evidence": item.get("evidence", [])}),
                    json_dumps({"score": item.get("score")}),
                    source_url_from_item(item),
                ],
            )
            count += 1
    return count


def stable_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def confidence_score(confidence: str) -> float:
    return {"high": 0.9, "medium": 0.65, "low": 0.35}.get(confidence.lower(), 0.5)


def author_from_item(item: dict[str, Any]) -> str | None:
    raw = item.get("raw") or {}
    author = raw.get("author")
    if isinstance(author, dict):
        return author.get("handle") or author.get("displayName")
    evidence = item.get("evidence") or []
    if evidence and isinstance(evidence[0], dict):
        return evidence[0].get("author")
    return None


def timestamp_from_item(item: dict[str, Any]) -> str:
    raw = item.get("raw") or {}
    for key in ("created_at", "createdAt", "date", "lastSeen", "updatedAt"):
        if raw.get(key):
            return str(raw[key])
    return datetime.utcnow().isoformat()


def source_url_from_item(item: dict[str, Any]) -> str | None:
    raw = item.get("raw") or {}
    for key in ("url", "sourceUrl"):
        if raw.get(key):
            return str(raw[key])
    evidence = item.get("evidence") or []
    if evidence and isinstance(evidence[0], dict):
        return evidence[0].get("url") or evidence[0].get("sourceUrl")
    return None

