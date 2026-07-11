from __future__ import annotations

from contextlib import closing

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import deps
from app.routers.portfolio import router
from app.routers.theses import router as theses_router
from investment_panel.database.authority import close_cached_runtimes
from investment_panel.database.migrations import upgrade_database


@pytest.fixture
def postgres_dsn(postgresql) -> str:
    info = postgresql.info
    credentials = info.user if not info.password else f"{info.user}:{info.password}"
    return f"postgresql://{credentials}@{info.host}:{info.port}/{info.dbname}"


@pytest.fixture
def client(postgres_dsn: str, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    upgrade_database(postgres_dsn)
    monkeypatch.setattr(deps, "load_config", lambda: {"database": {"url": postgres_dsn}})
    application = FastAPI()
    application.include_router(router)
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
    assert deleted.status_code == 200
    assert deleted.json()["portfolio"] == {"rows": [], "count": 0}


def test_watchlist_route_round_trip_and_soft_exclusion(client: TestClient, postgres_dsn: str) -> None:
    response = client.post(
        "/api/watchlist/symbols",
        json={"symbol": "btc-usd", "name": "Bitcoin", "asset_class": "equity", "notes": "macro"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["watchlist_symbol"]["asset_class"] == "crypto"
    assert payload["data_refresh"]["status"] == "pending_source_refresh"
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
    assert invalid_position.status_code == 400
    assert invalid_watchlist.status_code == 400


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
    assert reviewed.json()["review"]["revision"] == 4

    theses = client.get("/api/theses").json()["rows"]
    assert len(theses) == 1
    assert theses[0]["revision"] == 4
    assert theses[0]["thesis_json"]["core_thesis"].startswith("Memory pricing and HBM")

    with closing(psycopg.connect(postgres_dsn)) as connection:
        history = connection.execute(
            "SELECT revision, status FROM app.thesis t JOIN catalog.instrument i ON i.id = t.instrument_id "
            "WHERE i.symbol = 'MU' ORDER BY revision"
        ).fetchall()
    assert history == [(1, "superseded"), (2, "superseded"), (3, "superseded"), (4, "current")]


def test_thesis_route_requires_content(client: TestClient) -> None:
    response = client.put("/api/theses/NVDA", json={"thesis": "   "})
    assert response.status_code == 400
