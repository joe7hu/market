"""A bounded readonly quote collector follows required contracts without enabling trading."""
from dataclasses import replace
from types import SimpleNamespace
import pytest
from investment_panel.settings import AppConfig
from investment_panel.jobs import paper_quotes


def setup(monkeypatch, *, lease=True, fail=False, persist_fail=False):
    config = AppConfig()
    brokers = replace(config.data_sources.brokers, robinhood=replace(config.data_sources.brokers.robinhood, enabled=True))
    config = replace(config, data_sources=replace(config.data_sources, brokers=brokers))
    calls = []
    monkeypatch.setattr(paper_quotes, "load_config", lambda _: config)
    monkeypatch.setattr(paper_quotes, "is_market_open", lambda _: True)
    monkeypatch.setattr(paper_quotes, "runtime_for_config", lambda _: object())
    monkeypatch.setattr(paper_quotes, "active_paper_contracts", lambda *_: [
        {"symbol": f"T{i}", "contract_id": str(i*2+j)} for i in range(41) for j in range(2)])
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
        return {"errors": [], "symbols_attempted": symbols}
    monkeypatch.setattr(paper_quotes, "collect_robinhood_option_chains", collect)
    def persist(*_args, **_kwargs):
        if persist_fail:
            raise RuntimeError("quote persistence unavailable")
        return {"contract_count": 16, "run_id": "capture"}
    monkeypatch.setattr(paper_quotes, "persist_collected_option_chains", persist)
    return calls


def test_only_required_contracts_all_legs_and_twenty_oldest_symbols(monkeypatch):
    calls = setup(monkeypatch)
    result = paper_quotes.run()
    provider, symbols, args = calls[1][1]
    assert provider.readonly and provider.max_collection_seconds == 45 and provider.timeout_seconds == 10
    assert symbols == [f"T{i}" for i in range(20)]
    assert len(args["required_contracts"]) == 40 and args["required_only"]
    assert result["contracts_required"] == 82 and result["remaining_contracts"] == 66
    assert not result["live_brokerage_submission"]
    assert calls[-1] == ("release", "lease")


def test_busy_provider_does_not_bypass_lease(monkeypatch):
    calls = setup(monkeypatch, lease=False)
    assert paper_quotes.run()["reason"] == "provider_capacity_busy"
    assert len(calls) == 1


@pytest.mark.parametrize("failure_stage", ["collection", "persistence"])
def test_failed_capture_releases_provider_lease(monkeypatch, failure_stage):
    calls = setup(monkeypatch, fail=failure_stage == "collection", persist_fail=failure_stage == "persistence")
    result = paper_quotes.run()
    assert result["status"] == "failed"
    assert result["source_status"] == ("failed" if failure_stage == "collection" else "ok")
    assert result["symbols_requested"] == [f"T{i}" for i in range(20)]
    assert result["symbols_attempted"] == ([] if failure_stage == "collection" else [f"T{i}" for i in range(20)])
    assert "contracts_captured" not in result
    assert calls[-1] == ("release", "lease")


def test_closed_session_never_collects(monkeypatch):
    calls = setup(monkeypatch)
    monkeypatch.setattr(paper_quotes, "is_market_open", lambda _: False)
    assert paper_quotes.run()["reason"] == "market_closed" and calls == []
