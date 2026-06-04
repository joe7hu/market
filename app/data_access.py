"""Data loading and JSON normalization for the investment panel API."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from importlib import import_module
import json
import os
from pathlib import Path
import sys
from typing import Any, Callable, Iterable


SETUP_INSTRUCTIONS = (
    "No investment panel data is available yet. Configure `config.yaml`, run the "
    "daily screen job that imports Arco evidence and market data, then refresh the app."
)


@dataclass(frozen=True)
class DataStatus:
    """Status summary for data loaded into the API."""

    ready: bool
    message: str
    source: str = "empty"


@dataclass
class PanelData:
    """Normalized tables consumed by API routes."""

    status: DataStatus
    tables: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def table(self, name: str) -> Any:
        return self.tables.get(name)

    def rows(self, name: str) -> list[dict[str, Any]]:
        return normalize_rows(self.table(name))


CORE_MODULE_CANDIDATES = (
    "src.investment_panel.core",
    "investment_panel.core",
)

CORE_HELPER_CANDIDATES = (
    "load_panel_data",
    "load_dashboard_data",
    "get_panel_snapshot",
    "get_dashboard_snapshot",
)


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _database_path(config: dict[str, Any]) -> Path:
    db_path = Path(config.get("database", {}).get("duckdb_path", "data/investment.duckdb"))
    return db_path if db_path.is_absolute() else project_root() / db_path


def database_path(config: dict[str, Any]) -> Path:
    return _database_path(config)


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load config.yaml when PyYAML is installed; fall back to sensible defaults."""

    config_path = Path(path) if path else project_root() / "config.yaml"
    defaults: dict[str, Any] = {
        "database": {"duckdb_path": "data/investment.duckdb"},
        "nas": {
            "source_root": "/Volumes/agent/data-sources",
            "status_dir": "/Volumes/agent/data-sources/status",
            "market_dir": "/Volumes/agent/data-sources/market-mini",
            "duckdb_snapshot_dir": "/Volumes/agent/data-sources/market-mini/duckdb-snapshots",
        },
        "arco": {"raw_dir": "/Volumes/agent/brain/raw/sources/arco"},
        "trader_profile_dir": "data/trader_profiles",
        "prompt_dir": "prompts",
    }
    if not config_path.exists():
        if os.environ.get("MARKET_DUCKDB_PATH"):
            defaults["database"]["duckdb_path"] = os.environ["MARKET_DUCKDB_PATH"]
        return defaults

    try:
        import yaml
    except ModuleNotFoundError:
        return defaults | {"config_warning": "Install PyYAML to read config.yaml."}

    with config_path.open("r", encoding="utf-8") as handle:
        parsed = yaml.safe_load(handle) or {}
    merged = _deep_merge(defaults, parsed)
    if os.environ.get("MARKET_DUCKDB_PATH"):
        merged.setdefault("database", {})["duckdb_path"] = os.environ["MARKET_DUCKDB_PATH"]
    return merged


def load_panel_data(config: dict[str, Any] | None = None) -> PanelData:
    """Load panel data through future core helpers, if present."""

    active_config = config or load_config()
    helper = _resolve_core_helper()
    if helper is None:
        return PanelData(
            status=DataStatus(
                ready=False,
                message=(
                    "Core data helpers are not installed yet. Expected one of "
                    f"{', '.join(CORE_HELPER_CANDIDATES)} under "
                    "`src.investment_panel.core` or `investment_panel.core`."
                ),
                source="missing-core",
            ),
            metadata={"setup_instructions": SETUP_INSTRUCTIONS},
        )

    try:
        raw_data = helper(active_config)
    except TypeError:
        raw_data = helper()
    except Exception as exc:  # pragma: no cover - defensive UI boundary
        return PanelData(
            status=DataStatus(
                ready=False,
                message=f"Core data helper failed: {exc}",
                source="core-error",
            ),
            metadata={"setup_instructions": SETUP_INSTRUCTIONS},
        )

    panel_data = _normalize_panel_data(raw_data)
    if _is_empty(panel_data):
        panel_data.status = DataStatus(
            ready=False,
            message="Core helpers returned no rows for the configured DuckDB.",
            source="empty-db",
        )
        panel_data.metadata.setdefault("setup_instructions", SETUP_INSTRUCTIONS)
    return panel_data


def load_market_panel_data(config: dict[str, Any] | None = None) -> PanelData:
    """Load only the broad-market tables required by the Market page."""

    active_config = config or load_config()
    _resolve_core_helper()
    from investment_panel.core.db import db, init_db
    from investment_panel.core.panel import market_environment_assets, market_environment_model, market_valuation_reference_charts

    db_path = _database_path(active_config)
    init_db(db_path)
    with db(db_path, read_only=False) as con:
        tables = {
            "market_valuation_reference_charts": market_valuation_reference_charts(con),
            "market_environment_assets": market_environment_assets(con),
            "market_environment_model": market_environment_model(con, [], include_exposure=False),
        }
    ready = any(tables.values())
    return PanelData(
        status=DataStatus(
            ready=ready,
            message="Loaded market environment data." if ready else "No market environment rows are loaded yet.",
            source="duckdb",
        ),
        tables=tables,
        metadata={},
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

    _resolve_core_helper()
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

    _resolve_core_helper()
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

    _resolve_core_helper()
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

    _resolve_core_helper()
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

    _resolve_core_helper()
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


def _positive_number(value: Any, name: str, allow_zero: bool = False) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if parsed < 0 or (parsed == 0 and not allow_zero):
        raise ValueError(f"{name} must be {'non-negative' if allow_zero else 'positive'}")
    return parsed


def _optional_date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value.isoformat()
    try:
        return datetime.fromisoformat(str(value)).date().isoformat()
    except ValueError as exc:
        raise ValueError("purchase_date must be YYYY-MM-DD") from exc


def _resolve_core_helper() -> Callable[..., Any] | None:
    src_path = project_root() / "src"
    if src_path.exists() and str(src_path) not in sys.path:
        sys.path.insert(0, str(src_path))
    for module_name in CORE_MODULE_CANDIDATES:
        try:
            module = import_module(module_name)
        except ModuleNotFoundError:
            continue
        for helper_name in CORE_HELPER_CANDIDATES:
            helper = getattr(module, helper_name, None)
            if callable(helper):
                return helper
    return None


def _normalize_panel_data(raw_data: Any) -> PanelData:
    if isinstance(raw_data, PanelData):
        return raw_data

    if isinstance(raw_data, dict):
        status = raw_data.get("status")
        if isinstance(status, DataStatus):
            data_status = status
        else:
            data_status = DataStatus(
                ready=bool(raw_data.get("ready", True)),
                message=str(raw_data.get("message", "Loaded data from core helpers.")),
                source=str(raw_data.get("source", "core")),
            )
        tables = raw_data.get("tables")
        if tables is None:
            tables = {
                key: value
                for key, value in raw_data.items()
                if key not in {"status", "ready", "message", "source", "metadata"}
            }
        return PanelData(
            status=data_status,
            tables=dict(tables),
            metadata=dict(raw_data.get("metadata", {})),
        )

    tables = {
        name: getattr(raw_data, name)
        for name in (
            "candidates",
            "discovered_universe",
            "decision_queue",
            "decision_readiness",
            "source_freshness",
            "symbol_decision_snapshot",
            "symbol_decision_snapshots",
            "signals",
            "ticker_memos",
            "portfolio",
            "theses",
            "thesis_monitor",
            "trader_twins",
            "catalysts",
            "fundamentals",
            "disclosures",
            "quotes",
            "screener",
            "options_expiries",
            "options_chain",
            "options_payoff_scenarios",
            "options_provider_capabilities",
            "options_expiry_signals",
            "options_ticker_signals",
            "option_strategy_versions",
            "option_snapshot",
            "option_features",
            "stock_features",
            "agent_thesis",
            "agent_thesis_request",
            "agent_thesis_validation",
            "candidate_event",
            "shadow_trade",
            "option_attribution",
            "missed_winner_event",
            "strategy_mutation_proposal",
            "news",
            "tradingview_symbol_search",
            "tradingview_watchlists",
            "tradingview_alerts",
            "tradingview_chart_state",
            "sepa",
            "liquidity",
            "correlations",
            "etf_premiums",
            "analyst_estimates",
            "earnings",
            "earnings_setups",
            "valuations",
            "provider_runs",
            "broker_status",
            "broker_accounts",
            "broker_positions",
            "broker_market_snapshots",
            "broker_scanner_signals",
            "agent_recommendations",
            "paper_orders",
            "daily_brief",
            "feed_signals",
            "universe_screen",
            "manual_watchlist",
            "source_consensus",
            "ownership_consensus",
            "market_context",
            "market_valuation_reference_charts",
            "market_valuation_charts",
            "market_environment_assets",
            "market_environment_model",
            "exposure_clusters",
            "correlation_edges",
            "portfolio_risk_cards",
            "review_actions",
            "source_health",
            "sources",
            "source_runs",
            "source_items",
            "ticker_source_signals",
            "settings",
        )
        if hasattr(raw_data, name)
    }
    return PanelData(
        status=DataStatus(True, "Loaded data from core helpers.", "core"),
        tables=tables,
        metadata={},
    )


def status_payload(panel_data: PanelData) -> dict[str, Any]:
    return {
        "ready": panel_data.status.ready,
        "message": panel_data.status.message,
        "source": panel_data.status.source,
        "metadata": jsonable(panel_data.metadata),
    }


def table_payload(panel_data: PanelData, table_name: str) -> dict[str, Any]:
    rows = panel_data.rows(table_name)
    return {"rows": rows, "count": len(rows), "status": status_payload(panel_data)}


def signals_payload(panel_data: PanelData) -> dict[str, Any]:
    rows = panel_data.rows("signals") or panel_data.rows("candidates")
    return {"rows": rows, "count": len(rows), "status": status_payload(panel_data)}


def dashboard_payload(panel_data: PanelData) -> dict[str, Any]:
    decision_queue = panel_data.rows("decision_queue")
    decision_readiness = panel_data.rows("decision_readiness")
    discovered_universe = panel_data.rows("discovered_universe")
    source_freshness = panel_data.rows("source_freshness")
    candidates = panel_data.rows("candidates")
    portfolio = panel_data.rows("portfolio")
    theses = panel_data.rows("theses")
    thesis_monitor = panel_data.rows("thesis_monitor")
    catalysts = panel_data.rows("catalysts")
    fundamentals = panel_data.rows("fundamentals")
    disclosures = panel_data.rows("disclosures")
    quotes = panel_data.rows("quotes")
    news = panel_data.rows("news")
    sepa = panel_data.rows("sepa")
    liquidity = panel_data.rows("liquidity")
    earnings = panel_data.rows("earnings")
    earnings_setups = panel_data.rows("earnings_setups")
    valuations = panel_data.rows("valuations")
    option_payoffs = panel_data.rows("options_payoff_scenarios")
    option_signals = panel_data.rows("options_ticker_signals")
    option_candidates = panel_data.rows("candidate_event")
    shadow_trades = panel_data.rows("shadow_trade")
    option_attributions = panel_data.rows("option_attribution")
    missed_winners = panel_data.rows("missed_winner_event")
    strategy_proposals = panel_data.rows("strategy_mutation_proposal")
    agent_thesis_requests = panel_data.rows("agent_thesis_request")
    agent_thesis_validations = panel_data.rows("agent_thesis_validation")
    source_health = panel_data.rows("source_health")
    sources = panel_data.rows("sources")
    source_runs = panel_data.rows("source_runs")
    source_items = panel_data.rows("source_items")
    ticker_source_signals = panel_data.rows("ticker_source_signals")
    broker_status = panel_data.rows("broker_status")
    agent_recommendations = panel_data.rows("agent_recommendations")
    daily_brief = panel_data.rows("daily_brief")
    feed_signals = panel_data.rows("feed_signals")
    universe_screen = panel_data.rows("universe_screen")
    source_consensus = panel_data.rows("source_consensus")
    ownership_consensus = panel_data.rows("ownership_consensus")
    market_context = panel_data.rows("market_context")
    market_valuation_reference_charts = panel_data.rows("market_valuation_reference_charts")
    market_valuation_charts = panel_data.rows("market_valuation_charts")
    market_environment_assets = panel_data.rows("market_environment_assets")
    market_environment_model = panel_data.rows("market_environment_model")
    portfolio_risk_cards = panel_data.rows("portfolio_risk_cards")
    review_actions = panel_data.rows("review_actions")
    priority_rows = decision_queue or candidates
    return {
        "status": status_payload(panel_data),
        "metrics": {
            "decision_queue": len(decision_queue),
            "discovered_universe": len(discovered_universe),
            "candidates": len(candidates),
            "holdings": len(portfolio),
            "theses": len(theses),
            "thesis_monitor": len(thesis_monitor),
            "catalysts": len(catalysts),
            "fundamentals": len(fundamentals),
            "disclosures": len(disclosures),
            "quotes": len(quotes),
            "news": len(news),
            "sepa": len(sepa),
            "liquidity": len(liquidity),
            "earnings": len(earnings),
            "earnings_setups": len(earnings_setups),
            "valuations": len(valuations),
            "options_payoff_scenarios": len(option_payoffs),
            "options_ticker_signals": len(option_signals),
            "option_radar_candidates": len(option_candidates),
            "shadow_trades": len(shadow_trades),
            "option_attributions": len(option_attributions),
            "missed_winners": len(missed_winners),
            "strategy_mutation_proposals": len(strategy_proposals),
            "agent_thesis_requests": len(agent_thesis_requests),
            "agent_thesis_validations": len(agent_thesis_validations),
            "sources": len(sources) or len(source_freshness) or len(source_health),
            "source_runs": len(source_runs),
            "source_items": len(source_items),
            "ticker_source_signals": len(ticker_source_signals),
            "broker_providers": len(broker_status),
            "agent_recommendations": len(agent_recommendations),
            "daily_brief": len(daily_brief),
            "feed_signals": len(feed_signals),
            "universe_screen": len(universe_screen),
            "source_consensus": len(source_consensus),
            "ownership_consensus": len(ownership_consensus),
            "market_context": len(market_context),
            "market_valuation_reference_charts": len(market_valuation_reference_charts),
            "market_valuation_charts": len(market_valuation_charts),
            "market_environment_assets": len(market_environment_assets),
            "market_environment_model": len(market_environment_model),
            "portfolio_risk_cards": len(portfolio_risk_cards),
            "review_actions": len(review_actions),
        },
        "decision_queue": decision_queue[:12],
        "decision_readiness": decision_readiness[:12],
        "priority_candidates": priority_rows[:8],
        "near_term_catalysts": catalysts[:8],
        "portfolio": portfolio[:8],
        "thesis_monitor": thesis_monitor[:8],
        "source_freshness": source_freshness[:12],
        "source_health": source_health[:8],
        "sources": sources[:12],
        "source_runs": source_runs[:12],
        "source_items": source_items[:12],
        "ticker_source_signals": ticker_source_signals[:12],
        "broker_status": broker_status[:8],
        "agent_recommendations": agent_recommendations[:8],
        "daily_brief": daily_brief[:12],
        "feed_signals": feed_signals[:12],
        "universe_screen": universe_screen[:12],
        "source_consensus": source_consensus[:12],
        "ownership_consensus": ownership_consensus[:12],
        "market_context": market_context[:12],
        "market_valuation_reference_charts": market_valuation_reference_charts[:8],
        "market_valuation_charts": market_valuation_charts[:24],
        "market_environment_assets": market_environment_assets[:80],
        "market_environment_model": market_environment_model[:12],
        "portfolio_risk_cards": portfolio_risk_cards[:8],
        "review_actions": review_actions[:8],
        "option_radar_candidates": option_candidates[:12],
        "shadow_trades": shadow_trades[:12],
        "option_attributions": option_attributions[:12],
        "missed_winners": missed_winners[:12],
        "strategy_mutation_proposals": strategy_proposals[:12],
        "agent_thesis_requests": agent_thesis_requests[:12],
        "agent_thesis_validations": agent_thesis_validations[:12],
        "disclosures": disclosures[:8],
        "news": news[:8],
    }


def panel_snapshot_payload(panel_data: PanelData, scope: str, offset: int = 0, limit: int | None = None) -> dict[str, Any]:
    if scope in {"watchlist-watched", "watchlist-unwatched"}:
        return watchlist_section_payload(panel_data, scope, offset=offset, limit=limit)

    scopes: dict[str, list[str]] = {
        "feed": [
            "feed_signals",
        ],
        "today": [
            "feed_signals",
            "decision_queue",
            "discovered_universe",
            "quotes",
            "portfolio",
            "catalysts",
            "earnings",
            "earnings_setups",
            "analyst_estimates",
            "fundamentals",
            "liquidity",
            "correlations",
            "technicals",
            "sepa",
            "valuations",
            "options_payoff_scenarios",
            "options_ticker_signals",
            "candidate_event",
            "shadow_trade",
            "option_attribution",
            "missed_winner_event",
            "strategy_mutation_proposal",
            "disclosures",
            "theses",
            "thesis_monitor",
            "research_packets",
            "agent_thesis",
            "agent_thesis_request",
            "agent_thesis_validation",
            "ticker_memos",
            "opportunity_sources",
            "daily_brief",
            "exposure_clusters",
            "correlation_edges",
            "portfolio_risk_cards",
            "review_actions",
        ],
        "watchlist": [
            "universe_screen",
            "manual_watchlist",
            "discovered_universe",
            "decision_queue",
            "quotes",
            "portfolio",
            "screener",
            "technicals",
            "valuations",
            "tradingview_watchlists",
            "options_ticker_signals",
        ],
        "sources": [
            "source_consensus",
            "feed_signals",
            "opportunity_sources",
            "theses",
            "news",
        ],
        "superinvestors": [
            "ownership_consensus",
            "disclosures",
        ],
        "market": [
            "market_valuation_reference_charts",
            "market_environment_assets",
            "market_environment_model",
        ],
        "dashboard": [
            "decision_queue",
            "discovered_universe",
            "quotes",
            "screener",
            "portfolio",
            "catalysts",
            "earnings",
            "earnings_setups",
            "analyst_estimates",
            "fundamentals",
            "etf_premiums",
            "liquidity",
            "correlations",
            "technicals",
            "sepa",
            "valuations",
            "options_expiries",
            "options_payoff_scenarios",
            "options_provider_capabilities",
            "options_expiry_signals",
            "options_ticker_signals",
            "option_strategy_versions",
            "option_snapshot",
            "option_features",
            "stock_features",
            "agent_thesis",
            "agent_thesis_request",
            "agent_thesis_validation",
            "candidate_event",
            "shadow_trade",
            "option_attribution",
            "missed_winner_event",
            "strategy_mutation_proposal",
            "disclosures",
            "theses",
            "thesis_monitor",
            "research_packets",
            "tradingview_symbol_search",
            "tradingview_watchlists",
            "tradingview_alerts",
            "tradingview_chart_state",
            "opportunity_sources",
            "ticker_source_signals",
            "exposure_clusters",
            "correlation_edges",
            "portfolio_risk_cards",
            "review_actions",
        ],
        "opportunities": [
            "decision_queue",
            "opportunities_ranked",
            "opportunity_sources",
            "signals",
            "candidates",
            "quotes",
            "catalysts",
            "earnings",
            "earnings_setups",
            "analyst_estimates",
            "liquidity",
            "technicals",
            "sepa",
            "valuations",
            "options_expiries",
            "options_payoff_scenarios",
            "options_expiry_signals",
            "options_ticker_signals",
            "candidate_event",
            "shadow_trade",
            "option_attribution",
            "missed_winner_event",
            "strategy_mutation_proposal",
            "agent_thesis_request",
            "agent_thesis_validation",
            "screener",
            "tradingview_symbol_search",
            "tradingview_watchlists",
            "tradingview_alerts",
            "tradingview_chart_state",
            "portfolio",
            "discovered_universe",
            "exposure_clusters",
            "correlation_edges",
            "portfolio_risk_cards",
            "review_actions",
        ],
        "portfolio": [
            "portfolio",
            "decision_queue",
            "quotes",
            "liquidity",
            "correlations",
            "valuations",
            "technicals",
            "sepa",
            "earnings_setups",
            "theses",
            "thesis_monitor",
            "catalysts",
            "disclosures",
            "exposure_clusters",
            "correlation_edges",
            "portfolio_risk_cards",
            "review_actions",
        ],
        "research": [
            "decision_queue",
            "research_packets",
            "ticker_memos",
            "theses",
            "thesis_monitor",
            "news",
            "fundamentals",
            "signals",
            "quotes",
            "earnings",
            "earnings_setups",
            "analyst_estimates",
            "valuations",
            "options_payoff_scenarios",
            "options_ticker_signals",
            "candidate_event",
            "shadow_trade",
            "option_attribution",
            "missed_winner_event",
            "strategy_mutation_proposal",
            "agent_thesis_request",
            "agent_thesis_validation",
            "tradingview_alerts",
            "tradingview_chart_state",
        ],
        "filings": ["ownership_consensus", "disclosures"],
        "calendar": ["catalysts", "earnings"],
        "health": ["source_freshness", "source_health", "provider_runs", "broker_status", "broker_accounts", "broker_positions", "agent_recommendations", "paper_orders"],
        "settings": [],
    }
    selected = scopes.get(scope, scopes["dashboard"])
    return {
        "scope": scope,
        "status": status_payload(panel_data),
        "dashboard": dashboard_payload(panel_data) if scope == "dashboard" else None,
        "tables": {name: {"rows": panel_data.rows(name), "count": len(panel_data.rows(name))} for name in selected},
    }


def watchlist_section_payload(panel_data: PanelData, scope: str, offset: int = 0, limit: int | None = None) -> dict[str, Any]:
    watched = scope == "watchlist-watched"
    prefix = "watchlist_watched" if watched else "watchlist_unwatched"
    sanitized_offset = max(0, int(offset or 0))
    sanitized_limit = max(1, int(limit)) if limit is not None else None
    universe_rows = [row for row in _watchlist_universe_rows(panel_data) if _is_active_watchlist_row(row) == watched]
    total_count = len(universe_rows)
    page_rows = universe_rows[sanitized_offset : sanitized_offset + sanitized_limit] if sanitized_limit is not None else universe_rows
    symbols = {str(row.get("symbol") or row.get("ticker") or "").upper() for row in page_rows if row.get("symbol") or row.get("ticker")}
    table_rows = {
        prefix: page_rows,
        f"{prefix}_quotes": _rows_for_symbols(panel_data.rows("quotes"), symbols),
        f"{prefix}_fundamentals": _rows_for_symbols(panel_data.rows("fundamentals"), symbols),
        f"{prefix}_technicals": _rows_for_symbols(panel_data.rows("technicals"), symbols),
        f"{prefix}_valuations": _rows_for_symbols(panel_data.rows("valuations"), symbols),
        f"{prefix}_screener": _rows_for_symbols(panel_data.rows("screener"), symbols),
        f"{prefix}_decision_queue": _rows_for_symbols(panel_data.rows("decision_queue"), symbols),
        f"{prefix}_portfolio": _rows_for_symbols(panel_data.rows("portfolio"), symbols),
        f"{prefix}_options": _rows_for_symbols(panel_data.rows("options_ticker_signals"), symbols),
    }
    table_counts = {name: len(rows) for name, rows in table_rows.items()}
    table_counts[prefix] = total_count
    if watched:
        unwatched_count = len([row for row in _watchlist_universe_rows(panel_data) if not _is_active_watchlist_row(row)])
        table_rows["watchlist_unwatched"] = []
        table_counts["watchlist_unwatched"] = unwatched_count
    return {
        "scope": scope,
        "status": status_payload(panel_data),
        "dashboard": None,
        "tables": {
            name: {
                "rows": rows,
                "count": table_counts[name],
                "offset": sanitized_offset if name == prefix else 0,
                "limit": sanitized_limit,
            }
            for name, rows in table_rows.items()
        },
    }


def _watchlist_universe_rows(panel_data: PanelData) -> list[dict[str, Any]]:
    manual_by_symbol = _manual_watchlist_by_symbol(panel_data)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_row in panel_data.rows("universe_screen"):
        symbol = _primary_symbol(raw_row)
        if not symbol:
            continue
        seen.add(symbol)
        manual = manual_by_symbol.get(symbol)
        watch_state = str((manual or {}).get("watch_state") or raw_row.get("watch_state") or "").lower()
        if watch_state == "excluded":
            continue
        row = dict(raw_row)
        if manual:
            row["watch_state"] = watch_state or "watched"
            row["name"] = manual.get("name") or row.get("name") or symbol
            row["asset_class"] = manual.get("asset_class") or row.get("asset_class")
        rows.append(row)

    for symbol, manual in manual_by_symbol.items():
        if symbol in seen:
            continue
        watch_state = str(manual.get("watch_state") or "watched").lower()
        if watch_state == "excluded":
            continue
        rows.append(
            {
                "symbol": symbol,
                "name": manual.get("name") or symbol,
                "asset_class": manual.get("asset_class") or ("crypto" if symbol.endswith("-USD") else "equity"),
                "watch_state": "watched",
                "source_count": 0,
                "rating": "-",
                "quality_score": None,
                "value_signal": "manual",
                "action": "Watch",
                "next_action": "New manual watchlist symbol. Run market refresh for full valuation and momentum context.",
                "freshness": "manual",
            }
        )
    return rows


def _manual_watchlist_by_symbol(panel_data: PanelData) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for row in panel_data.rows("manual_watchlist"):
        symbol = _primary_symbol(row)
        if symbol:
            rows[symbol] = row
    return rows


def _is_active_watchlist_row(row: dict[str, Any]) -> bool:
    return str(row.get("watch_state") or "").lower() in {"owned", "watched"}


def _primary_symbol(row: dict[str, Any]) -> str:
    return str(row.get("symbol") or row.get("ticker") or "").upper()


def _rows_for_symbols(rows: list[dict[str, Any]], symbols: set[str]) -> list[dict[str, Any]]:
    if not symbols:
        return []
    return [row for row in rows if _row_symbols(row) & symbols]


def ticker_payload(panel_data: PanelData, ticker: str) -> dict[str, Any]:
    normalized_ticker = ticker.upper()
    tables = {
        "candidates": _matching_ticker_rows(panel_data.rows("candidates"), normalized_ticker),
        "decision_queue": _matching_ticker_rows(panel_data.rows("decision_queue"), normalized_ticker),
        "discovered_universe": _matching_ticker_rows(panel_data.rows("discovered_universe"), normalized_ticker),
        "universe_screen": _matching_ticker_rows(panel_data.rows("universe_screen"), normalized_ticker),
        "symbol_decision_snapshots": _matching_ticker_rows(panel_data.rows("symbol_decision_snapshots"), normalized_ticker),
        "symbol_decision_snapshot": _matching_ticker_rows(panel_data.rows("symbol_decision_snapshot"), normalized_ticker),
        "opportunities_ranked": _matching_ticker_rows(panel_data.rows("opportunities_ranked"), normalized_ticker),
        "opportunity_sources": _matching_ticker_rows(panel_data.rows("opportunity_sources"), normalized_ticker),
        "feed_signals": _matching_ticker_rows(panel_data.rows("feed_signals"), normalized_ticker),
        "source_consensus": _matching_ticker_rows(panel_data.rows("source_consensus"), normalized_ticker),
        "ticker_source_signals": _matching_ticker_rows(panel_data.rows("ticker_source_signals"), normalized_ticker),
        "ownership_consensus": _matching_ticker_rows(panel_data.rows("ownership_consensus"), normalized_ticker),
        "portfolio": _matching_ticker_rows(panel_data.rows("portfolio"), normalized_ticker),
        "theses": _matching_ticker_rows(panel_data.rows("theses"), normalized_ticker),
        "thesis_monitor": _matching_ticker_rows(panel_data.rows("thesis_monitor"), normalized_ticker),
        "catalysts": _matching_ticker_rows(panel_data.rows("catalysts"), normalized_ticker),
        "signals": _matching_ticker_rows(panel_data.rows("signals"), normalized_ticker),
        "fundamentals": _matching_ticker_rows(panel_data.rows("fundamentals"), normalized_ticker),
        "disclosures": _matching_ticker_rows(panel_data.rows("disclosures"), normalized_ticker),
        "quotes": _matching_ticker_rows(panel_data.rows("quotes"), normalized_ticker),
        "options_expiries": _matching_ticker_rows(panel_data.rows("options_expiries"), normalized_ticker),
        "options_chain": _matching_ticker_rows(panel_data.rows("options_chain"), normalized_ticker),
        "options_payoff_scenarios": _matching_ticker_rows(panel_data.rows("options_payoff_scenarios"), normalized_ticker),
        "options_provider_capabilities": panel_data.rows("options_provider_capabilities"),
        "options_expiry_signals": _matching_ticker_rows(panel_data.rows("options_expiry_signals"), normalized_ticker),
        "options_ticker_signals": _matching_ticker_rows(panel_data.rows("options_ticker_signals"), normalized_ticker),
        "news": _matching_ticker_rows(panel_data.rows("news"), normalized_ticker),
        "tradingview_symbol_search": _matching_ticker_rows(panel_data.rows("tradingview_symbol_search"), normalized_ticker),
        "tradingview_watchlists": _matching_ticker_rows(panel_data.rows("tradingview_watchlists"), normalized_ticker),
        "tradingview_alerts": _matching_ticker_rows(panel_data.rows("tradingview_alerts"), normalized_ticker),
        "tradingview_chart_state": _matching_ticker_rows(panel_data.rows("tradingview_chart_state"), normalized_ticker),
        "sepa": _matching_ticker_rows(panel_data.rows("sepa"), normalized_ticker),
        "liquidity": _matching_ticker_rows(panel_data.rows("liquidity"), normalized_ticker),
        "correlations": _matching_ticker_rows(panel_data.rows("correlations"), normalized_ticker),
        "etf_premiums": _matching_ticker_rows(panel_data.rows("etf_premiums"), normalized_ticker),
        "analyst_estimates": _matching_ticker_rows(panel_data.rows("analyst_estimates"), normalized_ticker),
        "earnings": _matching_ticker_rows(panel_data.rows("earnings"), normalized_ticker),
        "earnings_setups": _matching_ticker_rows(panel_data.rows("earnings_setups"), normalized_ticker),
        "valuations": _matching_ticker_rows(panel_data.rows("valuations"), normalized_ticker),
        "technicals": _matching_ticker_rows(panel_data.rows("technicals"), normalized_ticker),
        "research_packets": _matching_ticker_rows(panel_data.rows("research_packets"), normalized_ticker),
        "exposure_clusters": [
            row
            for row in panel_data.rows("exposure_clusters")
            if normalized_ticker in _row_symbols(row)
        ],
        "correlation_edges": [
            row
            for row in panel_data.rows("correlation_edges")
            if normalized_ticker in {str(row.get("symbol") or "").upper(), str(row.get("peer_symbol") or "").upper()}
        ],
        "portfolio_risk_cards": [
            row
            for row in panel_data.rows("portfolio_risk_cards")
            if normalized_ticker in _row_symbols(row)
        ],
        "review_actions": [
            row
            for row in panel_data.rows("review_actions")
            if normalized_ticker in _row_symbols(row)
        ],
        "memos": _matching_ticker_rows(
            panel_data.rows("ticker_memos") or panel_data.rows("memos"),
            normalized_ticker,
        ),
    }
    _ensure_ticker_dossier_tables(normalized_ticker, tables)
    return {
        "ticker": normalized_ticker,
        "status": status_payload(panel_data),
        "tables": tables,
        "decision_snapshot": (tables["symbol_decision_snapshot"] or tables["symbol_decision_snapshots"] or [None])[0],
        "decision_brief": ticker_decision_brief(normalized_ticker, tables),
        "found": any(tables.values()),
    }


def _ensure_ticker_dossier_tables(symbol: str, tables: dict[str, list[dict[str, Any]]]) -> None:
    """Guarantee every ticker dossier tab has backend-owned context rows."""

    decision = _first_row(tables, "symbol_decision_snapshot", "symbol_decision_snapshots", "decision_queue", "opportunities_ranked", "discovered_universe", "universe_screen")
    universe = _first_row(tables, "universe_screen", "discovered_universe")
    quote = _latest_row(tables.get("quotes") or [], ("observed_at", "as_of", "date"))

    if not any(tables.get(key) for key in ("quotes", "technicals", "sepa", "liquidity", "valuations")):
        tables["quotes"] = [_ticker_price_context(symbol, decision, universe)]
    elif not quote and universe:
        tables.setdefault("quotes", []).append(_ticker_price_context(symbol, decision, universe))

    if not any(tables.get(key) for key in ("fundamentals", "analyst_estimates", "earnings", "earnings_setups", "valuations")):
        tables["fundamentals"] = [_ticker_fundamental_context(symbol, decision, universe)]

    if not tables.get("source_consensus"):
        tables["source_consensus"] = [_ticker_source_context(symbol, decision, universe)]

    if not any(tables.get(key) for key in ("disclosures", "ownership_consensus")):
        tables["ownership_consensus"] = [_ticker_ownership_context(symbol)]

    if not tables.get("feed_signals"):
        tables["feed_signals"] = [_ticker_feed_context(symbol, decision, universe)]

    if not any(tables.get(key) for key in ("theses", "thesis_monitor", "memos", "research_packets")):
        tables["thesis_monitor"] = [_ticker_thesis_context(symbol, decision, universe)]


def _ticker_price_context(symbol: str, decision: dict[str, Any], universe: dict[str, Any]) -> dict[str, Any]:
    price = _number(universe.get("price") or decision.get("latest_quote"))
    return {
        "symbol": symbol,
        "source": "ticker_dossier_coverage",
        "observed_at": universe.get("latest_observed_at") or decision.get("latest_quote_at") or decision.get("as_of"),
        "price": price or None,
        "change_pct": universe.get("change_pct"),
        "freshness_status": decision.get("quote_freshness") or universe.get("freshness") or "not_loaded",
        "summary": "Price context from watchlist, universe, and decision evidence." if price else "No current price is available; treat this as a coverage gap before acting.",
    }


def _ticker_fundamental_context(symbol: str, decision: dict[str, Any], universe: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "form_type": "ticker_dossier_coverage",
        "source": "universe_screen",
        "filing_date": decision.get("as_of") or universe.get("updated_at"),
        "metrics": {
            "market_cap": universe.get("market_cap"),
            "pe_ratio": universe.get("pe_ratio"),
            "forward_pe": universe.get("forward_pe"),
            "forward_pe_source": universe.get("forward_pe_source"),
            "roic": universe.get("roic"),
            "roic_source": universe.get("roic_source"),
            "quality_score": universe.get("quality_score"),
            "value_signal": universe.get("value_signal"),
            "watch_state": universe.get("watch_state"),
        },
        "summary": "Fundamental context synthesized from watchlist evidence because direct fundamentals are not available for this ticker.",
    }


def _ticker_source_context(symbol: str, decision: dict[str, Any], universe: dict[str, Any]) -> dict[str, Any]:
    basis = _object(decision.get("decision_basis"))
    counts = _object(basis.get("source_counts"))
    return {
        "source_name": "Ticker source coverage",
        "source": "decision_read_model",
        "symbol": symbol,
        "content_type": "coverage",
        "items_count": int(sum(_number(value) for value in counts.values())) if counts else int(_number(universe.get("source_count"))),
        "tickers_count": 1,
        "bullish_symbols": [symbol] if not _is_no_trade_action(decision.get("action_grade")) else [],
        "bearish_symbols": [symbol] if _is_no_trade_action(decision.get("action_grade")) else [],
        "ticker_history": [{"symbols": [symbol], "title": basis.get("summary") or decision.get("source_cluster") or "No per-source history loaded.", "date": decision.get("as_of")}],
        "recommendation": "coverage_gap" if not counts else "loaded",
    }


def _ticker_ownership_context(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "source_type": "coverage_gap",
        "source": "ownership_consensus",
        "holders": 0,
        "net_buys": 0,
        "net_sells": 0,
        "total_value": 0,
        "summary": "No mapped disclosure or ownership consensus row is loaded for this ticker.",
    }


def _ticker_feed_context(symbol: str, decision: dict[str, Any], universe: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"coverage:{symbol}",
        "symbol": symbol,
        "symbols": [symbol],
        "source": "ticker_dossier_coverage",
        "source_type": "coverage",
        "date": decision.get("as_of") or universe.get("updated_at"),
        "title": f"{symbol} coverage row",
        "thesis": _brief_summary(symbol, decision, _object(decision.get("decision_basis")), _text_list(decision.get("blocking_gates"))),
        "antithesis": decision.get("invalidation") or "No ticker-specific countercase has been promoted yet.",
        "portfolio_relevance": _text(_object(decision.get("portfolio_impact")).get("summary")) or "Review against Joe's portfolio/watchlist before action.",
        "next_action": decision.get("catalyst_window") or "Open the thesis tab and fill any missing evidence families.",
        "freshness": decision.get("freshness_status") or universe.get("freshness") or "not_loaded",
    }


def _ticker_thesis_context(symbol: str, decision: dict[str, Any], universe: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "source": "ticker_dossier_coverage",
        "status": universe.get("watch_state") or "candidate",
        "thesis": _text_join(decision.get("inclusion_reasons")) or "No explicit thesis row is loaded yet.",
        "why_owned_watched": universe.get("portfolio_relevance") or "Ticker is present in the investment universe.",
        "invalidation": decision.get("invalidation") or "Define the countercase before changing exposure.",
        "needs_review": True,
        "review_reason": "Needs review because this dossier has coverage context but no stored thesis.",
        "evidence_links": [],
        "last_reviewed": decision.get("as_of") or universe.get("updated_at"),
    }


def ticker_decision_brief(symbol: str, tables: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Build a trader-readable per-symbol decision brief from existing ticker rows."""

    decision_row = _first_row(tables, "symbol_decision_snapshot", "symbol_decision_snapshots", "decision_queue", "opportunities_ranked", "candidates")
    quote_row = _latest_row(tables.get("quotes") or [], ("observed_at", "as_of", "date"))
    snapshot = _object(decision_row.get("snapshot"))
    basis = _object(decision_row.get("decision_basis"))
    canonical_quote = _canonical_quote(symbol, quote_row, decision_row, snapshot)
    latest_price = _number(canonical_quote.get("price"))
    technical = _latest_row(tables.get("technicals") or [], ("date", "as_of"))
    sepa = _latest_row(tables.get("sepa") or [], ("as_of", "date"))
    liquidity = _latest_row(tables.get("liquidity") or [], ("as_of", "date"))
    valuation_rows = sorted(tables.get("valuations") or [], key=lambda row: _number(row.get("upside_pct")), reverse=True)
    best_valuation = valuation_rows[0] if valuation_rows else {}
    earnings_setup = _latest_row(tables.get("earnings_setups") or [], ("event_date", "as_of"))
    option_rows = tables.get("options_payoff_scenarios") or []
    live_option_rows = [row for row in option_rows if not _is_option_expired(row)]
    expired_option_rows = [row for row in option_rows if _is_option_expired(row)]
    best_option = _best_option(live_option_rows)
    portfolio_row = _first_row(tables, "portfolio")
    research_packet = _latest_row(tables.get("research_packets") or [], ("created_at", "as_of"))
    action = _text(decision_row.get("action_grade") or decision_row.get("decision") or "Watch")
    blockers = _text_list(decision_row.get("blocking_gates"))
    if _is_no_trade_action(action) and "decision_reject" not in blockers:
        blockers = [*blockers, "decision_reject"]
    if expired_option_rows and not live_option_rows:
        blockers = list(dict.fromkeys([*blockers, "expired_options_context"]))
    missing_families = _missing_families(tables)

    stance = _stance(action, blockers, best_valuation, research_packet)
    setup = {
        "stance": stance,
        "timeframe": _timeframe(earnings_setup, best_option),
        "catalyst": _catalyst(decision_row, snapshot, earnings_setup, tables.get("catalysts") or []),
        "entry_zone": _entry_zone(latest_price, technical, sepa, best_valuation, blockers),
        "invalidation_level": _invalidation_level(technical, sepa),
        "target_range": _target_range(best_valuation, latest_price, blockers),
        "risk_reward": _risk_reward(latest_price, technical, best_valuation),
        "review_date": _review_date(earnings_setup, research_packet),
    }

    return {
        "symbol": symbol,
        "canonical_quote": canonical_quote,
        "verdict": {
            "action": action or "Watch",
            "freshness": decision_row.get("freshness_status") or decision_row.get("overall_decision_freshness") or "not_loaded",
            "confidence": _confidence(decision_row, basis),
            "summary": _brief_summary(symbol, decision_row, basis, blockers),
            "blockers": blockers,
            "blocker_labels": [_readable_gate(blocker) for blocker in blockers],
            "blocker_tasks": _blocker_tasks(blockers, missing_families, tables),
            "next_action": _next_action(decision_row, research_packet, missing_families, blockers),
        },
        "setup": setup,
        "risk_plan": {
            "max_sizing": _max_sizing(liquidity, portfolio_row, blockers, missing_families),
            "max_loss": "Not applicable while decision grade is Reject." if "decision_reject" in blockers else "Not applicable while blockers are active." if blockers else _max_loss(best_option),
            "liquidity_ceiling": _liquidity_ceiling(liquidity),
            "portfolio_overlap": _portfolio_overlap(portfolio_row, tables.get("correlations") or []),
            "invalidation": decision_row.get("invalidation") or snapshot.get("invalidation") or _text_join(research_packet.get("invalidation")) or "No ticker-specific invalidation row is loaded in the current decision tables.",
        },
        "portfolio_fit": {
            "owned": bool(portfolio_row) or bool(_object(snapshot.get("portfolio_impact")).get("owned")),
            "current_exposure": _portfolio_exposure(portfolio_row, snapshot),
            "theme_concentration": "AI infrastructure exposure; compare against NVDA, QQQ, SOX, and any current semiconductor holdings.",
            "duplicates_risk": bool(portfolio_row),
        },
        "evidence_for": _evidence_for(tables, technical, sepa, liquidity, best_valuation, earnings_setup, best_option, research_packet),
        "evidence_against": _evidence_against(tables, technical, sepa, earnings_setup, best_valuation, blockers, research_packet, expired_option_rows),
        "unknowns": _unknowns(tables, missing_families),
        "changed_since_last_review": _changed_since_last_review(canonical_quote, decision_row, technical, earnings_setup, blockers, tables),
        "source_health_by_family": _source_health_by_family(tables),
        "chart_context": _chart_context(latest_price, technical, sepa),
        "options_context": _options_context(best_option, option_rows, setup),
        "tab_summaries": _ticker_tab_summaries(tables, setup),
    }


def _first_row(tables: dict[str, list[dict[str, Any]]], *keys: str) -> dict[str, Any]:
    for key in keys:
        rows = tables.get(key) or []
        if rows:
            return rows[0]
    return {}


def _latest_row(rows: list[dict[str, Any]], date_keys: tuple[str, ...]) -> dict[str, Any]:
    if not rows:
        return {}
    return max(rows, key=lambda row: max((_timestamp(row.get(key)) for key in date_keys), default=0.0))


def _timestamp(value: Any) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time()).timestamp()
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _number(value: Any, fallback: float = 0.0) -> float:
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value.replace("$", "").replace(",", "").replace("%", ""))
        except ValueError:
            return fallback
    return fallback


def _text(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    return json.dumps(jsonable(value), sort_keys=True)


def _text_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [_text(item) for item in value if _text(item)]
    if isinstance(value, tuple):
        return [_text(item) for item in value if _text(item)]
    if isinstance(value, str) and value.strip():
        parsed = _parsed_json(value)
        if isinstance(parsed, list):
            return [_text(item) for item in parsed if _text(item)]
        return [item.strip() for item in value.replace("|", ";").split(";") if item.strip()]
    return []


def _parsed_json(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _text_join(value: Any) -> str:
    items = _text_list(value)
    if items:
        return " ".join(items)
    return _text(value)


def _fmt_money(value: float) -> str:
    if not value:
        return "-"
    return f"${value:,.2f}" if abs(value) < 1000 else f"${value:,.0f}"


def _fmt_pct(value: float) -> str:
    return f"{value:+.2f}%"


def _canonical_quote(symbol: str, quote: dict[str, Any], decision: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    if quote:
        source = _text(quote.get("source")) or "quote"
        quote_type = "prior_close" if source.startswith("previous_close") or source.startswith("closing") else "market_quote"
        return {
            "symbol": symbol,
            "price": _number(quote.get("price") or quote.get("close") or quote.get("last")),
            "change_pct": _number(quote.get("change_pct") or quote.get("percent_change") or quote.get("change"), 0.0),
            "observed_at": quote.get("observed_at") or quote.get("as_of") or quote.get("date"),
            "source": source,
            "type": quote_type,
            "label": "Prior close" if quote_type == "prior_close" else "Market quote",
        }
    price = _number(decision.get("latest_quote") or snapshot.get("latest_quote"))
    return {
        "symbol": symbol,
        "price": price,
        "change_pct": None,
        "observed_at": decision.get("latest_quote_at") or snapshot.get("latest_quote_at") or decision.get("as_of"),
        "source": "decision_snapshot",
        "type": "decision_snapshot_quote",
        "label": "Decision snapshot quote",
    }


def _is_no_trade_action(action: Any) -> bool:
    normalized = _text(action).lower()
    return any(term in normalized for term in ("reject", "avoid", "pass", "no trade"))


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def _is_option_expired(row: dict[str, Any], today: date | None = None) -> bool:
    expiry = _parse_date(row.get("expiry") or row.get("expiration"))
    if expiry is not None:
        return expiry < (today or date.today())
    dte = _number(row.get("dte") or row.get("days_to_expiry"), 0.0)
    return dte < 0


def _best_option(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {}
    finite_loss = [row for row in rows if _number(row.get("max_loss")) < 0]
    return min(finite_loss or rows, key=lambda row: abs(_number(row.get("max_loss"), 10**12)))


GATE_LABELS = {
    "chart_extended_without_thesis": "Price is extended and no current thesis supports chasing.",
    "decision_reject": "Current decision grade is Reject; do not add exposure until the score and setup change.",
    "expired_options_context": "Options context is expired; refresh the chain before using options for risk.",
    "source_thin": "Evidence is source-thin.",
    "evidence_thin": "Primary evidence count is below the decision threshold.",
    "stale_data": "Some source data is stale.",
    "stale_intraday_quote": "Intraday quote is stale; refresh quotes before making a decision.",
    "stale_quote": "Quote is stale; refresh quotes before making a decision.",
    "missing_intraday_quote": "No current intraday quote row is loaded for this ticker.",
    "missing_daily_analysis": "Daily analysis rows are not loaded for this ticker.",
    "liquidity_unknown": "No current liquidity row is loaded for this ticker.",
    "missing_thesis": "No current ticker thesis is loaded.",
    "missing_portfolio_context": "Portfolio context is not loaded for this ticker.",
}


def _readable_gate(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return ""
    return GATE_LABELS.get(raw, raw.replace("_", " ").replace("-", " ").capitalize())


def _gate_sentence(blockers: list[str]) -> str:
    labels = [_readable_gate(blocker).rstrip(".") for blocker in blockers if _readable_gate(blocker)]
    return "; ".join(labels)


def _blocker_tasks(blockers: list[str], missing: list[str], tables: dict[str, list[dict[str, Any]]]) -> list[dict[str, str]]:
    tasks: list[dict[str, str]] = []

    def add(key: str, label: str, action: str, detail: str, severity: str = "warn") -> None:
        if not any(task["key"] == key for task in tasks):
            tasks.append({"key": key, "label": label, "action": action, "detail": detail, "severity": severity})

    for blocker in blockers:
        if blocker == "chart_extended_without_thesis":
            add("thesis", "Thesis support missing", "Add thesis or avoid chase", _readable_gate(blocker), "bad")
        elif blocker == "decision_reject":
            add("decision_reject", "Decision is Reject", "No new exposure", _readable_gate(blocker), "bad")
        elif blocker == "expired_options_context":
            add("options", "Refresh option chain", "Refresh options", _readable_gate(blocker), "bad")
        else:
            add(blocker, _readable_gate(blocker), "Review gate", _text(blocker), "warn")

    missing_actions = {
        "thesis": ("Optional thesis missing", "Add thesis if conviction work continues"),
    }
    for family in missing:
        if family in missing_actions:
            label, action = missing_actions[family]
            add(family.lower().replace(" ", "_"), label, action, f"No {family} row is loaded for this ticker.")

    if tables.get("research_packets") and not tables.get("memos"):
        add("memo", "Decision memo not promoted", "Promote packet to memo", "A research packet exists, but no ticker memo row is loaded.", "info")
    return tasks


def _missing_families(tables: dict[str, list[dict[str, Any]]]) -> list[str]:
    checks = {
        "quote": ["quotes"],
        "thesis": ["theses", "memos"],
        "news": ["news"],
        "filings": ["disclosures"],
    }
    return [label for label, keys in checks.items() if not any(tables.get(key) for key in keys)]


def _stance(action: Any, blockers: list[str], valuation: dict[str, Any], packet: dict[str, Any]) -> str:
    normalized = _text(action).lower()
    if _is_no_trade_action(action):
        return "No new exposure; current decision grade is Reject."
    if blockers:
        return "Do not initiate; blocked pending research or setup confirmation."
    if "act" in normalized or "buy" in normalized:
        return "Actionable long candidate after risk and portfolio checks."
    if "reject" in normalized:
        return "Pass for now; monitor only if the setup resets or evidence improves."
    if _number(valuation.get("upside_pct")) < 0:
        return "Monitor/avoid chase; valuation support is negative at the current price."
    decision = _text(packet.get("decision")).lower()
    if decision:
        return f"Research stance from packet: {decision}."
    return "Watchlist candidate; needs stronger source-backed thesis before action."


def _timeframe(earnings_setup: dict[str, Any], option: dict[str, Any]) -> str:
    expiry = option.get("expiry")
    event_date = earnings_setup.get("event_date")
    if expiry:
        return f"Options setup through {expiry}; reassess before expiry."
    if event_date:
        return f"Swing/research window into {event_date} earnings."
    return "Swing/research timeframe; review at the next weekly decision cycle."


def _catalyst(decision: dict[str, Any], snapshot: dict[str, Any], earnings_setup: dict[str, Any], catalysts: list[dict[str, Any]]) -> str:
    catalyst = _text(_object(decision.get("decision_basis")).get("catalyst"))
    if catalyst:
        return catalyst
    if snapshot.get("catalyst_window"):
        return _text(snapshot["catalyst_window"])
    if earnings_setup.get("event_date"):
        return f"{earnings_setup['event_date']}: earnings"
    if catalysts:
        event = catalysts[0]
        return " · ".join(item for item in [_text(event.get("event_date") or event.get("start_at")), _text(event.get("event") or event.get("title"))] if item)
    return "No near-term catalyst row is loaded for this ticker."


def _entry_zone(price: float, technical: dict[str, Any], sepa: dict[str, Any], valuation: dict[str, Any], blockers: list[str]) -> str:
    ma20 = _number(technical.get("ma20") or _object(technical.get("features")).get("ma20"))
    ma50 = _number(technical.get("ma50") or _object(technical.get("features")).get("ma50"))
    fair = _number(valuation.get("fair_value"))
    if "decision_reject" in blockers:
        return "No entry while the decision grade is Reject."
    if blockers:
        return "No chase entry while blockers are active."
    if ma20 and price > ma20 * 1.1:
        return f"Prefer pullback toward 20d MA near {_fmt_money(ma20)} before sizing."
    if fair and price > fair:
        return f"Do not pay above fair-value support near {_fmt_money(fair)} without a fresh thesis."
    if ma50:
        return f"Initial entry zone above rising 50d MA near {_fmt_money(ma50)}."
    return _text(sepa.get("verdict")) or "Entry zone not defined by current source rows."


def _invalidation_level(technical: dict[str, Any], sepa: dict[str, Any]) -> str:
    ma50 = _number(technical.get("ma50") or _object(technical.get("features")).get("ma50"))
    ma200 = _number(technical.get("ma200") or _object(technical.get("features")).get("ma200"))
    if ma50:
        return f"Close below 50d MA near {_fmt_money(ma50)} or SEPA stage deterioration."
    if ma200:
        return f"Close below 200d MA near {_fmt_money(ma200)}."
    return _text(sepa.get("stage")) or "No technical invalidation level loaded."


def _target_range(valuation: dict[str, Any], price: float, blockers: list[str]) -> str:
    fair = _number(valuation.get("fair_value"))
    if fair:
        implied = ((fair / price) - 1) * 100 if price else _number(valuation.get("upside_pct"))
        if "decision_reject" in blockers:
            return f"Model fair value is {_fmt_money(fair)} ({_fmt_pct(implied)}), but the decision grade is Reject; no active target."
        if price and fair < price:
            return f"{_fmt_money(fair)} fair value ({_fmt_pct(implied)} vs canonical quote); no upside at current price."
        return f"{_fmt_money(fair)} fair value ({_fmt_pct(implied)} vs canonical quote)." if price else _fmt_money(fair)
    return "No valuation target range loaded."


def _risk_reward(price: float, technical: dict[str, Any], valuation: dict[str, Any]) -> str:
    fair = _number(valuation.get("fair_value"))
    stop = _number(technical.get("ma50") or _object(technical.get("features")).get("ma50"))
    if price and fair and stop and price != stop:
        reward = fair - price
        risk = price - stop
        if reward <= 0:
            return "No long setup: fair value is below canonical quote."
        if risk > 0:
            return f"{reward / risk:.2f}:1 using fair value vs 50d MA."
    return "Not computable from current target/stop rows."


def _review_date(earnings_setup: dict[str, Any], packet: dict[str, Any]) -> str:
    entry_plan = _object(packet.get("entry_plan"))
    if entry_plan.get("first_review_date"):
        return _text(entry_plan["first_review_date"])
    if earnings_setup.get("event_date"):
        return f"Before {earnings_setup['event_date']} earnings."
    return "Next weekly review."


def _confidence(decision: dict[str, Any], basis: dict[str, Any]) -> int:
    score = _number(decision.get("action_score") or basis.get("action_score") or decision.get("decision_score") or basis.get("decision_score"))
    if score:
        return max(0, min(100, round(score)))
    source_count = _number(basis.get("independent_source_count") or basis.get("source_count"))
    evidence = _number(basis.get("evidence_count"))
    return max(0, min(100, round(30 + min(source_count, 10) * 4 + min(evidence, 5) * 6)))


def _brief_summary(symbol: str, decision: dict[str, Any], basis: dict[str, Any], blockers: list[str]) -> str:
    if blockers:
        return f"{symbol} is gated: {_gate_sentence(blockers)}."
    summary = _text(basis.get("summary") or decision.get("decision_basis"))
    if summary:
        return summary
    source_count = _number(basis.get("source_count") or basis.get("independent_source_count"))
    evidence_count = _number(basis.get("evidence_count") or basis.get("primary_evidence_count"))
    if source_count or evidence_count:
        return f"{symbol} has {source_count:.0f} source rows and {evidence_count:.0f} primary evidence items; no promoted memo summary is loaded."
    return f"{symbol} has no current decision summary row in the loaded ticker tables."


def _next_action(decision: dict[str, Any], packet: dict[str, Any], missing: list[str], blockers: list[str]) -> str:
    if "decision_reject" in blockers:
        entry_plan = _object(packet.get("entry_plan"))
        return _text(entry_plan.get("ideal_entry")) or "Wait for the score, setup, or primary-source catalyst to improve before reconsidering."
    if blockers:
        if any("thesis" in blocker.lower() for blocker in blockers):
            return "Avoid chasing the extended chart unless an explicit thesis supports the trade."
        return f"Load or refresh gated source rows before action: {_gate_sentence(blockers)}."
    entry_plan = _object(packet.get("entry_plan"))
    if entry_plan.get("ideal_entry"):
        return _text(entry_plan["ideal_entry"])
    if missing:
        return f"Load ticker rows before action: {', '.join(missing[:3])}."
    return "Review entry, invalidation, sizing, and portfolio overlap against the rows shown in this ticker dossier."


def _max_sizing(liquidity: dict[str, Any], portfolio: dict[str, Any], blockers: list[str], missing: list[str]) -> str:
    if "decision_reject" in blockers:
        return "No new exposure while decision grade remains Reject."
    if blockers:
        return "No new exposure until evidence gates clear."
    grade = _text(liquidity.get("grade")).lower()
    if portfolio:
        return "Add only if portfolio concentration remains within existing risk limits."
    if "very_high" in grade or "very high" in grade:
        return "Liquid enough for normal discretionary sizing, subject to thesis and portfolio caps."
    if "high" in grade:
        return "Size modestly; verify spread and slippage before entry."
    return "Small only; liquidity row does not support full-size exposure."


def _max_loss(option: dict[str, Any]) -> str:
    loss = _number(option.get("max_loss"))
    if loss < 0:
        return _fmt_money(abs(loss))
    return "No bounded-loss option scenario selected."


def _liquidity_ceiling(liquidity: dict[str, Any]) -> str:
    adv = _number(liquidity.get("avg_dollar_volume"))
    impact = _number(liquidity.get("impact_1pct_adv_bps"))
    if adv:
        return f"{_fmt_money(adv)} ADV; 1% ADV modeled impact {impact:.1f} bps." if impact else f"{_fmt_money(adv)} ADV."
    return "No liquidity ceiling row loaded."


def _portfolio_overlap(portfolio: dict[str, Any], correlations: list[dict[str, Any]]) -> str:
    if portfolio:
        return "Already owned; treat as add/trim decision."
    peers = [_text(row.get("peer_symbol") or row.get("benchmark") or row.get("related_symbol")) for row in correlations[:3]]
    peers = [peer for peer in peers if peer]
    return f"Unowned; compare correlation against {', '.join(peers)}." if peers else "Unowned; correlation peer rows are limited."


def _portfolio_exposure(portfolio: dict[str, Any], snapshot: dict[str, Any]) -> str:
    if portfolio:
        weight = _number(portfolio.get("weight") or portfolio.get("portfolio_weight"))
        value = _number(portfolio.get("market_value") or portfolio.get("value"))
        if weight:
            return f"{weight:.2f}% current weight."
        if value:
            return f"{_fmt_money(value)} current market value."
        return "Owned."
    impact = _object(snapshot.get("portfolio_impact"))
    return "Owned." if impact.get("owned") else "Unowned."


def _evidence_for(
    tables: dict[str, list[dict[str, Any]]],
    technical: dict[str, Any],
    sepa: dict[str, Any],
    liquidity: dict[str, Any],
    valuation: dict[str, Any],
    earnings: dict[str, Any],
    option: dict[str, Any],
    packet: dict[str, Any],
) -> list[str]:
    items: list[str] = []
    score = _number(technical.get("technical_score") or _object(technical.get("features")).get("technical_score"))
    if score >= 60:
        items.append(f"Technical score is {score:.0f}; 20d return {_fmt_pct(_number(technical.get('return_20d')) * 100)}.")
    sepa_text = _text(sepa.get("verdict") or sepa.get("stage")).lower()
    if sepa and any(term in sepa_text for term in ("strong", "constructive", "stage_2", "advance")):
        items.append(f"SEPA setup is {_text(sepa.get('verdict') or sepa.get('stage'))}.")
    if _text(liquidity.get("grade")):
        items.append(f"Liquidity is {_text(liquidity.get('grade')).replace('_', ' ')} with {_liquidity_ceiling(liquidity)}")
    if _number(valuation.get("upside_pct")) > 0:
        items.append(f"Best valuation row shows {_fmt_pct(_number(valuation.get('upside_pct')))} modeled upside.")
    earnings_score = _number(earnings.get("score"))
    earnings_verdict = _text(earnings.get("verdict")).lower()
    if earnings and (earnings_score >= 60 or "positive" in earnings_verdict):
        items.append(f"Earnings setup is {_text(earnings.get('verdict')) or 'loaded'} with score {_number(earnings.get('score')):.0f}.")
    if option:
        items.append(f"Options scenario loaded: {_text(option.get('strategy_type')).replace('_', ' ')}.")
    for note in _text_list(packet.get("why_now"))[:2]:
        lowered = note.lower()
        if any(term in lowered for term in ("not yet strong", "not strong", "insufficient", "weak evidence")):
            continue
        items.append(note)
    return items or ["No positive source-backed evidence rows are loaded."]


def _evidence_against(
    tables: dict[str, list[dict[str, Any]]],
    technical: dict[str, Any],
    sepa: dict[str, Any],
    earnings: dict[str, Any],
    valuation: dict[str, Any],
    blockers: list[str],
    packet: dict[str, Any],
    expired_options: list[dict[str, Any]],
) -> list[str]:
    items = [_readable_gate(blocker) for blocker in blockers]
    score = _number(technical.get("technical_score") or _object(technical.get("features")).get("technical_score"))
    if technical and score < 50:
        items.append(f"Technical score is weak at {score:.0f}; 20d return {_fmt_pct(_number(technical.get('return_20d')) * 100)}.")
    sepa_text = _text(sepa.get("verdict") or sepa.get("stage"))
    if sepa and any(term in sepa_text.lower() for term in ("pass", "risk", "declin", "stage_4")):
        items.append(f"SEPA setup is {sepa_text}; do not treat the chart as constructive.")
    earnings_score = _number(earnings.get("score"))
    earnings_verdict = _text(earnings.get("verdict"))
    if earnings and (earnings_score < 50 or "risk" in earnings_verdict.lower()):
        items.append(f"Earnings setup is {earnings_verdict or 'risk'} with score {earnings_score:.0f}.")
    upside = _number(valuation.get("upside_pct"))
    if valuation and upside < 0:
        items.append(f"Best valuation row is below price: {_fmt_pct(upside)} upside.")
    if expired_options:
        expiries = sorted({_text(row.get("expiry") or row.get("expiration")) for row in expired_options if _text(row.get("expiry") or row.get("expiration"))})
        expiry_text = ", ".join(expiries[:3])
        items.append(f"Options scenarios are expired{f' ({expiry_text})' if expiry_text else ''}; do not use them for live max loss, breakeven, or trade setup.")
    drawdown = _number(technical.get("drawdown_from_high") or _object(technical.get("features")).get("drawdown_from_high"))
    if drawdown > -0.1 and technical:
        items.append("Price is near recent highs; avoid chasing an extended move without a thesis.")
    items.extend(_text_list(packet.get("bear_case"))[:2])
    if not tables.get("theses") and not tables.get("memos"):
        items.append("No ticker-specific thesis or memo row is loaded in the current local research tables.")
    return items or ["Loaded rows do not contain a negative evidence item for this ticker."]


def _unknowns(tables: dict[str, list[dict[str, Any]]], missing: list[str]) -> list[str]:
    unknowns = []
    for family in missing:
        if family == "thesis":
            unknowns.append("Optional thesis is not loaded; rely on source evidence and deterministic analysis until conviction work is added.")
        elif family == "news":
            unknowns.append("No ticker-specific news row is loaded in the current local news table.")
        elif family == "filings":
            unknowns.append("No tracked disclosure row is loaded for this ticker in the current local filing set.")
        else:
            unknowns.append(f"No current {family} row is loaded for this ticker.")
    if not tables.get("news"):
        unknowns.append("No ticker-specific news row is loaded in the current local news table.")
    return list(dict.fromkeys(unknowns)) or ["Loaded ticker tables cover the required quote, setup, risk, and evidence fields for this dossier."]


def _changed_since_last_review(
    quote: dict[str, Any],
    decision: dict[str, Any],
    technical: dict[str, Any],
    earnings: dict[str, Any],
    blockers: list[str],
    tables: dict[str, list[dict[str, Any]]],
) -> list[str]:
    changes = []
    if quote.get("change_pct") is not None:
        changes.append(f"{quote.get('label')}: {_fmt_money(_number(quote.get('price')))} ({_fmt_pct(_number(quote.get('change_pct')))}).")
    if decision.get("as_of"):
        changes.append(f"Decision row refreshed {decision['as_of']} with action {decision.get('action_grade') or 'not recorded'}.")
    if technical:
        changes.append(f"Technical row shows 20d return {_fmt_pct(_number(technical.get('return_20d')) * 100)}.")
    if earnings:
        changes.append(f"Earnings setup score {_number(earnings.get('score')):.0f}; next event {earnings.get('event_date') or 'not loaded'}.")
    if blockers:
        changes.append(f"Active blocker set: {_gate_sentence(blockers)}.")
    loaded = sum(1 for rows in tables.values() if rows)
    changes.append(f"{loaded} ticker-specific API table families currently loaded.")
    return changes


def _source_health_by_family(tables: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    families = {
        "quote": ["quotes"],
        "technical": ["technicals", "sepa"],
        "valuation": ["valuations"],
        "earnings": ["earnings", "earnings_setups", "analyst_estimates"],
        "options": ["options_expiries", "options_chain", "options_payoff_scenarios", "options_expiry_signals", "options_ticker_signals"],
        "thesis": ["theses", "memos"],
        "research_packet": ["research_packets"],
        "news": ["news"],
        "filings": ["disclosures"],
        "portfolio": ["portfolio"],
        "tradingview": ["tradingview_symbol_search", "tradingview_watchlists", "tradingview_alerts", "tradingview_chart_state"],
    }
    health = {}
    for family, keys in families.items():
        row_count = sum(len(tables.get(key) or []) for key in keys)
        status = "live" if row_count else "missing"
        if family == "options" and row_count:
            option_rows = tables.get("options_payoff_scenarios") or []
            if option_rows and all(_is_option_expired(row) for row in option_rows):
                status = "expired"
        health[family] = {"status": status, "rows": row_count}
    return health


def _chart_context(price: float, technical: dict[str, Any], sepa: dict[str, Any]) -> dict[str, Any]:
    features = _object(technical.get("features"))
    ma20 = _number(technical.get("ma20") or features.get("ma20"))
    ma50 = _number(technical.get("ma50") or features.get("ma50"))
    ma200 = _number(technical.get("ma200") or features.get("ma200"))
    high = _number(_object(sepa.get("metrics")).get("high_52w"))
    low = _number(_object(sepa.get("metrics")).get("low_52w"))
    extension = (price / ma20 - 1) * 100 if price and ma20 else 0
    return {
        "ma20": ma20,
        "ma50": ma50,
        "ma200": ma200,
        "drawdown_from_high": _number(technical.get("drawdown_from_high") or features.get("drawdown_from_high")),
        "return_20d": _number(technical.get("return_20d") or features.get("return_20d")),
        "return_60d": _number(technical.get("return_60d") or features.get("return_60d")),
        "high_52w": high,
        "low_52w": low,
        "extension_warning": f"{extension:.1f}% above 20d MA" if extension > 10 else "",
        "support": ma50 or ma20,
        "resistance": high,
    }


def _options_context(option: dict[str, Any], option_rows: list[dict[str, Any]], setup: dict[str, Any]) -> dict[str, Any]:
    expired_count = sum(1 for row in option_rows if _is_option_expired(row))
    live_count = len(option_rows) - expired_count
    if not option:
        if expired_count:
            return {
                "status": "expired",
                "summary": "All options scenarios are expired; refresh the option chain before using options.",
                "scenario_count": len(option_rows),
                "live_scenario_count": 0,
                "expired_scenario_count": expired_count,
            }
        return {"status": "missing", "summary": "No options scenario row loaded.", "scenario_count": 0, "live_scenario_count": 0}
    spot = _number(option.get("spot"))
    breakevens = option.get("breakevens") if isinstance(option.get("breakevens"), list) else []
    first_breakeven = _number(breakevens[0]) if breakevens else 0.0
    move_to_breakeven = ((first_breakeven / spot) - 1) * 100 if spot and first_breakeven else 0.0
    return {
        "status": "live",
        "summary": f"{_text(option.get('strategy_type')).replace('_', ' ')} expires {option.get('expiry')}; breakeven move {_fmt_pct(move_to_breakeven)}.",
        "scenario_count": len(option_rows),
        "live_scenario_count": live_count,
        "expired_scenario_count": expired_count,
        "iv": _number(option.get("iv")),
        "dte": _number(option.get("dte")),
        "breakeven": first_breakeven,
        "max_loss": _max_loss(option),
        "event_fit": "Check expiry against catalyst window: " + _text(setup.get("catalyst")),
    }


def _ticker_tab_summaries(tables: dict[str, list[dict[str, Any]]], setup: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    fundamentals = _first_row(tables, "fundamentals")
    estimates = _first_row(tables, "analyst_estimates")
    financial_valuation = _first_row(tables, "valuations")
    earnings = _first_row(tables, "earnings_setups", "earnings")
    memo = _first_row(tables, "research_packets", "memos", "theses")
    option_rows = tables.get("options_payoff_scenarios") or []
    expired_options = sum(1 for row in option_rows if _is_option_expired(row))
    live_options = len(option_rows) - expired_options
    return {
        "Evidence Stack": [
            {"label": "For", "value": str(len(tables.get("opportunity_sources") or [])), "caption": "source rows supporting current setup"},
            {"label": "Against", "value": _text(setup.get("stance")), "caption": "stance from gates and valuation"},
            {"label": "Open Inputs", "value": str(len(_missing_families(tables))), "caption": "source families not loaded for this ticker"},
        ],
        "Fundamentals": [
            {"label": "Latest Filing", "value": _text(fundamentals.get("form_type")) or "Not loaded", "caption": _text(fundamentals.get("filing_date") or fundamentals.get("period_end")) or "No SEC company-facts row"},
            {"label": "Revenue", "value": _fmt_money(_number(_object(fundamentals.get("metrics")).get("revenue"))), "caption": "SEC company facts"},
            {"label": "Net Margin", "value": _fmt_pct(_number(_object(fundamentals.get("metrics")).get("net_margin")) * 100), "caption": "latest annual period"},
        ],
        "Estimates": [
            {"label": "Earnings Setup", "value": _text(earnings.get("verdict")) or "Not loaded", "caption": f"score {_number(earnings.get('score')):.0f}" if earnings else "No earnings setup row"},
            {"label": "Event", "value": _text(earnings.get("event_date")) or "Not loaded", "caption": "next earnings/event row"},
            {"label": "Estimate Snapshot", "value": "Loaded" if estimates else "Not loaded", "caption": _text(estimates.get("as_of")) or "No analyst estimate row"},
        ],
        "Financials": [
            {"label": "Best Fair Value", "value": _fmt_money(_number(financial_valuation.get("fair_value"))), "caption": _text(financial_valuation.get("method"))},
            {"label": "Modeled Upside", "value": _fmt_pct(_number(financial_valuation.get("upside_pct"))), "caption": "relative to model quote"},
            {"label": "Confidence", "value": _text(_object(financial_valuation.get("diagnostics")).get("confidence")) or "Not scored", "caption": _text(_object(financial_valuation.get("diagnostics")).get("note")) or "No valuation diagnostics row"},
        ],
        "Options": [
            {"label": "Live Scenarios", "value": str(live_options), "caption": "usable current option setups"},
            {"label": "Expired Scenarios", "value": str(expired_options), "caption": "hidden from live risk plan"},
            {"label": "Status", "value": "Expired" if option_rows and not live_options else "Loaded" if live_options else "Not loaded", "caption": "option chain usability"},
        ],
        "TradingView": [
            {"label": "Personal Context", "value": "Loaded" if any(tables.get(key) for key in ("tradingview_symbol_search", "tradingview_watchlists", "tradingview_alerts", "tradingview_chart_state")) else "Not loaded", "caption": "watchlists, alerts, search, chart state"},
            {"label": "Chart", "value": "Embedded", "caption": "daily technical chart available on overview"},
        ],
        "News": [
            {"label": "Ticker News", "value": str(len(tables.get("news") or [])), "caption": "ticker-specific news rows"},
            {"label": "Catalysts", "value": str(len(tables.get("catalysts") or [])), "caption": _text(setup.get("catalyst"))},
        ],
        "Filings": [
            {"label": "Disclosure Rows", "value": str(len(tables.get("disclosures") or [])), "caption": "tracked filings/disclosures"},
            {"label": "Portfolio Rows", "value": str(len(tables.get("portfolio") or [])), "caption": "current position context"},
        ],
        "Memos": [
            {"label": "Decision Memo", "value": _text(memo.get("decision") or memo.get("conviction")) or "Not loaded", "caption": _text_join(memo.get("why_now")) or "No ticker-specific memo row"},
            {"label": "Entry Plan", "value": _text(_object(memo.get("entry_plan")).get("initial_weight")) or "Review required", "caption": _text(_object(memo.get("entry_plan")).get("ideal_entry"))},
        ],
    }


def settings_payload(config: dict[str, Any], panel_data: PanelData) -> dict[str, Any]:
    return {
        "status": status_payload(panel_data),
        "config": jsonable(config),
        "integration": {
            "core_modules": list(CORE_MODULE_CANDIDATES),
            "helper_names": list(CORE_HELPER_CANDIDATES),
            "duckdb_path": config.get("database", {}).get("duckdb_path"),
            "arco_raw_dir": config.get("arco", {}).get("raw_dir"),
            "birdclaw_command": config.get("birdclaw", {}).get("command") or "Not configured",
        },
    }


def normalize_rows(table: Any) -> list[dict[str, Any]]:
    """Convert common table shapes into JSON-ready row dictionaries."""

    if table is None:
        return []
    if hasattr(table, "to_dict"):
        try:
            records = table.to_dict(orient="records")
            return [_row_dict(row) for row in records]
        except TypeError:
            pass
    if isinstance(table, dict):
        if "rows" in table:
            return normalize_rows(table["rows"])
        return [_row_dict(table)]
    if isinstance(table, Iterable) and not isinstance(table, (str, bytes)):
        return [_row_dict(row) for row in table]
    return [_row_dict(table)]


def jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return value


def _row_dict(row: Any) -> dict[str, Any]:
    if is_dataclass(row):
        return jsonable(asdict(row))
    if isinstance(row, dict):
        return jsonable(row)
    if hasattr(row, "_asdict"):
        return jsonable(row._asdict())
    if hasattr(row, "dict"):
        return jsonable(row.dict())
    if hasattr(row, "model_dump"):
        return jsonable(row.model_dump())
    if hasattr(row, "__dict__"):
        return jsonable(vars(row))
    return {"value": jsonable(row)}


def _row_symbols(row: dict[str, Any]) -> set[str]:
    symbols: set[str] = set()
    for field in ("ticker", "symbol", "peer_symbol", "security", "name"):
        value = row.get(field)
        if isinstance(value, str) and value:
            symbols.add(value.split(":")[-1].upper())
    for field in ("symbols", "related_symbols", "bullish_symbols", "bearish_symbols", "holder_names"):
        value = row.get(field)
        if isinstance(value, list):
            symbols.update(str(item).split(":")[-1].upper() for item in value if item)
        elif isinstance(value, str):
            symbols.update(item.strip().split(":")[-1].upper() for item in value.replace(";", ",").split(",") if item.strip())
    history = row.get("ticker_history")
    if isinstance(history, list):
        for item in history:
            if isinstance(item, dict):
                symbols.update(_row_symbols(item))
    return symbols


def _matching_ticker_rows(rows: list[dict[str, Any]], ticker: str) -> list[dict[str, Any]]:
    ticker_fields = ("ticker", "symbol", "security", "name")
    matches: list[dict[str, Any]] = []
    for row in rows:
        if ticker in _row_symbols(row):
            matches.append(row)
            continue
        related = row.get("related_symbols")
        if isinstance(related, list) and any(str(item).split(":")[-1].upper() == ticker for item in related):
            matches.append(row)
            continue
        if isinstance(related, str):
            symbols = [item.strip().split(":")[-1].upper() for item in related.replace(";", ",").split(",")]
            if ticker in symbols:
                matches.append(row)
                continue
        symbols_value = row.get("symbols")
        if isinstance(symbols_value, list) and any(str(item).split(":")[-1].upper() == ticker for item in symbols_value):
            matches.append(row)
            continue
        if isinstance(symbols_value, str):
            symbols = [item.strip().split(":")[-1].upper() for item in symbols_value.replace(";", ",").split(",")]
            if ticker in symbols:
                matches.append(row)
                continue
        for field in ticker_fields:
            value = row.get(field)
            if isinstance(value, str) and value.split(":")[-1].upper() == ticker:
                matches.append(row)
                break
    return matches


def _is_empty(panel_data: PanelData) -> bool:
    if not panel_data.tables:
        return True
    for table in panel_data.tables.values():
        if table is None:
            continue
        if hasattr(table, "empty"):
            if not table.empty:
                return False
            continue
        try:
            if len(table) > 0:
                return False
        except TypeError:
            return False
    return True


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged
