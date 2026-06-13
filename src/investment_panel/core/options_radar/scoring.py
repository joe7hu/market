"""Candidate EV, buy-under, scoring, theme matching and quality grading."""

from __future__ import annotations

from typing import Any

from investment_panel.analysis.option_ev import (EVInputs, compute_ev, conviction_from_ev)
from investment_panel.core.options_radar.coerce import (_elapsed_hours, _json, _number)
from investment_panel.core.options_radar.constants import (OPTION_PEER_CROSSCHECK_MAX_AGE_HOURS, OPTION_QUALITY_DELTA_BAD_ABSOLUTE_DIFF, OPTION_QUALITY_DELTA_CAUTION_ABSOLUTE_DIFF, OPTION_QUALITY_IV_BAD_RELATIVE_DIFF, OPTION_QUALITY_IV_CAUTION_RELATIVE_DIFF, OPTION_QUALITY_MID_BAD_RELATIVE_DIFF, OPTION_QUALITY_MID_CAUTION_RELATIVE_DIFF, THEME_WATCH_KEYWORDS)
from investment_panel.core.options_radar.indicators import (_relative_diff)

def _ev_raw(ev_result: Any) -> dict[str, Any] | None:
    """Serializable EV summary stashed on the candidate event for scoring,
    calibration (Phase 2) and the trader UI (Phase 4). ``None`` when unpriceable."""

    if ev_result is None:
        return None
    return {
        "ev_multiple": ev_result.ev_multiple,
        "p_2x": ev_result.p_2x,
        "p_5x": ev_result.p_5x,
        "p_10x": ev_result.p_10x,
        "ev_per_theta": ev_result.ev_per_theta,
        "sigma_eff": ev_result.sigma_eff,
        "conviction_ev": conviction_from_ev(ev_result.p_2x, ev_result.ev_multiple),
        "horizons": ev_result.horizons,
        "scenario_curve": ev_result.scenario_curve,
        "basis": ev_result.basis,
    }


def _buy_under(row: dict[str, Any], strategy: dict[str, Any]) -> float | None:
    underlying = _number(row.get("underlying_price"))
    strike = _number(row.get("strike"))
    if underlying is None or strike is None:
        return None
    max_move = float(strategy["max_required_move_pct"])
    option_type = str(row.get("option_type") or "").lower()
    if option_type == "put":
        return max(0.0, (strike - underlying * (1 - max_move)) / 10)
    return max(0.0, (underlying * (1 + max_move) - strike) / 10)


def _candidate_ev(row: dict[str, Any], *, option_type: str, dte: int | None) -> tuple[EVInputs, Any] | None:
    """Build EV-engine inputs from a candidate row and price it. Returns ``(inputs,
    EVResult)`` or ``None`` when the required fields (spot/strike/dte/premium/iv)
    are missing. ``rv_60d`` comes from the stock_features raw blob threaded through
    the candidate query."""

    premium = _number(row.get("mid"))
    spot = _number(row.get("underlying_price"))
    strike = _number(row.get("strike"))
    iv = _number(row.get("iv"))
    if premium is None or premium <= 0 or spot is None or strike is None or dte is None or iv is None:
        return None
    stock_raw = _json(row.get("stock_features_raw"))
    rv_60d = _number(stock_raw.get("rv_60d"))
    # Free flow proxy widens the EV scenario tail: strong OI/volume expansion is the
    # best free precursor of an outlier move, so a high flow_score lifts tail width up
    # to +60%. This is the single point a future paid flow feed plugs into.
    flow_score = _number(row.get("flow_score"))
    tail_multiplier = 1.0 + (min(100.0, max(0.0, flow_score)) / 100.0) * 0.6 if flow_score is not None else 1.0
    inputs = EVInputs(
        option_type=option_type if option_type in {"call", "put"} else "call",
        spot=spot,
        strike=strike,
        dte=int(dte),
        premium=premium,
        iv=iv,
        rv_60d=rv_60d,
        tail_multiplier=tail_multiplier,
    )
    result = compute_ev(inputs)
    if result is None:
        return None
    return inputs, result


def _setup_score(row: dict[str, Any]) -> float:
    """Continuous 0-100 entry-setup quality from features compute_stock_feature already
    produces but the old binary MA50 gate ignored: proximity to the breakout level, base
    length, volume contraction, RS slope, and ATR compression. Falls back to the binary
    above/below-MA50 read when the richer features are absent."""

    price = _number(row.get("price"))
    breakout = _number(row.get("breakout_level"))
    base_len = _number(row.get("base_length_days"))
    volume_ratio = _number(row.get("volume_ratio"))
    rs20 = _number(row.get("rs_vs_qqq_20d"))
    rs60 = _number(row.get("rs_vs_qqq_60d"))
    atr_pct = _number(row.get("atr_pct"))

    components: list[float] = []
    if price is not None and breakout and breakout > 0:
        # 1.0 = at the breakout; reward proximity from ~0.85x upward, cap above.
        components.append(max(0.0, min(100.0, (price / breakout - 0.85) / 0.15 * 100.0)))
    if base_len is not None:
        components.append(max(0.0, min(100.0, base_len / 120.0 * 100.0)))
    if volume_ratio is not None:
        # Contraction (ratio < 1) is constructive; 0.6x -> 100, 1.2x -> 0.
        components.append(max(0.0, min(100.0, (1.2 - volume_ratio) / 0.6 * 100.0)))
    if rs20 is not None or rs60 is not None:
        rs = max(rs20 or 0.0, rs60 or 0.0)
        components.append(max(0.0, min(100.0, (rs * 100.0 + 10.0) / 0.2)))
    if atr_pct is not None:
        components.append(max(0.0, min(100.0, (0.06 - atr_pct) / 0.06 * 100.0)))

    if not components:
        return 100.0 if (price or 0) >= (_number(row.get("ma_50")) or 10**9) else 45.0
    return round(sum(components) / len(components), 2)


def _candidate_score(
    row: dict[str, Any],
    state: str,
    watch_themes: list[str] | None = None,
    *,
    ev_asymmetry: float | None = None,
) -> float:
    if state == "REJECT":
        return 0.0
    required_move = _number(row.get("required_move_10x_pct")) or 10
    liquidity = _number(row.get("liquidity_score")) or 0
    convexity = _number(row.get("convexity_score")) or 0
    rs = _number(row.get("rs_vs_qqq_20d")) or 0
    technical = _setup_score(row)
    rs_term = (max(-20.0, min(20.0, rs * 100)) + 20) * 0.05
    if ev_asymmetry is not None:
        # EV-derived asymmetry (probability- and theta-aware) replaces the linear
        # required-move and convexity proxies; liquidity/technical/RS keep their weight.
        score = (ev_asymmetry * 0.65) + (liquidity * 0.20) + (technical * 0.10) + rs_term
    else:
        score = (max(0.0, 100.0 - required_move * 20.0) * 0.35) + (liquidity * 0.20) + (convexity * 0.30) + (technical * 0.10) + rs_term
    score += _theme_watch_score(watch_themes or _theme_watch_matches(row))
    if state == "WATCH":
        score *= 0.70
    if state == "SETUP":
        score *= 0.88
    return round(max(0.0, min(100.0, score)), 2)


def _theme_watch_score(themes: list[str]) -> float:
    if not themes:
        return 0.0
    return min(8.0, 4.0 + max(0, len(themes) - 1) * 2.0)


def _theme_watch_matches(row: dict[str, Any]) -> list[str]:
    text = _theme_context_text(row)
    if not text:
        return []
    matches: list[str] = []
    for theme, keywords in THEME_WATCH_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            matches.append(theme)
    return matches


def _theme_context_text(row: dict[str, Any]) -> str:
    parts = [
        row.get("ticker"),
        row.get("instrument_name"),
        row.get("asset_class"),
        row.get("sector"),
        row.get("industry"),
        row.get("category"),
    ]
    return f" {' '.join(str(part or '').lower() for part in parts)} "


def _has_missing_data(blockers: list[str]) -> bool:
    return any(blocker.startswith("missing_") for blocker in blockers)


def _is_delayed_feed(row: dict[str, Any]) -> bool:
    """Whether a candidate's quotes came from a delayed (non-real-time) feed.

    IBKR delayed OPRA chains stamp the chain row ``market_data='delayed'``; other
    providers expose it via ``market_data_type``/``data_status``. Such feeds do not
    carry usable real-time option volume, so the volume gate is not applied to them.
    """

    raw = _json(row.get("raw"))
    marker = str(
        raw.get("market_data")
        or raw.get("market_data_type")
        or raw.get("data_status")
        or row.get("data_status")
        or ""
    ).lower()
    return "delayed" in marker


def _candidate_quality(row: dict[str, Any], *, state: str, blockers: list[str], hard_rejects: list[str]) -> dict[str, Any]:
    if state == "REJECT":
        return {"status": "ok", "flags": [], "peer": {}}

    flags: list[str] = []
    bad_flags: set[str] = set()
    raw = _json(row.get("raw"))
    greeks_source = str(raw.get("greeks_source") or "provider")
    data_source = str(row.get("data_source") or "unknown")
    peer_source = row.get("peer_data_source")
    peer: dict[str, Any] = {"source": peer_source} if peer_source else {}
    peer_age_hours = _elapsed_hours(row.get("peer_snapshot_time"), row.get("snapshot_time"))
    peer_fresh = peer_age_hours is None or peer_age_hours <= OPTION_PEER_CROSSCHECK_MAX_AGE_HOURS
    if peer_source and peer_age_hours is not None:
        peer["age_hours"] = round(peer_age_hours, 2)
    if peer_source and not peer_fresh:
        peer["crosscheck_skipped"] = "stale_peer_snapshot"

    missing_flags = [blocker for blocker in blockers if blocker in {"missing_delta", "missing_spread", "missing_open_interest", "missing_volume", "missing_iv_percentile"}]
    if missing_flags:
        flags.extend(missing_flags)
        bad_flags.update(missing_flags)
    if "spread_above_fire_threshold" in blockers:
        flags.append("spread_above_threshold")
    if any(reject in hard_rejects for reject in {"spread_reject"}):
        flags.append("spread_reject")
        bad_flags.add("spread_reject")
    if state == "FIRE" and greeks_source in {"black_scholes_model", "mixed_fallback"}:
        flags.append("modeled_greeks")
    if state == "FIRE" and greeks_source == "mixed_fallback":
        flags.append("mixed_greek_sources")

    data_status = str(raw.get("market_data") or raw.get("market_data_type") or raw.get("data_status") or raw.get("entitlement_status") or "").lower()
    if "delayed" in data_status:
        flags.append("delayed_market_data")
    if "stale" in data_status:
        flags.append("stale_market_data")
        bad_flags.add("stale_market_data")

    mid = _number(row.get("mid"))
    peer_mid = _number(row.get("peer_mid"))
    mid_diff = _relative_diff(mid, peer_mid) if peer_fresh else None
    if mid_diff is not None:
        peer["mid_relative_diff"] = round(mid_diff, 4)
        if mid_diff >= OPTION_QUALITY_MID_BAD_RELATIVE_DIFF:
            flags.append("source_mid_disagreement")
            bad_flags.add("source_mid_disagreement")
        elif mid_diff >= OPTION_QUALITY_MID_CAUTION_RELATIVE_DIFF:
            flags.append("source_mid_disagreement")

    iv = _number(row.get("iv"))
    peer_iv = _number(row.get("peer_iv"))
    iv_diff = _relative_diff(iv, peer_iv) if peer_fresh else None
    if iv_diff is not None:
        peer["iv_relative_diff"] = round(iv_diff, 4)
        if iv_diff >= OPTION_QUALITY_IV_BAD_RELATIVE_DIFF:
            flags.append("source_iv_disagreement")
            bad_flags.add("source_iv_disagreement")
        elif iv_diff >= OPTION_QUALITY_IV_CAUTION_RELATIVE_DIFF:
            flags.append("source_iv_disagreement")

    delta = _number(row.get("delta"))
    peer_delta = _number(row.get("peer_delta"))
    if peer_fresh and delta is not None and peer_delta is not None:
        delta_diff = abs(delta - peer_delta)
        peer["delta_absolute_diff"] = round(delta_diff, 4)
        if delta_diff >= OPTION_QUALITY_DELTA_BAD_ABSOLUTE_DIFF:
            flags.append("source_delta_disagreement")
            bad_flags.add("source_delta_disagreement")
        elif delta_diff >= OPTION_QUALITY_DELTA_CAUTION_ABSOLUTE_DIFF:
            flags.append("source_delta_disagreement")

    deduped_flags = list(dict.fromkeys(flags))
    if bad_flags:
        status = "bad"
    elif deduped_flags:
        status = "caution"
    else:
        status = "ok"
    return {
        "status": status,
        "flags": deduped_flags,
        "source": data_source,
        "greeks_source": greeks_source,
        "peer": peer,
    }
