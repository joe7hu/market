"""Ticker decision-brief assembly and gating helpers."""

from __future__ import annotations
from typing import Any

from investment_panel.core.decision.brief_coerce import (
    _first_row,
    _fmt_money,
    _fmt_pct,
    _latest_row,
    _number,
    _object,
    _text,
    _text_join,
    _text_list,
)
from investment_panel.core.decision.brief_options import (
    _best_option,
    _is_option_expired,
    _max_loss,
    _missing_families,
    _options_context,
    _ticker_tab_summaries,
)


def ticker_decision_brief(symbol: str, tables: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Build a trader-readable per-symbol decision brief from existing ticker rows."""

    decision_row = _first_row(tables, "symbol_decision_snapshot", "symbol_decision_snapshots", "decision_queue", "opportunities_ranked", "candidates")
    quote_row = _latest_row(tables.get("quotes") or [], ("observed_at", "as_of", "date"))
    snapshot = _object(decision_row.get("snapshot"))
    basis = _object(decision_row.get("decision_basis"))
    canonical_quote = _canonical_quote(symbol, quote_row, decision_row, snapshot)
    latest_price = _number(canonical_quote.get("price"))
    technical = _latest_row(tables.get("technicals") or [], ("date", "as_of"))
    sepa = _latest_row(tables.get("sepa") or [], ("as_of", "date"))
    liquidity = _latest_row(tables.get("liquidity") or [], ("as_of", "date"))
    valuation_rows = sorted(tables.get("valuations") or [], key=lambda row: _number(row.get("upside_pct")), reverse=True)
    best_valuation = valuation_rows[0] if valuation_rows else {}
    earnings_setup = _latest_row(tables.get("earnings_setups") or [], ("event_date", "as_of"))
    option_rows = tables.get("options_payoff_scenarios") or []
    live_option_rows = [row for row in option_rows if not _is_option_expired(row)]
    expired_option_rows = [row for row in option_rows if _is_option_expired(row)]
    best_option = _best_option(live_option_rows)
    portfolio_row = _first_row(tables, "portfolio")
    research_packet = _latest_row(tables.get("research_packets") or [], ("created_at", "as_of"))
    action = _text(decision_row.get("action_grade") or decision_row.get("decision") or "Watch")
    blockers = _text_list(decision_row.get("blocking_gates"))
    if _is_no_trade_action(action) and "decision_reject" not in blockers:
        blockers = [*blockers, "decision_reject"]
    if expired_option_rows and not live_option_rows:
        blockers = list(dict.fromkeys([*blockers, "expired_options_context"]))
    missing_families = _missing_families(tables)

    stance = _stance(action, blockers, best_valuation, research_packet)
    setup = {
        "stance": stance,
        "timeframe": _timeframe(earnings_setup, best_option),
        "catalyst": _catalyst(decision_row, snapshot, earnings_setup, tables.get("catalysts") or []),
        "entry_zone": _entry_zone(latest_price, technical, sepa, best_valuation, blockers),
        "invalidation_level": _invalidation_level(technical, sepa),
        "target_range": _target_range(best_valuation, latest_price, blockers),
        "risk_reward": _risk_reward(latest_price, technical, best_valuation),
        "review_date": _review_date(earnings_setup, research_packet),
    }

    return {
        "symbol": symbol,
        "canonical_quote": canonical_quote,
        "verdict": {
            "action": action or "Watch",
            "freshness": decision_row.get("freshness_status") or decision_row.get("overall_decision_freshness") or "not_loaded",
            "confidence": _confidence(decision_row, basis),
            "summary": _brief_summary(symbol, decision_row, basis, blockers),
            "blockers": blockers,
            "blocker_labels": [_readable_gate(blocker) for blocker in blockers],
            "blocker_tasks": _blocker_tasks(blockers, missing_families, tables),
            "next_action": _next_action(decision_row, research_packet, missing_families, blockers),
        },
        "setup": setup,
        "risk_plan": {
            "max_sizing": _max_sizing(liquidity, portfolio_row, blockers, missing_families),
            "max_loss": "Not applicable while decision grade is Reject." if "decision_reject" in blockers else "Not applicable while blockers are active." if blockers else _max_loss(best_option),
            "liquidity_ceiling": _liquidity_ceiling(liquidity),
            "portfolio_overlap": _portfolio_overlap(portfolio_row, tables.get("correlations") or []),
            "invalidation": decision_row.get("invalidation") or snapshot.get("invalidation") or _text_join(research_packet.get("invalidation")) or "No ticker-specific invalidation row is loaded in the current decision tables.",
        },
        "portfolio_fit": {
            "owned": bool(portfolio_row) or bool(_object(snapshot.get("portfolio_impact")).get("owned")),
            "current_exposure": _portfolio_exposure(portfolio_row, snapshot),
            "theme_concentration": _theme_concentration(symbol, tables, snapshot),
            "duplicates_risk": bool(portfolio_row),
        },
        "evidence_for": _evidence_for(tables, technical, sepa, liquidity, best_valuation, earnings_setup, best_option, research_packet),
        "evidence_against": _evidence_against(tables, technical, sepa, earnings_setup, best_valuation, blockers, research_packet, expired_option_rows),
        "unknowns": _unknowns(tables, missing_families),
        "changed_since_last_review": _changed_since_last_review(canonical_quote, decision_row, technical, earnings_setup, blockers, tables),
        "source_health_by_family": _source_health_by_family(tables),
        "chart_context": _chart_context(latest_price, technical, sepa),
        "options_context": _options_context(best_option, option_rows, setup),
        "tab_summaries": _ticker_tab_summaries(tables, setup),
    }


def _canonical_quote(symbol: str, quote: dict[str, Any], decision: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    if quote:
        source = _text(quote.get("source")) or "quote"
        quote_type = "prior_close" if source.startswith("previous_close") or source.startswith("closing") else "market_quote"
        return {
            "symbol": symbol,
            "price": _number(quote.get("price") or quote.get("close") or quote.get("last")),
            "change_pct": _optional_change_pct(quote.get("change_pct") or quote.get("percent_change") or quote.get("change")),
            "observed_at": quote.get("observed_at") or quote.get("as_of") or quote.get("date"),
            "source": source,
            "type": quote_type,
            "label": "Prior close" if quote_type == "prior_close" else "Market quote",
        }
    price = _number(decision.get("latest_quote") or snapshot.get("latest_quote"))
    return {
        "symbol": symbol,
        "price": price,
        "change_pct": None,
        "observed_at": decision.get("latest_quote_at") or snapshot.get("latest_quote_at") or decision.get("as_of"),
        "source": "decision_snapshot",
        "type": "decision_snapshot_quote",
        "label": "Decision snapshot quote",
    }


def _optional_change_pct(value: Any) -> float | None:
    """Coerce a quote change to a number, but keep a genuinely missing one None.

    Mirrors the non-canonical ``build_quote`` path: a missing change must not be
    rendered as a real-looking 0.00%.
    """

    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value.replace("$", "").replace(",", "").replace("%", ""))
        except ValueError:
            return None
    return None


def _theme_concentration(symbol: str, tables: dict[str, list[dict[str, Any]]], snapshot: dict[str, Any]) -> str:
    """Describe sector/theme exposure derived from the ticker's own rows.

    Prefer the sector/category carried by the universe, candidate, or fundamentals
    rows; fall back to a neutral generic string when no sector is classified. Never
    name a specific sector that the data does not support.
    """

    universe = _first_row(tables, "universe_screen", "discovered_universe")
    candidate = _first_row(tables, "candidates", "signals")
    fundamentals = _first_row(tables, "fundamentals")
    sector = _text(
        universe.get("sector")
        or fundamentals.get("sector")
        or candidate.get("sector")
        or candidate.get("category")
        or _object(snapshot.get("identity")).get("sector")
    ).strip()
    if sector:
        return f"{sector} sector exposure; compare against current holdings and peers in the same sector."
    return "Sector exposure not classified for this ticker."


def _is_no_trade_action(action: Any) -> bool:
    normalized = _text(action).lower()
    return any(term in normalized for term in ("reject", "avoid", "pass", "no trade"))


GATE_LABELS = {
    "chart_extended_without_thesis": "Price is extended and no current thesis supports chasing.",
    "decision_reject": "Current decision grade is Reject; do not add exposure until the score and setup change.",
    "expired_options_context": "Options context is expired; refresh the chain before using options for risk.",
    "source_thin": "Evidence is source-thin.",
    "evidence_thin": "Primary evidence count is below the decision threshold.",
    "stale_data": "Some source data is stale.",
    "stale_intraday_quote": "Intraday quote is stale; refresh quotes before making a decision.",
    "stale_quote": "Quote is stale; refresh quotes before making a decision.",
    "missing_intraday_quote": "No current intraday quote row is loaded for this ticker.",
    "missing_daily_analysis": "Daily analysis rows are not loaded for this ticker.",
    "liquidity_unknown": "No current liquidity row is loaded for this ticker.",
    "missing_thesis": "No current ticker thesis is loaded.",
    "missing_portfolio_context": "Portfolio context is not loaded for this ticker.",
}


def _readable_gate(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return ""
    return GATE_LABELS.get(raw, raw.replace("_", " ").replace("-", " ").capitalize())


def _gate_sentence(blockers: list[str]) -> str:
    labels = [_readable_gate(blocker).rstrip(".") for blocker in blockers if _readable_gate(blocker)]
    return "; ".join(labels)


def _blocker_tasks(blockers: list[str], missing: list[str], tables: dict[str, list[dict[str, Any]]]) -> list[dict[str, str]]:
    tasks: list[dict[str, str]] = []

    def add(key: str, label: str, action: str, detail: str, severity: str = "warn") -> None:
        if not any(task["key"] == key for task in tasks):
            tasks.append({"key": key, "label": label, "action": action, "detail": detail, "severity": severity})

    for blocker in blockers:
        if blocker == "chart_extended_without_thesis":
            add("thesis", "Thesis support missing", "Add thesis or avoid chase", _readable_gate(blocker), "bad")
        elif blocker == "decision_reject":
            add("decision_reject", "Decision is Reject", "No new exposure", _readable_gate(blocker), "bad")
        elif blocker == "expired_options_context":
            add("options", "Refresh option chain", "Refresh options", _readable_gate(blocker), "bad")
        else:
            add(blocker, _readable_gate(blocker), "Review gate", _text(blocker), "warn")

    missing_actions = {
        "thesis": ("Optional thesis missing", "Add thesis if conviction work continues"),
    }
    for family in missing:
        if family in missing_actions:
            label, action = missing_actions[family]
            add(family.lower().replace(" ", "_"), label, action, f"No {family} row is loaded for this ticker.")

    if tables.get("research_packets") and not tables.get("memos"):
        add("memo", "Decision memo not promoted", "Promote packet to memo", "A research packet exists, but no ticker memo row is loaded.", "info")
    return tasks


def _stance(action: Any, blockers: list[str], valuation: dict[str, Any], packet: dict[str, Any]) -> str:
    normalized = _text(action).lower()
    if _is_no_trade_action(action):
        return "No new exposure; current decision grade is Reject."
    if blockers:
        return "Do not initiate; blocked pending research or setup confirmation."
    if "act" in normalized or "buy" in normalized:
        return "Actionable long candidate after risk and portfolio checks."
    if "reject" in normalized:
        return "Pass for now; monitor only if the setup resets or evidence improves."
    if _number(valuation.get("upside_pct")) < 0:
        return "Monitor/avoid chase; valuation support is negative at the current price."
    decision = _text(packet.get("decision")).lower()
    if decision:
        return f"Research stance from packet: {decision}."
    return "Watchlist candidate; needs stronger source-backed thesis before action."


def _timeframe(earnings_setup: dict[str, Any], option: dict[str, Any]) -> str:
    expiry = option.get("expiry")
    event_date = earnings_setup.get("event_date")
    if expiry:
        return f"Options setup through {expiry}; reassess before expiry."
    if event_date:
        return f"Swing/research window into {event_date} earnings."
    return "Swing/research timeframe; review at the next weekly decision cycle."


def _catalyst(decision: dict[str, Any], snapshot: dict[str, Any], earnings_setup: dict[str, Any], catalysts: list[dict[str, Any]]) -> str:
    catalyst = _text(_object(decision.get("decision_basis")).get("catalyst"))
    if catalyst:
        return catalyst
    if snapshot.get("catalyst_window"):
        return _text(snapshot["catalyst_window"])
    if earnings_setup.get("event_date"):
        return f"{earnings_setup['event_date']}: earnings"
    if catalysts:
        event = catalysts[0]
        return " · ".join(item for item in [_text(event.get("event_date") or event.get("start_at")), _text(event.get("event") or event.get("title"))] if item)
    return "No near-term catalyst row is loaded for this ticker."


def _entry_zone(price: float, technical: dict[str, Any], sepa: dict[str, Any], valuation: dict[str, Any], blockers: list[str]) -> str:
    ma20 = _number(technical.get("ma20") or _object(technical.get("features")).get("ma20"))
    ma50 = _number(technical.get("ma50") or _object(technical.get("features")).get("ma50"))
    fair = _number(valuation.get("fair_value"))
    if "decision_reject" in blockers:
        return "No entry while the decision grade is Reject."
    if blockers:
        return "No chase entry while blockers are active."
    if ma20 and price > ma20 * 1.1:
        return f"Prefer pullback toward 20d MA near {_fmt_money(ma20)} before sizing."
    if fair and price > fair:
        return f"Do not pay above fair-value support near {_fmt_money(fair)} without a fresh thesis."
    if ma50:
        return f"Initial entry zone above rising 50d MA near {_fmt_money(ma50)}."
    return _text(sepa.get("verdict")) or "Entry zone not defined by current source rows."


def _invalidation_level(technical: dict[str, Any], sepa: dict[str, Any]) -> str:
    ma50 = _number(technical.get("ma50") or _object(technical.get("features")).get("ma50"))
    ma200 = _number(technical.get("ma200") or _object(technical.get("features")).get("ma200"))
    if ma50:
        return f"Close below 50d MA near {_fmt_money(ma50)} or SEPA stage deterioration."
    if ma200:
        return f"Close below 200d MA near {_fmt_money(ma200)}."
    return _text(sepa.get("stage")) or "No technical invalidation level loaded."


def _target_range(valuation: dict[str, Any], price: float, blockers: list[str]) -> str:
    fair = _number(valuation.get("fair_value"))
    if fair:
        implied = ((fair / price) - 1) * 100 if price else _number(valuation.get("upside_pct"))
        if "decision_reject" in blockers:
            return f"Model fair value is {_fmt_money(fair)} ({_fmt_pct(implied)}), but the decision grade is Reject; no active target."
        if price and fair < price:
            return f"{_fmt_money(fair)} fair value ({_fmt_pct(implied)} vs canonical quote); no upside at current price."
        return f"{_fmt_money(fair)} fair value ({_fmt_pct(implied)} vs canonical quote)." if price else _fmt_money(fair)
    return "No valuation target range loaded."


def _risk_reward(price: float, technical: dict[str, Any], valuation: dict[str, Any]) -> str:
    fair = _number(valuation.get("fair_value"))
    stop = _number(technical.get("ma50") or _object(technical.get("features")).get("ma50"))
    if price and fair and stop and price != stop:
        reward = fair - price
        risk = price - stop
        if reward <= 0:
            return "No long setup: fair value is below canonical quote."
        if risk > 0:
            return f"{reward / risk:.2f}:1 using fair value vs 50d MA."
    return "Not computable from current target/stop rows."


def _review_date(earnings_setup: dict[str, Any], packet: dict[str, Any]) -> str:
    entry_plan = _object(packet.get("entry_plan"))
    if entry_plan.get("first_review_date"):
        return _text(entry_plan["first_review_date"])
    if earnings_setup.get("event_date"):
        return f"Before {earnings_setup['event_date']} earnings."
    return "Next weekly review."


def _confidence(decision: dict[str, Any], basis: dict[str, Any]) -> int:
    score = _number(decision.get("action_score") or basis.get("action_score") or decision.get("decision_score") or basis.get("decision_score"))
    if score:
        return max(0, min(100, round(score)))
    source_count = _number(basis.get("independent_source_count") or basis.get("source_count"))
    evidence = _number(basis.get("evidence_count"))
    return max(0, min(100, round(30 + min(source_count, 10) * 4 + min(evidence, 5) * 6)))


def _brief_summary(symbol: str, decision: dict[str, Any], basis: dict[str, Any], blockers: list[str]) -> str:
    if blockers:
        return f"{symbol} is gated: {_gate_sentence(blockers)}."
    summary = _text(basis.get("summary") or decision.get("decision_basis"))
    if summary:
        return summary
    source_count = _number(basis.get("source_count") or basis.get("independent_source_count"))
    evidence_count = _number(basis.get("evidence_count") or basis.get("primary_evidence_count"))
    if source_count or evidence_count:
        return f"{symbol} has {source_count:.0f} source rows and {evidence_count:.0f} primary evidence items; no promoted memo summary is loaded."
    return f"{symbol} has no current decision summary row in the loaded ticker tables."


def _next_action(decision: dict[str, Any], packet: dict[str, Any], missing: list[str], blockers: list[str]) -> str:
    if "decision_reject" in blockers:
        entry_plan = _object(packet.get("entry_plan"))
        return _text(entry_plan.get("ideal_entry")) or "Wait for the score, setup, or primary-source catalyst to improve before reconsidering."
    if blockers:
        if any("thesis" in blocker.lower() for blocker in blockers):
            return "Avoid chasing the extended chart unless an explicit thesis supports the trade."
        return f"Load or refresh gated source rows before action: {_gate_sentence(blockers)}."
    entry_plan = _object(packet.get("entry_plan"))
    if entry_plan.get("ideal_entry"):
        return _text(entry_plan["ideal_entry"])
    if missing:
        return f"Load ticker rows before action: {', '.join(missing[:3])}."
    return "Review entry, invalidation, sizing, and portfolio overlap against the rows shown in this ticker dossier."


def _max_sizing(liquidity: dict[str, Any], portfolio: dict[str, Any], blockers: list[str], missing: list[str]) -> str:
    if "decision_reject" in blockers:
        return "No new exposure while decision grade remains Reject."
    if blockers:
        return "No new exposure until evidence gates clear."
    grade = _text(liquidity.get("grade")).lower()
    if portfolio:
        return "Add only if portfolio concentration remains within existing risk limits."
    if "very_high" in grade or "very high" in grade:
        return "Liquid enough for normal discretionary sizing, subject to thesis and portfolio caps."
    if "high" in grade:
        return "Size modestly; verify spread and slippage before entry."
    return "Small only; liquidity row does not support full-size exposure."


def _liquidity_ceiling(liquidity: dict[str, Any]) -> str:
    adv = _number(liquidity.get("avg_dollar_volume"))
    impact = _number(liquidity.get("impact_1pct_adv_bps"))
    if adv:
        return f"{_fmt_money(adv)} ADV; 1% ADV modeled impact {impact:.1f} bps." if impact else f"{_fmt_money(adv)} ADV."
    return "No liquidity ceiling row loaded."


def _portfolio_overlap(portfolio: dict[str, Any], correlations: list[dict[str, Any]]) -> str:
    if portfolio:
        return "Already owned; treat as add/trim decision."
    peers = [_text(row.get("peer_symbol") or row.get("benchmark") or row.get("related_symbol")) for row in correlations[:3]]
    peers = [peer for peer in peers if peer]
    return f"Unowned; compare correlation against {', '.join(peers)}." if peers else "Unowned; correlation peer rows are limited."


def _portfolio_exposure(portfolio: dict[str, Any], snapshot: dict[str, Any]) -> str:
    if portfolio:
        weight = _number(portfolio.get("weight") or portfolio.get("portfolio_weight"))
        value = _number(portfolio.get("market_value") or portfolio.get("value"))
        if weight:
            return f"{weight:.2f}% current weight."
        if value:
            return f"{_fmt_money(value)} current market value."
        return "Owned."
    impact = _object(snapshot.get("portfolio_impact"))
    return "Owned." if impact.get("owned") else "Unowned."


def _evidence_for(
    tables: dict[str, list[dict[str, Any]]],
    technical: dict[str, Any],
    sepa: dict[str, Any],
    liquidity: dict[str, Any],
    valuation: dict[str, Any],
    earnings: dict[str, Any],
    option: dict[str, Any],
    packet: dict[str, Any],
) -> list[str]:
    items: list[str] = []
    score = _number(technical.get("technical_score") or _object(technical.get("features")).get("technical_score"))
    if score >= 60:
        items.append(f"Technical score is {score:.0f}; 20d return {_fmt_pct(_number(technical.get('return_20d')) * 100)}.")
    sepa_text = _text(sepa.get("verdict") or sepa.get("stage")).lower()
    if sepa and any(term in sepa_text for term in ("strong", "constructive", "stage_2", "advance")):
        items.append(f"SEPA setup is {_text(sepa.get('verdict') or sepa.get('stage'))}.")
    if _text(liquidity.get("grade")):
        items.append(f"Liquidity is {_text(liquidity.get('grade')).replace('_', ' ')} with {_liquidity_ceiling(liquidity)}")
    if _number(valuation.get("upside_pct")) > 0:
        items.append(f"Best valuation row shows {_fmt_pct(_number(valuation.get('upside_pct')))} modeled upside.")
    earnings_score = _number(earnings.get("score"))
    earnings_verdict = _text(earnings.get("verdict")).lower()
    if earnings and (earnings_score >= 60 or "positive" in earnings_verdict):
        items.append(f"Earnings setup is {_text(earnings.get('verdict')) or 'loaded'} with score {_number(earnings.get('score')):.0f}.")
    if option:
        items.append(f"Options scenario loaded: {_text(option.get('strategy_type')).replace('_', ' ')}.")
    for note in _text_list(packet.get("why_now"))[:2]:
        lowered = note.lower()
        if any(term in lowered for term in ("not yet strong", "not strong", "insufficient", "weak evidence")):
            continue
        items.append(note)
    return items or ["No positive source-backed evidence rows are loaded."]


def _evidence_against(
    tables: dict[str, list[dict[str, Any]]],
    technical: dict[str, Any],
    sepa: dict[str, Any],
    earnings: dict[str, Any],
    valuation: dict[str, Any],
    blockers: list[str],
    packet: dict[str, Any],
    expired_options: list[dict[str, Any]],
) -> list[str]:
    items = [_readable_gate(blocker) for blocker in blockers]
    score = _number(technical.get("technical_score") or _object(technical.get("features")).get("technical_score"))
    if technical and score < 50:
        items.append(f"Technical score is weak at {score:.0f}; 20d return {_fmt_pct(_number(technical.get('return_20d')) * 100)}.")
    sepa_text = _text(sepa.get("verdict") or sepa.get("stage"))
    if sepa and any(term in sepa_text.lower() for term in ("pass", "risk", "declin", "stage_4")):
        items.append(f"SEPA setup is {sepa_text}; do not treat the chart as constructive.")
    earnings_score = _number(earnings.get("score"))
    earnings_verdict = _text(earnings.get("verdict"))
    if earnings and (earnings_score < 50 or "risk" in earnings_verdict.lower()):
        items.append(f"Earnings setup is {earnings_verdict or 'risk'} with score {earnings_score:.0f}.")
    upside = _number(valuation.get("upside_pct"))
    if valuation and upside < 0:
        items.append(f"Best valuation row is below price: {_fmt_pct(upside)} upside.")
    if expired_options:
        expiries = sorted({_text(row.get("expiry") or row.get("expiration")) for row in expired_options if _text(row.get("expiry") or row.get("expiration"))})
        expiry_text = ", ".join(expiries[:3])
        items.append(f"Options scenarios are expired{f' ({expiry_text})' if expiry_text else ''}; do not use them for live max loss, breakeven, or trade setup.")
    drawdown = _number(technical.get("drawdown_from_high") or _object(technical.get("features")).get("drawdown_from_high"))
    if drawdown > -0.1 and technical:
        items.append("Price is near recent highs; avoid chasing an extended move without a thesis.")
    items.extend(_text_list(packet.get("bear_case"))[:2])
    if not tables.get("theses") and not tables.get("memos"):
        items.append("No ticker-specific thesis or memo row is loaded in the current local research tables.")
    return items or ["Loaded rows do not contain a negative evidence item for this ticker."]


def _unknowns(tables: dict[str, list[dict[str, Any]]], missing: list[str]) -> list[str]:
    unknowns = []
    for family in missing:
        if family == "thesis":
            unknowns.append("Optional thesis is not loaded; rely on source evidence and deterministic analysis until conviction work is added.")
        elif family == "news":
            unknowns.append("No ticker-specific news row is loaded in the current local news table.")
        elif family == "filings":
            unknowns.append("No tracked disclosure row is loaded for this ticker in the current local filing set.")
        else:
            unknowns.append(f"No current {family} row is loaded for this ticker.")
    if not tables.get("news"):
        unknowns.append("No ticker-specific news row is loaded in the current local news table.")
    return list(dict.fromkeys(unknowns)) or ["Loaded ticker tables cover the required quote, setup, risk, and evidence fields for this dossier."]


def _changed_since_last_review(
    quote: dict[str, Any],
    decision: dict[str, Any],
    technical: dict[str, Any],
    earnings: dict[str, Any],
    blockers: list[str],
    tables: dict[str, list[dict[str, Any]]],
) -> list[str]:
    changes = []
    if quote.get("change_pct") is not None:
        changes.append(f"{quote.get('label')}: {_fmt_money(_number(quote.get('price')))} ({_fmt_pct(_number(quote.get('change_pct')))}).")
    if decision.get("as_of"):
        changes.append(f"Decision row refreshed {decision['as_of']} with action {decision.get('action_grade') or 'not recorded'}.")
    if technical:
        changes.append(f"Technical row shows 20d return {_fmt_pct(_number(technical.get('return_20d')) * 100)}.")
    if earnings:
        changes.append(f"Earnings setup score {_number(earnings.get('score')):.0f}; next event {earnings.get('event_date') or 'not loaded'}.")
    if blockers:
        changes.append(f"Active blocker set: {_gate_sentence(blockers)}.")
    loaded = sum(1 for rows in tables.values() if rows)
    changes.append(f"{loaded} ticker-specific API table families currently loaded.")
    return changes


def _source_health_by_family(tables: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    families = {
        "quote": ["quotes"],
        "technical": ["technicals", "sepa"],
        "valuation": ["valuations"],
        "earnings": ["earnings", "earnings_setups", "analyst_estimates"],
        "options": ["options_expiries", "options_chain", "options_payoff_scenarios", "options_expiry_signals", "options_ticker_signals"],
        "thesis": ["theses", "memos"],
        "research_packet": ["research_packets"],
        "news": ["news"],
        "filings": ["disclosures"],
        "portfolio": ["portfolio"],
        "tradingview": ["tradingview_symbol_search", "tradingview_watchlists", "tradingview_alerts", "tradingview_chart_state"],
    }
    health = {}
    for family, keys in families.items():
        row_count = sum(len(tables.get(key) or []) for key in keys)
        status = "live" if row_count else "missing"
        if family == "options" and row_count:
            option_rows = tables.get("options_payoff_scenarios") or []
            if option_rows and all(_is_option_expired(row) for row in option_rows):
                status = "expired"
        health[family] = {"status": status, "rows": row_count}
    return health


def _chart_context(price: float, technical: dict[str, Any], sepa: dict[str, Any]) -> dict[str, Any]:
    features = _object(technical.get("features"))
    ma20 = _number(technical.get("ma20") or features.get("ma20"))
    ma50 = _number(technical.get("ma50") or features.get("ma50"))
    ma200 = _number(technical.get("ma200") or features.get("ma200"))
    high = _number(_object(sepa.get("metrics")).get("high_52w"))
    low = _number(_object(sepa.get("metrics")).get("low_52w"))
    extension = (price / ma20 - 1) * 100 if price and ma20 else 0
    return {
        "ma20": ma20,
        "ma50": ma50,
        "ma200": ma200,
        "drawdown_from_high": _number(technical.get("drawdown_from_high") or features.get("drawdown_from_high")),
        "return_20d": _number(technical.get("return_20d") or features.get("return_20d")),
        "return_60d": _number(technical.get("return_60d") or features.get("return_60d")),
        "high_52w": high,
        "low_52w": low,
        "extension_warning": f"{extension:.1f}% above 20d MA" if extension > 10 else "",
        "support": ma50 or ma20,
        "resistance": high,
    }
