"""Configuration loading for the investment panel."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def resolve_path(value: str | Path, base: Path | None = None) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return (base or project_root()) / path


@dataclass(frozen=True)
class DatabaseConfig:
    duckdb_path: Path = project_root() / "data" / "investment.duckdb"


@dataclass(frozen=True)
class NasConfig:
    source_root: Path = Path("/Volumes/agent/data-sources")
    status_dir: Path = Path("/Volumes/agent/data-sources/status")
    market_dir: Path = Path("/Volumes/agent/data-sources/market-mini")
    duckdb_snapshot_dir: Path = Path("/Volumes/agent/data-sources/market-mini/duckdb-snapshots")


@dataclass(frozen=True)
class ArcoConfig:
    raw_dir: Path = Path("/Users/joehu/brain/raw/sources/arco")
    signals_path: str = "signals.json"
    beliefs_path: str = "beliefs.json"
    source_manifest_glob: str = "source-manifest-*.json"
    birdclaw_bookmarks_glob: str = "birdclaw-bookmarks-*.json"


@dataclass(frozen=True)
class MarketDataConfig:
    mode: str = "online"
    lookback_days: int = 260
    equity_provider: str = "yfinance"
    crypto_provider: str = "coingecko"
    user_agent: str = "joehu-market-panel/0.1 contact:local"


@dataclass(frozen=True)
class ScoringConfig:
    weights: dict[str, float] = field(
        default_factory=lambda: {
            "technical": 0.25,
            "fundamental": 0.20,
            "category": 0.20,
            "thesis": 0.15,
            "trader": 0.10,
            "portfolio_fit": 0.10,
        }
    )
    research_threshold: float = 75.0


@dataclass(frozen=True)
class AppConfig:
    database: DatabaseConfig = DatabaseConfig()
    nas: NasConfig = NasConfig()
    arco: ArcoConfig = ArcoConfig()
    market_data: MarketDataConfig = MarketDataConfig()
    scoring: ScoringConfig = ScoringConfig()
    watchlist: list[dict[str, Any]] = field(default_factory=list)
    portfolio_csv: Path | None = None
    trader_profile_dir: Path = project_root() / "data" / "trader_profiles"
    prompt_dir: Path = project_root() / "prompts"
    report_dir: Path = project_root() / "data" / "reports"
    packet_dir: Path = project_root() / "data" / "packets"


def load_config(path: str | Path | None = None) -> AppConfig:
    config_path = resolve_path(path or "config.yaml")
    raw: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}

    base = project_root()
    database = DatabaseConfig(
        duckdb_path=resolve_path(raw.get("database", {}).get("duckdb_path", "data/investment.duckdb"), base)
    )
    nas_raw = raw.get("nas", {})
    nas = NasConfig(
        source_root=resolve_path(nas_raw.get("source_root", "/Volumes/agent/data-sources"), base),
        status_dir=resolve_path(nas_raw.get("status_dir", "/Volumes/agent/data-sources/status"), base),
        market_dir=resolve_path(nas_raw.get("market_dir", "/Volumes/agent/data-sources/market-mini"), base),
        duckdb_snapshot_dir=resolve_path(
            nas_raw.get("duckdb_snapshot_dir", "/Volumes/agent/data-sources/market-mini/duckdb-snapshots"),
            base,
        ),
    )
    arco_raw = raw.get("arco", {})
    arco = ArcoConfig(
        raw_dir=resolve_path(arco_raw.get("raw_dir", "/Users/joehu/brain/raw/sources/arco"), base),
        signals_path=arco_raw.get("signals_path", "signals.json"),
        beliefs_path=arco_raw.get("beliefs_path", "beliefs.json"),
        source_manifest_glob=arco_raw.get("source_manifest_glob", "source-manifest-*.json"),
        birdclaw_bookmarks_glob=arco_raw.get("birdclaw_bookmarks_glob", "birdclaw-bookmarks-*.json"),
    )
    market_data_raw = raw.get("market_data", {})
    market_data = MarketDataConfig(
        mode=str(market_data_raw.get("mode", "sample")),
        lookback_days=int(market_data_raw.get("lookback_days", 260)),
        equity_provider=str(market_data_raw.get("equity_provider", "yfinance")),
        crypto_provider=str(market_data_raw.get("crypto_provider", "coingecko")),
        user_agent=str(market_data_raw.get("user_agent", "joehu-market-panel/0.1 contact:local")),
    )
    scoring_raw = raw.get("scoring", {})
    scoring = ScoringConfig(
        weights={**ScoringConfig().weights, **dict(scoring_raw.get("weights", {}))},
        research_threshold=float(scoring_raw.get("research_threshold", 75.0)),
    )
    portfolio_csv = raw.get("portfolio", {}).get("csv_path") or raw.get("portfolio_csv")
    return AppConfig(
        database=database,
        nas=nas,
        arco=arco,
        market_data=market_data,
        scoring=scoring,
        watchlist=list(raw.get("watchlist", [])),
        portfolio_csv=resolve_path(portfolio_csv, base) if portfolio_csv else None,
        trader_profile_dir=resolve_path(raw.get("trader_profile_dir", "data/trader_profiles"), base),
        prompt_dir=resolve_path(raw.get("prompt_dir", "prompts"), base),
        report_dir=resolve_path(raw.get("report_dir", "data/reports"), base),
        packet_dir=resolve_path(raw.get("packet_dir", "data/packets"), base),
    )


def config_to_dict(config: AppConfig) -> dict[str, Any]:
    return {
        "database": {"duckdb_path": str(config.database.duckdb_path)},
        "nas": {
            "source_root": str(config.nas.source_root),
            "status_dir": str(config.nas.status_dir),
            "market_dir": str(config.nas.market_dir),
            "duckdb_snapshot_dir": str(config.nas.duckdb_snapshot_dir),
        },
        "arco": {
            "raw_dir": str(config.arco.raw_dir),
            "signals_path": config.arco.signals_path,
            "beliefs_path": config.arco.beliefs_path,
        },
        "market_data": {
            "mode": config.market_data.mode,
            "lookback_days": config.market_data.lookback_days,
            "equity_provider": config.market_data.equity_provider,
            "crypto_provider": config.market_data.crypto_provider,
        },
        "scoring": {
            "weights": config.scoring.weights,
            "research_threshold": config.scoring.research_threshold,
        },
        "watchlist": config.watchlist,
        "portfolio_csv": str(config.portfolio_csv) if config.portfolio_csv else None,
        "trader_profile_dir": str(config.trader_profile_dir),
        "prompt_dir": str(config.prompt_dir),
    }
