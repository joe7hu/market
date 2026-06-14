"""Thin adapter: re-export decision-brief synthesis from the decision engine.

The brief synthesis is investment-decision reasoning and lives in
``investment_panel.core.decision`` (see ``core/decision/brief.py`` and
``core/decision/brief_options.py``). This module preserves the historical
``app.data_access.decision_brief`` import contract so routers, payloads, and the
ticker dossier can keep importing from here unchanged.
"""

from __future__ import annotations

from investment_panel.core.decision import (
    GATE_LABELS,
    _brief_summary,
    _is_no_trade_action,
    _is_option_expired,
    ticker_decision_brief,
)

__all__ = [
    "GATE_LABELS",
    "_brief_summary",
    "_is_no_trade_action",
    "_is_option_expired",
    "ticker_decision_brief",
]
