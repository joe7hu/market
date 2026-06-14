"""Shared helpers for live opencli ingestion: run recording + symbol extraction."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from investment_panel.core.db import query_rows
from investment_panel.core.source_ingestion.raw_sources.coerce import normalize_timestamp
from investment_panel.core.instruments import normalize_symbol, symbols_from_text
from investment_panel.core.source_ingestion.canonical import record_source_run
from investment_panel.core.source_ingestion.utils import stable_id


@dataclass
class LiveFetchResult:
    """Outcome of one live fetch (one source_run)."""

    source_id: str
    status: str = "ok"
    items: int = 0
    signals: int = 0
    skipped: int = 0
    detail: str = ""
    rate_limited: bool = False
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "status": self.status,
            "items": self.items,
            "signals": self.signals,
            "skipped": self.skipped,
            "rate_limited": self.rate_limited,
            "detail": self.detail,
            "error": self.error,
        }


# Upper-cased plain words that look like cashtag candidates but are common English
# noise; we only trust true $CASHTAGs plus DB-known symbols, so this is a light
# guard for the known-symbol path.
_STOPWORDS = {"A", "I", "THE", "AND", "OR", "FOR", "USA", "CEO", "AI", "GDP", "USD", "EUR"}


def known_symbols(con: Any) -> set[str]:
    """Set of symbols known to the DB universe (instruments + signals)."""

    symbols: set[str] = set()
    for row in query_rows(con, "SELECT symbol FROM instruments WHERE symbol IS NOT NULL"):
        normalized = normalize_symbol(str(row.get("symbol") or ""))
        if normalized:
            symbols.add(normalized)
    return symbols


def extract_symbols(text: str, known: set[str] | None = None) -> list[str]:
    """Deterministic ticker extraction: $CASHTAGs + DB-known bare mentions.

    Cashtags are always trusted. Bare uppercase tokens are only kept when they
    match a DB-known symbol, to avoid false positives on ordinary words.
    """

    found = set(symbols_from_text(text or ""))
    if known:
        for token in _bare_upper_tokens(text or ""):
            if token in known and token not in _STOPWORDS:
                found.add(token)
    return sorted(found)


def _bare_upper_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    current = []
    for ch in text:
        if ch.isalnum() or ch == ".":
            current.append(ch)
        else:
            tokens.add("".join(current))
            current = []
    tokens.add("".join(current))
    return {token.upper() for token in tokens if 1 <= len(token) <= 5 and token.isupper()}


def normalize_published(value: Any, *, fallback_iso: str) -> str:
    """Coerce a feed timestamp to ISO so DuckDB can store it; fall back to now."""

    if value in (None, ""):
        return fallback_iso
    normalized = normalize_timestamp(value)
    text = str(normalized)
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
        return text
    except ValueError:
        return fallback_iso


def record_live_run(con: Any, result: LiveFetchResult, *, capability: str, run_key: Any = None) -> None:
    """Persist a source_run row for a completed live fetch."""

    now = datetime.now(UTC)
    record_source_run(
        con,
        source_id=result.source_id,
        run_id=stable_id("live_run", result.source_id, capability, run_key or now.isoformat()),
        capability=capability,
        started_at=now,
        finished_at=now,
        status=result.status,
        item_count=result.items,
        ticker_count=result.signals,
        failure_detail=result.error or result.detail,
        raw=result.as_dict(),
    )


def existing_source_item_ids(con: Any, item_ids: list[str]) -> set[str]:
    """Return the subset of item_ids already present in source_items (dedupe)."""

    if not item_ids:
        return set()
    placeholders = ", ".join("?" for _ in item_ids)
    rows = query_rows(con, f"SELECT id FROM source_items WHERE id IN ({placeholders})", item_ids)
    return {str(row.get("id")) for row in rows}
