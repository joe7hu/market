from __future__ import annotations

from investment_panel.jobs import full_market_refresh


def test_full_market_refresh_delegates_to_postgresql_composition(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        full_market_refresh.postgres_refresh,
        "full",
        lambda path, continue_on_error: calls.append((path, continue_on_error)) or {"status": "ok", "database": "postgresql:///market"},
    )

    result = full_market_refresh.run(
        "config.yaml", online_check=True, max_filings=2, fetch_holdings=True, continue_on_error=True
    )

    assert result == {"status": "ok", "database": "postgresql:///market"}
    assert calls == [("config.yaml", True)]
