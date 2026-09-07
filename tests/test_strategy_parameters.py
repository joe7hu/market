from investment_panel.database.strategy_parameters import (
    merge_strategy_parameters,
    mutation_capability,
    normalize_gates,
)


def test_normalize_gates_collapses_legacy_aliases_to_strictest_limit() -> None:
    gates = normalize_gates(
        {
            "gates": {"max_spread_pct": 0.25, "max_iv_percentile": 90, "min_dte": 14},
            "reject_spread_pct": 0.05,
            "reject_iv_percentile": 70,
            "dte_min": 30,
        }
    )

    assert gates == {
        "max_spread_pct": 0.05,
        "max_iv_percentile": 70,
        "min_dte": 30,
    }


def test_merge_strategy_parameters_persists_one_canonical_gate_shape() -> None:
    merged = merge_strategy_parameters(
        {"reject_spread_pct": 0.25, "dte_min": 14, "feature_version": "v1"},
        {"reject_spread_pct": 0.05, "dte_min": 30},
    )

    assert merged == {
        "feature_version": "v1",
        "gates": {"max_spread_pct": 0.05, "min_dte": 30},
    }


def test_agent_schema_and_mutation_gate_share_the_runtime_parameters() -> None:
    from investment_panel.jobs.option_agent_contract import POSTMORTEM_SCHEMA
    from investment_panel.database.strategy_parameters import EVALUABLE_GATES

    schema = POSTMORTEM_SCHEMA["properties"]["proposed_parameter_changes"]
    assert set(schema["properties"]) == EVALUABLE_GATES
    base = {"gates": {"min_dte": 14, "max_spread_pct": .25}}
    assert mutation_capability(base, {"dte_min": 30})["blocking_verdict"] is None
    assert mutation_capability(base, {"require_rs_improving": True})["blocking_verdict"] == "unsupported_parameters"
    assert mutation_capability(base, {"min_dte": 2})["blocking_verdict"] == "requires_rejected_or_shadow_outcomes"
    assert mutation_capability(base, {"min_dte": 30, "max_risk_per_trade_pct": None})["blocking_verdict"] == "unsupported_parameters"
    assert merge_strategy_parameters(base, {"min_dte": None})["gates"] == base["gates"]
    for invalid in (True, float("nan"), float("inf"), -1):
        try:
            merge_strategy_parameters(base, {"min_dte": invalid})
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid gate accepted: {invalid}")
