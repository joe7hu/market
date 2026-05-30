"""Configuration loading for the investment panel."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Any

import yaml


def project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def resolve_path(value: str | Path, base: Path | None = None) -> Path:
    path = Path(os.path.expandvars(str(value))).expanduser()
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
class OpenCliConfig:
    enabled: bool = True
    command: str = "opencli"
    timeout_seconds: int = 25


@dataclass(frozen=True)
class TradingViewConfig:
    enabled: bool = True
    options_symbols: list[str] = field(default_factory=list)
    search_symbols: list[str] = field(default_factory=list)
    watchlist_colors: list[str] = field(default_factory=lambda: ["red", "orange", "yellow", "green", "blue", "purple"])
    alert_types: list[str] = field(default_factory=lambda: ["active", "triggered", "offline"])
    personal_surfaces_enabled: bool = True
    chart_state_enabled: bool = True
    screener_limit: int = 50
    news_limit: int = 50
    strikes_around_spot: int = 6


@dataclass(frozen=True)
class YFinanceConfig:
    enabled: bool = True


@dataclass(frozen=True)
class IBKRConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 77
    account_id: str | None = None
    readonly: bool = True
    paper_only: bool = True
    stale_after_minutes: int = 15


@dataclass(frozen=True)
class MoomooConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 11111
    paper_only: bool = True
    stale_after_minutes: int = 15
    scanner_limit: int = 50


@dataclass(frozen=True)
class BrokerPolicyConfig:
    require_account_for_recommendations: bool = False
    max_trade_notional: float = 10_000.0
    max_position_weight_pct: float = 20.0
    min_primary_evidence_count: int = 1
    min_total_evidence_count: int = 2
    earnings_blackout_days: int = 2


@dataclass(frozen=True)
class BrokerSourcesConfig:
    enabled: bool = True
    advisory_only: bool = True
    ibkr: IBKRConfig = IBKRConfig()
    moomoo: MoomooConfig = MoomooConfig()
    policy: BrokerPolicyConfig = BrokerPolicyConfig()


@dataclass(frozen=True)
class DataSourcesConfig:
    opencli: OpenCliConfig = OpenCliConfig()
    tradingview: TradingViewConfig = TradingViewConfig()
    yfinance: YFinanceConfig = YFinanceConfig()
    brokers: BrokerSourcesConfig = BrokerSourcesConfig()


@dataclass(frozen=True)
class EventSourcesConfig:
    enabled: bool = False
    seed_requested_week: bool = False
    bls_enabled: bool = True
    federal_reserve_enabled: bool = True
    treasury_enabled: bool = True
    sec_enabled: bool = True
    watchlist_enabled: bool = True


@dataclass(frozen=True)
class AnalysisConfig:
    enabled: bool = True
    correlation_lookback_days: int = 180
    max_correlation_peers: int = 8


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
    data_sources: DataSourcesConfig = DataSourcesConfig()
    event_sources: EventSourcesConfig = EventSourcesConfig()
    analysis: AnalysisConfig = AnalysisConfig()
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
        mode=str(market_data_raw.get("mode", "online")),
        lookback_days=int(market_data_raw.get("lookback_days", 260)),
        equity_provider=str(market_data_raw.get("equity_provider", "yfinance")),
        crypto_provider=str(market_data_raw.get("crypto_provider", "coingecko")),
        user_agent=str(market_data_raw.get("user_agent", "joehu-market-panel/0.1 contact:local")),
    )
    data_sources_raw = raw.get("data_sources", {})
    opencli_raw = data_sources_raw.get("opencli", {})
    tradingview_raw = data_sources_raw.get("tradingview", {})
    yfinance_raw = data_sources_raw.get("yfinance", {})
    brokers_raw = data_sources_raw.get("brokers", {})
    ibkr_raw = brokers_raw.get("ibkr", {})
    moomoo_raw = brokers_raw.get("moomoo", {})
    policy_raw = brokers_raw.get("policy", {})
    data_sources = DataSourcesConfig(
        opencli=OpenCliConfig(
            enabled=bool(opencli_raw.get("enabled", True)),
            command=str(opencli_raw.get("command", "opencli")),
            timeout_seconds=int(opencli_raw.get("timeout_seconds", 25)),
        ),
        tradingview=TradingViewConfig(
            enabled=bool(tradingview_raw.get("enabled", True)),
            options_symbols=list(tradingview_raw.get("options_symbols", [])),
            search_symbols=list(tradingview_raw.get("search_symbols", [])),
            watchlist_colors=list(
                tradingview_raw.get("watchlist_colors", ["red", "orange", "yellow", "green", "blue", "purple"])
            ),
            alert_types=list(tradingview_raw.get("alert_types", ["active", "triggered", "offline"])),
            personal_surfaces_enabled=bool(tradingview_raw.get("personal_surfaces_enabled", True)),
            chart_state_enabled=bool(tradingview_raw.get("chart_state_enabled", True)),
            screener_limit=int(tradingview_raw.get("screener_limit", 50)),
            news_limit=int(tradingview_raw.get("news_limit", 50)),
            strikes_around_spot=int(tradingview_raw.get("strikes_around_spot", 6)),
        ),
        yfinance=YFinanceConfig(enabled=bool(yfinance_raw.get("enabled", True))),
        brokers=BrokerSourcesConfig(
            enabled=bool(brokers_raw.get("enabled", True)),
            advisory_only=bool(brokers_raw.get("advisory_only", True)),
            ibkr=IBKRConfig(
                enabled=bool(ibkr_raw.get("enabled", False)),
                host=str(ibkr_raw.get("host", "127.0.0.1")),
                port=int(ibkr_raw.get("port", 7497)),
                client_id=int(ibkr_raw.get("client_id", 77)),
                account_id=ibkr_raw.get("account_id"),
                readonly=bool(ibkr_raw.get("readonly", True)),
                paper_only=bool(ibkr_raw.get("paper_only", True)),
                stale_after_minutes=int(ibkr_raw.get("stale_after_minutes", 15)),
            ),
            moomoo=MoomooConfig(
                enabled=bool(moomoo_raw.get("enabled", False)),
                host=str(moomoo_raw.get("host", "127.0.0.1")),
                port=int(moomoo_raw.get("port", 11111)),
                paper_only=bool(moomoo_raw.get("paper_only", True)),
                stale_after_minutes=int(moomoo_raw.get("stale_after_minutes", 15)),
                scanner_limit=int(moomoo_raw.get("scanner_limit", 50)),
            ),
            policy=BrokerPolicyConfig(
                require_account_for_recommendations=bool(policy_raw.get("require_account_for_recommendations", False)),
                max_trade_notional=float(policy_raw.get("max_trade_notional", 10_000.0)),
                max_position_weight_pct=float(policy_raw.get("max_position_weight_pct", 20.0)),
                min_primary_evidence_count=int(policy_raw.get("min_primary_evidence_count", 1)),
                min_total_evidence_count=int(policy_raw.get("min_total_evidence_count", 2)),
                earnings_blackout_days=int(policy_raw.get("earnings_blackout_days", 2)),
            ),
        ),
    )
    event_sources_raw = raw.get("event_sources", {})
    event_sources = EventSourcesConfig(
        enabled=bool(event_sources_raw.get("enabled", False)),
        seed_requested_week=bool(event_sources_raw.get("seed_requested_week", False)),
        bls_enabled=bool(event_sources_raw.get("bls_enabled", True)),
        federal_reserve_enabled=bool(event_sources_raw.get("federal_reserve_enabled", True)),
        treasury_enabled=bool(event_sources_raw.get("treasury_enabled", True)),
        sec_enabled=bool(event_sources_raw.get("sec_enabled", True)),
        watchlist_enabled=bool(event_sources_raw.get("watchlist_enabled", True)),
    )
    analysis_raw = raw.get("analysis", {})
    analysis = AnalysisConfig(
        enabled=bool(analysis_raw.get("enabled", True)),
        correlation_lookback_days=int(analysis_raw.get("correlation_lookback_days", 180)),
        max_correlation_peers=int(analysis_raw.get("max_correlation_peers", 8)),
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
        data_sources=data_sources,
        event_sources=event_sources,
        analysis=analysis,
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
        "data_sources": {
            "opencli": {
                "enabled": config.data_sources.opencli.enabled,
                "command": config.data_sources.opencli.command,
                "timeout_seconds": config.data_sources.opencli.timeout_seconds,
            },
            "tradingview": {
                "enabled": config.data_sources.tradingview.enabled,
                "options_symbols": config.data_sources.tradingview.options_symbols,
                "search_symbols": config.data_sources.tradingview.search_symbols,
                "watchlist_colors": config.data_sources.tradingview.watchlist_colors,
                "alert_types": config.data_sources.tradingview.alert_types,
                "personal_surfaces_enabled": config.data_sources.tradingview.personal_surfaces_enabled,
                "chart_state_enabled": config.data_sources.tradingview.chart_state_enabled,
                "screener_limit": config.data_sources.tradingview.screener_limit,
                "news_limit": config.data_sources.tradingview.news_limit,
                "strikes_around_spot": config.data_sources.tradingview.strikes_around_spot,
            },
            "yfinance": {"enabled": config.data_sources.yfinance.enabled},
            "brokers": {
                "enabled": config.data_sources.brokers.enabled,
                "advisory_only": config.data_sources.brokers.advisory_only,
                "ibkr": {
                    "enabled": config.data_sources.brokers.ibkr.enabled,
                    "host": config.data_sources.brokers.ibkr.host,
                    "port": config.data_sources.brokers.ibkr.port,
                    "client_id": config.data_sources.brokers.ibkr.client_id,
                    "account_id": config.data_sources.brokers.ibkr.account_id,
                    "readonly": config.data_sources.brokers.ibkr.readonly,
                    "paper_only": config.data_sources.brokers.ibkr.paper_only,
                    "stale_after_minutes": config.data_sources.brokers.ibkr.stale_after_minutes,
                },
                "moomoo": {
                    "enabled": config.data_sources.brokers.moomoo.enabled,
                    "host": config.data_sources.brokers.moomoo.host,
                    "port": config.data_sources.brokers.moomoo.port,
                    "paper_only": config.data_sources.brokers.moomoo.paper_only,
                    "stale_after_minutes": config.data_sources.brokers.moomoo.stale_after_minutes,
                    "scanner_limit": config.data_sources.brokers.moomoo.scanner_limit,
                },
                "policy": {
                    "max_trade_notional": config.data_sources.brokers.policy.max_trade_notional,
                    "require_account_for_recommendations": config.data_sources.brokers.policy.require_account_for_recommendations,
                    "max_position_weight_pct": config.data_sources.brokers.policy.max_position_weight_pct,
                    "min_primary_evidence_count": config.data_sources.brokers.policy.min_primary_evidence_count,
                    "min_total_evidence_count": config.data_sources.brokers.policy.min_total_evidence_count,
                    "earnings_blackout_days": config.data_sources.brokers.policy.earnings_blackout_days,
                },
            },
        },
        "event_sources": {
            "enabled": config.event_sources.enabled,
            "seed_requested_week": config.event_sources.seed_requested_week,
            "bls_enabled": config.event_sources.bls_enabled,
            "federal_reserve_enabled": config.event_sources.federal_reserve_enabled,
            "treasury_enabled": config.event_sources.treasury_enabled,
            "sec_enabled": config.event_sources.sec_enabled,
            "watchlist_enabled": config.event_sources.watchlist_enabled,
        },
        "analysis": {
            "enabled": config.analysis.enabled,
            "correlation_lookback_days": config.analysis.correlation_lookback_days,
            "max_correlation_peers": config.analysis.max_correlation_peers,
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
