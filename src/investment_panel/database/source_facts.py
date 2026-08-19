"""Compact PostgreSQL persistence for content, events, and disclosures."""

from __future__ import annotations

from datetime import UTC, date, datetime
import hashlib
from typing import Any, Sequence
from uuid import UUID

from psycopg.types.json import Jsonb

from investment_panel.database.instruments import canonical_symbol, reconcile_instrument
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


class SourceFactRepository:
    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def store_content_items(
        self, run_id: UUID, source_id: str, rows: Sequence[dict[str, Any]], *, payload_id: int | None = None
    ) -> dict[str, Any]:
        stored = 0
        linked = 0
        affected_symbols: set[str] = set()
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
                existing = connection.execute(
                    "SELECT id, content_hash FROM raw.content_item WHERE source_id = %s AND source_key = %s",
                    [source_id, source_key],
                ).fetchone()
                content_changed = existing is None or existing["content_hash"] != content_hash
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
                if content_changed and existing is not None:
                    affected_symbols.update(
                        str(row["symbol"])
                        for row in connection.execute(
                            "SELECT instrument.symbol FROM raw.content_item_instrument link "
                            "JOIN catalog.instrument instrument ON instrument.id = link.instrument_id "
                            "WHERE link.content_item_id = %s",
                            [item["id"]],
                        ).fetchall()
                    )
                for raw_symbol in source.get("symbols") or source.get("tickers") or []:
                    try:
                        symbol = canonical_symbol(raw_symbol)
                    except ValueError:
                        continue
                    instrument_id = reconcile_instrument(
                        connection, symbol, name=symbol, category="content_reference"
                    )
                    result = connection.execute(
                        """
                        INSERT INTO raw.content_item_instrument (content_item_id, instrument_id, relevance)
                        VALUES (%s, %s, %s) ON CONFLICT DO NOTHING
                        """,
                        [item["id"], instrument_id, _number(source.get("relevance"))],
                    )
                    linked += int(result.rowcount)
                    if content_changed or result.rowcount:
                        affected_symbols.add(symbol)
        return {
            "items": stored,
            "instrument_links": linked,
            "affected_symbols": sorted(affected_symbols),
        }

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
                    INSERT INTO raw.market_event_version (
                        market_event_id, instrument_id, source_id, ingest_run_id, payload_id,
                        source_key, event_scope, event_kind, title, starts_at, ends_at,
                        importance, verification_status, source_url, details
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (market_event_id, ingest_run_id) DO NOTHING
                    """,
                    [
                        event["id"], instrument_id, source_id, run_id, payload_id, source_key,
                        str(source.get("event_scope") or "macro"),
                        str(source.get("event_kind") or "economic"), title, starts_at,
                        _aware_datetime(source.get("ends_at") or source.get("end_at")),
                        source.get("importance"), source.get("verification_status"),
                        source.get("source_url"), Jsonb(dict(source.get("details") or {})),
                    ],
                )
                self._project_catalyst(
                    connection,
                    event_id=int(event["id"]),
                    instrument_id=instrument_id,
                    source_id=source_id,
                    source=source,
                    starts_at=starts_at,
                    title=title,
                )
                stored += 1
        return stored

    def _project_catalyst(
        self,
        connection: Any,
        *,
        event_id: int,
        instrument_id: int | None,
        source_id: str,
        source: dict[str, Any],
        starts_at: datetime,
        title: str,
    ) -> None:
        """Append a canonical catalyst version without letting an estimate win.

        ``raw.market_event`` keeps each provider's current assertion.  The
        read-model needs a distinct owner because a low-confidence calendar
        update must not overwrite a company IR or SEC confirmation.  A source
        may supply ``event_key`` to identify a fiscal event exactly; the
        deterministic fallback keeps independently sourced earnings for the
        same quarter in one canonical version chain.
        """

        event_key = _catalyst_event_key(source, starts_at)
        source_priority, confidence = _catalyst_authority(source_id, source)
        expected_impact = source.get("expected_impact")
        notes = source.get("notes")
        current = connection.execute(
            """
            SELECT id, market_event_id, starts_at, title, expected_impact, notes,
                   source_id, source_priority, confidence, version
            FROM app.catalyst
            WHERE event_key = %s AND status = 'current'
            FOR UPDATE
            """,
            [event_key],
        ).fetchone()
        candidate = {
            "market_event_id": event_id,
            "instrument_id": instrument_id,
            "starts_at": starts_at,
            "title": title,
            "expected_impact": expected_impact,
            "notes": notes,
            "source_id": source_id,
            "source_priority": source_priority,
            "confidence": confidence,
        }
        if current is not None:
            current_values = dict(current)
            comparable = (
                "market_event_id", "instrument_id", "starts_at", "title",
                "expected_impact", "notes", "source_id", "source_priority", "confidence",
            )
            if all(current_values.get(key) == candidate[key] for key in comparable):
                return
            # An estimate is still retained in raw.market_event, but cannot
            # displace the higher-authority canonical catalyst.
            if int(current_values["source_priority"] or 0) > source_priority:
                return
            connection.execute(
                "UPDATE app.catalyst SET status = 'superseded', superseded_at = now() WHERE id = %s",
                [current_values["id"]],
            )
            version = int(current_values["version"] or 1) + 1
            supersedes_id = current_values["id"]
        else:
            version = 1
            supersedes_id = None
        connection.execute(
            """
            INSERT INTO app.catalyst
                (instrument_id, market_event_id, event_key, version, status,
                 supersedes_id, starts_at, title, expected_impact, notes,
                 source_id, source_priority, confidence)
            VALUES (%(instrument_id)s, %(market_event_id)s, %(event_key)s, %(version)s, 'current',
                    %(supersedes_id)s, %(starts_at)s, %(title)s, %(expected_impact)s, %(notes)s,
                    %(source_id)s, %(source_priority)s, %(confidence)s)
            """,
            {**candidate, "event_key": event_key, "version": version, "supersedes_id": supersedes_id},
        )

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
    if raw_symbol in (None, ""):
        return None
    try:
        return reconcile_instrument(connection, raw_symbol, category=category)
    except ValueError:
        return None


def _catalyst_event_key(source: dict[str, Any], starts_at: datetime) -> str:
    explicit = str(source.get("event_key") or source.get("canonical_event_key") or "").strip()
    if explicit:
        return explicit
    event_kind = str(source.get("event_kind") or "event").strip().lower()
    raw_symbol = source.get("symbol") or source.get("ticker")
    try:
        symbol = canonical_symbol(raw_symbol) if raw_symbol else ""
    except ValueError:
        symbol = ""
    if symbol and event_kind == "earnings":
        period = str(source.get("fiscal_period") or source.get("event_period") or "").strip()
        if not period:
            period = f"{starts_at.year}-Q{((starts_at.month - 1) // 3) + 1}"
        return f"{symbol}:earnings:{period}"
    source_key = str(source.get("source_key") or source.get("id") or "").strip()
    return f"{symbol or 'market'}:{event_kind}:{source_key}"


def _catalyst_authority(source_id: str, source: dict[str, Any]) -> tuple[int, float]:
    """Return deterministic canonical authority for a source assertion."""

    details = dict(source.get("details") or {})
    tier = str(source.get("source_tier") or details.get("source_tier") or "").strip().lower()
    verified = str(source.get("verification_status") or "").strip().lower()
    normalized_source = source_id.strip().lower()
    if tier in {"company_ir", "issuer_ir", "company"} or "investor-relations" in normalized_source or normalized_source.endswith("-ir"):
        return 400, 1.0 if verified == "confirmed" else 0.95
    if tier in {"sec", "filing"} or normalized_source in {"sec", "sec-edgar"}:
        return 350, 0.98 if verified == "confirmed" else 0.93
    if tier in {"verified_wire", "wire"} or verified in {"verified", "confirmed"}:
        return 300, 0.95 if verified == "confirmed" else 0.90
    if tier in {"exchange", "official_calendar"} or normalized_source == "official-event-calendar":
        return 250, 0.90 if verified == "confirmed" else 0.80
    if tier in {"estimate", "aggregator"} or normalized_source in {"yfinance", "yahoo-finance"}:
        return 100, 0.45
    return 150, 0.65 if verified in {"verified", "confirmed"} else 0.50


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
