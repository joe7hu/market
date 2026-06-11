"""Options radar pipeline orchestration: the ``refresh_options_radar``
flywheel that sequences every stage.

The heavy lifting lives in focused submodules (registration, features,
candidates, opportunities, shadow, calibration, strategy_* ...); this module
wires them together. The package ``__init__`` re-exports the full public surface.
"""

from __future__ import annotations

from typing import Any

from investment_panel.core.options_radar.alerts import (refresh_radar_alerts)
from investment_panel.core.options_radar.attributions import (detect_missed_winners, refresh_option_attributions)
from investment_panel.core.options_radar.calibration import (refresh_conviction_calibration)
from investment_panel.core.options_radar.candidates import (generate_candidate_events)
from investment_panel.core.options_radar.constants import (DEFAULT_STRATEGY_VERSION)
from investment_panel.core.options_radar.features import (refresh_option_features, refresh_option_flow_features)
from investment_panel.core.options_radar.features_surface import (refresh_stock_features_for_option_snapshots, refresh_vol_surface_features)
from investment_panel.core.options_radar.marks import (refresh_candidate_event_attributions, refresh_candidate_event_marks)
from investment_panel.core.options_radar.opportunities import (refresh_option_radar_opportunities)
from investment_panel.core.options_radar.registration import (candidate_strategy_versions, register_default_strategy, register_strategy_families)
from investment_panel.core.options_radar.shadow import (apply_shadow_trade_exits, create_shadow_trades, mark_shadow_trades, refresh_shadow_trade_marks)
from investment_panel.core.options_radar.snapshots import (persist_option_snapshots, persist_spread_snapshots)
from investment_panel.core.options_radar.state import (refresh_radar_state_transitions)
from investment_panel.core.options_radar.strategy_backtest import (refresh_strategy_cohort_results)
from investment_panel.core.options_radar.strategy_promotion import (refresh_strategy_proposal_evaluations)
from investment_panel.core.options_radar.strategy_proposals import (generate_strategy_mutation_proposals)

def refresh_options_radar(
    con: Any,
    symbols: list[str] | None = None,
    *,
    strategy_version: str = DEFAULT_STRATEGY_VERSION,
    source: str | None = None,
    snapshot_time: str | None = None,
    include_agent_work: bool = True,
    include_learning: bool = True,
) -> dict[str, int]:
    """Refresh the deterministic radar tables from already-ingested market data.

    ``include_learning`` controls the heavy backtest/attribution machinery
    (shadow trades, marks, attributions, transitions, mutation proposals,
    cohorts) that reprocesses the full event-sourced history each run. The
    continuous scheduler runs with it OFF so the fast fresh-signal path
    (snapshots -> features -> candidates -> opportunities) stays cheap; the
    learning pass runs on a slower cadence.
    """

    register_default_strategy(con, strategy_version)
    register_strategy_families(con)
    snapshot_rows = persist_option_snapshots(con, symbols=symbols, source=source, snapshot_time=snapshot_time)
    spread_snapshot_rows = persist_spread_snapshots(con, symbols=symbols, source=source, snapshot_time=snapshot_time)
    feature_rows = refresh_option_features(con, symbols=symbols, source=source)
    flow_rows = refresh_option_flow_features(con, symbols=symbols, source=source)
    stock_rows = refresh_stock_features_for_option_snapshots(con, symbols=symbols, source=source)
    vol_surface_rows = refresh_vol_surface_features(con, symbols=symbols, source=source)
    # Generate candidates for the primary strategy and every registered archetype family
    # (each shadow-traded on its own strategy_version).
    candidate_rows = sum(
        generate_candidate_events(con, symbols=symbols, strategy_version=version, source=source)
        for version in candidate_strategy_versions(con, strategy_version)
    )
    if include_agent_work:
        from investment_panel.core.option_agent_thesis import refresh_option_agent_work

        agent_work = refresh_option_agent_work(con, strategy_version=strategy_version)
    else:
        agent_work = {
            "agent_thesis_requests": 0,
            "agent_thesis_requests_superseded": 0,
            "agent_theses_attached": 0,
            "agent_thesis_validations": 0,
        }
    if include_learning:
        shadow_rows = create_shadow_trades(con, strategy_version=strategy_version)
        marked_rows = mark_shadow_trades(con)
        mark_rows = refresh_shadow_trade_marks(con, strategy_version=strategy_version)
        candidate_mark_rows = refresh_candidate_event_marks(con, strategy_version=strategy_version)
        calibration_bins = refresh_conviction_calibration(con, strategy_version=strategy_version)
        candidate_attribution_rows = refresh_candidate_event_attributions(con, strategy_version=strategy_version)
        transition_rows = refresh_radar_state_transitions(con, strategy_version=strategy_version)
        exited_rows = apply_shadow_trade_exits(con, strategy_version=strategy_version)
        attribution_rows = refresh_option_attributions(con, strategy_version=strategy_version)
        missed_rows = detect_missed_winners(con, symbols=symbols, strategy_version=strategy_version, source=source)
        proposal_rows = generate_strategy_mutation_proposals(con, strategy_version=strategy_version)
    else:
        shadow_rows = marked_rows = mark_rows = candidate_mark_rows = candidate_attribution_rows = 0
        transition_rows = exited_rows = attribution_rows = missed_rows = proposal_rows = 0
        calibration_bins = 0
    if include_agent_work:
        from investment_panel.core.option_agent_postmortem import refresh_option_agent_postmortem_work

        postmortem_work = refresh_option_agent_postmortem_work(con, strategy_version=strategy_version)
    else:
        postmortem_work = {
            "agent_postmortem_requests": 0,
            "agent_postmortem_strategy_proposals": 0,
        }
    if include_learning:
        evaluation_rows = refresh_strategy_proposal_evaluations(con, strategy_version=strategy_version)
        cohort_rows = refresh_strategy_cohort_results(con, strategy_version=strategy_version)
    else:
        evaluation_rows = {}
        cohort_rows = 0
    opportunity_rows = refresh_option_radar_opportunities(con, symbols=symbols, strategy_version=strategy_version)
    alert_rows = refresh_radar_alerts(con, strategy_version=strategy_version)
    return {
        "option_snapshots": snapshot_rows,
        "spread_snapshots": spread_snapshot_rows,
        "option_features": feature_rows,
        "option_flow_features": flow_rows,
        "vol_surface_features": vol_surface_rows,
        "stock_features": stock_rows,
        "candidate_events": candidate_rows,
        **agent_work,
        "shadow_trades": shadow_rows,
        "shadow_trades_marked": marked_rows,
        "shadow_trade_marks": mark_rows,
        "candidate_event_marks": candidate_mark_rows,
        "conviction_calibration_bins": calibration_bins,
        "candidate_event_attributions": candidate_attribution_rows,
        "radar_state_transitions": transition_rows,
        "shadow_trades_exited": exited_rows,
        "option_attributions": attribution_rows,
        "missed_winners": missed_rows,
        "strategy_mutation_proposals": proposal_rows,
        **postmortem_work,
        **evaluation_rows,
        "strategy_cohorts": cohort_rows,
        "option_radar_opportunities": opportunity_rows,
        "radar_alerts": alert_rows,
    }
