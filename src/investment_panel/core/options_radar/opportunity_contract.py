"""Opportunity data-contract, plausibility blockers and trader-facing text."""

from __future__ import annotations

from typing import Any

from investment_panel.core.options_radar.coerce import (_integer, _json, _list_value, _number)
from investment_panel.core.options_radar.constants import (DATA_CONTRACT_READY, DATA_CONTRACT_REPAIR_REQUIRED, SERVICE_BUG_TIER, SERVICE_REPAIR_JOB_ORDER)
from investment_panel.core.options_radar.opportunity_scoring import (_source_backed_thesis_score, _thesis_score)
from investment_panel.core.options_radar.scoring import (_theme_watch_matches)

def _opportunity_data_contract(
    row: dict[str, Any],
    *,
    validation: dict[str, Any],
    source_context: dict[str, Any],
    qqq_above_200d: bool | None,
) -> dict[str, Any]:
    failures: list[str] = []
    satisfied: list[str] = []
    repair_jobs: list[str] = []

    def fail(reason: str, jobs: list[str]) -> None:
        failures.append(reason)
        repair_jobs.extend(jobs)

    def ok(reason: str) -> None:
        satisfied.append(reason)

    option_required = {
        "option_contract_quote": [_number(row.get("premium_mid")), _number(row.get("required_10x_price")), _number(row.get("buy_under"))],
        "option_chain_terms": [_integer(row.get("dte")), _number(row.get("spread_pct"))],
        "option_liquidity": [_number(row.get("open_interest")), _number(row.get("volume"))],
        "option_iv_and_delta": [_number(row.get("iv_percentile")), _number(row.get("delta"))],
    }
    for label, values in option_required.items():
        if any(value is None for value in values):
            fail(f"{label}_sync_gap", ["update_free_sources", "refresh_options_radar"])
        else:
            ok(label)

    if _blocking_quality_flags(row):
        fail("option_data_conflict", ["update_free_sources", "refresh_options_radar"])
    else:
        ok("option_provider_crosscheck")

    stock_required = [_number(row.get("price")), _number(row.get("ma_50")), _number(row.get("rs_vs_qqq_20d"))]
    if any(value is None for value in stock_required):
        fail("stock_context_sync_gap", ["update_free_sources", "refresh_options_radar"])
    else:
        ok("stock_context")

    if qqq_above_200d is None:
        fail("market_regime_sync_gap", ["update_free_sources", "refresh_options_radar"])
    else:
        ok("market_regime_context")

    asset_class = str(row.get("asset_class") or "").lower()
    is_index_like_etf = asset_class == "etf"
    if is_index_like_etf:
        ok("etf_macro_contract")
    elif int(source_context.get("count") or 0) < 2 or float(source_context.get("score") or 0.0) < 45.0:
        fail("source_evidence_sync_gap", ["update_arco_data", "update_free_sources", "refresh_options_radar"])
    else:
        ok("source_evidence_cluster")

    if is_index_like_etf:
        ok("etf_systematic_thesis")
    elif max(_thesis_score(validation, row), _source_backed_thesis_score(source_context)) < 80.0:
        fail("thesis_synthesis_sync_gap", ["run_option_agents", "refresh_options_radar"])
    else:
        ok("source_backed_thesis")

    repair_jobs = [job for job in SERVICE_REPAIR_JOB_ORDER if job in set(repair_jobs)]
    failures = list(dict.fromkeys(failures))
    satisfied = list(dict.fromkeys(satisfied))
    status = DATA_CONTRACT_READY if not failures else DATA_CONTRACT_REPAIR_REQUIRED
    return {
        "status": status,
        "failures": failures,
        "satisfied": satisfied,
        "repair_jobs": repair_jobs,
        "summary": _data_contract_summary(failures, repair_jobs),
    }


def _data_contract_summary(failures: list[str], repair_jobs: list[str]) -> str:
    if not failures:
        return "Data contract clean: option chain, liquidity, stock context, source evidence, and thesis synthesis are loaded."
    labels = ", ".join(failures[:3])
    if len(failures) > 3:
        labels = f"{labels}, +{len(failures) - 3}"
    return f"Service bug: {labels}. Trade state is withheld until the data contract is clean."


def _extreme_opportunity_blockers(
    row: dict[str, Any],
    *,
    validation: dict[str, Any],
    source_context: dict[str, Any],
    qqq_above_200d: bool | None,
    scores: dict[str, float],
) -> list[str]:
    blockers: list[str] = []
    state = str(row.get("state") or "").upper()
    if state != "FIRE":
        blockers.append("wait_for_fire_setup")
    spread = _number(row.get("spread_pct"))
    if spread is not None and spread > 0.18:
        blockers.append("spread_not_exceptional")
    open_interest = _number(row.get("open_interest"))
    if open_interest is not None and open_interest < 250:
        blockers.append("open_interest_not_exceptional")
    volume = _number(row.get("volume"))
    if volume is not None and volume < 1:
        blockers.append("no_printed_volume")
    dte = _integer(row.get("dte"))
    if dte is not None and (dte < 365 or dte > 900):
        blockers.append("leap_survivability_not_exceptional")
    required_move = _number(row.get("required_move_pct"))
    if required_move is not None and required_move > 2.0:
        blockers.append("required_move_not_exceptional")
    if validation.get("invalidation_status") == "breached" or validation.get("state") == "invalidated":
        blockers.append("thesis_invalidated")
    if validation.get("red_team_status") == "hard_risk_triggered":
        blockers.append("hard_red_team_risk")
    if qqq_above_200d is False:
        blockers.append("market_regime_hostile_to_long_premium")
    if scores["entry_quality_score"] < 70.0:
        blockers.append("entry_quality_below_exceptional_bar")
    if scores["asymmetry_score"] < 65.0:
        blockers.append("asymmetry_below_exceptional_bar")
    blockers.extend(_business_plausibility_blockers(row, validation=validation))
    return list(dict.fromkeys(blockers))


def _business_plausibility_blockers(row: dict[str, Any], *, validation: dict[str, Any]) -> list[str]:
    """Keep Exceptional reserved for moves that fit the business context."""

    if validation.get("state") in {"validated", "strengthening"}:
        return []
    required_move = _number(row.get("required_move_pct"))
    if required_move is None:
        return []
    annualized_move = _annualized_required_move(row, required_move)
    sector = _business_context_text(row.get("sector"))
    industry = _business_context_text(row.get("industry"))
    market_cap = _market_cap(row)
    revenue_growth = _revenue_growth(row)

    blockers: list[str] = []
    if _is_bank_or_financial(sector, industry) and annualized_move > 0.30:
        blockers.append("bank_move_implausible_without_validated_catalyst")
    if _is_regulated_healthcare_plan(sector, industry) and annualized_move > 0.45:
        blockers.append("regulated_healthcare_move_implausible_without_validated_catalyst")
    mega_cap_ceiling = _mega_cap_annual_move_ceiling(market_cap, revenue_growth)
    if mega_cap_ceiling is not None and annualized_move > mega_cap_ceiling:
        blockers.append("mega_cap_move_implausible_without_validated_catalyst")
    return blockers


def _annualized_required_move(row: dict[str, Any], required_move: float) -> float:
    dte = _integer(row.get("dte"))
    if dte is None or dte <= 0:
        return required_move
    years = max(float(dte) / 365.0, 0.25)
    return (1.0 + required_move) ** (1.0 / years) - 1.0


def _business_context_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _is_bank_or_financial(sector: str, industry: str) -> bool:
    text = f"{sector} {industry}"
    return "financial" in text or "bank" in text or "capital markets" in text or "insurance" in text


def _is_regulated_healthcare_plan(sector: str, industry: str) -> bool:
    text = f"{sector} {industry}"
    return "healthcare plans" in text or "managed care" in text


def _mega_cap_annual_move_ceiling(market_cap: float | None, revenue_growth: float | None) -> float | None:
    if market_cap is None:
        return None
    if market_cap >= 1_000_000_000_000:
        ceiling = 0.50
    elif market_cap >= 500_000_000_000:
        ceiling = 0.55
    elif market_cap >= 200_000_000_000:
        ceiling = 0.75
    else:
        return None
    if revenue_growth is not None:
        if revenue_growth >= 0.40:
            ceiling += 0.20
        elif revenue_growth >= 0.25:
            ceiling += 0.10
    return ceiling


def _opportunity_top_reasons(
    row: dict[str, Any],
    *,
    validation: dict[str, Any],
    source_context: dict[str, Any],
    qqq_above_200d: bool | None,
    scores: dict[str, float],
) -> list[str]:
    reasons: list[str] = []
    reasons.extend(_theme_watch_matches(row))
    if scores["asymmetry_score"] >= 70:
        reasons.append("convexity_inside_extreme_bar")
    if scores["entry_quality_score"] >= 70:
        reasons.append("entry_quality_supported")
    if _thesis_score(validation, row) >= 80:
        reasons.append("thesis_validated")
    elif _source_backed_thesis_score(source_context) >= 80:
        reasons.append("source_backed_thesis")
    if int(source_context.get("count") or 0) >= 2:
        reasons.append("source_evidence_cluster")
    if qqq_above_200d is True:
        reasons.append("supportive_market_regime")
    if scores["survivability_score"] >= 70:
        reasons.append("leap_survivability_supported")
    if not reasons:
        raw = _json(row.get("raw"))
        positives = raw.get("positives") if isinstance(raw.get("positives"), list) else []
        reasons.extend([str(item) for item in positives[:3]])
    return list(dict.fromkeys(reasons))[:5]


def _blocking_quality_flags(row: dict[str, Any]) -> list[str]:
    quality = str(row.get("quality_status") or "ok").lower()
    flags = set(_list_value(row.get("quality_flags")))
    blockers: list[str] = []
    severe_flags = {
        "missing_delta",
        "missing_spread",
        "missing_open_interest",
        "missing_volume",
        "missing_iv_percentile",
        "spread_reject",
        "stale_market_data",
    }
    if quality == "bad" or flags & severe_flags:
        blockers.append("fix_option_data_disagreement")
    return blockers


def _entry_zone(row: dict[str, Any]) -> str:
    buy_under = _number(row.get("buy_under"))
    fill = _number(row.get("premium_fill_assumption"))
    if buy_under is None:
        return "wait_for_priced_entry"
    if fill is not None and fill <= buy_under:
        return f"at_or_below_{buy_under:.2f}"
    return f"wait_below_{buy_under:.2f}"


def _market_metrics(row: dict[str, Any]) -> dict[str, Any]:
    metrics = _json(row.get("market_metrics"))
    return metrics if isinstance(metrics, dict) else {}


def _market_cap(row: dict[str, Any]) -> float | None:
    metrics = _market_metrics(row)
    return _first_number(metrics, ("market_cap", "marketCap", "market_cap_basic", "market_capitalization"))


def _revenue_growth(row: dict[str, Any]) -> float | None:
    metrics = _market_metrics(row)
    return _first_number(metrics, ("revenue_growth", "revenueGrowth"))


def _first_number(values: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = _number(values.get(key))
        if value is not None:
            return value
    return None


def _position_sizing_band(tier: str) -> str:
    if tier == "Exceptional":
        return "0.25%-1.00% max premium risk"
    if tier == SERVICE_BUG_TIER:
        return "service bug before decision"
    if tier == "Research":
        return "research only"
    return "no position"


def _why_now(top_reasons: list[str], blockers: list[str], *, data_contract: dict[str, Any] | None = None) -> str:
    if data_contract and data_contract.get("status") != DATA_CONTRACT_READY:
        return str(data_contract.get("summary") or "Service bug blocks trade-state computation.")
    if blockers:
        return f"Trade gate failed now: {', '.join(blockers[:3])}."
    return f"Exceptional setup because {', '.join(top_reasons[:3])}."


def _kill_switch(row: dict[str, Any], validation: dict[str, Any]) -> str:
    reason = str(validation.get("reason") or "").strip()
    if validation.get("invalidation_status") == "breached":
        return reason or "Thesis invalidation breached."
    if validation.get("red_team_status") == "hard_risk_triggered":
        return reason or "Hard red-team risk triggered."
    ma50 = _number(row.get("ma_50"))
    if ma50 is not None:
        return f"Kill if thesis validation fails, spread widens, or stock loses 50D context near {ma50:.2f}."
    return "Kill if thesis validation fails, data quality degrades, or spread widens."


def _compact_opportunity_contract(detail: dict[str, Any]) -> dict[str, Any]:
    raw = detail.get("raw") if isinstance(detail.get("raw"), dict) else {}
    ev = raw.get("ev") if isinstance(raw.get("ev"), dict) else {}
    return {
        "event_id": detail.get("event_id"),
        "contract_id": detail.get("contract_id"),
        "state": detail.get("state"),
        "tier": detail.get("tier"),
        "conviction_score": detail.get("conviction_score"),
        "required_move_pct": detail.get("required_move_pct"),
        "premium_mid": detail.get("premium_mid"),
        "buy_under": detail.get("buy_under"),
        "data_contract_status": detail.get("data_contract_status"),
        "data_contract_failures": detail.get("data_contract_failures"),
        "service_repair_jobs": detail.get("service_repair_jobs"),
        "expiration": raw.get("expiration"),
        "strike": raw.get("strike"),
        "dte": raw.get("dte"),
        "spread_pct": raw.get("spread_pct"),
        "open_interest": raw.get("open_interest"),
        "volume": raw.get("volume"),
        # EV asymmetry + catalyst proximity: the per-ticker signals the UI ranks/reads on.
        "ev_multiple": ev.get("ev_multiple"),
        "p_2x": ev.get("p_2x"),
        "p_5x": ev.get("p_5x"),
        "conviction_ev": ev.get("conviction_ev"),
        "days_to_earnings": raw.get("days_to_earnings"),
        "blockers": detail.get("blockers"),
    }


def tier_rank(tier: str) -> int:
    if tier == "Exceptional":
        return 0
    if tier == "Research":
        return 1
    return 2
