from __future__ import annotations

from datetime import date
from pathlib import Path

from investment_panel.core.db import db, init_db, query_rows, upsert_instrument
from investment_panel.core.free_sources import store_options_chain
from investment_panel.core.ibkr_options import (
    chain_row,
    parse_option_ticks,
    pick_chain_param_set,
    select_leap_call_strikes,
    select_leap_expiries,
    select_strikes_around_spot,
)
from investment_panel.core.options_radar import persist_option_snapshots


def test_select_leap_expiries_filters_to_dte_window() -> None:
    today = date(2026, 6, 9)
    expirations = [
        "20260612",  # 3 dte - too soon
        "20270618",  # ~374 dte - in window
        "20271217",  # ~556 dte - in window
        "20281215",  # ~919 dte - too far
    ]
    out = select_leap_expiries(expirations, today=today, min_dte=365, max_dte=900, max_per_symbol=2)
    assert out == ["20270618", "20271217"]


def test_select_leap_expiries_ignores_bad_dates() -> None:
    today = date(2026, 6, 9)
    out = select_leap_expiries(["garbage", "20270618"], today=today, min_dte=365, max_dte=900, max_per_symbol=5)
    assert out == ["20270618"]


def test_select_strikes_around_spot_picks_nearest() -> None:
    strikes = [600.0, 700.0, 740.0, 745.0, 800.0, 900.0]
    out = select_strikes_around_spot(strikes, spot=742.0, count=3)
    assert out == [700.0, 740.0, 745.0]  # nearest 3, returned sorted


def test_select_strikes_around_spot_handles_unknown_spot() -> None:
    strikes = [10.0, 20.0, 30.0, 40.0, 50.0]
    out = select_strikes_around_spot(strikes, spot=None, count=2)
    assert len(out) == 2 and out == sorted(out)


def test_select_leap_call_strikes_targets_otm_band() -> None:
    # 10x LEAP calls want OTM strikes (delta ~0.20-0.45), not ATM. Spot 100 ->
    # band [100, 160]. ATM (100) and below should not dominate; picks stay OTM.
    strikes = [float(s) for s in range(50, 205, 5)]  # 50..200
    out = select_leap_call_strikes(strikes, spot=100.0, count=6)
    assert out == sorted(out)
    assert all(100.0 <= s <= 160.0 for s in out)  # within the OTM band
    assert len(out) == 6
    assert max(out) > 120.0  # genuinely reaches into OTM, not clustered at ATM


def test_select_leap_call_strikes_falls_back_when_band_empty() -> None:
    # No strikes in the OTM band -> fall back to nearest-spot rather than empty.
    strikes = [10.0, 20.0, 30.0]
    out = select_leap_call_strikes(strikes, spot=100.0, count=2)
    assert len(out) == 2


def test_pick_chain_param_set_prefers_real_class_over_adjusted() -> None:
    # The exact ambiguity that broke the probe: SMART exchange only exposed the
    # adjusted "2SPY" class with 2 strikes; the real chain is "SPY" with hundreds.
    param_sets = [
        {"exchange": "SMART", "tradingClass": "2SPY", "strikes": [587.0, 609.0]},
        {"exchange": "CBOE", "tradingClass": "SPY", "strikes": [float(s) for s in range(50, 1500, 5)]},
    ]
    chosen = pick_chain_param_set(param_sets, "SPY")
    assert chosen["tradingClass"] == "SPY"
    assert len(chosen["strikes"]) > 100


def test_pick_chain_param_set_falls_back_to_most_strikes() -> None:
    param_sets = [
        {"exchange": "SMART", "tradingClass": "WEEKLY", "strikes": [1.0, 2.0]},
        {"exchange": "CBOE", "tradingClass": "OTHER", "strikes": [1.0, 2.0, 3.0, 4.0]},
    ]
    chosen = pick_chain_param_set(param_sets, "XYZ")
    assert len(chosen["strikes"]) == 4


def test_parse_option_ticks_from_real_delayed_probe_data() -> None:
    # Exact payload returned by scripts/ibkr_market_data_probe.py for
    # SPY 20260630 C742 (delayed, pre-market) on 2026-06-09.
    ticks = {"size_27": 966, "size_28": 0, "size_74": 0, "price_75": 9.66}
    greeks = {
        "opt_83": [0, 0.15119932259969943, 0.47103890265158527, 9.65966338616164, 1.829, 0.01483557707081943, 0.7273248883267023, -0.28963591433119795, 739.08],
    }
    parsed = parse_option_ticks(ticks, greeks, option_type="call")
    assert parsed["open_interest"] == 966  # call OI from tick 27
    assert parsed["volume"] == 0  # delayed pre-market volume present as 0
    assert round(parsed["iv"], 4) == 0.1512
    assert round(parsed["delta"], 4) == 0.4710
    assert round(parsed["gamma"], 4) == 0.0148
    assert round(parsed["theta"], 4) == -0.2896
    assert parsed["close"] == 9.66


def test_parse_option_ticks_uses_put_open_interest_tick() -> None:
    ticks = {"size_27": 100, "size_28": 555}
    parsed = parse_option_ticks(ticks, {}, option_type="put")
    assert parsed["open_interest"] == 555  # put OI from tick 28
    assert parsed["delta"] is None  # no greeks present


def test_parse_option_ticks_prefers_live_over_delayed_greeks() -> None:
    greeks = {
        "opt_13": [0, 0.2, 0.5, 5.0, 0.0, 0.01, 0.1, -0.05, 100.0],  # live model
        "opt_83": [0, 0.9, 0.9, 9.0, 0.0, 0.09, 0.9, -0.9, 100.0],  # delayed model
    }
    parsed = parse_option_ticks({}, greeks, option_type="call")
    assert parsed["iv"] == 0.2  # live preferred


def test_chain_row_is_store_options_chain_compatible() -> None:
    parsed = parse_option_ticks({"size_27": 966, "price_75": 9.66}, {}, option_type="call")
    row = chain_row("SPY", "20260630", 742.0, parsed, delayed=True)
    assert row["expiry"] == "2026-06-30"  # ISO for store_options_chain
    assert row["type"] == "call"
    assert row["strike"] == 742.0
    assert row["open_interest"] == 966
    assert row["market_data"] == "delayed"


def test_ibkr_chain_rows_flow_into_option_snapshots(tmp_path: Path) -> None:
    """End-to-end: IBKR-shaped rows persist as source='ibkr' and the radar's
    persist step extracts OI/volume/greeks into option_snapshot columns."""

    greeks = {"opt_83": [0, 0.455, 0.622, 41.35, 0.0, 0.012, 0.5, -0.1, 210.0]}
    parsed = parse_option_ticks({"size_27": 7928, "size_74": 3, "price_75": 41.35}, greeks, option_type="call")
    row = chain_row("NVDA", "20270617", 210.0, parsed, delayed=True)

    db_path = tmp_path / "investment.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        upsert_instrument(con, {"symbol": "NVDA", "name": "NVIDIA", "asset_class": "equity"})
        stored = store_options_chain(con, "NVDA", "2026-06-09T12:00:00", [row], source="ibkr")
        assert stored == 1
        snaps = persist_option_snapshots(con, symbols=["NVDA"], source="ibkr")
        assert snaps >= 1
        rows = query_rows(
            con,
            "SELECT open_interest, volume, iv, delta, data_source FROM option_snapshot WHERE ticker = 'NVDA'",
        )
    assert rows, "expected an option_snapshot row from the IBKR chain"
    snap = rows[0]
    assert snap["open_interest"] == 7928  # OI carried from IBKR into the snapshot
    assert snap["volume"] == 3
    assert round(float(snap["delta"]), 3) == 0.622
    assert snap["data_source"] == "ibkr"


def test_update_ibkr_skips_unquoted_offhours_snapshot(tmp_path: Path, monkeypatch) -> None:
    """Off-hours the delayed feed returns OI but no bid/ask. Such a quote-less pull
    must NOT be persisted — it would supersede the last good market-hours snapshot
    and poison the radar with null-spread 'data gap' contracts."""

    from investment_panel.jobs import update_ibkr_options

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
database:
  duckdb_path: {tmp_path / "investment.duckdb"}
nas:
  status_dir: {tmp_path / "status"}
data_sources:
  brokers:
    enabled: true
    ibkr:
      enabled: true
""",
        encoding="utf-8",
    )

    # Five contracts with OI but no bid/ask (the off-hours signature).
    unquoted = [
        chain_row("NVDA", "20270617", 200.0 + i, parse_option_ticks({"size_27": 100}, {}, option_type="call"), delayed=True)
        for i in range(5)
    ]
    assert all((r.get("bid") or 0) <= 0 and (r.get("ask") or 0) <= 0 for r in unquoted)

    monkeypatch.setattr(
        update_ibkr_options,
        "collect_ibkr_option_chains",
        lambda cfg, symbols, **kw: {"rows": {"NVDA": unquoted}, "market_data": "delayed", "observed_at": "2026-06-10T02:00:00", "errors": []},
    )

    result = update_ibkr_options.run(str(config_path), symbols=["NVDA"])
    assert result["status"] == "skipped_unquoted_snapshot"
    with db(tmp_path / "investment.duckdb") as con:
        rows = query_rows(con, "SELECT count(*) AS c FROM options_chain WHERE source = 'ibkr'")
    assert rows[0]["c"] == 0  # nothing persisted
