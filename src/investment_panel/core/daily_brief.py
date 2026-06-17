"""Daily attention brief read model for the /today surface.

The brief is organised into four lanes that map 1:1 to the Today page sections:

- ``whats_changed``   — fresh source-backed signals (news/blog/memo/thesis/13F)
                        touching owned or watched names, from ``feed_signals``.
- ``decide_now``      — things that want a decision today: act-grade decision-queue
                        candidates, portfolio risk cards, and thesis contradictions.
- ``catalysts``       — scheduled catalysts inside the next two weeks, with days-until.
- ``portfolio_pulse`` — biggest owned movers plus a concentration check.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

from investment_panel.core import brokers
from investment_panel.core.db import query_rows
from investment_panel.core.decision import canonical_quote_rows, manual_watchlist_rows
from investment_panel.core.portfolio_intelligence import portfolio_risk_cards, review_actions
from investment_panel.core.thesis_monitor import thesis_monitor_rows


ACTIONABLE_GRADES = {"act", "research"}
OPERATIONAL_NOISE_GATES = {
    "liquidity_unknown",
    "missing_daily_analysis",
    "missing_intraday_quote",
    "missing_quote",
    "stale_daily_analysis",
    "stale_data",
    "stale_intraday_quote",
    "stale_quote",
}
OPERATIONAL_RISK_CARD_IDS = {"stale-owned-quotes"}
# Concentration is re-homed to the portfolio-pulse lane (with a richer card), so
# skip the equivalent risk card here to avoid showing the same fact twice.
CONCENTRATION_RISK_CARD_IDS = {"largest-position"}
CONCENTRATION_WARN_WEIGHT = 30.0
CATEGORY_LIMITS = {
    "whats_changed": 6,
    "decide_now": 8,
    "catalysts": 8,
    "portfolio_pulse": 5,
}
CATEGORY_ORDER = {
    "whats_changed": 0,
    "decide_now": 1,
    "catalysts": 2,
    "portfolio_pulse": 3,
}


def daily_brief(con: Any) -> list[dict[str, Any]]:
    """Return a ranked, cross-model daily brief for the Today page."""

    book = _book(con)
    items: list[dict[str, Any]] = []
    items.extend(_source_delta_items(con, book))
    items.extend(_decide_now_items(con, book))
    items.extend(_catalyst_items(con, book))
    items.extend(_portfolio_pulse_items(con, book))
    return _category_limited_items(_rank_items(_dedupe(items)))


class _Book:
    """The investor's position context: owned weights + the watchlist set.

    Every card is framed against this so the page reads as *your* book, not a
    generic market feed — owned names carry their weight, watched names are
    flagged, and unrelated symbols can be filtered out.
    """

    def __init__(self, weights: dict[str, float], watched: set[str]) -> None:
        self.weights = weights
        self.watched = watched
        self.owned = set(weights)

    def relevance(self, symbols: list[str]) -> dict[str, Any]:
        owned = [symbol for symbol in symbols if symbol in self.owned]
        watched = [symbol for symbol in symbols if symbol in self.watched and symbol not in self.owned]
        weight = sum(self.weights.get(symbol, 0.0) for symbol in owned)
        if owned:
            label = f"Owned {weight:.1f}%" if weight else "Owned"
        elif watched:
            label = "Watchlist"
        else:
            label = ""
        return {"owned": owned, "watched": watched, "weight": weight, "label": label}

    def bonus(self, symbols: list[str]) -> float:
        """Score lift proportional to owned weight, so decisions about real money
        outrank watchlist housekeeping (a 64%-of-book risk beats a watch-name's
        stale thesis)."""

        weight = sum(self.weights.get(symbol, 0.0) for symbol in symbols if symbol in self.owned)
        return min(20.0, weight * 0.25)


def _book(con: Any) -> _Book:
    priced, total = _priced_holdings(con)
    weights = {row["symbol"]: _weight(float(row.get("market_value") or 0.0), total) for row in priced if row.get("symbol")}
    watched = {str(row.get("symbol") or "").upper() for row in manual_watchlist_rows(con) if row.get("symbol")}
    return _Book(weights, watched)


# --------------------------------------------------------------------------- #
# Lane 1: what changed (source deltas)
# --------------------------------------------------------------------------- #


def _source_delta_items(con: Any, book: _Book) -> list[dict[str, Any]]:
    # ``panel`` imports this module transitively, so import the feed read model
    # lazily (by call time the package is fully initialised) to dodge a cycle.
    from investment_panel.core.panel import feed_signals

    output: list[dict[str, Any]] = []
    for row in feed_signals(con):
        # A "baseline" disclosure is a full holdings snapshot, not a change, and a
        # generic "disclosed" rollup carries no direction — neither is news.
        if str(row.get("action") or "").lower() in {"baseline", "disclosed"}:
            continue
        relevance = book.relevance(_symbols(row))
        # Only surface source moves that touch the investor's book — owned first,
        # then watched. A 24-ticker disclosure becomes "the names you hold".
        if not relevance["owned"] and not relevance["watched"]:
            continue
        display_symbols = relevance["owned"] + relevance["watched"]
        sentiment = str(row.get("sentiment") or "neutral").lower()
        owned = bool(relevance["owned"])
        if owned and sentiment == "bearish":
            severity = "warn"
        elif sentiment == "bullish":
            severity = "good"
        elif sentiment == "bearish":
            severity = "bad"
        else:
            severity = "info"
        family = str(row.get("source_family") or row.get("source_type") or "source")
        source_name = str(row.get("source") or _format_gate(family))
        output.append(
            _item(
                category="whats_changed",
                item_id=f"source-delta:{row.get('id') or row.get('title')}",
                title=str(row.get("title") or "Source update"),
                reason=str(row.get("thesis") or row.get("portfolio_relevance") or ""),
                stats=[source_name, _date_label(row.get("date"))],
                symbols=display_symbols,
                context=relevance["label"],
                score=58 + float(row.get("score") or 0) * 0.3 + (15 if owned else 0) + (8 if sentiment in {"bullish", "bearish"} else 0),
                severity=severity,
                source_models=[f"feed:{family}"],
                sentiment=sentiment,
                antithesis=str(row.get("antithesis") or "").strip(),
                as_of=row.get("date"),
            )
        )
    return output[:12]


# --------------------------------------------------------------------------- #
# Lane 2: decide now (opportunities + risks + thesis contradictions)
# --------------------------------------------------------------------------- #


def _decide_now_items(con: Any, book: _Book) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    output.extend(_opportunity_items(con, book))
    output.extend(_risk_items(con, book))
    output.extend(_thesis_contradiction_items(con, book))
    return output


def _opportunity_items(con: Any, book: _Book) -> list[dict[str, Any]]:
    primary = []
    for row in _decision_queue(con):
        action = str(row.get("action_grade") or "").lower()
        blockers = _list(row.get("blocking_gates"))
        if action not in ACTIONABLE_GRADES or blockers:
            continue
        symbol = str(row.get("symbol") or "").upper()
        reasons = _list(row.get("inclusion_reasons"))
        grade = str(row.get("action_grade") or "Watch")
        stats = [grade.upper()]
        window = str(row.get("catalyst_window") or "").strip()
        if window:
            stats.append(f"Catalyst {window}")
        liquidity = str(row.get("liquidity_grade") or "").strip()
        if liquidity:
            stats.append(f"Liquidity {liquidity}")
        relevance = book.relevance([symbol])
        primary.append(
            _item(
                category="decide_now",
                item_id=f"opportunity:{symbol}",
                title=f"{symbol} — {grade} candidate",
                reason=reasons[0] if reasons else str(row.get("source_cluster") or "Ranked by the decision queue."),
                stats=stats,
                symbols=[symbol],
                context=relevance["label"] or "New idea",
                score=72 + min(25, _float(row.get("action_score")) or _float(row.get("score")) or 0) + book.bonus([symbol]),
                severity="good" if action == "act" else "watch",
                source_models=["decision_queue"],
                as_of=row.get("as_of"),
            )
        )
    return primary[:8]


def _risk_items(con: Any, book: _Book) -> list[dict[str, Any]]:
    output = []
    for card in portfolio_risk_cards(con)[:8]:
        symbols = _symbols(card)
        card_id = str(card.get("card_id") or card.get("risk_type") or "risk")
        if card_id in OPERATIONAL_RISK_CARD_IDS or card_id in CONCENTRATION_RISK_CARD_IDS:
            continue
        relevance = book.relevance(symbols)
        output.append(
            _item(
                category="decide_now",
                item_id=f"risk:{card_id}",
                title=str(card.get("title") or "Portfolio risk"),
                reason=_decision_copy(str(card.get("summary") or "Portfolio risk card requires review.")),
                stats=_substantive(_decision_evidence(_list(card.get("evidence")))) or [str(card.get("impact") or "portfolio risk")],
                symbols=symbols,
                context=relevance["label"],
                score=(_float(card.get("score")) or 60) + book.bonus(symbols),
                severity=str(card.get("severity") or "warn"),
                source_models=_list(card.get("source_models")) or ["portfolio_risk_cards"],
            )
        )
    return output


def _thesis_contradiction_items(con: Any, book: _Book) -> list[dict[str, Any]]:
    output = []
    for row in thesis_monitor_rows(con, []):
        if not row.get("needs_review"):
            continue
        flags = _list(row.get("contradiction_flags"))
        stale = bool(row.get("stale_thesis"))
        if not flags and not stale:
            continue
        symbol = str(row.get("symbol") or "").upper()
        owned = bool(row.get("owned"))
        last = _float(row.get("latest_price"))
        exit_price = _float(row.get("invalidation_price"))
        exit_dist = _float(row.get("invalidation_distance_pct"))
        signal = str(row.get("decision_action") or "").strip()
        stats: list[str] = []
        if last is not None:
            stats.append(f"Last ${last:,.0f}")
        if signal:
            stats.append(f"Signal {signal}")
        if exit_price is not None:
            stats.append(f"Exit ${exit_price:,.0f}" + (f" ({exit_dist:+.0f}%)" if exit_dist is not None else ""))
        elif stale:
            # The missing level is itself the finding for an owned name.
            stats.append("No written exit rule")
        relevance = book.relevance([symbol])
        output.append(
            _item(
                category="decide_now",
                item_id=f"thesis-review:{symbol}",
                title=f"{symbol} — thesis at risk",
                reason=_decision_copy(str(row.get("review_reason") or "Thesis is stale or contradicted by current data.")),
                stats=stats or [_decision_copy(flag) for flag in flags] or [str(row.get("stale_reason") or "thesis review")],
                symbols=[symbol],
                context=relevance["label"] or ("Owned" if owned else "Watchlist"),
                score=76 + book.bonus([symbol]) + len(flags) * 3,
                severity="warn",
                source_models=["thesis_monitor"],
            )
        )
    return output[:6]


# --------------------------------------------------------------------------- #
# Lane 3: this week (catalyst timeline, framed by your exposure)
# --------------------------------------------------------------------------- #


def _catalyst_items(con: Any, book: _Book) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        WITH calendar_rows AS (
            SELECT symbol, event_date, event, expected_impact, source, importance, verification_status, source_url
            FROM catalysts
            WHERE event_date IS NOT NULL AND event_date >= current_date AND event_date <= current_date + INTERVAL 14 DAYS
            UNION ALL
            SELECT symbol, event_date, event_type AS event,
                   'Upcoming earnings event' AS expected_impact,
                   source, 'medium' AS importance, 'watch' AS verification_status, CAST(NULL AS TEXT) AS source_url
            FROM earnings_events
            WHERE event_date IS NOT NULL AND event_date >= current_date AND event_date <= current_date + INTERVAL 14 DAYS
        )
        SELECT *
        FROM calendar_rows
        ORDER BY event_date ASC, importance DESC NULLS LAST, symbol
        LIMIT 60
        """,
    )
    # Only catalysts on names the investor actually holds or watches — a wall of
    # foreign-exchange earnings they don't own is noise, not signal. When there's
    # nothing on their book, the lane honestly shows empty.
    mine: list[dict[str, Any]] = []
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        relevance = book.relevance([symbol])
        if not (relevance["owned"] or relevance["watched"]):
            continue
        mine.append(_catalyst_item(row, symbol, relevance))
    return mine[:8]


def _catalyst_item(row: dict[str, Any], symbol: str, relevance: dict[str, Any]) -> dict[str, Any]:
    event = str(row.get("event") or "Scheduled event")
    days_until = _days_until(row.get("event_date"))
    high = str(row.get("importance") or "").lower() == "high"
    held = bool(relevance["owned"] or relevance["watched"])
    exposure = relevance["label"] or "Not held"
    impact = str(row.get("expected_impact") or "").strip()
    stats = [_due_label(days_until, row.get("event_date")), exposure]
    if impact and impact.lower() != "upcoming earnings event":
        stats.append(impact)
    return _item(
        category="catalysts",
        item_id=f"catalyst:{symbol or 'market'}:{row.get('event_date')}:{event}",
        title=f"{symbol or 'Market'} — {event}",
        reason=impact or event,
        stats=stats,
        symbols=[symbol] if symbol else [],
        context=exposure,
        # Sooner, higher-importance, and held names rank first within the lane.
        score=80 - min(60, max(0, days_until if days_until is not None else 14) * 3) + (8 if high else 0) + (12 if held else 0),
        severity="warn" if (high and held) else "info",
        source_models=["catalysts", "earnings_events"],
        days_until=days_until,
        as_of=row.get("event_date"),
    )


# --------------------------------------------------------------------------- #
# Lane 4: portfolio pulse (movers + concentration)
# --------------------------------------------------------------------------- #


def _portfolio_pulse_items(con: Any, book: _Book) -> list[dict[str, Any]]:
    priced, total = _priced_holdings(con)
    output: list[dict[str, Any]] = []
    for row in sorted(priced, key=lambda item: abs(float(item.get("day_change_value") or 0.0)) + abs(float(item.get("change_pct") or 0.0)), reverse=True)[:5]:
        symbol = row["symbol"]
        weight = _weight(float(row.get("market_value") or 0.0), total)
        change_pct = _float(row.get("change_pct"))
        day_change_value = _float(row.get("day_change_value"))
        missing_change = change_pct is None and day_change_value is None
        stats: list[str] = []
        if change_pct is not None:
            stats.append(f"{change_pct:+.1f}% today")
        if day_change_value is not None:
            stats.append(f"{_signed_money(day_change_value)} P/L")
        stats.append(f"{weight:.0f}% of book")
        output.append(
            _item(
                category="portfolio_pulse",
                item_id=f"portfolio-change:{symbol}",
                title=f"{symbol} {_move_label(change_pct, day_change_value)}",
                reason="" if not missing_change else f"{symbol} has no current quote, so today's P/L scan is incomplete.",
                stats=stats,
                symbols=[symbol],
                context=f"Owned {weight:.1f}%",
                score=80 + min(20, abs(change_pct or 0) * 2 + min(10, weight / 5)),
                severity="warn" if missing_change else "good" if (change_pct or 0) >= 0 else "bad",
                source_models=["portfolio", "quotes"],
                sentiment="bullish" if (change_pct or 0) > 0 else "bearish" if (change_pct or 0) < 0 else "neutral",
            )
        )

    if priced and total:
        top = max(priced, key=lambda item: float(item.get("market_value") or 0.0))
        weight = _weight(float(top.get("market_value") or 0.0), total)
        if weight >= CONCENTRATION_WARN_WEIGHT:
            symbol = top["symbol"]
            output.append(
                _item(
                    category="portfolio_pulse",
                    item_id="concentration",
                    title=f"{symbol} is {weight:.0f}% of your book",
                    reason=f"A single-name drawdown in {symbol} moves the whole book; size against your concentration limit.",
                    stats=[f"{weight:.0f}% of book", f"${float(top.get('market_value') or 0.0):,.0f}"],
                    symbols=[symbol],
                    context=f"Owned {weight:.1f}%",
                    score=86,
                    severity="warn",
                    source_models=["portfolio"],
                )
            )
    return output


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def _decision_queue(con: Any) -> list[dict[str, Any]]:
    rows = query_rows(
        con,
        """
        SELECT symbol, as_of, rank, action_grade, score, action_score,
               freshness_status, quote_freshness, daily_analysis_freshness,
               filing_freshness, thesis_freshness, source_cluster, evidence_count,
               inclusion_reasons, blocking_gates, decision_basis, next_event_at,
               catalyst_window, liquidity_grade, portfolio_impact, invalidation
        FROM decision_queue
        ORDER BY rank ASC NULLS LAST, action_score DESC NULLS LAST, score DESC NULLS LAST
        LIMIT 40
        """,
    )
    for row in rows:
        row["inclusion_reasons"] = _json(row.get("inclusion_reasons"), [])
        row["blocking_gates"] = _json(row.get("blocking_gates"), [])
        row["decision_basis"] = _json(row.get("decision_basis"), {})
        row["portfolio_impact"] = _json(row.get("portfolio_impact"), {})
    return rows


def _rank_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counters: dict[str, int] = {}
    ranked = sorted(items, key=lambda item: (CATEGORY_ORDER.get(str(item.get("category")), 9), -float(item.get("score") or 0), str(item.get("title") or "")))
    for item in ranked:
        category = str(item.get("category") or "brief")
        counters[category] = counters.get(category, 0) + 1
        item["rank"] = counters[category]
    return ranked


def _category_limited_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for item in items:
        category = str(item.get("category") or "")
        limit = CATEGORY_LIMITS.get(category, 6)
        if int(item.get("rank") or 0) <= limit:
            output.append(item)
    return output[: sum(CATEGORY_LIMITS.values())]


def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    output = []
    for item in items:
        key = (str(item.get("category") or ""), str(item.get("item_id") or item.get("title") or ""))
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def _item(
    *,
    category: str,
    item_id: str,
    title: str,
    reason: str,
    stats: list[str],
    score: float,
    severity: str,
    symbols: list[str] | None = None,
    context: str | None = None,
    source_models: list[str] | None = None,
    sentiment: str | None = None,
    antithesis: str | None = None,
    days_until: int | None = None,
    as_of: Any = None,
) -> dict[str, Any]:
    symbols = [symbol for symbol in (symbols or []) if symbol]
    return {
        "item_id": item_id,
        "category": category,
        "rank": None,
        "title": title,
        "symbol": symbols[0] if symbols else None,
        "symbols": symbols,
        # Position relevance ("Owned 6.2%" / "Watchlist") — the first thing a
        # power investor reads to know whether this touches their money.
        "context": context or "",
        "reason": reason or "",
        # Short, numeric, decision-relevant facts rendered as a stat row.
        "stats": _substantive([item for item in stats if item and item != "-"]),
        "antithesis": antithesis or "",
        "score": round(float(score or 0), 2),
        "severity": severity or "info",
        "sentiment": sentiment or "",
        "days_until": days_until,
        "source_models": source_models or [],
        "as_of": as_of or datetime.now(UTC),
    }


def _move_label(change_pct: float | None, day_change_value: float | None) -> str:
    if change_pct is None and day_change_value is None:
        return "has no current quote"
    direction = "up" if (change_pct or 0) >= 0 else "down"
    if change_pct is not None:
        return f"{direction} {abs(change_pct):.1f}% today"
    return f"moved {direction} today"


def _priced_holdings(con: Any) -> tuple[list[dict[str, Any]], float]:
    """Owned positions with market value and today's move — shared by the pulse
    lane and the position-context book so the two never disagree."""

    quotes = {str(row.get("symbol") or "").upper(): row for row in canonical_quote_rows(con)}
    priced: list[dict[str, Any]] = []
    for row in brokers.effective_portfolio_rows(con):
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        quote = quotes.get(symbol, {})
        price = _float(row.get("market_price")) or _float(quote.get("price"))
        quantity = _float(row.get("quantity")) or 0.0
        avg_cost = _float(row.get("avg_cost") or row.get("average_cost")) or 0.0
        market_value = _float(row.get("market_value"))
        if market_value is None and price is not None:
            market_value = quantity * price
        if market_value is None:
            market_value = quantity * avg_cost
        change_abs = _float(quote.get("change_abs"))
        priced.append(
            {
                "symbol": symbol,
                "market_value": market_value or 0.0,
                "change_pct": _float(quote.get("change_pct")),
                "day_change_value": quantity * change_abs if change_abs is not None else None,
            }
        )
    total = sum(float(row.get("market_value") or 0.0) for row in priced)
    return priced, total


def _substantive(stats: list[str]) -> list[str]:
    """Drop operational meta (rank/score/coverage %) that isn't an investment fact."""

    noise = ("rank ", "score ", "evidence rows", "% affected", "affected", "verification ", "source ")
    out = []
    for item in stats:
        lowered = item.lower()
        if any(lowered.startswith(prefix) or prefix in lowered for prefix in noise):
            continue
        out.append(item)
    return out


def _signed_money(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):,.0f}"


def _date_label(value: Any) -> str:
    target = _as_date(value)
    return target.strftime("%b %-d") if target else ""


def _format_gate(value: Any) -> str:
    text = str(value or "").replace("_", " ").strip()
    return text[:1].upper() + text[1:] if text else "Unknown gate"


def _decision_evidence(evidence: list[str]) -> list[str]:
    noisy_terms = ("quote stale", "stale quote", "quote fresh", "freshness not fresh", "freshness stale")
    clean = []
    for item in evidence:
        lowered = item.lower()
        if any(term in lowered for term in noisy_terms):
            continue
        if "thesis_json" in lowered:
            clean.append("thesis record is empty")
        else:
            clean.append(_decision_copy(item))
    return clean


def _decision_copy(value: str) -> str:
    return value.replace("invalidation", "exit rule").replace("Invalidation", "Exit rule")


def _due_label(days_until: int | None, event_date: Any) -> str:
    if days_until is None:
        return f"date {event_date}"
    if days_until <= 0:
        return "due today"
    if days_until == 1:
        return "due tomorrow"
    return f"in {days_until} days"


def _days_until(value: Any) -> int | None:
    target = _as_date(value)
    if target is None:
        return None
    # Compare against the local date so this lines up with DuckDB ``current_date``
    # (session-local) used by the catalyst query — otherwise an evening-UTC roll
    # over shows today's events as "-1 / due yesterday".
    return (target - datetime.now().date()).days


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value[:10]).date()
        except ValueError:
            return None
    return None


def _symbols(row: dict[str, Any]) -> list[str]:
    value = row.get("symbols")
    if isinstance(value, list):
        return [str(item).upper() for item in value if item]
    symbol = str(row.get("symbol") or "").upper()
    return [symbol] if symbol else []


def _list(value: Any) -> list[str]:
    parsed = _json(value, value)
    if isinstance(parsed, list):
        return [str(item) for item in parsed if item not in (None, "")]
    if isinstance(parsed, str) and parsed:
        return [parsed]
    return []


def _json(value: Any, fallback: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return fallback
    return value if value is not None else fallback


def _float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _weight(value: float, total: float) -> float:
    return (value / total) * 100 if total else 0.0
