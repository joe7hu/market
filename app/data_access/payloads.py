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
from investment_panel.core.decision import build_ticker_decision, evaluate_ticker_policy, ticker_decision_brief

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
    learning = ticker_learning_payload(
        ticker_decision_payload,
        tables.get("ticker_outcomes") or [],
        tables,
    )
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
        "resolution": ticker_decision_payload["resolution"],
        "policy_version": ticker_decision_payload["policy_version"],
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
    selected_kind = str((ticker_decision.get("selected_expression") or {}).get("kind") or "")
    option_expression = expressions.get(selected_kind) if selected_kind in {
        "CALL", "PUT", "DEBIT_SPREAD", "CASH_SECURED_PUT",
    } else None
    if not isinstance(option_expression, dict) or option_expression.get("status") not in {"eligible", "blocked"}:
        option_expression = None
    candidate = _compatibility_option_candidate(ticker_decision, option_expression, capital) if option_expression else {}
    if candidate:
        # The legacy payload is intentionally not merged. The ticker
        # expression owns the contracts, quote package, thesis, and readiness;
        # this route only preserves the old envelope shape.
        candidate["ticker_decision_revision"] = ticker_decision.get("decision_revision")
        candidate["policy_version"] = ticker_decision.get("policy_version")
        candidate["resolution"] = ticker_decision.get("resolution")
        candidate["ticker_expression"] = option_expression
        candidate["ticker_thesis"] = {
            "tactical": ticker_decision.get("tactical"),
            "fundamental": ticker_decision.get("fundamental"),
            "capital_action": capital,
        }
        candidate["thesis"] = {"ticker": ticker_decision.get("ticker"), "capital_action": capital}
        candidate["forecast"] = {"scenarios": option_expression.get("scenarios") or []}
        candidate["paper_state"] = "PAPER_READY" if candidate.get("execution_ready") else "WATCH"
        candidate["blockers"] = list(dict.fromkeys([
            *list(candidate.get("blockers") or []),
            *[request.get("field") for request in ticker_decision.get("data_requests") or []],
        ]))
        candidate["state_reasons"] = candidate["blockers"]
        payload["strongest_candidate"] = candidate
    else:
        # A legacy candidate without a current ticker expression is not a
        # valid compatibility result.
        payload.pop("strongest_candidate", None)
    payload["ticker_decision_revision"] = ticker_decision.get("decision_revision")
    payload["summary"] = {
        **dict(payload.get("summary") or {}),
        "ticker_action": capital.get("action"),
        "resolution": ticker_decision.get("resolution"),
        "policy_version": ticker_decision.get("policy_version"),
        "ticker_rationale": capital.get("rationale"),
        "ticker_selected_expression": (ticker_decision.get("selected_expression") or {}).get("kind"),
    }
    existing_truth = dict(payload.get("decision_truth") or {})
    ticker_blockers = [request.get("field") for request in ticker_decision.get("data_requests") or []]
    payload["decision_truth"] = {
        **existing_truth,
        "symbol": ticker_decision.get("ticker") or payload.get("symbol"),
        "as_of": ticker_decision.get("as_of"),
        "candidate_state": capital.get("action"),
        "route_verdict": capital.get("action"),
        "readiness_state": "PAPER_READY" if candidate and candidate.get("execution_ready") else "WATCH",
        "execution_state": "PAPER_ONLY",
        "blockers": list(dict.fromkeys([
            *list(existing_truth.get("blockers") or []),
            *(list(candidate.get("blockers") or []) if candidate else []),
            *ticker_blockers,
        ])),
        "next_action": capital.get("rationale"),
        "route_version": ticker_decision.get("decision_contract_version", "ticker-decision.v1"),
        "evidence_refs": [
            {"source": item.get("source"), "reference": item.get("reference"), "statement": item.get("statement")}
            for item in (ticker_decision.get("fundamental", {}).get("evidence_for") or [])
        ],
    }
    readiness = dict(payload.get("readiness") or {})
    tactical = dict(ticker_decision.get("tactical") or {})
    fundamental = dict(ticker_decision.get("fundamental") or {})
    invalidation = fundamental.get("invalidation") or tactical.get("invalidation") or {}
    readiness["thesis"] = {
        "eligible": bool(option_expression and option_expression.get("status") == "eligible"),
        "present": bool(option_expression),
        "revision": ticker_decision.get("decision_revision"),
        "invalidation": invalidation.get("statement") or invalidation.get("value"),
        "blocker": (ticker_decision.get("data_requests") or [{}])[0].get("field") if ticker_decision.get("data_requests") else None,
        "direction": fundamental.get("stance") or tactical.get("stance"),
    }
    readiness["next_required_action"] = capital.get("rationale") or "Use the ticker capital action."
    payload["readiness"] = readiness
    payload["state"] = "PAPER_READY" if candidate and candidate.get("execution_ready") else "WATCH"
    return payload


def _compatibility_option_candidate(
    ticker_decision: dict[str, Any],
    expression: dict[str, Any],
    capital: dict[str, Any],
) -> dict[str, Any]:
    """Build the old candidate envelope without creating a second thesis."""

    raw_legs = expression.get("legs")
    if not isinstance(raw_legs, list):
        return {}
    legs: list[dict[str, Any]] = []
    for raw_leg in raw_legs:
        if not isinstance(raw_leg, dict):
            continue
        try:
            contract_id = int(raw_leg.get("contract_id"))
            strike = float(raw_leg.get("strike"))
        except (TypeError, ValueError):
            continue
        quote_time = raw_leg.get("quote_time") or raw_leg.get("observed_at")
        legs.append({
            "contract_id": contract_id,
            "option_type": str(raw_leg.get("option_type") or "").lower(),
            "side": str(raw_leg.get("side") or "long").lower(),
            "strike": strike,
            "bid": _finite_float(raw_leg.get("bid")),
            "ask": _finite_float(raw_leg.get("ask")),
            "observed_at": quote_time,
            "bid_size": raw_leg.get("bid_size"),
            "ask_size": raw_leg.get("ask_size"),
            "quote_age_seconds": _finite_float(raw_leg.get("quote_age_seconds")),
            "open_interest": raw_leg.get("open_interest"),
            "volume": raw_leg.get("volume"),
            "provider_iv": _finite_float(raw_leg.get("provider_iv") or raw_leg.get("iv")),
            "provider_delta": _finite_float(raw_leg.get("provider_delta") or raw_leg.get("delta")),
        })
    if not legs:
        return {}
    expiration = raw_legs[0].get("expiration") if isinstance(raw_legs[0], dict) else None
    if not expiration:
        return {}
    blockers = [str(request.get("field")) for request in ticker_decision.get("data_requests") or []]
    status = str(expression.get("status") or "unavailable")
    quantity = expression.get("quantity")
    try:
        quantity_int = int(quantity) if quantity is not None else None
    except (TypeError, ValueError):
        quantity_int = None
    ready = status == "eligible" and not blockers and quantity_int is not None and quantity_int > 0
    entry = expression.get("entry_range") if isinstance(expression.get("entry_range"), dict) else {}
    lower_expectancy = _finite_float(expression.get("lower_confidence_expectancy"))
    net_expectancy = _finite_float(expression.get("net_expected_value_per_loss_dollar"))
    decision_id = str(ticker_decision.get("decision_revision") or ticker_decision.get("ticker") or "ticker-decision")
    symbol = str(ticker_decision.get("ticker") or "")
    structure = str(expression.get("kind") or "option").lower()
    max_loss = _finite_float(expression.get("max_loss_per_unit"))
    invalidation = (
        (ticker_decision.get("fundamental") or {}).get("invalidation")
        or (ticker_decision.get("tactical") or {}).get("invalidation")
        or {}
    )
    ticket_legs = [
        {
            **leg,
            "contract_id": str(leg["contract_id"]),
            "quote_time": leg.get("observed_at"),
        }
        for leg in legs
    ]
    return {
        "decision_id": decision_id,
        "relative_value_id": 0,
        "paper_state": "PAPER_READY" if ready else "WATCH",
        "discovery_lane": "ticker",
        "structure": structure,
        "expiration": expiration,
        "strike": legs[0]["strike"],
        "option_type": legs[0]["option_type"],
        "legs": legs,
        "conservative_entry": {"price": _finite_float(entry.get("low")), "fill_basis": "worst_side_quote"},
        "one_unit_max_loss": _finite_float(expression.get("max_loss_per_unit")),
        "fair_value_interval": {"low": None, "high": None},
        "expected_value_interval": {"low": lower_expectancy, "high": net_expectancy},
        "uncertainty": {},
        "modeled_net_edge": net_expectancy,
        "quote_quality": {"spread_pct": expression.get("spread_pct"), "fill_probability": expression.get("fill_probability")},
        "liquidity": {"liquidity_score": expression.get("liquidity_score")},
        "thesis": {"ticker": ticker_decision.get("ticker"), "capital_action": capital},
        "state_reasons": blockers,
        "blockers": blockers,
        "reassessment_date": (ticker_decision.get("fundamental") or {}).get("expiry_date"),
        "comparable_exact_structure_outcomes": {},
        "forecast": {"scenarios": expression.get("scenarios") or []},
        "execution_ready": ready,
        "strategy_route": {"route_version": ticker_decision.get("decision_contract_version", "ticker-decision.v1"), "selected_structure": expression.get("kind"), "ai_can_override": False},
        "market_regime": {},
        "ticket": {
            "ticket_version": 1,
            "decision_id": decision_id,
            "decision_revision": ticker_decision.get("decision_revision"),
            "lane": "ticker",
            "symbol": symbol,
            "state": "PAPER_READY" if ready else "WATCH",
            "structure": structure,
            "expiration": expiration,
            "legs": ticket_legs,
            "entry": {
                "limit_price": _finite_float(entry.get("low")),
                "maximum_chase_price": _finite_float(entry.get("high")),
            },
            "risk": {
                "one_unit_max_loss": max_loss,
                "recommended_quantity": quantity_int or 0,
                "total_risk": (max_loss * quantity_int) if max_loss is not None and quantity_int is not None else 0,
                "blockers": blockers,
            },
            "thesis": {
                "summary": capital.get("rationale"),
                "invalidation": invalidation.get("statement") or invalidation.get("value"),
            },
            "forecast": {
                "interval": expression.get("scenarios") or [],
                "expected_value": net_expectancy,
                "lower_confidence_expected_value": lower_expectancy,
            },
            "entry_price": _finite_float(entry.get("low")),
            "quantity": quantity,
            "max_loss": max_loss,
            "blockers": blockers,
            "required_next_action": "stage_paper_entry" if ready else "collect_data",
            "provenance": {
                "decision_contract_version": ticker_decision.get("decision_contract_version", "ticker-decision.v1"),
                "decision_revision": ticker_decision.get("decision_revision"),
            },
            "paper_only": True,
        },
        "paper_only": True,
    }


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


def ticker_learning_payload(
    ticker_decision: dict[str, Any],
    outcomes: list[dict[str, Any]],
    tables: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Compose disagreement, expression tournament, mistakes, and policy gates."""

    tables = tables or {}
    fundamental = dict(ticker_decision.get("fundamental") or {})
    expressions = dict(ticker_decision.get("expressions") or {})
    episode_ids = {
        str(row.get("ticker_decision_id") or row.get("decision_id") or "")
        for row in outcomes
        if row.get("ticker_decision_id") or row.get("decision_id")
    }
    horizon_episode_ids = {
        (str(row.get("ticker_decision_id") or row.get("decision_id") or ""), str(row.get("horizon") or ""))
        for row in outcomes
        if (row.get("ticker_decision_id") or row.get("decision_id")) and row.get("horizon")
    }
    scenarios = fundamental.get("scenarios") or []
    global_policy_row = next(iter(tables.get("ticker_policy_learning") or []), {})
    global_policy_rows = global_policy_row.get("episodes")
    policy_rows = (
        list(global_policy_rows)
        if isinstance(global_policy_rows, list)
        else [{**row, "scenarios": row.get("scenarios") or scenarios} for row in outcomes]
    )
    strategy_learning = evaluate_ticker_policy(policy_rows)
    return {
        "independent_episode_count": len(episode_ids) or (1 if outcomes else 0),
        "independent_horizon_episode_count": len({
            (str(row.get("ticker_decision_id") or row.get("decision_id") or ""), str(row.get("horizon") or ""))
            for row in outcomes
            if row.get("ticker_decision_id") or row.get("decision_id")
        }),
        "effective_sample_count": len(horizon_episode_ids) or (1 if outcomes else 0),
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
                "outcomes": [
                    {
                        **row,
                        "expression_return": (
                            dict(row.get("metadata") or {}).get("expression_returns") or {}
                        ).get(kind),
                    }
                    for row in outcomes
                ],
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
        "strategy_learning": strategy_learning,
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
