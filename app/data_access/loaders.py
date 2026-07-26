"""PostgreSQL panel read-model loading and scope selection."""

from __future__ import annotations

from typing import Any, Iterable

from app.data_access.config import load_config, tables_for_scope
from app.data_access.postgres_panel import load_postgres_tables
from app.data_access.types import DataStatus, PanelData
from app.panel_contracts import TICKER_TABLES, panel_contract_payload as contract_panel_payload


def load_panel_data(
    config: dict[str, Any] | None = None,
    table_names: Iterable[str] | None = None,
    ensure_decision_models: bool | None = None,
    ensure_source_models: bool | None = None,
    query_row_limits: dict[str, int] | None = None,
    query_symbol_filter: set[str] | None = None,
) -> PanelData:
    del ensure_decision_models, ensure_source_models
    active_config = config or load_config()
    requested = _all_contract_tables() if table_names is None else tuple(table_names)
    if not requested:
        return PanelData(
            status=DataStatus(True, "No PostgreSQL read models requested.", "postgresql"),
            tables={},
            metadata={"database": "postgresql", "table_count": 0},
        )
    try:
        query_options: dict[str, Any] = {}
        if query_row_limits:
            query_options["query_row_limits"] = query_row_limits
        if query_symbol_filter is not None:
            query_options["query_symbol_filter"] = query_symbol_filter
        tables, metadata = load_postgres_tables(active_config, requested, **query_options)
    except Exception as exc:
        return PanelData(
            status=DataStatus(False, f"PostgreSQL read models unavailable: {exc}", "postgresql-error"),
            tables={name: [] for name in requested},
            metadata={"database": "postgresql", "error": str(exc)},
        )
    unavailable = list(metadata.get("unavailable_models") or [])
    available_count = int(metadata.get("available_model_count") or 0)
    if unavailable:
        message = f"PostgreSQL loaded with {len(unavailable)} explicitly unavailable read models."
        status = DataStatus(available_count > 0, message, "postgresql-partial")
    else:
        status = DataStatus(True, "PostgreSQL read models loaded.", "postgresql")
    return PanelData(
        status=status,
        tables=tables,
        metadata=metadata,
    )


def load_daily_research_panel_data(config: dict[str, Any] | None = None) -> PanelData:
    """Load the prompt seed first, then SQL-bound symbol detail for that universe."""

    from investment_panel.core.daily_research_prompt_fields import (
        DAILY_RESEARCH_QUERY_LIMITS,
        DAILY_RESEARCH_MACRO_SYMBOLS,
        DAILY_RESEARCH_TABLES,
    )

    active_config = config or load_config()
    seed_names = ("portfolio", "manual_watchlist", "universe_screen", "option_radar_opportunity")
    seed = load_panel_data(active_config, table_names=seed_names)
    symbols: set[str] = set()
    for name in seed_names:
        for row in seed.rows(name):
            watch_state = str(row.get("watch_state") or "").lower()
            if watch_state == "excluded" or (name == "universe_screen" and watch_state not in {"owned", "watched"}):
                continue
            symbol = str(row.get("symbol") or row.get("ticker") or row.get("underlying") or "").strip().upper()
            if symbol:
                symbols.add(symbol)
    symbols.update(DAILY_RESEARCH_MACRO_SYMBOLS)
    remaining = tuple(name for name in DAILY_RESEARCH_TABLES if name not in seed_names)
    detail = load_panel_data(
        active_config,
        table_names=remaining,
        query_row_limits=DAILY_RESEARCH_QUERY_LIMITS,
        query_symbol_filter=symbols,
    )
    metadata = {**detail.metadata, "daily_research_bounded": True, "daily_research_symbol_count": len(symbols)}
    ready = seed.status.ready and detail.status.ready
    message = "PostgreSQL daily research context loaded with bounded symbol queries." if ready else detail.status.message
    return PanelData(
        status=DataStatus(ready, message, detail.status.source),
        tables={**seed.tables, **detail.tables},
        metadata=metadata,
    )


def load_panel_scope_data(config: dict[str, Any] | None, scope: str) -> PanelData:
    return load_panel_data(config, table_names=tables_for_scope(scope))


def load_watchlist_scope_data(
    config: dict[str, Any] | None, scope: str, *, offset: int = 0, limit: int | None = None
) -> PanelData:
    """Load a watchlist page in two bounded passes.

    The universe is small enough to fetch once for totals, but detailed models
    must be restricted to visible symbols.  This keeps candidate pagination in
    PostgreSQL and avoids scanning every historical fundamental/option row just
    to render the first page.
    """
    active_config = config or load_config()
    seed = load_panel_data(active_config, table_names=("universe_screen", "manual_watchlist", "portfolio"))
    rows = seed.rows("universe_screen")
    if scope == "watchlist-watched":
        selected = [row for row in rows if str(row.get("watch_state") or "").lower() in {"watched", "owned"}]
    elif scope == "watchlist-unwatched":
        selected = [row for row in rows if str(row.get("watch_state") or "").lower() == "candidate"]
        selected = selected[max(0, offset): max(0, offset) + max(1, limit or 80)]
    else:
        selected = rows
    symbols = {str(row.get("symbol") or "").upper() for row in selected if row.get("symbol")}
    detail_names = (
        "quotes", "fundamentals", "technicals", "valuations", "decision_queue",
        "research_packets", "ticker_memos", "thesis_monitor", "options_ticker_signals",
    )
    detail = load_panel_data(
        active_config,
        table_names=detail_names,
        query_symbol_filter=symbols,
        query_row_limits={name: max(80, len(symbols) * 8) for name in detail_names},
    )
    # ``screener`` is an alias for the already-loaded universe screen; reusing
    # it prevents a second whole-universe CTE for the same request.
    tables = {**seed.tables, **detail.tables, "screener": seed.rows("universe_screen")}
    ready = seed.status.ready and detail.status.ready
    return PanelData(
        status=DataStatus(ready, "PostgreSQL loaded bounded watchlist details." if ready else detail.status.message, detail.status.source),
        tables=tables,
        metadata={**seed.metadata, **detail.metadata, "watchlist_symbol_count": len(symbols), "watchlist_bounded": True},
    )


def load_table_panel_data(config: dict[str, Any] | None, table_name: str) -> PanelData:
    return load_panel_data(config, table_names=(table_name,))


def load_ticker_panel_data(config: dict[str, Any] | None, ticker: str) -> PanelData:
    normalized = ticker.strip().upper()
    if not normalized:
        return PanelData(status=DataStatus(False, "Ticker is required.", "invalid-request"), tables={})
    panel = load_panel_data(
        config,
        table_names=TICKER_TABLES,
        query_symbol_filter={normalized},
        query_row_limits={name: 80 for name in TICKER_TABLES},
    )
    panel.tables = {
        name: [row for row in rows if _row_symbol(row) in {"", normalized}]
        for name, rows in panel.tables.items()
    }
    panel.metadata["ticker"] = normalized
    return panel


def panel_contract_payload() -> dict[str, Any]:
    return contract_panel_payload()


def load_market_panel_data(config: dict[str, Any] | None = None) -> PanelData:
    return load_panel_scope_data(config, "market")


def _all_contract_tables() -> tuple[str, ...]:
    contract = contract_panel_payload()
    values = set(contract.get("tables") or [])
    for names in (contract.get("scopes") or {}).values():
        values.update(names or [])
    values.update(contract.get("ticker_tables") or [])
    return tuple(sorted(values))


def _row_symbol(row: dict[str, Any]) -> str:
    return str(row.get("symbol") or row.get("ticker") or "").upper()
