"""PostgreSQL persistence for durable user-owned Market state."""

from __future__ import annotations

from datetime import UTC, datetime
import re
from typing import Any

from psycopg.types.json import Jsonb

from investment_panel.database.authority import runtime_for_config


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


def save_position(config: dict[str, Any], position: dict[str, Any]) -> dict[str, Any]:
    runtime = runtime_for_config(config)
    symbol = _symbol(position.get("symbol"))
    quantity = float(position["quantity"])
    average_cost = float(position["avg_cost"])
    purchase_date = position.get("purchase_date")
    notes = str(position.get("notes") or "").strip()
    with runtime.transaction() as connection:
        instrument_id = _upsert_instrument(
            connection, symbol, symbol, _infer_asset_class(symbol), replace_asset_class=False
        )
        connection.execute(
            """
            INSERT INTO app.portfolio_position
                (instrument_id, quantity, average_cost, purchase_date, notes, updated_at)
            VALUES (%s, %s, %s, %s, %s, now())
            ON CONFLICT (instrument_id) DO UPDATE
            SET quantity = EXCLUDED.quantity,
                average_cost = EXCLUDED.average_cost,
                purchase_date = EXCLUDED.purchase_date,
                notes = EXCLUDED.notes,
                updated_at = now()
            """,
            [instrument_id, quantity, average_cost, purchase_date, notes],
        )
        connection.execute(
            """
            INSERT INTO app.thesis (instrument_id, revision, status, thesis)
            SELECT %s, 1, 'current', %s
            WHERE NOT EXISTS (
                SELECT 1 FROM app.thesis WHERE instrument_id = %s AND status = 'current'
            )
            """,
            [instrument_id, Jsonb(DEFAULT_OWNED_THESIS), instrument_id],
        )
    return {
        "symbol": symbol,
        "quantity": quantity,
        "avg_cost": average_cost,
        "purchase_date": purchase_date,
        "notes": notes,
    }


def delete_position(config: dict[str, Any], symbol: str) -> dict[str, Any]:
    runtime = runtime_for_config(config)
    normalized = _symbol(symbol)
    with runtime.transaction() as connection:
        row = connection.execute("SELECT id FROM catalog.instrument WHERE symbol = %s", [normalized]).fetchone()
        if row:
            connection.execute("DELETE FROM app.portfolio_position WHERE instrument_id = %s", [row["id"]])
            connection.execute(
                "DELETE FROM app.thesis WHERE instrument_id = %s AND revision = 1 AND thesis = %s",
                [row["id"], Jsonb(DEFAULT_OWNED_THESIS)],
            )
    return {"symbol": normalized, "deleted": True}


def portfolio_rows(config: dict[str, Any]) -> list[dict[str, Any]]:
    runtime = runtime_for_config(config)
    with runtime.read() as connection:
        rows = connection.execute(
            """
            SELECT i.symbol, i.name, i.asset_class, i.category,
                   p.quantity, p.average_cost, p.purchase_date, p.notes, p.updated_at,
                   q.price, q.change_pct, q.change_abs, q.source_id AS quote_source
            FROM app.portfolio_position p
            JOIN catalog.instrument i ON i.id = p.instrument_id
            LEFT JOIN LATERAL (
                SELECT quote.price, quote.change_pct, quote.change_abs, quote.source_id
                FROM raw.quote quote
                WHERE quote.instrument_id = p.instrument_id
                ORDER BY quote.observed_at DESC
                LIMIT 1
            ) q ON true
            ORDER BY i.symbol
            """
        ).fetchall()
    output: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        row["avg_cost"] = float(row["average_cost"]) if row.get("average_cost") is not None else None
        row["quantity"] = float(row["quantity"])
        price = float(row["price"]) if row.get("price") is not None else None
        row["price"] = price
        if price is not None and row["avg_cost"] is not None:
            row["market_value"] = row["quantity"] * price
            row["unrealized_pnl"] = row["quantity"] * (price - row["avg_cost"])
            row["unrealized_pnl_pct"] = ((price - row["avg_cost"]) / row["avg_cost"]) * 100 if row["avg_cost"] else None
        output.append(_without_none(row))
    total = sum(float(row.get("market_value") or 0) for row in output)
    for row in output:
        if total and row.get("market_value") is not None:
            row["portfolio_weight"] = float(row["market_value"]) / total * 100
    return output


def save_watchlist_item(config: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    runtime = runtime_for_config(config)
    symbol = _symbol(item.get("symbol"))
    name = str(item.get("name") or symbol).strip()
    asset_class = str(item.get("asset_class") or _infer_asset_class(symbol)).lower()
    if asset_class not in {"equity", "etf", "crypto"}:
        raise ValueError("asset_class must be equity, etf, or crypto")
    notes = str(item.get("notes") or "").strip()
    with runtime.transaction() as connection:
        instrument_id = _upsert_instrument(connection, symbol, name, asset_class)
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


def delete_watchlist_item(config: dict[str, Any], symbol: str) -> dict[str, Any]:
    runtime = runtime_for_config(config)
    normalized = _symbol(symbol)
    with runtime.transaction() as connection:
        row = connection.execute("SELECT id FROM catalog.instrument WHERE symbol = %s", [normalized]).fetchone()
        if row:
            connection.execute(
                "UPDATE app.watchlist_item SET watch_state = 'excluded', updated_at = now() WHERE instrument_id = %s",
                [row["id"]],
            )
    return {"symbol": normalized, "deleted": True}


def watchlist_rows(config: dict[str, Any], *, include_excluded: bool = False) -> list[dict[str, Any]]:
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
    return [dict(row) for row in rows]


def table_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"rows": rows, "count": len(rows)}


def save_thesis(config: dict[str, Any], symbol: str, fields: dict[str, Any]) -> dict[str, Any]:
    normalized = _symbol(symbol)
    thesis_text = str(fields.get("thesis") or "").strip()
    if not thesis_text:
        raise ValueError("thesis is required")
    runtime = runtime_for_config(config)
    with runtime.transaction() as connection:
        instrument_id = _upsert_instrument(
            connection, normalized, normalized, _infer_asset_class(normalized), replace_asset_class=False
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


def mark_thesis_reviewed(config: dict[str, Any], symbol: str) -> dict[str, Any]:
    normalized = _symbol(symbol)
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
            instrument_id = _upsert_instrument(
                connection, normalized, normalized, _infer_asset_class(normalized), replace_asset_class=False
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


def thesis_rows(config: dict[str, Any]) -> list[dict[str, Any]]:
    runtime = runtime_for_config(config)
    with runtime.read() as connection:
        rows = connection.execute(
            "SELECT i.symbol, t.revision, t.thesis AS thesis_json, t.updated_at "
            "FROM app.thesis t JOIN catalog.instrument i ON i.id = t.instrument_id "
            "WHERE t.status = 'current' ORDER BY t.updated_at DESC, i.symbol"
        ).fetchall()
    return [dict(row) for row in rows]


def thesis_monitor_rows(config: dict[str, Any]) -> list[dict[str, Any]]:
    runtime = runtime_for_config(config)
    with runtime.read() as connection:
        rows = connection.execute(
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
                SELECT price, observed_at FROM raw.quote
                WHERE instrument_id = i.id ORDER BY observed_at DESC LIMIT 1
            ) q ON true
            WHERE p.instrument_id IS NOT NULL
               OR (w.instrument_id IS NOT NULL AND w.watch_state <> 'excluded')
               OR t.id IS NOT NULL
            ORDER BY i.symbol
            """
        ).fetchall()
    output = [_thesis_monitor_row(dict(row)) for row in rows]
    return sorted(output, key=lambda row: (row["needs_review"], row["owned"], row["symbol"]), reverse=True)


def _thesis_monitor_row(row: dict[str, Any]) -> dict[str, Any]:
    symbol = str(row["symbol"])
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
    return _without_none({
        "symbol": symbol,
        "thesis": core_thesis or f"No structured thesis loaded for {symbol}; review before action.",
        "thesis_text": core_thesis or f"No structured thesis loaded for {symbol}; review before action.",
        "why_owned_watched": why or "Why-owned/watched rationale is missing.",
        "why": why or "Why-owned/watched rationale is missing.",
        "invalidation": invalidation or "No invalidation rule loaded.",
        "invalidation_text": invalidation or "No invalidation rule loaded.",
        "evidence_links": list(thesis.get("evidence_links") or []),
        "last_reviewed": reviewed_at,
        "last_reviewed_age_days": age_days,
        "status": str(thesis.get("status") or thesis.get("position_status") or ("owned" if row["owned"] else "watched")),
        "owned": bool(row["owned"]),
        "watched": bool(row["watched"]),
        "source": "theses" if core_thesis else "portfolio_watchlist",
        "updated_at": row.get("updated_at"),
        "stale_thesis": stale,
        "stale_reason": stale_reason,
        "contradiction_flags": flags,
        "needs_review": stale or bool(flags),
        "review_reason": stale_reason or ("invalidation requires review" if flags else "Auditable thesis is current."),
        "latest_price": latest_price,
        "latest_quote_at": row.get("latest_quote_at"),
        "invalidation_price": invalidation_price,
        "invalidation_distance_pct": distance,
        "evidence_count": len(thesis.get("evidence_links") or []),
        "raw_thesis": thesis,
        "structured_fields_missing": [name.replace(" owned/watched", "_owned_watched") for name in missing],
    })


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


def _upsert_instrument(
    connection: Any,
    symbol: str,
    name: str,
    asset_class: str,
    *,
    replace_asset_class: bool = True,
) -> int:
    row = connection.execute(
        """
        INSERT INTO catalog.instrument (symbol, name, asset_class, category)
        VALUES (%s, %s, %s, 'watchlist')
        ON CONFLICT (symbol) DO UPDATE
        SET name = CASE WHEN EXCLUDED.name = EXCLUDED.symbol THEN catalog.instrument.name ELSE EXCLUDED.name END,
            asset_class = CASE WHEN %s THEN EXCLUDED.asset_class ELSE catalog.instrument.asset_class END,
            updated_at = now()
        RETURNING id
        """,
        [symbol, name, asset_class, replace_asset_class],
    ).fetchone()
    return int(row["id"])


def _symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    if not symbol or len(symbol) > 15 or not all(character.isalnum() or character in ".-" for character in symbol):
        raise ValueError("symbol must be a valid ticker")
    return symbol


def _infer_asset_class(symbol: str) -> str:
    return "crypto" if symbol.endswith("-USD") else "equity"


def _without_none(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if value is not None}
