from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from investment_panel.core.db import db, init_db, query_rows
from investment_panel.core.options_radar import DEFAULT_STRATEGY_VERSION, refresh_options_radar
from app.data_access import ticker_decision_brief
import app.main as app_main
from app.main import app, _require_local_request
from tests.test_option_agent_postmortem import seed_missed_winner
from tests.test_option_agent_thesis import seed_fire_candidate


def _use_temp_api_db(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    app_main._invalidate_context_cache()
    monkeypatch.setattr(
        app_main,
        "load_config",
        lambda _path=None: {
            "database": {"duckdb_path": str(db_path)},
            "nas": {"status_dir": str(db_path.parent / "status")},
        },
    )


def test_api_routes_return_json() -> None:
    client = TestClient(app)
    for path in [
        "/api/status",
        "/api/dashboard",
        "/api/panel-snapshot?scope=feed",
        "/api/panel-snapshot?scope=watchlist",
        "/api/panel-snapshot?scope=sources",
        "/api/panel-snapshot?scope=superinvestors",
        "/api/panel-snapshot?scope=market",
        "/api/panel-snapshot?scope=options-radar",
        "/api/panel-snapshot?scope=today",
        "/api/panel-snapshot?scope=dashboard",
        "/api/decision-readiness",
        "/api/signals",
        "/api/opportunities-ranked",
        "/api/opportunity-sources",
        "/api/candidates",
        "/api/portfolio",
        "/api/theses",
        "/api/thesis-monitor",
        "/api/trader-twins",
        "/api/catalysts",
        "/api/fundamentals",
        "/api/disclosures",
        "/api/quotes",
        "/api/screener",
        "/api/options-expiries",
        "/api/options-chain",
        "/api/options-payoff-scenarios",
        "/api/options-provider-capabilities",
        "/api/options-expiry-signals",
        "/api/options-ticker-signals",
        "/api/option-strategy-versions",
        "/api/option-snapshot",
        "/api/option-features",
        "/api/stock-features",
        "/api/agent-thesis",
        "/api/agent-thesis-requests",
        "/api/agent-thesis-validations",
        "/api/agent-postmortem-requests",
        "/api/agent-postmortems",
        "/api/candidate-events",
        "/api/candidate-event-marks",
        "/api/candidate-event-attributions",
        "/api/shadow-trades",
        "/api/shadow-trade-marks",
        "/api/radar-state-transitions",
        "/api/option-attributions",
        "/api/missed-winner-events",
        "/api/strategy-mutation-proposals",
        "/api/strategy-backtests",
        "/api/strategy-forward-tests",
        "/api/strategy-cohorts",
        "/api/news",
        "/api/tradingview-symbol-search",
        "/api/tradingview-watchlists",
        "/api/tradingview-alerts",
        "/api/tradingview-chart-state",
        "/api/sepa",
        "/api/liquidity",
        "/api/correlations",
        "/api/etf-premiums",
        "/api/analyst-estimates",
        "/api/earnings",
        "/api/earnings-setups",
        "/api/valuations",
        "/api/technicals",
        "/api/research-packets",
        "/api/provider-runs",
        "/api/broker/status",
        "/api/broker/accounts",
        "/api/broker/positions",
        "/api/agent/recommendations",
        "/api/paper-orders",
        "/api/daily-brief",
        "/api/feed",
        "/api/watchlist-screen",
        "/api/watchlist/symbols",
        "/api/source-consensus",
        "/api/source-ticker-rankings",
        "/api/ownership-consensus",
        "/api/market-context",
        "/api/portfolio-risk/exposure-clusters",
        "/api/portfolio-risk/correlation-edges",
        "/api/portfolio-risk/cards",
        "/api/portfolio-risk/review-actions",
        "/api/source-health",
        "/api/sources",
        "/api/source-items",
        "/api/source-runs",
        "/api/ticker-source-signals",
        "/api/sources/sec_edgar",
        "/api/source-ingestion-audit",
        "/api/refresh-jobs",
        "/api/settings",
        "/api/tickers/TSLA",
    ]:
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")


def test_market_snapshot_only_returns_market_tables() -> None:
    client = TestClient(app)

    response = client.get("/api/panel-snapshot?scope=market")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload["tables"]) == {
        "market_valuation_reference_charts",
        "market_environment_assets",
        "market_environment_model",
    }


def test_options_radar_snapshot_returns_radar_tables() -> None:
    client = TestClient(app)

    response = client.get("/api/panel-snapshot?scope=options-radar")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload["tables"]) == {
        "option_strategy_versions",
        "candidate_event",
        "candidate_event_mark",
        "candidate_event_attribution",
        "shadow_trade",
        "shadow_trade_mark",
        "radar_state_transition",
        "option_attribution",
        "missed_winner_event",
        "strategy_mutation_proposal",
        "strategy_backtest_result",
        "strategy_forward_test_result",
        "strategy_cohort_result",
        "agent_thesis",
        "agent_thesis_request",
        "agent_thesis_validation",
        "agent_postmortem_request",
        "agent_postmortem",
        "option_radar_summary",
        "option_snapshot",
        "option_features",
        "stock_features",
    }


def test_refresh_job_launcher_rejects_unallowlisted_job() -> None:
    client = TestClient(app)
    response = client.post("/api/refresh-jobs/not-a-real-job")
    assert response.status_code == 400
    assert "allowlisted" in response.text


def test_refresh_jobs_exposes_options_radar_job() -> None:
    client = TestClient(app)
    response = client.get("/api/refresh-jobs")

    assert response.status_code == 200
    assert "refresh_options_radar" in response.json()["allowlist"]


def test_agent_thesis_post_fulfills_request_and_validates(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "agent-thesis-api.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        seed_fire_candidate(con)
        refresh_options_radar(con, ["TSLA"])
        con.execute(
            """
            INSERT INTO ticker_source_signals
            (id, source_item_id, source_id, symbol, observed_at, signal_type,
             sentiment, direction, confidence, thesis, antithesis, catalysts,
             risks, invalidation, evidence_refs, needs_market_context, raw)
            VALUES (
             'sig-tsla-api-proof', 'source-tsla-api-proof', 'test_research', 'TSLA',
             '2026-06-03T12:00:00Z', 'earnings', 'positive', 'bullish', 0.9,
             'gross margin stabilizes while deliveries recover into the next report',
             'pricing pressure remains the bear case',
             '[{"type":"earnings","what_to_watch":"margins and delivery guide"}]',
             '["pricing pressure"]',
             'stock breaks below $80 without recovery',
             '[{"type":"source_item","id":"source-tsla-api-proof"}]',
             true,
             '{}'
            )
            """
        )
        con.execute(
            """
            INSERT INTO catalysts
            (id, symbol, event_date, event, expected_impact, source, verification_status, raw)
            VALUES ('cat-tsla-api-earnings', 'TSLA', '2026-06-15', 'earnings', 'high', 'test', 'confirmed', '{}')
            """
        )

    _use_temp_api_db(monkeypatch, db_path)
    client = TestClient(app)
    response = client.post(
        "/api/agent-thesis",
        json={
            "ticker": "TSLA",
            "strategy_version": DEFAULT_STRATEGY_VERSION,
            "created_at": "2026-06-03T12:00:00Z",
            "bull_target_price": 180,
            "bull_target_date": "2028-01-21",
            "base_target_price": 95,
            "core_thesis": "Energy storage and autonomy narrative returns while margins stabilize.",
            "required_proofs": ["gross margin stabilizes", "deliveries recover"],
            "catalysts": [{"type": "earnings", "what_to_watch": "margins and delivery guide"}],
            "invalidation": ["stock breaks below $80 without recovery"],
            "bear_case": "Demand weakness and pricing pressure can keep the stock below trend.",
            "confidence": 72,
            "evidence_refs": [{"type": "source_signal", "id": "source-tsla-api-proof"}],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "accepted"
    assert payload["agent_theses_attached"] >= 1
    assert payload["agent_thesis_validations"] == 1
    with db(db_path) as con:
        request = query_rows(con, "SELECT status FROM agent_thesis_request WHERE ticker = 'TSLA'")[0]
        validation = query_rows(con, "SELECT state, proof_status, catalyst_status, red_team_status FROM agent_thesis_validation WHERE ticker = 'TSLA'")[0]
    assert request["status"] == "fulfilled"
    assert validation == {"state": "validated", "proof_status": "supported", "catalyst_status": "scheduled", "red_team_status": "source_backed"}


def test_agent_thesis_post_rejects_unstructured_payload(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "agent-thesis-invalid-api.duckdb"
    init_db(db_path)
    _use_temp_api_db(monkeypatch, db_path)
    client = TestClient(app)

    response = client.post("/api/agent-thesis", json={"ticker": "TSLA", "bull_target_price": 180})

    assert response.status_code == 400
    assert "core_thesis" in response.text


def test_agent_postmortem_post_keeps_strategy_mutation_gated(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "agent-postmortem-api.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        seed_missed_winner(con)
        refresh_options_radar(con, ["RBLX"])
        request = query_rows(con, "SELECT * FROM agent_postmortem_request")[0]

    _use_temp_api_db(monkeypatch, db_path)
    client = TestClient(app)
    response = client.post(
        "/api/agent-postmortems",
        json={
            "request_id": request["request_id"],
            "ticker": "RBLX",
            "strategy_version": DEFAULT_STRATEGY_VERSION,
            "source_type": request["source_type"],
            "source_id": request["source_id"],
            "outcome_type": "missed_10x_winner",
            "failure_type": "delta_range_too_strict",
            "evidence": ["Contract was rejected for delta_outside_strategy_range before reaching 10x."],
            "proposed_rule_change": "Test a lower-delta sleeve for strong momentum reversals.",
            "proposed_parameter_changes": {"delta_min": 0.10, "candidate_note": "agent postmortem lower-delta sleeve"},
            "expected_effect": "Increase recall for lower-delta 10x winners.",
            "risk": "May increase false positives and earlier entries.",
            "confidence": 70,
            "evidence_refs": [{"type": "missed_winner_event", "id": request["source_id"]}],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "accepted"
    assert payload["strategy_backtests"] >= 1
    assert payload["strategy_forward_tests"] >= 1
    with db(db_path) as con:
        stored_request = query_rows(con, "SELECT status FROM agent_postmortem_request WHERE request_id = ?", [request["request_id"]])[0]
        proposal = query_rows(con, "SELECT status, requires_backtest, requires_forward_test, human_approval_status FROM strategy_mutation_proposal")[0]
    assert stored_request["status"] == "fulfilled"
    assert proposal["requires_backtest"] is True
    assert proposal["requires_forward_test"] is True
    assert proposal["human_approval_status"] == "required"
    assert proposal["status"] in {"forward_test_required", "backtest_failed", "ready_for_human_review"}


def test_strategy_mutation_promote_endpoint_requires_gates_and_approval(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "strategy-promotion-api.duckdb"
    init_db(db_path)
    with db(db_path) as con:
        seed_missed_winner(con)
        refresh_options_radar(con, ["RBLX"])
        proposal_id = query_rows(con, "SELECT proposal_id FROM strategy_mutation_proposal")[0]["proposal_id"]

    _use_temp_api_db(monkeypatch, db_path)
    client = TestClient(app)
    blocked = client.post(
        f"/api/strategy-mutation-proposals/{proposal_id}/promote",
        json={"approved_by": "joe"},
    )

    assert blocked.status_code == 400
    assert "forward shadow test" in blocked.text

    with db(db_path) as con:
        con.execute(
            """
            UPDATE strategy_forward_test_result
            SET verdict = 'pass', status = 'complete', days_observed = 30
            WHERE proposal_id = ?
            """,
            [proposal_id],
        )

    unapproved = client.post(
        f"/api/strategy-mutation-proposals/{proposal_id}/promote",
        json={"approved_by": ""},
    )
    response = client.post(
        f"/api/strategy-mutation-proposals/{proposal_id}/promote",
        json={"approved_by": "joe"},
    )

    assert unapproved.status_code == 400
    assert "human approval" in unapproved.text
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "promoted"
    assert payload["proposal_id"] == proposal_id
    assert payload["strategy_version"] == "leap_10x_momentum_lottery_proposed_v1"
    with db(db_path) as con:
        proposal = query_rows(
            con,
            """
            SELECT status, human_approval_status, approved_by, approved_at
            FROM strategy_mutation_proposal
            WHERE proposal_id = ?
            """,
            [proposal_id],
        )[0]
        strategy = query_rows(
            con,
            "SELECT strategy_version, status, supersedes FROM option_strategy_versions WHERE strategy_version = ?",
            [payload["strategy_version"]],
        )[0]
    assert proposal["status"] == "promoted"
    assert proposal["human_approval_status"] == "approved"
    assert proposal["approved_by"] == "joe"
    assert proposal["approved_at"]
    assert strategy == {"strategy_version": payload["strategy_version"], "status": "promoted", "supersedes": DEFAULT_STRATEGY_VERSION}


def test_local_write_guard_allows_private_lan_clients() -> None:
    _require_local_request(SimpleNamespace(client=SimpleNamespace(host="100.120.95.8")))
    _require_local_request(SimpleNamespace(client=SimpleNamespace(host="192.168.50.197")))
    _require_local_request(SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")))

    with pytest.raises(HTTPException):
        _require_local_request(SimpleNamespace(client=SimpleNamespace(host="8.8.8.8")))


def test_ticker_decision_brief_prefers_quote_row_over_decision_snapshot_price() -> None:
    brief = ticker_decision_brief(
        "AMD",
        {
            "quotes": [
                {
                    "symbol": "AMD",
                    "price": 424.10,
                    "change_pct": -5.69,
                    "observed_at": "2026-05-15T00:00:00",
                    "source": "previous_close:yahoo-chart",
                }
            ],
            "symbol_decision_snapshot": [
                {
                    "symbol": "AMD",
                    "action_grade": "Reject",
                    "freshness_status": "fresh",
                    "latest_quote": 455.19,
                    "blocking_gates": ["chart_extended_without_thesis"],
                    "decision_basis": {"summary": "snapshot price should not be canonical"},
                }
            ],
        },
    )

    assert brief["canonical_quote"]["price"] == 424.10
    assert brief["canonical_quote"]["source"] == "previous_close:yahoo-chart"
    assert brief["canonical_quote"]["type"] == "prior_close"
    assert brief["verdict"]["blockers"] == ["chart_extended_without_thesis", "decision_reject"]
    assert brief["setup"]["entry_zone"] == "No entry while the decision grade is Reject."
    assert brief["risk_plan"]["max_sizing"] == "No new exposure while decision grade remains Reject."


def test_ticker_decision_brief_surfaces_missing_thesis_news_and_filings() -> None:
    brief = ticker_decision_brief(
        "AMD",
        {
            "quotes": [{"symbol": "AMD", "price": 424.10, "source": "previous_close:yahoo-chart"}],
            "symbol_decision_snapshot": [
                {
                    "symbol": "AMD",
                    "action_grade": "Reject",
                    "freshness_status": "fresh",
                    "blocking_gates": ["chart_extended_without_thesis"],
                    "snapshot": {"invalidation": "Needs a verified thesis."},
                }
            ],
            "technicals": [{"symbol": "AMD", "technical_score": 99.8, "return_20d": 0.52, "ma50": 279.2}],
            "sepa": [{"symbol": "AMD", "verdict": "strong_setup", "stage": "stage_2_advancing"}],
            "liquidity": [{"symbol": "AMD", "grade": "very_high", "avg_dollar_volume": 10_000_000_000}],
            "valuations": [{"symbol": "AMD", "method": "relative", "fair_value": 407.79, "upside_pct": -10.41}],
            "options_payoff_scenarios": [{"symbol": "AMD", "strategy_type": "call_debit_spread", "max_loss": -165, "expiry": "2026-05-15"}],
            "research_packets": [{"symbol": "AMD", "decision": "monitor", "why_now": ["Technical setup is constructive."]}],
        },
    )

    assert any("Technical score" in item for item in brief["evidence_for"])
    assert any("valuation" in item.lower() for item in brief["evidence_against"])
    assert any("Optional thesis" in item for item in brief["unknowns"])
    assert any("No ticker-specific news row" in item for item in brief["unknowns"])
    assert any("No tracked disclosure row" in item for item in brief["unknowns"])
    assert brief["risk_plan"]["max_loss"] == "Not applicable while decision grade is Reject."
    assert brief["options_context"]["status"] == "expired"
    assert "expired_options_context" in brief["verdict"]["blockers"]
    assert any("Options context is expired" in item for item in brief["verdict"]["blocker_labels"])
    assert {row["label"] for row in brief["tab_summaries"]["Evidence Stack"]} == {"For", "Against", "Open Inputs"}


def test_ticker_decision_brief_uses_specific_source_gap_language() -> None:
    brief = ticker_decision_brief(
        "MU",
        {
            "symbol_decision_snapshot": [
                {
                    "symbol": "MU",
                    "action_grade": "Watch",
                    "blocking_gates": ["liquidity_unknown", "missing_daily_analysis", "stale_intraday_quote"],
                    "decision_basis": {"source_count": 3, "evidence_count": 1},
                }
            ],
        },
    )

    joined = " ".join(
        [
            brief["verdict"]["summary"],
            " ".join(brief["verdict"]["blocker_labels"]),
            " ".join(brief["evidence_against"]),
            " ".join(brief["unknowns"]),
        ]
    )
    assert "Liquidity unknown" not in joined
    assert "Missing daily analysis" not in joined
    assert "No explicit" not in joined
    assert "No major missing" not in joined
    assert "No current liquidity row is loaded for this ticker." in joined
    assert "Daily analysis rows are not loaded for this ticker." in joined
    assert brief["risk_plan"]["max_loss"] == "Not applicable while blockers are active."
    assert brief["risk_plan"]["max_sizing"] == "No new exposure until evidence gates clear."


def test_frontend_fallback_serves_spa_deep_links_after_build() -> None:
    dist_index = Path(__file__).resolve().parents[1] / "frontend" / "dist" / "index.html"
    if not dist_index.exists():
        pytest.skip("frontend build output is not present")

    client = TestClient(app)
    for path in [
        "/",
        "/feed",
        "/today",
        "/dashboard",
        "/watchlist",
        "/sources",
        "/superinvestors",
        "/market",
        "/opportunities",
        "/portfolio",
        "/research",
        "/research-queue",
        "/thesis-monitor",
        "/filings",
        "/calendar",
        "/health",
        "/settings",
        "/tickers/NVDA",
        "/not-a-market-route",
    ]:
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert '<div id="root">' in response.text
