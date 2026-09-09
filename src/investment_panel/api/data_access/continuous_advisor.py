"""Application-owned reads for the continuous advisor surface."""

from __future__ import annotations

from typing import Any

from investment_panel.api.scheduler import scheduler_status
from investment_panel.settings import AppConfig
from investment_panel.infrastructure.postgres.authority import runtime_for_config
from investment_panel.infrastructure.postgres.continuous_advisor import ContinuousAdvisorRepository
from investment_panel.infrastructure.postgres.instruments import canonical_symbol
from investment_panel.infrastructure.postgres.thesis import thesis_monitor_rows


def overview(config: AppConfig) -> dict[str, Any]:
    rows = _continuous_rows(config)
    symbols = [str(row.get("symbol") or "").upper() for row in rows if row.get("symbol")]
    repository = _repository(config)
    return _overview(config, repository, rows, symbols)


def tickers(config: AppConfig) -> dict[str, Any]:
    rows = _continuous_rows(config)
    symbols = [str(row.get("symbol") or "").upper() for row in rows if row.get("symbol")]
    repository = _repository(config)
    briefs = _ticker_briefs(repository, rows, symbols, prompt_version=repository.active_prompt_version(config.agents.thesis_monitor.prompt_version))
    return {"rows": briefs, "count": len(briefs)}


def ticker(config: AppConfig, symbol: str) -> dict[str, Any] | None:
    normalized = normalize_symbol(symbol)
    rows = [row for row in _continuous_rows(config) if str(row.get("symbol") or "").upper() == normalized]
    repository = _repository(config)
    briefs = _ticker_briefs(repository, rows, [normalized], prompt_version=repository.active_prompt_version(config.agents.thesis_monitor.prompt_version))
    return briefs[0] if briefs else None


def runs(config: AppConfig, *, symbol: str | None, limit: int) -> dict[str, Any]:
    rows = _repository(config).list_runs(symbol=symbol, limit=limit)
    return {"rows": rows, "count": len(rows)}


def normalize_symbol(symbol: str) -> str:
    return canonical_symbol(symbol)


def run_detail(config: AppConfig, task_id: str) -> dict[str, Any] | None:
    return _repository(config).run_detail(task_id)


def replay(config: AppConfig, *, prompt_version: str | None) -> dict[str, Any]:
    scorecards = _repository(config).replay_scorecards(prompt_version=prompt_version)
    return {"scorecards": scorecards, "count": len(scorecards)}


def prompts(config: AppConfig) -> dict[str, Any]:
    return _repository(config).prompt_status(
        config.agents.thesis_monitor.prompt_version,
        scheduler=scheduler_status(config),
    )


def _repository(config: AppConfig) -> ContinuousAdvisorRepository:
    return ContinuousAdvisorRepository(runtime_for_config(config))


def _continuous_rows(config: AppConfig) -> list[dict[str, Any]]:
    return [
        row for row in thesis_monitor_rows(config, include_current_prices=False)
        if row.get("owned") or row.get("watched")
    ]


def _overview(
    config: AppConfig,
    repository: ContinuousAdvisorRepository,
    rows: list[dict[str, Any]],
    symbols: list[str],
) -> dict[str, Any]:
    return {
        "enabled": config.agents.thesis_monitor.continuous_enabled,
        "cadence_minutes": config.agents.thesis_monitor.continuous_cadence_minutes,
        "budget_usd": config.agents.thesis_monitor.continuous_budget_usd,
        "tickers": _ticker_briefs(
            repository,
            rows,
            symbols,
            prompt_version=repository.active_prompt_version(config.agents.thesis_monitor.prompt_version),
        ),
        "strategy_health": repository.prompt_status(
            config.agents.thesis_monitor.prompt_version,
            scheduler=scheduler_status(config),
        ),
    }


def _ticker_briefs(
    repository: ContinuousAdvisorRepository,
    rows: list[dict[str, Any]],
    symbols: list[str],
    *,
    prompt_version: str | None = None,
) -> list[dict[str, Any]]:
    latest = {str(item["symbol"]): item for item in repository.ticker_briefs(symbols=symbols, prompt_version=prompt_version)}
    output: list[dict[str, Any]] = []
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        if symbol in latest:
            output.append(latest[symbol])
            continue
        output.append({
            "symbol": symbol,
            "verdict": {
                "thesis": row.get("thesis") or "",
                "countercase": "No continuous-advisor verdict yet.",
                "forecasts": [],
                "invalidations": row.get("invalidation_rules") or [],
                "change_since_prior": "No continuous-advisor cycle recorded.",
                "next_review_trigger": "First successful continuous-advisor cycle",
                "next_review_at": None,
                "outcome_date": None,
                "evidence_freshness": {
                    "thesis": "available" if row.get("thesis") else "unavailable",
                    "source_evidence": "unavailable",
                },
                "blockers": ["no_continuous_advisor_run"],
            },
            "provenance": {"thesis_revision_id": row.get("revision_id"), "source": "thesis_monitor"},
        })
    return output


__all__ = ["normalize_symbol", "overview", "prompts", "replay", "run_detail", "runs", "ticker", "tickers"]
