from investment_panel.database.strategy_parameters import (
    merge_strategy_parameters,
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
