from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import deps
from app.routers.portfolio import router
from app.routers.panel import router as panel_router
from app.routers.theses import router as theses_router
from investment_panel.database.authority import close_cached_runtimes
from investment_panel.database.ingestion import IngestionRepository
from investment_panel.database.migrations import upgrade_database
from investment_panel.database.runtime import DatabaseRuntime


def _confirm_price_facts(connection: psycopg.Connection, run_id: object) -> None:
    connection.execute("UPDATE ingest.run SET finished_at = now() WHERE id = %s", [run_id])
    connection.execute(
        """
        INSERT INTO raw.quote_confirmation (fact_id, fact_available_at, ingest_run_id)
        SELECT id, available_at, %s FROM raw.quote WHERE ingest_run_id = %s
        ON CONFLICT DO NOTHING
        """,
        [run_id, run_id],
    )
    connection.execute(
        """
        INSERT INTO raw.price_bar_confirmation (fact_id, fact_available_at, ingest_run_id)
        SELECT id, available_at, %s FROM raw.price_bar WHERE ingest_run_id = %s
        ON CONFLICT DO NOTHING
        """,
        [run_id, run_id],
    )


@pytest.fixture
def postgres_dsn(postgresql) -> str:
    info = postgresql.info
    credentials = info.user if not info.password else f"{info.user}:{info.password}"
    return f"postgresql://{credentials}@{info.host}:{info.port}/{info.dbname}"


@pytest.fixture
def client(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    upgrade_database(postgres_dsn)
    monkeypatch.setattr(deps, "load_config", lambda: {"database": {"url": postgres_dsn}})
    monkeypatch.setattr(
        deps,
        "populate_watchlist_symbol_data",
        lambda _config, symbol, asset_class: {
            "status": "ok", "symbol": symbol, "asset_class": asset_class, "quote_rows": 1,
        },
    )
    application = FastAPI()
    application.include_router(router)
    application.include_router(panel_router)
    application.include_router(theses_router)
    with TestClient(application) as test_client:
        yield test_client
    close_cached_runtimes()


def test_portfolio_route_round_trip_and_latest_quote_metrics(client: TestClient, postgres_dsn: str) -> None:
    response = client.post(
        "/api/portfolio/positions",
        json={
            "symbol": "nvda",
            "quantity": 2,
            "avg_cost": 100,
            "purchase_date": "2026-07-01",
            "notes": "core position",
        },
    )
    assert response.status_code == 200
    saved_row = response.json()["portfolio"]["rows"][0]
    assert saved_row | {"updated_at": "ignored"} == {
        "symbol": "NVDA",
        "name": "NVDA",
        "asset_class": "equity",
        "category": "watchlist",
        "quantity": 2.0,
        "average_cost": "100.000000",
        "purchase_date": "2026-07-01",
            "notes": "core position",
            "avg_cost": 100.0,
            "valuation_price": 100.0,
            "valuation_status": "cost_basis_fallback",
            "market_value": 200.0,
            "unrealized_pnl": 0.0,
            "unrealized_pnl_pct": 0.0,
            "portfolio_weight": 100.0,
            "updated_at": "ignored",
    }

    with closing(psycopg.connect(postgres_dsn)) as connection:
        instrument_id = connection.execute("SELECT id FROM catalog.instrument WHERE symbol = 'NVDA'").fetchone()[0]
        source_id = connection.execute(
            "INSERT INTO ingest.source (id, name, family, kind) VALUES ('test', 'Test', 'test', 'quote') RETURNING id"
        ).fetchone()[0]
        run_id = connection.execute(
            "INSERT INTO ingest.run (source_id, capability, started_at, status) "
            "VALUES (%s, 'quotes', now(), 'succeeded') RETURNING id",
            [source_id],
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO raw.quote "
            "(instrument_id, source_id, ingest_run_id, observed_at, price, change_abs, change_pct) "
            "VALUES (%s, %s, %s, now(), 125, 5, 4.1667)",
            [instrument_id, source_id, run_id],
        )
        _confirm_price_facts(connection, run_id)
        connection.commit()

    row = client.get("/api/portfolio").json()["rows"][0]
    assert row["market_value"] == 250.0
    assert row["unrealized_pnl"] == 50.0
    assert row["unrealized_pnl_pct"] == 25.0
    assert row["portfolio_weight"] == 100.0
    assert row["quote_source"] == "test"

    with closing(psycopg.connect(postgres_dsn)) as connection:
        thesis = connection.execute(
            "SELECT thesis FROM app.thesis t JOIN catalog.instrument i ON i.id = t.instrument_id "
            "WHERE i.symbol = 'NVDA' AND t.status = 'current'"
        ).fetchone()[0]
    assert thesis["position_status"] == "owned"

    deleted = client.delete("/api/portfolio/positions/NVDA")
    assert deleted.status_code == 410
    assert "record a sell" in deleted.json()["detail"]
    assert client.get("/api/portfolio/transactions").json()["rows"][0]["transaction_type"] == "opening_balance"
    assert client.get("/api/portfolio/performance").json()["rows"][0]["date"] == "2026-07-01"


def test_portfolio_transaction_buy_appends_activity_and_updates_position(client: TestClient) -> None:
    response = client.post(
        "/api/portfolio/transactions",
        json={
            "symbol": "nvda",
            "transaction_type": "buy",
            "quantity": 2,
            "price": 100,
            "fees": 1,
            "executed_at": "2026-07-14T15:30:00Z",
            "notes": "starter position",
            "idempotency_key": "test-buy-nvda-1",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["transaction"] | {"id": "ignored", "created_at": "ignored"} == {
        "id": "ignored",
        "created_at": "ignored",
        "symbol": "NVDA",
        "transaction_type": "buy",
        "quantity": 2.0,
        "price": 100.0,
        "fees": 1.0,
        "amount": 200.0,
        "realized_pnl": 0.0,
        "currency": "USD",
            "executed_at": "2026-07-14T15:30:00+00:00",
        "account": "manual",
        "notes": "starter position",
        "idempotency_key": "test-buy-nvda-1",
        "reverses_transaction_id": None,
        "is_reversal": False,
        "is_reversed": False,
    }
    assert payload["portfolio"]["rows"][0]["symbol"] == "NVDA"
    assert payload["portfolio"]["rows"][0]["quantity"] == 2.0
    assert payload["portfolio"]["rows"][0]["avg_cost"] == 100.5

    activity = client.get("/api/portfolio/transactions").json()
    assert activity["count"] == 1
    assert activity["rows"][0]["idempotency_key"] == "test-buy-nvda-1"


def test_portfolio_transaction_sell_previews_and_realizes_average_cost_pnl(client: TestClient) -> None:
    buy = {
        "symbol": "MSFT",
        "transaction_type": "buy",
        "quantity": 10,
        "price": 100,
        "executed_at": "2026-07-01T15:30:00Z",
        "idempotency_key": "test-buy-msft-1",
    }
    assert client.post("/api/portfolio/transactions", json=buy).status_code == 200
    sell = {
        "symbol": "MSFT",
        "transaction_type": "sell",
        "quantity": 4,
        "price": 125,
        "fees": 2,
        "executed_at": "2026-07-02T15:30:00Z",
        "idempotency_key": "test-sell-msft-1",
    }

    preview = client.post("/api/portfolio/transactions/preview", json=sell)
    assert preview.status_code == 200
    assert preview.json()["position_version"]
    assert preview.json() == {
        "symbol": "MSFT",
        "transaction_type": "sell",
        "old_quantity": 10.0,
        "new_quantity": 6.0,
        "old_average_cost": 100.0,
        "new_average_cost": 100.0,
        "amount": 500.0,
        "fees": 2.0,
        "realized_pnl": 98.0,
        "position_version": preview.json()["position_version"],
    }

    first = client.post("/api/portfolio/transactions", json=sell)
    duplicate = client.post("/api/portfolio/transactions", json=sell)
    assert first.status_code == duplicate.status_code == 200
    assert first.json()["transaction"]["id"] == duplicate.json()["transaction"]["id"]
    assert first.json()["transaction"]["realized_pnl"] == 98.0
    assert duplicate.json()["portfolio"]["rows"][0]["quantity"] == 6.0
    assert client.get("/api/portfolio/transactions").json()["count"] == 2

    oversized = client.post(
        "/api/portfolio/transactions/preview",
        json={**sell, "quantity": 7, "idempotency_key": "test-sell-msft-oversized"},
    )
    assert oversized.status_code == 400
    assert "only 6 held" in oversized.json()["detail"]


def test_portfolio_summary_and_performance_reconcile_to_one_price_set(client: TestClient, postgres_dsn: str) -> None:
    assert client.post(
        "/api/portfolio/transactions",
        json={
            "symbol": "MSFT",
            "transaction_type": "buy",
            "quantity": 10,
            "price": 100,
            "executed_at": "2026-07-14T15:30:00Z",
            "idempotency_key": "test-performance-msft-buy",
        },
    ).status_code == 200
    with closing(psycopg.connect(postgres_dsn)) as connection:
        instrument_id = connection.execute("SELECT id FROM catalog.instrument WHERE symbol = 'MSFT'").fetchone()[0]
        connection.execute(
            "INSERT INTO ingest.source (id, name, family, kind) VALUES ('performance-test', 'Performance Test', 'test', 'price')"
        )
        run_id = connection.execute(
            "INSERT INTO ingest.run (source_id, capability, started_at, status) "
            "VALUES ('performance-test', 'prices', now(), 'succeeded') RETURNING id"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO raw.price_bar
                (instrument_id, source_id, ingest_run_id, interval, trading_date, observed_at, close)
            VALUES
                (%s, 'performance-test', %s, '1d', '2026-07-14', '2026-07-14T20:00:00Z', 100),
                (%s, 'performance-test', %s, '1d', '2026-07-15', '2026-07-15T20:00:00Z', 110)
            """,
            [instrument_id, run_id, instrument_id, run_id],
        )
        connection.execute(
            """
            INSERT INTO raw.quote
                (instrument_id, source_id, ingest_run_id, observed_at, price, change_abs, change_pct)
            VALUES (%s, 'performance-test', %s, '2026-07-15T20:00:00Z', 110, 10, 10)
            """,
            [instrument_id, run_id],
        )
        _confirm_price_facts(connection, run_id)
        connection.commit()

    summary = client.get("/api/portfolio/summary")
    assert summary.status_code == 200
    assert summary.json() | {"as_of": "ignored", "oldest_quote_at": "ignored"} == {
        "as_of": "ignored",
        "oldest_quote_at": "ignored",
        "portfolio_value": 1100.0,
        "cost_basis": 1000.0,
        "net_contributions": 1000.0,
        "invested_capital": 1000.0,
        "total_pnl": 100.0,
        "total_pnl_pct": 10.0,
        "day_pnl": 100.0,
        "day_pnl_pct": 10.0,
        "day_pnl_as_of": "2026-07-15",
        "day_pnl_status": "ready",
        "realized_pnl": 0.0,
        "income": 0.0,
        "fees": 0.0,
        "holdings_count": 1,
        "cost_basis_fallback_count": 0,
        "valuation_status": "market_quotes",
        "currency": "USD",
        "performance_method": "daily-close external-flow adjusted",
    }
    performance = client.get("/api/portfolio/performance").json()
    assert performance["count"] == 2
    assert performance["rows"][-1] | {"date": "ignored"} == {
        "date": "ignored",
        "portfolio_value": 1100.0,
        "net_contributions": 1000.0,
        "invested_capital": 1000.0,
        "total_pnl": 100.0,
        "total_return_pct": 10.0,
        "time_weighted_return_pct": 10.0,
        "drawdown_pct": 0.0,
        "benchmark_return_pct": None,
    }


def test_first_trade_uses_same_prior_close_for_holding_summary_and_performance(
    client: TestClient,
    postgres_dsn: str,
) -> None:
    assert client.post(
        "/api/portfolio/transactions",
        json={
            "symbol": "MSFT",
            "transaction_type": "buy",
            "quantity": 10,
            "price": 100,
            "executed_at": "2026-07-15T15:30:00Z",
            "idempotency_key": "prior-close-msft-buy",
        },
    ).status_code == 200
    with closing(psycopg.connect(postgres_dsn)) as connection:
        instrument_id = connection.execute(
            "SELECT id FROM catalog.instrument WHERE symbol = 'MSFT'"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO ingest.source (id, name, family, kind) "
            "VALUES ('prior-close-test', 'Prior Close Test', 'test', 'price')"
        )
        run_id = connection.execute(
            "INSERT INTO ingest.run (source_id, capability, started_at, status) "
            "VALUES ('prior-close-test', 'prices', now(), 'succeeded') RETURNING id"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO raw.price_bar "
            "(instrument_id, source_id, ingest_run_id, interval, trading_date, observed_at, close) "
            "VALUES (%s, 'prior-close-test', %s, '1d', '2026-07-14', '2026-07-14T20:00:00Z', 90)",
            [instrument_id, run_id],
        )
        _confirm_price_facts(connection, run_id)
        connection.commit()

    holding = client.get("/api/portfolio").json()["rows"][0]
    summary = client.get("/api/portfolio/summary").json()
    performance = client.get("/api/portfolio/performance").json()["rows"][-1]

    assert holding["market_value"] == 900
    assert summary["portfolio_value"] == performance["portfolio_value"] == 900
    assert summary["total_pnl"] == performance["total_pnl"] == -100


def test_portfolio_transactions_reject_backdating_and_conflicting_idempotency(client: TestClient) -> None:
    original = {
        "symbol": "NVDA",
        "transaction_type": "buy",
        "quantity": 2,
        "price": 100,
        "executed_at": "2026-07-02T15:30:00Z",
        "idempotency_key": "integrity-nvda-buy",
    }
    assert client.post("/api/portfolio/transactions", json=original).status_code == 200

    conflict = client.post(
        "/api/portfolio/transactions",
        json={**original, "quantity": 3},
    )
    assert conflict.status_code == 400
    assert "different transaction" in conflict.json()["detail"]

    backdated = client.post(
        "/api/portfolio/transactions/preview",
        json={**original, "executed_at": "2026-07-01T15:30:00Z", "idempotency_key": "backdated-nvda-buy"},
    )
    assert backdated.status_code == 400
    assert "backdated transactions are not supported" in backdated.json()["detail"]


def test_portfolio_transactions_reject_split_without_position(client: TestClient) -> None:
    response = client.post(
        "/api/portfolio/transactions",
        json={
            "symbol": "NVDA",
            "transaction_type": "split",
            "quantity": 2,
            "executed_at": "2026-07-14T15:30:00Z",
            "idempotency_key": "empty-position-split",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "split requires an existing position"


def test_trade_confirmation_rejects_position_changed_after_preview(client: TestClient) -> None:
    trade = {
        "symbol": "NVDA",
        "transaction_type": "buy",
        "quantity": 1,
        "price": 100,
        "executed_at": "2026-07-01T15:30:00Z",
        "idempotency_key": "preview-version-original",
    }
    preview = client.post("/api/portfolio/transactions/preview", json=trade)
    assert preview.status_code == 200
    assert preview.json()["position_version"] == "empty"
    assert client.post(
        "/api/portfolio/transactions",
        json={**trade, "idempotency_key": "preview-version-intervening"},
    ).status_code == 200

    stale_confirmation = client.post(
        "/api/portfolio/transactions",
        json={**trade, "expected_position_version": preview.json()["position_version"]},
    )

    assert stale_confirmation.status_code == 400
    assert "changed since preview" in stale_confirmation.json()["detail"]


def test_portfolio_idempotency_uses_database_numeric_precision(client: TestClient) -> None:
    trade = {
        "symbol": "NVDA",
        "transaction_type": "buy",
        "quantity": 1.123456789,
        "price": 100.1234567,
        "fees": 0.1234567,
        "executed_at": "2026-07-01T15:30:00Z",
        "idempotency_key": "precision-nvda-buy",
    }
    first = client.post("/api/portfolio/transactions", json=trade)
    retry = client.post("/api/portfolio/transactions", json=trade)
    assert first.status_code == retry.status_code == 200
    assert first.json()["transaction"]["id"] == retry.json()["transaction"]["id"]
    assert first.json()["transaction"]["quantity"] == 1.12345679
    assert first.json()["transaction"]["price"] == 100.123457


def test_position_transaction_notional_is_always_derived(client: TestClient) -> None:
    response = client.post(
        "/api/portfolio/transactions",
        json={
            "symbol": "NVDA",
            "transaction_type": "buy",
            "quantity": 10,
            "price": 100,
            "amount": 1,
            "executed_at": "2026-07-01T15:30:00Z",
            "idempotency_key": "derived-notional-nvda-buy",
        },
    )
    assert response.status_code == 200
    assert response.json()["transaction"]["amount"] == 1000.0
    assert client.get("/api/portfolio/summary").json()["net_contributions"] == 1000.0


def test_portfolio_transactions_reject_future_account_and_duplicate_opening(client: TestClient) -> None:
    now = datetime.now(UTC)
    opening = {
        "symbol": "NVDA",
        "transaction_type": "opening_balance",
        "quantity": 2,
        "price": 100,
        "executed_at": (now - timedelta(days=1)).isoformat(),
        "idempotency_key": "constraints-nvda-opening",
    }
    assert client.post("/api/portfolio/transactions", json=opening).status_code == 200

    duplicate_opening = client.post(
        "/api/portfolio/transactions",
        json={**opening, "executed_at": now.isoformat(), "idempotency_key": "constraints-nvda-opening-2"},
    )
    assert duplicate_opening.status_code == 400
    assert "requires an empty position" in duplicate_opening.json()["detail"]

    wrong_account = client.post(
        "/api/portfolio/transactions",
        json={
            **opening,
            "transaction_type": "buy",
            "account": "retirement",
            "executed_at": now.isoformat(),
            "idempotency_key": "constraints-nvda-account",
        },
    )
    assert wrong_account.status_code == 400
    assert "account must be manual" in wrong_account.json()["detail"]

    future = client.post(
        "/api/portfolio/transactions",
        json={
            **opening,
            "transaction_type": "buy",
            "executed_at": (now + timedelta(days=1)).isoformat(),
            "idempotency_key": "constraints-nvda-future",
        },
    )
    assert future.status_code == 400
    assert "five minutes in the future" in future.json()["detail"]


def test_session_pnl_includes_full_sale_and_fees(client: TestClient) -> None:
    buy_at = datetime(2026, 7, 16, 15, 30, tzinfo=UTC)
    sell_at = datetime(2026, 7, 17, 15, 30, tzinfo=UTC)
    assert client.post(
        "/api/portfolio/transactions",
        json={
            "symbol": "MSFT",
            "transaction_type": "buy",
            "quantity": 10,
            "price": 100,
            "executed_at": buy_at.isoformat(),
            "idempotency_key": "session-msft-buy",
        },
    ).status_code == 200
    assert client.post(
        "/api/portfolio/transactions",
        json={
            "symbol": "MSFT",
            "transaction_type": "sell",
            "quantity": 10,
            "price": 110,
            "fees": 1,
            "executed_at": sell_at.isoformat(),
            "idempotency_key": "session-msft-sell",
        },
    ).status_code == 200

    summary = client.get("/api/portfolio/summary").json()
    assert summary["portfolio_value"] == 0.0
    assert summary["day_pnl"] == 99.0
    assert summary["day_pnl_pct"] == 9.9
    assert summary["day_pnl_as_of"] == sell_at.astimezone(ZoneInfo("America/New_York")).date().isoformat()


def test_sparse_history_does_not_claim_a_single_session_pnl(client: TestClient, postgres_dsn: str) -> None:
    assert client.post(
        "/api/portfolio/transactions",
        json={
            "symbol": "MSFT",
            "transaction_type": "buy",
            "quantity": 1,
            "price": 100,
            "executed_at": "2026-07-01T15:30:00Z",
            "idempotency_key": "sparse-msft-buy",
        },
    ).status_code == 200
    with closing(psycopg.connect(postgres_dsn)) as connection:
        instrument_id = connection.execute("SELECT id FROM catalog.instrument WHERE symbol = 'MSFT'").fetchone()[0]
        connection.execute(
            "INSERT INTO ingest.source (id, name, family, kind) VALUES ('sparse-test', 'Sparse Test', 'test', 'quote')"
        )
        run_id = connection.execute(
            "INSERT INTO ingest.run (source_id, capability, started_at, status) "
            "VALUES ('sparse-test', 'quotes', now(), 'succeeded') RETURNING id"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO raw.quote (instrument_id, source_id, ingest_run_id, observed_at, price) "
            "VALUES (%s, 'sparse-test', %s, '2026-07-15T20:00:00Z', 120)",
            [instrument_id, run_id],
        )
        _confirm_price_facts(connection, run_id)
        connection.commit()

    summary = client.get("/api/portfolio/summary").json()
    assert summary["total_pnl"] == 20.0
    assert summary["day_pnl"] is None
    assert summary["day_pnl_pct"] is None
    assert summary["day_pnl_status"] == "insufficient_adjacent_history"


def test_transaction_reversal_replays_position_accounting_and_thesis(
    client: TestClient,
    postgres_dsn: str,
) -> None:
    buy = client.post(
        "/api/portfolio/transactions",
        json={
            "symbol": "MSFT",
            "transaction_type": "buy",
            "quantity": 10,
            "price": 100,
            "executed_at": "2026-07-01T15:30:00Z",
            "idempotency_key": "reversal-msft-buy",
        },
    ).json()["transaction"]
    sell = client.post(
        "/api/portfolio/transactions",
        json={
            "symbol": "MSFT",
            "transaction_type": "sell",
            "quantity": 4,
            "price": 125,
            "fees": 2,
            "executed_at": "2026-07-02T15:30:00Z",
            "idempotency_key": "reversal-msft-sell",
        },
    ).json()["transaction"]
    assert sell["realized_pnl"] == 98.0

    reversed_sell = client.post(
        f"/api/portfolio/transactions/{sell['id']}/reverse",
        json={"idempotency_key": "reverse-msft-sell", "notes": "execution correction"},
    )
    assert reversed_sell.status_code == 200
    portfolio = reversed_sell.json()["portfolio"]["rows"]
    assert portfolio[0]["quantity"] == 10.0
    assert portfolio[0]["avg_cost"] == 100.0
    summary = client.get("/api/portfolio/summary").json()
    assert summary["realized_pnl"] == 0.0
    assert summary["total_pnl"] == 0.0
    activity = client.get("/api/portfolio/transactions").json()["rows"]
    assert len(activity) == 3
    assert next(row for row in activity if row["id"] == sell["id"])["is_reversed"] is True
    assert next(row for row in activity if row["reverses_transaction_id"] == sell["id"])["is_reversal"] is True
    duplicate = client.post(
        f"/api/portfolio/transactions/{sell['id']}/reverse",
        json={"idempotency_key": "reverse-msft-sell-again"},
    )
    assert duplicate.status_code == 400
    assert "already reversed" in duplicate.json()["detail"]

    reversed_buy = client.post(
        f"/api/portfolio/transactions/{buy['id']}/reverse",
        json={"idempotency_key": "reverse-msft-buy"},
    )
    assert reversed_buy.status_code == 200
    assert reversed_buy.json()["portfolio"] == {"rows": [], "count": 0}
    with closing(psycopg.connect(postgres_dsn)) as connection:
        current_theses = connection.execute(
            "SELECT count(*) FROM app.thesis thesis JOIN catalog.instrument instrument "
            "ON instrument.id = thesis.instrument_id WHERE instrument.symbol = 'MSFT' AND thesis.status = 'current'"
        ).fetchone()[0]
    assert current_theses == 0
    immediate_trade = client.post(
        "/api/portfolio/transactions",
        json={
            "symbol": "MSFT",
            "transaction_type": "buy",
            "quantity": 1,
            "price": 101,
            "executed_at": datetime.now(UTC).replace(second=0, microsecond=0).isoformat(),
            "idempotency_key": "post-reversal-msft-buy",
        },
    )
    assert immediate_trade.status_code == 200


def test_cash_transaction_reversal_does_not_mutate_unowned_thesis(
    client: TestClient,
    postgres_dsn: str,
) -> None:
    assert client.put(
        "/api/theses/LLY",
        json={"thesis": "Watch the obesity franchise without owning it."},
    ).status_code == 200
    dividend = client.post(
        "/api/portfolio/transactions",
        json={
            "symbol": "LLY", "transaction_type": "dividend", "amount": 25,
            "executed_at": "2026-07-14T15:30:00Z", "idempotency_key": "cash-reversal-dividend",
        },
    )
    assert dividend.status_code == 200
    with closing(psycopg.connect(postgres_dsn)) as connection:
        before = connection.execute(
            "SELECT thesis FROM app.thesis thesis JOIN catalog.instrument instrument "
            "ON instrument.id = thesis.instrument_id "
            "WHERE instrument.symbol = 'LLY' AND thesis.status = 'current'"
        ).fetchone()[0]
    assert client.post(
        f"/api/portfolio/transactions/{dividend.json()['transaction']['id']}/reverse",
        json={"idempotency_key": "cash-reversal-dividend-reverse"},
    ).status_code == 200
    with closing(psycopg.connect(postgres_dsn)) as connection:
        after = connection.execute(
            "SELECT thesis FROM app.thesis thesis JOIN catalog.instrument instrument "
            "ON instrument.id = thesis.instrument_id "
            "WHERE instrument.symbol = 'LLY' AND thesis.status = 'current'"
        ).fetchone()[0]
    assert after == before


def test_replay_resets_notes_after_full_exit_and_blank_reentry(client: TestClient) -> None:
    trades = [
        {"transaction_type": "buy", "quantity": 2, "price": 100, "notes": "old episode", "executed_at": "2026-07-10T15:30:00Z", "idempotency_key": "notes-buy-old"},
        {"transaction_type": "sell", "quantity": 2, "price": 110, "executed_at": "2026-07-11T15:30:00Z", "idempotency_key": "notes-exit"},
        {"transaction_type": "buy", "quantity": 1, "price": 120, "executed_at": "2026-07-12T15:30:00Z", "idempotency_key": "notes-reentry"},
        {"transaction_type": "buy", "quantity": 1, "price": 130, "notes": "reverse me", "executed_at": "2026-07-13T15:30:00Z", "idempotency_key": "notes-later-buy"},
    ]
    responses = [client.post("/api/portfolio/transactions", json={"symbol": "MSFT", **trade}) for trade in trades]
    assert all(response.status_code == 200 for response in responses)
    last_id = responses[-1].json()["transaction"]["id"]
    assert client.post(
        f"/api/portfolio/transactions/{last_id}/reverse",
        json={"idempotency_key": "notes-later-buy-reversal"},
    ).status_code == 200
    assert client.get("/api/portfolio").json()["rows"][0].get("notes", "") == ""


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"symbol": "BAD/SYMBOL"}, "valid ticker"),
        ({"quantity": "NaN"}, "finite"),
        ({"currency": "EUR"}, "currency must be USD"),
    ],
)
def test_portfolio_transactions_reject_unsafe_inputs(
    client: TestClient,
    overrides: dict[str, object],
    message: str,
) -> None:
    response = client.post(
        "/api/portfolio/transactions",
        json={
            "symbol": "NVDA",
            "transaction_type": "buy",
            "quantity": 1,
            "price": 100,
            "executed_at": "2026-07-01T15:30:00Z",
            "idempotency_key": f"unsafe-{message}",
            **overrides,
        },
    )
    assert response.status_code in {400, 422}
    assert message in str(response.json())


def test_unpriced_holdings_use_labeled_cost_basis_fallback(client: TestClient) -> None:
    assert client.post(
        "/api/portfolio/transactions",
        json={
            "symbol": "NVDA",
            "transaction_type": "buy",
            "quantity": 2,
            "price": 100,
            "executed_at": "2026-07-01T15:30:00Z",
            "idempotency_key": "fallback-nvda-buy",
        },
    ).status_code == 200

    position = client.get("/api/portfolio").json()["rows"][0]
    assert "price" not in position or position["price"] is None
    assert position["valuation_price"] == 100.0
    assert position["valuation_status"] == "cost_basis_fallback"
    assert position["market_value"] == 200.0
    summary = client.get("/api/portfolio/summary").json()
    assert summary["portfolio_value"] == 200.0
    assert summary["total_pnl"] == 0.0
    assert summary["cost_basis_fallback_count"] == 1
    assert summary["valuation_status"] == "cost_basis_fallback"


def test_portfolio_rejects_arbitrarily_stale_quote_for_current_valuation(
    client: TestClient,
    postgres_dsn: str,
) -> None:
    assert client.post(
        "/api/portfolio/transactions",
        json={
            "symbol": "NVDA", "transaction_type": "buy", "quantity": 2, "price": 100,
            "executed_at": "2026-07-14T15:30:00Z", "idempotency_key": "stale-quote-buy",
        },
    ).status_code == 200
    with closing(psycopg.connect(postgres_dsn)) as connection:
        instrument_id = connection.execute("SELECT id FROM catalog.instrument WHERE symbol = 'NVDA'").fetchone()[0]
        connection.execute(
            "INSERT INTO ingest.source (id, name, family, kind) "
            "VALUES ('stale-valuation-test', 'Stale Valuation Test', 'test', 'quote')"
        )
        run_id = connection.execute(
            "INSERT INTO ingest.run (source_id, capability, started_at, status) "
            "VALUES ('stale-valuation-test', 'quotes', now(), 'succeeded') RETURNING id"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO raw.quote (instrument_id, source_id, ingest_run_id, observed_at, price) "
            "VALUES (%s, 'stale-valuation-test', %s, '2026-07-01T20:00:00Z', 200)",
            [instrument_id, run_id],
        )
        _confirm_price_facts(connection, run_id)
        connection.commit()

    holding = client.get("/api/portfolio").json()["rows"][0]
    assert holding["valuation_status"] == "cost_basis_fallback"
    assert holding["market_value"] == 200


def test_summary_holdings_and_performance_share_latest_daily_bar_price(
    client: TestClient,
    postgres_dsn: str,
) -> None:
    assert client.post(
        "/api/portfolio/transactions",
        json={
            "symbol": "NVDA",
            "transaction_type": "buy",
            "quantity": 2,
            "price": 100,
            "fees": 1,
            "executed_at": "2026-07-14T15:30:00Z",
            "idempotency_key": "bar-price-nvda-buy",
        },
    ).status_code == 200
    with closing(psycopg.connect(postgres_dsn)) as connection:
        instrument_id = connection.execute("SELECT id FROM catalog.instrument WHERE symbol = 'NVDA'").fetchone()[0]
        connection.execute(
            "INSERT INTO ingest.source (id, name, family, kind) VALUES ('bar-price-test', 'Bar Price Test', 'test', 'price')"
        )
        run_id = connection.execute(
            "INSERT INTO ingest.run (source_id, capability, started_at, status) "
            "VALUES ('bar-price-test', 'prices', now(), 'succeeded') RETURNING id"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO raw.price_bar
                (instrument_id, source_id, ingest_run_id, interval, trading_date, observed_at, close)
            VALUES (%s, 'bar-price-test', %s, '1d', '2026-07-15', '2026-07-15T21:00:00Z', 110)
            """,
            [instrument_id, run_id],
        )
        connection.execute(
            "INSERT INTO raw.quote (instrument_id, source_id, ingest_run_id, observed_at, price) "
            "VALUES (%s, 'bar-price-test', %s, '2026-07-15T14:00:00Z', 105)",
            [instrument_id, run_id],
        )
        _confirm_price_facts(connection, run_id)
        connection.commit()

    holding = client.get("/api/portfolio").json()["rows"][0]
    summary = client.get("/api/portfolio/summary").json()
    performance = client.get("/api/portfolio/performance").json()["rows"][-1]
    assert holding["valuation_status"] == "daily_close"
    assert holding["market_value"] == 220.0
    assert summary["portfolio_value"] == performance["portfolio_value"] == 220.0
    assert summary["total_pnl"] == performance["total_pnl"] == 19.0


def test_dividend_and_split_fees_reduce_portfolio_pnl(client: TestClient) -> None:
    base = {
        "symbol": "NVDA",
        "quantity": 10,
        "price": 100,
        "executed_at": "2026-07-01T15:30:00Z",
    }
    assert client.post(
        "/api/portfolio/transactions",
        json={**base, "transaction_type": "buy", "idempotency_key": "fees-nvda-buy"},
    ).status_code == 200
    assert client.post(
        "/api/portfolio/transactions",
        json={
            "symbol": "NVDA",
            "transaction_type": "dividend",
            "amount": 100,
            "fees": 10,
            "executed_at": "2026-07-02T15:30:00Z",
            "idempotency_key": "fees-nvda-dividend",
        },
    ).status_code == 200
    assert client.post(
        "/api/portfolio/transactions",
        json={
            "symbol": "NVDA",
            "transaction_type": "split",
            "quantity": 2,
            "fees": 5,
            "executed_at": "2026-07-03T15:30:00Z",
            "idempotency_key": "fees-nvda-split",
        },
    ).status_code == 200

    summary = client.get("/api/portfolio/summary").json()
    assert summary["income"] == 100.0
    assert summary["fees"] == 15.0
    assert summary["total_pnl"] == 85.0
    assert client.get("/api/portfolio/performance").json()["rows"][-1]["total_pnl"] == 85.0


def test_split_ignores_stale_pre_split_quote_until_price_scale_catches_up(
    client: TestClient,
    postgres_dsn: str,
) -> None:
    assert client.post(
        "/api/portfolio/transactions",
        json={
            "symbol": "NVDA",
            "transaction_type": "buy",
            "quantity": 10,
            "price": 100,
            "executed_at": "2026-07-01T15:30:00Z",
            "idempotency_key": "split-scale-buy",
        },
    ).status_code == 200
    with closing(psycopg.connect(postgres_dsn)) as connection:
        instrument_id = connection.execute(
            "SELECT id FROM catalog.instrument WHERE symbol = 'NVDA'"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO ingest.source (id, name, family, kind) "
            "VALUES ('split-scale-test', 'Split Scale Test', 'test', 'quote')"
        )
        run_id = connection.execute(
            "INSERT INTO ingest.run (source_id, capability, started_at, status) "
            "VALUES ('split-scale-test', 'quotes', now(), 'succeeded') RETURNING id"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO raw.quote (instrument_id, source_id, ingest_run_id, observed_at, price) "
            "VALUES (%s, 'split-scale-test', %s, '2026-07-02T20:00:00Z', 100)",
            [instrument_id, run_id],
        )
        connection.commit()
    assert client.post(
        "/api/portfolio/transactions",
        json={
            "symbol": "NVDA",
            "transaction_type": "split",
            "quantity": 2,
            "executed_at": "2026-07-03T15:30:00Z",
            "idempotency_key": "split-scale-event",
        },
    ).status_code == 200

    holding = client.get("/api/portfolio").json()["rows"][0]

    assert holding["quantity"] == 20
    assert holding["avg_cost"] == 50
    assert holding["valuation_status"] == "cost_basis_fallback"
    assert holding["market_value"] == 1000


def test_position_purchase_date_uses_market_day_and_survives_replay(client: TestClient) -> None:
    first = client.post(
        "/api/portfolio/transactions",
        json={
            "symbol": "NVDA",
            "transaction_type": "buy",
            "quantity": 1,
            "price": 100,
            "executed_at": "2026-07-02T01:00:00Z",
            "idempotency_key": "market-date-first",
        },
    )
    second = client.post(
        "/api/portfolio/transactions",
        json={
            "symbol": "NVDA",
            "transaction_type": "buy",
            "quantity": 1,
            "price": 110,
            "executed_at": "2026-07-02T15:00:00Z",
            "idempotency_key": "market-date-second",
        },
    )
    assert first.status_code == second.status_code == 200
    second_id = second.json()["transaction"]["id"]
    assert client.post(
        f"/api/portfolio/transactions/{second_id}/reverse",
        json={"idempotency_key": "market-date-reverse-second"},
    ).status_code == 200

    holding = client.get("/api/portfolio").json()["rows"][0]

    assert holding["quantity"] == 1
    assert holding["purchase_date"] == "2026-07-01"


def test_portfolio_return_uses_invested_capital_after_full_sale(client: TestClient) -> None:
    buy = {
        "symbol": "MSFT",
        "transaction_type": "buy",
        "quantity": 10,
        "price": 100,
        "executed_at": "2026-07-01T15:30:00Z",
        "idempotency_key": "return-msft-buy",
    }
    sell = {
        "symbol": "MSFT",
        "transaction_type": "sell",
        "quantity": 10,
        "price": 120,
        "executed_at": "2026-07-02T15:30:00Z",
        "idempotency_key": "return-msft-sell",
    }
    assert client.post("/api/portfolio/transactions", json=buy).status_code == 200
    assert client.post("/api/portfolio/transactions", json=sell).status_code == 200

    summary = client.get("/api/portfolio/summary").json()
    assert summary["portfolio_value"] == 0.0
    assert summary["net_contributions"] == -200.0
    assert summary["invested_capital"] == 1000.0
    assert summary["total_pnl"] == 200.0
    assert summary["total_pnl_pct"] == 20.0
    performance = client.get("/api/portfolio/performance").json()["rows"]
    assert performance[-1]["total_return_pct"] == 20.0


def test_portfolio_summary_aggregates_beyond_activity_limit(client: TestClient, postgres_dsn: str) -> None:
    with closing(psycopg.connect(postgres_dsn)) as connection:
        connection.execute(
            """
            INSERT INTO app.portfolio_transaction
                (transaction_type, amount, fees, realized_pnl, executed_at, idempotency_key)
            SELECT 'fee', 1, 0, 0, now() + make_interval(secs => value), 'fee-' || value
            FROM generate_series(1, 501) value
            """
        )
        connection.commit()

    assert client.get("/api/portfolio/transactions").json()["count"] == 100
    summary = client.get("/api/portfolio/summary").json()
    assert summary["fees"] == 501.0
    assert summary["total_pnl"] == -501.0


def test_portfolio_performance_starts_at_first_transaction(client: TestClient, postgres_dsn: str) -> None:
    assert client.post(
        "/api/portfolio/transactions",
        json={
            "symbol": "LLY",
            "transaction_type": "buy",
            "quantity": 1,
            "price": 100,
            "executed_at": "2026-07-10T15:30:00Z",
            "idempotency_key": "inception-lly-buy",
        },
    ).status_code == 200
    with closing(psycopg.connect(postgres_dsn)) as connection:
        instrument_id = connection.execute("SELECT id FROM catalog.instrument WHERE symbol = 'LLY'").fetchone()[0]
        connection.execute(
            "INSERT INTO ingest.source (id, name, family, kind) VALUES ('inception-test', 'Inception Test', 'test', 'price')"
        )
        run_id = connection.execute(
            "INSERT INTO ingest.run (source_id, capability, started_at, status) "
            "VALUES ('inception-test', 'prices', now(), 'succeeded') RETURNING id"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO raw.price_bar
                (instrument_id, source_id, ingest_run_id, interval, trading_date, observed_at, close)
            VALUES
                (%s, 'inception-test', %s, '1d', '2026-07-01', '2026-07-01T20:00:00Z', 90),
                (%s, 'inception-test', %s, '1d', '2026-07-10', '2026-07-10T20:00:00Z', 100)
            """,
            [instrument_id, run_id, instrument_id, run_id],
        )
        connection.commit()

    rows = client.get("/api/portfolio/performance").json()["rows"]
    assert [row["date"] for row in rows] == ["2026-07-10"]


def test_portfolio_correlation_explains_window_weight_and_risk(client: TestClient, postgres_dsn: str) -> None:
    for symbol in ("MSFT", "LLY"):
        assert client.post(
            "/api/portfolio/transactions",
            json={
                "symbol": symbol,
                "transaction_type": "buy",
                "quantity": 10,
                "price": 100,
                "executed_at": "2026-04-01T15:30:00Z",
                "idempotency_key": f"test-correlation-{symbol}",
            },
        ).status_code == 200
    with closing(psycopg.connect(postgres_dsn)) as connection:
        connection.execute(
            "INSERT INTO ingest.source (id, name, family, kind) VALUES ('correlation-test', 'Correlation Test', 'test', 'price')"
        )
        run_id = connection.execute(
            "INSERT INTO ingest.run (source_id, capability, started_at, status) "
            "VALUES ('correlation-test', 'prices', now(), 'succeeded') RETURNING id"
        ).fetchone()[0]
        ids = dict(connection.execute("SELECT symbol, id FROM catalog.instrument WHERE symbol IN ('MSFT', 'LLY')").fetchall())
        for symbol, multiplier in (("MSFT", 1), ("LLY", 2)):
            connection.execute(
                """
                INSERT INTO raw.price_bar
                    (instrument_id, source_id, ingest_run_id, interval, trading_date, observed_at, close)
                SELECT %s, 'correlation-test', %s, '1d',
                       date '2026-07-15' - (69 - value),
                       timestamptz '2026-07-15 20:00:00+00' - make_interval(days => 69 - value),
                       %s * (100 + value)
                FROM generate_series(0, 69) value
                """,
                [ids[symbol], run_id, multiplier],
            )
            connection.execute(
                "INSERT INTO raw.quote (instrument_id, source_id, ingest_run_id, observed_at, price) "
                "VALUES (%s, 'correlation-test', %s, '2026-07-15T20:00:00Z', %s)",
                [ids[symbol], run_id, multiplier * 169],
            )
        _confirm_price_facts(connection, run_id)
        connection.commit()

    payload = client.get("/api/portfolio-risk/correlation-edges")
    assert payload.status_code == 200
    rows = payload.json()["rows"]
    sixty = next(row for row in rows if row["lookback_days"] == 60)
    assert sixty["symbol"] == "LLY"
    assert sixty["peer_symbol"] == "MSFT"
    assert sixty["observations"] == 60
    assert sixty["correlation"] == 1.0
    assert sixty["combined_weight"] == 100.0
    assert sixty["risk_level"] == "critical"
    assert "move together" in sixty["interpretation"]

    cards = client.get("/api/portfolio-risk/cards").json()["rows"]
    correlation_card = next(row for row in cards if row["risk_type"] == "correlation")
    assert correlation_card["severity"] == "critical"
    assert correlation_card["symbols"] == ["LLY", "MSFT"]


def test_portfolio_panel_scope_publishes_reconciled_intelligence_tables(client: TestClient) -> None:
    assert client.post(
        "/api/portfolio/transactions",
        json={
            "symbol": "NVDA",
            "transaction_type": "buy",
            "quantity": 2,
            "price": 100,
            "executed_at": "2026-07-01T15:30:00Z",
            "idempotency_key": "test-panel-scope-nvda",
        },
    ).status_code == 200

    payload = client.get("/api/panel-snapshot?scope=portfolio")
    assert payload.status_code == 200
    tables = payload.json()["tables"]
    assert {
        "portfolio",
        "portfolio_summary",
        "portfolio_performance",
        "portfolio_transactions",
        "correlation_edges",
        "exposure_clusters",
        "portfolio_risk_cards",
        "review_actions",
    }.issubset(tables)
    assert tables["portfolio_summary"]["rows"][0]["portfolio_value"] == 200.0
    assert tables["portfolio_transactions"]["rows"][0]["symbol"] == "NVDA"


def test_portfolio_only_panel_read_skips_full_intelligence_bundle(
    client: TestClient,
    postgres_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from investment_panel.database import panel_models as postgres_panel

    def fail_if_bundled(_config: dict[str, object]) -> dict[str, list[dict[str, object]]]:
        raise AssertionError("portfolio-only reads must not build the full intelligence bundle")

    monkeypatch.setattr(postgres_panel, "portfolio_intelligence_tables", fail_if_bundled)

    tables, _metadata = postgres_panel.load_postgres_tables(
        {"database": {"url": postgres_dsn}},
        ("portfolio",),
    )

    assert tables["portfolio"] == []


def test_shared_risk_models_use_live_portfolio_contracts(
    client: TestClient,
    postgres_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from investment_panel.database import panel_models as postgres_panel

    published = {
        "portfolio_risk_cards": [{"card_id": "published-card"}],
        "review_actions": [{"action_id": "published-action"}],
    }
    live = {
        "portfolio": [], "portfolio_summary": [], "portfolio_performance": [], "portfolio_transactions": [],
        "correlation_edges": [], "exposure_clusters": [],
        "portfolio_risk_cards": [{"card_id": "live-card", "risk_type": "concentration"}],
        "review_actions": [{"action_id": "live-action", "next_step": "Review sizing"}],
    }
    monkeypatch.setattr(postgres_panel, "_published_tables", lambda _runtime, _requested: published.copy())
    monkeypatch.setattr(postgres_panel, "portfolio_intelligence_tables", lambda _config: live)
    tables, _metadata = postgres_panel.load_postgres_tables(
        {"database": {"url": postgres_dsn}},
        ("portfolio_risk_cards", "review_actions"),
    )
    assert tables["portfolio_risk_cards"] == live["portfolio_risk_cards"]
    assert tables["review_actions"] == live["review_actions"]


def test_shared_scopes_load_one_live_portfolio_contract_bundle(
    client: TestClient,
    postgres_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from investment_panel.database import panel_models as postgres_panel

    published = {
        "portfolio_risk_cards": [{"card_id": "published-card"}],
        "review_actions": [{"action_id": "published-action"}],
    }
    live = {
        "portfolio": [],
        "portfolio_summary": [],
        "portfolio_performance": [],
        "portfolio_transactions": [],
        "correlation_edges": [{"edge_id": "live-edge", "lookback_days": 60}],
        "exposure_clusters": [{"cluster_id": "live-cluster"}],
        "portfolio_risk_cards": [{"card_id": "live-card"}],
        "review_actions": [{"action_id": "live-action"}],
    }
    monkeypatch.setattr(postgres_panel, "_published_tables", lambda _runtime, _requested: published.copy())
    monkeypatch.setattr(postgres_panel, "portfolio_intelligence_tables", lambda _config: live)

    tables, _metadata = postgres_panel.load_postgres_tables(
        {"database": {"url": postgres_dsn}},
        ("correlation_edges", "exposure_clusters", "portfolio_risk_cards", "review_actions"),
    )
    assert tables["correlation_edges"] == live["correlation_edges"]
    assert tables["exposure_clusters"] == live["exposure_clusters"]
    assert tables["portfolio_risk_cards"] == live["portfolio_risk_cards"]
    assert tables["review_actions"] == live["review_actions"]


def test_future_prices_cannot_become_current_portfolio_value(client: TestClient, postgres_dsn: str) -> None:
    now = datetime.now(UTC)
    assert client.post(
        "/api/portfolio/transactions",
        json={
            "symbol": "MSFT",
            "transaction_type": "buy",
            "quantity": 1,
            "price": 100,
            "executed_at": (now - timedelta(days=2)).isoformat(),
            "idempotency_key": "future-price-msft-buy",
        },
    ).status_code == 200
    with closing(psycopg.connect(postgres_dsn)) as connection:
        instrument_id = connection.execute("SELECT id FROM catalog.instrument WHERE symbol = 'MSFT'").fetchone()[0]
        connection.execute(
            "INSERT INTO ingest.source (id, name, family, kind) VALUES ('future-price-test', 'Future Price Test', 'test', 'quote')"
        )
        run_id = connection.execute(
            "INSERT INTO ingest.run (source_id, capability, started_at, status) "
            "VALUES ('future-price-test', 'quotes', now(), 'succeeded') RETURNING id"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO raw.quote
                (instrument_id, source_id, ingest_run_id, observed_at, price)
            VALUES
                (%s, 'future-price-test', %s, %s, 110),
                (%s, 'future-price-test', %s, %s, 1000)
            """,
            [instrument_id, run_id, now - timedelta(hours=1), instrument_id, run_id, now + timedelta(days=1)],
        )
        connection.execute(
            """
            INSERT INTO raw.price_bar
                (instrument_id, source_id, ingest_run_id, interval, trading_date, observed_at, close)
            VALUES (%s, 'future-price-test', %s, '1d', %s, %s, 2000)
            """,
            [instrument_id, run_id, (now + timedelta(days=1)).date(), now + timedelta(days=1)],
        )
        _confirm_price_facts(connection, run_id)
        connection.commit()

    summary = client.get("/api/portfolio/summary").json()
    assert summary["portfolio_value"] == 110.0
    performance = client.get("/api/portfolio/performance").json()["rows"]
    assert performance[-1]["portfolio_value"] == 110.0
    assert performance[-1]["date"] <= now.date().isoformat()


def test_failed_price_correction_falls_back_to_last_confirmed_fact(
    client: TestClient,
    postgres_dsn: str,
) -> None:
    now = datetime.now(UTC)
    assert client.post(
        "/api/portfolio/transactions",
        json={
            "symbol": "MSFT", "transaction_type": "buy", "quantity": 1, "price": 90,
            "executed_at": (now - timedelta(days=2)).isoformat(),
            "idempotency_key": "failed-correction-msft-buy",
        },
    ).status_code == 200
    runtime = DatabaseRuntime(postgres_dsn)
    runtime.open()
    try:
        repository = IngestionRepository(runtime)
        repository.register_source("correction", name="Correction", family="market_data", kind="quote")
        observed_at = now - timedelta(hours=1)
        successful_run = repository.start_run("correction", "quotes")
        repository.store_quotes(
            successful_run, "correction",
            [{"symbol": "MSFT", "observed_at": observed_at, "price": 100}],
        )
        repository.finish_run(successful_run, "succeeded", item_count=1, instrument_count=1)
        failed_run = repository.start_run("correction", "quotes")
        repository.store_quotes(
            failed_run, "correction",
            [{"symbol": "MSFT", "observed_at": observed_at, "price": 200}],
        )
        repository.finish_run(failed_run, "failed", failure_detail="provider validation failed")
    finally:
        runtime.close()

    holding = client.get("/api/portfolio").json()["rows"][0]
    assert holding["price"] == 100
    assert holding["market_value"] == 100


def test_watchlist_route_round_trip_and_soft_exclusion(client: TestClient, postgres_dsn: str) -> None:
    response = client.post(
        "/api/watchlist/symbols",
        json={"symbol": "btc-usd", "name": "Bitcoin", "asset_class": "equity", "notes": "macro"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["watchlist_symbol"]["asset_class"] == "crypto"
    assert payload["data_refresh"] == {
        "status": "ok", "symbol": "BTC-USD", "asset_class": "crypto", "quote_rows": 1,
    }
    assert payload["watchlist"]["rows"][0]["symbol"] == "BTC-USD"

    deleted = client.delete("/api/watchlist/symbols/BTC-USD")
    assert deleted.status_code == 200
    assert deleted.json()["watchlist"] == {"rows": [], "count": 0}

    with closing(psycopg.connect(postgres_dsn)) as connection:
        state = connection.execute(
            "SELECT watch_state FROM app.watchlist_item w "
            "JOIN catalog.instrument i ON i.id = w.instrument_id WHERE i.symbol = 'BTC-USD'"
        ).fetchone()[0]
    assert state == "excluded"


def test_position_and_thesis_edits_preserve_existing_crypto_asset_class(client: TestClient, postgres_dsn: str) -> None:
    watched = client.post(
        "/api/watchlist/symbols",
        json={"symbol": "BTC-USD", "name": "Bitcoin", "asset_class": "crypto"},
    )
    assert watched.status_code == 200
    assert client.post(
        "/api/portfolio/positions",
        json={"symbol": "BTC-USD", "quantity": 0.5, "avg_cost": 50000},
    ).status_code == 200
    assert client.put(
        "/api/theses/BTC-USD",
        json={"thesis": "Institutional adoption continues."},
    ).status_code == 200

    with closing(psycopg.connect(postgres_dsn)) as connection:
        asset_class = connection.execute(
            "SELECT asset_class FROM catalog.instrument WHERE symbol = 'BTC-USD'"
        ).fetchone()[0]
    assert asset_class == "crypto"


def test_routes_reject_invalid_user_state(client: TestClient) -> None:
    invalid_position = client.post(
        "/api/portfolio/positions",
        json={"symbol": "NVDA", "quantity": 0, "avg_cost": 100},
    )
    invalid_watchlist = client.post(
        "/api/watchlist/symbols",
        json={"symbol": "not a ticker!", "asset_class": "equity"},
    )
    invalid_purchase_date = client.post(
        "/api/portfolio/positions",
        json={"symbol": "NVDA", "quantity": 1, "avg_cost": 100, "purchase_date": "2026-02-30"},
    )
    assert invalid_position.status_code == 400
    assert invalid_watchlist.status_code == 400
    assert invalid_purchase_date.status_code == 400


def test_position_compatibility_route_accepts_iso_datetime_purchase_date(client: TestClient) -> None:
    response = client.post(
        "/api/portfolio/positions",
        json={
            "symbol": "MSFT",
            "quantity": 1,
            "avg_cost": 100,
            "purchase_date": "2026-07-01T15:30:00Z",
        },
    )

    assert response.status_code == 200
    assert response.json()["portfolio"]["rows"][0]["purchase_date"] == "2026-07-01"


def test_thesis_routes_keep_revision_history_and_monitor_invalidation(client: TestClient, postgres_dsn: str) -> None:
    client.post(
        "/api/portfolio/positions",
        json={"symbol": "MU", "quantity": 4, "avg_cost": 95},
    )
    first = client.put(
        "/api/theses/MU",
        json={
            "thesis": "Memory pricing is entering an upcycle.",
            "why": "Owned for improving supply discipline.",
            "invalidation": "Below $80 the cycle thesis breaks.",
            "invalidation_price": 80,
            "evidence_links": ["https://example.com/memory"],
        },
    )
    assert first.status_code == 200
    assert first.json()["thesis"]["revision"] == 2
    monitor = first.json()["thesis_monitor"]["rows"][0]
    assert monitor["source"] == "theses"
    assert monitor["stale_thesis"] is False
    assert monitor["needs_review"] is False
    assert monitor["invalidation_price"] == 80.0

    second = client.put(
        "/api/theses/MU",
        json={
            "thesis": "Memory pricing and HBM demand are accelerating.",
            "why": "Owned for improving supply discipline.",
            "invalidation": "Below $82 the cycle thesis breaks.",
        },
    )
    assert second.status_code == 200
    assert second.json()["thesis"]["revision"] == 3

    reviewed = client.post("/api/theses/MU/review")
    assert reviewed.status_code == 200
    assert reviewed.json()["review"]["revision"] == 3
    assert reviewed.json()["review"]["outcome"] == "unchanged"

    theses = client.get("/api/theses").json()["rows"]
    assert len(theses) == 1
    assert theses[0]["revision"] == 3
    assert theses[0]["thesis_json"]["core_thesis"].startswith("Memory pricing and HBM")
    api_history = client.get("/api/theses/MU/history").json()
    assert api_history["review_events"][0]["outcome"] == "unchanged"

    with closing(psycopg.connect(postgres_dsn)) as connection:
        history = connection.execute(
            "SELECT revision, status FROM app.thesis t JOIN catalog.instrument i ON i.id = t.instrument_id "
            "WHERE i.symbol = 'MU' ORDER BY revision"
        ).fetchall()
    assert history == [(1, "superseded"), (2, "superseded"), (3, "current")]


def test_thesis_route_requires_content(client: TestClient) -> None:
    response = client.put("/api/theses/NVDA", json={"thesis": "   "})
    assert response.status_code == 400
