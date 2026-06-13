"""Radar alerts: evaluation, dedupe, and acknowledgement (Phase 2d)."""

from __future__ import annotations

from investment_panel.core.options_radar import (
    DEFAULT_STRATEGY_VERSION,
    acknowledge_radar_alert,
    build_radar_alerts,
    refresh_radar_alerts,
)


def _opp(**overrides) -> dict:
    opp = {
        "ticker": "NVDA",
        "primary_contract_id": "NVDA_C1",
        "primary_event_id": "ev1",
        "primary_state": "FIRE",
        "premium_mid": 4.0,
        "buy_under": 6.0,
        "conviction_score": 81.0,
    }
    opp.update(overrides)
    return opp


def test_build_alerts_fires_expected_types():
    alerts = build_radar_alerts([_opp()], {"NVDA_C1": 2.5}, set())
    types = {a["alert_type"] for a in alerts}
    assert "premium_below_buy_under" in types  # premium 4 < buy_under 6
    assert "exceptional_conviction" in types   # conviction 81 >= 78, FIRE
    assert "flow_oi_spike" in types            # oi z 2.5 >= 2


def test_build_alerts_dedupes_against_open_keys():
    existing = {("premium_below_buy_under", "NVDA_C1")}
    alerts = build_radar_alerts([_opp(conviction_score=50.0)], {}, existing)
    # The premium alert is already open; nothing new emitted.
    assert alerts == []


def test_no_alert_when_premium_above_buy_under():
    alerts = build_radar_alerts([_opp(premium_mid=9.0, conviction_score=50.0)], {}, set())
    assert all(a["alert_type"] != "premium_below_buy_under" for a in alerts)


def test_refresh_and_ack_end_to_end(tmp_path):
    from investment_panel.core.db import db, init_db, query_rows

    init_db(tmp_path / "a.duckdb")
    with db(tmp_path / "a.duckdb") as con:
        con.execute(
            "INSERT INTO option_radar_opportunity "
            "(opportunity_id, ticker, strategy_version, primary_contract_id, primary_event_id, primary_state, premium_mid, buy_under, conviction_score) "
            "VALUES ('o1', 'NVDA', ?, 'NVDA_C1', 'ev1', 'FIRE', 4.0, 6.0, 82.0)",
            [DEFAULT_STRATEGY_VERSION],
        )
        first = refresh_radar_alerts(con, strategy_version=DEFAULT_STRATEGY_VERSION)
        # Second run must not duplicate the still-open alerts.
        second = refresh_radar_alerts(con, strategy_version=DEFAULT_STRATEGY_VERSION)
        rows = query_rows(con, "SELECT alert_id, acknowledged_at FROM radar_alert")
        acked = acknowledge_radar_alert(con, rows[0]["alert_id"])
        missing = acknowledge_radar_alert(con, "does-not-exist")

    assert first >= 2 and second == 0  # deduped on re-run
    assert acked == 1 and missing == 0
