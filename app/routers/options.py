"""Options radar, agent thesis/postmortem, learning-loop, and strategy routes."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from app import deps
from app.actions.options import OptionsActions

router = APIRouter()


def _actions() -> OptionsActions:
    return OptionsActions(deps.load_config())


@router.get("/api/options-expiries")
def options_expiries() -> dict[str, Any]:
    return deps._table_payload("options_expiries")


@router.get("/api/options-chain")
def options_chain() -> dict[str, Any]:
    return deps._table_payload("options_chain")


@router.get("/api/options-payoff-scenarios")
def options_payoff_scenarios() -> dict[str, Any]:
    return deps._table_payload("options_payoff_scenarios")


@router.get("/api/options-provider-capabilities")
def options_provider_capabilities() -> dict[str, Any]:
    return deps._table_payload("options_provider_capabilities")


@router.get("/api/options-expiry-signals")
def options_expiry_signals() -> dict[str, Any]:
    return deps._table_payload("options_expiry_signals")


@router.get("/api/options-ticker-signals")
def options_ticker_signals() -> dict[str, Any]:
    return deps._table_payload("options_ticker_signals")


@router.get("/api/option-strategy-versions")
def option_strategy_versions() -> dict[str, Any]:
    return deps._table_payload("option_strategy_versions")


@router.get("/api/option-snapshot")
def option_snapshot() -> dict[str, Any]:
    return deps.user_state_table_payload(deps.options_radar_rows(deps.load_config(), "option_snapshot"))


@router.get("/api/option-features")
def option_features() -> dict[str, Any]:
    return deps.user_state_table_payload(deps.options_radar_rows(deps.load_config(), "option_features"))


@router.get("/api/stock-features")
def stock_features() -> dict[str, Any]:
    return deps._table_payload("stock_features")


@router.get("/api/option-radar-opportunities")
def option_radar_opportunities() -> dict[str, Any]:
    return deps.user_state_table_payload(deps.options_radar_rows(deps.load_config(), "option_radar_opportunity"))


@router.get("/api/options-radar/signals/{decision_id}")
def option_radar_signal_detail(decision_id: UUID) -> dict[str, Any]:
    detail = _actions().signal_detail(decision_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Options-radar signal not found")
    return detail


@router.post("/api/options-radar/signals/{decision_id}/paper-entry")
def stage_option_radar_paper_entry(
    decision_id: UUID,
    payload: deps.OptionPaperEntryInput,
    request: Request,
) -> dict[str, Any]:
    deps._require_local_request(request)
    try:
        result = _actions().stage_paper_entry(decision_id, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    deps._invalidate_context_cache()
    return result


@router.get("/api/agent-thesis")
def agent_thesis() -> dict[str, Any]:
    return deps._table_payload("agent_thesis")


@router.post("/api/agent-thesis")
def submit_agent_thesis(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    deps._require_local_request(request)
    try:
        result = _actions().submit_thesis(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    deps._invalidate_context_cache()
    return result


@router.get("/api/agent-thesis-requests")
def agent_thesis_requests() -> dict[str, Any]:
    return deps._table_payload("agent_thesis_request")


@router.get("/api/agent-thesis-validations")
def agent_thesis_validations() -> dict[str, Any]:
    return deps._table_payload("agent_thesis_validation")


@router.get("/api/agent-postmortem-requests")
def agent_postmortem_requests() -> dict[str, Any]:
    return deps._table_payload("agent_postmortem_request")


@router.get("/api/agent-postmortems")
def agent_postmortems() -> dict[str, Any]:
    return deps._table_payload("agent_postmortem")


@router.post("/api/agent-postmortems")
def submit_agent_postmortem(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    deps._require_local_request(request)
    try:
        result = _actions().submit_postmortem(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    deps._invalidate_context_cache()
    return result


@router.get("/api/candidate-events")
def candidate_events() -> dict[str, Any]:
    return deps.user_state_table_payload(deps.options_radar_rows(deps.load_config(), "candidate_event"))


@router.get("/api/radar-alerts")
def radar_alerts() -> dict[str, Any]:
    return deps._table_payload("radar_alert")


@router.post("/api/radar-alerts/{alert_id}/ack")
def acknowledge_radar_alert_endpoint(alert_id: str, request: Request) -> dict[str, Any]:
    deps._require_local_request(request)
    acknowledged = _actions().acknowledge_alert(alert_id)
    deps._invalidate_context_cache()
    if not acknowledged:
        raise HTTPException(status_code=404, detail="Radar alert not found")
    return acknowledged


@router.get("/api/candidate-event-marks")
def candidate_event_marks() -> dict[str, Any]:
    return deps._table_payload("candidate_event_mark")


@router.get("/api/candidate-event-attributions")
def candidate_event_attributions() -> dict[str, Any]:
    return deps._table_payload("candidate_event_attribution")


@router.get("/api/shadow-trades")
def shadow_trades() -> dict[str, Any]:
    return deps._table_payload("shadow_trade")


@router.get("/api/shadow-trade-marks")
def shadow_trade_marks() -> dict[str, Any]:
    return deps._table_payload("shadow_trade_mark")


@router.get("/api/radar-state-transitions")
def radar_state_transitions() -> dict[str, Any]:
    return deps._table_payload("radar_state_transition")


@router.get("/api/conviction-calibration")
def conviction_calibration() -> dict[str, Any]:
    return deps._table_payload("conviction_calibration")


@router.get("/api/vol-surface-features")
def vol_surface_features() -> dict[str, Any]:
    return deps._table_payload("vol_surface_features")


@router.get("/api/trade-journal")
def trade_journal() -> dict[str, Any]:
    return deps._table_payload("trade_journal")


@router.post("/api/trade-journal")
def create_trade_journal_entry(payload: deps.TradeJournalInput, request: Request) -> dict[str, Any]:
    deps._require_local_request(request)
    result = _actions().record_trade_journal(payload.model_dump())
    deps._invalidate_context_cache()
    return result


@router.get("/api/option-attributions")
def option_attributions() -> dict[str, Any]:
    return deps._table_payload("option_attribution")


@router.get("/api/missed-winner-events")
def missed_winner_events() -> dict[str, Any]:
    return deps._table_payload("missed_winner_event")


@router.get("/api/strategy-mutation-proposals")
def strategy_mutation_proposals() -> dict[str, Any]:
    return deps._table_payload("strategy_mutation_proposal")


@router.post("/api/strategy-mutation-proposals/{proposal_id}/promote")
def promote_strategy_mutation_endpoint(
    proposal_id: str,
    request: Request,
    payload: deps.StrategyPromotionInput | None = None,
) -> dict[str, Any]:
    deps._require_local_request(request)
    approved_by = payload.approved_by.strip() if payload else "joe"
    try:
        result = _actions().promote_strategy(proposal_id, approved_by=approved_by)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    deps._invalidate_context_cache()
    return result


@router.get("/api/strategy-backtests")
def strategy_backtests() -> dict[str, Any]:
    return deps._table_payload("strategy_backtest_result")


@router.get("/api/strategy-forward-tests")
def strategy_forward_tests() -> dict[str, Any]:
    return deps._table_payload("strategy_forward_test_result")


@router.get("/api/strategy-cohorts")
def strategy_cohorts() -> dict[str, Any]:
    return deps._table_payload("strategy_cohort_result")


@router.get("/api/exploration-gate-report")
def exploration_gate_report() -> dict[str, Any]:
    return deps._table_payload("exploration_gate_report")
