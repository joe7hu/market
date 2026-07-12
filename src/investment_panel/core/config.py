"""Configuration loading for the investment panel."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Any

import yaml

from investment_panel.core.config_mutations import update_agent_settings_config, update_research_sources_config
from investment_panel.database.configuration import DatabaseConfig, load_database_config, merge_persisted_setting_sections
def project_root() -> Path:
    return Path(__file__).resolve().parents[3]
def resolve_path(value: str | Path, base: Path | None = None) -> Path:
    path = Path(os.path.expandvars(str(value))).expanduser()
    if path.is_absolute():
        return path
    return (base or project_root()) / path
@dataclass(frozen=True)
class NasConfig:
    source_root: Path = Path("/Volumes/agent/data-sources")
    status_dir: Path = Path("/Volumes/agent/data-sources/status")
    market_dir: Path = Path("/Volumes/agent/data-sources/market-mini")
    duckdb_snapshot_dir: Path = Path("/Volumes/agent/data-sources/market-mini/duckdb-snapshots")
    postgres_backup_dir: Path = Path("/Volumes/agent/data-sources/market-mini/postgres-backups")
@dataclass(frozen=True)
class ArcoConfig:
    raw_dir: Path = Path("/Volumes/agent/brain/raw/sources/arco")
    signals_path: str = "signals.json"
    beliefs_path: str = "beliefs.json"
    brief_beliefs_glob: str = "brief-beliefs/brief-beliefs-*.json"
    source_manifest_glob: str = "source-manifest-*.json"
    birdclaw_bookmarks_glob: str = "birdclaw-bookmarks-*.json"
    web_captures_glob: str = "web-captures-*.json"


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
    option_scan_limit: int = 80


@dataclass(frozen=True)
class YFinanceConfig:
    enabled: bool = True


@dataclass(frozen=True)
class IBKRConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 4002
    client_id: int = 77
    account_id: str | None = None
    readonly: bool = True
    paper_only: bool = True
    stale_after_minutes: int = 15
    market_data_type: str = "live_or_delayed"
    quote_limit: int = 50


@dataclass(frozen=True)
class RobinhoodConfig:
    enabled: bool = False
    mcp_url: str = "https://agent.robinhood.com/mcp/trading"
    token_path: str = "~/.config/market/robinhood-mcp-token.json"
    auth_token_env: str = "ROBINHOOD_MCP_TOKEN"
    prefer_codex_credentials: bool = True
    codex_credentials_path: str = "~/.codex/.credentials.json"
    codex_mcp_server_name: str = "robinhood-trading"
    client_id: str | None = None
    scope: str = "internal"
    callback_host: str = "127.0.0.1"
    callback_port: int = 8765
    timeout_seconds: int = 30
    max_collection_seconds: int = 600
    max_response_bytes: int = 8 * 1024 * 1024
    readonly: bool = True
    max_symbols: int = 40
    max_expiries: int = 2
    strikes_around_spot: int = 12
    quote_batch_size: int = 20
    collect_puts: bool = False
    near_term_dte: int = 35


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
    robinhood: RobinhoodConfig = RobinhoodConfig()
    moomoo: MoomooConfig = MoomooConfig()
    policy: BrokerPolicyConfig = BrokerPolicyConfig()


@dataclass(frozen=True)
class DataSourcesConfig:
    opencli: OpenCliConfig = OpenCliConfig()
    tradingview: TradingViewConfig = TradingViewConfig()
    yfinance: YFinanceConfig = YFinanceConfig()
    brokers: BrokerSourcesConfig = BrokerSourcesConfig()


@dataclass(frozen=True)
class ResearchXConfig:
    enabled: bool = True
    list_id: str = ""
    priority_handles: list[str] = field(
        default_factory=lambda: ["balajis", "karpathy", "citrini", "BillAckman", "dylan522p", "IncomeSharks"]
    )
    limit: int = 30
    # Per-cycle cap on per-account fallback requests (the list call is one request).
    account_fetch_cap: int = 2


@dataclass(frozen=True)
class ResearchNewsConfig:
    enabled: bool = True
    providers: list[str] = field(default_factory=lambda: ["bloomberg", "reuters", "google-news", "hackernews"])
    limit: int = 30


@dataclass(frozen=True)
class ResearchBlogsConfig:
    enabled: bool = True
    substack_urls: list[str] = field(default_factory=list)
    rss_urls: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ResearchSourcesConfig:
    x: ResearchXConfig = ResearchXConfig()
    news: ResearchNewsConfig = ResearchNewsConfig()
    blogs: ResearchBlogsConfig = ResearchBlogsConfig()


@dataclass(frozen=True)
class EventSourcesConfig:
    enabled: bool = False
    seed_requested_week: bool = False
    bls_enabled: bool = True
    dol_enabled: bool = True
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
class AgentCommandConfig:
    enabled: bool = False
    command: str = ""
    timeout_seconds: int = 120
    limit: int = 20


DEFAULT_AGENT_CONTEXT_SOURCES: dict[str, bool] = {
    "fundamentals": True,
    "technicals": True,
    "ownership": True,
    "news": True,
    "social_signals": True,
    "catalysts": True,
    "portfolio": True,
    "decision": True,
}

DEFAULT_AGENT_PRICING: dict[str, dict[str, float]] = {
    "default": {"input_per_1m": 1.25, "output_per_1m": 10.0},
    "gpt-5.2": {"input_per_1m": 1.25, "output_per_1m": 10.0},
}


@dataclass(frozen=True)
class OptionAgentConfig:
    """Unified single-pass option agent (consolidated thesis + postmortem)."""

    enabled: bool = False
    command: str = ""
    timeout_seconds: int = 180
    thesis_limit: int = 8
    postmortem_limit: int = 4
    provider: str = "codex"
    model: str = ""
    reasoning_effort: str = ""
    # In-app scheduler cadence override (0 = use MARKET_AGENT_REFRESH_SECONDS / default).
    auto_run_seconds: int = 0
    max_runs_per_day: int = 1
    # Per-ticker context sources fed to each run; toggle off to trim the prompt.
    context_sources: dict[str, bool] = field(default_factory=lambda: dict(DEFAULT_AGENT_CONTEXT_SOURCES))


@dataclass(frozen=True)
class AgentsConfig:
    option_thesis: AgentCommandConfig = AgentCommandConfig()
    option_postmortem: AgentCommandConfig = AgentCommandConfig()
    option_agent: OptionAgentConfig = OptionAgentConfig()
    pricing: dict[str, dict[str, float]] = field(default_factory=lambda: {k: dict(v) for k, v in DEFAULT_AGENT_PRICING.items()})


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
    research_sources: ResearchSourcesConfig = ResearchSourcesConfig()
    event_sources: EventSourcesConfig = EventSourcesConfig()
    analysis: AnalysisConfig = AnalysisConfig()
    agents: AgentsConfig = AgentsConfig()
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
    database = load_database_config(raw, base)
    # PostgreSQL is the settings authority whether its DSN came from the
    # environment or config.yaml. The repository handles an unavailable
    # database as a no-op so initial migration/config tooling remains usable.
    if database.url.startswith(("postgresql://", "postgresql+psycopg://")):
        raw = merge_persisted_setting_sections(raw, database.url)
    nas_raw = raw.get("nas", {})
    nas = NasConfig(
        source_root=resolve_path(nas_raw.get("source_root", "/Volumes/agent/data-sources"), base),
        status_dir=resolve_path(nas_raw.get("status_dir", "/Volumes/agent/data-sources/status"), base),
        market_dir=resolve_path(nas_raw.get("market_dir", "/Volumes/agent/data-sources/market-mini"), base),
        duckdb_snapshot_dir=resolve_path(
            nas_raw.get("duckdb_snapshot_dir", "/Volumes/agent/data-sources/market-mini/duckdb-snapshots"),
            base,
        ),
        postgres_backup_dir=resolve_path(
            nas_raw.get("postgres_backup_dir", "/Volumes/agent/data-sources/market-mini/postgres-backups"),
            base,
        ),
    )
    arco_raw = raw.get("arco", {})
    arco = ArcoConfig(
        raw_dir=resolve_path(arco_raw.get("raw_dir", "/Volumes/agent/brain/raw/sources/arco"), base),
        signals_path=arco_raw.get("signals_path", "signals.json"),
        beliefs_path=arco_raw.get("beliefs_path", "beliefs.json"),
        brief_beliefs_glob=arco_raw.get("brief_beliefs_glob", "brief-beliefs/brief-beliefs-*.json"),
        source_manifest_glob=arco_raw.get("source_manifest_glob", "source-manifest-*.json"),
        birdclaw_bookmarks_glob=arco_raw.get("birdclaw_bookmarks_glob", "birdclaw-bookmarks-*.json"),
        web_captures_glob=arco_raw.get("web_captures_glob", "web-captures-*.json"),
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
    robinhood_raw = brokers_raw.get("robinhood", {})
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
            option_scan_limit=int(tradingview_raw.get("option_scan_limit", 80)),
        ),
        yfinance=YFinanceConfig(enabled=bool(yfinance_raw.get("enabled", True))),
        brokers=BrokerSourcesConfig(
            enabled=bool(brokers_raw.get("enabled", True)),
            advisory_only=bool(brokers_raw.get("advisory_only", True)),
            ibkr=IBKRConfig(
                enabled=bool(ibkr_raw.get("enabled", False)),
                host=str(ibkr_raw.get("host", "127.0.0.1")),
                port=int(ibkr_raw.get("port", 4002)),
                client_id=int(ibkr_raw.get("client_id", 77)),
                account_id=ibkr_raw.get("account_id"),
                readonly=bool(ibkr_raw.get("readonly", True)),
                paper_only=bool(ibkr_raw.get("paper_only", True)),
                stale_after_minutes=int(ibkr_raw.get("stale_after_minutes", 15)),
                market_data_type=str(ibkr_raw.get("market_data_type", "live_or_delayed")),
                quote_limit=int(ibkr_raw.get("quote_limit", 50)),
            ),
            robinhood=RobinhoodConfig(
                enabled=bool(robinhood_raw.get("enabled", False)),
                mcp_url=str(robinhood_raw.get("mcp_url", "https://agent.robinhood.com/mcp/trading")),
                token_path=str(robinhood_raw.get("token_path", "~/.config/market/robinhood-mcp-token.json")),
                auth_token_env=str(robinhood_raw.get("auth_token_env", "ROBINHOOD_MCP_TOKEN")),
                prefer_codex_credentials=bool(robinhood_raw.get("prefer_codex_credentials", True)),
                codex_credentials_path=str(robinhood_raw.get("codex_credentials_path", "~/.codex/.credentials.json")),
                codex_mcp_server_name=str(robinhood_raw.get("codex_mcp_server_name", "robinhood-trading")),
                client_id=robinhood_raw.get("client_id"),
                scope=str(robinhood_raw.get("scope", "internal")),
                callback_host=str(robinhood_raw.get("callback_host", "127.0.0.1")),
                callback_port=int(robinhood_raw.get("callback_port", 8765)),
                timeout_seconds=int(robinhood_raw.get("timeout_seconds", 30)),
                max_collection_seconds=int(robinhood_raw.get("max_collection_seconds", 600)),
                max_response_bytes=int(robinhood_raw.get("max_response_bytes", 8 * 1024 * 1024)),
                readonly=bool(robinhood_raw.get("readonly", True)),
                max_symbols=int(robinhood_raw.get("max_symbols", 40)),
                max_expiries=int(robinhood_raw.get("max_expiries", 2)),
                strikes_around_spot=int(robinhood_raw.get("strikes_around_spot", 12)),
                quote_batch_size=int(robinhood_raw.get("quote_batch_size", 20)),
                collect_puts=bool(robinhood_raw.get("collect_puts", False)),
                near_term_dte=int(robinhood_raw.get("near_term_dte", 35)),
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
    research_sources_raw = raw.get("research_sources", {})
    research_x_raw = research_sources_raw.get("x", {})
    research_news_raw = research_sources_raw.get("news", {})
    research_blogs_raw = research_sources_raw.get("blogs", {})
    research_sources = ResearchSourcesConfig(
        x=ResearchXConfig(
            enabled=bool(research_x_raw.get("enabled", True)),
            list_id=str(research_x_raw.get("list_id", "") or ""),
            priority_handles=list(
                research_x_raw.get(
                    "priority_handles", ["balajis", "karpathy", "citrini", "BillAckman", "dylan522p", "IncomeSharks"]
                )
            ),
            limit=int(research_x_raw.get("limit", 30)),
            account_fetch_cap=int(research_x_raw.get("account_fetch_cap", 2)),
        ),
        news=ResearchNewsConfig(
            enabled=bool(research_news_raw.get("enabled", True)),
            providers=list(research_news_raw.get("providers", ["bloomberg", "reuters", "google-news", "hackernews"])),
            limit=int(research_news_raw.get("limit", 30)),
        ),
        blogs=ResearchBlogsConfig(
            enabled=bool(research_blogs_raw.get("enabled", True)),
            substack_urls=list(research_blogs_raw.get("substack_urls", [])),
            rss_urls=list(research_blogs_raw.get("rss_urls", [])),
        ),
    )
    event_sources_raw = raw.get("event_sources", {})
    event_sources = EventSourcesConfig(
        enabled=bool(event_sources_raw.get("enabled", False)),
        seed_requested_week=bool(event_sources_raw.get("seed_requested_week", False)),
        bls_enabled=bool(event_sources_raw.get("bls_enabled", True)),
        dol_enabled=bool(event_sources_raw.get("dol_enabled", True)),
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
    agents_raw = raw.get("agents", {})
    option_thesis_raw = agents_raw.get("option_thesis", {})
    option_postmortem_raw = agents_raw.get("option_postmortem", {})
    option_thesis_env_command = os.environ.get("MARKET_OPTION_THESIS_AGENT_COMMAND")
    option_postmortem_env_command = os.environ.get("MARKET_OPTION_POSTMORTEM_AGENT_COMMAND")
    option_thesis_command = str(option_thesis_env_command or option_thesis_raw.get("command", ""))
    option_postmortem_command = str(option_postmortem_env_command or option_postmortem_raw.get("command", ""))
    option_agent_raw = agents_raw.get("option_agent", {})
    option_agent_env_command = os.environ.get("MARKET_OPTION_AGENT_COMMAND")
    option_agent_command = str(option_agent_env_command or option_agent_raw.get("command", ""))
    agents = AgentsConfig(
        option_thesis=AgentCommandConfig(
            enabled=bool(option_thesis_env_command) or bool(option_thesis_raw.get("enabled", bool(option_thesis_command))),
            command=option_thesis_command,
            timeout_seconds=int(option_thesis_raw.get("timeout_seconds", 120)),
            limit=int(option_thesis_raw.get("limit", 20)),
        ),
        option_postmortem=AgentCommandConfig(
            enabled=bool(option_postmortem_env_command) or bool(option_postmortem_raw.get("enabled", bool(option_postmortem_command))),
            command=option_postmortem_command,
            timeout_seconds=int(option_postmortem_raw.get("timeout_seconds", 120)),
            limit=int(option_postmortem_raw.get("limit", 20)),
        ),
        option_agent=OptionAgentConfig(
            enabled=bool(option_agent_env_command) or bool(option_agent_raw.get("enabled", bool(option_agent_command))),
            command=option_agent_command,
            timeout_seconds=int(option_agent_raw.get("timeout_seconds", 180)),
            thesis_limit=int(option_agent_raw.get("thesis_limit", option_thesis_raw.get("limit", 8))),
            postmortem_limit=int(option_agent_raw.get("postmortem_limit", option_postmortem_raw.get("limit", 4))),
            provider=str(option_agent_raw.get("provider", "codex")),
            model=str(option_agent_raw.get("model", "")),
            reasoning_effort=str(option_agent_raw.get("reasoning_effort", "")),
            auto_run_seconds=int(option_agent_raw.get("auto_run_seconds", 0)),
            max_runs_per_day=int(option_agent_raw.get("max_runs_per_day", 1)),
            context_sources={**DEFAULT_AGENT_CONTEXT_SOURCES, **{k: bool(v) for k, v in dict(option_agent_raw.get("context_sources", {})).items()}},
        ),
        pricing={**{k: dict(v) for k, v in DEFAULT_AGENT_PRICING.items()}, **{k: dict(v) for k, v in dict(agents_raw.get("pricing", {})).items()}},
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
        research_sources=research_sources,
        event_sources=event_sources,
        analysis=analysis,
        agents=agents,
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
        "database": {"url": config.database.url},
        "nas": {
            "source_root": str(config.nas.source_root),
            "status_dir": str(config.nas.status_dir),
            "market_dir": str(config.nas.market_dir),
            "postgres_backup_dir": str(config.nas.postgres_backup_dir),
        },
        "arco": {
            "raw_dir": str(config.arco.raw_dir),
            "signals_path": config.arco.signals_path,
            "beliefs_path": config.arco.beliefs_path,
            "brief_beliefs_glob": config.arco.brief_beliefs_glob,
            "source_manifest_glob": config.arco.source_manifest_glob,
            "birdclaw_bookmarks_glob": config.arco.birdclaw_bookmarks_glob,
            "web_captures_glob": config.arco.web_captures_glob,
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
                "option_scan_limit": config.data_sources.tradingview.option_scan_limit,
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
                    "market_data_type": config.data_sources.brokers.ibkr.market_data_type,
                    "quote_limit": config.data_sources.brokers.ibkr.quote_limit,
                },
                "robinhood": {
                    "enabled": config.data_sources.brokers.robinhood.enabled,
                    "mcp_url": config.data_sources.brokers.robinhood.mcp_url,
                    "token_path": config.data_sources.brokers.robinhood.token_path,
                    "auth_token_env": config.data_sources.brokers.robinhood.auth_token_env,
                    "prefer_codex_credentials": config.data_sources.brokers.robinhood.prefer_codex_credentials,
                    "codex_credentials_path": config.data_sources.brokers.robinhood.codex_credentials_path,
                    "codex_mcp_server_name": config.data_sources.brokers.robinhood.codex_mcp_server_name,
                    "client_id": config.data_sources.brokers.robinhood.client_id,
                    "scope": config.data_sources.brokers.robinhood.scope,
                    "callback_host": config.data_sources.brokers.robinhood.callback_host,
                    "callback_port": config.data_sources.brokers.robinhood.callback_port,
                    "timeout_seconds": config.data_sources.brokers.robinhood.timeout_seconds,
                    "max_collection_seconds": config.data_sources.brokers.robinhood.max_collection_seconds,
                    "max_response_bytes": config.data_sources.brokers.robinhood.max_response_bytes,
                    "readonly": config.data_sources.brokers.robinhood.readonly,
                    "max_symbols": config.data_sources.brokers.robinhood.max_symbols,
                    "max_expiries": config.data_sources.brokers.robinhood.max_expiries,
                    "strikes_around_spot": config.data_sources.brokers.robinhood.strikes_around_spot,
                    "quote_batch_size": config.data_sources.brokers.robinhood.quote_batch_size,
                    "collect_puts": config.data_sources.brokers.robinhood.collect_puts,
                    "near_term_dte": config.data_sources.brokers.robinhood.near_term_dte,
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
            "dol_enabled": config.event_sources.dol_enabled,
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
        "agents": {
            "option_thesis": {
                "enabled": config.agents.option_thesis.enabled,
                "command": config.agents.option_thesis.command,
                "timeout_seconds": config.agents.option_thesis.timeout_seconds,
                "limit": config.agents.option_thesis.limit,
            },
            "option_postmortem": {
                "enabled": config.agents.option_postmortem.enabled,
                "command": config.agents.option_postmortem.command,
                "timeout_seconds": config.agents.option_postmortem.timeout_seconds,
                "limit": config.agents.option_postmortem.limit,
            },
            "option_agent": {
                "enabled": config.agents.option_agent.enabled,
                "command": config.agents.option_agent.command,
                "timeout_seconds": config.agents.option_agent.timeout_seconds,
                "thesis_limit": config.agents.option_agent.thesis_limit,
                "postmortem_limit": config.agents.option_agent.postmortem_limit,
                "provider": config.agents.option_agent.provider,
                "model": config.agents.option_agent.model,
                "reasoning_effort": config.agents.option_agent.reasoning_effort,
                "auto_run_seconds": config.agents.option_agent.auto_run_seconds,
                "max_runs_per_day": config.agents.option_agent.max_runs_per_day,
                "context_sources": dict(config.agents.option_agent.context_sources),
            },
            "pricing": {k: dict(v) for k, v in config.agents.pricing.items()},
        },
        "research_sources": {
            "x": {
                "enabled": config.research_sources.x.enabled,
                "list_id": config.research_sources.x.list_id,
                "priority_handles": config.research_sources.x.priority_handles,
                "limit": config.research_sources.x.limit,
                "account_fetch_cap": config.research_sources.x.account_fetch_cap,
            },
            "news": {
                "enabled": config.research_sources.news.enabled,
                "providers": config.research_sources.news.providers,
                "limit": config.research_sources.news.limit,
            },
            "blogs": {
                "enabled": config.research_sources.blogs.enabled,
                "substack_urls": config.research_sources.blogs.substack_urls,
                "rss_urls": config.research_sources.blogs.rss_urls,
            },
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
