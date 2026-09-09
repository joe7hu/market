import pytest

from investment_panel.domain.strategies.catalog import (
    MECHANISM_CLASSES,
    StrategySpec,
    default_strategy_definitions,
    daily_trend_underreaction,
    evaluate_strategy,
    event_propagation,
    full_denominator_complete,
    manifest_hash,
    monitoring_complete,
    options_recovery_v2,
    resolve_builtin_strategy,
)


def test_p3_a01_four_mechanism_classes_are_registered() -> None:
    definitions = default_strategy_definitions()
    assert {spec.mechanism_class for spec in definitions} >= set(MECHANISM_CLASSES)


def test_p3_a02_every_revision_has_mechanism_and_falsification() -> None:
    assert all(spec.economic_mechanism and spec.falsification_rule for spec in default_strategy_definitions())


def test_p3_a03_manifests_are_complete_and_content_addressed() -> None:
    for spec in default_strategy_definitions():
        assert set(spec.manifest) == {"source", "data", "cost", "capacity", "failure"}
        assert manifest_hash(spec.manifest) == manifest_hash(dict(spec.manifest))


def test_p3_a04_classics_use_normal_promotability_and_martingale_is_negative_control() -> None:
    assert resolve_builtin_strategy("classic_momentum_v1").promotability == "standard"
    martingale = resolve_builtin_strategy("martingale_v1")
    assert martingale.promotability == "negative_control"
    assert evaluate_strategy(martingale, {}).status == "blocked"
    with pytest.raises(ValueError, match="Martingale"):
        StrategySpec(
            strategy_key="martingale_v2", revision=2, name="Martingale v2",
            mechanism_class="gap_regime", economic_mechanism="x", falsification_rule="x",
            source_definition_version="martingale.v2", manifest={key: {"x": 1} for key in ("source", "data", "cost", "capacity", "failure")},
            promotability="standard", actionability="daily_research",
        )
    with pytest.raises(ValueError, match="Martingale"):
        StrategySpec(
            strategy_key="ordinary_control_v1", revision=1, name="Ordinary control",
            mechanism_class="trend_underreaction", economic_mechanism="x", falsification_rule="x",
            source_definition_version="ordinary.v1", strategy_family="martingale_v2",
            manifest={key: {"x": 1} for key in ("source", "data", "cost", "capacity", "failure")},
            promotability="standard", actionability="daily_research",
        )


def test_p3_a05_keys_are_versioned_and_resolvable() -> None:
    assert all(spec.strategy_key.endswith(f"_v{spec.revision}") and resolve_builtin_strategy(spec.strategy_key) == spec for spec in default_strategy_definitions())


def test_options_recovery_defaults_to_its_registered_implementation_version() -> None:
    spec = StrategySpec(
        strategy_key="options_recovery_v2", revision=2, name="Options recovery v2",
        mechanism_class="options_recovery", economic_mechanism="x", falsification_rule="x",
        source_definition_version="options-recovery.v2",
        manifest={key: {"x": 1} for key in ("source", "data", "cost", "capacity", "failure")},
    )
    assert spec.implementation_id == "options_recovery"
    assert spec.implementation_version == "2"


def test_p3_a06_full_denominator_requires_outcomes_for_every_member() -> None:
    rows = [{"instrument_id": "1", "outcome": {"net_return": 0.1}}, {"instrument_id": "2", "outcome": {"net_return": -0.1}}]
    assert full_denominator_complete(("1", "2"), rows)
    assert not full_denominator_complete(("1", "2", "3"), rows)
    assert not full_denominator_complete(("1", "2"), [{"instrument_id": "1", "outcome": {}}])


def test_p3_a07_active_evidence_requires_all_monitoring_dimensions() -> None:
    assert monitoring_complete(("correlation", "tail_correlation", "crowding", "capacity", "decay", "regime"))
    assert not monitoring_complete(("correlation", "capacity"))


def test_p3_a08_flow_replica_is_exposure_sleeve() -> None:
    assert resolve_builtin_strategy("structural_flow_v1").promotability == "exposure_sleeve"


def test_p3_a09_similar_strategies_have_distinct_versioned_definitions() -> None:
    left, right = resolve_builtin_strategy("classic_momentum_v1"), resolve_builtin_strategy("daily_trend_underreaction_v1")
    assert left.source_definition_version != right.source_definition_version
    assert left.falsification_rule != right.falsification_rule


def test_p3_a10_daily_only_families_do_not_claim_intraday_actionability() -> None:
    spec = resolve_builtin_strategy("daily_event_propagation_v1")
    assert spec.actionability == "shadow_only"
    assert evaluate_strategy(spec, {"input_cutoff": "2026-09-02T13:00:00Z", "event": {"status": "confirmed", "confirmed": True, "disabled": False, "release_at": "2026-09-02T12:00:00Z", "observed_at": "2026-09-02T12:01:00Z", "available_at": "2026-09-02T12:01:00Z", "actual": 3.2, "consensus": 3.0}}).actionability == "shadow_only"


def test_p3_a11_crypto_registration_is_blocked_without_venue_controls() -> None:
    spec = resolve_builtin_strategy("crypto_funding_basis_v1")
    signal = evaluate_strategy(spec, {})
    assert spec.promotability == "registration_only"
    assert set(signal.blockers) == {"venue_identity_required", "executable_depth_required", "liquidation_data_required", "failure_scenarios_required"}


def test_p3_inputs_are_authoritative_pit_and_options_controls_are_typed() -> None:
    cutoff = "2026-09-02T13:00:00Z"
    future_daily = daily_trend_underreaction({
        "input_cutoff": cutoff,
        "daily_bars": [{"status": "confirmed", "confirmed": True, "disabled": False, "observed_at": "2026-09-03T13:00:00Z", "available_at": "2026-09-03T13:00:00Z", "close": 101}],
    })
    assert future_daily.status == "unavailable"
    invalid_event = event_propagation({
        "input_cutoff": cutoff,
        "event": {"status": "confirmed", "confirmed": True, "disabled": False, "release_at": "2026-09-02T14:00:00Z", "observed_at": "2026-09-02T14:00:00Z", "available_at": "2026-09-02T14:00:00Z", "actual": 3, "consensus": 2},
    })
    assert invalid_event.status == "unavailable"
    invalid_options = options_recovery_v2({
        "full_chain_state": {"ok": True}, "oi_volume_state": {"ok": True}, "dividend_state": {"ok": True},
        "quote_quality": True, "fill_model_proven": 1,
    })
    assert invalid_options.status == "unavailable"


@pytest.mark.parametrize("disabled", [None, "false", True, 0])
def test_p3_options_disabled_must_be_exact_false(disabled: object) -> None:
    state = {"status": "confirmed", "confirmed": True, "disabled": disabled,
             "observed_at": "2026-09-01T12:00:00Z", "available_at": "2026-09-01T12:00:00Z"}
    signal = options_recovery_v2({
        "input_cutoff": "2026-09-02T13:00:00Z",
        "full_chain_state": state, "oi_volume_state": state, "dividend_state": state,
        "quote_quality": 0.9, "fill_model_proven": True,
    })
    assert signal.status == "unavailable"


def test_p3_daily_and_event_numeric_fields_reject_boolean_values() -> None:
    cutoff = "2026-09-02T13:00:00Z"
    daily = daily_trend_underreaction({
        "input_cutoff": cutoff,
        "daily_bars": [
            {"status": "confirmed", "confirmed": True, "disabled": False, "observed_at": "2026-09-01T13:00:00Z", "available_at": "2026-09-01T13:00:00Z", "close": 100},
            {"status": "confirmed", "confirmed": True, "disabled": False, "observed_at": "2026-09-02T13:00:00Z", "available_at": "2026-09-02T13:00:00Z", "close": True},
        ],
    })
    assert daily.status == "unavailable"
    event = event_propagation({
        "input_cutoff": cutoff,
        "event": {"status": "confirmed", "confirmed": True, "disabled": False, "release_at": "2026-09-02T12:00:00Z", "observed_at": "2026-09-02T12:01:00Z", "available_at": "2026-09-02T12:01:00Z", "actual": True, "consensus": 2},
    })
    assert event.status == "unavailable"


def test_p3_actionability_is_a_closed_daily_enum() -> None:
    with pytest.raises(ValueError):
        StrategySpec(
            strategy_key="intraday_test_v1", revision=1, name="Intraday test",
            mechanism_class="trend_underreaction", economic_mechanism="x", falsification_rule="x",
            source_definition_version="intraday-test.v1", actionability="intraday",
            manifest={key: {"x": 1} for key in ("source", "data", "cost", "capacity", "failure")},
        )


def test_strategy_spec_rejects_unversioned_keys() -> None:
    try:
        StrategySpec(
            strategy_key="bad", revision=1, name="bad", mechanism_class="trend_underreaction",
            economic_mechanism="x", falsification_rule="x", source_definition_version="bad.v1",
            manifest={key: {"x": 1} for key in ("source", "data", "cost", "capacity", "failure")},
        )
    except ValueError as exc:
        assert "revision" in str(exc)
    else:
        raise AssertionError("unversioned strategy key was accepted")


def test_strategy_parameters_are_consumed_and_account_actionability_is_a_ceiling() -> None:
    inputs = {
        "input_cutoff": "2026-09-05T13:00:00Z",
        "daily_bars": [
            {"status": "confirmed", "confirmed": True, "disabled": False,
             "observed_at": f"2026-09-0{day}T12:00:00Z", "available_at": f"2026-09-0{day}T12:00:00Z",
             "trading_date": f"2026-09-0{day}", "close": close}
            for day, close in enumerate((100, 102, 108, 104), start=1)
        ],
    }
    base = resolve_builtin_strategy("daily_trend_underreaction_v1")
    short = evaluate_strategy(base.model_copy(update={"parameters": {"lookback_days": 1}}), inputs)
    long = evaluate_strategy(base.model_copy(update={"parameters": {"lookback_days": 3}}), inputs)
    assert short.evidence["lookback_days"] == 1
    assert long.evidence["lookback_days"] == 3
    assert short.value != long.value
    capped = evaluate_strategy(base, inputs, account_actionability="research_only")
    assert capped.actionability == "research_only"
    invalid = evaluate_strategy(base.model_copy(update={"parameters": {"lookback_days": 1, "unused": 2}}), inputs)
    assert invalid.status == "blocked"
    assert invalid.blockers == ("strategy_parameters_invalid",)


def test_unknown_implementation_and_handler_defaults_fail_closed() -> None:
    spec = resolve_builtin_strategy("daily_gap_regime_v1")
    unknown = evaluate_strategy(spec.model_copy(update={"implementation_id": "missing", "implementation_version": "1"}), {})
    assert unknown.status == "blocked"
    assert unknown.actionability == "registration_only"
    assert unknown.blockers == ("strategy_implementation_unavailable",)
