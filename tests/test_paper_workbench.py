from datetime import UTC, datetime, timedelta
from decimal import Decimal

from investment_panel.infrastructure.postgres.paper_workbench import (
    paper_trade_payload,
)


def _row(**updates):
    row = {
        "paper_order_id": "order-1",
        "symbol": "QQQ",
        "asset_class": "etf",
        "decision_id": "decision-1",
        "contract_id": 1,
        "paper_only": True,
        "paper_status": "staged",
        "side": "buy",
        "quantity": Decimal("1"),
        "requested_quantity": Decimal("1"),
        "staged_limit_price": Decimal("1.25"),
        "structure": "long_option",
        "entry_quantity": Decimal("0"),
        "exit_quantity": Decimal("0"),
        "entry_units": Decimal("0"),
        "exit_units": Decimal("0"),
        "missing_fees": 0,
        "invalid_fills": 0,
        "fill_multipliers_verified": None,
        "order_multiplier": Decimal("100"),
        "fill_rows": [],
        "decision_evidence": [],
    }
    row.update(updates)
    return row


def test_staged_order_does_not_turn_limit_or_shadow_data_into_a_fill():
    result = paper_trade_payload(
        _row(shadow_id="shadow-1", shadow_entry_at="2026-09-11T14:00:00+00:00")
    )
    assert result["lifecycle"] == "staged"
    assert result["entry_price"] is None
    assert result["realized_pnl"] is None
    assert result["staged_limit_price"] == 1.25
    assert result["related_research"]["shadow_id"] == "shadow-1"


def test_origin_uses_only_explicit_paper_order_lineage():
    assert paper_trade_payload(_row(decision_id=None))["origin"] == "unattributed_paper_order"
    assert paper_trade_payload(_row(decision_id="decision-1"))["origin"] == "decision_linked_paper_order"
    assert paper_trade_payload(_row(ticker_decision_id="ticker-1"))["origin"] == "ticker_decision_paper_policy"


def test_partial_exit_uses_verified_journal_fills_and_fees():
    result = paper_trade_payload(
        _row(
            paper_status="partial_exited",
            entry_quantity=Decimal("2"),
            exit_quantity=Decimal("1"),
            entry_units=Decimal("100"),
            exit_units=Decimal("60"),
            entry_fees=Decimal("1"),
            exit_fees=Decimal("0.50"),
            actual_fees=Decimal("1.50"),
            missing_fees=0,
            invalid_fills=0,
            fill_multipliers_verified=True,
            fill_rows=[{"action": "paper_entry"}, {"action": "paper_exit:take_profit"}],
        )
    )
    assert result["lifecycle"] == "partial_exited"
    assert result["filled_quantity"] == 2
    assert result["remaining_quantity"] == 1
    assert result["realized_pnl"] == 999.0
    assert result["net_pnl"] is None


def test_missing_multiplier_keeps_options_pnl_unknown():
    result = paper_trade_payload(
        _row(
            paper_status="exited",
            entry_quantity=Decimal("1"),
            exit_quantity=Decimal("1"),
            entry_units=Decimal("50"),
            exit_units=Decimal("20"),
            entry_fees=Decimal("0.65"),
            exit_fees=Decimal("0.65"),
            actual_fees=Decimal("1.30"),
            fill_multipliers_verified=None,
        )
    )
    assert result["reconciliation_status"] == "partial"
    assert result["realized_pnl"] is None
    assert (
        "contract_multiplier_evidence_missing_or_conflicting"
        in result["evidence_reasons"]
    )


def test_verified_stock_mark_calculates_open_value_and_known_fee_adjusted_pnl():
    as_of = datetime(2026, 9, 11, 15, 0, tzinfo=UTC)
    result = paper_trade_payload(
        _row(
            contract_id=None,
            structure="equity",
            order_multiplier=Decimal("1"),
            paper_status="entered",
            entry_quantity=Decimal("2"),
            entry_units=Decimal("20"),
            entry_fees=Decimal("1"),
            missing_fees=0,
            invalid_fills=0,
            stock_mark_price=Decimal("12"),
            stock_mark_observed_at=as_of - timedelta(hours=1),
            stock_mark_available_at=as_of - timedelta(minutes=30),
            stock_mark_source="confirmed-test",
            mark_as_of=as_of,
        )
    )
    assert result["mark_status"] == "verified"
    assert result["mark_basis"] == "confirmed_quote_price"
    assert result["mark_value"] == 24.0
    assert result["unrealized_pnl"] == 3.0
    assert result["net_pnl"] == 3.0


def test_verified_option_package_marks_every_stored_leg():
    as_of = datetime(2026, 9, 11, 15, 0, tzinfo=UTC)
    result = paper_trade_payload(
        _row(
            contract_id=None,
            structure="debit_spread",
            expression_kind="DEBIT_SPREAD",
            paper_status="entered",
            entry_quantity=Decimal("1"),
            entry_units=Decimal("2"),
            entry_fees=Decimal("1"),
            order_multiplier=Decimal("100"),
            missing_fees=0,
            invalid_fills=0,
            fill_multipliers_verified=True,
            order_legs=[
                {"contract_id": 101, "side": "long", "multiplier": 100},
                {"contract_id": 102, "side": "short", "multiplier": 100},
            ],
            option_marks=[
                {
                    "contract_id": 101,
                    "bid": Decimal("1.5"),
                    "ask": Decimal("1.7"),
                    "observed_at": as_of - timedelta(hours=1),
                    "available_at": as_of - timedelta(minutes=30),
                    "source": "confirmed-options",
                },
                {
                    "contract_id": 102,
                    "bid": Decimal("0.4"),
                    "ask": Decimal("0.6"),
                    "observed_at": as_of - timedelta(hours=1),
                    "available_at": as_of - timedelta(minutes=30),
                    "source": "confirmed-options",
                },
            ],
            mark_as_of=as_of,
        )
    )
    assert result["mark_status"] == "verified"
    assert result["mark_basis"] == "conservative_liquidation_legs"
    assert result["mark_price"] == 0.9
    assert result["mark_value"] == 90.0
    assert result["unrealized_pnl"] == -111.0
    assert len(result["mark"]["evidence"]["legs"]) == 2


def test_stale_mark_is_visible_but_cannot_value_open_exposure():
    as_of = datetime(2026, 9, 11, 15, 0, tzinfo=UTC)
    result = paper_trade_payload(
        _row(
            contract_id=None,
            structure="equity",
            order_multiplier=None,
            paper_status="entered",
            entry_quantity=Decimal("1"),
            entry_units=Decimal("10"),
            missing_fees=0,
            invalid_fills=0,
            stock_mark_price=Decimal("12"),
            stock_mark_observed_at=as_of - timedelta(days=4),
            stock_mark_available_at=as_of - timedelta(days=4),
            stock_mark_source="confirmed-test",
            mark_as_of=as_of,
        )
    )
    assert result["mark_status"] == "stale"
    assert result["mark_price"] == 12.0
    assert result["mark_value"] is None
    assert result["unrealized_pnl"] is None


def test_chart_bound_keeps_extremes_and_drawdown_trough():
    from investment_panel.infrastructure.postgres.paper_workbench import paper_chart_indices
    series = [{"cumulative_net_pnl": 0} for _ in range(10000)]
    drawdowns = [{"drawdown": 0} for _ in series]
    series[4321]["cumulative_net_pnl"] = 100
    series[4322]["cumulative_net_pnl"] = -100
    drawdowns[4323]["drawdown"] = -200
    indices = paper_chart_indices(series, drawdowns)
    assert len(indices) <= 2000
    assert {0, 9999, 4321, 4322, 4323} <= set(indices)


def test_visual_performance_markers_and_attribution_only_use_verified_exits():
    from investment_panel.infrastructure.postgres.paper_workbench import paper_performance_visuals

    row = paper_trade_payload(
        _row(
            paper_status="closed",
            entry_quantity=Decimal("1"),
            exit_quantity=Decimal("1"),
            entry_units=Decimal("10"),
            exit_units=Decimal("15"),
            entry_fees=Decimal("0.10"),
            exit_fees=Decimal("0.10"),
            actual_fees=Decimal("0.20"),
            missing_fees=0,
            invalid_fills=0,
            fill_multipliers_verified=True,
            strategy_name="Recovery",
            strategy_key="recovery",
            fill_rows=[
                {"id": "entry", "action": "paper_entry", "quantity": 1, "price": 10, "fees": 0.10, "created_at": "2026-09-01T14:00:00+00:00"},
                {"id": "exit", "action": "paper_exit", "quantity": 1, "price": 15, "fees": 0.10, "created_at": "2026-09-03T14:00:00+00:00"},
            ],
        )
    )
    visuals = paper_performance_visuals([row])
    markers = visuals["event_markers"]
    attribution = visuals["attribution"]

    assert [marker["kind"] for marker in markers] == ["entry", "exit"]
    assert markers[-1]["pnl"] == row["realized_pnl"]
    assert markers[-1]["cumulative_net_pnl"] == row["realized_pnl"]
    assert attribution["strategy"][0]["label"] == "Recovery"
    assert attribution["strategy"][0]["trades"] == 1
    assert attribution["strategy"][0]["win_rate"] == 1.0
