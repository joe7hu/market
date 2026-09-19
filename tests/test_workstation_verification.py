"""The local handoff is executable, bounded, read-only, and does not leak balances."""
import json
from urllib.error import URLError
import pytest
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


def test_transport_errors_do_not_echo_sensitive_exception_text(monkeypatch):
    seen = []
    def fail(base, path, timeout):
        seen.append(path)
        raise URLError("postgres://private-secret@localhost/customer-account")
    monkeypatch.setattr(check, "read_json", fail)
    result = check.verify("http://127.0.0.1:8010")
    assert result["status"] == "failed" and len(seen) == 6
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
