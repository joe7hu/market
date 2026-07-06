"""Static configuration for the options radar pipeline.

Pure data — strategy parameter presets, gate thresholds, theme keyword maps.
No logic and no intra-package dependencies, so every other radar submodule can
import from here without risk of a cycle.
"""

from __future__ import annotations

from typing import Any


DEFAULT_STRATEGY_VERSION = "leap_10x_reversal_v1"
MIN_FORWARD_TEST_DAYS = 30
SHORT_HORIZON_FORWARD_TEST_DAYS = 5
CATALYST_FORWARD_TEST_DAYS = 10
# Trailing-stop give-back used to turn a peak-mark series into a realizable exit return.
# A move must hold within this fraction of its running peak gain to be "captured"; a
# one-mark spike that collapses trails out near the breach instead of crediting the high.
# This is what makes hit-rates / calibration measure exitable wins, not paper highs.
REALIZED_EXIT_TRAIL_FRAC = 0.35
# Epsilon-exploration of SETUP (near-miss) candidates: shadow-trade this fraction of them
# so the learning loop sees realized outcomes from the rejected region, but only once the
# SETUP population clears the floor (keeps thin unit/edge tapes from fabricating trades).
EXPLORATION_SAMPLE_RATE = 0.12
EXPLORATION_MIN_POPULATION = 10
DEFAULT_OPTION_RISK_FREE_RATE = 0.045
MIN_OPTION_MODEL_DTE_DAYS = 1
MIN_OPTION_MODEL_IV = 0.0001
OPTION_QUALITY_MID_CAUTION_RELATIVE_DIFF = 0.10
OPTION_QUALITY_MID_BAD_RELATIVE_DIFF = 0.20
OPTION_QUALITY_IV_CAUTION_RELATIVE_DIFF = 0.15
OPTION_QUALITY_IV_BAD_RELATIVE_DIFF = 0.30
OPTION_QUALITY_DELTA_CAUTION_ABSOLUTE_DIFF = 0.07
OPTION_QUALITY_DELTA_BAD_ABSOLUTE_DIFF = 0.15
OPTION_PEER_CROSSCHECK_MAX_AGE_HOURS = 2.0
EXCEPTIONAL_CONVICTION_BAR = 78.0
RADAR_ALERT_DEDUP_HOURS = 72
RADAR_ALERT_TYPES = ("data_contract", "exceptional_conviction", "buy_under_hit")
DATA_CONTRACT_READY = "ready"
DATA_CONTRACT_REPAIR_REQUIRED = "repair_required"
SERVICE_BUG_TIER = "Service Bug"
SERVICE_REPAIR_JOB_ORDER = [
    "update_free_sources",
    "update_arco_data",
    "run_option_agents",
    "refresh_options_radar",
]
# An off-hours/pre-market chain pull can return strikes with zero bid/ask/mid for
# nearly every contract. Such a snapshot only yields option_features/candidates for
# the few contracts that carried a premium, collapsing a full radar to 1-2 names.
# We refuse to let a snapshot below this premium-coverage floor overwrite a
# healthier existing radar.
MIN_SNAPSHOT_PREMIUM_COVERAGE = 0.5

DEFAULT_STRATEGY_PARAMETERS: dict[str, Any] = {
    "strategy_name": "leap_10x_reversal",
    "strategy_family": "leap_10x_reversal",
    "version": 1,
    "option_type": "call",
    "delta_min": 0.20,
    "delta_max": 0.45,
    "dte_min": 365,
    "dte_max": 900,
    "max_spread_pct": 0.25,
    "reject_spread_pct": 0.40,
    "min_open_interest": 100,
    "min_volume": 1,
    "max_required_move_pct": 3.50,
    "max_iv_percentile": 70.0,
    "reject_iv_percentile": 85.0,
    "require_price_above_ma50": True,
    "require_rs_improving": True,
    "fill_slippage_pct": 0.03,
}

SHORT_DATED_LOTTERY_COMMON_PARAMETERS: dict[str, Any] = {
    "delta_min": 0.01,
    "delta_max": 0.20,
    "dte_min": 2,
    "dte_max": 45,
    "max_spread_pct": 0.20,
    "max_required_move_pct": 5.0,
    "max_iv_percentile": 85.0,
    "reject_iv_percentile": 95.0,
    "require_price_above_ma50": False,
}

# Additional archetype families (Phase 3). Each is a full parameter set layered over
# the defaults; they register as 'forward_test' so they shadow-trade before earning UI
# prominence. Gate flags absent from the defaults (requires_catalyst,
# require_price_below_ma50, require_rs_deteriorating, max_iv_rv_ratio) keep the legacy
# LEAP behavior unchanged while making each family selective in its own way.
STRATEGY_FAMILY_PRESETS: dict[str, dict[str, Any]] = {
    "catalyst_call_v1": {
        **DEFAULT_STRATEGY_PARAMETERS,
        "strategy_name": "catalyst_call",
        "strategy_family": "catalyst_call",
        "option_type": "call",
        "delta_min": 0.25,
        "delta_max": 0.50,
        "dte_min": 45,
        "dte_max": 180,
        "max_required_move_pct": 1.20,
        "requires_catalyst": True,
        "max_iv_rv_ratio": 1.6,  # IV-crush guard: don't overpay for vol vs realized
        "require_price_above_ma50": True,
        "require_rs_improving": False,
    },
    "breakdown_put_v1": {
        **DEFAULT_STRATEGY_PARAMETERS,
        "strategy_name": "breakdown_put",
        "strategy_family": "breakdown_put",
        "option_type": "put",
        "delta_min": 0.25,
        "delta_max": 0.45,
        "dte_min": 90,
        "dte_max": 365,
        "max_required_move_pct": 3.50,
        "require_price_above_ma50": False,
        "require_rs_improving": False,
        "require_price_below_ma50": True,
        "require_rs_deteriorating": True,
    },
    # Two-leg vertical (long lower-strike call + short higher-strike call). It is threaded
    # through the pipeline as a synthetic single contract priced at the *net debit*
    # (see persist_spread_snapshots / build_spread_snapshot_row): the feature/EV math
    # treats it as a long call at the long strike, so its net delta is lower than a single
    # call and its tracked option_type is the synthetic marker "call_spread".
    "call_debit_spread_v1": {
        **DEFAULT_STRATEGY_PARAMETERS,
        "strategy_name": "call_debit_spread",
        "strategy_family": "call_debit_spread",
        "option_type": "call_spread",
        "delta_min": 0.10,
        "delta_max": 0.45,
        "dte_min": 60,
        "dte_max": 365,
        "max_required_move_pct": 3.50,
        "require_price_above_ma50": True,
        "require_rs_improving": False,
    },
    "short_dated_lottery_call_v1": {
        **DEFAULT_STRATEGY_PARAMETERS,
        "strategy_name": "short_dated_lottery_call",
        "strategy_family": "short_dated_lottery_call",
        "option_type": "call",
        **SHORT_DATED_LOTTERY_COMMON_PARAMETERS,
        "reject_spread_pct": 0.40,
        "min_open_interest": 100,
        "min_volume": 1,
        # Short-dated convexity can work before 20d relative strength confirms; keep
        # liquidity strict but do not force a LEAP-style RS reclaim gate.
        "require_rs_improving": False,
    },
    "short_dated_lottery_call_spread_v1": {
        **DEFAULT_STRATEGY_PARAMETERS,
        "strategy_name": "short_dated_lottery_call_spread",
        "strategy_family": "short_dated_lottery_call_spread",
        "option_type": "call_spread",
        **SHORT_DATED_LOTTERY_COMMON_PARAMETERS,
        "reject_spread_pct": 0.40,
        "min_open_interest": 100,
        "min_volume": 1,
        "require_rs_improving": False,
    },
    "deep_otm_lottery_call_v1": {
        **DEFAULT_STRATEGY_PARAMETERS,
        "strategy_name": "deep_otm_lottery_call",
        "strategy_family": "deep_otm_lottery_call",
        "option_type": "call",
        "delta_min": 0.05,
        "delta_max": 0.20,
        "dte_min": 365,
        "dte_max": 900,
        "max_spread_pct": 0.35,
        "reject_spread_pct": 0.55,
        "min_open_interest": 25,
        "min_volume": 0,
        "max_required_move_pct": 4.00,
        "max_iv_percentile": 85.0,
        "reject_iv_percentile": 95.0,
        "require_price_above_ma50": False,
        "require_rs_improving": True,
    },
}

# option_type markers that the radar treats as call-like (direction = +1) when building
# features and pricing — the real "call" plus the synthetic "call_spread" debit vertical.
CALL_LIKE_OPTION_TYPES = {"call", "call_spread"}

THEME_WATCH_KEYWORDS: dict[str, tuple[str, ...]] = {
    "theme_ai_infrastructure": (
        "artificial intelligence",
        " ai ",
        "semiconductor",
        "gpu",
        "accelerator",
        "data center",
        "datacenter",
        "cloud infrastructure",
        "networking",
        "memory",
        "foundry",
        "fabless",
        "electronic components",
    ),
    "theme_ai_applications": (
        "software",
        "application software",
        "cloud",
        "automation",
        "analytics",
        "cybersecurity",
        "digital advertising",
        "internet content",
    ),
    "theme_robotics_physical_ai": (
        "robotics",
        "robot",
        "humanoid",
        "autonomous",
        "autonomy",
        "physical ai",
        "industrial automation",
        "factory automation",
        "machine vision",
        "sensors",
        "actuator",
        "drones",
        "unmanned",
        "advanced manufacturing",
    ),
    "theme_space_tech": (
        "space",
        "aerospace",
        "satellite",
        "rocket",
        "defense",
        "orbital",
    ),
    "theme_ai_biotech": (
        "biotech",
        "biotechnology",
        "bioinformatics",
        "genomics",
        "life sciences",
        "drug discovery",
        "computational biology",
        "precision medicine",
    ),
    "theme_crypto_infrastructure": (
        "crypto",
        "cryptocurrency",
        "bitcoin",
        "blockchain",
        "digital assets",
        "coinbase",
        "mining",
    ),
}
