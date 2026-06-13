"""Decision read-model refresh orchestration."""

from __future__ import annotations
from typing import Any
from investment_panel.core.sources import promote_source_signal_instruments, sync_canonical_sources

from investment_panel.core.decision.watchlist import effective_watchlist, ensure_watchlist_instruments, promote_universe_instruments, watchlist_from_config
from investment_panel.core.decision.builders import build_decision_queue, build_discovered_universe, build_source_freshness, build_symbol_decision_snapshots
from investment_panel.core.decision.persistence import persist_decision_queue, persist_discovered_universe, persist_source_freshness, persist_symbol_decision_snapshots



def refresh_decision_read_models(con: Any, config: Any | None = None) -> dict[str, Any]:
    """Build and persist the decision read models from current source tables."""

    watchlist = effective_watchlist(con, watchlist_from_config(config))
    promoted_watchlist_instruments = ensure_watchlist_instruments(con, watchlist)
    source_sync = sync_canonical_sources(con)
    promoted_instruments = promote_source_signal_instruments(con)
    source_freshness = build_source_freshness(con)
    universe = build_discovered_universe(con, watchlist)
    promoted_universe_instruments = promote_universe_instruments(con, universe)
    queue = build_decision_queue(con, universe, source_freshness)
    snapshots = build_symbol_decision_snapshots(queue, universe)

    persist_source_freshness(con, source_freshness)
    persist_discovered_universe(con, universe)
    persist_decision_queue(con, queue)
    persist_symbol_decision_snapshots(con, snapshots)
    return {
        "status": "decision_models_refreshed",
        "discovered_universe": len(universe),
        "source_freshness": len(source_freshness),
        "decision_queue": len(queue),
        "symbol_decision_snapshots": len(snapshots),
        "decision_universe_members": sum(1 for row in universe if row.get("decision_universe_member")),
        "stale_queue_rows": sum(1 for row in queue if row.get("action_grade") == "Stale"),
        "source_items": source_sync.get("items", 0),
        "source_signals": source_sync.get("signals", 0),
        "promoted_source_instruments": promoted_instruments,
        "promoted_watchlist_instruments": promoted_watchlist_instruments,
        "promoted_universe_instruments": promoted_universe_instruments,
        "watchlist_symbols": len(watchlist),
    }
