from __future__ import annotations

from datetime import UTC, datetime, timedelta

from investment_panel.core.event_scout import (
    EventScout,
    build_event_decision_packet,
    build_options_decision_truth,
    latest_short_interest_snapshot,
    match_historical_cases,
)
from investment_panel.core.event_replays import replay_mrna


def test_short_interest_uses_latest_report_date_without_mixing_history() -> None:
    result = latest_short_interest_snapshot(
        [
            {"short_shares": 52_400_000, "record_date": "2026-07-15", "publish_date": "2026-07-25T13:00:00Z"},
            {"short_shares": 49_770_000, "record_date": "2026-07-31", "publish_date": "2026-07-31T13:00:00Z"},
        ],
        as_of="2026-07-31T14:32:00Z",
    )
    assert result["selected"]["short_shares"] == 49_770_000
    assert result["selected"]["record_date"] == "2026-07-31"
    assert len(result["history"]) == 2
    assert result["mixed_report_date"] is False


def test_mrna_replay_is_tactical_setup_but_not_a_short_or_fundamental_clear() -> None:
    packet = replay_mrna()
    assert packet["market_tape"]["latest_price"]["value"] == 153.44
    assert packet["market_tape"]["intraday_high"]["observed_at"] == packet["as_of"]
    assert packet["positioning"]["short_shares"]["value"] == 49_770_000
    assert packet["positioning"]["short_interest_record_date"]["value"] == "2026-07-31"
    assert packet["positioning"]["volume_exceeds_latest_reported_short_shares"]["value"] is True
    assert packet["tactical_decision"]["do_not_short"] is True
    assert packet["tactical_decision"]["stance"] == "event_driven_momentum_watch"
    assert packet["tactical_decision"]["paper_only_momentum_probe"]["eligible"] is False
    assert packet["fundamental_decision"]["underwriting_state"] == "UNUNDERWRITTEN"
    assert packet["fundamental_decision"]["not_bearish_by_missing_data"] is True
    assert packet["decision_truth"]["candidate_state"] == "SETUP"
    assert packet["decision_truth"]["route_verdict"] == "NO_TRADE"


def test_every_required_packet_category_has_timestamped_source_fields() -> None:
    packet = build_event_decision_packet(
        "ABC",
        as_of="2026-08-20T14:00:00Z",
        source_url="https://example.test/event",
        source_kind="official",
        market_tape={"latest_price": 10.0, "volume": 1000},
        positioning={"short_shares": 500},
        event_fundamentals={"trial_phase": "Phase 3"},
        platform_optionality={"read_through_to_other_trials": "inference"},
    )
    for category in ("market_tape", "positioning", "event_fundamentals", "platform_optionality"):
        for field in packet[category].values():
            if isinstance(field, dict) and "value" in field:
                assert {"observed_at", "record_date", "source_url", "source_kind", "freshness", "evidence_class"} <= set(field)
    assert packet["event_fundamentals"]["hazard_ratio"]["value"] is None
    assert packet["event_fundamentals"]["hazard_ratio"]["evidence_class"] == "missing"
    assert packet["positioning"]["gamma_inference"]["value"] is None


def test_history_horizon_mismatch_is_excluded_from_prediction() -> None:
    result = match_historical_cases(
        [
            {"id": "intraday", "forecast_horizon": "intraday", "event_kind": "clinical_announcement"},
            {"id": "monthly", "forecast_horizon": "monthly_yearly", "event_kind": "clinical_announcement"},
        ],
        {"forecast_horizon": "intraday", "event_kind": "clinical_announcement"},
    )
    assert [row["id"] for row in result["matched"]] == ["intraday"]
    assert result["excluded"][0]["id"] == "monthly"
    assert "forecast_horizon_mismatch" in result["excluded"][0]["excluded_reasons"]


def test_event_scout_limits_symbols_and_applies_thirty_minute_cooldown() -> None:
    scout = EventScout()
    base = datetime(2026, 8, 20, 14, 0, tzinfo=UTC)
    signals = [
        {"symbol": f"S{i}", "trigger_type": "abnormal_volume", "observed_at": base.isoformat()}
        for i in range(7)
    ]
    accepted = scout.accept_signals(signals, now=base)
    assert len(accepted) == 5
    assert scout.accept_signals([signals[0]], now=base + timedelta(minutes=29)) == []
    assert len(scout.accept_signals([signals[0]], now=base + timedelta(minutes=30))) == 1


def test_event_scout_options_collection_feeds_positioning_metrics() -> None:
    scout = EventScout()
    result = scout.process_signal(
        {
            "symbol": "ABC",
            "trigger_type": "abnormal_options_flow",
            "observed_at": "2026-08-20T14:00:00Z",
            "data": {"market_tape": {"latest_price": 10, "volume": 100}},
        },
        collectors={
            "options_chain": lambda _signal: {
                "put_call_volume": 0.4,
                "put_call_open_interest": 0.7,
                "volume_to_open_interest": 1.8,
            },
        },
        packet_builder=build_event_decision_packet,
        now="2026-08-20T14:00:00Z",
    )
    assert result["accepted"] is True
    positioning = result["packet"]["positioning"]
    assert positioning["put_call_volume"]["value"] == 0.4
    assert result["scout_event"]["collection_status"]["options_chain"]["status"] == "collected"


def test_options_truth_blocks_route_structure_and_execution_mismatch() -> None:
    truth = build_options_decision_truth({
        "symbol": "QQQ", "state": "READY", "structure": "long_call", "execution_ready": True,
        "strategy_route": {"route_version": "route-v3", "selected_structure": "long_put", "route_blockers": []},
        "data_readiness": "A",
    })
    assert truth["route_verdict"] == "NO_TRADE"
    assert truth["readiness_state"] == "incomplete"
    assert "route_structure_mismatch" in truth["blockers"]
