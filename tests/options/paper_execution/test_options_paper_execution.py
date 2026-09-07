from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from psycopg.types.json import Jsonb

from investment_panel.jobs import options_paper_execution
from investment_panel.core.decision import ExpressionKind
from investment_panel.core.option_trade_ticket import exit_reason
from investment_panel.database import options_paper_execution as paper_execution_database
from investment_panel.database import ticker_execution as ticker_execution_database
from investment_panel.database.instruments import reconcile_instrument
from investment_panel.database.options_paper_execution import GENERIC_LANES, OptionsPaperExecutionRepository
from investment_panel.database.ticker_execution import TickerPaperExecutionRepository
from investment_panel.database.options_paper_ledger import active_paper_exposure
from investment_panel.database.options_paper_execution import (
    available_quantity,
    net_pnl,
)
from investment_panel.database.options_paper_quotes import package_price


NOW = datetime(2026, 8, 12, 15, 30, tzinfo=UTC)


class _Result:
    def __init__(self, row=None) -> None:
        self.row = row

    def fetchone(self):
        return self.row


class _RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[tuple[str, object]] = []

    def execute(self, statement: str, parameters=None) -> _Result:
        self.statements.append((statement, parameters))
        return _Result()


def _open_order(*, filled_quantity: float = 1, exited_quantity: float = 0) -> dict[str, object]:
    return {
        "id": "paper-order-1",
        "instrument_id": 1,
        "lane": "radar",
        "status": "entered",
        "quantity": filled_quantity,
        "filled_quantity": filled_quantity,
        "exited_quantity": exited_quantity,
        "actual_fill_price": 1.0,
        "structure": "long_option",
    }


def _executable_long_quote(*, quote_time: datetime, bid_size: int = 1) -> dict[str, object]:
    return {
        "contract_id": "1",
        "side": "buy",
        "bid": 2.0,
        "ask": 2.1,
        "bid_size": bid_size,
        "ask_size": bid_size,
        "open_interest": 100,
        "quote_time": quote_time,
    }


def test_paper_fill_prices_are_never_better_than_the_displayed_market_side() -> None:
    debit = [{"side": "buy", "bid": 1.0, "ask": 1.2, "bid_size": 4, "ask_size": 2}]
    credit = [{"side": "sell", "bid": 1.0, "ask": 1.2, "bid_size": 2, "ask_size": 4}]
    assert package_price(debit, phase="entry") == 1.2
    assert package_price(debit, phase="exit") == 1.0
    assert package_price(credit, phase="entry") == 1.0
    assert package_price(credit, phase="exit") == 1.2
    assert available_quantity(debit, phase="entry", requested=5) == 2
    assert available_quantity(debit, phase="exit", requested=5) == 4


def test_paper_exit_uses_profit_stop_time_and_liquidity_gates() -> None:
    ticket = {"expiration": "2026-09-30", "expires_at": "2026-08-12T20:00:00+00:00"}
    exits = {"profit_price": 2.0, "loss_price": 0.5, "time_exit_dte": 7}
    assert exit_reason(
        ticket=ticket, exits=exits, credit=False, entry_price=1.0,
        exit_price=2.05, execution_blockers=[], now=NOW,
    ) == "profit_target"
    assert exit_reason(
        ticket=ticket, exits=exits, credit=False, entry_price=1.0,
        exit_price=0.45, execution_blockers=[], now=NOW,
    ) == "stop_loss"
    assert exit_reason(
        ticket=ticket, exits=exits, credit=True, entry_price=1.0,
        exit_price=0.45, execution_blockers=[], now=NOW,
    ) == "profit_target"
    assert exit_reason(
        ticket=ticket, exits=exits, credit=False, entry_price=1.0,
        exit_price=1.0, execution_blockers=["long_leg_open_interest_below_100"], now=NOW,
    ) == "liquidity_exit"
    assert exit_reason(
        ticket={**ticket, "expires_at": (NOW - timedelta(seconds=1)).isoformat()},
        exits=exits, credit=False, entry_price=1.0, exit_price=1.0, execution_blockers=[], now=NOW,
    ) is None
    assert exit_reason(
        ticket={**ticket, "expiration": (NOW.date() + timedelta(days=7)).isoformat()},
        exits=exits, credit=False, entry_price=1.0, exit_price=1.0, execution_blockers=[], now=NOW,
    ) == "time_exit"


@pytest.mark.parametrize("entry_blocker", ["ticket_expired", "candidate entry blocked_terminal_evidence"])
def test_blocked_partial_entry_keeps_the_filled_quantity_in_holding_management(monkeypatch, entry_blocker) -> None:
    order = {**_open_order(filled_quantity=1), "quantity": 2, "status": "open",
             "ticket_snapshot": {"expires_at": (NOW - timedelta(seconds=1)).isoformat()}}

    class Connection(_RecordingConnection):
        def execute(self, statement, parameters=None):
            if "FOR UPDATE OF paper" in statement:
                return _Result(order)
            if "FROM app.paper_order_leg" in statement:
                return SimpleNamespace(fetchall=lambda: [{"contract_id": 1}])
            return super().execute(statement, parameters)

    connection = Connection()

    @contextmanager
    def transaction(*_args, **_kwargs):
        yield connection

    repository = OptionsPaperExecutionRepository.__new__(OptionsPaperExecutionRepository)
    repository.runtime = SimpleNamespace(transaction=transaction)

    def current_ticket(_connection, _order, ticket, **kwargs):
        if kwargs.get("for_entry", True):
            return None, entry_blocker
        return ticket, ""

    def manage_open(_connection, item, _ticket, _legs, _now, **kwargs):
        assert item["status"] == "entered" and item["filled_quantity"] == 1 and item["quantity"] == 2
        cancelled = item["execution_quote"][paper_execution_database.ENTRY_CANCELLATION_KEY]
        assert cancelled == {"status": "cancelled", "paper_order_id": "paper-order-1", "cancelled_at": NOW.isoformat(),
                             "reason": entry_blocker, "requested_quantity": 2, "filled_quantity": 1, "cancelled_quantity": 1}
        assert kwargs["forced_exit_reason"] is None
        return {"status": "filled", "reason": "exit_not_triggered"}

    monkeypatch.setattr(repository, "_current_ticket", current_ticket)
    monkeypatch.setattr(repository, "_manage_open", manage_open)
    monkeypatch.setattr(paper_execution_database, "_thesis_blocker", lambda *_args: None)
    result = repository._manage_one("paper-order-1", NOW)
    assert result["reason"] == "exit_not_triggered"
    assert any(parameters[0] == f"{entry_blocker}: unfilled_remainder_cancelled"
               for statement, parameters in connection.statements if "unfilled_reason" in statement)
    assert not any("app.trade_journal" in statement for statement, _ in connection.statements)


def test_paper_net_pnl_includes_both_sides_of_conservative_fees() -> None:
    # One long contract bought at 1.20 and sold at 2.00: 80 gross less 1.30 fees.
    assert net_pnl(credit=False, entry_price=1.2, exit_price=2.0, quantity=1, leg_count=1) == 78.7


def test_phase4_option_execution_math_and_coercion_are_conservative() -> None:
    legs = [{"side": "sell", "bid": 1.0, "ask": 1.2}, {"side": "buy", "bid": 0.2, "ask": 0.4}]
    assert paper_execution_database._midpoint_package(legs) == 0.8
    assert paper_execution_database._entry_slippage(legs, 0.7, True) == 0.1
    assert paper_execution_database._entry_slippage(legs, 1.0, False) == 0.2
    assert paper_execution_database._exit_slippage(legs, 1.0, True) == 0.2
    assert paper_execution_database._exit_slippage(legs, 0.7, False) == 0.1
    assert paper_execution_database._midpoint_package([{"bid": 0, "ask": 1}]) is None
    assert paper_execution_database._entry_slippage([{"bid": 0, "ask": 1}], 1, True) is None
    assert paper_execution_database._exit_slippage([{"bid": 0, "ask": 1}], 1, False) is None
    assert paper_execution_database._fees(2, 3) == 3.9
    assert paper_execution_database._net_pnl(credit=False, entry_price=1.0, exit_price=2.0, quantity=2, leg_count=1) == 197.4
    assert paper_execution_database._timestamp(NOW.isoformat()) == NOW
    assert paper_execution_database._timestamp(NOW.replace(tzinfo=None)) == NOW
    assert paper_execution_database._timestamp("bad") is None
    assert paper_execution_database._utc(None).tzinfo is UTC
    assert paper_execution_database._date(NOW) == NOW.date()
    assert paper_execution_database._date(NOW.date()) == NOW.date()
    assert paper_execution_database._date("2026-08-12") == NOW.date()
    assert paper_execution_database._date("bad") is None
    assert paper_execution_database._number(None) is None
    assert paper_execution_database._number("1.5") == 1.5
    assert paper_execution_database._number("") is None
    assert paper_execution_database._number("bad") is None
    assert paper_execution_database._integer("2") == 2
    assert paper_execution_database._integer(None) is None
    assert paper_execution_database._integer("") is None
    assert paper_execution_database._integer("bad") is None
    assert paper_execution_database._quantity("2.5") == 2.5
    assert paper_execution_database._quantity(None) == 0
    assert paper_execution_database._quantity("bad") == 0
    assert str(paper_execution_database._uuid("00000000-0000-0000-0000-000000000001")) == "00000000-0000-0000-0000-000000000001"


def test_phase4_ticker_option_guards_validate_sizes_quotes_and_dates() -> None:
    leg = {
        "contract_id": "contract:1", "option_type": "put", "side": "sell", "strike": 100,
        "bid": 2.0, "ask": 2.2, "bid_size": 3, "ask_size": 4, "quote_time": NOW,
        "expiration": date(2026, 9, 18),
    }
    assert ticker_execution_database._complete_option_legs([leg])
    assert not ticker_execution_database._complete_option_legs([{**leg, "contract_id": None}])
    assert ticker_execution_database._option_available_quantity([leg], 5, phase="entry") == 3
    assert ticker_execution_database._option_available_quantity([leg], 5, phase="exit") == 4
    assert ticker_execution_database._option_available_quantity([{**leg, "bid_size": 0}], 5, phase="entry") == 0
    assert ticker_execution_database._option_available_quantity([{**leg, "ask_size": 0}], 5, phase="exit") == 0
    assert ticker_execution_database._option_available_quantity([{**leg, "ask_size": None}], 5, phase="exit") == 0
    assert ticker_execution_database._option_available_quantity([], 5, phase="entry") == 0
    assert ticker_execution_database._option_midpoint([leg]) == 2.1
    assert ticker_execution_database._option_midpoint([]) is None
    assert ticker_execution_database._option_midpoint([{**leg, "bid": -1}]) is None
    assert ticker_execution_database._option_midpoint([{**leg, "ask": 1.0}]) is None
    assert ticker_execution_database._option_midpoint([{**leg, "ask": 1.0}]) is None
    assert ticker_execution_database._option_expiration({}, [leg]) == date(2026, 9, 18)
    assert ticker_execution_database._option_expiration({"expiration": "2026-09-19"}, []) == date(2026, 9, 19)
    assert ticker_execution_database._option_expiration({"legs": [{"expiration": "2026-09-20"}]}, []) == date(2026, 9, 20)
    assert ticker_execution_database._option_expiration({"expiration": NOW}, []) == NOW.date()
    assert ticker_execution_database._option_expiration({"expiration": "bad"}, []) is None
    assert ticker_execution_database._option_expiration({}, []) is None
    assert not ticker_execution_database._complete_option_legs([{**leg, "quote_time": "bad"}])
    assert ticker_execution_database._limit_reached("buy", 99, 100)
    assert ticker_execution_database._limit_reached("sell", 101, 100)
    assert not ticker_execution_database._limit_reached("buy", 101, 100)
    assert not ticker_execution_database._limit_reached("sell", 99, 100)
    assert ticker_execution_database._option_structure(ExpressionKind.CALL) == "long_call"
    assert ticker_execution_database._option_structure(ExpressionKind.PUT) == "long_put"
    assert ticker_execution_database._option_structure(ExpressionKind.DEBIT_SPREAD) == "debit_spread"
    assert ticker_execution_database._option_structure(ExpressionKind.CASH_SECURED_PUT) == "cash_secured_put"
    assert ticker_execution_database._utc(NOW.replace(tzinfo=None)) == NOW
    assert ticker_execution_database._utc(None).tzinfo is UTC
    assert ticker_execution_database._timestamp(NOW) == NOW
    assert ticker_execution_database._number("bad") is None
    assert ticker_execution_database._number(float("inf")) is None
    assert ticker_execution_database._quantity(-2) == 0
    assert ticker_execution_database._timestamp("bad") is None


def test_phase4_ticker_paper_switches_fail_closed() -> None:
    repo = object.__new__(TickerPaperExecutionRepository)
    settings = SimpleNamespace(
        mode="paper", ticker_paper_actions_enabled=True,
        stock_paper_actions_enabled=True, options_paper_actions_enabled=True,
    )
    repo.config = SimpleNamespace(analysis=SimpleNamespace(options_decision_system=settings))
    repo._check_switches(ExpressionKind.STOCK)
    repo._check_switches(ExpressionKind.CALL)
    for field, kind in (
        ("mode", ExpressionKind.STOCK), ("ticker_paper_actions_enabled", ExpressionKind.STOCK),
        ("stock_paper_actions_enabled", ExpressionKind.STOCK), ("options_paper_actions_enabled", ExpressionKind.CALL),
    ):
        original = getattr(settings, field)
        setattr(settings, field, "live" if field == "mode" else False)
        with pytest.raises(ValueError):
            repo._check_switches(kind)
        setattr(settings, field, original)


@pytest.mark.parametrize(
    ("global_enabled", "radar_enabled", "qqq_enabled", "expected_lanes"),
    [
        (False, False, False, []),
        (True, True, False, ["radar"]),
        (True, False, True, ["qqq"]),
        (True, True, True, ["radar", "qqq"]),
    ],
)
def test_lane_switches_only_control_entry_staging(
    monkeypatch,
    global_enabled: bool,
    radar_enabled: bool,
    qqq_enabled: bool,
    expected_lanes: list[str],
) -> None:
    calls: dict[str, object] = {}

    class _Repository:
        def __init__(self, _runtime) -> None:
            pass

        def process(self, **kwargs):
            calls["process"] = kwargs
            return {
                "status": "ok",
                "entry_staging": "enabled" if kwargs["enabled_lanes"] else "disabled",
                "staged": [],
                "managed": [{"paper_order_id": "existing", "status": "closed"}],
            }

    settings = SimpleNamespace(
        options_paper_actions_enabled=global_enabled,
        radar_paper_actions_enabled=radar_enabled,
        qqq_paper_actions_enabled=qqq_enabled,
        options_risk_sleeve_capital=25_000,
        daily_loss_halt_pct=0.02,
        max_recovery_open_positions=2,
        decision_inbox_enabled=True,
    )
    monkeypatch.setattr(
        options_paper_execution,
        "load_config",
        lambda _path: SimpleNamespace(analysis=SimpleNamespace(options_decision_system=settings)),
    )
    monkeypatch.setattr(options_paper_execution, "runtime_for_config", lambda _config: object())
    monkeypatch.setattr(options_paper_execution, "OptionsPaperExecutionRepository", _Repository)
    monkeypatch.setattr(options_paper_execution, "advance_experiment_shadows", lambda *_args, **_kwargs: {"closed": 1})

    result = options_paper_execution.run("config.yaml")

    assert result["paper_only"] is True
    assert result["experiments"]["status"] == "disabled" and result["experiments"]["observations"]["closed"] == 1
    assert calls["process"]["enabled_lanes"] == expected_lanes


def test_process_stages_enabled_lanes_but_manages_all_existing_generic_positions(monkeypatch) -> None:
    repository = OptionsPaperExecutionRepository.__new__(OptionsPaperExecutionRepository)
    calls: dict[str, object] = {}

    def stage_current_ready(**kwargs):
        calls["stage"] = kwargs
        return [{"paper_order_id": "new-radar-order"}]

    def manage_orders(**kwargs):
        calls["manage"] = kwargs
        return [{"paper_order_id": "existing-qqq-order"}]

    monkeypatch.setattr(repository, "stage_current_ready", stage_current_ready)
    monkeypatch.setattr(repository, "manage_orders", manage_orders)

    result = repository.process(
        enabled_lanes=("radar",),
        sleeve_capital=25_000,
        daily_loss_halt_pct=0.02,
        max_open_positions=2,
        decision_inbox_enabled=True,
        now=NOW,
    )

    assert calls["stage"]["enabled_lanes"] == ("radar",)
    assert calls["manage"]["lanes"] == GENERIC_LANES
    assert result["managed"] == [{"paper_order_id": "existing-qqq-order"}]


@pytest.mark.parametrize(
    ("market_open", "quote_time", "expected_blocker"),
    [
        (True, NOW - timedelta(seconds=121), "quote_age_over_120_seconds"),
        (False, NOW - timedelta(seconds=1), "regular_market_session_required"),
    ],
)
def test_unexecutable_exit_quote_never_books_exit_or_pnl(
    monkeypatch,
    market_open: bool,
    quote_time: datetime,
    expected_blocker: str,
) -> None:
    connection = _RecordingConnection()
    repository = OptionsPaperExecutionRepository.__new__(OptionsPaperExecutionRepository)
    quote = _executable_long_quote(quote_time=quote_time)
    monkeypatch.setattr(paper_execution_database, "latest_option_legs", lambda *_args, **_kwargs: [quote])
    monkeypatch.setattr(paper_execution_database, "is_market_open", lambda _now: market_open)

    result = repository._manage_open(
        connection,
        _open_order(),
        {
            "expiration": "2026-09-30",
            "exits": {"profit_price": 1.5, "loss_price": 0.5, "time_exit_dte": 7},
        },
        [{"contract_id": "1"}],
        NOW,
    )

    assert result["reason"] == "profit_target_pending_executable_quote"
    assert expected_blocker in result["blockers"]
    assert any("unfilled_reason" in statement for statement, _ in connection.statements)
    assert not any("exit_price" in statement for statement, _ in connection.statements)
    assert not any("app.trade_journal" in statement for statement, _ in connection.statements)


def test_partial_exit_keeps_residual_position_open_until_all_filled_contracts_exit(monkeypatch) -> None:
    connection = _RecordingConnection()
    repository = OptionsPaperExecutionRepository.__new__(OptionsPaperExecutionRepository)
    quote = _executable_long_quote(quote_time=NOW - timedelta(seconds=1), bid_size=1)
    monkeypatch.setattr(paper_execution_database, "latest_option_legs", lambda *_args, **_kwargs: [quote])
    monkeypatch.setattr(paper_execution_database, "is_market_open", lambda _now: True)

    result = repository._manage_open(
        connection,
        _open_order(filled_quantity=3, exited_quantity=1),
        {
            "expiration": "2026-09-30",
            "exits": {"profit_price": 1.5, "loss_price": 0.5, "time_exit_dte": 7},
        },
        [{"contract_id": "1"}],
        NOW,
    )

    assert result["status"] == "filled"
    assert result["exit_quantity"] == 1
    update_parameters = next(
        parameters
        for statement, parameters in connection.statements
        if "SET status = %s, exited_quantity" in statement
    )
    assert update_parameters[0] == "partial_exited"
    assert update_parameters[1] == 2


def test_paper_liquidation_mark_uses_all_entry_fills_and_partial_exit_cash() -> None:
    class JournalConnection(_RecordingConnection):
        def execute(self, statement, parameters=None):
            if "AS entry_quantity" in statement:
                # Two entries at .80 and 1.00; one prior exit at 1.20.
                return _Result({"entry_quantity": 2, "exit_quantity": 1, "entry_units": 1.8,
                                "exit_units": 1.2, "actual_fees": 1.95, "missing_fees": 0,
                                "journal_ids": ["entry-a", "entry-b", "exit-a"]})
            return super().execute(statement, parameters)

    at = datetime.now(UTC)
    connection = JournalConnection()
    key = paper_execution_database.PAPER_MARK_KEY
    order = {**_open_order(filled_quantity=2, exited_quantity=1), "decision_id": "decision-id",
             "actual_fill_price": .8, "contract_multiplier": 100, "fees": 1.95,
             "execution_quote": {key: {"entry_cash": 180, "peak_net_return": .1,
                 "peak_at": (at - timedelta(seconds=10)).isoformat(), "mark_count": 1,
                 "quote_ids": ["earlier-quote"], "observed_max_drawdown": 0}}}
    quote = {**_executable_long_quote(quote_time=at, bid_size=1), "quote_id": "current-quote",
             "bid": .7, "ask": .8, "multiplier": 100, "observed_at": at, "capture_complete": True}
    paper_execution_database._record_liquidation_mark(connection, order, [quote], now=at, execution_blockers=[])
    mark = connection.statements[-1][1][0].obj[key]
    expected = (120 + 70 - 180 - 1.95 - .65) / 180
    assert mark["status"] == "observed" and mark["remaining_quantity"] == 1
    assert mark["current_net_return"] == pytest.approx(expected)
    assert mark["max_drawdown"] == pytest.approx((1 + expected) / 1.1 - 1)
    assert mark["actual_fees"] == 1.95 and mark["modeled_remaining_exit_fees"] == .65


@pytest.mark.parametrize("valid_cancellation,entry_quote_available", [(True, True), (True, False), (False, True)])
def test_cancelled_partial_holding_measures_its_exact_filled_basis(monkeypatch, valid_cancellation, entry_quote_available) -> None:
    class JournalConnection(_RecordingConnection):
        def execute(self, statement, parameters=None):
            if "AS entry_quantity" in statement:
                return _Result({"entry_quantity": 1, "exit_quantity": 0, "entry_units": .5, "exit_units": 0,
                                "actual_fees": .65, "missing_fees": 0,
                                "journal_ids": ["actual-entry"], "entry_journal_ids": ["actual-entry"]})
            return super().execute(statement, parameters)

    at = datetime.now(UTC)
    connection = JournalConnection()
    key, cancellation_key = paper_execution_database.PAPER_MARK_KEY, paper_execution_database.ENTRY_CANCELLATION_KEY
    order = {**_open_order(filled_quantity=1), "quantity": 2, "status": "open", "decision_id": "decision-id",
             "actual_fill_price": .5, "filled_at": at, "contract_multiplier": 100, "fees": .65, "execution_quote": {}}
    quote = {**_executable_long_quote(quote_time=at), "quote_id": "actual-entry-quote",
             "bid": .48, "ask": .5, "multiplier": 100, "observed_at": at, "capture_complete": entry_quote_available}
    paper_execution_database._record_liquidation_mark(connection, order, [quote], now=at, execution_blockers=[])
    pending = connection.statements[-1][1][0].obj[key]
    assert pending["status"] == "unknown"
    assert pending["current_net_return"] is None and pending["max_drawdown"] is None
    if entry_quote_available:
        assert pending["reason"] == "entry_fill_quantity_incomplete" and pending["entry_quantity_fixed"] is False
        assert pending["mark_count"] == 1 and pending["entry_cash"] == 50 and pending["quotes"][0]["quote_id"] == "actual-entry-quote"
    else:
        assert pending["reason"] == "complete_fresh_executable_mark_unavailable" and pending.get("mark_count", 0) == 0
    cancellation = {"status": "cancelled", "paper_order_id": order["id"], "cancelled_at": (at + timedelta(seconds=10)).isoformat(),
                    "requested_quantity": 2, "filled_quantity": 1 if valid_cancellation else 2, "cancelled_quantity": 1}
    order.update(status="entered", execution_quote={key: pending, cancellation_key: cancellation})
    later = {**quote, "quote_id": "later-holding-quote", "bid": .6, "ask": .62, "capture_complete": True,
             "observed_at": at + timedelta(seconds=11), "quote_time": at + timedelta(seconds=11)}
    paper_execution_database._record_liquidation_mark(connection, order, [later], now=at + timedelta(seconds=11), execution_blockers=[])
    mark = connection.statements[-1][1][0].obj[key]
    if not valid_cancellation:
        assert mark["status"] == "unknown" and mark["max_drawdown"] is None
        return
    assert mark["status"] == "observed" and mark["entry_quantity_fixed"] is True
    assert mark["entry_quantity_basis"] == "cancelled_remainder" and mark["entry_quantity"] == 1
    assert mark["current_net_return"] == pytest.approx(.174)
    assert mark["max_drawdown"] == pytest.approx(-.066) if entry_quote_available else mark["max_drawdown"] is None
    assert mark["mark_count"] == (2 if entry_quote_available else 1) and mark["journal_ids"] == ["actual-entry"]
    order["execution_quote"][key] = mark
    exit_quote = {**later, "quote_id": "actual-exit-quote", "bid": .2, "ask": .22,
                  "observed_at": at + timedelta(seconds=21), "quote_time": at + timedelta(seconds=21)}
    monkeypatch.setattr(paper_execution_database, "latest_option_legs", lambda *_args, **_kwargs: [exit_quote])
    monkeypatch.setattr(paper_execution_database, "is_market_open", lambda _now: True)
    repository = OptionsPaperExecutionRepository.__new__(OptionsPaperExecutionRepository)
    exited = repository._manage_open(connection, order, {"exits": {"loss_price": .25}}, [quote], at + timedelta(seconds=21))
    assert exited["status"] == "closed" and exited["exit_quantity"] == 1
    mark = next(parameters[0].obj[key] for statement, parameters in reversed(connection.statements) if "execution_quote = coalesce" in statement)
    assert mark["current_net_return"] == pytest.approx(-.626)
    assert mark["max_drawdown"] == pytest.approx(.374 / 1.174 - 1) and mark["mark_count"] == (3 if entry_quote_available else 2)
    assert mark["drawdown_peak_quotes"][0]["quote_id"] == "later-holding-quote"
    assert mark["drawdown_trough_quotes"][0]["quote_id"] == "actual-exit-quote"


def test_partial_exit_residual_is_aggregated_for_risk_and_cash_collateral(migrated_postgres_dsn: str) -> None:
    from investment_panel.database.runtime import DatabaseRuntime

    runtime = DatabaseRuntime(migrated_postgres_dsn)
    runtime.open()
    try:
        with runtime.transaction() as connection:
            instrument_id = reconcile_instrument(connection, "PARTIAL", category="test")
            connection.execute(
                """
                INSERT INTO app.paper_order
                    (instrument_id, side, quantity, limit_price, status, policy_result,
                     lane, structure, reserved_collateral, ticket_snapshot,
                     filled_quantity, exited_quantity)
                VALUES
                    (%s, 'buy', 4, 10, 'partial_exited', '{}'::jsonb,
                     'radar', 'call_debit_spread', NULL, %s, 4, 3),
                    (%s, 'sell', 4, 10, 'partial_exited', '{}'::jsonb,
                     'qqq', 'cash_secured_put', 4000, '{}'::jsonb, 4, 3)
                """,
                [instrument_id, Jsonb({"risk": {"total_risk": 400}}), instrument_id],
            )
            exposure = active_paper_exposure(
                connection,
                symbol="PARTIAL",
                instrument_id=instrument_id,
            )
    finally:
        runtime.close()

    assert float(exposure["symbol_risk"]) == 100
    assert float(exposure["total_risk"]) == 100
    assert float(exposure["symbol_csp_collateral"]) == 1000
    assert float(exposure["total_csp_collateral"]) == 1000
    assert float(exposure["total_committed"]) == 1100
    assert exposure["unvalued_commitments"] == 0
