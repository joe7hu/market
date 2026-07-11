"""Portfolio and watchlist write operations."""

from __future__ import annotations
from datetime import date
from typing import Any, Iterable

from app.data_access.config import _database_path
from app.data_access.coerce import _optional_date, _positive_number
from app.data_access.user_state import (
    delete_position as delete_postgres_position,
    delete_watchlist_item,
    save_position as save_postgres_position,
    save_thesis as save_postgres_thesis,
    save_watchlist_item,
    mark_thesis_reviewed as mark_postgres_thesis_reviewed,
)



def save_portfolio_position(config: dict[str, Any], position: dict[str, Any]) -> dict[str, Any]:
    """Insert or update a manually entered portfolio position."""

    symbol = str(position.get("symbol", "")).strip().upper()
    if not symbol:
        raise ValueError("symbol is required")
    quantity = _positive_number(position.get("quantity"), "quantity")
    avg_cost = _positive_number(position.get("avg_cost"), "avg_cost", allow_zero=True)
    purchase_date = _optional_date(position.get("purchase_date"))
    notes = str(position.get("notes", "") or "").strip()

    return save_postgres_position(
        config,
        {"symbol": symbol, "quantity": quantity, "avg_cost": avg_cost, "purchase_date": purchase_date, "notes": notes},
    )




def save_watchlist_symbol(config: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    """Insert or update a manually entered watchlist symbol."""

    symbol = str(item.get("symbol", "")).strip().upper()
    if not symbol:
        raise ValueError("symbol is required")
    name = str(item.get("name") or "").strip() or symbol
    notes = str(item.get("notes", "") or "").strip()

    from investment_panel.core.decision import SYMBOL_RE
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
    return save_watchlist_item(
        config,
        {"symbol": normalized, "name": name, "asset_class": asset_class, "notes": notes},
    )




def populate_watchlist_symbol_data(config: dict[str, Any], symbol: str, asset_class: str | None = None) -> dict[str, Any]:
    """Best-effort targeted market-data refresh for a newly watched symbol."""

    normalized = str(symbol or "").strip().upper()
    if not normalized:
        return {"status": "skipped", "error": "symbol is required"}

    from investment_panel.analysis.valuation import store_valuation_models
    from investment_panel.core.db import db, init_db
    from investment_panel.core.decision import refresh_decision_read_models
    from investment_panel.core.free_sources import update_tradingview_sources, update_yfinance_sources
    from investment_panel.core.prices import fetch_prices, upsert_prices
    from investment_panel.core.scoring import score_and_store
    from investment_panel.core.technicals import compute_and_store

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
        "yfinance": {"status": "skipped"},
        "tradingview": {"status": "skipped"},
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
                result["yfinance"] = update_yfinance_sources(
                    con,
                    _targeted_refresh_config(config, normalized, asset_class),
                    symbols=[normalized],
                )
                result["market_snapshots"] = int(result["yfinance"].get("market_snapshots", 0) or 0)
            except Exception as exc:  # pragma: no cover - provider boundary
                result["errors"]["yfinance"] = f"{type(exc).__name__}: {exc}"

        if asset_class in {"equity", "etf"} and _tradingview_enabled(data_sources):
            try:
                result["tradingview"] = update_tradingview_sources(
                    con,
                    _targeted_refresh_config(config, normalized, asset_class),
                    symbols=[normalized],
                )
            except Exception as exc:  # pragma: no cover - provider boundary
                result["errors"]["tradingview"] = f"{type(exc).__name__}: {exc}"

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


def _tradingview_enabled(data_sources: dict[str, Any]) -> bool:
    tradingview = data_sources.get("tradingview")
    return isinstance(tradingview, dict) and bool(tradingview.get("enabled"))


def _targeted_refresh_config(config: dict[str, Any], symbol: str, asset_class: str | None):
    from pathlib import Path

    from investment_panel.core.config import (
        AppConfig,
        DataSourcesConfig,
        DatabaseConfig,
        OpenCliConfig,
        TradingViewConfig,
    )

    data_sources = config.get("data_sources", {}) if isinstance(config.get("data_sources"), dict) else {}
    opencli_raw = data_sources.get("opencli", {}) if isinstance(data_sources.get("opencli"), dict) else {}
    tradingview_raw = data_sources.get("tradingview", {}) if isinstance(data_sources.get("tradingview"), dict) else {}

    return AppConfig(
        database=DatabaseConfig(duckdb_path=Path(_database_path(config))),
        data_sources=DataSourcesConfig(
            opencli=OpenCliConfig(
                enabled=bool(opencli_raw.get("enabled", True)),
                command=str(opencli_raw.get("command") or "opencli"),
                timeout_seconds=int(opencli_raw.get("timeout_seconds", 25)),
            ),
            tradingview=TradingViewConfig(
                enabled=True,
                options_symbols=list(tradingview_raw.get("options_symbols") or []),
                search_symbols=list(tradingview_raw.get("search_symbols") or []),
                watchlist_colors=list(tradingview_raw.get("watchlist_colors") or ["red", "orange", "yellow", "green", "blue", "purple"]),
                alert_types=list(tradingview_raw.get("alert_types") or ["active", "triggered", "offline"]),
                personal_surfaces_enabled=bool(tradingview_raw.get("personal_surfaces_enabled", True)),
                chart_state_enabled=bool(tradingview_raw.get("chart_state_enabled", True)),
                screener_limit=int(tradingview_raw.get("screener_limit", 50)),
                news_limit=int(tradingview_raw.get("news_limit", 50)),
                strikes_around_spot=int(tradingview_raw.get("strikes_around_spot", 6)),
                option_scan_limit=int(tradingview_raw.get("option_scan_limit", 80)),
            ),
        ),
        watchlist=[
            *list(config.get("watchlist") or []),
            {"symbol": symbol, "asset_class": asset_class or "equity"},
        ],
    )




def delete_watchlist_symbol(config: dict[str, Any], symbol: str) -> dict[str, Any]:
    normalized = symbol.strip().upper()
    if not normalized:
        raise ValueError("symbol is required")

    from investment_panel.core.decision import SYMBOL_RE
    from investment_panel.core.instruments import normalize_symbol

    normalized = normalize_symbol(normalized)
    if not normalized or not SYMBOL_RE.match(normalized):
        raise ValueError("symbol must be a valid ticker")
    return delete_watchlist_item(config, normalized)




def delete_portfolio_position(config: dict[str, Any], symbol: str) -> dict[str, Any]:
    normalized = symbol.strip().upper()
    if not normalized:
        raise ValueError("symbol is required")

    return delete_postgres_position(config, normalized)


def save_thesis(config: dict[str, Any], symbol: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Author or update the structured thesis content for a symbol.

    Merges supplied fields onto any existing thesis_json and stamps last_reviewed
    so the monitor can leave the stale/needs-review state once content exists.
    """

    return save_postgres_thesis(config, symbol, fields)


def mark_thesis_reviewed(config: dict[str, Any], symbol: str) -> dict[str, Any]:
    """Stamp the thesis last_reviewed date so an audited thesis leaves the queue."""

    return mark_postgres_thesis_reviewed(config, symbol)
