from __future__ import annotations

import plistlib
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_market_open_options_radar_runs_full_hard_refresh_at_0942_weekdays() -> None:
    plist_path = (
        PROJECT_ROOT
        / "ops"
        / "launchd"
        / "com.joehu.market.market-open-options-radar.plist"
    )

    with plist_path.open("rb") as handle:
        payload = plistlib.load(handle)

    intervals = payload["StartCalendarInterval"]
    assert intervals == [
        {"Weekday": weekday, "Hour": 9, "Minute": 42}
        for weekday in range(1, 6)
    ]
    command = payload["ProgramArguments"][2]
    assert "investment_panel.core.refresh_jobs options_radar_hard_refresh" in command
    assert "MARKET_ROBINHOOD_INCREMENTAL_SYMBOLS=80" in command


def test_intraday_radar_refreshes_every_fifteen_minutes_from_postgresql() -> None:
    plist_path = PROJECT_ROOT / "ops" / "launchd" / "com.joehu.market.hourly-options-radar.plist"

    with plist_path.open("rb") as handle:
        payload = plistlib.load(handle)

    assert payload["StartInterval"] == 900
    command = payload["ProgramArguments"][2]
    assert "investment_panel.core.refresh_jobs refresh_options_radar_signal_robinhood" in command
    assert "MARKET_DATABASE_URL=postgresql:///market" in command
    assert "MARKET_" + "DUCK" + "DB_PATH" not in command


def test_premarket_launchd_routes_through_postgresql_job_authority() -> None:
    plist_path = PROJECT_ROOT / "ops" / "launchd" / "com.joehu.market.premarket-options-intelligence.plist"
    with plist_path.open("rb") as handle:
        payload = plistlib.load(handle)

    command = payload["ProgramArguments"][2]
    assert "investment_panel.core.refresh_jobs premarket_options_intelligence" in command
    assert "MARKET_DATABASE_URL=postgresql:///market" in command
    assert "MARKET_" + "DUCK" + "DB_PATH" not in command


def test_risk_management_and_research_delivery_have_intraday_cadence() -> None:
    contracts = {
        "com.joehu.market.decision-inbox.plist": (60, "investment_panel.jobs.decision_inbox"),
        "com.joehu.market.options-risk-manager.plist": (60, "investment_panel.jobs.options_paper_execution"),
        "com.joehu.market.recovery-watch.plist": (120, "investment_panel.jobs.robinhood_option_history"),
    }
    for name, (interval, job) in contracts.items():
        with (PROJECT_ROOT / "ops" / "launchd" / name).open("rb") as handle:
            payload = plistlib.load(handle)
        assert payload["StartInterval"] == interval
        command = payload["ProgramArguments"][2]
        assert job in command
        assert "MARKET_DATABASE_URL=postgresql:///market" in command
