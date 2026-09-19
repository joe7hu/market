"""A bounded readonly quote collector follows required contracts without enabling trading."""
from dataclasses import replace
from types import SimpleNamespace
from investment_panel.settings import AppConfig
from investment_panel.jobs import paper_quotes


def setup(monkeypatch, *, lease=True, fail=False):
    config = AppConfig()
    brokers = replace(config.data_sources.brokers, robinhood=replace(config.data_sources.brokers.robinhood, enabled=True))
    config = replace(config, data_sources=replace(config.data_sources, brokers=brokers))
    calls = []
    monkeypatch.setattr(paper_quotes, "load_config", lambda _: config)
    monkeypatch.setattr(paper_quotes, "is_market_open", lambda _: True)
    monkeypatch.setattr(paper_quotes, "runtime_for_config", lambda _: object())
    monkeypatch.setattr(paper_quotes, "active_paper_contracts", lambda *_: [
        {"symbol": f"T{i}", "contract_id": str(i*2+j)} for i in range(12) for j in range(2)])
    class Policy:
        def acquire_provider_lease(self, **kwargs):
            calls.append(("acquire", kwargs))
            return SimpleNamespace(id="lease") if lease else None
        def release_provider_lease(self, value):
            calls.append(("release", value))
    monkeypatch.setattr(paper_quotes, "OptionHistoryPolicyRepository", lambda _: Policy())
    def collect(provider, symbols, **kwargs):
        calls.append(("collect", (provider, symbols, kwargs)))
        if fail:
            raise RuntimeError("quote provider unavailable")
        return {"errors": []}
    monkeypatch.setattr(paper_quotes, "collect_robinhood_option_chains", collect)
    monkeypatch.setattr(paper_quotes, "persist_collected_option_chains", lambda *_a, **_kw: {"contract_count": 16, "run_id": "capture"})
    return calls


def test_only_required_contracts_all_legs_and_eight_oldest_symbols(monkeypatch):
    calls = setup(monkeypatch)
    result = paper_quotes.run()
    provider, symbols, args = calls[1][1]
    assert provider.readonly and provider.max_collection_seconds == 30 and provider.timeout_seconds == 10
    assert symbols == [f"T{i}" for i in range(8)]
    assert len(args["required_contracts"]) == 16 and args["required_only"]
    assert result["contracts_required"] == 24 and result["remaining_contracts"] == 8
    assert not result["live_brokerage_submission"]
    assert calls[-1] == ("release", "lease")


def test_busy_provider_does_not_bypass_lease(monkeypatch):
    calls = setup(monkeypatch, lease=False)
    assert paper_quotes.run()["reason"] == "provider_capacity_busy"
    assert len(calls) == 1


def test_failed_capture_releases_provider_lease(monkeypatch):
    calls = setup(monkeypatch, fail=True)
    result = paper_quotes.run()
    assert result["status"] == "failed" and result["source_status"] == "failed"
    assert result["symbols_attempted"] == [f"T{i}" for i in range(8)]
    assert "contracts_captured" not in result
    assert calls[-1] == ("release", "lease")


def test_closed_session_never_collects(monkeypatch):
    calls = setup(monkeypatch)
    monkeypatch.setattr(paper_quotes, "is_market_open", lambda _: False)
    assert paper_quotes.run()["reason"] == "market_closed" and calls == []
