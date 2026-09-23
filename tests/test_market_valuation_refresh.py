from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from investment_panel.infrastructure.postgres.analysis import AnalysisRepository
from investment_panel.workflows.market import refresh_market_publication
from investment_panel.infrastructure.postgres.runtime import DatabaseRuntime
from investment_panel.jobs import update_market_valuations
from conftest import typed_config


class _Response:
    def __init__(self, payload, *, status_code: int = 200, headers: dict[str, str] | None = None) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise update_market_valuations.httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=self
            )

    def json(self):
        return self._payload


def _payload() -> dict[str, object]:
    return {
        "sp500_forward_pe": {
            "label": "S&P 500 Forward P/E",
            "suffix": "x",
            "data": [
                {"date": "2026-08-20", "value": 20.1},
                {"date": "2026-08-21", "value": 20.2},
            ],
        },
        "sp500_price": {
            "label": "S&P 500",
            "suffix": "",
            "data": [{"date": "2026-08-21", "value": 7674.37}],
        },
        "not_consumed_by_market": {"data": [{"date": "2026-08-21", "value": 1}]},
    }


def test_mungermode_refresh_persists_series_and_market_publication(
    migrated_postgres_dsn: str,
    monkeypatch,
) -> None:
    monkeypatch.setattr(update_market_valuations, "load_config", lambda _path: typed_config(migrated_postgres_dsn))
    monkeypatch.setattr(update_market_valuations.httpx, "get", lambda *_args, **_kwargs: _Response(_payload()))

    result = update_market_valuations.run("config.yaml")

    assert result["status"] == "ok"
    assert result["source_status"] == "ok"
    assert result["downstream_status"] == "ok"
    assert result["series"] == 2
    assert result["rows"] == 2
    assert result["market_publication"]["status"] == "ok"

    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        publication = refresh_market_publication(runtime, now=datetime.now(UTC) + timedelta(minutes=1))
        assert publication["valuation_series"] == 2
        rows = AnalysisRepository(runtime).publication_rows("market", "market_valuation_reference_charts")
    finally:
        runtime.close()
    assert {row["metric"] for row in rows} == {"sp500_forward_pe", "sp500_price"}
    forward = next(row for row in rows if row["metric"] == "sp500_forward_pe")
    assert forward["latest_value"] == 20.2
    assert forward["history"][-1] == {"date": "2026-08-21", "value": 20.2}
    assert forward["source"] == "mungermode-market-valuations"


def test_market_publication_defers_when_a_terminal_bar_is_missing(monkeypatch) -> None:
    from investment_panel.workflows import market

    monkeypatch.setattr(market, "terminal_bar_retry", lambda *_args: {
        "status": "partial",
        "reason": "terminal_bar_retry",
        "expected_terminal_bar": "2026-09-22",
        "missing_terminal_bars": ["UNH"],
        "retry_after_seconds": 300,
    })
    monkeypatch.setattr(
        market,
        "load_market_inputs",
        lambda *_args, **_kwargs: pytest.fail("missing terminal bar must not build a Market snapshot"),
    )

    result = refresh_market_publication(
        object(), now=datetime(2026, 9, 22, 21, tzinfo=UTC), require_current_terminal_bars=True,
    )

    assert result == {
        "status": "partial",
        "reason": "terminal_bar_retry",
        "expected_terminal_bar": "2026-09-22",
        "missing_terminal_bars": ["UNH"],
        "retry_after_seconds": 300,
    }


def test_market_publication_rechecks_terminal_bars_before_persisting(monkeypatch) -> None:
    from investment_panel.workflows import market

    retry = {
        "status": "partial",
        "reason": "terminal_bar_retry",
        "expected_terminal_bar": "2026-09-22",
        "missing_terminal_bars": ["UNH"],
        "retry_after_seconds": 300,
    }
    checks = iter((None, retry))
    monkeypatch.setattr(market, "terminal_bar_retry", lambda *_args: next(checks))
    monkeypatch.setattr(market, "load_market_inputs", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(market, "build_market_publication", lambda **_kwargs: {})
    monkeypatch.setattr(
        market,
        "persist_market_publication",
        lambda *_args, **_kwargs: pytest.fail("terminal-bar retry persisted a stale Market snapshot"),
    )

    result = refresh_market_publication(object(), require_current_terminal_bars=True)

    assert result == retry


def test_market_publication_rebuilds_after_a_close_crossing(monkeypatch) -> None:
    from investment_panel.workflows import market

    clock = iter((
        datetime(2026, 9, 22, 19, 59, tzinfo=UTC),
        datetime(2026, 9, 22, 20, 1, tzinfo=UTC),
        datetime(2026, 9, 22, 20, 1, tzinfo=UTC),
    ))

    class Clock:
        @staticmethod
        def now(_timezone):
            return next(clock)

    cutoffs: list[datetime] = []
    monkeypatch.setattr(market, "datetime", Clock)
    monkeypatch.setattr(market, "terminal_bar_retry", lambda *_args: None)
    monkeypatch.setattr(
        market,
        "load_market_inputs",
        lambda _runtime, *, as_of, **_kwargs: cutoffs.append(as_of) or object(),
    )
    monkeypatch.setattr(market, "build_market_publication", lambda **_kwargs: {})
    monkeypatch.setattr(
        market,
        "persist_market_publication",
        lambda _runtime, *, as_of, draft: {"status": "ok", "as_of": as_of, "draft": draft},
    )

    result = refresh_market_publication(
        object(), require_current_terminal_bars=True, recheck_current_terminal_bars=True,
    )

    assert cutoffs == [
        datetime(2026, 9, 22, 19, 59, tzinfo=UTC),
        datetime(2026, 9, 22, 20, 1, tzinfo=UTC),
    ]
    assert result["as_of"] == datetime(2026, 9, 22, 20, 1, tzinfo=UTC)


def test_mungermode_publication_failure_does_not_hide_source_success(
    migrated_postgres_dsn: str,
    monkeypatch,
) -> None:
    monkeypatch.setattr(update_market_valuations, "load_config", lambda _path: typed_config(migrated_postgres_dsn))
    monkeypatch.setattr(update_market_valuations.httpx, "get", lambda *_args, **_kwargs: _Response(_payload()))
    monkeypatch.setattr(
        update_market_valuations,
        "refresh_market_publication",
        lambda _runtime, **_kwargs: (_ for _ in ()).throw(RuntimeError("publication unavailable")),
    )

    result = update_market_valuations.run("config.yaml")

    assert result["status"] == "partial"
    assert result["source_status"] == "ok"
    assert result["downstream_status"] == "failed"
    assert result["market_publication"]["status"] == "failed"


def test_mungermode_refresh_retries_retryable_http_status(
    monkeypatch,
) -> None:
    responses = [_Response({}, status_code=503, headers={"Retry-After": "0"}), _Response(_payload())]
    waits: list[float] = []
    monkeypatch.setattr(update_market_valuations.httpx, "get", lambda *_args, **_kwargs: responses.pop(0))
    monkeypatch.setattr(update_market_valuations.time, "sleep", waits.append)

    payload = update_market_valuations._fetch_payload("https://example.test/metrics")

    assert payload == _payload()
    assert waits == [0.0]
