"""Compact PostgreSQL persistence for content, events, and disclosures."""

from __future__ import annotations

from datetime import UTC, date, datetime
import hashlib
from typing import Any, Sequence
from uuid import UUID

from psycopg.types.json import Jsonb

from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


class SourceFactRepository:
    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def store_content_items(
        self, run_id: UUID, source_id: str, rows: Sequence[dict[str, Any]], *, payload_id: int | None = None
    ) -> dict[str, int]:
        stored = 0
        linked = 0
        with self.runtime.transaction(JOB_PROFILE) as connection:
            for source in rows:
                source_key = str(source.get("source_key") or source.get("id") or "").strip()
                observed_at = _aware_datetime(source.get("observed_at")) or datetime.now(UTC)
                published_at = _aware_datetime(source.get("published_at") or source.get("published"))
                if not source_key:
                    continue
                title = str(source.get("title") or "").strip() or None
                summary = str(source.get("summary") or source.get("description") or "").strip() or None
                digest_value = "\n".join(filter(None, (title, summary, str(source.get("url") or ""))))
                content_hash = hashlib.sha256(digest_value.encode()).hexdigest() if digest_value else None
                item = connection.execute(
                    """
                    INSERT INTO raw.content_item (
                        source_id, ingest_run_id, payload_id, source_key, kind, title,
                        url, author, published_at, observed_at, summary, content_hash,
                        license_status, metadata
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (source_id, source_key) DO UPDATE
                    SET ingest_run_id = EXCLUDED.ingest_run_id,
                        payload_id = COALESCE(EXCLUDED.payload_id, raw.content_item.payload_id),
                        title = EXCLUDED.title, url = EXCLUDED.url, author = EXCLUDED.author,
                        published_at = EXCLUDED.published_at, observed_at = EXCLUDED.observed_at,
                        summary = EXCLUDED.summary, content_hash = EXCLUDED.content_hash,
                        license_status = EXCLUDED.license_status,
                        metadata = raw.content_item.metadata || EXCLUDED.metadata
                    RETURNING id
                    """,
                    [
                        source_id, run_id, payload_id, source_key, str(source.get("kind") or "article"),
                        title, source.get("url"), source.get("author"), published_at, observed_at,
                        summary, content_hash, str(source.get("license_status") or "provider_link_only"),
                        Jsonb(dict(source.get("metadata") or {})),
                    ],
                ).fetchone()
                stored += 1
                for raw_symbol in source.get("symbols") or source.get("tickers") or []:
                    symbol = str(raw_symbol).strip().upper().lstrip("$")
                    if not symbol:
                        continue
                    instrument = connection.execute(
                        """
                        INSERT INTO catalog.instrument (symbol, name, asset_class, category)
                        VALUES (%s, %s, 'equity', 'content_reference')
                        ON CONFLICT (symbol) DO UPDATE SET updated_at = now() RETURNING id
                        """,
                        [symbol, symbol],
                    ).fetchone()
                    result = connection.execute(
                        """
                        INSERT INTO raw.content_item_instrument (content_item_id, instrument_id, relevance)
                        VALUES (%s, %s, %s) ON CONFLICT DO NOTHING
                        """,
                        [item["id"], instrument["id"], _number(source.get("relevance"))],
                    )
                    linked += int(result.rowcount)
        return {"items": stored, "instrument_links": linked}

    def store_market_events(
        self, run_id: UUID, source_id: str, rows: Sequence[dict[str, Any]], *, payload_id: int | None = None
    ) -> int:
        stored = 0
        with self.runtime.transaction(JOB_PROFILE) as connection:
            for source in rows:
                source_key = str(source.get("source_key") or source.get("id") or "").strip()
                starts_at = _aware_datetime(source.get("starts_at") or source.get("start_at"))
                title = str(source.get("title") or source.get("event") or "").strip()
                if not source_key or starts_at is None or not title:
                    continue
                instrument_id = _optional_instrument(connection, source.get("symbol"), "event_reference")
                event = connection.execute(
                    """
                    INSERT INTO raw.market_event (
                        instrument_id, source_id, ingest_run_id, payload_id, source_key,
                        event_scope, event_kind, title, starts_at, ends_at, importance,
                        verification_status, source_url, details
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (source_id, source_key) DO UPDATE
                    SET ingest_run_id = EXCLUDED.ingest_run_id,
                        payload_id = COALESCE(EXCLUDED.payload_id, raw.market_event.payload_id),
                        instrument_id = EXCLUDED.instrument_id, event_scope = EXCLUDED.event_scope,
                        event_kind = EXCLUDED.event_kind, title = EXCLUDED.title,
                        starts_at = EXCLUDED.starts_at, ends_at = EXCLUDED.ends_at,
                        importance = EXCLUDED.importance,
                        verification_status = EXCLUDED.verification_status,
                        source_url = EXCLUDED.source_url, details = EXCLUDED.details
                    RETURNING id
                    """,
                    [
                        instrument_id, source_id, run_id, payload_id, source_key,
                        str(source.get("event_scope") or "macro"), str(source.get("event_kind") or "economic"),
                        title, starts_at, _aware_datetime(source.get("ends_at") or source.get("end_at")),
                        source.get("importance"), source.get("verification_status"), source.get("source_url"),
                        Jsonb(dict(source.get("details") or {})),
                    ],
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO app.catalyst (instrument_id, market_event_id, starts_at, title, expected_impact, notes)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (market_event_id) DO UPDATE
                    SET instrument_id = EXCLUDED.instrument_id, starts_at = EXCLUDED.starts_at,
                        title = EXCLUDED.title, expected_impact = EXCLUDED.expected_impact, notes = EXCLUDED.notes
                    """,
                    [instrument_id, event["id"], starts_at, title, source.get("expected_impact"), source.get("notes")],
                )
                stored += 1
        return stored

    def store_disclosures(
        self, run_id: UUID, source_id: str, rows: Sequence[dict[str, Any]], *, payload_id: int | None = None
    ) -> int:
        stored = 0
        with self.runtime.transaction(JOB_PROFILE) as connection:
            for source in rows:
                source_key = str(source.get("source_key") or source.get("id") or "").strip()
                if not source_key:
                    continue
                instrument_id = _optional_instrument(
                    connection, source.get("symbol") or source.get("ticker"), "disclosure_reference"
                )
                connection.execute(
                    """
                    INSERT INTO raw.disclosure (
                        instrument_id, source_id, ingest_run_id, payload_id, source_key,
                        source_type, trader_name, filer_name, event_date, filed_date,
                        action, amount_text, source_url, details
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (source_id, source_key) DO UPDATE
                    SET ingest_run_id = EXCLUDED.ingest_run_id,
                        payload_id = COALESCE(EXCLUDED.payload_id, raw.disclosure.payload_id),
                        instrument_id = EXCLUDED.instrument_id, source_type = EXCLUDED.source_type,
                        trader_name = EXCLUDED.trader_name, filer_name = EXCLUDED.filer_name,
                        event_date = EXCLUDED.event_date, filed_date = EXCLUDED.filed_date,
                        action = EXCLUDED.action, amount_text = EXCLUDED.amount_text,
                        source_url = EXCLUDED.source_url, details = EXCLUDED.details
                    """,
                    [
                        instrument_id, source_id, run_id, payload_id, source_key,
                        str(source.get("source_type") or "public_disclosure"), source.get("trader_name"),
                        source.get("filer_name"), _date(source.get("event_date") or source.get("transaction_date")),
                        _date(source.get("filed_date") or source.get("filing_date")),
                        source.get("action") or source.get("transaction_type"),
                        source.get("amount_text") or source.get("amount") or source.get("amount_range"),
                        source.get("source_url") or source.get("url"),
                        Jsonb(dict(source.get("details") or source.get("raw") or {})),
                    ],
                )
                stored += 1
        return stored


def _optional_instrument(connection: Any, raw_symbol: Any, category: str) -> int | None:
    symbol = str(raw_symbol or "").strip().upper()
    if not symbol:
        return None
    return int(connection.execute(
        """
        INSERT INTO catalog.instrument (symbol, name, asset_class, category)
        VALUES (%s, %s, 'equity', %s)
        ON CONFLICT (symbol) DO UPDATE SET updated_at = now() RETURNING id
        """,
        [symbol, symbol, category],
    ).fetchone()["id"])


def _number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _aware_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
