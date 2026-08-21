"""PostgreSQL-owned thesis revisions, reviews, and monitor rows."""

from __future__ import annotations

from datetime import UTC, datetime
import re
from typing import Any

from psycopg.types.json import Jsonb

from investment_panel.core.config import AppConfig
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.instruments import canonical_symbol, reconcile_instrument
from investment_panel.database.thesis_evidence import assessments_by_revision, thesis_source_evidence
from investment_panel.database.thesis_history import with_revision_diffs
from investment_panel.database.thesis_monitor_universe import monitored_thesis_rows


THESIS_STALE_DAYS = 45
INVALIDATION_NEAR_PCT = 10.0
INVALIDATION_PRICE_RE = re.compile(
    r"(?:below|under|stop(?:\s+loss)?(?:\s+at)?|invalidat\w*(?:\s+at)?)\s*\$?\s*([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)
REVIEW_OUTCOMES = {"unchanged", "updated", "invalidated", "closed"}


def save_thesis(config: AppConfig, symbol: str, fields: dict[str, Any]) -> dict[str, Any]:
    normalized = canonical_symbol(symbol)
    runtime = runtime_for_config(config)
    now = datetime.now(UTC)
    author_kind = str(fields.get("author_kind") or "human").lower()
    if author_kind not in {"human", "ai", "legacy"}:
        raise ValueError("author_kind must be human, ai, or legacy")
    with runtime.transaction() as connection:
        instrument_id = reconcile_instrument(connection, normalized, name=normalized, category="thesis")
        current = connection.execute(
            """
            SELECT id, revision, thesis
            FROM app.thesis
            WHERE instrument_id = %s AND status = 'current'
            ORDER BY revision DESC LIMIT 1
            FOR UPDATE
            """,
            [instrument_id],
        ).fetchone()
        previous = dict(current["thesis"]) if current else {}
        thesis = normalize_thesis_v3(fields, previous=previous, symbol=normalized)
        revision = int(current["revision"]) + 1 if current else 1
        superseded_id = int(current["id"]) if current else None
        change_rationale = str(fields.get("change_rationale") or fields.get("notes") or "").strip()
        connection.execute(
            "UPDATE app.thesis SET status = 'superseded', updated_at = now() "
            "WHERE instrument_id = %s AND status = 'current'",
            [instrument_id],
        )
        inserted = connection.execute(
            """
            INSERT INTO app.thesis (
                instrument_id, revision, status, thesis, schema_version, author_kind,
                automation_run_id, superseded_revision_id, change_rationale,
                last_assessed_at, last_human_reviewed_at
            )
            VALUES (%s, %s, 'current', %s, 3, %s, %s, %s, %s, %s, %s)
            RETURNING id, created_at, updated_at
            """,
            [
                instrument_id,
                revision,
                Jsonb(thesis),
                author_kind,
                fields.get("automation_run_id"),
                superseded_id,
                change_rationale or None,
                now if author_kind == "ai" else None,
                now if author_kind == "human" else _parse_datetime(thesis.get("last_reviewed")),
            ],
        ).fetchone()
        if author_kind == "human":
            connection.execute(
                """
                INSERT INTO app.thesis_review_event (
                    instrument_id, thesis_revision_id, outcome, notes, reviewed_evidence_cutoff, reviewed_by
                )
                VALUES (%s, %s, 'updated', %s, %s, 'joe')
                """,
                [instrument_id, inserted["id"], change_rationale or None, fields.get("reviewed_evidence_cutoff")],
            )
    return {
        "symbol": normalized,
        "thesis": thesis,
        "revision": revision,
        "revision_id": inserted["id"],
        "created_at": inserted["created_at"],
        "updated_at": inserted["updated_at"],
    }


def normalize_thesis_v3(fields: dict[str, Any], *, previous: dict[str, Any] | None = None, symbol: str = "") -> dict[str, Any]:
    previous = previous or {}
    now = datetime.now(UTC).isoformat()
    core = _first_text(fields, "core_thesis", "thesis") or _first_text(previous, "core_thesis", "thesis")
    if not core:
        raise ValueError("thesis is required")
    why = _first_text(fields, "why_owned_watched", "why", "why_owned", "why_watched") or _first_text(
        previous, "why_owned_watched", "why", "why_owned", "why_watched"
    )
    direction = (_first_text(fields, "direction") or _first_text(previous, "direction") or "long").lower()
    conviction = (_first_text(fields, "conviction") or _first_text(previous, "conviction") or "unknown").lower()
    confidence = (_first_text(fields, "confidence") or _first_text(previous, "confidence") or "low").lower()
    automation_policy = (_first_text(fields, "automation_policy") or _first_text(previous, "automation_policy") or "auto").lower()
    lifecycle = (_first_text(fields, "lifecycle_status", "status") or _first_text(previous, "lifecycle_status", "status") or "active").lower()
    return _without_none(
        {
            "schema_version": 3,
            "core_thesis": core,
            "why_owned_watched": why,
            "direction": direction,
            "timeframe": _first_text(fields, "timeframe") or _first_text(previous, "timeframe"),
            "horizon_date": _first_text(fields, "horizon_date") or _first_text(previous, "horizon_date"),
            "conviction": conviction,
            "confidence": confidence,
            "pillars": _list_value(fields.get("pillars"), previous.get("pillars")),
            "scenarios": _scenario_value(fields.get("scenarios"), previous.get("scenarios")),
            "catalysts": _list_value(fields.get("catalysts"), previous.get("catalysts")),
            "invalidation_rules": _invalidation_rules(fields, previous, direction),
            "review_cadence_days": _int_value(fields.get("review_cadence_days"), _int_value(previous.get("review_cadence_days"), THESIS_STALE_DAYS)),
            "next_review_date": _first_text(fields, "next_review_date") or _first_text(previous, "next_review_date"),
            "lifecycle_status": lifecycle,
            "evidence_coverage_status": (
                _first_text(fields, "evidence_coverage_status")
                or _first_text(previous, "evidence_coverage_status")
                or "unknown"
            ),
            "automation_policy": automation_policy,
            "provenance": _dict_value(fields.get("provenance"), previous.get("provenance")),
            "evidence_links": _list_value(fields.get("evidence_links"), previous.get("evidence_links")),
            "last_reviewed": now if str(fields.get("author_kind") or "human") == "human" else previous.get("last_reviewed"),
            "legacy": previous.get("legacy") or (previous if previous.get("schema_version") != 3 else None),
            "symbol": symbol or previous.get("symbol"),
        }
    )


def record_thesis_review(config: AppConfig, symbol: str, fields: dict[str, Any]) -> dict[str, Any]:
    normalized = canonical_symbol(symbol)
    outcome = str(fields.get("outcome") or "unchanged").lower()
    if outcome not in REVIEW_OUTCOMES:
        raise ValueError("review outcome must be unchanged, updated, invalidated, or closed")
    runtime = runtime_for_config(config)
    with runtime.transaction() as connection:
        row = connection.execute(
            """
            SELECT i.id AS instrument_id, thesis.id AS thesis_revision_id, thesis.revision, thesis.thesis
            FROM catalog.instrument i
            LEFT JOIN app.thesis thesis ON thesis.instrument_id = i.id AND thesis.status = 'current'
            WHERE i.symbol = %s
            FOR UPDATE OF i
            """,
            [normalized],
        ).fetchone()
        if row is None or row["thesis_revision_id"] is None:
            raise ValueError("cannot review a missing thesis")
        thesis = dict(row["thesis"] or {})
        if not str(thesis.get("core_thesis") or thesis.get("thesis") or "").strip():
            raise ValueError("cannot record an empty-thesis review")
        event = connection.execute(
            """
            INSERT INTO app.thesis_review_event (
                instrument_id, thesis_revision_id, outcome, notes, reviewed_evidence_cutoff, reviewed_by
            )
            VALUES (%s, %s, %s, %s, %s, 'joe')
            RETURNING id, created_at
            """,
            [
                row["instrument_id"],
                row["thesis_revision_id"],
                outcome,
                _first_text(fields, "notes"),
                fields.get("reviewed_evidence_cutoff"),
            ],
        ).fetchone()
        if outcome in {"invalidated", "closed"}:
            thesis["lifecycle_status"] = outcome
            connection.execute(
                "UPDATE app.thesis SET thesis = %s, updated_at = now(), last_human_reviewed_at = now() WHERE id = %s",
                [Jsonb(thesis), row["thesis_revision_id"]],
            )
        else:
            connection.execute(
                "UPDATE app.thesis SET last_human_reviewed_at = now(), updated_at = now() WHERE id = %s",
                [row["thesis_revision_id"]],
            )
    return {
        "symbol": normalized,
        "outcome": outcome,
        "review_event_id": event["id"],
        "last_reviewed": event["created_at"],
        "revision": row["revision"],
    }


def mark_thesis_reviewed(config: AppConfig, symbol: str) -> dict[str, Any]:
    return record_thesis_review(config, symbol, {"outcome": "unchanged"})


def thesis_rows(config: AppConfig) -> list[dict[str, Any]]:
    runtime = runtime_for_config(config)
    with runtime.read() as connection:
        rows = connection.execute(
            """
            SELECT instrument.symbol, thesis.id AS revision_id, thesis.revision,
                   thesis.thesis AS thesis_json, thesis.author_kind,
                   thesis.change_rationale, thesis.last_assessed_at,
                   thesis.last_human_reviewed_at, thesis.created_at, thesis.updated_at
            FROM app.thesis thesis
            JOIN catalog.instrument instrument ON instrument.id = thesis.instrument_id
            WHERE thesis.status = 'current'
            ORDER BY thesis.updated_at DESC, instrument.symbol
            """
        ).fetchall()
    return [dict(row) for row in rows]


def thesis_history(config: AppConfig, symbol: str) -> dict[str, Any]:
    normalized = canonical_symbol(symbol)
    runtime = runtime_for_config(config)
    with runtime.read() as connection:
        instrument = connection.execute("SELECT id FROM catalog.instrument WHERE symbol = %s", [normalized]).fetchone()
        if not instrument:
            return {"symbol": normalized, "revisions": [], "review_events": [], "automation_runs": [], "assessments": []}
        instrument_id = instrument["id"]
        revisions = [dict(row) for row in connection.execute(
            """
            SELECT id AS revision_id, revision, status, thesis AS thesis_json, author_kind,
                   automation_run_id, superseded_revision_id, change_rationale,
                   last_assessed_at, last_human_reviewed_at, created_at, updated_at
            FROM app.thesis WHERE instrument_id = %s ORDER BY revision DESC
            """,
            [instrument_id],
        ).fetchall()]
        events = [dict(row) for row in connection.execute(
            "SELECT * FROM app.thesis_review_event WHERE instrument_id = %s ORDER BY created_at DESC",
            [instrument_id],
        ).fetchall()]
        runs = [dict(row) for row in connection.execute(
            "SELECT * FROM app.thesis_automation_run WHERE instrument_id = %s ORDER BY started_at DESC LIMIT 20",
            [instrument_id],
        ).fetchall()]
        assessments = [dict(row) for row in connection.execute(
            "SELECT * FROM app.thesis_evidence_assessment WHERE instrument_id = %s ORDER BY created_at DESC LIMIT 100",
            [instrument_id],
        ).fetchall()]
    return {
        "symbol": normalized,
        "revisions": with_revision_diffs(revisions),
        "review_events": events,
        "automation_runs": runs,
        "assessments": assessments,
    }


def thesis_monitor_payload(config: AppConfig) -> dict[str, Any]:
    rows = thesis_monitor_rows(config)
    summary = {
        "total": len(rows),
        "active": sum(1 for row in rows if row.get("has_active_revision")),
        "owned": sum(1 for row in rows if row.get("owned")),
        "watchlist": sum(1 for row in rows if row.get("watched") and not row.get("owned")),
        "needs_review": sum(1 for row in rows if row.get("needs_review")),
        "contradictions": sum(1 for row in rows if row.get("contradiction_available")),
        "invalidation_rule_coverage": {
            "covered": sum(1 for row in rows if row.get("invalidation_rule_count")),
            "denominator": len(rows),
        },
        "coverage": {
            "active_v3": sum(1 for row in rows if row.get("schema_version") == 3 and row.get("has_active_revision")),
            "denominator": len(rows),
        },
        "automation_health": _automation_health(rows),
    }
    return {"rows": rows, "count": len(rows), "summary": summary}


def thesis_monitor_rows(
    config: AppConfig,
    *,
    symbols: list[str] | set[str] | None = None,
    include_current_prices: bool = True,
) -> list[dict[str, Any]]:
    runtime = runtime_for_config(config)
    with runtime.read() as connection:
        rows = monitored_thesis_rows(
            connection,
            symbols=symbols,
            include_current_prices=include_current_prices,
        )
        evidence_by_symbol = thesis_source_evidence(connection, [str(row["symbol"]) for row in rows])
        assessments_by_revision_map = assessments_by_revision(connection, [row.get("revision_id") for row in rows])
    total_market_value = 0.0
    for row in rows:
        row["market_value"] = _market_value(row)
        total_market_value += float(row.get("market_value") or 0)
    output = [
        _thesis_monitor_row(
            row,
            source_evidence=evidence_by_symbol.get(str(row["symbol"]), []),
            assessments=(assessments_by_revision_map.get(int(row["revision_id"])) or []) if row.get("revision_id") else [],
            total_market_value=total_market_value,
        )
        for row in rows
    ]
    return sorted(output, key=lambda row: (float(row.get("priority_score") or 0), bool(row.get("owned")), str(row.get("symbol"))), reverse=True)


def _thesis_monitor_row(
    row: dict[str, Any],
    *,
    source_evidence: list[dict[str, Any]],
    assessments: list[dict[str, Any]],
    total_market_value: float,
) -> dict[str, Any]:
    symbol = str(row["symbol"])
    thesis = dict(row.get("thesis") or {})
    core = str(thesis.get("core_thesis") or thesis.get("thesis") or "").strip()
    why = str(thesis.get("why_owned_watched") or thesis.get("why") or "").strip()
    invalidation_rules = _list_value(thesis.get("invalidation_rules"), [])
    invalidation_text = _invalidation_text(invalidation_rules, thesis)
    reviewed_at = _parse_datetime(row.get("last_human_reviewed_at")) or _parse_datetime(thesis.get("last_reviewed"))
    assessed_at = _parse_datetime(row.get("last_assessed_at"))
    missing = [name for name, value in (("thesis", core), ("why_owned_watched", why), ("invalidation", invalidation_text)) if not value]
    age_days = (datetime.now(UTC).date() - reviewed_at.date()).days if reviewed_at else None
    stale_reason = f"missing {', '.join(missing)}" if missing else (
        f"last reviewed {age_days} days ago" if age_days is not None and age_days > int(thesis.get("review_cadence_days") or THESIS_STALE_DAYS) else ""
    )
    invalidation_price, invalidation_operator = _price_rule(invalidation_rules, thesis, invalidation_text)
    latest_price = _float_or_none(row.get("latest_price"))
    distance = _distance_pct(latest_price, invalidation_price)
    flags: list[str] = []
    if latest_price is not None and invalidation_price is not None:
        breached = latest_price <= invalidation_price if invalidation_operator != ">=" else latest_price >= invalidation_price
        if breached:
            flags.append("invalidation_breached")
        elif distance is not None and distance <= INVALIDATION_NEAR_PCT:
            flags.append("invalidation_near")
    market_value = _float_or_none(row.get("market_value"))
    portfolio_weight = (market_value / total_market_value * 100) if total_market_value and market_value else None
    scoring_row = {**row, "portfolio_weight": portfolio_weight}
    high_contradictions = [
        item for item in assessments
        if item.get("stance") == "contradict"
        and str(item.get("materiality")) in {"medium", "high"}
        and float(item.get("confidence") or 0) >= 0.70
    ]
    if high_contradictions:
        flags.append("high_confidence_contradiction")
    evidence_newer_than_review = _evidence_newer_than_review(source_evidence, reviewed_at)
    confidence = str(thesis.get("confidence") or "unknown").lower()
    priority, lane, review_reason = _priority(scoring_row, thesis, missing, stale_reason, flags, evidence_newer_than_review, high_contradictions)
    evidence_cards = _evidence_cards(source_evidence, assessments)
    source_names = sorted({str(item.get("source_name") or item.get("source_id") or "") for item in source_evidence if item.get("source_name") or item.get("source_id")})
    return _without_none({
        "symbol": symbol,
        "schema_version": 3 if thesis else None,
        "revision": row.get("revision"),
        "revision_id": row.get("revision_id"),
        "has_active_revision": bool(row.get("revision_id") and core),
        "author_kind": row.get("author_kind"),
        "change_rationale": row.get("change_rationale"),
        "last_assessed_at": assessed_at,
        "last_human_reviewed_at": reviewed_at,
        "last_reviewed": reviewed_at,
        "last_reviewed_age_days": age_days,
        "thesis": core or f"No active v3 thesis loaded for {symbol}; review before action.",
        "thesis_text": core or f"No active v3 thesis loaded for {symbol}; review before action.",
        "why_owned_watched": why or "Why-owned/watched rationale is missing.",
        "why": why or "Why-owned/watched rationale is missing.",
        "direction": thesis.get("direction"),
        "timeframe": thesis.get("timeframe"),
        "horizon_date": thesis.get("horizon_date"),
        "conviction": thesis.get("conviction"),
        "confidence": confidence,
        "confidence_tier": confidence,
        "invalidation": invalidation_text or "No invalidation rule loaded.",
        "invalidation_text": invalidation_text or "No invalidation rule loaded.",
        "invalidation_rules": invalidation_rules,
        "invalidation_rule_count": len(invalidation_rules),
        "invalidation_price": invalidation_price,
        "invalidation_operator": invalidation_operator,
        "invalidation_distance_pct": distance,
        "evidence_links": _evidence_links(thesis, source_evidence),
        "source_evidence": source_evidence,
        "evidence_cards": evidence_cards,
        "assessments": assessments,
        "source_names": source_names,
        "source_count": len(source_names),
        "source_evidence_count": len(source_evidence),
        "latest_source_evidence_at": _latest_observed_at(source_evidence),
        "evidence_newer_than_review": evidence_newer_than_review,
        "evidence_coverage_status": thesis.get("evidence_coverage_status") or ("covered" if source_evidence else "low"),
        "status": thesis.get("lifecycle_status") or (
            "owned" if row["owned"] else "watched" if row["watched"] else "underwriting"
        ),
        "owned": bool(row["owned"]),
        "watched": bool(row["watched"]),
        "options_underwriting": bool(row.get("options_underwriting")),
        "source": "theses" if core else "source_evidence" if source_evidence else (
            "options_policy" if row.get("options_underwriting") else "portfolio_watchlist"
        ),
        "stale_thesis": bool(stale_reason),
        "stale_reason": stale_reason,
        "contradiction_flags": flags,
        "contradiction_available": bool(high_contradictions),
        "needs_review": bool(stale_reason or flags or evidence_newer_than_review),
        "review_reason": review_reason,
        "priority_lane": lane,
        "priority_score": priority,
        "portfolio_weight": portfolio_weight,
        "market_value": market_value,
        "unrealized_pnl": _unrealized_pnl(row),
        "unrealized_pnl_pct": _unrealized_pnl_pct(row),
        "latest_price": latest_price,
        "latest_quote_at": row.get("latest_quote_at"),
        "quote_freshness": _freshness(row.get("latest_quote_at")),
        "next_catalyst": row.get("next_catalyst"),
        "next_catalyst_at": row.get("next_catalyst_at"),
        "catalyst_urgency": _catalyst_urgency(row.get("next_catalyst_at")),
        "automation_policy": thesis.get("automation_policy") or "auto",
        "automation_health": _row_automation_health(row),
        "latest_automation_status": row.get("latest_automation_status"),
        "latest_automation_error": row.get("latest_automation_error"),
        "raw_thesis": thesis,
        "structured_fields_missing": missing,
    })


def _priority(
    row: dict[str, Any],
    thesis: dict[str, Any],
    missing: list[str],
    stale_reason: str,
    flags: list[str],
    evidence_newer_than_review: bool,
    contradictions: list[dict[str, Any]],
) -> tuple[int, str, str]:
    owned = bool(row.get("owned"))
    lane = "Owned Risk Exceptions" if owned else (
        "Options Underwriting Gaps" if row.get("options_underwriting") else "Watchlist Underwriting Gaps"
    )
    if "invalidation_breached" in flags:
        base, reason = 100, "breached invalidation rule"
    elif contradictions:
        base, reason = 90, "high-confidence contradiction"
    elif owned and (missing or str(thesis.get("confidence") or "").lower() in {"", "low", "unknown"}):
        base, reason = 80, "incomplete or low-confidence owned thesis"
    elif evidence_newer_than_review:
        base, reason = 70, "new source evidence since last review"
    elif stale_reason:
        base, reason = 60, stale_reason
    else:
        base, reason = 0, "Auditable thesis is current."
        lane = "Current"
    score = base
    if owned:
        score += min(15, int((_float_or_none(row.get("portfolio_weight")) or 0) / 2))
    if _catalyst_urgency(row.get("next_catalyst_at")) == "within_7_days":
        score += 10
    if any(str(item.get("materiality")) == "high" for item in contradictions):
        score += 5
    return min(score, 120), lane, reason


def _invalidation_rules(fields: dict[str, Any], previous: dict[str, Any], direction: str) -> list[dict[str, Any]]:
    supplied = fields.get("invalidation_rules")
    if isinstance(supplied, list):
        return [_normalize_rule(item, direction) for item in supplied if isinstance(item, dict)]
    existing = previous.get("invalidation_rules")
    if isinstance(existing, list) and existing and not any(key in fields for key in ("invalidation", "invalidation_price")):
        return [_normalize_rule(item, direction) for item in existing if isinstance(item, dict)]
    invalidation = str(fields.get("invalidation") or previous.get("invalidation") or "").strip()
    price = _float_or_none(fields.get("invalidation_price", previous.get("invalidation_price")))
    if price is None and invalidation:
        match = INVALIDATION_PRICE_RE.search(invalidation)
        price = _float_or_none(match.group(1)) if match else None
    if invalidation or price is not None:
        return [_without_none({"type": "price" if price is not None else "event", "operator": ">=" if direction in {"short", "bearish"} else "<=", "price": price, "text": invalidation})]
    return []


def _normalize_rule(item: dict[str, Any], direction: str) -> dict[str, Any]:
    rule_type = str(item.get("type") or "event").lower()
    operator = str(item.get("operator") or (">=" if direction in {"short", "bearish"} else "<="))
    if rule_type == "price" and operator not in {"<", "<=", ">", ">="}:
        raise ValueError("price invalidation rules require an explicit comparison operator")
    return _without_none({**item, "type": rule_type, "operator": operator})


def _first_text(source: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = source.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _list_value(value: Any, fallback: Any) -> list[Any]:
    candidate = value if value is not None else fallback
    return candidate if isinstance(candidate, list) else []


def _dict_value(value: Any, fallback: Any) -> dict[str, Any]:
    candidate = value if value is not None else fallback
    return candidate if isinstance(candidate, dict) else {}


def _scenario_value(value: Any, fallback: Any) -> dict[str, Any]:
    candidate = value if value is not None else fallback
    return candidate if isinstance(candidate, dict) else {}


def _int_value(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


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
        return float(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _without_none(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if value is not None}


def _market_value(row: dict[str, Any]) -> float | None:
    quantity = _float_or_none(row.get("quantity"))
    latest_price = _float_or_none(row.get("latest_price"))
    if quantity is None or latest_price is None:
        return None
    return quantity * latest_price


def _unrealized_pnl(row: dict[str, Any]) -> float | None:
    quantity = _float_or_none(row.get("quantity"))
    price = _float_or_none(row.get("latest_price"))
    cost = _float_or_none(row.get("average_cost"))
    if quantity is None or price is None or cost is None:
        return None
    return quantity * (price - cost)


def _unrealized_pnl_pct(row: dict[str, Any]) -> float | None:
    price = _float_or_none(row.get("latest_price"))
    cost = _float_or_none(row.get("average_cost"))
    if price is None or cost in (None, 0):
        return None
    return (price / cost - 1) * 100


def _distance_pct(price: float | None, invalidation_price: float | None) -> float | None:
    if price is None or invalidation_price is None or price == 0:
        return None
    return round(abs(price - invalidation_price) / price * 100, 2)


def _price_rule(rules: list[Any], thesis: dict[str, Any], invalidation_text: str) -> tuple[float | None, str]:
    for raw in rules:
        if isinstance(raw, dict) and str(raw.get("type")) == "price":
            return _float_or_none(raw.get("price")), str(raw.get("operator") or "<=")
    price = _float_or_none(thesis.get("invalidation_price"))
    if price is None and invalidation_text:
        match = INVALIDATION_PRICE_RE.search(invalidation_text)
        price = _float_or_none(match.group(1)) if match else None
    return price, "<="


def _invalidation_text(rules: list[Any], thesis: dict[str, Any]) -> str:
    texts = [str(item.get("text") or item.get("condition") or "").strip() for item in rules if isinstance(item, dict)]
    texts = [text for text in texts if text]
    if texts:
        return "; ".join(texts)
    value = thesis.get("invalidation")
    return "; ".join(map(str, value)) if isinstance(value, list) else str(value or "").strip()


def _evidence_links(thesis: dict[str, Any], source_evidence: list[dict[str, Any]]) -> list[str]:
    stored = [str(item) for item in thesis.get("evidence_links") or [] if item]
    source_links = [str(item.get("reference") or "") for item in source_evidence if item.get("reference")]
    return list(dict.fromkeys(stored + source_links))


def _latest_observed_at(rows: list[dict[str, Any]]) -> datetime | None:
    return max((_parse_datetime(item.get("observed_at")) for item in rows), default=None)


def _evidence_newer_than_review(rows: list[dict[str, Any]], reviewed_at: datetime | None) -> bool:
    latest = _latest_observed_at(rows)
    return bool(latest and (reviewed_at is None or latest > reviewed_at))


def _evidence_cards(source_evidence: list[dict[str, Any]], assessments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    assessment_by_ref = {str(item.get("evidence_reference")): item for item in assessments}
    cards: list[dict[str, Any]] = []
    for item in source_evidence:
        reference = str(item.get("reference") or "")
        assessment = assessment_by_ref.get(reference, {})
        cards.append(_without_none({
            "reference": reference,
            "title": item.get("title") or assessment.get("evidence_title"),
            "source_name": item.get("source_name"),
            "date": item.get("observed_at"),
            "materiality": assessment.get("materiality") or "unknown",
            "stance": assessment.get("stance") or "unassessed",
            "affected_pillar_ids": assessment.get("affected_pillar_ids") or [],
            "rationale": assessment.get("rationale") or item.get("summary"),
        }))
    return cards


def _freshness(value: Any) -> str:
    observed = _parse_datetime(value)
    if observed is None:
        return "unknown"
    hours = (datetime.now(UTC) - observed).total_seconds() / 3600
    if hours <= 24:
        return "fresh"
    if hours <= 72:
        return "stale"
    return "blocked"


def _catalyst_urgency(value: Any) -> str:
    starts = _parse_datetime(value)
    if starts is None:
        return "none"
    days = (starts - datetime.now(UTC)).total_seconds() / 86400
    if days <= 7:
        return "within_7_days"
    if days <= 30:
        return "within_30_days"
    return "later"


def _row_automation_health(row: dict[str, Any]) -> str:
    status = str(row.get("latest_automation_status") or "")
    if not status:
        return "unknown"
    if status == "succeeded":
        return "ok"
    if status in {"failed", "timeout"}:
        return "blocked"
    return status


def _automation_health(rows: list[dict[str, Any]]) -> dict[str, Any]:
    blocked = sum(1 for row in rows if row.get("automation_health") == "blocked")
    unknown = sum(1 for row in rows if row.get("automation_health") == "unknown")
    return {
        "status": "blocked" if blocked else "unknown" if unknown else "ok",
        "blocked": blocked,
        "unknown": unknown,
        "denominator": len(rows),
    }
