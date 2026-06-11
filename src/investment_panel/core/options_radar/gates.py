"""Declarative candidate gating for the options radar.

``build_candidate_event`` assembles a :class:`CandidateContext` (parsing the
snapshot row plus the handful of EV / theme / liquidity signals that need the
wider pipeline) and folds it through :data:`GATES` — an ordered list of small
gate functions. Each gate records reason codes on a :class:`GateVerdict` as a
hard reject, a soft blocker, or a positive.

Adding a strategy gate is now a localized change: append one function to
``GATES`` (or have an existing gate read a new ``strategy`` flag) instead of
editing a 200-line procedure. Gate order is preserved verbatim from the original
imperative implementation so the emitted ``trigger_reason`` is byte-identical.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class CandidateContext:
    """Everything the gates need, parsed once by ``build_candidate_event``."""

    row: dict[str, Any]
    strategy: dict[str, Any]
    option_type: str
    required_move: float
    premium: float
    dte: int | None
    delta_value: float | None
    spread_pct: float | None
    open_interest: float | None
    volume: float | None
    off_hours: bool
    delayed_feed: bool
    iv_percentile: float | None
    price: float | None
    ma50: float | None
    rs20: float | None
    ev_multiple: float | None
    flow_zscore: float | None
    volume_oi_ratio: float | None
    oi_change_1d: float | None
    term_slope: float | None
    put_call_skew_25d: float | None
    iv_rv_ratio: float | None
    catalyst_in_window: bool
    buy_under: float | None
    watch_themes: list[str]


@dataclass
class GateVerdict:
    hard_rejects: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    positives: list[str] = field(default_factory=list)

    def reject(self, reason: str) -> None:
        self.hard_rejects.append(reason)

    def block(self, reason: str) -> None:
        self.blockers.append(reason)

    def ok(self, reason: str) -> None:
        self.positives.append(reason)


def _gate_option_type(c: CandidateContext, v: GateVerdict) -> None:
    if c.option_type != c.strategy["option_type"]:
        v.reject(f"strategy_only_tracks_{c.strategy['option_type']}s")


def _gate_dte(c: CandidateContext, v: GateVerdict) -> None:
    if c.dte is None:
        v.block("missing_dte")
    elif c.dte < int(c.strategy["dte_min"]) or c.dte > int(c.strategy["dte_max"]):
        v.reject("dte_outside_strategy_range")


def _gate_delta(c: CandidateContext, v: GateVerdict) -> None:
    if c.delta_value is not None:
        delta = abs(c.delta_value)
        if delta < float(c.strategy["delta_min"]) or delta > float(c.strategy["delta_max"]):
            v.reject("delta_outside_strategy_range")
        else:
            v.ok("delta_in_range")
    else:
        v.block("missing_delta")


def _gate_required_move(c: CandidateContext, v: GateVerdict) -> None:
    if c.required_move > float(c.strategy["max_required_move_pct"]):
        v.reject("required_move_too_high")
    else:
        v.ok("10x_math_inside_cap")


def _gate_spread(c: CandidateContext, v: GateVerdict) -> None:
    if c.spread_pct is None:
        v.block("missing_spread")
    elif c.spread_pct > float(c.strategy["reject_spread_pct"]):
        v.reject("spread_reject")
    elif c.spread_pct > float(c.strategy["max_spread_pct"]):
        v.block("spread_above_fire_threshold")
    else:
        v.ok("spread_usable")


def _gate_open_interest(c: CandidateContext, v: GateVerdict) -> None:
    if c.open_interest is None:
        v.block("missing_open_interest")
    elif c.open_interest < float(c.strategy["min_open_interest"]):
        v.block("open_interest_below_threshold")
    else:
        v.ok("open_interest_supported")


def _gate_volume(c: CandidateContext, v: GateVerdict) -> None:
    min_oi = float(c.strategy["min_open_interest"])
    oi_supported = c.open_interest is not None and c.open_interest >= min_oi
    if c.off_hours:
        # Volume is a regular-hours metric; off-hours it is ~0 and not meaningful.
        # Lean on open interest for liquidity and mark the candidate indicative so
        # it never presents as trade-ready until RTH volume confirms it.
        if oi_supported:
            v.ok("off_hours_oi_liquidity")
        else:
            v.block("off_hours_low_open_interest")
        v.block("off_hours_indicative")
    elif c.volume is not None and c.volume >= float(c.strategy["min_volume"]):
        v.ok("volume_seen")
    elif c.delayed_feed:
        # A delayed feed (e.g. IBKR delayed OPRA) does not carry reliable real-time
        # option volume — it prints 0 or nothing even when the contract is liquid.
        # Volume is not a usable liquidity gate here, so lean on open interest like
        # the off-hours path and mark the candidate indicative rather than failing it
        # on a volume the feed can never supply. Delayed rows where volume actually
        # printed (>= min_volume, handled above) keep their volume_seen credit.
        if oi_supported:
            v.ok("delayed_oi_liquidity")
        else:
            v.block("delayed_low_open_interest")
        v.block("delayed_indicative")
    elif c.volume is None:
        v.block("missing_volume")
    else:
        v.block("volume_below_threshold")


def _gate_iv_percentile(c: CandidateContext, v: GateVerdict) -> None:
    if c.iv_percentile is None:
        v.block("missing_iv_percentile")
    elif c.iv_percentile > float(c.strategy["reject_iv_percentile"]):
        v.reject("iv_percentile_reject")
    elif c.iv_percentile > float(c.strategy["max_iv_percentile"]):
        v.block("iv_percentile_above_fire_threshold")
    else:
        v.ok("iv_not_overpriced")


def _gate_price_above_ma50(c: CandidateContext, v: GateVerdict) -> None:
    if not c.strategy.get("require_price_above_ma50"):
        return
    if c.price is None or c.ma50 is None:
        v.block("missing_50d_context")
    elif c.price < c.ma50:
        v.block("stock_below_50d")
    else:
        v.ok("stock_above_50d")


def _gate_price_below_ma50(c: CandidateContext, v: GateVerdict) -> None:
    # Breakdown-put family mirrors the long gates: it wants the stock *under* its 50d.
    if not c.strategy.get("require_price_below_ma50"):
        return
    if c.price is None or c.ma50 is None:
        v.block("missing_50d_context")
    elif c.price > c.ma50:
        v.block("stock_above_50d")
    else:
        v.ok("stock_below_50d")


def _gate_rs_improving(c: CandidateContext, v: GateVerdict) -> None:
    if not c.strategy.get("require_rs_improving"):
        return
    if c.rs20 is None:
        v.block("missing_rs_vs_qqq")
    elif c.rs20 < 0:
        v.block("rs_vs_qqq_20d_negative")
    else:
        v.ok("rs_vs_qqq_improving")


def _gate_rs_deteriorating(c: CandidateContext, v: GateVerdict) -> None:
    if not c.strategy.get("require_rs_deteriorating"):
        return
    if c.rs20 is None:
        v.block("missing_rs_vs_qqq")
    elif c.rs20 > 0:
        v.block("rs_vs_qqq_20d_positive")
    else:
        v.ok("rs_vs_qqq_deteriorating")


def _gate_ev_asymmetry(c: CandidateContext, v: GateVerdict) -> None:
    if c.ev_multiple is not None and c.ev_multiple >= 2.0:
        v.ok("ev_asymmetry_2x")


def _gate_flow_expansion(c: CandidateContext, v: GateVerdict) -> None:
    # Free flow expansion is a positive precursor signal: >=2 sigma OI expansion, or
    # heavy volume into rising OI.
    if (c.flow_zscore is not None and c.flow_zscore >= 2.0) or (
        c.volume_oi_ratio is not None and c.volume_oi_ratio >= 1.0 and (c.oi_change_1d or 0) > 0
    ):
        v.ok("flow_expansion_detected")


def _gate_vol_surface(c: CandidateContext, v: GateVerdict) -> None:
    # Vol-surface tail signals (additive, reasons-only): an inverted/flattening term
    # structure anticipates an event; negative 25d skew is upside (call) demand; a
    # cheap IV/RV ratio means convexity is underpriced relative to realized movement.
    if c.term_slope is not None and c.term_slope < -0.02:
        v.ok("term_structure_inverted")
    if c.put_call_skew_25d is not None and c.put_call_skew_25d <= -0.03:
        v.ok("call_skew_demand")
    if c.iv_rv_ratio is not None and c.iv_rv_ratio <= 1.1:
        v.ok("cheap_convexity_iv_rv")


def _gate_catalyst_within(c: CandidateContext, v: GateVerdict) -> None:
    if c.catalyst_in_window:
        v.ok("catalyst_within_dte")


def _gate_requires_catalyst(c: CandidateContext, v: GateVerdict) -> None:
    # Catalyst-call family requires a known catalyst inside the contract's life.
    if c.strategy.get("requires_catalyst") and not c.catalyst_in_window:
        v.block("no_catalyst_in_window")


def _gate_max_iv_rv(c: CandidateContext, v: GateVerdict) -> None:
    # Guard against overpaying for vol that will crush after the event (IV/RV cap).
    max_iv_rv = c.strategy.get("max_iv_rv_ratio")
    if max_iv_rv is not None and c.iv_rv_ratio is not None and c.iv_rv_ratio > float(max_iv_rv):
        v.block("iv_rich_vs_rv")


def _gate_buy_under(c: CandidateContext, v: GateVerdict) -> None:
    if c.buy_under is None:
        v.block("buy_under_unavailable")
    elif c.premium > c.buy_under:
        v.block("premium_above_buy_under")
    else:
        v.ok("premium_inside_buy_under")


def _gate_themes(c: CandidateContext, v: GateVerdict) -> None:
    v.positives.extend(c.watch_themes)


# Ordered gate pipeline. Order is significant: it determines the emitted reason
# sequence, and is preserved exactly from the original implementation.
GATES: list[Callable[[CandidateContext, GateVerdict], None]] = [
    _gate_option_type,
    _gate_dte,
    _gate_delta,
    _gate_required_move,
    _gate_spread,
    _gate_open_interest,
    _gate_volume,
    _gate_iv_percentile,
    _gate_price_above_ma50,
    _gate_price_below_ma50,
    _gate_rs_improving,
    _gate_rs_deteriorating,
    _gate_ev_asymmetry,
    _gate_flow_expansion,
    _gate_vol_surface,
    _gate_catalyst_within,
    _gate_requires_catalyst,
    _gate_max_iv_rv,
    _gate_buy_under,
    _gate_themes,
]


def run_gates(context: CandidateContext) -> GateVerdict:
    verdict = GateVerdict()
    for gate in GATES:
        gate(context, verdict)
    return verdict
