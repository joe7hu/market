"""API payload builders for panel views."""

from __future__ import annotations
from datetime import UTC, datetime
from typing import Any
from app.scheduler import scheduler_status
from investment_panel.core.panel import (
    build_ticker_dossier,
    dashboard_payload as core_dashboard_payload,
    panel_snapshot_payload as core_panel_snapshot_payload,
)

from app.data_access.types import PanelData
from app.data_access.coerce import int_value as _int_value, jsonable
from investment_panel.core.agent_config import ThesisMonitorAgentConfig
from investment_panel.core.config import AppConfig, OptionAgentConfig
from investment_panel.core.decision import build_ticker_decision, ticker_decision_brief

DEFAULT_AGENT_THESIS_REQUEST_LIMIT = 12



def status_payload(panel_data: PanelData) -> dict[str, Any]:
    return {
        "ready": panel_data.status.ready,
        "message": panel_data.status.message,
        "source": panel_data.status.source,
        "metadata": jsonable(panel_data.metadata),
    }




def runtime_metadata(config: AppConfig) -> dict[str, Any]:
    agents = config.agents
    option_agent = agents.option_agent
    thesis_monitor = agents.thesis_monitor
    return {
        "agents": {
            # Unified single-pass agent runtime. Sub-limits keep thesis/postmortem
            # counts visible even though one consolidated call covers both.
            "option_agent": _agent_runtime_metadata(option_agent, default_limit=8) | {
                "thesis_limit": _int_value(option_agent.thesis_limit, 8),
                "postmortem_limit": _int_value(option_agent.postmortem_limit, 4),
                "request_cap": DEFAULT_AGENT_THESIS_REQUEST_LIMIT,
                "queue_policy": "current_ranked_candidates_plus_ondemand",
                "cadence": "launchd_weekdays_0815_or_ondemand",
                "schedule_owner": "launchd",
                "max_runs_per_day": _int_value(option_agent.max_runs_per_day, 1),
                "mode": "consolidated_single_pass",
            },
            "thesis_monitor": _thesis_monitor_runtime_metadata(thesis_monitor),
        },
        "options_radar": {
            "deterministic_cadence": "hourly",
            "agent_cadence": "daily_premarket",
        },
        "scheduler": scheduler_status(config),
    }




def _agent_runtime_metadata(
    config: OptionAgentConfig,
    *,
    default_limit: int,
) -> dict[str, Any]:
    command = str(config.command or "")
    enabled = bool(config.enabled)
    configured = bool(command.strip())
    return {
        "enabled": enabled,
        "configured": configured,
        "active": enabled and configured,
        "status": "active" if enabled and configured else "paused",
        "limit": _int_value(config.thesis_limit + config.postmortem_limit, default_limit),
        "timeout_seconds": _int_value(config.timeout_seconds, 120),
    }


def _thesis_monitor_runtime_metadata(config: ThesisMonitorAgentConfig) -> dict[str, Any]:
    enabled = bool(getattr(config, "enabled", False))
    provider = str(getattr(config, "provider", None) or "codex")
    model = str(getattr(config, "model", None) or "")
    configured_providers = {"codex", "deepseek"}
    return {
        "enabled": enabled,
        "configured": provider in configured_providers,
        "active": enabled,
        "status": "active" if enabled else "paused",
        "provider": provider,
        "model": model or "gpt-5.6-luna",
        "reasoning_effort": str(getattr(config, "reasoning_effort", None) or "high"),
        "prompt_version": str(getattr(config, "prompt_version", None) or "thesis_v3_20260725"),
        "concurrency": _int_value(getattr(config, "concurrency", None), 2),
        "evidence_items_per_symbol": _int_value(getattr(config, "evidence_items_per_symbol", None), 12),
        "cadence": "preopen_plus_material_events",
        "preopen_enabled": bool(getattr(config, "preopen_enabled", False)),
        "material_event_enabled": bool(getattr(config, "material_event_enabled", False)),
        "debounce_minutes": _int_value(getattr(config, "debounce_minutes", None), 30),
        "max_material_runs_per_symbol_per_day": _int_value(getattr(config, "max_material_runs_per_symbol_per_day", None), 2),
        "authority": "research_ranking_only",
    }




def table_payload(panel_data: PanelData, table_name: str) -> dict[str, Any]:
    rows = panel_data.rows(table_name)
    return {"rows": rows, "count": len(rows), "status": status_payload(panel_data)}




def signals_payload(panel_data: PanelData) -> dict[str, Any]:
    rows = panel_data.rows("signals") or panel_data.rows("candidates")
    return {"rows": rows, "count": len(rows), "status": status_payload(panel_data)}




def dashboard_payload(panel_data: PanelData) -> dict[str, Any]:
    return core_dashboard_payload(status_payload(panel_data), panel_data.rows)




def panel_snapshot_payload(panel_data: PanelData, scope: str, offset: int = 0, limit: int | None = None) -> dict[str, Any]:
    return core_panel_snapshot_payload(
        scope=scope,
        status=status_payload(panel_data),
        rows_for_table=panel_data.rows,
        offset=offset,
        limit=limit,
    )


def watchlist_section_payload(panel_data: PanelData, scope: str, offset: int = 0, limit: int | None = None) -> dict[str, Any]:
    return panel_snapshot_payload(panel_data, scope, offset=offset, limit=limit)




def ticker_payload(panel_data: PanelData, ticker: str) -> dict[str, Any]:
    """Section-organized per-ticker dossier (the single authoritative API model).

    Loads the per-ticker read-model tables, synthesizes the decision brief, and
    composes both into one ``dossier`` of normalized sections (quote,
    fundamentals, estimates, technicals, options, ownership, sources, thesis,
    portfolio, decision) plus a coverage overview. Each section carries an
    explicit ``coverage.status`` so callers can degrade gracefully.
    """

    normalized_ticker = ticker.upper()
    tables = {
        name: [row for row in rows if _payload_symbol(row) in {"", normalized_ticker}]
        for name, rows in panel_data.tables.items()
    }
    decision_brief = ticker_decision_brief(normalized_ticker, tables)
    dossier = build_ticker_dossier(normalized_ticker, tables, decision_brief)
    ticker_decision = build_ticker_decision(
        normalized_ticker,
        tables,
        as_of=_ticker_as_of(panel_data, tables),
    )
    ticker_decision_payload = ticker_decision.model_dump(mode="json")
    learning = ticker_learning_payload(ticker_decision_payload, tables.get("ticker_outcomes") or [])
    # Keep the established dossier sections readable by existing clients while
    # making the typed ticker decision the authoritative new surface.
    dossier["decision"] = ticker_decision_summary(ticker_decision_payload)
    return {
        "symbol": normalized_ticker,
        "ticker": normalized_ticker,
        "status": status_payload(panel_data),
        "as_of": dossier["coverage"].get("as_of"),
        "dossier": dossier,
        "ticker_decision": ticker_decision_payload,
        "capital_action": ticker_decision_payload["capital_action"],
        "expressions": ticker_decision_payload["expressions"],
        "data_requests": ticker_decision_payload["data_requests"],
        "learning_history": ticker_decision_payload["learning_history"],
        "learning": learning,
        "decision_revision": ticker_decision_payload["decision_revision"],
        "found": bool(dossier["coverage"].get("present") or dossier["coverage"]["live"]),
    }


def ticker_decision_summary(ticker_decision: dict[str, Any]) -> dict[str, Any]:
    """Provide a compatibility summary without reviving ambiguous actions."""

    tactical = dict(ticker_decision.get("tactical") or {})
    fundamental = dict(ticker_decision.get("fundamental") or {})
    capital = dict(ticker_decision.get("capital_action") or {})
    invalidation = fundamental.get("invalidation") or tactical.get("invalidation") or {}
    return {
        "verdict": {
            "action": capital.get("action"),
            "summary": capital.get("rationale"),
            "confidence": fundamental.get("confidence") or tactical.get("confidence"),
            "freshness": ticker_decision.get("as_of"),
            "owned": capital.get("owned"),
        },
        "setup": {
            "entry_zone": _range_summary(tactical.get("entry_range") or fundamental.get("entry_range")),
            "target_range": _range_summary(tactical.get("target_range") or fundamental.get("target_range")),
            "timeframe": f"{tactical.get('horizon', 'TACTICAL')} + {fundamental.get('horizon', 'FUNDAMENTAL')}",
            "catalyst": capital.get("catalyst"),
            "review_date": capital.get("expires_at") or fundamental.get("expiry_date") or tactical.get("expiry_date"),
        },
        "risk_plan": {"invalidation": invalidation.get("statement") or invalidation.get("value")},
        "evidence_for": fundamental.get("evidence_for") or tactical.get("evidence_for") or [],
        "evidence_against": fundamental.get("evidence_against") or tactical.get("evidence_against") or [],
        "unknowns": [request.get("field") for request in ticker_decision.get("data_requests") or []],
    }


def _range_summary(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    low, high = value.get("low"), value.get("high")
    if low is None or high is None:
        return None
    return str(low) if low == high else f"{low}–{high}"


def option_decision_adapter(
    ticker_decision: dict[str, Any],
    legacy_payload: dict[str, Any],
) -> dict[str, Any]:
    """Adapt the ticker decision to the legacy options brief contract.

    The old options read model still supplies quote and ticket mechanics.  It
    no longer supplies an independent directional thesis: the action, horizon,
    invalidation, scenarios, and expression identity come from the ticker
    decision passed here.
    """

    payload = dict(legacy_payload)
    capital = dict(ticker_decision.get("capital_action") or {})
    expressions = dict(ticker_decision.get("expressions") or {})
    option_expression = next(
        (
            value for key, value in expressions.items()
            if key in {"CALL", "PUT", "DEBIT_SPREAD", "CASH_SECURED_PUT"}
            and isinstance(value, dict)
            and value.get("status") in {"eligible", "blocked"}
        ),
        None,
    )
    if option_expression is not None:
        candidate = dict(payload.get("strongest_candidate") or {})
        candidate["ticker_decision_revision"] = ticker_decision.get("decision_revision")
        candidate["ticker_expression"] = option_expression
        candidate["ticker_thesis"] = {
            "tactical": ticker_decision.get("tactical"),
            "fundamental": ticker_decision.get("fundamental"),
            "capital_action": capital,
        }
        candidate["blockers"] = list(dict.fromkeys([
            *list(candidate.get("blockers") or []),
            *[request.get("field") for request in ticker_decision.get("data_requests") or []],
        ]))
        payload["strongest_candidate"] = candidate
    payload["ticker_decision_revision"] = ticker_decision.get("decision_revision")
    payload["summary"] = {
        **dict(payload.get("summary") or {}),
        "ticker_action": capital.get("action"),
        "ticker_rationale": capital.get("rationale"),
        "ticker_selected_expression": (ticker_decision.get("selected_expression") or {}).get("kind"),
    }
    payload["decision_truth"] = {
        **dict(payload.get("decision_truth") or {}),
        "symbol": ticker_decision.get("ticker") or payload.get("symbol"),
        "as_of": ticker_decision.get("as_of"),
        "candidate_state": capital.get("action"),
        "route_verdict": capital.get("action"),
        "readiness_state": "READY" if option_expression and option_expression.get("status") == "eligible" else "WAITING",
        "execution_state": "PAPER_ONLY",
        "blockers": [request.get("field") for request in ticker_decision.get("data_requests") or []],
        "next_action": capital.get("rationale"),
        "route_version": ticker_decision.get("decision_contract_version", "ticker-decision.v1"),
        "evidence_refs": [
            {"source": item.get("source"), "reference": item.get("reference"), "statement": item.get("statement")}
            for item in (ticker_decision.get("fundamental", {}).get("evidence_for") or [])
        ],
    }
    return payload


def ticker_learning_payload(
    ticker_decision: dict[str, Any],
    outcomes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compose disagreement, expression tournament, and mistake cards."""

    fundamental = dict(ticker_decision.get("fundamental") or {})
    expressions = dict(ticker_decision.get("expressions") or {})
    episode_ids = {
        str(row.get("ticker_decision_id") or row.get("decision_id") or "")
        for row in outcomes
        if row.get("ticker_decision_id") or row.get("decision_id")
    }
    return {
        "independent_episode_count": len(episode_ids) or (1 if outcomes else 0),
        "disagreement": {
            "strongest_bull_case": _first_statement(fundamental.get("evidence_for")),
            "strongest_bear_case": _first_statement(fundamental.get("evidence_against")),
            "resolving_fact": (fundamental.get("fact_that_would_flip") or {}).get("statement"),
        },
        "expression_tournament": [
            {
                "expression_kind": kind,
                "selected": bool(value.get("selected")),
                "status": value.get("status"),
                "planned_loss": value.get("planned_loss"),
                "lower_confidence_expectancy": value.get("lower_confidence_expectancy"),
                "outcomes": outcomes,
            }
            for kind, value in expressions.items()
            if isinstance(value, dict)
        ],
        "mistake_cards": [
            {
                "horizon": row.get("horizon"),
                "horizon_sessions": row.get("horizon_sessions"),
                "error_type": row.get("error_type"),
                "card": row.get("mistake_card") or {},
            }
            for row in outcomes if row.get("error_type") or row.get("mistake_card")
        ],
    }


def _first_statement(values: Any) -> str | None:
    if not isinstance(values, list) or not values:
        return None
    first = values[0]
    return str(first.get("statement") or "") if isinstance(first, dict) else str(first)


def _ticker_as_of(panel_data: PanelData, tables: dict[str, list[dict[str, Any]]]) -> datetime:
    candidate = panel_data.metadata.get("as_of")
    if isinstance(candidate, datetime):
        return candidate if candidate.tzinfo else candidate.replace(tzinfo=UTC)
    values: list[datetime] = []
    for rows in tables.values():
        for row in rows:
            for key in ("available_at", "observed_at", "as_of", "published_at", "updated_at"):
                value = row.get(key)
                if isinstance(value, datetime):
                    values.append(value if value.tzinfo else value.replace(tzinfo=UTC))
                elif value:
                    try:
                        values.append(datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC))
                    except (TypeError, ValueError):
                        continue
    return max(values, default=datetime.now(UTC))


def _payload_symbol(row: dict[str, Any]) -> str:
    return str(row.get("symbol") or row.get("ticker") or "").upper()
