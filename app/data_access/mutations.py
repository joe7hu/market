"""Portfolio and watchlist write operations."""

from __future__ import annotations
from typing import Any, Iterable

from investment_panel.core.config import AppConfig
from investment_panel.database.user_state import (
    delete_watchlist_item,
    save_watchlist_item,
)
from investment_panel.database.thesis import (
    mark_thesis_reviewed as mark_postgres_thesis_reviewed,
    save_thesis as save_postgres_thesis,
)



def save_watchlist_symbol(config: AppConfig, item: dict[str, Any]) -> dict[str, Any]:
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




def populate_watchlist_symbol_data(config: AppConfig, symbol: str, asset_class: str | None = None) -> dict[str, Any]:
    """Run the canonical targeted market-data refresh for a newly watched symbol."""

    normalized = str(symbol or "").strip().upper()
    if not normalized:
        return {"status": "skipped", "error": "symbol is required"}

    try:
        from investment_panel.jobs.update_market_data import run_for_config

        result = run_for_config(config, symbols=[normalized], publish=False)
        if (
            result.get("status") == "ok"
            and int(result.get("symbols") or 0) == 1
            and int(result.get("price_rows") or 0) > 0
        ):
            return {
                "status": "ok",
                "symbol": normalized,
                "asset_class": asset_class,
                "quote_rows": int(result.get("price_rows") or 0),
                "market_metric_rows": int(result.get("market_metric_rows") or 0),
                "provider_rows_received": int(result.get("price_rows") or 0),
                "history_policy": "full_refresh",
                "analysis": "next_premarket_publication",
            }
    except Exception as exc:  # provider boundary
        return {"status": "error", "symbol": normalized, "quote_rows": 0, "error": f"{type(exc).__name__}: {exc}"}
    errors = {**dict(result.get("price_errors") or {}), **dict(result.get("market_metric_errors") or {})}
    reason = "; ".join(f"{key}: {value}" for key, value in errors.items()) or "targeted refresh returned no price rows"
    return {"status": "error", "symbol": normalized, "quote_rows": 0, "error": reason}




def delete_watchlist_symbol(config: AppConfig, symbol: str) -> dict[str, Any]:
    normalized = symbol.strip().upper()
    if not normalized:
        raise ValueError("symbol is required")

    from investment_panel.core.decision import SYMBOL_RE
    from investment_panel.core.instruments import normalize_symbol

    normalized = normalize_symbol(normalized)
    if not normalized or not SYMBOL_RE.match(normalized):
        raise ValueError("symbol must be a valid ticker")
    return delete_watchlist_item(config, normalized)




def save_thesis(config: AppConfig, symbol: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Author or update the structured thesis content for a symbol.

    Merges supplied fields onto any existing thesis_json and stamps last_reviewed
    so the monitor can leave the stale/needs-review state once content exists.
    """

    return save_postgres_thesis(config, symbol, fields)


def mark_thesis_reviewed(config: AppConfig, symbol: str) -> dict[str, Any]:
    """Stamp the thesis last_reviewed date so an audited thesis leaves the queue."""

    return mark_postgres_thesis_reviewed(config, symbol)
