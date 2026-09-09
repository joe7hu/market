"""Expected-value option scoring engine for the 10x radar (Phase 1f).

Pure, deterministic functions — no Monte Carlo, no DB, no network. The engine
reprices an option across a grid of terminal underlying moves and a few horizons
under a fat-tailed (Student-t) return density, then integrates to an expected
value, a multiple, and multiple-hit probabilities (P(2x)/P(5x)/P(10x)).

It reuses the Black-Scholes pricer in :mod:`analysis.options_payoff` so the
codebase keeps a single options model. Because each scenario is repriced with
Black-Scholes at the *remaining* DTE, the theta path is intrinsic to the EV
rather than approximated by a linear-breakeven penalty — that is the whole point
of replacing the old ``_candidate_score`` heuristic.

The ``flow_score`` / ``tail_multiplier`` inputs are the designed abstraction
point for a future paid flow feed: a richer signal raises the effective tail
width or the EV prior without any change to the scoring math here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

from .options_payoff import black_scholes

DEFAULT_RISK_FREE_RATE = 0.04
# Student-t degrees of freedom. nu in [3,4] gives fat tails (the free precursor of
# outlier option moves) while keeping a finite variance (nu>2).
DEFAULT_STUDENT_T_DOF = 4.0
DEFAULT_TAIL_MULTIPLIER = 1.0
# Terminal probabilities understate capturable returns because a trade can be
# exited at a peak before expiry. Correct terminal P(kx) upward by this factor
# until ``candidate_event_mark`` history calibrates it (conservative 1.3x start).
DEFAULT_PEAK_UPLIFT = 1.3
# EV multiple required for a contract to be considered a real asymmetric bet; the
# buy-under price is the premium at which EV crosses this floor.
EV_FLOOR = 2.0

# Log-return grid spanning a terminal underlying multiple of 0.30x .. 4.0x.
GRID_LO_MULTIPLE = 0.30
GRID_HI_MULTIPLE = 4.0
GRID_POINTS = 17

# Multiples whose hit-probabilities the engine reports.
PROBABILITY_MULTIPLES = (2.0, 5.0, 10.0)


@dataclass
class EVInputs:
    """Everything the EV engine needs to price one option contract.

    ``iv`` anchors the (flat, by default) implied-vol surface used for repricing.
    ``rv_60d`` lets realized vol widen the scenario density when it exceeds IV
    (the cheap-convexity / underpriced-vol case). ``iv_for_scenario`` is an
    optional sticky-delta surface hook (logret, horizon_days) -> iv; the default
    keeps IV flat, which is sufficient and deterministic for unit tests.
    """

    option_type: str
    spot: float
    strike: float
    dte: int
    premium: float
    iv: float
    rv_60d: float | None = None
    tail_multiplier: float = DEFAULT_TAIL_MULTIPLIER
    dof: float = DEFAULT_STUDENT_T_DOF
    peak_uplift: float = DEFAULT_PEAK_UPLIFT
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE
    iv_for_scenario: Callable[[float, int], float] | None = None


@dataclass
class EVResult:
    ev_multiple: float
    p_2x: float
    p_5x: float
    p_10x: float
    ev_per_theta: float | None
    sigma_eff: float
    horizons: list[int]
    scenario_curve: list[dict[str, float]] = field(default_factory=list)
    basis: dict[str, float | str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "ev_multiple": self.ev_multiple,
            "p_2x": self.p_2x,
            "p_5x": self.p_5x,
            "p_10x": self.p_10x,
            "ev_per_theta": self.ev_per_theta,
            "sigma_eff": self.sigma_eff,
            "horizons": list(self.horizons),
            "scenario_curve": self.scenario_curve,
            "basis": self.basis,
        }


def _student_t_pdf(x: float, dof: float) -> float:
    """Unnormalized Student-t density (the normalizer cancels after we sum)."""

    return (1.0 + (x * x) / dof) ** (-(dof + 1.0) / 2.0)


def scenario_grid() -> list[tuple[float, float]]:
    """Return ``(underlying_multiple, log_return)`` pairs, log-spaced 0.30x..4.0x."""

    lo, hi = math.log(GRID_LO_MULTIPLE), math.log(GRID_HI_MULTIPLE)
    step = (hi - lo) / (GRID_POINTS - 1)
    grid: list[tuple[float, float]] = []
    for i in range(GRID_POINTS):
        logret = lo + step * i
        grid.append((math.exp(logret), logret))
    return grid


def horizons_for(dte: int) -> list[int]:
    """Repricing horizons in days: 90, 180, and min(365, DTE/2).

    Only horizons strictly inside the contract's life are kept; an at/after-expiry
    horizon is collapsed to ``dte`` so the option is valued at intrinsic there.
    """

    raw = [90, 180, min(365, max(1, dte // 2))]
    kept = sorted({h for h in raw if 0 < h <= dte})
    return kept or [max(1, dte)]


def _sigma_eff(inputs: EVInputs) -> float:
    base = max(inputs.iv or 0.0, inputs.rv_60d or 0.0)
    return max(0.0, base) * max(0.0, inputs.tail_multiplier)


def scenario_value(inputs: EVInputs, underlying_multiple: float, horizon_days: int) -> float:
    """Black-Scholes value of the contract if the underlying is at ``spot*multiple``
    after ``horizon_days``, repriced at the remaining DTE (intrinsic at/after expiry)."""

    spot_t = inputs.spot * underlying_multiple
    remaining_years = max(0.0, (inputs.dte - horizon_days) / 365.0)
    if inputs.iv_for_scenario is not None:
        iv = inputs.iv_for_scenario(math.log(max(underlying_multiple, 1e-9)), horizon_days)
    else:
        iv = inputs.iv
    return black_scholes(inputs.option_type, spot_t, inputs.strike, remaining_years, inputs.risk_free_rate, iv)


def _horizon_distribution(inputs: EVInputs, horizon_days: int, sigma_eff: float) -> list[tuple[float, float, float]]:
    """Return ``(weight, underlying_multiple, scenario_value)`` over the grid for one
    horizon. Weights are a variance-matched Student-t over log returns, normalized."""

    sigma_h = sigma_eff * math.sqrt(horizon_days / 365.0)
    grid = scenario_grid()
    # Variance correction so the *actual* std of log returns equals sigma_h despite
    # the Student-t's nu/(nu-2) raw variance.
    dof = inputs.dof
    var_scale = math.sqrt(dof / (dof - 2.0)) if dof > 2.0 else 1.0
    rows: list[tuple[float, float, float]] = []
    total = 0.0
    for multiple, logret in grid:
        if sigma_h > 0:
            standardized = (logret / sigma_h) * var_scale
            weight = _student_t_pdf(standardized, dof)
        else:
            weight = 1.0 if abs(logret) < 1e-9 else 0.0
        value = scenario_value(inputs, multiple, horizon_days)
        rows.append((weight, multiple, value))
        total += weight
    if total <= 0:
        return [(1.0 / len(rows), m, v) for _w, m, v in rows]
    return [(w / total, m, v) for w, m, v in rows]


def _valid(inputs: EVInputs) -> bool:
    return (
        inputs.option_type in {"call", "put"}
        and inputs.spot > 0
        and inputs.strike > 0
        and inputs.dte > 0
        and inputs.premium > 0
        and inputs.iv is not None
        and inputs.dof > 2.0
        and all(math.isfinite(v) for v in (inputs.spot, inputs.strike, inputs.premium, inputs.iv or 0.0))
    )


def compute_ev(inputs: EVInputs) -> EVResult | None:
    """Integrate expected value and hit-probabilities across horizons. ``None`` when
    inputs are unusable (missing IV, non-positive premium, etc.)."""

    if not _valid(inputs):
        return None
    sigma_eff = _sigma_eff(inputs)
    if sigma_eff <= 0:
        return None
    horizons = horizons_for(inputs.dte)

    ev_multiples: list[float] = []
    prob_accumulator: dict[float, list[float]] = {k: [] for k in PROBABILITY_MULTIPLES}
    curve_by_multiple: dict[float, dict[str, float]] = {}

    for horizon in horizons:
        dist = _horizon_distribution(inputs, horizon, sigma_eff)
        ev_value = sum(weight * value for weight, _m, value in dist)
        ev_multiples.append(ev_value / inputs.premium)
        for k in PROBABILITY_MULTIPLES:
            hit = sum(weight for weight, _m, value in dist if value >= k * inputs.premium)
            prob_accumulator[k].append(hit)
        for _weight, multiple, value in dist:
            entry = curve_by_multiple.setdefault(
                round(multiple, 4), {"underlying": round(inputs.spot * multiple, 4), "multiple": round(multiple, 4)}
            )
            entry[f"value_h{horizon}"] = round(value, 4)

    # theta cost: value lost if the underlying is unchanged after the nearest horizon.
    flat_value_first = scenario_value(inputs, 1.0, horizons[0])
    ev_multiple = sum(ev_multiples) / len(ev_multiples)
    uplift = max(1.0, inputs.peak_uplift)
    p = {k: min(1.0, (sum(vals) / len(vals)) * uplift) for k, vals in prob_accumulator.items()}

    # theta cost as the fraction of premium lost if the underlying does not move,
    # measured over the nearest horizon. ev_per_theta normalizes asymmetry by decay.
    ev_per_theta: float | None = None
    theta_cost_fraction = (inputs.premium - flat_value_first) / inputs.premium
    if theta_cost_fraction > 1e-6:
        ev_per_theta = round((ev_multiple - 1.0) / theta_cost_fraction, 4)

    scenario_curve = [curve_by_multiple[m] for m in sorted(curve_by_multiple)]
    return EVResult(
        ev_multiple=round(ev_multiple, 4),
        p_2x=round(p[2.0], 4),
        p_5x=round(p[5.0], 4),
        p_10x=round(p[10.0], 4),
        ev_per_theta=ev_per_theta,
        sigma_eff=round(sigma_eff, 4),
        horizons=horizons,
        scenario_curve=scenario_curve,
        basis={
            "dof": inputs.dof,
            "tail_multiplier": inputs.tail_multiplier,
            "peak_uplift": uplift,
            "iv": inputs.iv,
            "rv_60d": inputs.rv_60d if inputs.rv_60d is not None else "",
        },
    )


def ev_inverse_buy_under(inputs: EVInputs, *, target_ev: float = EV_FLOOR) -> float | None:
    """The maximum premium at which the EV multiple still clears ``target_ev`` — a
    real limit price. The expected payoff in dollars is premium-independent (the
    scenario values don't depend on what we paid), so ``EV_multiple = ev_value /
    premium`` inverts in closed form: ``buy_under = ev_value / target_ev``."""

    if not _valid(inputs):
        return None
    base = compute_ev(inputs)
    if base is None:
        return None
    ev_value = base.ev_multiple * inputs.premium  # expected payoff in dollars
    if ev_value <= 0:
        return None
    return round(max(0.0, ev_value / target_ev), 4)


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-value))


def ev_score(ev_multiple: float | None, spread_pct: float | None) -> float:
    """Asymmetry score 0..100 = 100*sigmoid(log(ev_multiple)) with a liquidity haircut
    for expected round-trip slippage (one spread crossing in, one out)."""

    if ev_multiple is None or ev_multiple <= 0:
        return 0.0
    raw = 100.0 * _sigmoid(math.log(ev_multiple))
    haircut = 1.0
    if spread_pct is not None and spread_pct > 0:
        haircut = max(0.0, 1.0 - min(1.0, spread_pct))  # full spread ~ full round-trip cost
    return round(max(0.0, min(100.0, raw * haircut)), 2)


def conviction_from_ev(p_2x: float | None, ev_multiple: float | None) -> float:
    """Conviction 0..100 = 100 * calibrated P(2x) * min(1, EV/EV_FLOOR).

    Callers pass an already-calibrated P(2x) once Phase 2 lands; until then the raw
    terminal-with-uplift probability is a serviceable prior.
    """

    if p_2x is None or ev_multiple is None:
        return 0.0
    ev_term = min(1.0, max(0.0, ev_multiple) / EV_FLOOR)
    return round(max(0.0, min(100.0, 100.0 * max(0.0, min(1.0, p_2x)) * ev_term)), 2)
