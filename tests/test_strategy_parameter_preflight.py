"""Configuration failures are not failed trades or independent learning samples."""
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from investment_panel.infrastructure.postgres.strategy_parameters import parameter_preflight, mutation_capability
from investment_panel.infrastructure.postgres.strategy_learning import StrategyLearningRepository
from investment_panel.infrastructure.postgres.strategy_learning import OPTIONS_IMPLEMENTATION_ID, OPTIONS_IMPLEMENTATION_VERSION

BASE = {"gates": {"min_dte": 14, "max_dte": 900, "delta_min": .1, "delta_max": .9,
                  "min_open_interest": 100, "max_spread_pct": .2}}


@pytest.mark.parametrize("changes", [
    {"min_dte": 30}, {"dte_min": 30}, {"gates": {"min_dte": 30}},
    {"gates": {"min_dte": 30}, "min_dte": 2},
    {"gates": {"dte_min": 30}, "min_dte": 2},
])
def test_nested_and_flat_aliases_have_one_strictest_meaning(changes):
    result = parameter_preflight(BASE, changes)
    assert result["status"] == "supported"
    assert result["parameters"]["gates"]["min_dte"] == 30
    assert mutation_capability(BASE, changes)["blocking_verdict"] is None


@pytest.mark.parametrize("changes", [
    {"min_dte": 1.1}, {"min_volume": -1}, {"delta_max": 40}, {"delta_min": .95},
    {"max_iv_percentile": 101}, {"max_spread_pct": True}, {"max_spread_pct": float("nan")},
    {"gates": "bad"}, {"min_dte": 901}, {"min_open_interest": float("inf")},
])
def test_invalid_units_or_impossible_gates_are_actionable_preflight_failures(changes):
    result = parameter_preflight(BASE, changes)
    assert result["status"] == "invalid_parameters"
    assert result["errors"]


def test_unknown_feature_is_not_silently_added_to_evaluator_capabilities():
    result = parameter_preflight(BASE, {"gates": {"require_magic_alpha": True}})
    assert result["status"] == "unsupported_parameters"
    assert result["blocked_parameters"] == ["require_magic_alpha"]


def test_loosened_gate_still_requires_rejected_or_shadow_evidence():
    assert parameter_preflight(BASE, {"min_dte": 2})["status"] == "requires_rejected_or_shadow_outcomes"


def test_invalid_parent_gate_is_not_hidden_by_an_unrelated_valid_mutation():
    base = deepcopy(BASE)
    base["gates"]["max_spread_pct"] = float("nan")
    assert parameter_preflight(base, {"min_dte": 30})["status"] == "invalid_parameters"


def test_preflight_failure_is_recorded_once_and_does_not_query_outcomes(monkeypatch):
    proposal = {"id": "proposal", "created_at": datetime.now(UTC),
                "result": {"candidate_revision_id": 7, "proposed_parameter_changes": {"magic": 1}},
                "validation": {}}
    candidate = {"parameters": BASE, "base_parameters": BASE, "supersedes_id": 1,
                 "implementation_id": OPTIONS_IMPLEMENTATION_ID,
                 "implementation_version": OPTIONS_IMPLEMENTATION_VERSION}
    writes = []
    class Connection:
        def execute(self, sql, params=None):
            if "SELECT id, created_at, result, validation" in sql:
                return SimpleNamespace(fetchone=lambda: proposal)
            if "SELECT candidate.parameters" in sql:
                return SimpleNamespace(fetchone=lambda: candidate)
            if "UPDATE analysis.agent_task" in sql:
                proposal["validation"] = params[1].obj
                return SimpleNamespace(rowcount=1)
            raise AssertionError("Unexpected outcome read or configuration write: " + sql)
    repo = StrategyLearningRepository(object())
    monkeypatch.setattr(repo, "_store_evaluation", lambda *args, **kwargs: writes.append(args))
    for _ in range(3):
        assert repo._evaluate(Connection(), "proposal") == {"strategy_backtests": 0, "strategy_forward_tests": 0}
    assert len(writes) == 1
    assert proposal["validation"]["preflight"]["status"] == "unsupported_parameters"
