"""API payload builders for panel views."""

from __future__ import annotations
from datetime import UTC, datetime
from math import isfinite
from typing import Any, Mapping
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
from investment_panel.core.decision import (
    InputLineage,
    OutcomeAttribution,
    TradePlan,
    apply_opportunity_rank_safety,
    build_ticker_decision,
    evaluate_ticker_policy,
    opportunity_episode_from_legacy,
    ticker_decision_brief,
)

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
    snapshot_row, signal_rows, rank_row, plan_row = _current_alpha_rows(
        tables, ticker_decision,
    )
    plan = _validated_trade_plan(plan_row, rank_row, ticker_decision, signal_rows)
    rank_current = _rank_is_current_for_decision(rank_row, ticker_decision)
    if plan is None or (plan.eligibility == "ACTIONABLE" and not rank_current):
        if not rank_current:
            reason = _rank_blocker(rank_row)
        else:
            reason = "trade_plan_missing"
        ticker_decision = apply_opportunity_rank_safety(
            ticker_decision,
            {"trade_rank_unavailable_reason": reason},
        )
        if rank_row is not None:
            rank_row = {
                **rank_row,
                "trade_rank": None,
                "trade_utility": None,
                "trade_rank_unavailable_reason": reason,
            }
        plan = None
    ticker_decision_payload = ticker_decision.model_dump(mode="json")
    ticker_decision_payload.update({
        "instrument_state_snapshot": snapshot_row,
        "alpha_signals": signal_rows,
        "opportunity_rank": rank_row,
        "trade_plan": plan.model_dump(mode="json") if plan is not None else None,
    })
    outcome_attributions, attribution_blocker = select_current_outcome_attributions(
        tables.get("outcome_attribution") or [], ticker_decision_payload,
    )
    learning = ticker_learning_payload(
        ticker_decision_payload,
        tables.get("ticker_outcomes") or [],
        tables,
        outcome_attributions=outcome_attributions,
        attribution_blocker=attribution_blocker,
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
        "opportunity_episode": ticker_decision_payload["opportunity_episode"],
        "expressions": ticker_decision_payload["expressions"],
        "data_requests": ticker_decision_payload["data_requests"],
        "learning_history": ticker_decision_payload["learning_history"],
        "instrument_state_snapshot": snapshot_row,
        "alpha_signals": signal_rows,
        "opportunity_rank": rank_row,
        "trade_plan": plan.model_dump(mode="json") if plan is not None else None,
        "outcome_attributions": outcome_attributions,
        "learning": learning,
        "decision_revision": ticker_decision_payload["decision_revision"],
        "found": bool(dossier["coverage"].get("present") or dossier["coverage"]["live"]),
    }


def _current_alpha_rows(
    tables: dict[str, list[dict[str, Any]]],
    decision: Any,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, Any] | None, dict[str, Any] | None]:
    symbol = decision.ticker.strip().upper()
    revision = decision.decision_revision
    episode_id = decision.opportunity_episode_id

    def matches(row: dict[str, Any], *, episode: bool = True) -> bool:
        row_symbol = _payload_symbol(row)
        if row_symbol and row_symbol != symbol:
            return False
        if row.get("decision_revision") and str(row["decision_revision"]) != revision:
            return False
        if episode and row.get("opportunity_episode_id") and str(row["opportunity_episode_id"]) != episode_id:
            return False
        return True

    expected_snapshot_id = str(
        (decision.instrument_state_snapshot or {}).get("snapshot_id") or ""
    )
    snapshots = [
        row for row in tables.get("instrument_state_snapshot") or []
        if matches(row, episode=False)
        and (not expected_snapshot_id or str(row.get("snapshot_id") or "") == expected_snapshot_id)
    ]
    signals = [row for row in tables.get("alpha_signal") or [] if matches(row)]
    ranks = [row for row in tables.get("opportunity_rank") or [] if matches(row)]
    plans = [row for row in tables.get("trade_plan") or [] if matches(row)]
    snapshot = max(snapshots, key=lambda row: str(row.get("as_of") or row.get("input_cutoff") or ""), default=None)
    rank = max(ranks, key=lambda row: str(row.get("published_at") or row.get("publication_published_at") or ""), default=None)
    plan = plans[0] if len(plans) == 1 else None
    return snapshot, signals, rank, plan


def _rank_blocker(rank: dict[str, Any] | None) -> str:
    if rank is None:
        return "opportunity_rank_missing"
    reason = str(rank.get("trade_rank_unavailable_reason") or "").strip()
    if reason:
        return reason
    if not bool(rank.get("evaluated_universe_complete")):
        return "ranking_universe_incomplete"
    return "opportunity_rank_identity_mismatch"


def _validated_trade_plan(
    plan: dict[str, Any] | None,
    rank: dict[str, Any] | None,
    decision: Any,
    signals: list[dict[str, Any]],
) -> TradePlan | None:
    if plan is None or rank is None:
        return None
    try:
        value = TradePlan.model_validate(plan)
        if value.eligibility == "ACTIONABLE" and not _rank_is_current_for_decision(rank, decision):
            return None
        if value.ticker != decision.ticker or value.opportunity_episode_id != decision.opportunity_episode_id:
            return None
        if value.decision_revision != decision.decision_revision or value.policy_version != decision.policy_version:
            return None
        if value.selected_expression_kind.value != str(rank.get("selected_expression_kind") or ""):
            return None
        if value.selected_expression_identity != str(rank.get("selected_expression_identity") or ""):
            return None
        if value.rank_id != str(rank.get("rank_id") or ""):
            return None
        if value.alpha_signal_id != str(rank.get("alpha_signal_id") or ""):
            return None
        if value.portfolio_impact_id != str(rank.get("portfolio_impact_id") or ""):
            return None
        if value.market_snapshot_id != str(rank.get("market_snapshot_id") or ""):
            return None
        if value.market_state_publication_id != str(rank.get("market_state_publication_id") or ""):
            return None
        if not value.publication_id or not rank.get("publication_id") or value.publication_id != rank.get("publication_id"):
            return None
        if value.alpha_signal_id not in {str(row.get("signal_id") or "") for row in signals}:
            return None
        if value.selected_expression.model_dump(mode="json") != (decision.selected_expression.model_dump(mode="json") if decision.selected_expression else None):
            return None
        if value.input_lineage != tuple(decision.input_lineage):
            return None
        if decision.resolution is None or decision.resolution.trade_plan_id != value.trade_plan_id:
            return None
        return value
    except (TypeError, ValueError, KeyError):
        return None


def _rank_is_current_for_decision(rank: dict[str, Any] | None, decision: Any) -> bool:
    if rank is None:
        return False
    selected = decision.selected_expression
    if selected is None:
        return False
    if str(rank.get("decision_revision") or "") != decision.decision_revision:
        return False
    if str(rank.get("opportunity_episode_id") or "") != decision.opportunity_episode_id:
        return False
    if str(rank.get("selected_expression_kind") or "") != selected.kind.value:
        return False
    try:
        from investment_panel.core.decision import trade_expression_identity

        if str(rank.get("selected_expression_identity") or "") != trade_expression_identity(selected):
            return False
        if not bool(rank.get("evaluated_universe_complete")):
            return False
        return (
            int(rank.get("trade_rank")) > 0
            and isfinite(float(rank.get("trade_utility")))
            and float(rank.get("trade_utility")) > 0
            and not rank.get("trade_rank_unavailable_reason")
        )
    except (TypeError, ValueError, OverflowError):
        return False


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


def _canonical_opportunity_fields(ticker_decision: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Read the canonical episode once and expose compatibility projections."""

    try:
        episode = opportunity_episode_from_legacy(ticker_decision)
        ticker = str(ticker_decision.get("ticker") or ticker_decision.get("symbol") or "").upper()
        if ticker and episode.ticker != ticker:
            raise ValueError("episode ticker mismatch")
        revision = str(ticker_decision.get("decision_revision") or "")
        if revision and episode.decision_revision != revision:
            raise ValueError("episode revision mismatch")
        policy = str(ticker_decision.get("policy_version") or "")
        if policy and episode.policy_version != policy:
            raise ValueError("episode policy mismatch")
        raw_cutoff = ticker_decision.get("cutoff") or ticker_decision.get("as_of")
        if raw_cutoff:
            parsed_cutoff = raw_cutoff
            if isinstance(parsed_cutoff, str):
                parsed_cutoff = datetime.fromisoformat(parsed_cutoff.replace("Z", "+00:00"))
            if parsed_cutoff.tzinfo is None or parsed_cutoff.astimezone(UTC) != episode.cutoff:
                raise ValueError("episode cutoff mismatch")
        serialized = episode.model_dump(mode="json")
        return {
            "opportunity_episode": serialized,
            "episode_id": episode.episode_id,
            "opportunity_episode_id": episode.episode_id,
            "decision_revision": episode.decision_revision,
            "policy_version": episode.policy_version,
            "cutoff": serialized["cutoff"],
            "input_lineage": serialized["input_lineage"],
        }, None
    except (TypeError, ValueError, KeyError):
        # Legacy or partial rows remain readable, but cannot become an
        # actionable compatibility result without the canonical episode.
        return {}, "opportunity_lineage_invalid"


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
    episode_fields, episode_blocker = _canonical_opportunity_fields(ticker_decision)
    episode = episode_fields.get("opportunity_episode") or {}
    context_blockers = _portfolio_context_blockers(ticker_decision)
    rank_blocker = _opportunity_rank_blocker(ticker_decision)
    if rank_blocker:
        context_blockers.append(rank_blocker)
    context_blockers = list(dict.fromkeys(context_blockers))
    selected_from_episode = episode.get("selected_expression") or {}
    selected_kind = str(
        selected_from_episode.get("kind")
        or (ticker_decision.get("selected_expression") or {}).get("kind")
        or ""
    )
    option_expression = expressions.get(selected_kind) if selected_kind in {
        "CALL", "PUT", "DEBIT_SPREAD", "CASH_SECURED_PUT",
    } else None
    if not isinstance(option_expression, dict) or option_expression.get("status") not in {"eligible", "blocked"}:
        option_expression = None
    candidate = (
        _compatibility_option_candidate(ticker_decision, option_expression, capital, context_blockers)
        if option_expression and not episode_blocker
        else {}
    )
    if candidate:
        # The legacy payload is intentionally not merged. The ticker
        # expression owns the contracts, quote package, thesis, and readiness;
        # this route only preserves the old envelope shape.
        candidate["ticker_decision_revision"] = ticker_decision.get("decision_revision")
        candidate["policy_version"] = ticker_decision.get("policy_version")
        candidate.update(episode_fields)
        if isinstance(candidate.get("ticket"), dict):
            candidate["ticket"].update({
                key: episode_fields[key]
                for key in (
                    "episode_id", "opportunity_episode_id", "decision_revision",
                    "policy_version", "cutoff", "input_lineage", "opportunity_episode",
                )
                if key in episode_fields
            })
            candidate["ticket"]["trade_expression"] = option_expression
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
            *context_blockers,
        ]))
        candidate["state_reasons"] = candidate["blockers"]
        payload["strongest_candidate"] = candidate
    else:
        # A legacy candidate without a current ticker expression is not a
        # valid compatibility result.
        payload.pop("strongest_candidate", None)
    payload["ticker_decision_revision"] = ticker_decision.get("decision_revision")
    payload.update(episode_fields)
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
    lineage_blockers = [episode_blocker] if episode_blocker else []
    payload["decision_truth"] = {
        **existing_truth,
        "symbol": ticker_decision.get("ticker") or payload.get("symbol"),
        "as_of": ticker_decision.get("as_of"),
        "candidate_state": capital.get("action"),
        "route_verdict": capital.get("action"),
        "readiness_state": "PAPER_READY" if candidate and candidate.get("execution_ready") else "WATCH",
        "execution_state": "PAPER_ONLY",
        **episode_fields,
        "primary_blocker": episode_blocker or existing_truth.get("primary_blocker"),
        "blockers": list(dict.fromkeys([
            *list(existing_truth.get("blockers") or []),
            *(list(candidate.get("blockers") or []) if candidate else []),
            *ticker_blockers,
            *lineage_blockers,
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


def _portfolio_context_blockers(ticker_decision: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    snapshot = ticker_decision.get("market_state_snapshot")
    if not isinstance(snapshot, dict):
        blockers.append("market_state_missing")
    else:
        if snapshot.get("availability") != "available":
            blockers.append("market_state_unavailable")
        blockers.extend(str(item) for item in snapshot.get("blockers") or [])
    if not ticker_decision.get("market_state_publication_id"):
        blockers.append("market_state_publication_missing")
    selected = ticker_decision.get("selected_expression") or {}
    selected_kind = selected.get("kind") if isinstance(selected, dict) else selected
    impacts = ticker_decision.get("portfolio_impacts")
    impact = impacts.get(str(selected_kind)) if isinstance(impacts, dict) else None
    if not isinstance(impact, dict):
        blockers.append("portfolio_impact_missing")
    else:
        if impact.get("availability") != "available":
            blockers.append("portfolio_impact_unavailable")
        blockers.extend(str(item) for item in impact.get("blockers") or [])
    policy = ticker_decision.get("risk_policy_snapshot")
    if not isinstance(policy, dict):
        blockers.append("risk_policy_snapshot_missing")
    else:
        blockers.extend(str(item) for item in policy.get("blockers") or [])
    return list(dict.fromkeys(item for item in blockers if item))


def _opportunity_rank_blocker(ticker_decision: dict[str, Any]) -> str | None:
    rank = ticker_decision.get("opportunity_rank")
    if not isinstance(rank, dict):
        return "opportunity_rank_missing"
    try:
        selected = ticker_decision.get("selected_expression") or {}
        if str(rank.get("selected_expression_kind") or "") != str(selected.get("kind") or ""):
            return "opportunity_rank_identity_mismatch"
        from investment_panel.core.decision import trade_expression_identity

        if str(rank.get("selected_expression_identity") or "") != trade_expression_identity(selected):
            return "opportunity_rank_identity_mismatch"
        if not bool(rank.get("evaluated_universe_complete")):
            return "ranking_universe_incomplete"
        rank_utility = float(rank.get("trade_utility"))
        if int(rank.get("trade_rank")) <= 0 or not isfinite(rank_utility) or rank_utility <= 0:
            return str(rank.get("trade_rank_unavailable_reason") or "opportunity_rank_unavailable")
        if rank.get("trade_rank_unavailable_reason"):
            return str(rank["trade_rank_unavailable_reason"])
    except (TypeError, ValueError, OverflowError):
        return str(rank.get("trade_rank_unavailable_reason") or "opportunity_rank_unavailable")
    return None


def _compatibility_option_candidate(
    ticker_decision: dict[str, Any],
    expression: dict[str, Any],
    capital: dict[str, Any],
    context_blockers: list[str] | None = None,
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
    blockers.extend(context_blockers or [])
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
    *,
    outcome_attributions: list[dict[str, Any]] | None = None,
    attribution_blocker: str | None = None,
) -> dict[str, Any]:
    """Compose disagreement, expression tournament, mistakes, and policy gates."""

    tables = tables or {}
    fundamental = dict(ticker_decision.get("fundamental") or {})
    expressions = dict(ticker_decision.get("expressions") or {})
    canonical_mode = outcome_attributions is not None or "outcome_attribution" in tables
    source_attributions = (
        list(outcome_attributions)
        if outcome_attributions is not None
        else list(tables.get("outcome_attribution") or [])
    )
    display_outcomes = (
        [_attribution_learning_row(row) for row in source_attributions]
        if canonical_mode
        else list(outcomes)
    )
    display_outcomes = [row for row in display_outcomes if row]
    episode_ids = {
        str(row.get("ticker_decision_id") or row.get("decision_id") or "")
        for row in display_outcomes
        if row.get("ticker_decision_id") or row.get("decision_id")
    }
    horizon_episode_ids = {
        (str(row.get("ticker_decision_id") or row.get("decision_id") or ""), str(row.get("horizon") or ""))
        for row in display_outcomes
        if (row.get("ticker_decision_id") or row.get("decision_id")) and row.get("horizon")
    }
    scenarios = fundamental.get("scenarios") or []
    if canonical_mode:
        strategy_learning = evaluate_ticker_policy(source_attributions, canonical_only=True)
        if not source_attributions:
            strategy_learning["blockers"] = list(dict.fromkeys([
                *strategy_learning.get("blockers", []),
                attribution_blocker or "canonical_outcome_attribution_missing",
            ]))
            strategy_learning["automatic_promotion"] = False
            strategy_learning["status"] = "collecting"
    else:
        global_policy_row = next(iter(tables.get("ticker_policy_learning") or []), {})
        global_policy_rows = global_policy_row.get("episodes")
        policy_rows = (
            list(global_policy_rows)
            if isinstance(global_policy_rows, list)
            else [{**row, "scenarios": row.get("scenarios") or scenarios} for row in display_outcomes]
        )
        strategy_learning = evaluate_ticker_policy(policy_rows)
    payload = {
        "independent_episode_count": len(episode_ids) or (1 if display_outcomes else 0),
        "independent_horizon_episode_count": len({
            (str(row.get("ticker_decision_id") or row.get("decision_id") or ""), str(row.get("horizon") or ""))
            for row in display_outcomes
            if row.get("ticker_decision_id") or row.get("decision_id")
        }),
        "effective_sample_count": len(horizon_episode_ids) or (1 if display_outcomes else 0),
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
                    for row in display_outcomes
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
            for row in display_outcomes if row.get("error_type") or row.get("mistake_card")
        ],
        "strategy_learning": strategy_learning,
    }
    if canonical_mode:
        payload["outcome_authority"] = "outcome-attribution.v1"
        payload["outcome_authority_blocker"] = attribution_blocker
        payload["outcome_evidence_label"] = (
            "REALIZED_PAPER"
            if any(
                str((_mapping_value(row.get("paper_execution"))).get("status") or "").upper() == "EXITED"
                for row in source_attributions
            )
            else "COUNTERFACTUAL_MARKS_ONLY" if source_attributions else "UNAVAILABLE"
        )
    return payload


def select_current_outcome_attributions(
    rows: list[dict[str, Any]], ticker_decision: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], str | None]:
    """Select only the current plan's exact canonical attribution units."""

    plan = ticker_decision.get("trade_plan")
    if not isinstance(plan, Mapping) or not plan.get("trade_plan_id"):
        return [], "trade_plan_missing"
    symbol = str(ticker_decision.get("ticker") or ticker_decision.get("symbol") or "").upper()
    plan_id = str(plan["trade_plan_id"])
    matches = [
        dict(row) for row in rows
        if str(row.get("trade_plan_id") or "") == plan_id
        and str(row.get("ticker") or row.get("symbol") or "").upper() == symbol
    ]
    if not matches:
        return [], "outcome_attribution_missing"
    expected = {
        "trade_plan_publication_id": plan.get("publication_id"),
        "opportunity_episode_id": plan.get("opportunity_episode_id"),
        "decision_revision": plan.get("decision_revision"),
        "policy_version": plan.get("policy_version"),
        "selected_expression_kind": plan.get("selected_expression_kind"),
        "selected_expression_identity": plan.get("selected_expression_identity"),
        "rank_id": plan.get("rank_id"),
        "alpha_signal_id": plan.get("alpha_signal_id"),
        "portfolio_impact_id": plan.get("portfolio_impact_id"),
        "market_snapshot_id": plan.get("market_snapshot_id"),
        "market_state_publication_id": plan.get("market_state_publication_id"),
    }
    for row in matches:
        if str(row.get("contract_version") or "") != "outcome-attribution.v1":
            return [], "outcome_attribution_contract_invalid"
        if any(str(row.get(key) or "") != str(value or "") for key, value in expected.items()):
            return [], "outcome_attribution_lineage_mismatch"
        if not row.get("outcome_attribution_id"):
            return [], "outcome_attribution_id_missing"
        try:
            attribution = OutcomeAttribution.model_validate(row)
            expected_lineage = tuple(
                InputLineage.model_validate(item) for item in (plan.get("input_lineage") or [])
            )
            if attribution.decision_input_lineage != expected_lineage:
                return [], "outcome_attribution_lineage_mismatch"
        except (TypeError, ValueError, KeyError):
            return [], "outcome_attribution_invalid"
    stable_keys = [str(row.get("stable_unit_key") or "") for row in matches]
    if not all(stable_keys) or len(set(stable_keys)) != len(stable_keys):
        return [], "outcome_attribution_unit_duplicated"
    units = {
        (str(row.get("horizon") or "").upper(), int(row.get("horizon_sessions") or 0))
        for row in matches
    }
    expected_units = {
        ("TACTICAL", 1), ("TACTICAL", 5), ("TACTICAL", 20),
        ("FUNDAMENTAL", 63), ("FUNDAMENTAL", 126), ("FUNDAMENTAL", 252),
    }
    if units != expected_units:
        return [], "outcome_attribution_units_incomplete"
    return sorted(matches, key=lambda row: (str(row.get("horizon") or ""), int(row.get("horizon_sessions") or 0))), None


def _attribution_learning_row(row: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(row)
    counterfactuals = value.get("counterfactuals") or {}
    expressions = value.get("all_expression_counterfactuals") or value.get("expression_outcomes") or {}
    stock = _mapping_value(counterfactuals.get("STOCK"))
    cash = _mapping_value(counterfactuals.get("CASH"))
    trend = _mapping_value(counterfactuals.get("TREND"))
    metadata = dict(value.get("learning_metadata") or {})
    metadata.update({
        "expression_returns": {
            str(kind): _number_value(_mapping_value(item).get("gross_return"))
            for kind, item in expressions.items()
        },
        "cost_adjusted_selected_return": _number_value(value.get("selected_net_return")),
        "cost_adjusted_stock_counterfactual_return": _number_value(stock.get("cost_adjusted_return")),
        "cost_adjusted_cash_return": _number_value(cash.get("cost_adjusted_return")),
        "trend_counterfactual_return": _number_value(trend.get("cost_adjusted_return")),
    })
    return {
        "outcome_attribution_id": value.get("outcome_attribution_id"),
        "trade_plan_id": value.get("trade_plan_id"),
        "ticker_decision_id": value.get("trade_plan_id"),
        "ticker": value.get("ticker"),
        "as_of": value.get("decision_cutoff"),
        "horizon": str(value.get("horizon") or "").lower(),
        "horizon_sessions": value.get("horizon_sessions"),
        "state": str(value.get("state") or "").lower(),
        "selected_return": value.get("selected_gross_return"),
        "stock_counterfactual_return": stock.get("gross_return"),
        "alternate_counterfactual_return": _mapping_value(counterfactuals.get("ALTERNATE_EXPRESSION")).get("gross_return"),
        "cash_return": cash.get("gross_return"),
        "metadata": metadata,
        "scenarios": metadata.get("scenarios") or [],
        "error_type": value.get("mistake_classification"),
        "mistake_card": value.get("mistake_card") or {},
        "evidence_state": value.get("evidence_state"),
        "sample_eligible": bool(value.get("sample_eligible")),
        "promotion_eligible": bool(value.get("promotion_eligible")),
        "paper_execution": value.get("paper_execution"),
    }


def _mapping_value(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _number_value(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


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
