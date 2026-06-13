"""Portfolio and watchlist write operations."""

from __future__ import annotations
from datetime import date, datetime
from typing import Any, Iterable

from app.data_access.config import _database_path
from app.data_access.coerce import _optional_date, _positive_number



def save_portfolio_position(config: dict[str, Any], position: dict[str, Any]) -> dict[str, Any]:
    """Insert or update a manually entered portfolio position."""

    symbol = str(position.get("symbol", "")).strip().upper()
    if not symbol:
        raise ValueError("symbol is required")
    quantity = _positive_number(position.get("quantity"), "quantity")
    avg_cost = _positive_number(position.get("avg_cost"), "avg_cost", allow_zero=True)
    purchase_date = _optional_date(position.get("purchase_date"))
    notes = str(position.get("notes", "") or "").strip()

    from investment_panel.core.db import db, init_db
    from investment_panel.core.decision import refresh_decision_read_models
    from investment_panel.core.portfolio import ensure_portfolio_instruments

    db_path = _database_path(config)
    init_db(db_path)
    with db(db_path, read_only=False) as con:
        con.execute(
            """
            INSERT OR REPLACE INTO portfolio_positions (symbol, quantity, avg_cost, purchase_date, notes)
            VALUES (?, ?, ?, ?, ?)
            """,
            [symbol, quantity, avg_cost, purchase_date, notes],
        )
        con.execute(
            """
            INSERT OR IGNORE INTO theses (symbol, thesis_json, updated_at)
            VALUES (?, ?, now())
            """,
            [
                symbol,
                '{"position_status":"owned","core_thesis":"","pillars":[],"risks":[],"invalidation":[],"catalysts":[],"conviction":"unknown"}',
            ],
        )
        ensure_portfolio_instruments(con)
        refresh_decision_read_models(con, config.get("watchlist", []))
    return {"symbol": symbol, "quantity": quantity, "avg_cost": avg_cost, "purchase_date": purchase_date, "notes": notes}




def save_watchlist_symbol(config: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    """Insert or update a manually entered watchlist symbol."""

    symbol = str(item.get("symbol", "")).strip().upper()
    if not symbol:
        raise ValueError("symbol is required")
    name = str(item.get("name") or "").strip() or symbol
    notes = str(item.get("notes", "") or "").strip()

    from investment_panel.core.db import db, init_db
    from investment_panel.core.decision import SYMBOL_RE, upsert_instrument_preserving
    from investment_panel.core.instruments import infer_asset_class, normalize_symbol

    normalized = normalize_symbol(symbol)
    if not normalized or not SYMBOL_RE.match(normalized):
        raise ValueError("symbol must be a valid ticker")
    requested_asset_class = str(item.get("asset_class") or "").strip().lower()
    if normalized.endswith("-USD"):
        asset_class = "crypto"
    else:
        asset_class = requested_asset_class or infer_asset_class(normalized)
    if asset_class not in {"equity", "etf", "crypto"}:
        raise ValueError("asset_class must be equity, etf, or crypto")
    db_path = _database_path(config)
    init_db(db_path)
    with db(db_path, read_only=False) as con:
        con.execute(
            """
            INSERT INTO manual_watchlist (symbol, name, asset_class, watch_state, notes, created_at, updated_at)
            VALUES (?, ?, ?, 'watched', ?, now(), now())
            ON CONFLICT (symbol) DO UPDATE
            SET name = excluded.name,
                asset_class = excluded.asset_class,
                watch_state = 'watched',
                notes = excluded.notes,
                updated_at = now()
            """,
            [normalized, name, asset_class, notes],
        )
        upsert_instrument_preserving(
            con,
            {
                "symbol": normalized,
                "name": name,
                "asset_class": asset_class,
                "category": "watchlist",
                "source": "manual_watchlist",
            },
        )
    return {"symbol": normalized, "name": name, "asset_class": asset_class, "notes": notes}




def populate_watchlist_symbol_data(config: dict[str, Any], symbol: str, asset_class: str | None = None) -> dict[str, Any]:
    """Best-effort targeted market-data refresh for a newly watched symbol."""

    normalized = str(symbol or "").strip().upper()
    if not normalized:
        return {"status": "skipped", "error": "symbol is required"}

    from investment_panel.analysis.valuation import store_valuation_models
    from investment_panel.core.db import db, init_db
    from investment_panel.core.decision import refresh_decision_read_models
    from investment_panel.core.free_sources import store_yfinance_market_snapshot, update_instrument_from_yfinance
    from investment_panel.core.prices import fetch_prices, upsert_prices
    from investment_panel.core.scoring import score_and_store
    from investment_panel.core.technicals import compute_and_store
    from investment_panel.providers.yfinance_provider import YFinanceProvider, YFinanceUnavailable

    db_path = _database_path(config)
    market_data = config.get("market_data", {})
    data_sources = config.get("data_sources", {})
    yfinance_config = data_sources.get("yfinance", {}) if isinstance(data_sources.get("yfinance"), dict) else {}
    scoring = config.get("scoring", {}) if isinstance(config.get("scoring"), dict) else {}
    weights = scoring.get("weights") or {
        "technical": 0.25,
        "fundamental": 0.20,
        "category": 0.20,
        "thesis": 0.15,
        "trader": 0.10,
        "portfolio_fit": 0.10,
    }
    result: dict[str, Any] = {
        "status": "ok",
        "symbol": normalized,
        "asset_class": asset_class,
        "price_rows": 0,
        "technical_rows": 0,
        "market_snapshots": 0,
        "valuation_rows": 0,
        "scored": 0,
        "errors": {},
    }

    init_db(db_path)
    with db(db_path, read_only=False) as con:
        if asset_class in {"equity", "etf", "crypto"}:
            try:
                frame = fetch_prices(
                    normalized,
                    int(market_data.get("lookback_days", 260)),
                    str(market_data.get("mode", "online")),
                )
                result["price_rows"] = upsert_prices(con, frame)
            except Exception as exc:  # pragma: no cover - provider boundary
                result["errors"]["prices"] = f"{type(exc).__name__}: {exc}"

            try:
                result["technical_rows"] = 1 if compute_and_store(con, normalized) else 0
            except Exception as exc:  # pragma: no cover - defensive provider boundary
                result["errors"]["technicals"] = f"{type(exc).__name__}: {exc}"

        if asset_class in {"equity", "etf"} and yfinance_config.get("enabled", True):
            try:
                provider = YFinanceProvider()
                info = provider.info(normalized)
                update_instrument_from_yfinance(con, normalized, info)
                observed_at = datetime.utcnow().isoformat()
                run_id = f"watchlist:{normalized}:{observed_at}"
                result["market_snapshots"] = 1 if store_yfinance_market_snapshot(con, run_id, normalized, observed_at, info) else 0
            except YFinanceUnavailable as exc:
                result["errors"]["yfinance"] = str(exc)
            except Exception as exc:  # pragma: no cover - provider boundary
                result["errors"]["yfinance"] = f"{type(exc).__name__}: {exc}"

        try:
            result["valuation_rows"] = store_valuation_models(con, [normalized])
        except Exception as exc:  # pragma: no cover - defensive analysis boundary
            result["errors"]["valuation"] = f"{type(exc).__name__}: {exc}"

        try:
            result["scored"] = len(score_and_store(con, [normalized], weights))
        except Exception as exc:  # pragma: no cover - defensive analysis boundary
            result["errors"]["scoring"] = f"{type(exc).__name__}: {exc}"

        try:
            result["decision_models"] = refresh_decision_read_models(con, config.get("watchlist", []))
        except Exception as exc:  # pragma: no cover - defensive read-model boundary
            result["errors"]["decision_models"] = f"{type(exc).__name__}: {exc}"

    if result["errors"] and not any(result[key] for key in ("price_rows", "technical_rows", "market_snapshots", "valuation_rows", "scored")):
        result["status"] = "error"
    elif result["errors"]:
        result["status"] = "partial"
    return result




def delete_watchlist_symbol(config: dict[str, Any], symbol: str) -> dict[str, Any]:
    normalized = symbol.strip().upper()
    if not normalized:
        raise ValueError("symbol is required")

    from investment_panel.core.db import db, init_db
    from investment_panel.core.decision import SYMBOL_RE
    from investment_panel.core.instruments import normalize_symbol

    normalized = normalize_symbol(normalized)
    if not normalized or not SYMBOL_RE.match(normalized):
        raise ValueError("symbol must be a valid ticker")
    db_path = _database_path(config)
    init_db(db_path)
    with db(db_path, read_only=False) as con:
        con.execute(
            """
            INSERT INTO manual_watchlist (symbol, name, asset_class, watch_state, notes, created_at, updated_at)
            VALUES (?, ?, ?, 'excluded', '', now(), now())
            ON CONFLICT (symbol) DO UPDATE
            SET watch_state = 'excluded',
                updated_at = now()
            """,
            [normalized, normalized, "crypto" if normalized.endswith("-USD") else "equity"],
        )
    return {"symbol": normalized, "deleted": True}




def delete_portfolio_position(config: dict[str, Any], symbol: str) -> dict[str, Any]:
    normalized = symbol.strip().upper()
    if not normalized:
        raise ValueError("symbol is required")

    from investment_panel.core.db import db, init_db
    from investment_panel.core.decision import refresh_decision_read_models

    db_path = _database_path(config)
    init_db(db_path)
    with db(db_path, read_only=False) as con:
        con.execute("DELETE FROM portfolio_positions WHERE symbol = ?", [normalized])
        con.execute(
            """
            DELETE FROM theses
            WHERE symbol = ?
              AND thesis_json = ?
            """,
            [
                normalized,
                '{"position_status":"owned","core_thesis":"","pillars":[],"risks":[],"invalidation":[],"catalysts":[],"conviction":"unknown"}',
            ],
        )
        refresh_decision_read_models(con, config.get("watchlist", []))
    return {"symbol": normalized, "deleted": True}
