"""Configuration constants for the deterministic options radar."""

from __future__ import annotations

from typing import Any


DEFAULT_STRATEGY_VERSION = "leap_10x_reversal_v1"
MIN_FORWARD_TEST_DAYS = 30
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
# An off-hours/pre-market chain pull can return strikes with zero bid/ask/mid for
# nearly every contract. Such a snapshot only yields option_features/candidates for
# the few contracts that carried a premium, collapsing a full radar to 1-2 names.
# We refuse to let a snapshot below this premium-coverage floor overwrite a
# healthier existing radar.
MIN_SNAPSHOT_PREMIUM_COVERAGE = 0.5
SERVICE_BUG_TIER = "Service Bug"
SERVICE_REPAIR_JOB_ORDER = [
    "update_free_sources",
    "update_arco_data",
    "run_option_agents",
    "refresh_options_radar",
]

DEFAULT_STRATEGY_PARAMETERS: dict[str, Any] = {
    "strategy_name": "leap_10x_reversal",
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
