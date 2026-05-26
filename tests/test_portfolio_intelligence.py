from __future__ import annotations

import json

from investment_panel.core.db import db, init_db, query_rows, upsert_instrument
from investment_panel.core.panel import load_panel_data
from investment_panel.core.portfolio_intelligence import correlation_edges, exposure_clusters, portfolio_risk_cards, review_actions


def test_portfolio_intelligence_flags_cluster_concentration_and_review_actions(tmp_path) -> None:
    db_path = tmp_path / "risk.duckdb"
    init_db(db_path)
    with db(db_path, read_only=False) as con:
        seed_portfolio_risk_inputs(con)

        clusters = exposure_clusters(con)
        edges = correlation_edges(con)
        cards = portfolio_risk_cards(con)
        actions = review_actions(con)

    tech = next(row for row in clusters if row["cluster_type"] == "sector" and row["cluster_name"] == "Technology")
    assert round(tech["portfolio_weight"], 1) == 80.0
    assert tech["duplicate_exposure"] is True
    assert set(tech["symbols"]) == {"AMD", "NVDA"}
    assert tech["is_actionable"] is True
    assert "max combined weight" in tech["next_step"]
    asset_class = next(row for row in clusters if row["cluster_type"] == "asset_class")
    assert asset_class["duplicate_exposure"] is False
    assert asset_class["is_actionable"] is False
    assert "not hidden duplicate exposure" in asset_class["risk_note"]

    owned_edge = next(row for row in edges if row["edge_type"] == "owned_owned")
    assert {owned_edge["symbol"], owned_edge["peer_symbol"]} == {"NVDA", "AMD"}
    assert owned_edge["risk_level"] in {"watch", "critical"}

    risk_types = {row["risk_type"] for row in cards}
    assert "cluster_concentration" in risk_types
    assert "hidden_duplicate_exposure" in risk_types
    assert "thesis_gap" in risk_types
    assert all(row["impact"] and row["next_step"] for row in cards)
    assert any(action["action_type"] == "duplicate_exposure_review" for action in actions)
    assert all(action["impact"] and action["suggested_next_step"] for action in actions)


def test_correlation_edges_prioritize_material_edges_over_weak_owned_pair(tmp_path) -> None:
    db_path = tmp_path / "edge-order.duckdb"
    init_db(db_path)
    with db(db_path, read_only=False) as con:
        for symbol in ["MSFT", "LLY"]:
            upsert_instrument(
                con,
                {
                    "symbol": symbol,
                    "name": symbol,
                    "asset_class": "equity",
                    "sector": "Technology" if symbol == "MSFT" else "Healthcare",
                    "industry": "test",
                    "category": "owned-portfolio",
                    "source": "test",
                },
            )
            con.execute("INSERT INTO portfolio_positions VALUES (?, 10, 100, '2025-01-01', '')", [symbol])
            con.execute("INSERT INTO prices_daily VALUES (?, '2026-05-19', 100, 100, 100, 100, 1000, 'test')", [symbol])
            con.execute("INSERT INTO prices_daily VALUES (?, '2026-05-20', 100, 100, 100, 100, 1000, 'test')", [symbol])
            con.execute(
                "INSERT INTO source_freshness VALUES (?, 'closing_quote', 'test', '2026-05-20', 'fresh', 'previous close', 'ok', '', false, current_timestamp)",
                [f"previous_close:{symbol}"],
            )
        con.execute(
            "INSERT INTO correlation_runs VALUES ('corr-msft', 'MSFT', '2026-05-20', 180, ?, '{}')",
            [json.dumps([{"symbol": "LLY", "correlation": 0.04}, {"symbol": "QQQ", "correlation": 0.64}])],
        )

        edges = correlation_edges(con)

    assert edges[0]["peer_symbol"] == "QQQ"
    assert edges[0]["correlation"] == 0.64
    assert any(row["edge_type"] == "owned_owned" and row["correlation"] == 0.04 for row in edges)


def test_portfolio_intelligence_tables_load_through_panel_contract(tmp_path) -> None:
    db_path = tmp_path / "panel-risk.duckdb"
    init_db(db_path)
    with db(db_path, read_only=False) as con:
        seed_portfolio_risk_inputs(con)

    panel = load_panel_data({"database": {"duckdb_path": str(db_path)}})
    tables = panel["tables"]

    assert tables["exposure_clusters"]
    assert tables["correlation_edges"]
    assert tables["portfolio_risk_cards"]
    assert tables["review_actions"]
    with db(db_path) as con:
        assert query_rows(con, "SELECT count(*) AS count FROM portfolio_positions")[0]["count"] == 3


def seed_portfolio_risk_inputs(con) -> None:
    for symbol, sector, industry in [
        ("NVDA", "Technology", "Semiconductors"),
        ("AMD", "Technology", "Semiconductors"),
        ("LLY", "Healthcare", "Drug Manufacturers"),
    ]:
        upsert_instrument(
            con,
            {
                "symbol": symbol,
                "name": symbol,
                "asset_class": "equity",
                "sector": sector,
                "industry": industry,
                "category": "owned-portfolio",
                "source": "test",
            },
        )
    con.execute("INSERT INTO portfolio_positions VALUES ('NVDA', 10, 100, '2025-01-01', 'ai')")
    con.execute("INSERT INTO portfolio_positions VALUES ('AMD', 10, 100, '2025-01-01', 'ai')")
    con.execute("INSERT INTO portfolio_positions VALUES ('LLY', 10, 100, '2025-01-01', 'glp1')")
    for symbol, close in [("NVDA", 500), ("AMD", 300), ("LLY", 200)]:
        con.execute("INSERT INTO prices_daily VALUES (?, '2026-05-19', 100, 100, 100, ?, 1000, 'test')", [symbol, close * 0.95])
        con.execute("INSERT INTO prices_daily VALUES (?, '2026-05-20', 100, 100, 100, ?, 1000, 'test')", [symbol, close])
        con.execute(
            "INSERT INTO source_freshness VALUES (?, 'closing_quote', 'test', '2026-05-20', 'fresh', 'previous close', 'ok', '', false, current_timestamp)",
            [f"previous_close:{symbol}"],
        )
    con.execute(
        "INSERT INTO correlation_runs VALUES ('corr-nvda', 'NVDA', '2026-05-20', 180, ?, '{}')",
        [json.dumps([{"symbol": "AMD", "correlation": 0.82}, {"symbol": "LLY", "correlation": 0.05}])],
    )
    con.execute(
        "INSERT INTO correlation_runs VALUES ('corr-amd', 'AMD', '2026-05-20', 180, ?, '{}')",
        [json.dumps([{"symbol": "NVDA", "correlation": 0.82}, {"symbol": "LLY", "correlation": 0.03}])],
    )
    con.execute(
        "INSERT INTO theses VALUES ('NVDA', ?, current_timestamp)",
        [json.dumps({"position_status": "owned", "core_thesis": "AI accelerator leader", "pillars": ["datacenter"], "risks": ["cycle"], "invalidation": ["margin break"], "catalysts": ["earnings"]})],
    )
    con.execute("INSERT INTO catalysts VALUES ('cat-nvda', 'NVDA', '2026-06-01', 'earnings', 'high', 'test', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, '{}')")
    con.execute("INSERT INTO disclosures VALUES ('disc-amd', 'public_disclosure_transaction', 'Tracker', 'Filer', 'AMD', '2026-04-01', '2026-04-15', 'BUY', '$1M', '{}', 'https://example.com')")
