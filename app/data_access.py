"""Data loading and JSON normalization for the investment panel API."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from importlib import import_module
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
        "arco": {"raw_dir": "/Users/joehu/brain/raw/sources/arco"},
        "trader_profile_dir": "data/trader_profiles",
        "prompt_dir": "prompts",
    }
    if not config_path.exists():
        return defaults

    try:
        import yaml
    except ModuleNotFoundError:
        return defaults | {"config_warning": "Install PyYAML to read config.yaml."}

    with config_path.open("r", encoding="utf-8") as handle:
        parsed = yaml.safe_load(handle) or {}
    return _deep_merge(defaults, parsed)


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
    return {"symbol": symbol, "quantity": quantity, "avg_cost": avg_cost, "purchase_date": purchase_date, "notes": notes}


def delete_portfolio_position(config: dict[str, Any], symbol: str) -> dict[str, Any]:
    normalized = symbol.strip().upper()
    if not normalized:
        raise ValueError("symbol is required")

    _resolve_core_helper()
    from investment_panel.core.db import db, init_db

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
            "signals",
            "ticker_memos",
            "portfolio",
            "theses",
            "trader_twins",
            "catalysts",
            "fundamentals",
            "disclosures",
            "quotes",
            "screener",
            "options_expiries",
            "options_chain",
            "news",
            "sepa",
            "liquidity",
            "correlations",
            "etf_premiums",
            "analyst_estimates",
            "earnings",
            "valuations",
            "provider_runs",
            "source_health",
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
    candidates = panel_data.rows("candidates")
    portfolio = panel_data.rows("portfolio")
    theses = panel_data.rows("theses")
    catalysts = panel_data.rows("catalysts")
    fundamentals = panel_data.rows("fundamentals")
    disclosures = panel_data.rows("disclosures")
    quotes = panel_data.rows("quotes")
    news = panel_data.rows("news")
    sepa = panel_data.rows("sepa")
    liquidity = panel_data.rows("liquidity")
    earnings = panel_data.rows("earnings")
    valuations = panel_data.rows("valuations")
    source_health = panel_data.rows("source_health")
    return {
        "status": status_payload(panel_data),
        "metrics": {
            "candidates": len(candidates),
            "holdings": len(portfolio),
            "theses": len(theses),
            "catalysts": len(catalysts),
            "fundamentals": len(fundamentals),
            "disclosures": len(disclosures),
            "quotes": len(quotes),
            "news": len(news),
            "sepa": len(sepa),
            "liquidity": len(liquidity),
            "earnings": len(earnings),
            "valuations": len(valuations),
            "sources": len(source_health),
        },
        "priority_candidates": candidates[:8],
        "near_term_catalysts": catalysts[:8],
        "portfolio": portfolio[:8],
        "source_health": source_health[:8],
        "disclosures": disclosures[:8],
        "news": news[:8],
    }


def ticker_payload(panel_data: PanelData, ticker: str) -> dict[str, Any]:
    normalized_ticker = ticker.upper()
    tables = {
        "candidates": _matching_ticker_rows(panel_data.rows("candidates"), normalized_ticker),
        "opportunities_ranked": _matching_ticker_rows(panel_data.rows("opportunities_ranked"), normalized_ticker),
        "opportunity_sources": _matching_ticker_rows(panel_data.rows("opportunity_sources"), normalized_ticker),
        "portfolio": _matching_ticker_rows(panel_data.rows("portfolio"), normalized_ticker),
        "theses": _matching_ticker_rows(panel_data.rows("theses"), normalized_ticker),
        "catalysts": _matching_ticker_rows(panel_data.rows("catalysts"), normalized_ticker),
        "signals": _matching_ticker_rows(panel_data.rows("signals"), normalized_ticker),
        "fundamentals": _matching_ticker_rows(panel_data.rows("fundamentals"), normalized_ticker),
        "disclosures": _matching_ticker_rows(panel_data.rows("disclosures"), normalized_ticker),
        "quotes": _matching_ticker_rows(panel_data.rows("quotes"), normalized_ticker),
        "options_expiries": _matching_ticker_rows(panel_data.rows("options_expiries"), normalized_ticker),
        "options_chain": _matching_ticker_rows(panel_data.rows("options_chain"), normalized_ticker),
        "news": _matching_ticker_rows(panel_data.rows("news"), normalized_ticker),
        "sepa": _matching_ticker_rows(panel_data.rows("sepa"), normalized_ticker),
        "liquidity": _matching_ticker_rows(panel_data.rows("liquidity"), normalized_ticker),
        "correlations": _matching_ticker_rows(panel_data.rows("correlations"), normalized_ticker),
        "etf_premiums": _matching_ticker_rows(panel_data.rows("etf_premiums"), normalized_ticker),
        "analyst_estimates": _matching_ticker_rows(panel_data.rows("analyst_estimates"), normalized_ticker),
        "earnings": _matching_ticker_rows(panel_data.rows("earnings"), normalized_ticker),
        "valuations": _matching_ticker_rows(panel_data.rows("valuations"), normalized_ticker),
        "technicals": _matching_ticker_rows(panel_data.rows("technicals"), normalized_ticker),
        "research_packets": _matching_ticker_rows(panel_data.rows("research_packets"), normalized_ticker),
        "memos": _matching_ticker_rows(
            panel_data.rows("ticker_memos") or panel_data.rows("memos"),
            normalized_ticker,
        ),
    }
    return {
        "ticker": normalized_ticker,
        "status": status_payload(panel_data),
        "tables": tables,
        "found": any(tables.values()),
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


def _matching_ticker_rows(rows: list[dict[str, Any]], ticker: str) -> list[dict[str, Any]]:
    ticker_fields = ("ticker", "symbol", "security", "name")
    matches: list[dict[str, Any]] = []
    for row in rows:
        related = row.get("related_symbols")
        if isinstance(related, list) and any(str(item).split(":")[-1].upper() == ticker for item in related):
            matches.append(row)
            continue
        if isinstance(related, str):
            symbols = [item.strip().split(":")[-1].upper() for item in related.replace(";", ",").split(",")]
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
