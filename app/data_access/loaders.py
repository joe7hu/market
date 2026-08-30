"""PostgreSQL panel read-model loading and scope selection."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from investment_panel.core.config import AppConfig, load_config
from investment_panel.core.panel import tables_for_scope
from app.data_access.types import DataStatus, PanelData
from investment_panel.core.panel import SCOPED_TABLE_ROW_LIMITS, TICKER_INITIAL_TABLES, panel_contract_payload as contract_panel_payload
from investment_panel.database.panel_models import load_postgres_tables
from investment_panel.database.runtime import DatabaseRuntime
from investment_panel.database.ticker_decisions import TickerDecisionRepository


# These dossier models are useful when present but can require deep historical
# joins. Keep their timeout or partial state local to the optional panel rather
# than aborting the complete ticker response and its decision evidence.
_TICKER_OPTIONAL_DEEP_TABLES = ("liquidity", "options_payoff_scenarios")


def load_decision_funnel(
    runtime: DatabaseRuntime, *, action_queue: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Load the backend-owned decision-lane diagnostic."""

    return TickerDecisionRepository(runtime).decision_funnel(action_queue=action_queue)


def load_panel_data(
    config: AppConfig | None = None,
    table_names: Iterable[str] | None = None,
    ensure_decision_models: bool | None = None,
    ensure_source_models: bool | None = None,
    query_row_limits: dict[str, int] | None = None,
    query_symbol_filter: set[str] | None = None,
    portfolio_summary_include_performance: bool = True,
    thesis_monitor_include_current_prices: bool = True,
) -> PanelData:
    del ensure_decision_models, ensure_source_models
    active_config = config if config is not None else load_config()
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
        if not portfolio_summary_include_performance:
            query_options["portfolio_summary_include_performance"] = False
        if not thesis_monitor_include_current_prices:
            query_options["thesis_monitor_include_current_prices"] = False
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


def load_daily_research_panel_data(config: AppConfig | None = None) -> PanelData:
    """Load the prompt seed first, then SQL-bound symbol detail for that universe."""

    from investment_panel.core.daily_research_prompt_fields import (
        DAILY_RESEARCH_QUERY_LIMITS,
        DAILY_RESEARCH_MACRO_SYMBOLS,
        DAILY_RESEARCH_TABLES,
    )

    active_config = config if config is not None else load_config()
    # The full universe model joins all recent source evidence. It is useful
    # for Watchlist, but the prompt seed is the owned set, explicit watches,
    # and Radar tickets. This avoids a broad discovery read on this API route.
    seed_names = ("portfolio", "manual_watchlist", "option_radar_opportunity")
    seed = load_panel_data(active_config, table_names=seed_names)
    symbols: set[str] = set()
    # Keep support for a preloaded universe row without loading it in the live
    # prompt path.
    for name in (*seed_names, "universe_screen"):
        for row in seed.rows(name):
            watch_state = str(row.get("watch_state") or "").lower()
            if watch_state == "excluded" or (name == "universe_screen" and watch_state not in {"owned", "watched"}):
                continue
            symbol = str(row.get("symbol") or row.get("ticker") or row.get("underlying") or "").strip().upper()
            if symbol:
                symbols.add(symbol)
    symbols.update(DAILY_RESEARCH_MACRO_SYMBOLS)
    remaining = tuple(
        name
        for name in DAILY_RESEARCH_TABLES
        if name not in seed_names and name not in {"quotes", "universe_screen"}
    )
    detail = load_panel_data(
        active_config,
        table_names=remaining,
        query_row_limits=DAILY_RESEARCH_QUERY_LIMITS,
        query_symbol_filter=symbols,
        portfolio_summary_include_performance=False,
        thesis_monitor_include_current_prices=False,
    )
    # Position rows already carry provider-backed owned quotes. Reuse them in
    # the research context. The generic current-price selector can scan for a
    # long time and still return no admissible fact; it remains in quote-aware
    # routes and is not an execution input here.
    owned_quotes = [
        {
            "symbol": row.get("symbol") or row.get("ticker"),
            "price": row.get("price"),
            "observed_at": row.get("quote_observed_at"),
            "source": row.get("quote_source"),
        }
        for row in seed.rows("portfolio")
        if row.get("price") is not None
    ]
    metadata = {**detail.metadata, "daily_research_bounded": True, "daily_research_symbol_count": len(symbols)}
    ready = seed.status.ready and detail.status.ready
    message = "PostgreSQL daily research context loaded with bounded symbol queries." if ready else detail.status.message
    return PanelData(
        status=DataStatus(ready, message, detail.status.source),
        tables={**seed.tables, **detail.tables, "quotes": owned_quotes},
        metadata=metadata,
    )


def load_panel_scope_data(config: AppConfig | None, scope: str) -> PanelData:
    active_config = config if config is not None else load_config()
    if scope == "portfolio":
        return load_portfolio_scope_data(active_config)
    if scope == "opportunities":
        return load_opportunities_scope_data(active_config)
    requested = tuple(tables_for_scope(scope))
    query_row_limits = {
        table: limit
        for table, limit in SCOPED_TABLE_ROW_LIMITS.get(scope, {}).items()
        if table in requested
    }
    return load_panel_data(active_config, table_names=requested, query_row_limits=query_row_limits or None)


def load_opportunities_scope_data(config: AppConfig | None = None) -> PanelData:
    """Load the opportunity queue and its secondary screener without the dashboard bundle."""

    active_config = config if config is not None else load_config()
    return load_panel_data(
        active_config,
        table_names=("opportunities_ranked", "screener"),
        query_row_limits={"screener": 120},
    )


def load_portfolio_scope_data(config: AppConfig | None = None) -> PanelData:
    """Load portfolio detail only for currently held instruments.

    The generic ``quotes`` model has an intentionally broad no-filter mode.
    The portfolio route must never use it: its source of truth is the current
    position set, so passing that concrete set protects the PIT selector from
    materializing the whole instrument catalog.
    """

    active_config = config if config is not None else load_config()
    seed = load_panel_data(active_config, table_names=("portfolio",))
    if not seed.status.ready:
        return seed
    symbols = {
        str(row.get("symbol") or row.get("ticker") or "").strip().upper()
        for row in seed.rows("portfolio")
        if str(row.get("symbol") or row.get("ticker") or "").strip()
    }
    detail_names = tuple(name for name in tables_for_scope("portfolio") if name != "portfolio")
    detail = load_panel_data(
        active_config,
        table_names=detail_names,
        query_symbol_filter=symbols,
        query_row_limits={"quotes": max(24, len(symbols) * 2)},
    )
    ready = seed.status.ready and detail.status.ready
    return PanelData(
        status=DataStatus(
            ready,
            "PostgreSQL loaded bounded portfolio details." if ready else detail.status.message,
            detail.status.source,
        ),
        tables={**seed.tables, **detail.tables},
        metadata={**seed.metadata, **detail.metadata, "portfolio_symbol_count": len(symbols), "portfolio_bounded": True},
    )


def load_watchlist_scope_data(
    config: AppConfig | None, scope: str, *, offset: int = 0, limit: int | None = None
) -> PanelData:
    """Load a watchlist page in two bounded passes.

    The universe is small enough to fetch once for totals, but detailed models
    must be restricted to visible symbols.  This keeps candidate pagination in
    PostgreSQL and avoids scanning every historical fundamental/option row just
    to render the first page.
    """
    active_config = config if config is not None else load_config()
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
        "ticker_decisions",
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


def load_table_panel_data(config: AppConfig | None, table_name: str) -> PanelData:
    limits = {
        "event_decision_packets": 200,
        "decision_truth": 500,
        "event_scout_events": 200,
    }
    return load_panel_data(
        config,
        table_names=(table_name,),
        query_row_limits={table_name: limits[table_name]} if table_name in limits else None,
    )


def load_table_panel_page(
    config: AppConfig | None,
    table_name: str,
    *,
    limit: int,
    snapshot_at: datetime,
    after: tuple[Any, str] | None = None,
) -> tuple[list[dict[str, Any]], int, tuple[Any, str] | None]:
    from investment_panel.database.panel_pagination import load_postgres_table_page

    return load_postgres_table_page(
        config if config is not None else load_config(),
        table_name,
        limit=limit,
        snapshot_at=snapshot_at,
        after=after,
    )


def load_ticker_panel_data(config: AppConfig | None, ticker: str) -> PanelData:
    normalized = ticker.strip().upper()
    if not normalized:
        return PanelData(status=DataStatus(False, "Ticker is required.", "invalid-request"), tables={})
    active_config = config if config is not None else load_config()
    optional_tables = set(_TICKER_OPTIONAL_DEEP_TABLES)
    core_tables = tuple(name for name in TICKER_INITIAL_TABLES if name not in optional_tables)
    panel = load_panel_data(
        active_config,
        table_names=core_tables,
        query_symbol_filter={normalized},
        query_row_limits={name: 24 for name in core_tables},
    )
    optional_failures: dict[str, str] = {}
    for table_name in _TICKER_OPTIONAL_DEEP_TABLES:
        optional = load_panel_data(
            active_config,
            table_names=(table_name,),
            query_symbol_filter={normalized},
            query_row_limits={table_name: 24},
        )
        panel.tables[table_name] = optional.tables.get(table_name, [])
        if not optional.status.ready:
            optional_failures[table_name] = optional.status.message
    if optional_failures:
        unavailable = set(panel.metadata.get("unavailable_models") or ())
        unavailable.update(optional_failures)
        panel.metadata["unavailable_models"] = sorted(unavailable)
        panel.metadata["ticker_optional_unavailable"] = optional_failures
        if panel.status.ready:
            panel.status = DataStatus(
                True,
                "PostgreSQL loaded ticker core; optional dossier evidence is unavailable.",
                "postgresql-partial",
            )
    try:
        policy_tables, policy_metadata = load_postgres_tables(
            active_config,
            ("ticker_policy_learning",),
            query_row_limits={"ticker_policy_learning": 1},
        )
        panel.tables["ticker_policy_learning"] = policy_tables.get("ticker_policy_learning", [])
        panel.metadata["ticker_policy_learning"] = {
            "database": policy_metadata.get("database"),
            "schema_revision": policy_metadata.get("schema_revision"),
        }
    except Exception as exc:
        panel.tables["ticker_policy_learning"] = []
        panel.metadata["ticker_policy_learning_error"] = str(exc)
    panel.tables = {
        name: [row for row in rows if _row_symbol(row) in {"", normalized}]
        for name, rows in panel.tables.items()
    }
    panel.metadata["ticker"] = normalized
    return panel


def panel_contract_payload() -> dict[str, Any]:
    return contract_panel_payload()


def load_market_panel_data(config: AppConfig | None = None) -> PanelData:
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
