from investment_panel.api.routers.paper import _strategy_lane


def test_strategy_lane_preserves_misconfigured_status_for_list_evidence():
    lane = _strategy_lane(
        active=None,
        challenger={
            "authority_group": "options-radar-core",
            "strategy_revision_id": 1,
            "evaluations": [{
                "evaluation_type": "walk_forward",
                "verdict": "implementation_version_mismatch",
                "evidence": [],
                "metrics": [],
            }],
        },
        performance={"counts": {"filled_orders": 0}, "missing_evidence_reasons": []},
        auto_promotion=False,
    )

    assert lane["status"] == "misconfigured"
    assert "implementation_version_mismatch" in lane["blockers"]
    assert lane["progress"]["historical_completed"] == 0
