from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest

from investment_panel.core.phase2 import (
    FIELD_CONTRACTS,
    PITObservation,
    Phase2Status,
    assess_crypto_venue_data,
    assess_option_oi_volume_sla,
    build_coverage_vector,
    build_market_state_posterior,
    build_scenario_paths,
    parse_coinmetrics_derivatives,
    parse_corporate_expectations,
    parse_event_consensus,
    parse_fred_alfred,
    parse_treasury_yield_curve,
    posterior_can_influence_rank,
    replay_scenario_path,
    select_point_in_time,
    source_status,
)


AS_OF = datetime(2026, 9, 2, 14, tzinfo=UTC)


def observation(key: str, field: str, value: float, *, available_at: datetime = AS_OF, observed_at: datetime = AS_OF - timedelta(days=1), source: str = "treasury", status: Phase2Status = Phase2Status.AVAILABLE) -> PITObservation:
    return PITObservation(
        observation_id=key, field_name=field, dimension="rates", asset_class="rates", source_id=source,
        source_version="fixture.v1", value=value, observed_at=observed_at,
        available_at=available_at, status=status,
    )


def test_p2_a01_contracts_and_pit_clock_are_canonical() -> None:
    assert set(("macro.value", "rates.nominal_yield", "rates.real_yield", "credit.spread", "event.actual", "event.consensus", "event.surprise", "event.revision", "corporate.expected", "crypto.venue_derivatives", "option.open_interest", "option.volume", "positioning.flow")) <= FIELD_CONTRACTS.keys()
    selected = select_point_in_time([observation("old", "rates.nominal_yield", 3), observation("future", "rates.nominal_yield", 4, available_at=AS_OF + timedelta(minutes=1))], AS_OF)
    assert [row.observation_id for row in selected.selected] == ["old"]


def test_p2_a02_missing_credentials_never_make_fake_rows() -> None:
    result = parse_fred_alfred({"observations": [{"series_id": "GDP", "date": "2026-01-01", "value": "1", "vintage_at": "2026-02-01"}]}, env={})
    assert result.status is Phase2Status.MISSING_SOURCE
    assert result.observations == ()
    assert source_status("short_interest") is Phase2Status.UNSUPPORTED
    assert source_status("fred", env={"FRED_API_KEY": "present"}, has_history=False) is Phase2Status.MISSING_HISTORY


def test_p2_a01_public_treasury_and_credentialed_corporate_seams_keep_clocks() -> None:
    treasury = parse_treasury_yield_curve({"observations": [{"date": AS_OF.isoformat(), "available_at": AS_OF.isoformat(), "tenor": "10Y", "value": "4.0"}]})
    corporate = parse_corporate_expectations({"observations": [{"period": "2026-Q3", "period_end": "2026-09-30", "publication_at": AS_OF.isoformat(), "available_at": AS_OF.isoformat(), "expected": "2.1"}]}, env={"ALPHAVANTAGE_API_KEY": "present"})
    assert treasury.status is Phase2Status.AVAILABLE
    assert corporate.status is Phase2Status.AVAILABLE
    assert treasury.observations[0].publication_at is None
    assert corporate.observations[0].publication_at == AS_OF
    assert parse_treasury_yield_curve({"observations": [{"date": AS_OF.date().isoformat(), "value": "4.0"}]}).status is Phase2Status.MISSING_HISTORY
    assert parse_treasury_yield_curve({"observations": [{"date": AS_OF.isoformat(), "available_at": "2026-09-02T14:00:00", "value": "4.0"}]}).observations == ()


def test_p2_a03_coverage_is_per_expression_and_fails_closed() -> None:
    vector = build_coverage_vector(AS_OF, {"trend": {"stock": ("rates.nominal_yield",)}, "options": {"positioning": ("option.open_interest", "option.volume")}}, [observation("rate", "rates.nominal_yield", 3)])
    rows = {(row.strategy, row.expression): row for row in vector.rows}
    assert rows["trend", "stock"].status is Phase2Status.AVAILABLE
    assert rows["options", "positioning"].status is Phase2Status.MISSING_HISTORY
    positioning = build_coverage_vector(AS_OF, {"stock": {"flow": ("positioning.flow",)}}, [observation("flow", "positioning.flow", 1, source="sec_13f")])
    assert positioning.rows[0].status is Phase2Status.AVAILABLE


def test_p2_a04_event_keeps_actual_consensus_surprise_revision_and_clocks() -> None:
    result = parse_event_consensus({"observations": [{"event_id": "cpi", "event_at": AS_OF.isoformat(), "available_at": AS_OF.isoformat(), "release_at": AS_OF.isoformat(), "actual": 3.2, "consensus": 3.0, "revision": -0.1}]}, env={"TRADING_ECONOMICS_API_KEY": "present"})
    event = result.observations[0]
    assert result.status is Phase2Status.AVAILABLE
    assert (event.actual, event.consensus, event.surprise, event.revision) == (3.2, 3.0, 0.2, -0.1)
    assert event.release_at == AS_OF
    with pytest.raises(ValueError):
        PITObservation.model_validate({**observation("bad", "rates.nominal_yield", 3).model_dump(), "received_at": AS_OF})


def test_p2_a05_option_sla_blocks_until_oi_and_volume_coverage() -> None:
    incomplete = assess_option_oi_volume_sla([{"open_interest": 10, "volume": 1}, {"open_interest": 10, "volume": None}])
    complete = assess_option_oi_volume_sla([{"open_interest": 10, "volume": 1}] * 50 + [{"open_interest": None, "volume": 1}])
    assert incomplete["status"] == "MISSING_HISTORY" and not incomplete["positioning_allowed"]
    assert complete["status"] == "AVAILABLE" and complete["positioning_allowed"]


def test_p2_a06_crypto_requires_venue_identity_and_depth() -> None:
    assert assess_crypto_venue_data([{"symbol": "BTC-PERP", "depth": 1000}][0:1])["executable"] is False
    assert assess_crypto_venue_data([{"venue": "venue-a", "instrument": "BTC-PERP", "depth": -1}])["executable"] is False
    result = parse_coinmetrics_derivatives({"observations": [{"venue": "venue-a", "instrument": "BTC-PERP", "observed_at": AS_OF.isoformat(), "available_at": AS_OF.isoformat(), "depth_usd": 1000, "funding": 0.001}]}, env={"COINMETRICS_API_KEY": "present"})
    assert result.status is Phase2Status.AVAILABLE and result.observations[0].metadata["venue"] == "venue-a"


def test_p2_a07_a08_posterior_is_deterministic_uncertain_and_advisory() -> None:
    rows = [observation("n", "macro.value", -1, source="fred", observed_at=AS_OF - timedelta(days=2)), observation("p", "macro.value", 1, source="fred")]
    left = build_market_state_posterior(rows, as_of=AS_OF)
    right = build_market_state_posterior(list(reversed(rows)), as_of=AS_OF)
    assert left.posterior_id == right.posterior_id
    assert left.entropy is not None and left.entropy != 1.0 and left.missingness == 0.0
    assert left.baseline["method"] == "observable-frequency.v1"
    assert left.challenger["method"] == "hmm-noisy-emission.v1"
    assert left.dimensions["rates"].transition_probabilities["positive"]["positive"] == 0.8
    assert 0.0 < left.dimensions["rates"].change_point_probability < 1.0
    assert not posterior_can_influence_rank(left)
    promoted = left.model_copy(update={
        "advisory_only": False,
        "rank_authorized": True,
        "incremental_oos_net_utility": 0.1,
        "phase1_evidence_verified": True,
        "phase1_evidence_id": "phase1-evidence",
        "phase1_evidence_hash": "phase1-hash",
    })
    assert not posterior_can_influence_rank(promoted)
    assert not posterior_can_influence_rank(promoted, phase1_evidence={"verified": True, "evidence_id": "wrong", "evidence_hash": "phase1-hash"})
    assert not posterior_can_influence_rank(promoted, phase1_evidence={"verified": True, "evidence_id": "phase1-evidence", "evidence_hash": "phase1-hash"})


def test_p2_a02_unsupported_is_never_pit_selected_or_safe() -> None:
    unsupported = observation("unsupported", "macro.value", 1, source="short_interest", status=Phase2Status.UNSUPPORTED)
    selection = select_point_in_time([unsupported], AS_OF, fields=("macro.value",))
    vector = build_coverage_vector(AS_OF, {"x": {"macro": ("macro.value",)}}, [unsupported])
    assert not selection.selected and selection.missing_fields == ("macro.value",)
    assert vector.rows[0].status is Phase2Status.MISSING_HISTORY
    assert not vector.rows[0].point_in_time_safe
    assert source_status("coingecko") is Phase2Status.UNSUPPORTED


def test_p2_a10_mixed_fallback_and_missing_source_never_reports_available() -> None:
    posterior = build_market_state_posterior(
        [observation("fallback", "macro.value", 1, source="treasury", status=Phase2Status.FALLBACK)],
        as_of=AS_OF,
        source_statuses={"fred": Phase2Status.MISSING_SOURCE, "treasury": Phase2Status.AVAILABLE},
    )
    assert posterior.status is Phase2Status.MISSING_SOURCE
    assert posterior.missingness > 0
    assert posterior.baseline["degraded_source_count"] == 1


class _AdversarialConnection:
    def execute(self, _query: str, _params: list[str]) -> "_AdversarialConnection":
        return self

    def fetchone(self) -> dict[str, object]:
        return {
            "result_kind": "negative_controls",
            "input_hash": "phase1-hash",
            "trial_input_hash": "phase1-hash",
            "metrics": {"lower_confidence_net_utility_after_costs": 999.0},
            "outcome": {},
            "strategy_revision_id": 1,
            "strategy_key": "forged-strategy",
            "strategy_status": "active",
            "complete": True,
        }


class _AdversarialRuntime:
    @contextmanager
    def read(self):
        yield _AdversarialConnection()


def test_phase1_rank_authorization_rejects_forged_runtime_evidence() -> None:
    posterior = build_market_state_posterior([observation("a", "macro.value", 1)], as_of=AS_OF).model_copy(update={
        "advisory_only": False, "rank_authorized": True, "incremental_oos_net_utility": 999.0,
        "phase1_evidence_verified": True, "phase1_evidence_id": "forged", "phase1_evidence_hash": "phase1-hash",
    })
    assert not posterior_can_influence_rank(posterior, runtime=_AdversarialRuntime())


def test_missing_source_and_history_are_never_selected_and_explain_coverage() -> None:
    rows = [
        observation("missing-source", "macro.value", 1, source="fred", status=Phase2Status.MISSING_SOURCE),
        observation("missing-history", "rates.nominal_yield", 1, source="treasury", status=Phase2Status.MISSING_HISTORY),
    ]
    selection = select_point_in_time(rows, AS_OF, fields=("macro.value", "rates.nominal_yield"))
    vector = build_coverage_vector(AS_OF, {"x": {"all": ("macro.value", "rates.nominal_yield")}}, rows)
    assert selection.selected == ()
    assert vector.rows[0].status is Phase2Status.MISSING_HISTORY
    assert "source_missing_source:fred" in vector.rows[0].blockers
    assert "source_missing_history:treasury" in vector.rows[0].blockers
    assert not vector.rows[0].point_in_time_safe


def test_p2_a10_lifecycle_removal_uses_fallback_with_confidence_haircut() -> None:
    rows = [
        observation("fred-row", "macro.value", 1, source="fred"),
        observation("treasury-row", "macro.value", 1, source="treasury"),
    ]
    selection = select_point_in_time(rows, AS_OF, source_lifecycle={
        "fred": {"enabled": False, "operational_state": "archived"},
        "treasury": {"enabled": True, "operational_state": "active"},
    })
    assert selection.selected[0].source_id == "treasury"
    assert selection.selected[0].status is Phase2Status.FALLBACK
    assert selection.selected[0].confidence == 0.75


def test_p2_a10_conflict_and_fallback_are_truthful() -> None:
    conflict = select_point_in_time([observation("a", "rates.nominal_yield", 3), observation("b", "rates.nominal_yield", 4)], AS_OF)
    assert conflict.conflicts and not conflict.selected
    explicit_conflict = build_coverage_vector(AS_OF, {"x": {"rate": ("rates.nominal_yield",)}}, [observation("c", "rates.nominal_yield", 3, status=Phase2Status.CONFLICTED)])
    assert explicit_conflict.rows[0].status is Phase2Status.CONFLICTED
    fallback = observation("fallback", "rates.nominal_yield", 3, source="treasury", status=Phase2Status.FALLBACK)
    vector = build_coverage_vector(AS_OF, {"x": {"rate": ("rates.nominal_yield",)}}, [fallback])
    assert vector.rows[0].status is Phase2Status.FALLBACK
    assert vector.rows[0].confidence == 0.75
    assert "fallback_source_confidence_haircut" in vector.rows[0].blockers


def test_p2_a11_scenario_hash_replays_and_tampering_fails() -> None:
    posterior = build_market_state_posterior([observation("a", "macro.value", 1), observation("b", "macro.value", 1)], as_of=AS_OF)
    path = build_scenario_paths("snapshot-1", posterior)[0]
    assert replay_scenario_path(path)
    assert not replay_scenario_path(path.model_copy(update={"scenario_hash": "tampered"}))
    assert not replay_scenario_path(path.model_copy(update={"path_id": "tampered"}))
    assert not replay_scenario_path(path.model_copy(update={"parent_snapshot_id": "other-snapshot"}))
