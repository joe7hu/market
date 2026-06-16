"""Structured thesis monitor read model."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, time
from typing import Any

from investment_panel.core import brokers
from investment_panel.core.db import query_rows
from investment_panel.core.decision import canonical_quote_rows


THESIS_STALE_DAYS = 45
INVALIDATION_NEAR_PCT = 10.0
# Prefer a price that is explicitly anchored to an invalidation keyword. A bare
# "$300 price target" should never be mistaken for the invalidation level.
INVALIDATION_PRICE_RE = re.compile(
    r"(?:below|under|stop(?:\s+loss)?(?:\s+at)?|invalidat\w*(?:\s+at)?)\s*\$?\s*([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)
BARE_PRICE_RE = re.compile(r"\$\s*([0-9]+(?:\.[0-9]+)?)")
# Keys that must survive field compaction so downstream consumers can rely on
# them being present even when falsey (e.g. needs_review == False).
_ALWAYS_KEEP_FIELDS = frozenset(
    {"symbol", "owned", "watched", "needs_review", "stale_thesis"}
)


def thesis_monitor_rows(con: Any, config_watchlist: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Return auditable thesis state for owned and watched symbols."""

    symbols = _symbols_to_monitor(con, config_watchlist or [])
    if not symbols:
        return []
    thesis_rows = _theses_by_symbol(con)
    birdclaw = _birdclaw_by_symbol(con)
    agent_theses = _agent_theses_by_symbol(con)
    quotes = {str(row.get("symbol") or "").upper(): row for row in canonical_quote_rows(con)}
    decisions = _decisions_by_symbol(con)
    portfolio = {str(row.get("symbol") or "").upper(): row for row in brokers.effective_portfolio_rows(con)}
    watchlist = {str(item.get("symbol") or "").upper() for item in config_watchlist or [] if item.get("symbol")}

    output = []
    for symbol in sorted(symbols):
        stored = thesis_rows.get(symbol, {})
        thesis_json = _json_obj(stored.get("thesis_json"))
        evidence = birdclaw.get(symbol, [])
        agent = agent_theses.get(symbol, {})
        decision = decisions.get(symbol, {})
        quote = quotes.get(symbol, {})
        owned = symbol in portfolio
        watched = symbol in watchlist
        updated_at = _parse_dt(stored.get("updated_at"))
        last_reviewed = _last_reviewed(thesis_json, updated_at)
        stored_thesis = _first_text(thesis_json, ("thesis", "core_thesis", "summary", "claim"))
        has_stored_content = bool(stored_thesis)
        raw_thesis = stored_thesis or _agent_thesis_text(agent) or _first_evidence_summary(evidence)
        raw_why = _first_text(thesis_json, ("why_owned_watched", "why_owned", "why_watched", "why", "rationale", "why_now"))
        raw_invalidation = _text_or_join(thesis_json.get("invalidation") or thesis_json.get("invalidation_criteria") or thesis_json.get("risk_trigger")) or _agent_invalidation_text(agent)
        evidence_links = _evidence_links(thesis_json, evidence) or _agent_evidence_links(agent) or _fallback_evidence_links(symbol, stored, evidence, decision)
        status = _status(thesis_json, owned, watched)
        latest_price = _float(quote.get("price") or decision.get("latest_quote"))
        thesis = raw_thesis or _fallback_thesis(symbol, owned, watched, decision)
        why = raw_why or _fallback_why(symbol, owned, watched, decision)
        invalidation = raw_invalidation or _fallback_invalidation(symbol, latest_price, decision)
        invalidation_price = _invalidation_price(thesis_json, raw_invalidation)
        invalidation_distance_pct = _invalidation_distance_pct(latest_price, invalidation_price)
        stale_thesis, stale_reason = _stale_status(raw_thesis, raw_why, raw_invalidation, last_reviewed)
        contradiction_flags = _contradiction_flags(decision, owned, latest_price, invalidation_price, invalidation_distance_pct)
        review_reason = _review_reason(stale_thesis, stale_reason, contradiction_flags, decision)
        output.append(
            _compact_empty_fields(
                {
                    "symbol": symbol,
                    "thesis": thesis,
                    "thesis_text": thesis,
                    "why_owned_watched": why,
                    "why_owned": why,
                    "why_watched": why,
                    "why": why,
                    "invalidation": invalidation,
                    "invalidation_text": invalidation,
                    "evidence_links": evidence_links,
                    "evidence": evidence_links,
                    "last_reviewed": last_reviewed,
                    "last_reviewed_age_days": _age_days(last_reviewed),
                    "status": status,
                    "owned": owned,
                    "watched": watched,
                    "source": _thesis_source(has_stored_content, agent, evidence),
                    "updated_at": updated_at,
                    "stale_thesis": stale_thesis,
                    "stale_reason": stale_reason,
                    "contradiction_flags": contradiction_flags,
                    "needs_review": stale_thesis or bool(contradiction_flags),
                    "review_reason": review_reason,
                    "latest_price": latest_price,
                    "latest_quote_at": quote.get("observed_at") or decision.get("latest_quote_at"),
                    "invalidation_price": invalidation_price,
                    "invalidation_distance_pct": invalidation_distance_pct,
                    "decision_action": decision.get("action_grade"),
                    "decision_freshness": decision.get("freshness_status") or decision.get("overall_decision_freshness"),
                    "blocking_gates": decision.get("blocking_gates") or [],
                    "evidence_count": len(evidence_links),
                    "raw_thesis": thesis_json,
                    "structured_fields_missing": _structured_fields_missing(raw_thesis, raw_why, raw_invalidation, last_reviewed),
                    "agent_confidence": _float(agent.get("confidence")) if agent else None,
                    "agent_required_proofs": _string_list(agent.get("required_proofs")) if agent else [],
                    "agent_bear_case": str(agent.get("bear_case") or "").strip() if agent else "",
                    "agent_bull_target_price": _float(agent.get("bull_target_price")) if agent else None,
                    "agent_base_target_price": _float(agent.get("base_target_price")) if agent else None,
                }
            )
        )
    return sorted(output, key=lambda row: (bool(row.get("needs_review")), bool(row.get("owned")), _age_days(row.get("last_reviewed")) or -1), reverse=True)


def _symbols_to_monitor(con: Any, config_watchlist: list[dict[str, Any]]) -> set[str]:
    symbols = {str(item.get("symbol") or "").upper() for item in config_watchlist if item.get("symbol")}
    for table in ("portfolio_positions", "theses", "birdclaw_theses"):
        for row in query_rows(con, f"SELECT DISTINCT symbol FROM {table} WHERE symbol IS NOT NULL"):
            symbol = str(row.get("symbol") or "").upper()
            if symbol:
                symbols.add(symbol)
    for row in brokers.effective_portfolio_rows(con):
        symbol = str(row.get("symbol") or "").upper()
        if symbol:
            symbols.add(symbol)
    return symbols


def _theses_by_symbol(con: Any) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("symbol") or "").upper(): row
        for row in query_rows(con, "SELECT symbol, thesis_json, updated_at FROM theses")
        if row.get("symbol")
    }


def _birdclaw_by_symbol(con: Any) -> dict[str, list[dict[str, Any]]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, thesis_summary, created_at, claims, source_url
        FROM birdclaw_theses
        WHERE symbol IS NOT NULL
        ORDER BY created_at DESC
        """,
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("symbol") or "").upper(), []).append(row)
    return grouped


def _agent_theses_by_symbol(con: Any) -> dict[str, dict[str, Any]]:
    """Latest option-agent thesis per ticker, enriching monitored symbols.

    Agent theses are falsifiable hypotheses (core_thesis, invalidation_conditions,
    targets, evidence_refs) and are the richest structured thesis source in the
    system. They enrich existing monitored symbols only; they never add new
    symbols to the monitor (radar candidates are not owned/watched names).
    """

    try:
        rows = query_rows(
            con,
            """
            SELECT ticker, core_thesis, required_proofs, invalidation_conditions,
                   catalysts, catalyst_summary, bear_case, confidence, evidence_refs,
                   bull_target_price, base_target_price, created_at
            FROM agent_thesis
            QUALIFY row_number() OVER (PARTITION BY ticker ORDER BY created_at DESC) = 1
            """,
        )
    except Exception:
        return {}
    output: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("ticker") or "").upper()
        if not symbol:
            continue
        decoded = dict(row)
        decoded["required_proofs"] = _json_list(decoded.get("required_proofs"))
        decoded["invalidation_conditions"] = _json_list(decoded.get("invalidation_conditions"))
        decoded["catalysts"] = _json_list(decoded.get("catalysts"))
        decoded["evidence_refs"] = _json_list(decoded.get("evidence_refs"))
        output[symbol] = decoded
    return output


def _agent_thesis_text(agent: dict[str, Any]) -> str:
    return str(agent.get("core_thesis") or "").strip()


def _agent_invalidation_text(agent: dict[str, Any]) -> str:
    return _text_or_join(agent.get("invalidation_conditions"))


def _agent_evidence_links(agent: dict[str, Any]) -> list[str]:
    links: list[str] = []
    for item in agent.get("evidence_refs") or []:
        if isinstance(item, str) and item.strip():
            links.append(item.strip())
        elif isinstance(item, dict):
            link = str(item.get("url") or item.get("source_url") or item.get("href") or item.get("ref") or "").strip()
            if link:
                links.append(link)
    return list(dict.fromkeys(links))


def _thesis_source(has_stored_content: bool, agent: dict[str, Any], evidence: list[dict[str, Any]]) -> str:
    if has_stored_content:
        return "theses"
    if agent:
        return "agent_thesis"
    if evidence:
        return "arco_thesis"
    return "portfolio_watchlist"


def _decisions_by_symbol(con: Any) -> dict[str, dict[str, Any]]:
    rows = query_rows(con, "SELECT * FROM decision_queue")
    output: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        decoded = dict(row)
        decoded["blocking_gates"] = _json_list(decoded.get("blocking_gates"))
        decoded["decision_basis"] = _json_obj(decoded.get("decision_basis"))
        output[symbol] = decoded
    return output


def _last_reviewed(thesis: dict[str, Any], updated_at: datetime | None) -> datetime | None:
    for key in ("last_reviewed", "reviewed_at", "lastReviewed", "last_reviewed_at"):
        parsed = _parse_dt(thesis.get(key))
        if parsed:
            return parsed
    return updated_at


def _stale_status(thesis: str, why: str, invalidation: str, last_reviewed: datetime | None) -> tuple[bool, str]:
    missing = []
    if not thesis:
        missing.append("thesis")
    if not why:
        missing.append("why owned/watched")
    if not invalidation:
        missing.append("invalidation")
    if missing:
        return True, f"missing {', '.join(missing)}"
    age = _age_days(last_reviewed)
    if age is None:
        return True, "missing review date"
    if age > THESIS_STALE_DAYS:
        return True, f"last reviewed {age} days ago"
    return False, ""


def _contradiction_flags(
    decision: dict[str, Any],
    owned: bool,
    latest_price: float | None,
    invalidation_price: float | None,
    invalidation_distance_pct: float | None,
) -> list[str]:
    flags: list[str] = []
    action = str(decision.get("action_grade") or "")
    if owned and action in {"Reject", "Stale"}:
        flags.append(f"owned_position_decision_{action.lower()}")
    gates = [str(gate) for gate in decision.get("blocking_gates") or []]
    if owned and any("liquidity_bad" == gate or "decision_reject" == gate for gate in gates):
        flags.append("owned_position_has_hard_gate")
    if latest_price is not None and invalidation_price is not None:
        if latest_price <= invalidation_price:
            flags.append("invalidation_breached")
        elif invalidation_distance_pct is not None and invalidation_distance_pct <= INVALIDATION_NEAR_PCT:
            flags.append("invalidation_near")
    return list(dict.fromkeys(flags))


def _review_reason(stale: bool, stale_reason: str, flags: list[str], decision: dict[str, Any]) -> str:
    if "invalidation_breached" in flags:
        return "Needs review because the latest price is through the stored invalidation level."
    if "invalidation_near" in flags:
        return "Needs review because the latest price is near the stored invalidation level."
    if flags:
        action = decision.get("action_grade") or "decision model"
        return f"Needs review because thesis conflicts with current {action} decision state."
    if stale:
        return f"Needs review because thesis is stale: {stale_reason}."
    return "Auditable thesis is current."


def _fallback_thesis(symbol: str, owned: bool, watched: bool, decision: dict[str, Any]) -> str:
    action = decision.get("action_grade") or "Monitor"
    if owned:
        return f"No structured thesis loaded for owned position {symbol}; review before increasing exposure."
    if watched:
        return f"No structured thesis loaded for watchlist symbol {symbol}; keep at {action} until a thesis is recorded."
    return f"No structured thesis loaded for {symbol}; use source evidence before action."


def _fallback_why(symbol: str, owned: bool, watched: bool, decision: dict[str, Any]) -> str:
    reason = decision.get("next_action") or decision.get("review_reason") or decision.get("freshness_status") or "source review"
    if owned:
        return f"Owned position {symbol}; why-owned rationale is missing. Current model reason: {reason}."
    if watched:
        return f"Watchlist symbol {symbol}; why-watched rationale is missing. Current model reason: {reason}."
    return f"{symbol} is source-discovered; add why-owned/watched rationale before promotion."


def _fallback_invalidation(symbol: str, latest_price: float | None, decision: dict[str, Any]) -> str:
    price_context = f" Latest price is {latest_price:.2f}." if latest_price is not None else ""
    gate_context = ", ".join(str(gate) for gate in decision.get("blocking_gates") or []) or "no hard gate loaded"
    return f"No invalidation rule loaded for {symbol}; set a price or evidence trigger before action.{price_context} Current gates: {gate_context}."


def _fallback_evidence_links(symbol: str, stored: dict[str, Any], evidence: list[dict[str, Any]], decision: dict[str, Any]) -> list[str]:
    if stored:
        return [f"local:theses:{symbol}"]
    if evidence:
        return [f"local:birdclaw_theses:{symbol}"]
    if decision:
        return [f"local:decision_queue:{symbol}"]
    return [f"local:watchlist:{symbol}"]


def _structured_fields_missing(thesis: str, why: str, invalidation: str, last_reviewed: datetime | None) -> list[str]:
    missing = []
    if not thesis:
        missing.append("thesis")
    if not why:
        missing.append("why_owned_watched")
    if not invalidation:
        missing.append("invalidation")
    if not last_reviewed:
        missing.append("last_reviewed")
    return missing


def _status(thesis: dict[str, Any], owned: bool, watched: bool) -> str:
    explicit = str(thesis.get("status") or thesis.get("position_status") or "").strip().lower()
    if explicit and explicit not in {"unknown", "none"}:
        return explicit
    if owned:
        return "owned"
    if watched:
        return "watched"
    return "monitor"


def _invalidation_price(thesis: dict[str, Any], invalidation: str) -> float | None:
    for key in ("invalidation_price", "invalidation_stop", "stop_price", "stop_loss", "risk_level"):
        value = _float(thesis.get(key))
        if value and value > 0:
            return value
    text = invalidation or ""
    keyword_match = INVALIDATION_PRICE_RE.search(text)
    if keyword_match:
        return _float(keyword_match.group(1))
    bare_match = BARE_PRICE_RE.search(text)
    if bare_match:
        return _float(bare_match.group(1))
    return None


def _invalidation_distance_pct(latest_price: float | None, invalidation_price: float | None) -> float | None:
    if latest_price is None or invalidation_price is None or latest_price <= 0:
        return None
    return round(abs(latest_price - invalidation_price) / latest_price * 100, 2)


def _evidence_links(thesis: dict[str, Any], evidence: list[dict[str, Any]]) -> list[str]:
    links: list[str] = []
    for key in ("evidence_links", "evidence", "links", "sources", "source_urls"):
        value = thesis.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    links.append(item.strip())
                elif isinstance(item, dict):
                    link = str(item.get("url") or item.get("source_url") or item.get("href") or "").strip()
                    if link:
                        links.append(link)
        elif isinstance(value, str) and value.strip():
            links.append(value.strip())
    for row in evidence:
        link = str(row.get("source_url") or "").strip()
        if link:
            links.append(link)
    return list(dict.fromkeys(links))


def _first_evidence_summary(evidence: list[dict[str, Any]]) -> str:
    for row in evidence:
        summary = str(row.get("thesis_summary") or "").strip()
        if summary:
            return summary
    return ""


def _first_text(source: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        text = _text_or_join(source.get(key))
        if text:
            return text
    return ""


def _text_or_join(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, list):
        return "; ".join(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, dict):
        return "; ".join(f"{key}: {item}" for key, item in value.items() if item not in (None, ""))
    return str(value).strip()


def _json_obj(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _string_list(value: Any) -> list[str]:
    items = value if isinstance(value, list) else _json_list(value)
    return [str(item).strip() for item in items if str(item).strip()]


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _age_days(value: Any) -> int | None:
    parsed = _parse_dt(value)
    if parsed is None:
        return None
    return max(0, (datetime.now(UTC) - parsed).days)


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _compact_empty_fields(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in row.items()
        if key in _ALWAYS_KEEP_FIELDS or value not in (None, "", [], {})
    }
