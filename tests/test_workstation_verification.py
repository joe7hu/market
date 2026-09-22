"""The local handoff is executable, bounded, read-only, and does not leak balances."""
import json
import sys
from urllib.error import URLError
import pytest
from investment_panel.domain.panel import PANEL_SCOPE_TABLES
from scripts import verify_workstation as check


def test_empty_paper_book_is_not_a_failed_strategy_or_a_required_trade():
    result = check.assess("paper", {"counts": {"total_orders": 0, "filled_orders": 0},
        "account": {"paper_only": True, "status": "complete", "nav": 123456, "opening_cash": 123456, "net_pnl": 0}})
    assert result["status"] == "pass"
    assert "123456" not in json.dumps(result)


def test_account_arithmetic_is_checked_without_publishing_the_money():
    with pytest.raises(check.ContractError):
        check.assess("paper", {"counts": {"total_orders": 1, "filled_orders": 1},
            "account": {"paper_only": True, "status": "complete", "nav": 100, "opening_cash": 100, "net_pnl": 20}})


def test_nav_gaps_are_valid_but_fake_gap_prices_are_not():
    payload = {"paper_only": True, "book": "paper", "points": [{"at": "2026-09-18T15:00:00Z", "status": "incomplete", "nav": None}]}
    assert check.assess("nav", payload)["status"] == "needs_attention"
    payload["points"][0]["nav"] = 0
    with pytest.raises(check.ContractError):
        check.assess("nav", payload)


def test_market_models_must_share_the_same_publication():
    tables = {key: {"rows": [{"publication_id": "one"}], "count": 1} for key in
        ("market_environment_assets", "market_environment_model", "market_state_snapshot", "coverage_matrix")}
    payload = {"scope": "market", "status": {"ready": True}, "tables": tables}
    assert check.assess("market", payload)["status"] == "pass"
    tables["market_state_snapshot"]["rows"][0]["publication_id"] = "two"
    with pytest.raises(check.ContractError):
        check.assess("market", payload)


def test_unread_workflow_populations_are_not_zero():
    payload = {"paper_only": True, "failed_reads": ["paper_orders"], "market": {"status": "available"},
        "workers": [{"job": "process_options_paper_orders", "status": "succeeded"}],
        "paper": {"status": "unavailable", "counts": {}}, "observations": {"status": "available", "counts": {}}}
    result = check.assess("workflow", payload)
    assert "paper_record_count" not in result["evidence"]
    assert result["status"] == "needs_attention"


def test_partial_workflow_population_retains_its_observed_count():
    payload = {"paper_only": True, "failed_reads": [], "market": {"status": "available"},
        "workers": [{"job": "process_options_paper_orders", "status": "succeeded"}],
        "paper": {"status": "available", "counts": {}},
        "observations": {"status": "partial", "counts": {"entered": 18}}}
    result = check.assess("workflow", payload)
    assert result["evidence"]["observations_record_count"] == 18
    assert "observations population could not be read; no zero is assumed" not in result["warnings"]
    assert "observations population is partial; inspect recorded blockers" in result["warnings"]


def test_transport_errors_do_not_echo_sensitive_exception_text(monkeypatch):
    seen = []
    def fail(base, path, timeout):
        seen.append(path)
        raise URLError("postgres://private-secret@localhost/customer-account")
    monkeypatch.setattr(check, "read_json", fail)
    result = check.verify("http://127.0.0.1:8010")
    assert result["status"] == "failed"
    assert len(seen) == len(check.ENDPOINTS) + len(check.TODAY_STABILITY_ENDPOINTS) * check.TODAY_STABILITY_ATTEMPTS
    assert all(path.startswith("/api/") for path in seen)
    assert "private-secret" not in json.dumps(result)


def test_http_reader_only_uses_bounded_get(monkeypatch):
    calls = []
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def read(self, limit):
            calls.append(limit)
            return b'{"status":"test"}'
    def request(value, *, timeout):
        calls.append((value.method, value.full_url, timeout))
        return Response()
    monkeypatch.setattr(check, "urlopen", request)
    assert check.read_json("http://localhost:8010", "/api/status", 10) == {"status": "test"}
    assert calls == [("GET", "http://localhost:8010/api/status", 10), check.MAX_RESPONSE_BYTES + 1]


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https://user:secret@example.com", "http://localhost:8010/?token=x"])
def test_no_credentials_or_unsupported_origins(url):
    with pytest.raises(ValueError):
        check.verify(url)


def test_malformed_json_numbers_are_rejected(monkeypatch):
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def read(self, limit):
            return b'{"nav":NaN}'
    monkeypatch.setattr(check, "urlopen", lambda *_a, **_kw: Response())
    with pytest.raises(check.ContractError):
        check.read_json("http://localhost", "/api/status", 10)


def test_runtime_reports_full_commit_for_exact_deployment_verification(monkeypatch):
    from types import SimpleNamespace
    from investment_panel.application.read_models import payloads
    expected = "a" * 40
    monkeypatch.delenv("MARKET_BACKEND_COMMIT", raising=False)
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(stdout=expected + "\n")
    monkeypatch.setattr(payloads.subprocess, "run", run)
    assert payloads._backend_commit() == expected
    assert commands == [["git", "rev-parse", "HEAD"]]
    result = check.assess("runtime", {"ready": True, "metadata": {
        "release": {"backend_commit": expected, "frontend_build": expected[:7], "scheduler_release": expected}, "schema_compatible": True,
        "schema_revision": "20260919_0025"}}, expected_commit=expected, expected_schema="20260919_0025")
    assert result["status"] == "pass"
    result = check.assess("runtime", {"ready": True, "metadata": {
        "release": {"backend_commit": expected, "frontend_build": "stale", "scheduler_release": expected}, "schema_compatible": True,
        "schema_revision": "20260919_0025"}}, expected_commit=expected, expected_schema="20260919_0025")
    assert result["status"] == "needs_attention"


def test_workstation_cli_defaults_to_current_schema(monkeypatch):
    from investment_panel.infrastructure.postgres.migrations import HEAD_REVISION

    seen = {}

    def verify(*_args, **kwargs):
        seen.update(kwargs)
        return {"status": "pass"}

    monkeypatch.setattr(check, "verify", verify)
    monkeypatch.setattr(sys, "argv", ["verify_workstation.py"])
    assert check.main() == 0
    assert seen["expected_schema"] == HEAD_REVISION


def test_today_names_the_blocker_without_reporting_private_action_text():
    payload = {"status": {"ready": True}, "actions": [{"primary_blocker": "trade_plan_missing", "next_action": "Review the evidence below: private-symbol"}]}
    result = check.assess("today", payload)
    assert result["status"] == "needs_attention"
    assert "private-symbol" not in json.dumps(result)
    payload["actions"][0]["next_action"] = "Refresh portfolio inputs before sizing."
    assert check.assess("today", payload)["status"] == "pass"


def test_release_candidate_repeats_today_and_snapshot_reads(monkeypatch):
    expected = "a" * 40
    calls = []
    runtime = {"ready": True, "metadata": {"release": {"backend_commit": expected, "frontend_build": expected, "scheduler_release": expected}, "schema_compatible": True, "schema_revision": "test"}}
    payloads = {
        "/api/status": runtime,
        "/api/today": {"status": {"ready": True}, "actions": []},
        "/api/panel-snapshot?scope=today": {"scope": "today", "status": {"ready": True},
            "tables": {name: {"rows": [], "count": 0} for name in PANEL_SCOPE_TABLES["today"]}},
    }

    def read(_base, endpoint, _timeout):
        calls.append(endpoint)
        return payloads[endpoint]

    monkeypatch.setattr(check, "read_json", read)
    result = check.verify("http://127.0.0.1:8010", expected_commit=expected, expected_schema="test", release_candidate=True)
    assert result["status"] == "pass"
    assert calls == ["/api/status", *(["/api/today", "/api/panel-snapshot?scope=today"] * check.TODAY_STABILITY_ATTEMPTS)]
    assert [row["attempt"] for row in result["checks"][1:]] == [1, 1, 2, 2, 3, 3]


def test_release_candidate_flags_changed_today_content(monkeypatch):
    expected = "a" * 40
    attempt = 0
    runtime = {"ready": True, "metadata": {"release": {"backend_commit": expected, "frontend_build": expected,
        "scheduler_release": expected}, "schema_compatible": True, "schema_revision": "test"}}

    def read(_base, endpoint, _timeout):
        nonlocal attempt
        if endpoint == "/api/status":
            return runtime
        if endpoint == "/api/today":
            attempt += 1
            return {"status": {"ready": True}, "actions": [{"action_identity": f"action-{attempt}"}]}
        return {"scope": "today", "status": {"ready": True},
            "tables": {name: {"rows": [], "count": 0} for name in PANEL_SCOPE_TABLES["today"]}}

    monkeypatch.setattr(check, "read_json", read)
    result = check.verify("http://127.0.0.1:8010", expected_commit=expected, expected_schema="test", release_candidate=True)
    assert result["status"] == "needs_attention"
    assert "Repeated today response changed during the smoke check" in result["checks"][3]["warnings"]


def test_today_stability_ignores_only_the_presentation_clock():
    first = {"actions": [{"action_identity": "one", "action": "NO_TRADE", "current_at": "2026-09-20T13:00:00Z", "current_at_is_fallback": True}]}
    second = {"actions": [{"action_identity": "one", "action": "NO_TRADE", "current_at": "2026-09-20T13:00:01Z", "current_at_is_fallback": True}]}
    assert check.stability_digest("today", first) == check.stability_digest("today", second)

    second["actions"][0]["current_at_is_fallback"] = False
    assert check.stability_digest("today", first) != check.stability_digest("today", second)


def test_today_snapshot_requires_its_contract_tables():
    with pytest.raises(check.ContractError):
        check.assess("today_snapshot", {"scope": "today", "status": {"ready": True}, "tables": {}})


def test_expired_opportunity_must_have_blocked_presentation_and_recovery_action():
    row = {"presentation_state": "paper_review", "presentation_blocker": "trade_plan_expired", "trade_plan": {"eligibility": "ACTIONABLE"}}
    payload = {"scope": "opportunities", "status": {"ready": True}, "tables": {"opportunities_ranked": {"rows": [row], "count": 1}}}
    with pytest.raises(check.ContractError):
        check.assess("opportunities", payload)
    row.update(presentation_state="blocked", presentation_next_action="Publish a current plan.")
    assert check.assess("opportunities", payload)["status"] == "pass"


def test_old_but_verified_price_does_not_fail_read_only_clock_check():
    row = {"mark_status": "verified", "mark_stale": False, "mark_observed_at": "2026-09-18T20:00:00Z", "mark_available_at": "2026-09-18T20:01:00Z"}
    assert check.assess("paper_trades", {"rows": [row]})["status"] == "pass"
    row.update(mark_stale=True, remaining_quantity=1, unrealized_pnl=123)
    with pytest.raises(check.ContractError):
        check.assess("paper_trades", {"rows": [row]})
