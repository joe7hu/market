"""PostgreSQL authority for durable portfolio, watchlist, and thesis state."""

from __future__ import annotations

from datetime import UTC, datetime
import re
from typing import Any

from psycopg.types.json import Jsonb

from investment_panel.core.config import AppConfig
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.instruments import canonical_symbol, instrument_identity, reconcile_instrument


DEFAULT_OWNED_THESIS = {
    "position_status": "owned",
    "core_thesis": "",
    "pillars": [],
    "risks": [],
    "invalidation": [],
    "catalysts": [],
    "conviction": "unknown",
}

THESIS_STALE_DAYS = 45
INVALIDATION_PRICE_RE = re.compile(
    r"(?:below|under|stop(?:\s+loss)?(?:\s+at)?|invalidat\w*(?:\s+at)?)\s*\$?\s*([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)


def portfolio_rows(config: AppConfig, *, connection: Any | None = None) -> list[dict[str, Any]]:
    if connection is None:
        runtime = runtime_for_config(config)
        with runtime.read() as owned_connection:
            return portfolio_rows(config, connection=owned_connection)
    rows = connection.execute(
        """
        WITH positions AS MATERIALIZED (
            SELECT i.id AS instrument_id, i.symbol, i.name, i.asset_class, i.sector,
                   i.industry, i.category, p.quantity, p.average_cost,
                   p.purchase_date, p.notes, p.updated_at,
                   latest_split.executed_at AS latest_split_at
            FROM app.portfolio_position p
            JOIN catalog.instrument i ON i.id = p.instrument_id
            LEFT JOIN LATERAL (
                SELECT max(transaction.executed_at) AS executed_at
                FROM app.portfolio_transaction transaction
                WHERE transaction.instrument_id = p.instrument_id
                  AND transaction.transaction_type = 'split'
                  AND transaction.reverses_transaction_id IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM app.portfolio_transaction reversal
                      WHERE reversal.reverses_transaction_id = transaction.id
                  )
            ) latest_split ON true
        ), current_prices AS MATERIALIZED (
            SELECT *
            FROM raw.current_price_at(
                now(),
                ARRAY(SELECT instrument_id FROM positions)::bigint[]
            )
        )
        SELECT p.symbol, p.name, p.asset_class, p.sector, p.industry, p.category,
               p.quantity, p.average_cost, p.purchase_date, p.notes, p.updated_at,
               q.price, q.change_pct, q.change_abs, q.observed_at AS quote_observed_at,
               q.available_at AS quote_available_at,
               q.source_id AS quote_source, q.valuation_status
        FROM positions p
        LEFT JOIN current_prices q ON q.instrument_id = p.instrument_id
          AND (
              p.purchase_date IS NULL
              OR (q.observed_at AT TIME ZONE 'America/New_York')::date >= p.purchase_date - 7
          )
          AND (p.latest_split_at IS NULL OR q.observed_at >= p.latest_split_at)
        ORDER BY p.symbol
        """
    ).fetchall()
    output: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        row["avg_cost"] = float(row["average_cost"]) if row.get("average_cost") is not None else None
        row["quantity"] = float(row["quantity"])
        price = float(row["price"]) if row.get("price") is not None else None
        row["price"] = price
        available_values = [
            value for value in (row.get("updated_at"), row.get("quote_available_at"))
            if isinstance(value, datetime)
        ]
        row["available_at"] = max(available_values) if available_values else None
        valuation_price = price if price is not None else row["avg_cost"]
        if valuation_price is not None and row["avg_cost"] is not None:
            row["valuation_price"] = valuation_price
            row["valuation_status"] = str(row.get("valuation_status") or "market_quote") if price is not None else "cost_basis_fallback"
            row["market_value"] = row["quantity"] * valuation_price
            row["unrealized_pnl"] = row["quantity"] * (valuation_price - row["avg_cost"])
            row["unrealized_pnl_pct"] = ((valuation_price - row["avg_cost"]) / row["avg_cost"]) * 100 if row["avg_cost"] else None
        output.append(_without_none(row))
    total = sum(float(row.get("market_value") or 0) for row in output)
    for row in output:
        if total and row.get("market_value") is not None:
            row["portfolio_weight"] = float(row["market_value"]) / total * 100
    return output


def save_watchlist_item(config: AppConfig, item: dict[str, Any]) -> dict[str, Any]:
    runtime = runtime_for_config(config)
    symbol = canonical_symbol(item.get("symbol"))
    name = str(item.get("name") or symbol).strip()
    asset_class = str(item.get("asset_class") or instrument_identity(symbol)["asset_class"]).lower()
    if asset_class not in {"equity", "etf", "crypto"}:
        raise ValueError("asset_class must be equity, etf, or crypto")
    notes = str(item.get("notes") or "").strip()
    with runtime.transaction() as connection:
        instrument_id = reconcile_instrument(
            connection, symbol, name=name, asset_class=asset_class, category="watchlist"
        )
        connection.execute(
            """
            INSERT INTO app.watchlist_item (instrument_id, watch_state, notes, created_at, updated_at)
            VALUES (%s, 'watched', %s, now(), now())
            ON CONFLICT (instrument_id) DO UPDATE
            SET watch_state = 'watched', notes = EXCLUDED.notes, updated_at = now()
            """,
            [instrument_id, notes],
        )
    return {"symbol": symbol, "name": name, "asset_class": asset_class, "watch_state": "watched", "notes": notes}


def delete_watchlist_item(config: AppConfig, symbol: str) -> dict[str, Any]:
    runtime = runtime_for_config(config)
    normalized = canonical_symbol(symbol)
    with runtime.transaction() as connection:
        row = connection.execute("SELECT id FROM catalog.instrument WHERE symbol = %s", [normalized]).fetchone()
        if row:
            connection.execute(
                "UPDATE app.watchlist_item SET watch_state = 'excluded', updated_at = now() WHERE instrument_id = %s",
                [row["id"]],
            )
    return {"symbol": normalized, "deleted": True}


def watchlist_rows(config: AppConfig, *, include_excluded: bool = False) -> list[dict[str, Any]]:
    runtime = runtime_for_config(config)
    state_filter = "" if include_excluded else "AND w.watch_state <> 'excluded'"
    with runtime.read() as connection:
        rows = connection.execute(
            f"""
            SELECT i.symbol, i.name, i.asset_class, w.watch_state, w.notes, w.created_at, w.updated_at
            FROM app.watchlist_item w
            JOIN catalog.instrument i ON i.id = w.instrument_id
            WHERE true {state_filter}
            ORDER BY i.symbol
            """
        ).fetchall()
    return [{**dict(row), "available_at": row.get("updated_at")} for row in rows]


def table_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"rows": rows, "count": len(rows)}


def save_thesis(config: AppConfig, symbol: str, fields: dict[str, Any]) -> dict[str, Any]:
    normalized = canonical_symbol(symbol)
    thesis_text = str(fields.get("thesis") or "").strip()
    if not thesis_text:
        raise ValueError("thesis is required")
    runtime = runtime_for_config(config)
    with runtime.transaction() as connection:
        instrument_id = reconcile_instrument(
            connection, normalized, name=normalized, category="thesis"
        )
        connection.execute("SELECT id FROM catalog.instrument WHERE id = %s FOR UPDATE", [instrument_id])
        current = connection.execute(
            "SELECT revision, thesis FROM app.thesis "
            "WHERE instrument_id = %s AND status = 'current' ORDER BY revision DESC LIMIT 1",
            [instrument_id],
        ).fetchone()
        thesis = dict(current["thesis"]) if current else {}
        thesis["core_thesis"] = thesis_text
        why = str(fields.get("why") or "").strip()
        invalidation = str(fields.get("invalidation") or "").strip()
        if why:
            thesis["why_owned_watched"] = why
        if invalidation:
            thesis["invalidation"] = invalidation
        invalidation_price = fields.get("invalidation_price")
        if invalidation_price not in (None, ""):
            try:
                thesis["invalidation_price"] = float(invalidation_price)
            except (TypeError, ValueError) as exc:
                raise ValueError("invalidation_price must be a number") from exc
        explicit_status = str(fields.get("status") or "").strip().lower()
        if explicit_status:
            thesis["status"] = explicit_status
        evidence_links = fields.get("evidence_links")
        if isinstance(evidence_links, list):
            cleaned = [str(link).strip() for link in evidence_links if str(link).strip()]
            if cleaned:
                thesis["evidence_links"] = cleaned
        schema_version = int(fields.get("schema_version") or 1)
        if schema_version == 2:
            direction = str(fields.get("direction") or "").strip().lower()
            horizon_date = str(fields.get("horizon_date") or "").strip()
            option_invalidation = str(fields.get("invalidation") or "").strip()
            try:
                max_loss = float(fields.get("max_loss"))
            except (TypeError, ValueError) as exc:
                raise ValueError("schema_version 2 requires positive max_loss") from exc
            if direction not in {"bullish", "bearish"} or not horizon_date or not option_invalidation or max_loss <= 0:
                raise ValueError("schema_version 2 requires direction, horizon_date, positive max_loss, and invalidation")
            thesis.update({
                "schema_version": 2, "direction": direction, "horizon_date": horizon_date,
                "max_loss": max_loss, "invalidation": option_invalidation,
            })
            catalyst = str(fields.get("catalyst") or "").strip()
            if catalyst:
                thesis["catalyst"] = catalyst
        else:
            thesis["schema_version"] = 1
        thesis["last_reviewed"] = datetime.now(UTC).isoformat()
        revision = int(current["revision"]) + 1 if current else 1
        connection.execute(
            "UPDATE app.thesis SET status = 'superseded', updated_at = now() "
            "WHERE instrument_id = %s AND status = 'current'",
            [instrument_id],
        )
        connection.execute(
            "INSERT INTO app.thesis (instrument_id, revision, status, thesis) VALUES (%s, %s, 'current', %s)",
            [instrument_id, revision, Jsonb(thesis)],
        )
    return {"symbol": normalized, "thesis": thesis, "revision": revision}


def mark_thesis_reviewed(config: AppConfig, symbol: str) -> dict[str, Any]:
    normalized = canonical_symbol(symbol)
    runtime = runtime_for_config(config)
    reviewed_at = datetime.now(UTC).isoformat()
    with runtime.transaction() as connection:
        row = connection.execute(
            "SELECT i.id, t.revision, t.thesis FROM catalog.instrument i "
            "LEFT JOIN app.thesis t ON t.instrument_id = i.id AND t.status = 'current' "
            "WHERE i.symbol = %s FOR UPDATE OF i",
            [normalized],
        ).fetchone()
        if row is None:
            instrument_id = reconcile_instrument(
                connection, normalized, name=normalized, category="thesis"
            )
            revision = 1
            thesis: dict[str, Any] = {}
        else:
            instrument_id = int(row["id"])
            revision = int(row["revision"] or 0) + 1
            thesis = dict(row["thesis"] or {})
        thesis["last_reviewed"] = reviewed_at
        connection.execute(
            "UPDATE app.thesis SET status = 'superseded', updated_at = now() "
            "WHERE instrument_id = %s AND status = 'current'",
            [instrument_id],
        )
        connection.execute(
            "INSERT INTO app.thesis (instrument_id, revision, status, thesis) VALUES (%s, %s, 'current', %s)",
            [instrument_id, revision, Jsonb(thesis)],
        )
    return {"symbol": normalized, "last_reviewed": reviewed_at, "revision": revision}


def thesis_rows(config: AppConfig) -> list[dict[str, Any]]:
    runtime = runtime_for_config(config)
    with runtime.read() as connection:
        rows = connection.execute(
            "SELECT i.symbol, t.revision, t.thesis AS thesis_json, t.updated_at "
            "FROM app.thesis t JOIN catalog.instrument i ON i.id = t.instrument_id "
            "WHERE t.status = 'current' ORDER BY t.updated_at DESC, i.symbol"
        ).fetchall()
    return [dict(row) for row in rows]


def thesis_monitor_rows(config: AppConfig) -> list[dict[str, Any]]:
    runtime = runtime_for_config(config)
    with runtime.read() as connection:
        rows = [dict(row) for row in connection.execute(
            """
            SELECT i.symbol, t.thesis, t.updated_at,
                   (p.instrument_id IS NOT NULL) AS owned,
                   (w.instrument_id IS NOT NULL AND w.watch_state <> 'excluded') AS watched,
                   q.price AS latest_price, q.observed_at AS latest_quote_at
            FROM catalog.instrument i
            LEFT JOIN app.thesis t ON t.instrument_id = i.id AND t.status = 'current'
            LEFT JOIN app.portfolio_position p ON p.instrument_id = i.id
            LEFT JOIN app.watchlist_item w ON w.instrument_id = i.id
            LEFT JOIN LATERAL (
                SELECT quote.price, quote.observed_at
                FROM raw.confirmed_quote quote
                JOIN ingest.source quote_source ON quote_source.id = quote.source_id
                WHERE quote.instrument_id = i.id
                  AND (
                      quote.observed_at <= now()
                      OR (
                          quote_source.kind IN ('daily_bars', 'daily_quote')
                          AND (quote.observed_at AT TIME ZONE 'UTC')::date
                              <= (now() AT TIME ZONE i.market_timezone)::date
                      )
                  )
                ORDER BY quote.observed_at DESC LIMIT 1
            ) q ON true
            WHERE p.instrument_id IS NOT NULL
               OR (w.instrument_id IS NOT NULL AND w.watch_state <> 'excluded')
               OR t.id IS NOT NULL
            ORDER BY i.symbol
            """
        ).fetchall()]
        evidence_by_symbol = _thesis_source_evidence(
            connection,
            [str(row["symbol"]) for row in rows],
        )
    output = [
        _thesis_monitor_row(row, evidence_by_symbol.get(str(row["symbol"]), []))
        for row in rows
    ]
    return sorted(output, key=lambda row: (row["needs_review"], row["owned"], row["symbol"]), reverse=True)


def _thesis_monitor_row(
    row: dict[str, Any],
    source_evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    symbol = str(row["symbol"])
    evidence_rows = source_evidence or []
    thesis = dict(row.get("thesis") or {})
    core_thesis = str(thesis.get("core_thesis") or thesis.get("thesis") or "").strip()
    why = str(thesis.get("why_owned_watched") or thesis.get("why") or "").strip()
    invalidation_value = thesis.get("invalidation")
    invalidation = "; ".join(map(str, invalidation_value)) if isinstance(invalidation_value, list) else str(invalidation_value or "").strip()
    reviewed_at = _parse_datetime(thesis.get("last_reviewed")) or _parse_datetime(row.get("updated_at"))
    missing = [name for name, value in (("thesis", core_thesis), ("why owned/watched", why), ("invalidation", invalidation)) if not value]
    age_days = (datetime.now(UTC).date() - reviewed_at.date()).days if reviewed_at else None
    stale_reason = f"missing {', '.join(missing)}" if missing else (f"last reviewed {age_days} days ago" if age_days is not None and age_days > THESIS_STALE_DAYS else "")
    stale = bool(stale_reason)
    invalidation_price = _float_or_none(thesis.get("invalidation_price"))
    if invalidation_price is None and invalidation:
        match = INVALIDATION_PRICE_RE.search(invalidation)
        invalidation_price = _float_or_none(match.group(1)) if match else None
    latest_price = _float_or_none(row.get("latest_price"))
    distance = round(abs(latest_price - invalidation_price) / latest_price * 100, 2) if latest_price and invalidation_price else None
    flags: list[str] = []
    if latest_price is not None and invalidation_price is not None:
        if latest_price <= invalidation_price:
            flags.append("invalidation_breached")
        elif distance is not None and distance <= 10:
            flags.append("invalidation_near")
    stored_evidence = [str(item) for item in thesis.get("evidence_links") or [] if item]
    source_links = [str(item.get("reference") or "") for item in evidence_rows if item.get("reference")]
    evidence_links = list(dict.fromkeys(stored_evidence + source_links))
    source_names = sorted({str(item.get("source_name") or item.get("source_id") or "") for item in evidence_rows if item.get("source_name") or item.get("source_id")})
    latest_source_evidence_at = max(
        (_parse_datetime(item.get("observed_at")) for item in evidence_rows),
        default=None,
    )
    evidence_newer_than_review = bool(
        latest_source_evidence_at
        and (reviewed_at is None or latest_source_evidence_at > reviewed_at)
    )
    if stale_reason:
        review_reason = stale_reason
    elif flags:
        review_reason = "invalidation requires review"
    elif evidence_newer_than_review:
        review_reason = f"{len(evidence_rows)} new source evidence items since last review"
    else:
        review_reason = "Auditable thesis is current."
    return _without_none({
        "symbol": symbol,
        "thesis": core_thesis or f"No structured thesis loaded for {symbol}; review before action.",
        "thesis_text": core_thesis or f"No structured thesis loaded for {symbol}; review before action.",
        "why_owned_watched": why or "Why-owned/watched rationale is missing.",
        "why": why or "Why-owned/watched rationale is missing.",
        "invalidation": invalidation or "No invalidation rule loaded.",
        "invalidation_text": invalidation or "No invalidation rule loaded.",
        "evidence_links": evidence_links,
        "source_evidence": evidence_rows,
        "source_names": source_names,
        "source_count": len(source_names),
        "source_evidence_count": len(evidence_rows),
        "latest_source_evidence_at": latest_source_evidence_at,
        "evidence_newer_than_review": evidence_newer_than_review,
        "last_reviewed": reviewed_at,
        "last_reviewed_age_days": age_days,
        "status": str(thesis.get("status") or thesis.get("position_status") or ("owned" if row["owned"] else "watched")),
        "owned": bool(row["owned"]),
        "watched": bool(row["watched"]),
        "source": "theses" if core_thesis else "source_evidence" if evidence_rows else "portfolio_watchlist",
        "updated_at": row.get("updated_at"),
        "stale_thesis": stale,
        "stale_reason": stale_reason,
        "contradiction_flags": flags,
        "needs_review": stale or bool(flags) or evidence_newer_than_review,
        "review_reason": review_reason,
        "latest_price": latest_price,
        "latest_quote_at": row.get("latest_quote_at"),
        "invalidation_price": invalidation_price,
        "invalidation_distance_pct": distance,
        "evidence_count": len(evidence_links),
        "raw_thesis": thesis,
        "structured_fields_missing": [name.replace(" owned/watched", "_owned_watched") for name in missing],
    })


def _thesis_source_evidence(
    connection: Any,
    symbols: list[str],
) -> dict[str, list[dict[str, Any]]]:
    if not symbols:
        return {}
    rows = connection.execute(
        """
        WITH evidence_rows AS (
            SELECT regexp_replace(upper(instrument.symbol), '[.]+$', '') AS symbol,
                   item.source_id,
                   CASE WHEN source.kind = 'news' THEN lower(source.name)
                        ELSE source.name END AS source_name,
                   CASE WHEN source.family IN ('social', 'private_graph') THEN 'thesis'
                        ELSE source.family END AS source_family,
                   item.kind AS source_type, item.title,
                   COALESCE(signal.thesis, item.summary, item.title) AS summary,
                   COALESCE(signal.sentiment, 'neutral') AS sentiment,
                   COALESCE(signal.observed_at, item.observed_at) AS observed_at,
                   COALESCE(item.url, 'source_item:' || item.id) AS reference,
                   row_number() OVER (
                       PARTITION BY regexp_replace(upper(instrument.symbol), '[.]+$', ''),
                                    CASE WHEN source.kind = 'news' THEN lower(source.name)
                                         ELSE source.id END
                       ORDER BY COALESCE(signal.observed_at, item.observed_at) DESC, item.id DESC
                   ) AS source_rank
            FROM raw.content_item_instrument link
            JOIN raw.content_item item ON item.id = link.content_item_id
            JOIN catalog.instrument instrument ON instrument.id = link.instrument_id
            JOIN ingest.source source ON source.id = item.source_id
            LEFT JOIN LATERAL (
                SELECT signal.thesis, signal.sentiment, signal.observed_at
                FROM analysis.source_signal signal
                WHERE signal.content_item_id = item.id AND signal.instrument_id = instrument.id
                ORDER BY signal.observed_at DESC LIMIT 1
            ) signal ON true
            WHERE regexp_replace(upper(instrument.symbol), '[.]+$', '') = ANY(%s)
              AND source.enabled
              AND item.kind NOT IN (
                  'analyst_estimate', 'crypto_fundamental', 'earnings_event',
                  'equity_fundamental', 'market_screener', 'trader_portfolio_model'
              )
              AND item.observed_at <= now()
              AND COALESCE(item.published_at, item.observed_at) <= now()
            UNION ALL
            SELECT regexp_replace(upper(instrument.symbol), '[.]+$', '') AS symbol,
                   disclosure.source_id,
                   CASE WHEN source.kind = 'news' THEN lower(source.name)
                        ELSE source.name END AS source_name,
                   'filing' AS source_family, disclosure.source_type,
                   concat_ws(' ', COALESCE(disclosure.trader_name, disclosure.filer_name),
                             disclosure.action, instrument.symbol) AS title,
                   concat_ws(' ', disclosure.action, disclosure.amount_text) AS summary,
                   CASE WHEN lower(COALESCE(disclosure.action, '')) ~ '(sell|sale|reduc)'
                        THEN 'bearish'
                        WHEN lower(COALESCE(disclosure.action, '')) ~ '(buy|purchase|add)'
                        THEN 'bullish' ELSE 'neutral' END AS sentiment,
                   COALESCE(disclosure.filed_date, disclosure.event_date)::timestamptz AS observed_at,
                   COALESCE(disclosure.source_url, 'disclosure:' || disclosure.id) AS reference,
                   row_number() OVER (
                       PARTITION BY regexp_replace(upper(instrument.symbol), '[.]+$', ''), disclosure.source_id
                       ORDER BY COALESCE(disclosure.filed_date, disclosure.event_date) DESC, disclosure.id DESC
                   ) AS source_rank
            FROM raw.disclosure disclosure
            JOIN catalog.instrument instrument ON instrument.id = disclosure.instrument_id
            JOIN ingest.source source ON source.id = disclosure.source_id
            WHERE regexp_replace(upper(instrument.symbol), '[.]+$', '') = ANY(%s)
              AND source.enabled
              AND COALESCE(disclosure.filed_date, disclosure.event_date) <= current_date
        ), balanced AS (
            SELECT evidence_rows.*,
                   row_number() OVER (
                       PARTITION BY symbol ORDER BY observed_at DESC, source_id, reference
                   ) AS symbol_rank
            FROM evidence_rows
            WHERE source_rank <= 2
        )
        SELECT symbol, source_id, source_name, source_family, source_type,
               title, summary, sentiment, observed_at, reference
        FROM balanced
        WHERE symbol_rank <= 12
        ORDER BY symbol, observed_at DESC
        """,
        [symbols, symbols],
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for raw_row in rows:
        item = dict(raw_row)
        grouped.setdefault(str(item["symbol"]), []).append(item)
    return grouped


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if value:
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _without_none(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if value is not None}
