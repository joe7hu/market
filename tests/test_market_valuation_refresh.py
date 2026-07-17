from __future__ import annotations

from types import SimpleNamespace

from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.market_analysis import refresh_market_publication
from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.jobs import update_market_valuations


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self.payload


def test_market_valuation_refresh_persists_current_series_and_publishes_them(migrated_postgres_dsn: str, monkeypatch) -> None:
    payload = {
        "sp500_forward_pe": {
            "label": "S&P 500 Forward P/E",
            "suffix": "x",
            "higher_is_better": False,
            "data": [{"date": "2026-07-16", "value": 21.1}, {"date": "2026-07-17", "value": 21.2}],
        },
        "shiller_pe": {
            "label": "Shiller P/E (CAPE)",
            "suffix": "x",
            "higher_is_better": False,
            "data": [{"date": "2026-07-01", "value": 40.9}, {"date": "2026-07-16", "value": 41.8}],
        },
        "unrelated": {"data": [{"date": "2026-07-17", "value": 1}]},
    }
    config = SimpleNamespace(database=SimpleNamespace(url=migrated_postgres_dsn))
    monkeypatch.setattr(update_market_valuations, "load_config", lambda _path: config)
    monkeypatch.setattr(update_market_valuations.httpx, "get", lambda *_args, **_kwargs: _Response(payload))

    result = update_market_valuations.run("test.yaml")

    assert result["status"] == "ok"
    assert result["series"] == 2
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        refresh_market_publication(runtime)
        rows = AnalysisRepository(runtime).publication_rows("market", "market_valuation_reference_charts")
    finally:
        runtime.close()
    by_metric = {row["metric"]: row for row in rows}
    assert str(by_metric["sp500_forward_pe"]["latest_date"]) == "2026-07-17"
    assert by_metric["sp500_forward_pe"]["latest_value"] == 21.2
    assert by_metric["shiller_pe"]["history"][-1] == {"date": "2026-07-16", "value": 41.8}


def test_market_valuation_refresh_reports_a_failed_source_without_writing_empty_series(migrated_postgres_dsn: str, monkeypatch) -> None:
    config = SimpleNamespace(database=SimpleNamespace(url=migrated_postgres_dsn))
    monkeypatch.setattr(update_market_valuations, "load_config", lambda _path: config)
    monkeypatch.setattr(update_market_valuations.httpx, "get", lambda *_args, **_kwargs: _Response({"unknown": {}}))

    result = update_market_valuations.run("test.yaml")

    assert result["status"] == "failed"
    assert "no supported series" in result["error"]
