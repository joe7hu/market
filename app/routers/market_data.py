"""Symbol-scoped quote read route."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from app import panel_snapshot as panel_owner
from app import dependencies
from app.data_access import loaders, payloads
from app.response_contracts import QuotesResponse
from investment_panel.core.config import AppConfig

router = APIRouter()


@router.get("/api/quotes", response_model=QuotesResponse, response_model_exclude_unset=True)
def quotes(
    symbols: str | None = Query(default=None, description="Comma-separated symbols, maximum 100."),
    config: AppConfig = Depends(dependencies.get_config),
) -> dict[str, Any]:
    requested = {symbol.strip().upper() for symbol in (symbols or "").split(",") if symbol.strip()}
    if len(requested) > 100:
        return {"status": "invalid", "error": "symbols supports at most 100 symbols", "rows": []}
    if not requested:
        return {"status": "invalid", "error": "symbols is required", "rows": []}
    cache_key = "table:quotes:" + ",".join(sorted(requested))
    _, panel_data = panel_owner.context(
        cache_key=cache_key,
        loader=lambda config: loaders.load_panel_data(
            config,
            table_names=("quotes",),
            query_symbol_filter=requested,
            query_row_limits={"quotes": len(requested)},
        ),
        config_loader=lambda: config,
    )
    return payloads.table_payload(panel_data, "quotes")


__all__ = ["router"]
