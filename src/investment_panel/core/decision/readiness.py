"""Decision-readiness blockers, status, and fit."""

from __future__ import annotations
from typing import Any



def readiness_blockers(row: dict[str, Any], source_counts: dict[str, Any], portfolio_count: int) -> list[str]:
    gates = [str(gate) for gate in row.get("blocking_gates") or []]
    blockers: list[str] = []
    if row.get("quote_freshness") in {"stale", "failed"} or "stale_intraday_quote" in gates:
        blockers.append("stale quote age")
    if row.get("quote_freshness") in {"missing", "unknown"} or "missing_intraday_quote" in gates:
        blockers.append("missing intraday quote")
    if row.get("daily_analysis_freshness") in {"stale", "failed"} or "stale_daily_analysis" in gates:
        blockers.append("stale daily analysis")
    if row.get("daily_analysis_freshness") in {"missing", "unknown"} or "missing_daily_analysis" in gates:
        blockers.append("missing daily analysis")
    if "liquidity_unknown" in gates:
        blockers.append("missing liquidity")
    if "liquidity_bad" in gates:
        blockers.append("liquidity below sizing threshold")
    if portfolio_count == 0:
        blockers.append("missing portfolio context")
    if not has_required_valuation_context(row, source_counts):
        blockers.append("missing valuation")
    if "evidence_thin" in gates:
        blockers.append("thin primary evidence")
    return sorted(set(blockers))




def readiness_missing_inputs(row: dict[str, Any], source_counts: dict[str, Any], portfolio_count: int) -> list[str]:
    missing: list[str] = []
    if row.get("quote_freshness") in {"missing", "unknown"}:
        missing.append("quote")
    if row.get("daily_analysis_freshness") in {"missing", "unknown"}:
        missing.append("daily_analysis")
    if "liquidity_unknown" in (row.get("blocking_gates") or []):
        missing.append("liquidity")
    if not has_required_valuation_context(row, source_counts):
        missing.append("valuation")
    if portfolio_count == 0:
        missing.append("portfolio")
    return sorted(set(missing))




def readiness_status(row: dict[str, Any], blockers: list[str], missing_inputs: list[str]) -> str:
    refresh_terms = ("stale quote", "stale daily", "missing intraday quote", "missing daily")
    if any(any(term in blocker for term in refresh_terms) for blocker in blockers) or row.get("freshness_status") in {"stale", "failed"}:
        return "blocked_refresh"
    context_terms = {"portfolio", "liquidity", "valuation"}
    if context_terms.intersection(missing_inputs):
        return "blocked_missing_context"
    if "thin primary evidence" in blockers:
        return "needs_research"
    if row.get("action_grade") in {"Act", "Research"} and not blockers:
        return "ready"
    return "monitor"




def readiness_next_action(status: str, blockers: list[str], missing_inputs: list[str]) -> str:
    if status == "blocked_refresh":
        return "Run full_market_refresh or the specific stale source refresh before acting."
    if status == "blocked_missing_context":
        if "portfolio" in missing_inputs:
            return "Import or enter portfolio positions so sizing and duplicate-risk checks are available."
        if "liquidity" in missing_inputs:
            return "Refresh liquidity metrics before sizing a trade."
        return "Add the missing valuation/context row before making a buy decision."
    if status == "needs_research":
        return "Create or refresh the primary-evidence packet, catalyst check, and optional thesis."
    if status == "ready":
        return "Review ticker dossier and sizing constraints before placing any order."
    return "Monitor until a stronger catalyst, thesis, or source update appears."




def readiness_portfolio_fit(row: dict[str, Any], portfolio_count: int) -> dict[str, Any]:
    impact = row.get("portfolio_impact") if isinstance(row.get("portfolio_impact"), dict) else {}
    return {
        "has_portfolio_context": portfolio_count > 0,
        "current_exposure": impact if impact else {"owned": False},
        "overlap_correlation": "unknown",
        "concentration_impact": "unknown" if portfolio_count == 0 else "review_required",
        "existing_thesis_status": row.get("thesis_freshness") or "unknown",
        "duplicates_risk": bool(impact.get("owned")),
    }




def has_required_valuation_context(row: dict[str, Any], source_counts: dict[str, Any]) -> bool:
    if int(source_counts.get("valuation") or 0):
        return True
    basis = row.get("decision_basis") if isinstance(row.get("decision_basis"), dict) else {}
    asset_class = str(row.get("asset_class") or basis.get("asset_class") or "").lower()
    if asset_class == "etf":
        return bool(int(source_counts.get("etf_premium") or 0))
    if asset_class == "crypto":
        return bool(int(source_counts.get("crypto_fundamental") or 0))
    return False
