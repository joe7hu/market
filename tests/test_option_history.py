from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from investment_panel.core.robinhood_options.history import collect_robinhood_full_option_chain
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.options_history import OptionHistoryRepository
from investment_panel.database.retention import RetentionRepository
from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.jobs.robinhood_option_history import history_slot


class FullChainClient:
    def get_equity_quotes(self, symbols):
        return {"data": {"results": [{"quote": {"symbol": symbols[0], "last_trade_price": "500"}}]}}

    def get_option_chains(self, underlying_symbol):
        return {"data": {"chains": [{"id": "qqq", "expiration_dates": ["2026-08-21"]}]}}

    def get_option_instruments(self, *, chain_id=None, expiration_dates=None, option_type=None, cursor=None, **_kwargs):
        strikes = [495, 500, 505] if cursor is None else []
        rows = [{"id": f"{option_type}-{strike}", "chain_id": chain_id, "chain_symbol": "QQQ", "expiration_date": expiration_dates, "strike_price": str(strike), "type": option_type, "tradability": "tradable"} for strike in strikes]
        return {"data": {"instruments": rows, "next": None}}

    def get_option_quotes(self, instrument_ids):
        return {"data": {"results": [{"quote": {"instrument_id": instrument_id, "bid_price": "2.0", "ask_price": "2.2", "mark_price": "2.1", "previous_close_price": "1.9", "implied_volatility": "0.20", "delta": "0.25", "gamma": "0.02", "theta": "-0.01", "vega": "0.10", "rho": "0.03", "open_interest": 100, "volume": 20, "updated_at": "2026-07-20T14:30:00Z"}} for instrument_id in instrument_ids]}}


class PartialQuoteClient(FullChainClient):
    def __init__(self) -> None:
        self.calls: dict[tuple[str, ...], int] = {}

    def get_option_quotes(self, instrument_ids):
        key = tuple(sorted(instrument_ids))
        self.calls[key] = self.calls.get(key, 0) + 1
        returned = instrument_ids[:1] if self.calls[key] == 1 else instrument_ids
        return super().get_option_quotes(returned)


class FourthAttemptQuoteClient(FullChainClient):
    def __init__(self) -> None:
        self.calls = 0

    def get_option_quotes(self, instrument_ids):
        self.calls += 1
        return super().get_option_quotes([] if self.calls < 4 else instrument_ids)


def test_full_collector_covers_all_expiries_types_and_preserves_payload() -> None:
    captured = collect_robinhood_full_option_chain(SimpleNamespace(quote_batch_size=2, max_collection_seconds=30), "QQQ", client=FullChainClient())
    assert captured["expected_contract_count"] == 6
    assert captured["received_contract_count"] == 6
    assert {row["type"] for row in captured["rows"]} == {"call", "put"}
    assert all(row["provider_payload"]["instrument"]["id"] for row in captured["rows"])
    assert all(row["provider_payload"]["quote"]["rho"] == "0.03" for row in captured["rows"])


def test_full_collector_retries_incomplete_quote_batches() -> None:
    client = PartialQuoteClient()
    captured = collect_robinhood_full_option_chain(
        SimpleNamespace(quote_batch_size=2, max_collection_seconds=30), "QQQ", client=client
    )
    assert captured["received_contract_count"] == captured["expected_contract_count"] == 6
    assert captured["errors"] == []
    assert captured["quote_diagnostics"]["retries"] == 3
    assert captured["quote_diagnostics"]["missing_quote_count"] == 0


def test_full_collector_uses_final_retry_for_missing_quote_batch() -> None:
    client = FourthAttemptQuoteClient()
    captured = collect_robinhood_full_option_chain(
        SimpleNamespace(quote_batch_size=6, max_collection_seconds=30), "QQQ", client=client
    )
    assert captured["received_contract_count"] == captured["expected_contract_count"] == 6
    assert captured["errors"] == []
    assert client.calls == 4
    assert captured["quote_diagnostics"]["retries"] == 3


def test_history_slot_skips_holiday_and_includes_close() -> None:
    assert history_slot(datetime(2026, 7, 3, 15, 0, tzinfo=UTC)) is None  # observed Independence Day
    slot = history_slot(datetime(2026, 7, 20, 20, 5, tzinfo=UTC))
    assert slot == datetime(2026, 7, 20, 20, 0, tzinfo=UTC)


def test_history_snapshot_persists_complete_rows_and_excludes_partial(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    ingestion = IngestionRepository(runtime)
    history = OptionHistoryRepository(runtime)
    ingestion.register_source("robinhood", name="Robinhood", family="broker", kind="option_chain")
    slot = datetime(2026, 7, 20, 14, 30, tzinfo=UTC)
    run_id = ingestion.start_run("robinhood", "option_history_full")
    assert history.claim_slot(source_id="robinhood", symbol="QQQ", slot_at=slot, run_id=run_id)
    rows = [
        {
            "underlying_symbol": "QQQ", "expiry": "2026-08-21", "strike": strike, "type": option_type,
            "underlying_price": 500, "bid": 2.0, "ask": 2.2, "mid": 2.1, "iv": 0.20 + index * 0.01,
            "delta": 0.25 if option_type == "call" else -0.25, "gamma": 0.02, "theta": -0.01,
            "vega": 0.1, "rho": 0.03, "open_interest": 100, "volume": 20,
            "provider_payload": {"instrument": {"id": f"{option_type}-{strike}"}, "quote": {"rho": "0.03"}},
        }
        for index, (option_type, strike) in enumerate((kind, strike) for kind in ("call", "put") for strike in (495, 500, 505))
    ]
    stored = history.store_capture(run_id=run_id, source_id="robinhood", symbol="QQQ", slot_at=slot, captured={"rows": rows, "expected_contract_count": 6, "received_contract_count": 6, "capture_started_at": slot, "capture_finished_at": slot})
    ingestion.finish_run(run_id, "succeeded", summary=stored)
    assert stored["capture_state"] == "complete"
    assert history.chain(symbol="QQQ")["count"] == 6
    assert history.surface(symbol="QQQ")["surfaces"]["call"]
    curves = history.curves(symbol="QQQ")
    assert curves["history_state"] == "collecting"
    assert all(row["skew_25"] is not None for row in curves["term_structure"])
    with runtime.read() as connection:
        raw = connection.execute("SELECT provider_rho, provider_payload FROM raw.option_quote LIMIT 1").fetchone()
    assert raw["provider_rho"] == 0.03
    assert raw["provider_payload"]["quote"]["rho"] == "0.03"

    partial_slot = datetime(2026, 7, 20, 14, 45, tzinfo=UTC)
    partial_run = ingestion.start_run("robinhood", "option_history_full")
    assert history.claim_slot(source_id="robinhood", symbol="QQQ", slot_at=partial_slot, run_id=partial_run)
    partial = history.store_capture(run_id=partial_run, source_id="robinhood", symbol="QQQ", slot_at=partial_slot, captured={"rows": rows[:1], "expected_contract_count": 6, "received_contract_count": 1, "capture_started_at": partial_slot, "capture_finished_at": partial_slot})
    ingestion.finish_run(partial_run, "partial", summary=partial)
    assert partial["capture_state"] == "partial"
    assert history.snapshots(symbol="QQQ")["count"] == 1
    assert history.snapshots(symbol="QQQ", include_partial=True)["count"] == 2
    second_run = ingestion.start_run("robinhood", "option_history_full")
    assert history.claim_slot(source_id="robinhood", symbol="QQQ", slot_at=slot, run_id=second_run) is None
    ingestion.finish_run(second_run, "skipped")
    runtime.close()


def test_history_profile_uses_730_day_retention(migrated_postgres_dsn: str) -> None:
    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    ingestion = IngestionRepository(runtime)
    history = OptionHistoryRepository(runtime)
    ingestion.register_source("robinhood", name="Robinhood", family="broker", kind="option_chain")
    observed = datetime(2026, 7, 20, 14, 30, tzinfo=UTC)
    run_id = ingestion.start_run("robinhood", "option_history_full")
    history.claim_slot(source_id="robinhood", symbol="QQQ", slot_at=observed, run_id=run_id)
    stored = history.store_capture(run_id=run_id, source_id="robinhood", symbol="QQQ", slot_at=observed, captured={"rows": [{"underlying_symbol": "QQQ", "expiry": "2026-08-21", "strike": 500, "type": "call", "underlying_price": 500, "bid": 2, "ask": 2.2, "mid": 2.1, "iv": 0.2}], "expected_contract_count": 1, "received_contract_count": 1, "capture_started_at": observed, "capture_finished_at": observed})
    ingestion.finish_run(run_id, "succeeded", summary=stored)
    RetentionRepository(runtime).prune(now=datetime(2026, 7, 30, 14, 30, tzinfo=UTC), option_days=1)
    assert history.chain(symbol="QQQ")["count"] == 1
    runtime.close()
