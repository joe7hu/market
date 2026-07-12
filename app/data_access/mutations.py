"""Portfolio and watchlist write operations."""

from __future__ import annotations
from datetime import UTC, date, datetime, time
from typing import Any, Iterable

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
    """Store only the latest provider quote for a newly watched symbol."""

    normalized = str(symbol or "").strip().upper()
    if not normalized:
        return {"status": "skipped", "error": "symbol is required"}

    from investment_panel.core.prices import fetch_prices
    from investment_panel.database.authority import runtime_for_config
    from investment_panel.database.ingestion import IngestionRepository

    market_data = config.get("market_data", {})
    repository = None
    run_id = None
    try:
        frame = fetch_prices(
            normalized,
            int(market_data.get("lookback_days", 30)),
            str(market_data.get("mode", "online")),
        )
        latest = frame.sort_values("date").iloc[-1].to_dict()
        observed_date = date.fromisoformat(str(latest["date"])[:10])
        observed_at = datetime.combine(observed_date, time(21), tzinfo=UTC)
        repository = IngestionRepository(runtime_for_config(config))
        repository.register_source(
            "watchlist_quote", name="Watchlist quote", family="market_data",
            kind="daily_quote", capabilities={"quotes": True},
        )
        run_id = repository.start_run("watchlist_quote", "quotes")
        stored = repository.store_quotes(
            run_id,
            "watchlist_quote",
            [{"symbol": normalized, "observed_at": observed_at, "price": latest["close"], "currency": "USD"}],
        )
        repository.finish_run(run_id, "succeeded", item_count=stored, instrument_count=1)
    except Exception as exc:  # provider boundary
        if repository is not None and run_id is not None:
            try:
                repository.finish_run(
                    run_id, "failed", failure_detail=f"{type(exc).__name__}: {exc}"
                )
            except Exception:
                pass
        return {"status": "error", "symbol": normalized, "quote_rows": 0, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "status": "ok",
        "symbol": normalized,
        "asset_class": asset_class,
        "quote_rows": stored,
        "provider_rows_received": len(frame),
        "history_policy": "latest_only",
        "analysis": "next_premarket_publication",
    }




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
