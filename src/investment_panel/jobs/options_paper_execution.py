"""Run the deterministic Radar/QQQ paper-only execution loop."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from typing import Any

from investment_panel.core.config import AppConfig, load_config
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.options_paper_execution import OptionsPaperExecutionRepository
from investment_panel.database.options_analysis import refresh_options_radar
from investment_panel.database.options_experiments import EXPERIMENT_KIND, EXPERIMENT_VERSION, advance_experiment_shadows, experiment_candidate
from investment_panel.database.runtime import DatabaseRuntime, JOB_PROFILE


def run(config_path: str | None = "config.yaml") -> dict[str, Any]:
    config = load_config(config_path)
    settings = config.analysis.options_decision_system
    lanes = [
        lane
        for lane, enabled in (
            ("radar", settings.radar_paper_actions_enabled),
            ("qqq", settings.qqq_paper_actions_enabled),
        )
        if settings.options_paper_actions_enabled and enabled
    ]
    runtime = runtime_for_config(config)
    repository = OptionsPaperExecutionRepository(runtime)
    try:
        experiments = run_experiments(runtime, config)
    except Exception as error:
        experiments = {"status": "failed", "error": str(error)}
    # The switches are entry gates only.  ``process`` always manages existing
    # Radar and QQQ orders, including when one or both creation lanes are off.
    result = repository.process(
        enabled_lanes=lanes,
        sleeve_capital=settings.options_risk_sleeve_capital,
        daily_loss_halt_pct=settings.daily_loss_halt_pct,
        max_open_positions=settings.max_recovery_open_positions,
        decision_inbox_enabled=settings.decision_inbox_enabled,
    )
    return {**result, "status": "partial" if experiments["status"] == "failed" else result["status"],
            "experiments": experiments, "paper_only": True, "live_brokerage_submission": False}


def run_experiments(runtime: DatabaseRuntime, config: AppConfig, *, now: datetime | None = None) -> dict[str, Any]:
    reference = now or datetime.now(UTC)
    observed = advance_experiment_shadows(runtime, now=reference)
    settings = config.analysis.options_decision_system
    if not getattr(settings, "strategy_auto_promotion_enabled", False):
        return {"status": "disabled", "reason": "new_candidates_disabled", "observations": observed}
    with runtime.read(JOB_PROFILE) as connection:
        count = connection.execute(
            """SELECT count(*) AS count FROM analysis.strategy_revision candidate
               JOIN analysis.strategy_revision parent ON parent.id = candidate.supersedes_id AND parent.status = 'active'
               WHERE candidate.authority_group = 'options-radar-core' AND candidate.status IN ('candidate', 'testing', 'approved')""",
        ).fetchone()["count"]
        selected = None
        blocked = []
        for offset in range(0, count, 10):
            candidates = connection.execute(
                """SELECT candidate.id FROM analysis.strategy_revision candidate
                   JOIN analysis.strategy_revision parent ON parent.id = candidate.supersedes_id AND parent.status = 'active'
                   LEFT JOIN analysis.run run ON run.strategy_revision_id = candidate.id AND run.run_type = %s
                   WHERE candidate.authority_group = 'options-radar-core' AND candidate.status IN ('candidate', 'testing', 'approved')
                   GROUP BY candidate.id ORDER BY max(run.started_at) NULLS FIRST, candidate.id LIMIT 10 OFFSET %s""",
                [EXPERIMENT_KIND, offset],
            ).fetchall()
            for row in candidates:
                try:
                    selected = experiment_candidate(connection, row["id"], as_of=reference)
                except (ValueError, TypeError, OverflowError) as error:
                    blocked.append({"candidate_revision_id": row["id"], "reason": str(error)})
                else:
                    break
            if selected is not None:
                break
    if selected is None:
        return {"status": "skipped", "reason": "no_qualified_candidate", "observations": observed, "blocked": blocked}
    publication = refresh_options_radar(runtime, candidate_revision_id=selected["id"], config=config,
                                        options_risk_sleeve_capital=settings.options_risk_sleeve_capital, code_version=EXPERIMENT_VERSION)
    staged = []
    if publication.get("publication_id") and settings.options_paper_actions_enabled and settings.radar_paper_actions_enabled:
        staged = OptionsPaperExecutionRepository(runtime).stage_current_ready(
            enabled_lanes=["radar"], sleeve_capital=settings.options_risk_sleeve_capital,
            daily_loss_halt_pct=settings.daily_loss_halt_pct, max_open_positions=settings.max_recovery_open_positions,
            now=max(reference, datetime.now(UTC)), limit=1, experiment_publication_id=publication["publication_id"],
        )
    return {"status": "ok", "candidate_revision_id": selected["id"], "observations": observed,
            "publication": publication, "staged": staged, "blocked": blocked}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args(argv)
    print(json.dumps(run(args.config), default=str))


if __name__ == "__main__":  # pragma: no cover
    main()
