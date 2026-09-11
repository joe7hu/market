from datetime import UTC, date, datetime, timedelta

from investment_panel.domain.strategies.catalog import resolve_builtin_strategy, requirements_for_strategy
from investment_panel.infrastructure.postgres import strategy_inputs


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class _Connection:
    def execute(self, query, _params):
        if "catalog.instrument" in query:
            return _Result([{"id": 7, "symbol": "AAA"}])
        raise AssertionError(f"unexpected loader query: {query}")


class _OptionConnection:
    def execute(self, query, _params):
        if "raw.option_snapshot" in query:
            return _Result([{
                "symbol": "AAA", "instrument_id": 7, "snapshot_id": 19, "source_id": "options-test",
                "observed_at": datetime(2026, 9, 10, 19, tzinfo=UTC),
                "available_at": datetime(2026, 9, 10, 19, 1, tzinfo=UTC),
                "capture_state": "complete", "contract_count": 12, "collection_profile": "history_full",
                "completeness": 1.0,
                "quote_count": 12, "oi_volume_count": 12,
                "dividend_observed_at": None, "dividend_available_at": None, "dividend_fact_id": None,
            }])
        if "catalog.instrument" in query:
            return _Result([{"id": 7, "symbol": "AAA"}])
        raise AssertionError(f"unexpected loader query: {query}")


def test_parameter_dependent_loader_requirement_supplies_lookback_plus_one(monkeypatch):
    spec = resolve_builtin_strategy("daily_trend_underreaction_v2").model_copy(update={"parameters": {"lookback_days": 252}})
    requirement = requirements_for_strategy(spec)[0]
    assert requirement.sessions == 253
    dates = tuple(date(2026, 1, 1) + timedelta(days=item) for item in range(253))
    observed = {}

    monkeypatch.setattr(strategy_inputs, "completed_trading_dates", lambda _as_of, count: dates[-count:])

    def fake_bars(_connection, instrument_ids, **kwargs):
        observed.update(kwargs)
        return {
            int(instrument_ids[0]): [
                {"trading_date": item, "close": 100 + index, "fact_id": index, "fact_table": "raw.price_bar"}
                for index, item in enumerate(dates)
            ],
        }

    monkeypatch.setattr(strategy_inputs, "confirmed_daily_bars", fake_bars)
    loaded = strategy_inputs.load_strategy_inputs(
        _Connection(), (spec,), as_of=datetime(2026, 9, 11, 13, tzinfo=UTC), symbols=("AAA",),
    )
    assert observed["max_bars"] == 253
    assert observed["require_session_close"] is True
    assert len(loaded["AAA"]["daily_bars"]) == 253
    assert len(loaded["AAA"]["required_trading_dates"]) == 253


def test_option_requirement_loads_point_in_time_chain_and_quote_states(monkeypatch):
    spec = resolve_builtin_strategy("options_recovery_v2")
    monkeypatch.setattr(strategy_inputs, "confirmed_daily_bars", lambda *_args, **_kwargs: {})
    loaded = strategy_inputs.load_strategy_inputs(
        _OptionConnection(), (spec,), as_of=datetime(2026, 9, 11, 13, tzinfo=UTC), symbols=("AAA",),
    )
    assert loaded["AAA"]["full_chain_state"]["status"] == "confirmed"
    assert loaded["AAA"]["oi_volume_state"]["status"] == "confirmed"
    assert loaded["AAA"]["dividend_state"]["status"] == "unavailable"
    assert loaded["AAA"]["option_evidence_refs"] == ("raw.option_snapshot:19",)
