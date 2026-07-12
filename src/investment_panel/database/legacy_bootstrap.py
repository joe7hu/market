"""Storage-efficient bootstrap of normalized facts from the legacy DuckDB.

The legacy database contains both durable facts and very large derived histories.
This module keeps compact facts in full, folds long valuation series into JSON
observations, and retains only the newest option chain for each symbol.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time
import hashlib
import json
import math
import re
from typing import Any, Iterable

from psycopg.types.json import Jsonb

from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE
from investment_panel.database.source_facts import SourceFactRepository


def import_source_catalog(runtime: DatabaseRuntime, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    with runtime.transaction(JOB_PROFILE) as connection:
        for row in rows:
            source_id = str(row.get("source_id") or "").strip()
            if not source_id:
                continue
            connection.execute(
                """
                INSERT INTO ingest.source
                    (id, name, family, kind, origin, enabled, ingestion_mode,
                     source_url, capabilities, config, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        COALESCE(%s, now()), COALESCE(%s, now()))
                ON CONFLICT (id) DO UPDATE
                SET name = EXCLUDED.name, family = EXCLUDED.family,
                    kind = EXCLUDED.kind, origin = EXCLUDED.origin,
                    enabled = EXCLUDED.enabled,
                    ingestion_mode = EXCLUDED.ingestion_mode,
                    source_url = EXCLUDED.source_url,
                    capabilities = ingest.source.capabilities || EXCLUDED.capabilities,
                    config = ingest.source.config || EXCLUDED.config,
                    updated_at = EXCLUDED.updated_at
                """,
                [
                    source_id,
                    str(row.get("source_name") or source_id),
                    str(row.get("source_family") or "legacy"),
                    str(row.get("source_kind") or "content"),
                    row.get("origin") or "legacy-duckdb",
                    row.get("enabled") is not False,
                    row.get("ingestion_mode"),
                    row.get("source_url"),
                    Jsonb({"legacy_import": True}),
                    Jsonb(_json(row.get("config"), {})),
                    row.get("created_at"),
                    row.get("updated_at"),
                ],
            )
    return len(rows)


def import_source_signals(runtime: DatabaseRuntime, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    digest = hashlib.sha256(
        "|".join(sorted(str(row.get("id") or "") for row in rows)).encode()
    ).hexdigest()
    cutoff = max((_aware(row.get("observed_at")) for row in rows), default=None) or datetime.now(UTC)
    with runtime.transaction(JOB_PROFILE) as connection:
        run = connection.execute(
            "SELECT id FROM analysis.run WHERE run_type = 'legacy-source-signals' "
            "AND code_version = 'duckdb-bootstrap-v2' ORDER BY started_at LIMIT 1"
        ).fetchone()
        if run is None:
            run = connection.execute(
                """
                INSERT INTO analysis.run
                    (run_type, input_cutoff, code_version, input_hash,
                     started_at, finished_at, status, summary)
                VALUES ('legacy-source-signals', %s, 'duckdb-bootstrap-v2', %s,
                        now(), now(), 'succeeded', %s)
                RETURNING id
                """,
                [cutoff, digest, Jsonb({"source_rows": len(rows), "authority": "historical_evidence"})],
            ).fetchone()
        stored = 0
        for row in rows:
            symbol = str(row.get("symbol") or "").strip().upper().lstrip("$")
            source_key = str(row.get("source_item_id") or "").strip()
            source_id = str(row.get("source_id") or "legacy-content").strip()
            if not symbol or not source_key:
                continue
            item = connection.execute(
                "SELECT id FROM raw.content_item WHERE source_id = %s AND source_key = %s",
                [source_id, source_key],
            ).fetchone()
            if item is None:
                item = connection.execute(
                    "SELECT id FROM raw.content_item WHERE metadata->>'legacy_id' = %s "
                    "ORDER BY observed_at DESC LIMIT 1",
                    [source_key],
                ).fetchone()
            if item is None:
                continue
            instrument = connection.execute(
                """
                INSERT INTO catalog.instrument (symbol, name, asset_class, category)
                VALUES (%s, %s, 'equity', 'content_reference')
                ON CONFLICT (symbol) DO UPDATE SET updated_at = now() RETURNING id
                """,
                [symbol, symbol],
            ).fetchone()
            connection.execute(
                """
                INSERT INTO raw.content_item_instrument
                    (content_item_id, instrument_id, relevance)
                VALUES (%s, %s, %s) ON CONFLICT DO NOTHING
                """,
                [item["id"], instrument["id"], row.get("confidence")],
            )
            connection.execute(
                """
                INSERT INTO analysis.source_signal
                    (run_id, content_item_id, instrument_id, observed_at,
                     signal_type, sentiment, direction, confidence, thesis,
                     antithesis, invalidation, details)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id, content_item_id, instrument_id, signal_type)
                DO UPDATE SET observed_at = EXCLUDED.observed_at,
                    sentiment = EXCLUDED.sentiment, direction = EXCLUDED.direction,
                    confidence = EXCLUDED.confidence, thesis = EXCLUDED.thesis,
                    antithesis = EXCLUDED.antithesis,
                    invalidation = EXCLUDED.invalidation, details = EXCLUDED.details
                """,
                [
                    run["id"], item["id"], instrument["id"],
                    _aware(row.get("observed_at")) or cutoff,
                    str(row.get("signal_type") or "source_evidence"),
                    row.get("sentiment"), row.get("direction"), row.get("confidence"),
                    row.get("thesis"), row.get("antithesis"), row.get("invalidation"),
                    Jsonb(_jsonable({
                        "legacy_id": row.get("id"),
                        "catalysts": _json(row.get("catalysts"), []),
                        "risks": _json(row.get("risks"), []),
                        "evidence_refs": _json(row.get("evidence_refs"), []),
                        "needs_market_context": bool(row.get("needs_market_context")),
                        "raw": _json(row.get("raw"), {}),
                    })),
                ],
            )
            stored += 1
    return stored


def import_fundamental_facts(
    runtime: DatabaseRuntime,
    *,
    fundamentals: list[dict[str, Any]],
    estimates: list[dict[str, Any]],
    market_valuations: list[dict[str, Any]],
) -> int:
    specs = (
        ("legacy-equity-fundamentals", "sec_fundamentals", fundamentals),
        ("legacy-analyst-estimates", "analyst_estimates", estimates),
    )
    total = 0
    repository = IngestionRepository(runtime)
    for source_id, metric_set, rows in specs:
        if not rows:
            continue
        repository.register_source(
            source_id, name=source_id.replace("-", " ").title(), family="fundamentals",
            kind=metric_set, origin="legacy-duckdb", capabilities={"fundamentals": True},
        )
        run_id = repository.start_run(source_id, metric_set)
        total += _store_fundamental_rows(runtime, run_id, source_id, metric_set, rows)
        repository.finish_run(run_id, "succeeded", item_count=len(rows))
    total += _import_market_valuations(runtime, market_valuations)
    return total


def import_earnings_events(runtime: DatabaseRuntime, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    source_id = "legacy-earnings-events"
    repository = IngestionRepository(runtime)
    repository.register_source(
        source_id, name="Legacy earnings calendar", family="calendar", kind="earnings",
        origin="legacy-duckdb", capabilities={"events": True},
    )
    run_id = repository.start_run(source_id, "events")
    normalized = [
        {
            "source_key": f"{row.get('symbol')}:{row.get('event_date')}:{row.get('event_type')}",
            "symbol": row.get("symbol"), "event_scope": "symbol", "event_kind": "earnings",
            "title": f"{row.get('symbol')} earnings", "starts_at": row.get("event_date"),
            "importance": "high", "verification_status": "legacy_import",
            "expected_impact": "Earnings can change valuation, trend, and option pricing.",
            "details": _jsonable({"metrics": _json(row.get("metrics"), {}), "legacy_source": row.get("source")}),
        }
        for row in rows
    ]
    count = SourceFactRepository(runtime).store_market_events(run_id, source_id, normalized)
    repository.finish_run(run_id, "succeeded", item_count=count)
    return count


def import_latest_options(runtime: DatabaseRuntime, rows: list[dict[str, Any]]) -> dict[str, int]:
    if not rows:
        return {"snapshots": 0, "quotes": 0}
    grouped: dict[tuple[str, str, datetime], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        observed_at = _aware(row.get("observed_at"))
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol or observed_at is None:
            continue
        raw = _json(row.get("raw"), {})
        enriched = {
            **row,
            "underlying_symbol": symbol,
            "expiration": row.get("expiry"),
            "provider_symbol": row.get("contract_symbol"),
            "underlying_price": raw.get("underlying_price"),
            "volume": raw.get("volume"),
            "open_interest": raw.get("open_interest"),
            "last": raw.get("last"),
        }
        grouped[(str(row.get("source") or "unknown"), symbol, observed_at)].append(enriched)
    repository = IngestionRepository(runtime)
    runs: dict[str, Any] = {}
    snapshots = quotes = 0
    for (provider, symbol, observed_at), option_rows in grouped.items():
        source_id = f"legacy-options-{_slug(provider)}"
        if source_id not in runs:
            repository.register_source(
                source_id, name=f"Legacy {provider} latest options", family="market_data",
                kind="option_chain", origin="legacy-duckdb", capabilities={"option_quotes": True},
            )
            runs[source_id] = repository.start_run(source_id, "option_quotes")
        result = repository.store_option_snapshot(
            runs[source_id], source_id=source_id, observed_at=observed_at,
            market_session="unknown", universe=symbol, rows=option_rows, completeness=1.0,
        )
        snapshots += 1
        quotes += result["contract_count"]
    for source_id, run_id in runs.items():
        source_quotes = sum(
            len(value) for (provider, _symbol, _at), value in grouped.items()
            if source_id == f"legacy-options-{_slug(provider)}"
        )
        repository.finish_run(run_id, "succeeded", item_count=source_quotes)
    return {"snapshots": snapshots, "quotes": quotes}


def _store_fundamental_rows(
    runtime: DatabaseRuntime, run_id: Any, source_id: str, metric_set: str,
    rows: Iterable[dict[str, Any]],
) -> int:
    stored = 0
    with runtime.transaction(JOB_PROFILE) as connection:
        for row in rows:
            symbol = str(row.get("symbol") or "").strip().upper()
            as_of = row.get("period_end") or row.get("as_of")
            if not symbol or not as_of:
                continue
            instrument = connection.execute(
                "INSERT INTO catalog.instrument (symbol, name, asset_class, category) "
                "VALUES (%s, %s, 'equity', 'fundamentals') "
                "ON CONFLICT (symbol) DO UPDATE SET updated_at = now() RETURNING id",
                [symbol, symbol],
            ).fetchone()
            values = _json(row.get("metrics") or row.get("estimates"), {})
            values.update({
                "form_type": row.get("form_type"), "source_url": row.get("source_url"),
                "legacy_source": row.get("source"),
            })
            observed_at = datetime.combine(_date(as_of), time(20), tzinfo=UTC)
            connection.execute(
                """
                INSERT INTO raw.fundamental_observation
                    (instrument_id, source_id, ingest_run_id, metric_set, period_end,
                     filed_at, observed_at, values)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (instrument_id, source_id, metric_set, period_end, observed_at)
                DO UPDATE SET ingest_run_id = EXCLUDED.ingest_run_id,
                    filed_at = EXCLUDED.filed_at, values = EXCLUDED.values
                """,
                [instrument["id"], source_id, run_id, metric_set, _date(as_of),
                 _aware(row.get("filing_date")), observed_at, Jsonb(_jsonable(values))],
            )
            stored += 1
    return stored


def _import_market_valuations(runtime: DatabaseRuntime, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    source_id = "legacy-market-valuations"
    repository = IngestionRepository(runtime)
    repository.register_source(
        source_id, name="Legacy market valuation series", family="market_data",
        kind="market_valuation", origin="legacy-duckdb", capabilities={"market_valuation": True},
    )
    run_id = repository.start_run(source_id, "market_valuation")
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("metric") or "unknown")].append(row)
    prepared = []
    for metric, points in grouped.items():
        points.sort(key=lambda item: str(item.get("as_of") or ""))
        latest = points[-1]
        prepared.append({
            "symbol": "SPY", "period_end": latest.get("as_of"),
            "metrics": {
                "metric": metric, "label": latest.get("label") or metric.replace("_", " ").title(),
                "value": latest.get("value"), "latest_value": latest.get("value"),
                "suffix": latest.get("suffix") or "", "higher_is_better": bool(latest.get("higher_is_better")),
                "source_url": latest.get("source_url"),
                "history": [{"date": point.get("as_of"), "value": point.get("value")} for point in points],
            },
        })
    total = 0
    for row in prepared:
        metric = str((row["metrics"] or {}).get("metric"))
        total += _store_fundamental_rows(runtime, run_id, source_id, f"market_valuation:{metric}", [row])
    repository.finish_run(run_id, "succeeded", item_count=total)
    return total


def _json(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value) if value not in (None, "") else fallback
    except (TypeError, ValueError):
        return fallback


def _aware(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, date):
        return datetime.combine(value, time(0), tzinfo=UTC)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "unknown"


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value
