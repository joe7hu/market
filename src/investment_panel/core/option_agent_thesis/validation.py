"""Auto-split from raw_sources.py — see ARCHITECTURE.md."""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any
from investment_panel.core.db import json_dumps, query_rows
from investment_panel.core.source_ingestion.utils import stable_id

from investment_panel.core.option_agent_thesis.coerce import _catalyst_list, _content_tokens, _date_value, _invalidation_price, _iso_or_none, _list_value, _metric_number, _number, _string_list
from investment_panel.core.option_agent_thesis.dbutil import _json, decode_json_fields, first_row, query_decoded


def refresh_agent_thesis_validations(con: Any, *, strategy_version: str) -> int:
    rows = query_rows(
        con,
        """
        SELECT *
        FROM agent_thesis
        QUALIFY row_number() OVER (PARTITION BY ticker ORDER BY created_at DESC) = 1
        """,
    )
    count = 0
    for thesis in rows:
        thesis = decode_json_fields(thesis, ("required_proofs", "invalidation_conditions", "catalysts", "evidence_refs", "raw"))
        candidate = first_row(
            con,
            """
            SELECT *
            FROM candidate_event
            WHERE ticker = ? AND strategy_version = ?
            ORDER BY snapshot_time DESC, score DESC
            LIMIT 1
            """,
            [thesis["ticker"], strategy_version],
            ("raw",),
        )
        stock = first_row(
            con,
            "SELECT * FROM stock_features WHERE ticker = ? ORDER BY snapshot_time DESC LIMIT 1",
            [thesis["ticker"]],
            ("raw",),
        )
        source_signals = query_decoded(
            con,
            """
            SELECT source_item_id, source_id, observed_at, signal_type, sentiment,
                   direction, confidence, thesis, antithesis, catalysts, risks,
                   invalidation, evidence_refs
            FROM ticker_source_signals
            WHERE symbol = ?
            ORDER BY observed_at DESC, confidence DESC NULLS LAST
            LIMIT 12
            """,
            [thesis["ticker"]],
            ("catalysts", "risks", "evidence_refs"),
        )
        dated_catalysts = query_decoded(
            con,
            """
            SELECT id, event_date, event, expected_impact, source,
                   verification_status, source_url, raw
            FROM catalysts
            WHERE symbol = ?
            ORDER BY event_date ASC NULLS LAST
            LIMIT 8
            """,
            [thesis["ticker"]],
            ("raw",),
        )
        news = query_decoded(
            con,
            """
            SELECT id, published_at, provider, title, related_symbols, link, source
            FROM news_items
            WHERE contains(CAST(related_symbols AS VARCHAR), ?)
            ORDER BY published_at DESC
            LIMIT 8
            """,
            [thesis["ticker"]],
            ("related_symbols",),
        )
        fundamentals = first_row(
            con,
            """
            SELECT symbol, period_end, filing_date, form_type, metrics, source_url
            FROM equity_fundamentals
            WHERE symbol = ?
            ORDER BY filing_date DESC NULLS LAST, period_end DESC NULLS LAST
            LIMIT 1
            """,
            [thesis["ticker"]],
            ("metrics",),
        )
        validation = build_agent_thesis_validation(
            thesis,
            candidate,
            stock,
            source_signals,
            dated_catalysts,
            news,
            fundamentals,
            strategy_version=strategy_version,
        )
        con.execute(
            """
            INSERT OR REPLACE INTO agent_thesis_validation
            (validation_id, thesis_id, ticker, strategy_version, validation_date,
             candidate_event_id, candidate_snapshot_time, validated_at, state, reason,
             option_still_valid, stock_progress, iv_status, candidate_state,
             proof_status, catalyst_status, invalidation_status, evidence_status,
             red_team_status, red_team_flags, evidence_refs, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                validation["validation_id"],
                validation["thesis_id"],
                validation["ticker"],
                validation["strategy_version"],
                validation["validation_date"],
                validation["candidate_event_id"],
                validation["candidate_snapshot_time"],
                validation["validated_at"],
                validation["state"],
                validation["reason"],
                validation["option_still_valid"],
                validation["stock_progress"],
                validation["iv_status"],
                validation["candidate_state"],
                validation["proof_status"],
                validation["catalyst_status"],
                validation["invalidation_status"],
                validation["evidence_status"],
                validation["red_team_status"],
                json_dumps(validation["red_team_flags"]),
                json_dumps(validation["evidence_refs"]),
                json_dumps(validation["raw"]),
            ],
        )
        count += 1
    return count


def build_agent_thesis_validation(
    thesis: dict[str, Any],
    candidate: dict[str, Any] | None,
    stock: dict[str, Any] | None,
    source_signals: list[dict[str, Any]] | None = None,
    dated_catalysts: list[dict[str, Any]] | None = None,
    news: list[dict[str, Any]] | None = None,
    fundamentals: dict[str, Any] | None = None,
    *,
    strategy_version: str = "unknown",
) -> dict[str, Any]:
    ticker = str(thesis.get("ticker") or "").upper()
    raw_candidate = _json(candidate.get("raw")) if candidate else {}
    blockers = [str(item) for item in raw_candidate.get("blockers") or []]
    hard_rejects = [str(item) for item in raw_candidate.get("hard_rejects") or []]
    candidate_state = str((candidate or {}).get("state") or "missing_candidate")
    price = _number((stock or {}).get("price"))
    base_target = _number(thesis.get("base_target_price"))
    invalidation = _string_list(thesis.get("invalidation_conditions"))
    invalidation_price = _invalidation_price(invalidation)
    option_still_valid = candidate_state in {"FIRE", "SETUP", "WATCH"} and not hard_rejects
    iv_status = "overpriced" if any("iv" in item for item in [*blockers, *hard_rejects]) else "acceptable_or_unknown"
    candidate_event_id = (candidate or {}).get("event_id")
    candidate_snapshot_time = _iso_or_none((candidate or {}).get("snapshot_time"))
    as_of_date = _date_value(candidate_snapshot_time) or _date_value((stock or {}).get("snapshot_time")) or date.today()
    proof_check = _proof_check(_string_list(thesis.get("required_proofs")), source_signals or [], news or [])
    catalyst_check = _catalyst_check(_catalyst_list(thesis.get("catalysts")), dated_catalysts or [], source_signals or [], news or [], as_of_date)
    evidence_status = _evidence_status(_list_value(thesis.get("evidence_refs")), source_signals or [], news or [])
    red_team_check = _red_team_check(thesis, candidate, stock, source_signals or [], news or [], fundamentals)
    invalidation_status = "missing"
    if price is not None and invalidation_price is not None and price <= invalidation_price:
        state = "invalidated"
        reason = "Latest price is through the agent thesis invalidation level."
        stock_progress = "invalidation_breached"
        invalidation_status = "breached"
    elif candidate_state == "REJECT" or hard_rejects:
        state = "weakening"
        reason = f"Latest deterministic candidate state is {candidate_state}."
        stock_progress = "candidate_rejected"
        invalidation_status = "clear" if invalidation_price is not None else "missing"
    elif price is not None and base_target is not None and price >= base_target:
        state = "validated"
        reason = "Latest price is at or above the agent base target."
        stock_progress = "base_target_reached"
        invalidation_status = "clear" if invalidation_price is not None else "missing"
    elif option_still_valid:
        state = "tracking"
        reason = "Thesis is tracking; deterministic option gates have not validated or invalidated it yet."
        stock_progress = "tracking"
        invalidation_status = "clear" if invalidation_price is not None else "missing"
    else:
        state = "weakening"
        reason = "Thesis lacks a current valid option candidate."
        stock_progress = "option_context_missing"
        invalidation_status = "clear" if invalidation_price is not None else "missing"
    if proof_check["status"] == "missing":
        reason = f"{reason} Required proof list is missing."
    elif proof_check["status"] == "pending" and state == "validated":
        state = "pending"
        reason = "Price reached the base target, but required proofs are not source-backed yet."
    evidence_refs = _list_value(thesis.get("evidence_refs"))
    if candidate:
        evidence_refs.append({"type": "candidate_event", "id": candidate.get("event_id")})
    for signal in (source_signals or [])[:3]:
        if signal.get("source_item_id"):
            evidence_refs.append({"type": "ticker_source_signal", "id": signal.get("source_item_id")})
    for catalyst in (dated_catalysts or [])[:2]:
        if catalyst.get("id"):
            evidence_refs.append({"type": "catalyst", "id": catalyst.get("id")})
    return {
        "validation_id": stable_id(
            "agent_thesis_validation",
            thesis.get("thesis_id"),
            strategy_version,
            candidate_event_id,
            as_of_date.isoformat(),
        ),
        "thesis_id": thesis.get("thesis_id"),
        "ticker": ticker,
        "strategy_version": strategy_version,
        "validation_date": as_of_date.isoformat(),
        "candidate_event_id": candidate_event_id,
        "candidate_snapshot_time": candidate_snapshot_time,
        "validated_at": datetime.utcnow().isoformat(),
        "state": state,
        "reason": reason,
        "option_still_valid": option_still_valid,
        "stock_progress": stock_progress,
        "iv_status": iv_status,
        "candidate_state": candidate_state,
        "proof_status": proof_check["status"],
        "catalyst_status": catalyst_check["status"],
        "invalidation_status": invalidation_status,
        "evidence_status": evidence_status,
        "red_team_status": red_team_check["status"],
        "red_team_flags": red_team_check["flags"],
        "evidence_refs": evidence_refs,
        "raw": {
            "price": price,
            "base_target_price": base_target,
            "invalidation_price": invalidation_price,
            "as_of_date": as_of_date.isoformat(),
            "strategy_version": strategy_version,
            "candidate_event_id": candidate_event_id,
            "candidate_snapshot_time": candidate_snapshot_time,
            "proof_check": proof_check,
            "catalyst_check": catalyst_check,
            "red_team_check": red_team_check,
            "evidence_status": evidence_status,
            "blockers": blockers,
            "hard_rejects": hard_rejects,
            "authority": "deterministic_validation_only",
        },
    }


def _proof_check(required_proofs: list[str], source_signals: list[dict[str, Any]], news: list[dict[str, Any]]) -> dict[str, Any]:
    if not required_proofs:
        return {"status": "missing", "matched": [], "missing": [], "match_count": 0, "required_count": 0}
    corpus = _evidence_corpus(source_signals, news)
    matched: list[str] = []
    missing: list[str] = []
    for proof in required_proofs:
        tokens = _content_tokens(proof)
        if tokens and any(token in corpus for token in tokens):
            matched.append(proof)
        else:
            missing.append(proof)
    if len(matched) == len(required_proofs):
        status = "supported"
    elif matched:
        status = "partial"
    else:
        status = "pending"
    return {
        "status": status,
        "matched": matched,
        "missing": missing,
        "match_count": len(matched),
        "required_count": len(required_proofs),
    }


def _catalyst_check(
    thesis_catalysts: list[dict[str, Any]],
    dated_catalysts: list[dict[str, Any]],
    source_signals: list[dict[str, Any]],
    news: list[dict[str, Any]],
    as_of_date: date,
) -> dict[str, Any]:
    if not thesis_catalysts:
        return {"status": "missing", "matched": [], "scheduled": []}
    scheduled = [
        catalyst
        for catalyst in dated_catalysts
        if (event_date := _date_value(catalyst.get("event_date"))) is not None and event_date >= as_of_date
    ]
    if scheduled:
        return {
            "status": "scheduled",
            "matched": [str(item.get("event") or item.get("event_kind") or item.get("id")) for item in scheduled[:3]],
            "scheduled": [str(item.get("event_date")) for item in scheduled[:3]],
        }
    corpus = _evidence_corpus(source_signals, news)
    matched: list[str] = []
    for catalyst in thesis_catalysts:
        text = " ".join(str(catalyst.get(key) or "") for key in ("type", "expected_window", "what_to_watch", "summary", "description"))
        tokens = _content_tokens(text)
        if tokens and any(token in corpus for token in tokens):
            matched.append(text.strip())
    if matched:
        return {"status": "source_confirmed", "matched": matched[:3], "scheduled": []}
    return {"status": "pending", "matched": [], "scheduled": []}


def _evidence_status(evidence_refs: list[Any], source_signals: list[dict[str, Any]], news: list[dict[str, Any]]) -> str:
    if evidence_refs and source_signals:
        return "source_backed"
    if evidence_refs:
        return "agent_cited"
    if source_signals:
        return "source_context_available"
    if news:
        return "news_only"
    return "missing"


def _red_team_check(
    thesis: dict[str, Any],
    candidate: dict[str, Any] | None,
    stock: dict[str, Any] | None,
    source_signals: list[dict[str, Any]],
    news: list[dict[str, Any]],
    fundamentals: dict[str, Any] | None,
) -> dict[str, Any]:
    bear_case = str(thesis.get("bear_case") or "").strip()
    if not bear_case:
        return {"status": "missing", "flags": [], "source_matches": [], "hard_checks": [], "bear_case": ""}

    raw_candidate = _json(candidate.get("raw")) if candidate else {}
    blockers = [str(item) for item in raw_candidate.get("blockers") or []]
    hard_rejects = [str(item) for item in raw_candidate.get("hard_rejects") or []]
    candidate_reasons = [*blockers, *hard_rejects]
    stock_raw = _json((stock or {}).get("raw"))
    metrics = _json((fundamentals or {}).get("metrics"))
    flags: list[dict[str, Any]] = []

    for reason in candidate_reasons:
        reason_text = reason.lower()
        if any(token in reason_text for token in ("spread", "open_interest", "volume", "liquidity")):
            flags.append({"type": "option_liquidity_risk", "evidence": reason})
        if "iv" in reason_text:
            flags.append({"type": "iv_overpricing_risk", "evidence": reason})
        if "stock_below_50d" in reason_text or "rs_vs_qqq_20d_negative" in reason_text:
            flags.append({"type": "technical_downtrend_risk", "evidence": reason})

    price = _number((stock or {}).get("price"))
    ma_50 = _number((stock or {}).get("ma_50"))
    ma_200 = _number((stock or {}).get("ma_200"))
    rs_20 = _number((stock or {}).get("rs_vs_qqq_20d"))
    if price is not None and ma_50 is not None and price < ma_50:
        flags.append({"type": "technical_downtrend_risk", "evidence": "price_below_50d"})
    if price is not None and ma_200 is not None and price < ma_200:
        flags.append({"type": "long_term_downtrend_risk", "evidence": "price_below_200d"})
    if rs_20 is not None and rs_20 < 0:
        flags.append({"type": "relative_strength_risk", "evidence": "rs_vs_qqq_20d_negative"})

    free_cash_flow = _metric_number(metrics, "free_cash_flow", "freeCashflow", "free_cashflow")
    operating_cash_flow = _metric_number(metrics, "operating_cash_flow", "operatingCashflow", "totalCashFromOperatingActivities")
    cash = _metric_number(metrics, "cash", "total_cash", "totalCash")
    debt = _metric_number(metrics, "total_debt", "totalDebt", "debt")
    liabilities = _metric_number(metrics, "liabilities", "total_liabilities", "totalLiabilities")
    assets = _metric_number(metrics, "assets", "total_assets", "totalAssets")
    revenue_growth = _metric_number(metrics, "revenue_growth", "revenueGrowth", "revenue_growth_yoy")
    if free_cash_flow is not None and free_cash_flow < 0:
        flags.append({"type": "cash_burn_risk", "evidence": "negative_free_cash_flow"})
    elif operating_cash_flow is not None and operating_cash_flow < 0:
        flags.append({"type": "cash_burn_risk", "evidence": "negative_operating_cash_flow"})
    if cash is not None and free_cash_flow is not None and free_cash_flow < 0 and cash < abs(free_cash_flow):
        flags.append({"type": "cash_runway_risk", "evidence": "cash_less_than_one_year_negative_fcf"})
    if debt is not None and cash is not None and debt > cash * 2:
        flags.append({"type": "balance_sheet_risk", "evidence": "debt_more_than_2x_cash"})
    if liabilities is not None and assets is not None and assets > 0 and liabilities / assets > 0.7:
        flags.append({"type": "balance_sheet_risk", "evidence": "liabilities_above_70pct_assets"})
    if revenue_growth is not None and revenue_growth < 0:
        flags.append({"type": "growth_deceleration_risk", "evidence": "negative_revenue_growth"})

    bear_tokens = _content_tokens(bear_case)
    risk_corpus = _risk_corpus(source_signals, news)
    source_matches = sorted(token for token in bear_tokens if token in risk_corpus)[:12]
    hard_flags = _dedupe_flags(flags)
    if hard_flags:
        status = "hard_risk_triggered"
    elif source_matches:
        status = "source_backed"
    else:
        status = "agent_only"
    return {
        "status": status,
        "flags": hard_flags,
        "source_matches": source_matches,
        "hard_checks": {
            "stock": {
                "price": price,
                "ma_50": ma_50,
                "ma_200": ma_200,
                "rs_vs_qqq_20d": rs_20,
                "raw": stock_raw,
            },
            "fundamentals": {
                "free_cash_flow": free_cash_flow,
                "operating_cash_flow": operating_cash_flow,
                "cash": cash,
                "total_debt": debt,
                "liabilities": liabilities,
                "assets": assets,
                "revenue_growth": revenue_growth,
            },
            "candidate_reasons": candidate_reasons,
        },
        "bear_case": bear_case,
    }


def _risk_corpus(source_signals: list[dict[str, Any]], news: list[dict[str, Any]]) -> set[str]:
    texts: list[str] = []
    for signal in source_signals:
        texts.extend(
            [
                str(signal.get("signal_type") or ""),
                str(signal.get("antithesis") or ""),
                str(signal.get("invalidation") or ""),
                json.dumps(signal.get("risks") or ""),
            ]
        )
    for item in news:
        texts.extend([str(item.get("title") or ""), str(item.get("provider") or ""), str(item.get("source") or "")])
    return {token for text in texts for token in _content_tokens(text)}


def _dedupe_flags(flags: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for flag in flags:
        key = (str(flag.get("type") or ""), str(flag.get("evidence") or ""))
        if key in seen:
            continue
        seen.add(key)
        output.append(flag)
    return output


def _evidence_corpus(source_signals: list[dict[str, Any]], news: list[dict[str, Any]]) -> set[str]:
    texts: list[str] = []
    for signal in source_signals:
        texts.extend(
            [
                str(signal.get("signal_type") or ""),
                str(signal.get("thesis") or ""),
                str(signal.get("antithesis") or ""),
                str(signal.get("invalidation") or ""),
                json.dumps(signal.get("catalysts") or ""),
                json.dumps(signal.get("risks") or ""),
            ]
        )
    for item in news:
        texts.extend([str(item.get("title") or ""), str(item.get("provider") or ""), str(item.get("source") or "")])
    return {token for text in texts for token in _content_tokens(text)}
