"""Top-level panel orchestration and dispatch.

The full-panel read uses the read-model :mod:`registry` for its name->loader
dispatch; the per-symbol ticker dossier composes accessors directly because it
filters and pushes the symbol down. Both choose their DuckDB connection mode by
need: a pure read takes a read-only connection, and only the ensure/refresh seam
opens read-write.
"""

from __future__ import annotations
from pathlib import Path
from threading import Lock
from typing import Any
from investment_panel.core.config import AppConfig, config_to_dict, load_config
from investment_panel.core.db import query_rows
from investment_panel.core.decision import effective_watchlist, refresh_decision_read_models
from investment_panel.core.sources import ensure_canonical_sources

from investment_panel.core.panel.registry import ReadContext, load_read_models
from investment_panel.core.panel.read_equity import symbol_decision_snapshots
from investment_panel.core.panel.read_options import radar_display_context
from investment_panel.core.panel.read_session import panel_read_session
from investment_panel.core.panel.ticker_dossier import load_ticker_dossier_tables



DECISION_REFRESH_LOCK = Lock()


DECISION_READ_MODEL_TABLES = {
    "decision_queue",
    "decision_readiness",
    "discovered_universe",
    "feed_signals",
    "source_freshness",
    "symbol_decision_snapshot",
    "symbol_decision_snapshots",
    "thesis_monitor",
    "universe_screen",
}

RADAR_CONTEXT_TABLES = {
    "option_radar_summary",
    "option_radar_opportunity",
    "agent_thesis",
    "agent_thesis_request",
    "agent_thesis_validation",
    "candidate_event",
    "candidate_event_mark",
    "candidate_event_attribution",
}


def _resolve_config(config: dict[str, Any] | AppConfig | None) -> tuple[AppConfig, Path, list[dict[str, Any]]]:
    """Normalize the caller's config into ``(app_config, db_path, watchlist)``.

    The API passes a plain dict (FastAPI compatibility path); every other caller
    passes an :class:`AppConfig`. This is the single place that branch lives.
    """

    app_config = config if isinstance(config, AppConfig) else load_config()
    if isinstance(config, dict):
        db_path = Path(config.get("database", {}).get("duckdb_path", "data/investment.duckdb"))
        if not db_path.is_absolute():
            db_path = Path.cwd() / db_path
        config_watchlist = list(config.get("watchlist", []))
    else:
        db_path = app_config.database.duckdb_path
        config_watchlist = app_config.watchlist
    return app_config, db_path, config_watchlist


def _ensure_or_probe_read_models(
    con: Any,
    active_watchlist: list[dict[str, Any]],
    requested_tables: set[str],
    should_ensure_sources: bool,
    should_ensure_decision: bool,
) -> dict[str, int | str | list[str]]:
    """Run the write-side refresh seam, or a read-only readiness probe.

    This is the only place the panel read path writes. When neither ensure flag
    is set the connection is read-only and this just reports row counts.
    """

    if should_ensure_sources:
        ensure_canonical_sources(con)
    if should_ensure_decision:
        return ensure_decision_read_models(con, active_watchlist)
    return decision_readiness_snapshot(con, requested_tables)


def load_panel_data(
    config: dict[str, Any] | AppConfig | None = None,
    table_names: list[str] | set[str] | tuple[str, ...] | None = None,
    ensure_decision_models: bool | None = None,
    ensure_source_models: bool | None = None,
) -> dict[str, Any]:
    app_config, db_path, config_watchlist = _resolve_config(config)
    requested_tables = None if table_names is None else set(table_names)
    requested_table_set = requested_tables or set()
    if requested_tables is not None and not requested_tables:
        return {
            "ready": True,
            "message": "No panel tables requested.",
            "source": "duckdb",
            "metadata": {
                "config": config_to_dict(app_config),
                "decision_refresh": {"status": "read_only_not_required", "missing": []},
            },
            "tables": {},
        }
    should_ensure_decision = (requested_tables is None) if ensure_decision_models is None else ensure_decision_models
    should_ensure_sources = should_ensure_decision or ((requested_tables is None) if ensure_source_models is None else ensure_source_models)
    # Pure reads take a read-only connection so they neither acquire the DuckDB
    # writer lock nor serialize behind other readers; only the ensure/refresh
    # seam needs read-write.
    needs_write = should_ensure_sources or should_ensure_decision
    with panel_read_session(db_path, needs_write=needs_write) as con:
        if con is None:
            tables = {name: [] for name in requested_table_set}
            return {
                "ready": False,
                "message": "DuckDB database does not exist yet. Run a refresh job to initialize it.",
                "source": "duckdb-missing",
                "metadata": {
                    "config": config_to_dict(app_config),
                    "decision_refresh": missing_database_readiness(requested_table_set),
                },
                "tables": tables,
            }
        active_watchlist = effective_watchlist(con, config_watchlist)
        decision_refresh = _ensure_or_probe_read_models(
            con,
            active_watchlist,
            requested_table_set,
            should_ensure_sources,
            should_ensure_decision,
        )
        decision_snapshots = symbol_decision_snapshots(con) if requested_tables is None or requested_table_set & DECISION_READ_MODEL_TABLES else []
        radar_context = radar_display_context(con) if requested_tables is None or requested_table_set & RADAR_CONTEXT_TABLES else None
        ctx = ReadContext(
            con=con,
            active_watchlist=active_watchlist,
            app_config=app_config,
            decision_snapshots=decision_snapshots,
            radar_context=radar_context,
        )
        tables = load_read_models(ctx, requested_tables)
    ready = any(tables.values()) if requested_tables is not None else any(tables.get(name) for name in ("signals", "candidates", "portfolio", "ticker_memos"))
    message = "Loaded investment panel data." if ready else "Database is initialized but contains no screened candidates yet."
    return {
        "ready": ready,
        "message": message,
        "source": "duckdb",
        "metadata": {"config": config_to_dict(app_config), "decision_refresh": decision_refresh},
        "tables": tables,
    }




def load_ticker_dossier_data(
    config: dict[str, Any] | AppConfig | None,
    ticker: str,
    ensure_decision_models: bool = True,
) -> dict[str, Any]:
    app_config, db_path, config_watchlist = _resolve_config(config)
    symbol = str(ticker or "").upper().strip()
    with panel_read_session(db_path, needs_write=ensure_decision_models) as con:
        if con is None:
            return {
                "ready": False,
                "message": "DuckDB database does not exist yet. Run a refresh job to initialize it.",
                "source": "duckdb-missing",
                "metadata": {
                    "config": config_to_dict(app_config),
                    "decision_refresh": missing_database_readiness(DECISION_READ_MODEL_TABLES),
                },
                "tables": {},
            }
        if ensure_decision_models:
            ensure_canonical_sources(con)
        active_watchlist = effective_watchlist(con, config_watchlist)
        readiness = (
            ensure_decision_read_models(con, active_watchlist)
            if ensure_decision_models
            else decision_readiness_snapshot(con, DECISION_READ_MODEL_TABLES)
        )
        tables = load_ticker_dossier_tables(con, active_watchlist, symbol)
    ready = any(rows for rows in tables.values())
    return {
        "ready": ready,
        "message": f"Loaded {symbol} ticker dossier." if ready else f"No ticker dossier rows loaded for {symbol}.",
        "source": "duckdb",
        "metadata": {"config": config_to_dict(app_config), "decision_refresh": readiness},
        "tables": tables,
    }




def ensure_decision_read_models(con: Any, config_watchlist: list[dict[str, Any]]) -> dict[str, int | str]:
    counts = query_rows(
        con,
        """
        SELECT
            (SELECT count(*) FROM discovered_universe) AS discovered_universe,
            (SELECT count(*) FROM decision_queue) AS decision_queue,
            (SELECT count(*) FROM source_freshness) AS source_freshness,
            (SELECT count(*) FROM symbol_decision_snapshots) AS symbol_decision_snapshots
        """,
    )[0]
    if all(int(counts.get(key) or 0) > 0 for key in counts):
        return {**counts, "status": "cached"}
    with DECISION_REFRESH_LOCK:
        counts = query_rows(
            con,
            """
            SELECT
                (SELECT count(*) FROM discovered_universe) AS discovered_universe,
                (SELECT count(*) FROM decision_queue) AS decision_queue,
                (SELECT count(*) FROM source_freshness) AS source_freshness,
                (SELECT count(*) FROM symbol_decision_snapshots) AS symbol_decision_snapshots
            """,
        )[0]
        if all(int(counts.get(key) or 0) > 0 for key in counts):
            return {**counts, "status": "cached"}
        result = refresh_decision_read_models(con, config_watchlist)
        return {**result, "status": "refreshed"}




def decision_readiness_snapshot(con: Any, requested_tables: set[str]) -> dict[str, int | str | list[str]]:
    counts = query_rows(
        con,
        """
        SELECT
            (SELECT count(*) FROM discovered_universe) AS discovered_universe,
            (SELECT count(*) FROM decision_queue) AS decision_queue,
            (SELECT count(*) FROM source_freshness) AS source_freshness,
            (SELECT count(*) FROM symbol_decision_snapshots) AS symbol_decision_snapshots
        """,
    )[0]
    missing = [name for name, count in counts.items() if int(count or 0) == 0]
    status = "read_only_ready" if not missing else "read_only_missing"
    if not requested_tables & DECISION_READ_MODEL_TABLES:
        status = "read_only_not_required"
    return {**counts, "status": status, "missing": missing}


def missing_database_readiness(requested_tables: set[str]) -> dict[str, int | str | list[str]]:
    missing = [
        "discovered_universe",
        "decision_queue",
        "source_freshness",
        "symbol_decision_snapshots",
    ]
    status = "read_only_missing_database"
    if not requested_tables & DECISION_READ_MODEL_TABLES:
        status = "read_only_not_required"
    return {
        "discovered_universe": 0,
        "decision_queue": 0,
        "source_freshness": 0,
        "symbol_decision_snapshots": 0,
        "status": status,
        "missing": missing,
    }




def get_panel_snapshot(config: dict[str, Any] | AppConfig | None = None) -> dict[str, Any]:
    return load_panel_data(config)
