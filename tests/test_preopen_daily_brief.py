from __future__ import annotations

from datetime import date, datetime, timedelta
import json
from zoneinfo import ZoneInfo

from investment_panel.core.db import db, init_db
from investment_panel.core.preopen_brief import (
    backtest_qqq_preopen_model,
    build_preopen_context,
    generate_preopen_llm_brief,
    preopen_daily_brief_rows,
    qqq_preopen_forecast,
    refresh_preopen_daily_brief,
    should_run_scheduled_preopen_brief,
)


def test_preopen_daily_brief_persists_deterministic_fallback(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MARKET_PREOPEN_BRIEF_LLM", "0")
    db_path = tmp_path / "preopen.duckdb"
    init_db(db_path)
    with db(db_path, read_only=False) as con:
        seed_qqq_prices(con)
        con.execute(
            """
            INSERT INTO catalysts
            (id, symbol, event_date, event, expected_impact, source, start_at, end_at,
             timezone, event_scope, event_kind, importance, verification_status,
             source_url, source_name, raw)
            VALUES ('macro-cpi', NULL, current_date, 'CPI release', 'Inflation risk',
                    'test', NULL, NULL, 'America/New_York', 'macro', 'inflation',
                    'high', 'confirmed', 'https://example.com/cpi', 'test', '{}')
            """
        )

        payload = refresh_preopen_daily_brief(con)
        rows = preopen_daily_brief_rows(con)

    assert payload["status"] == "deterministic_fallback"
    assert payload["qqq_forecast"]["status"] == "ok"
    assert payload["qqq_forecast"]["model_version"] == "qqq_preopen_stat_ensemble_v1"
    assert rows[0]["headline"] == "Pre-open market brief"
    assert rows[0]["key_events"][0]["event"] == "CPI release"
    assert rows[0]["backtest"]["status"] == "ok"


def test_qqq_preopen_forecast_and_backtest_are_backtestable() -> None:
    history = [
        {"date": (date(2026, 1, 1) + timedelta(days=index)).isoformat(), "close": 400 + index * 0.35 + (index % 5) * 0.4}
        for index in range(140)
    ]

    forecast = qqq_preopen_forecast(history)
    backtest = backtest_qqq_preopen_model(history)

    assert forecast["status"] == "ok"
    assert forecast["low"] < forecast["expected_close"] < forecast["high"]
    assert forecast["support"] == forecast["low"]
    assert backtest["status"] == "ok"
    assert backtest["observations"] > 20
    assert "mae_pct" in backtest


def test_preopen_llm_uses_configured_model_and_medium_reasoning(monkeypatch) -> None:
    captured = {}

    def fake_post(url, headers, json, timeout):
        captured.update({"url": url, "headers": headers, "json": json, "timeout": timeout})

        class Response:
            status_code = 200

            @staticmethod
            def json():
                return {
                    "output_text": json_module.dumps(
                        {
                            "headline": "FOMC digestion",
                            "macro_regime": "Rates up, breadth mixed.",
                            "narrative": "Market is digesting Fed guidance.",
                            "opening_scenario": "Choppy open.",
                            "qqq_path": "Use supplied levels.",
                            "risks": ["Fed speaker surprise"],
                            "watch_items": ["Jobless claims"],
                            "evidence_refs": ["prices_daily"],
                        }
                    )
                }

        json_module = json_lib
        return Response()

    json_lib = json
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("MARKET_PREOPEN_BRIEF_MODEL", "gpt-5.5")
    monkeypatch.setenv("MARKET_PREOPEN_BRIEF_REASONING_EFFORT", "medium")
    monkeypatch.setattr("investment_panel.core.preopen_brief.httpx.post", fake_post)

    result = generate_preopen_llm_brief(
        {
            "brief_date": "2026-06-20",
            "qqq_forecast": {"status": "ok", "expected_close": 500, "support": 495, "resistance": 505},
            "backtest": {"status": "ok", "mae_pct": 0.8},
            "key_events": [],
            "market_environment": [],
            "fresh_source_items": [],
            "source_runs": [],
        }
    )

    assert result["headline"] == "FOMC digestion"
    assert captured["json"]["model"] == "gpt-5.5"
    assert captured["json"]["reasoning"] == {"effort": "medium"}


def test_preopen_context_excludes_same_day_price_bar(tmp_path) -> None:
    target = date(2026, 6, 19)
    db_path = tmp_path / "preopen-point-in-time.duckdb"
    init_db(db_path)
    with db(db_path, read_only=False) as con:
        close = 100.0
        for index in range(140):
            current = target - timedelta(days=200 - index)
            if current.weekday() >= 5:
                continue
            close += 0.1
            con.execute(
                "INSERT INTO prices_daily VALUES ('QQQ', ?, ?, ?, ?, ?, ?, 'test')",
                [current, close - 0.5, close + 1.0, close - 1.0, close, 1_000_000],
            )
        con.execute(
            "INSERT INTO prices_daily VALUES ('QQQ', ?, ?, ?, ?, ?, ?, 'test')",
            [target, 999.0, 1_010.0, 990.0, 1_000.0, 1_000_000],
        )

        context = build_preopen_context(con, target_date=target)

    assert context["qqq_forecast"]["status"] == "ok"
    assert context["qqq_forecast"]["prior_close"] < 200


def test_scheduled_preopen_brief_gate_uses_preopen_window_and_single_daily_write(tmp_path) -> None:
    db_path = tmp_path / "preopen-gate.duckdb"
    init_db(db_path)
    tz = ZoneInfo("America/New_York")
    with db(db_path, read_only=False) as con:
        should_run, gate = should_run_scheduled_preopen_brief(con, datetime(2026, 6, 19, 12, 0, tzinfo=tz))
        assert should_run is False
        assert gate["reason"] == "outside_preopen_window"

        should_run, gate = should_run_scheduled_preopen_brief(con, datetime(2026, 6, 19, 8, 0, tzinfo=tz))
        assert should_run is True
        assert gate["reason"] == "preopen_window_open"

        con.execute(
            "INSERT INTO preopen_daily_brief (brief_date, generated_at, session, status) VALUES (?, ?, ?, ?)",
            [date(2026, 6, 19), datetime(2026, 6, 19, 12, 0), "pre_open", "ok"],
        )
        should_run, gate = should_run_scheduled_preopen_brief(con, datetime(2026, 6, 19, 8, 5, tzinfo=tz))
        assert should_run is False
        assert gate["reason"] == "preopen_brief_already_generated"


def seed_qqq_prices(con) -> None:
    start = date.today() - timedelta(days=180)
    close = 430.0
    for index in range(180):
        current = start + timedelta(days=index)
        if current.weekday() >= 5:
            continue
        close += 0.25 + (index % 7 - 3) * 0.08
        con.execute(
            "INSERT INTO prices_daily VALUES ('QQQ', ?, ?, ?, ?, ?, ?, 'test')",
            [current, close - 0.5, close + 1.0, close - 1.0, close, 1_000_000],
        )
