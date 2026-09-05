from __future__ import annotations

from investment_panel.core import job_policy
from investment_panel.core.refresh_jobs import ALLOWLIST


def test_every_refresh_runner_has_canonical_job_definition() -> None:
    assert set(ALLOWLIST) == set(job_policy.JOB_DEFINITIONS)


def test_source_health_job_routing_uses_allowlisted_job_names() -> None:
    routed = job_policy.source_refresh_job_names()
    assert routed
    assert routed <= set(ALLOWLIST)


def test_option_freshness_policy_is_owned_by_canonical_job_policy() -> None:
    definition = job_policy.job_definition("options_radar_hard_refresh")
    assert definition.freshness_seconds == 259200
    assert definition.name in ALLOWLIST


def test_mungermode_freshness_policy_is_owned_by_canonical_job_policy() -> None:
    definition = job_policy.job_definition("update_market_valuations")
    assert definition.freshness_seconds == 86400
    assert definition.name in ALLOWLIST


def test_source_writers_wait_one_interval_before_first_run() -> None:
    assert job_policy.initial_delay_seconds("options_radar_hard_refresh", 900, 0) == 900
    assert job_policy.initial_delay_seconds("update_robinhood_options", 120, 1) == 120
    assert job_policy.initial_delay_seconds("refresh_options_radar_signal_robinhood", 60, 2) == 10


def test_timeout_override_precedes_job_default() -> None:
    assert job_policy.job_timeout_seconds("options_radar_hard_refresh", {}) == 5400
    assert job_policy.job_timeout_seconds("options_radar_hard_refresh", {"options_radar_hard_refresh": 12}) == 12


def test_outcome_refresh_timeout_matches_job_database_profile() -> None:
    assert job_policy.job_timeout_seconds("refresh_symbol_decision_outcomes", {}) == 900
