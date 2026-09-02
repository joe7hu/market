from investment_panel.core.strategy_factory import (
    MECHANISM_CLASSES,
    StrategySpec,
    default_strategy_registry,
    full_denominator_complete,
    manifest_hash,
    monitoring_complete,
)


def test_p3_a01_four_mechanism_classes_are_registered() -> None:
    registry = default_strategy_registry()
    assert {spec.mechanism_class for spec in registry.all()} >= set(MECHANISM_CLASSES)


def test_p3_a02_every_revision_has_mechanism_and_falsification() -> None:
    assert all(spec.economic_mechanism and spec.falsification_rule for spec in default_strategy_registry().all())


def test_p3_a03_manifests_are_complete_and_content_addressed() -> None:
    for spec in default_strategy_registry().all():
        assert set(spec.manifest) == {"source", "data", "cost", "capacity", "failure"}
        assert manifest_hash(spec.manifest) == manifest_hash(dict(spec.manifest))


def test_p3_a04_classics_use_normal_promotability_and_martingale_is_negative_control() -> None:
    registry = default_strategy_registry()
    assert registry.resolve("classic_momentum_v1").promotability == "standard"
    martingale = registry.resolve("martingale_v1")
    assert martingale.promotability == "negative_control"
    assert registry.forecast("martingale_v1", {}).status == "blocked"


def test_p3_a05_keys_are_versioned_and_resolvable() -> None:
    registry = default_strategy_registry()
    assert all(spec.strategy_key.endswith(f"_v{spec.revision}") and registry.resolve(spec.strategy_key) == spec for spec in registry.all())


def test_p3_a06_full_denominator_requires_outcomes_for_every_member() -> None:
    rows = [{"instrument_id": "1", "outcome": {"net_return": 0.1}}, {"instrument_id": "2", "outcome": {"net_return": -0.1}}]
    assert full_denominator_complete(("1", "2"), rows)
    assert not full_denominator_complete(("1", "2", "3"), rows)
    assert not full_denominator_complete(("1", "2"), [{"instrument_id": "1", "outcome": {}}])


def test_p3_a07_active_evidence_requires_all_monitoring_dimensions() -> None:
    assert monitoring_complete(("correlation", "tail_correlation", "crowding", "capacity", "decay", "regime"))
    assert not monitoring_complete(("correlation", "capacity"))


def test_p3_a08_flow_replica_is_exposure_sleeve() -> None:
    assert default_strategy_registry().resolve("structural_flow_v1").promotability == "exposure_sleeve"


def test_p3_a09_similar_strategies_have_distinct_versioned_definitions() -> None:
    registry = default_strategy_registry()
    left, right = registry.resolve("classic_momentum_v1"), registry.resolve("daily_trend_underreaction_v1")
    assert left.source_definition_version != right.source_definition_version
    assert left.falsification_rule != right.falsification_rule


def test_p3_a10_daily_only_families_do_not_claim_intraday_actionability() -> None:
    registry = default_strategy_registry()
    assert registry.resolve("daily_event_propagation_v1").actionability == "shadow_only"
    assert registry.forecast("daily_event_propagation_v1", {"event": {"release_at": "2026-09-02T12:00:00Z", "actual": 3.2, "consensus": 3.0}}).actionability == "shadow_only"


def test_p3_a11_crypto_registration_is_blocked_without_venue_controls() -> None:
    spec = default_strategy_registry().resolve("crypto_funding_basis_v1")
    signal = default_strategy_registry().forecast(spec.strategy_key, {})
    assert spec.promotability == "registration_only"
    assert set(signal.blockers) == {"venue_identity_required", "executable_depth_required", "liquidation_data_required", "failure_scenarios_required"}


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
