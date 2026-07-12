"""Structure-correct, advisory-only trade guidance for radar publications."""

from __future__ import annotations

from typing import Any


def recommendation_fields(row: dict[str, Any]) -> dict[str, Any]:
    structure = str(row.get("structure") or "")
    state = str(row.get("state") or "WATCH").upper()
    blockers = list(row.get("blockers") or [])
    spot = _number(row.get("underlying_price"))
    break_even = _number(row.get("break_even"))
    entry = _number(row.get("entry_price"))
    buy_under = _number(row.get("buy_under"))
    max_profit = _number(row.get("max_profit"))

    short_put = structure == "cash_secured_put"
    paper_ready = state == "READY" and not blockers
    posture = "PAPER_READY" if paper_ready else "RESEARCH_SETUP" if state == "SETUP" else "NO_TRADE"
    verb = "SELL CASH-SECURED PUT" if short_put else "BUY TO OPEN"
    action = f"PAPER — {verb}" if paper_ready else f"RESEARCH — {verb}" if state == "SETUP" else "NO TRADE"
    suggested_limit = entry if short_put else (buy_under if buy_under is not None and buy_under > 0 else entry)
    break_even_move = abs(break_even - spot) / spot if break_even is not None and spot and spot > 0 else None

    if short_put:
        profit_take = entry * 0.5 if entry is not None else None
        exit_plan = "Buy to close near 50% of the entry credit; do not hold through a broken assignment thesis."
        invalidation = f"Underlying closes below the ${break_even:.2f} break-even or the assignment thesis breaks." if break_even is not None else "Assignment thesis or secured-cash capacity breaks."
        limit_label = "minimum_credit"
    else:
        profit_take = entry * 2 if entry is not None else None
        exit_plan = "Take partial or full profit near 2x premium; exit before expiry if the thesis or liquidity deteriorates."
        invalidation = f"Underlying invalidates the thesis before reaching the ${break_even:.2f} expiry break-even." if break_even is not None else "Thesis, liquidity, or risk budget breaks."
        limit_label = "maximum_entry"

    no_trade_reason = None if paper_ready else (str(blockers[0]) if blockers else "forward_calibration_not_mature")
    return {
        "recommendation_state": posture,
        "advisory_action": action,
        "paper_ready": paper_ready,
        "suggested_limit": suggested_limit,
        limit_label: suggested_limit,
        "break_even_move_pct": break_even_move,
        "profit_take_option_price": profit_take,
        "exit_plan": exit_plan,
        "invalidation": invalidation,
        "no_trade_reason": no_trade_reason,
        "payoff_cap": max_profit,
    }


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None
