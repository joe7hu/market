"""Small PostgreSQL-only refresh compositions used by the live app."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Callable

from investment_panel.core.config import AppConfig, load_config
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.retention import RetentionRepository
from investment_panel.database.today_analysis import refresh_today_publication
from investment_panel.database.market_analysis import refresh_market_publication
from investment_panel.database.outcomes import OutcomeRepository
from investment_panel.database.portfolio import PortfolioLoopRepository
from investment_panel.jobs import (
    refresh_options_radar,
    run_option_agents,
    run_thesis_monitor,
    ticker_decisions,
    update_market_data,
)


def publish_decisions(config_path: str | None = None) -> dict[str, Any]:
    """Rebuild deterministic publications from the latest normalized facts."""

    config = load_config(config_path)
    runtime = runtime_for_config(config)
    cutoff = datetime.now(UTC)
    options = refresh_options_radar.run_deterministic_only(config_path)
    market = refresh_market_publication(
        runtime,
        now=cutoff,
        configured_watchlist=config.watchlist,
        configured_watchlist_as_of=cutoff,
    )
    decision_cutoff = _market_publication_cutoff(market, fallback=cutoff)
    tickers = ticker_decisions.publish(
        config_path,
        as_of=decision_cutoff,
        market_state_publication_id=_market_state_publication_id(market),
    )
    outcomes = _refresh_option_outcomes(runtime, config)
    today = refresh_today_publication(runtime, now=decision_cutoff)
    allocation = PortfolioLoopRepository(runtime).refresh_authoritative_allocation(as_of=decision_cutoff)
    status = "ok" if all(
        str(row.get("status")) == "ok"
        for row in (tickers, today, market)
    ) else "partial"
    return {
        "status": status,
        "database": "postgresql",
        "options_radar": options,
        "ticker_decisions": tickers,
        "outcomes": outcomes,
        "today": today,
        "portfolio_allocation": {"allocation_id": allocation.allocation_id, "status": allocation.status},
        "market": market,
    }


def scheduled_preopen(config_path: str | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    """Publish once in the New York premarket window; otherwise skip cheaply."""

    from investment_panel.core.decision import is_us_market_day
    from zoneinfo import ZoneInfo

    reference = (now or datetime.now(UTC)).astimezone(ZoneInfo("America/New_York"))
    if not is_us_market_day(reference.date()):
        return {"status": "skipped", "ok": True, "database": "postgresql", "reason": "market_closed"}
    if not (4 <= reference.hour < 10):
        return {"status": "skipped", "ok": True, "database": "postgresql", "reason": "outside_premarket_window"}
    config = load_config(config_path)
    runtime = runtime_for_config(config)
    with runtime.read() as connection:
        already_published = connection.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM app.publication publication
                JOIN app.publication_content_item item ON item.publication_id = publication.id
                WHERE publication.scope = 'today' AND publication.status = 'published'
                  AND publication.published_at::date = %s
                  AND item.model_name = 'preopen_daily_brief'
                  AND item.payload->>'status' = 'agent_generated'
            ) AS published
            """,
            [reference.date()],
        ).fetchone()["published"]
    if already_published:
        return {"status": "skipped", "ok": True, "database": "postgresql", "reason": "already_published"}
    result = refresh_today_publication(
        runtime, now=reference.astimezone(UTC), use_agent_narrative=True,
        agent_model=config.agents.thesis_monitor.model,
        reasoning_effort=config.agents.thesis_monitor.reasoning_effort,
    )
    return {"ok": result.get("status") == "ok", "database": "postgresql", **result}


def premarket(config_path: str | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    """Publish the daily decision snapshot from already-ingested raw facts."""

    from investment_panel.core.decision import is_us_market_day
    from zoneinfo import ZoneInfo

    reference = now or datetime.now(UTC)
    if not is_us_market_day(reference.astimezone(ZoneInfo("America/New_York")).date()):
        return {
            "ok": True, "status": "skipped", "database": "postgresql",
            "cadence": "daily_premarket", "reason": "market_closed",
        }

    config = load_config(config_path)
    runtime = runtime_for_config(config)
    before_agents = refresh_options_radar.run(config_path)
    agents = run_option_agents.run(config_path)
    thesis_monitor = run_thesis_monitor.run(config_path, trigger="preopen")
    after_agents = refresh_options_radar.run_deterministic_only(config_path)
    cutoff = reference.astimezone(UTC)
    market = refresh_market_publication(
        runtime,
        now=cutoff,
        configured_watchlist=config.watchlist,
        configured_watchlist_as_of=cutoff,
    )
    decision_cutoff = _market_publication_cutoff(market, fallback=cutoff)
    tickers = ticker_decisions.publish(
        config_path,
        as_of=decision_cutoff,
        market_state_publication_id=_market_state_publication_id(market),
    )
    outcomes = _refresh_option_outcomes(runtime, config)
    today = refresh_today_publication(
        runtime, now=decision_cutoff, use_agent_narrative=True,
        agent_model=config.agents.thesis_monitor.model,
        reasoning_effort=config.agents.thesis_monitor.reasoning_effort,
    )
    option_ready = any(str(result.get("status") or "").lower() == "ok" for result in (before_agents, after_agents))
    thesis_status = str(thesis_monitor.get("status") or "failed").lower()
    publication_ready = all(str(result.get("status") or "").lower() == "ok" for result in (tickers, today, market))
    status = "ok" if option_ready and publication_ready and thesis_status in {"ok", "skipped"} else "partial"
    return {
        "ok": status == "ok",
        "status": status,
        "database": config.database.url,
        "cadence": "daily_premarket",
        "before_agents": before_agents,
        "agents": agents,
        "thesis_monitor": thesis_monitor,
        "after_agents": after_agents,
        "ticker_decisions": tickers,
        "outcomes": outcomes,
        "today": today,
        "market": market,
    }


def _refresh_option_outcomes(runtime: Any, config: AppConfig) -> dict[str, Any]:
    """Allow deterministic paper-policy promotion when the configured switch is on."""

    enabled = bool(config.analysis.options_decision_system.strategy_auto_promotion_enabled)
    repository = OutcomeRepository(runtime)
    return repository.refresh(strategy_auto_promotion_enabled=enabled)


def full(config_path: str | None = None, *, continue_on_error: bool = True) -> dict[str, Any]:
    """Run bounded option ingestion, analysis, agents, and retention.

    Raw providers are independent steps so one unavailable broker cannot prevent
    publication from the latest good facts. PostgreSQL job rows provide the
    single-flight boundary; no file lock or application shutdown is required.
    """

    from investment_panel.jobs import (
        snapshot_database,
        update_arco_sources,
        update_broker_sources,
        update_content_sources,
        update_disclosure_sources,
        update_ibkr_options,
        update_market_events,
        update_robinhood_options,
    )

    config = load_config(config_path)
    publication_cutoff: datetime | None = None
    market_state_publication_id: str | None = None
    market_state_visible_at: datetime | None = None

    def bounded_cutoff() -> datetime:
        nonlocal publication_cutoff
        if publication_cutoff is None:
            publication_cutoff = datetime.now(UTC)
        return publication_cutoff

    def publish_market() -> dict[str, Any]:
        nonlocal market_state_publication_id, market_state_visible_at
        cutoff = bounded_cutoff()
        result = refresh_market_publication(
            runtime_for_config(config),
            now=cutoff,
            configured_watchlist=config.watchlist,
            configured_watchlist_as_of=cutoff,
        )
        market_state_publication_id = _market_state_publication_id(result)
        market_state_visible_at = _market_publication_cutoff(result, fallback=bounded_cutoff())
        return result

    steps: list[tuple[str, bool, Callable[[], dict[str, Any]]]] = [
        ("arco_sources", False, lambda: update_arco_sources.run(config_path)),
        ("market_data", False, lambda: update_market_data.run(config_path, publish=False)),
        ("content_sources", False, lambda: update_content_sources.run(config_path)),
        ("market_events", False, lambda: update_market_events.run(config_path)),
        ("disclosures", False, lambda: update_disclosure_sources.run(config_path)),
        ("robinhood_options", False, lambda: update_robinhood_options.run(config_path)),
        ("ibkr_options", False, lambda: update_ibkr_options.run(config_path)),
        ("broker_sources", False, lambda: update_broker_sources.run(config_path)),
        ("options_radar", True, lambda: refresh_options_radar.run(config_path)),
        ("market_publication", True, publish_market),
        ("ticker_decisions", True, lambda: ticker_decisions.publish(
            config_path,
            as_of=market_state_visible_at or bounded_cutoff(),
            market_state_publication_id=market_state_publication_id,
        )),
        (
            "option_outcomes",
            False,
            lambda: _refresh_option_outcomes(runtime_for_config(config), config),
        ),
        ("option_agents", True, lambda: run_option_agents.run(config_path)),
        ("thesis_monitor", False, lambda: run_thesis_monitor.run(config_path, trigger="preopen")),
        ("today_publication", True, lambda: refresh_today_publication(
            runtime_for_config(config), now=market_state_visible_at or bounded_cutoff()
        )),
        ("retention", True, lambda: RetentionRepository(runtime_for_config(config)).prune()),
        ("database_snapshot", False, lambda: snapshot_database.run(config_path)),
    ]
    results: list[dict[str, Any]] = []
    failed: list[str] = []
    warnings: list[str] = []
    for name, required, runner in steps:
        started = datetime.now(UTC)
        try:
            result = runner()
            status = str(result.get("status") or "ok").lower()
            if name in {"robinhood_options", "ibkr_options"}:
                step_failed = status not in {"ok", "partial"}
            elif name == "broker_sources":
                step_failed = status != "ok"
            elif name in {"options_radar", "today_publication", "market_publication"}:
                step_failed = status != "ok"
            else:
                step_failed = status in {"error", "failed", "unsafe_config"}
            results.append({"name": name, "ok": not step_failed, "started_at": started, "result": result})
            if step_failed:
                (failed if required else warnings).append(name)
                if required and not continue_on_error:
                    break
        except Exception as exc:  # provider boundary is reflected in job status
            results.append({"name": name, "ok": False, "started_at": started, "error": f"{type(exc).__name__}: {exc}"})
            (failed if required else warnings).append(name)
            if required and not continue_on_error:
                break
    status = "failed" if failed and not any(row["ok"] for row in results) else "partial" if failed or warnings else "ok"
    return {
        "ok": not failed,
        "status": status,
        "database": "postgresql",
        "started_at": results[0]["started_at"] if results else datetime.now(UTC),
        "finished_at": datetime.now(UTC),
        "failed_steps": failed,
        "warning_steps": warnings,
        "steps": results,
    }


def _market_state_publication_id(result: dict[str, Any]) -> str | None:
    if str(result.get("status") or "").lower() != "ok":
        return None
    return str(result.get("publication_id") or "") or None


def _market_publication_cutoff(result: dict[str, Any], *, fallback: datetime) -> datetime:
    """Use the first cutoff at which the exact Market publication is visible."""

    value = result.get("published_at")
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        return max(fallback, datetime.now(UTC))
    if value.tzinfo is None:
        raise ValueError("market publication timestamp must be timezone-aware")
    return max(fallback, value.astimezone(UTC))


def main_publish() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    print(json.dumps(publish_decisions(args.config), indent=2, default=str))


def main_preopen() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    print(json.dumps(scheduled_preopen(args.config), indent=2, default=str))
