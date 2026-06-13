"""Free flow-anomaly features: OI expansion + volume/OI proxy (Phase 1b)."""

from __future__ import annotations

from investment_panel.core.options_radar import (
    build_option_flow_feature,
    refresh_option_flow_features,
)


def _snap(day: int, oi: float, volume: float | None = None, *, contract: str = "NVDA C", ticker: str = "NVDA") -> dict:
    return {
        "snapshot_time": f"2026-06-{day:02d}T14:00:00",
        "contract_id": contract,
        "ticker": ticker,
        "option_type": "call",
        "open_interest": oi,
        "volume": volume,
        "mid": 5.0,
    }


def test_oi_expansion_spike_scores_high():
    # Flat OI for 6 days, then a sharp jump on day 7 -> high z-score, high flow_score.
    history = [_snap(d, oi=1000 + d) for d in range(1, 7)]
    history.append(_snap(7, oi=4000))
    feature = build_option_flow_feature(history)
    assert feature is not None
    assert feature["oi_change_1d"] > 2000
    assert feature["oi_zscore_20d"] is not None and feature["oi_zscore_20d"] >= 2.0
    assert feature["flow_score"] is not None and feature["flow_score"] >= 40.0


def test_flat_oi_scores_low():
    history = [_snap(d, oi=1000 + d) for d in range(1, 8)]
    feature = build_option_flow_feature(history)
    assert feature is not None
    assert feature["flow_score"] is not None and feature["flow_score"] < 40.0


def test_volume_oi_ratio_and_intraday_snapshots_collapse():
    # Two snapshots on the same day (<18h apart) must not produce a settled delta.
    history = [
        _snap(1, oi=1000, volume=10),
        {"snapshot_time": "2026-06-01T15:00:00", "contract_id": "NVDA C", "ticker": "NVDA", "option_type": "call", "open_interest": 1000, "volume": 20, "mid": 5.0},
        _snap(2, oi=1500, volume=2000),
    ]
    feature = build_option_flow_feature(history)
    assert feature is not None
    # Only one settled day-over-day delta (day1 -> day2), intraday pair collapsed.
    assert feature["raw"]["settled_deltas"] == 1
    assert feature["volume_oi_ratio"] == round(2000 / 1500, 4)


def test_refresh_persists_flow_rows_and_ticker_aggregate(tmp_path):
    from investment_panel.core.db import db, init_db, query_rows

    init_db(tmp_path / "f.duckdb")
    with db(tmp_path / "f.duckdb") as con:
        for d in range(1, 7):
            con.execute(
                "INSERT INTO option_snapshot (snapshot_time, ticker, contract_id, option_type, open_interest, volume, mid, data_source) "
                "VALUES (?, 'NVDA', 'NVDA_C1', 'call', ?, 100, 5.0, 'ibkr')",
                [f"2026-06-{d:02d}T14:00:00", 1000 + d],
            )
        con.execute(
            "INSERT INTO option_snapshot (snapshot_time, ticker, contract_id, option_type, open_interest, volume, mid, data_source) "
            "VALUES ('2026-06-07T14:00:00', 'NVDA', 'NVDA_C1', 'call', 5000, 8000, 5.0, 'ibkr')"
        )
        written = refresh_option_flow_features(con)
        rows = query_rows(con, "SELECT * FROM option_flow_features WHERE contract_id = 'NVDA_C1'")

    assert written == 1
    assert rows[0]["flow_score"] is not None and rows[0]["flow_score"] > 0
    assert rows[0]["ticker_call_oi_delta_1d"] is not None
