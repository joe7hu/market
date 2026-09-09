"""Independent-episode scorecards for the option lanes.

The scorecard is deliberately conservative.  It reports raw observation volume
separately, but computes metrics and gates from one canonical row per stable
episode. Radar retains the active incumbent's prospective observation across
refreshes. It never turns an incomplete denominator into a win rate or EV.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import sqrt
from statistics import mean, stdev
from typing import Any, Iterable

from psycopg.errors import QueryCanceled

from investment_panel.infrastructure.postgres.opportunity_episodes import (
    SCORECARD_TRUTH_VERSION,
    SCORECARD_TRUTH_PREFIX,
    has_current_scorecard_truth,
)
from investment_panel.infrastructure.postgres.runtime import API_PROFILE, DatabaseRuntime


LANES = frozenset({"radar", "qqq", "recovery"})
MIN_RESOLVED_EPISODES = 30
MIN_TRADING_DAYS = 20
CURRENT_TRUTH_DEFECT = "current_observation_truth_contract_missing"
NON_DEFECT_QUARANTINE_REASONS = frozenset({
    "quality_gated", "promotion_ineligible", "generic_mark_not_execution_grade",
    "outcome_not_resolved_execution_grade", "quality_status_sizing_blocked",
})


class OpportunityScorecardRepository:
    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def scorecard(self, *, lane: str, window_days: int = 120, as_of: datetime | None = None) -> dict[str, Any]:
        normalized_lane = str(lane).strip().lower()
        if normalized_lane not in LANES:
            raise ValueError("lane must be radar, qqq, or recovery")
        if not 1 <= window_days <= 3_650:
            raise ValueError("window must be between 1 and 3650 days")
        reference = as_of or datetime.now(UTC)
        if reference.tzinfo is None:
            raise ValueError("scorecard time must be timezone-aware")
        since = reference - timedelta(days=window_days)
        query_defects: dict[str, int] = {}
        try:
            rows = self._recovery_rows(since, reference) if normalized_lane == "recovery" else self._decision_rows(normalized_lane, since, reference)
        except QueryCanceled:
            rows = []
            query_defects["scorecard_query_timeout"] = 1
        scope = self._scope_counts(normalized_lane, since, reference)
        return _scorecard(
            lane=normalized_lane,
            as_of=reference,
            window_days=window_days,
            raw_observation_count=scope["observed"],
            episodes=rows,
            external_defects=(
                {
                    **({"legacy_or_unversioned_truth_contract": scope["quarantined"]}
                       if scope["quarantined"] else {}),
                    **({CURRENT_TRUTH_DEFECT: scope["current_truth_missing"]}
                       if scope.get("current_truth_missing") else {}),
                    **query_defects,
                }
                or None
            ),
            externally_quarantined_episode_count=scope["quarantined"],
            excluded_legacy_observation_count=scope.get("excluded_legacy_observations", 0),
            excluded_legacy_independent_episode_count=scope.get("excluded_legacy_episodes", 0),
        )

    def _scope_counts(self, lane: str, since: datetime, reference: datetime) -> dict[str, int]:
        """Return cheap cohort health counts without reading publication payloads.

        Historic records are quarantined before the scorecard follows a
        publication join. This keeps an INVALID / REBUILDING scorecard fast on
        the large legacy cohort while new versioned writes use the exact join.
        """

        if lane == "radar":
            return self._radar_scope_counts(since, reference)
        if lane == "recovery":
            relation = "analysis.option_opportunity_observation"
            available_at = "available_at"
            lane_clause = "lane = 'recovery'"
        else:
            relation = "analysis.decision"
            available_at = "as_of"
            lane_clause = "kind = 'option' AND lane = %s"
        parameters: list[Any] = [since, reference]
        if lane != "recovery":
            parameters.insert(0, lane)
        with self.runtime.read(API_PROFILE) as connection:
            row = connection.execute(
                f"""
                SELECT count(*) AS observed,
                       count(*) FILTER (
                           WHERE calibration_cohort IS NULL
                              OR calibration_cohort NOT LIKE 'option-scorecard-truth-v1:%%'
                       ) AS quarantined
                FROM {relation}
                WHERE {lane_clause}
                  AND {available_at} >= %s
                  AND {available_at} <= %s
                """,
                parameters,
            ).fetchone()
        return {"observed": int(row["observed"] or 0), "quarantined": int(row["quarantined"] or 0)}

    def _radar_scope_counts(self, since: datetime, reference: datetime) -> dict[str, int]:
        """Keep excluded history separate from defects in the current writer.

        A new observation that loses its truth marker is still a current
        defect when its run or retained shadow identifies the experiment.
        """
        with self.runtime.read(API_PROFILE) as connection:
            row = connection.execute(
                """
                WITH retained AS MATERIALIZED (
                    SELECT decision_id FROM analysis.shadow_trade
                    WHERE source_kind = 'options_paper_experiment' AND created_at <= %(reference)s
                ), runs AS MATERIALIZED (
                    SELECT id, coalesce(inputs ? 'observation', false) AS current_observation
                    FROM analysis.run WHERE run_type = 'options-radar'
                ), scoped AS NOT MATERIALIZED (
                    SELECT decision.episode_key,
                           (decision.calibration_cohort IS NULL OR decision.calibration_cohort
                               NOT LIKE 'option-scorecard-truth-v1:%%') AS missing_truth,
                           (run.current_observation OR retained.decision_id IS NOT NULL) AS current_observation
                    FROM analysis.decision decision
                    JOIN runs run ON run.id = decision.run_id
                    LEFT JOIN retained ON retained.decision_id = decision.id
                    WHERE decision.kind = 'option' AND decision.lane = 'radar'
                      AND decision.as_of BETWEEN %(since)s AND %(reference)s
                      AND decision.strategy_revision_id = (
                          SELECT id FROM analysis.strategy_revision
                          WHERE authority_group = 'options-radar-core' AND status = 'active'
                            AND created_at <= %(reference)s AND promoted_at <= %(reference)s
                      )
                )
                SELECT count(*) AS observed,
                       count(*) FILTER (WHERE missing_truth AND NOT current_observation) AS excluded_legacy_observations,
                       -- Sort only excluded keys, not the full capture history.
                       (SELECT count(DISTINCT episode_key) FROM scoped
                        WHERE missing_truth AND NOT current_observation) AS excluded_legacy_episodes,
                       count(*) FILTER (WHERE missing_truth AND current_observation) AS current_truth_missing
                FROM scoped
                """,
                {"since": since, "reference": reference},
            ).fetchone()
        return {"quarantined": 0, **{key: int(value or 0) for key, value in row.items()}}

    def _recovery_rows(self, since: datetime, reference: datetime) -> list[dict[str, Any]]:
        with self.runtime.read(API_PROFILE) as connection:
            rows = connection.execute(
                """
                SELECT observation.episode_key, observation.available_at, observation.selection_stage,
                       observation.sample_eligible, observation.quarantine_reason,
                       observation.data_status, observation.outcome_classification,
                       observation.realized_return, observation.lower_confidence_expectancy,
                       observation.entry_fill_at, observation.exit_fill_at,
                       observation.calibration_cohort,
                       EXISTS (
                         SELECT 1
                         FROM app.publication publication
                         JOIN app.publication_content_item item ON item.publication_id = publication.id
                         WHERE publication.status IN ('published', 'superseded')
                           -- Recovery has its own immutable publication owner.
                           -- Do not probe the large Radar publication history
                           -- for every recovery observation.
                           AND publication.scope = 'options-recovery'
                           AND item.model_name = 'option_recovery_signal'
                           AND item.payload->>'signal_id' = observation.signal_id::text
                       ) AS published
                FROM analysis.option_opportunity_observation observation
                WHERE observation.lane = 'recovery'
                  AND observation.available_at >= %s
                  AND observation.available_at <= %s
                  AND observation.calibration_cohort LIKE %s
                ORDER BY observation.available_at, observation.id
                """,
                [since, reference, f"{SCORECARD_TRUTH_PREFIX}%"],
            ).fetchall()
        normalized: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            stage = str(item.get("selection_stage") or "observed")
            if not bool(item.get("published")) and stage in {"published", "ticketed", "filled", "exited"}:
                item["selection_stage"] = "observed"
            normalized.append(item)
        return normalized

    def _decision_rows(self, lane: str, since: datetime, reference: datetime) -> list[dict[str, Any]]:
        if lane == "radar":
            return self._incumbent_rows(since, reference)
        with self.runtime.read(API_PROFILE) as connection:
            rows = connection.execute(
                """
                WITH latest_decisions AS MATERIALIZED (
                    SELECT DISTINCT ON (decision.episode_key)
                           decision.id, decision.run_id, decision.episode_key,
                           decision.as_of, decision.state,
                           decision.sample_eligible, decision.quarantine_reason,
                           decision.calibration_cohort
                    FROM analysis.decision decision
                    WHERE decision.kind = 'option'
                      AND decision.lane = %s
                      AND decision.as_of >= %s
                      AND decision.as_of <= %s
                      AND decision.calibration_cohort LIKE 'option-scorecard-truth-v1:%%'
                    ORDER BY decision.episode_key, decision.as_of DESC, decision.id DESC
                ), eligible_runs AS MATERIALIZED (
                    SELECT DISTINCT run_id FROM latest_decisions
                ), published_items AS MATERIALIZED (
                    SELECT eligible_runs.run_id, item.payload
                    FROM eligible_runs
                    JOIN app.publication publication
                      ON publication.analysis_run_id = eligible_runs.run_id
                    JOIN app.publication_content_item item ON item.publication_id = publication.id
                    WHERE publication.status IN ('published', 'superseded')
                      AND (
                        (publication.scope = 'options-radar'
                         AND item.model_name = 'option_radar_opportunity')
                        OR
                        (publication.scope = 'options-decision-system'
                         AND item.model_name = 'options_decision_candidate')
                      )
                ), published_ids AS MATERIALIZED (
                    SELECT run_id, payload->>'decision_id' AS decision_id
                    FROM published_items
                    WHERE payload->>'decision_id' IS NOT NULL
                    UNION
                    SELECT run_id, payload->>'opportunity_id' AS decision_id
                    FROM published_items
                    WHERE payload->>'opportunity_id' IS NOT NULL
                )
                SELECT decision.episode_key, decision.as_of AS available_at, decision.state,
                       decision.sample_eligible AS decision_sample_eligible,
                       decision.quarantine_reason AS decision_quarantine_reason,
                       decision.calibration_cohort,
                       outcome.sample_eligible AS outcome_sample_eligible,
                       outcome.quarantine_reason AS outcome_quarantine_reason,
                       outcome.outcome_classification, outcome.maturity_state,
                       coalesce(outcome.realized_exit_return, outcome.current_return) AS realized_return,
                       option_decision.probability_profit,
                       paper.status AS paper_status, paper.filled_at, paper.exit_at,
                       EXISTS (
                         SELECT 1
                         FROM published_ids
                         WHERE published_ids.run_id = decision.run_id
                           AND published_ids.decision_id = decision.id::text
                       ) AS published
                FROM latest_decisions decision
                LEFT JOIN analysis.option_decision option_decision ON option_decision.decision_id = decision.id
                LEFT JOIN analysis.option_outcome outcome ON outcome.decision_id = decision.id
                LEFT JOIN LATERAL (
                    SELECT status, filled_at, exit_at
                    FROM app.paper_order paper_order
                    WHERE paper_order.decision_id = decision.id
                    ORDER BY paper_order.created_at DESC
                    LIMIT 1
                ) paper ON true
                ORDER BY decision.as_of, decision.id
                """,
                [lane, since, reference],
            ).fetchall()
        return _normalize_decision_rows(rows)

    def _incumbent_rows(self, since: datetime, reference: datetime) -> list[dict[str, Any]]:
        from investment_panel.infrastructure.postgres.options_experiments import EXPERIMENT_VERSION
        from investment_panel.infrastructure.postgres.strategy_learning import observation_lineage_matches

        with self.runtime.read(API_PROFILE) as connection:
            rows = connection.execute(
                """
                WITH active_revision AS MATERIALIZED (
                    SELECT id FROM analysis.strategy_revision
                    WHERE authority_group = 'options-radar-core' AND status = 'active'
                      AND created_at <= %(reference)s AND promoted_at <= %(reference)s
                ), episode_keys AS MATERIALIZED (
                    -- Group narrow keys before any run, shadow, or payload join.
                    -- Repeated captures must not require a wide per-capture sort.
                    SELECT episode_key FROM analysis.decision
                    WHERE strategy_revision_id = (SELECT id FROM active_revision)
                      AND kind = 'option' AND lane = 'radar'
                      AND as_of BETWEEN %(since)s AND %(reference)s
                      AND calibration_cohort LIKE 'option-scorecard-truth-v1:%%'
                    GROUP BY episode_key
                ), latest_ids AS MATERIALIZED (
                    SELECT latest.id, keys.episode_key FROM episode_keys keys
                    JOIN LATERAL (
                        SELECT decision.id FROM analysis.decision decision
                        WHERE decision.strategy_revision_id = (SELECT id FROM active_revision)
                          AND decision.kind = 'option' AND decision.lane = 'radar'
                          AND decision.as_of BETWEEN %(since)s AND %(reference)s
                          AND decision.calibration_cohort LIKE 'option-scorecard-truth-v1:%%'
                          AND decision.episode_key = keys.episode_key
                          AND EXISTS (SELECT 1 FROM analysis.run run WHERE run.id = decision.run_id
                              AND run.strategy_revision_id = decision.strategy_revision_id
                              AND run.run_type = 'options-radar')
                        ORDER BY decision.as_of DESC, decision.id DESC LIMIT 1
                    ) latest ON true WHERE keys.episode_key IS NOT NULL
                    UNION ALL
                    -- Keep a malformed null key visible to the integrity gate.
                    (SELECT decision.id, decision.episode_key FROM analysis.decision decision
                     WHERE decision.strategy_revision_id = (SELECT id FROM active_revision)
                       AND decision.kind = 'option' AND decision.lane = 'radar'
                       AND decision.as_of BETWEEN %(since)s AND %(reference)s
                       AND decision.calibration_cohort LIKE 'option-scorecard-truth-v1:%%'
                       AND decision.episode_key IS NULL
                       AND EXISTS (SELECT 1 FROM analysis.run run WHERE run.id = decision.run_id
                           AND run.strategy_revision_id = decision.strategy_revision_id
                           AND run.run_type = 'options-radar')
                     ORDER BY decision.as_of DESC, decision.id DESC LIMIT 1)
                ), retained_ids AS MATERIALIZED (
                    SELECT DISTINCT ON (decision.episode_key)
                           decision.id, decision.episode_key, shadow.id AS retained_shadow_id
                    FROM analysis.shadow_trade shadow
                    JOIN analysis.decision decision ON decision.id = shadow.decision_id
                    WHERE decision.strategy_revision_id = (SELECT id FROM active_revision)
                      AND shadow.source_kind = 'options_paper_experiment'
                      AND shadow.created_at <= %(reference)s
                      AND decision.kind = 'option' AND decision.lane = 'radar'
                      AND decision.as_of BETWEEN %(since)s AND %(reference)s
                      AND EXISTS (SELECT 1 FROM analysis.run run WHERE run.id = decision.run_id
                          AND run.strategy_revision_id = decision.strategy_revision_id
                          AND run.run_type = 'options-radar')
                    -- Retain even a missing truth marker; never replace that
                    -- current defect with a later apparently valid capture.
                    ORDER BY decision.episode_key, decision.as_of, decision.id DESC
                ), canonical_ids AS MATERIALIZED (
                    SELECT id, retained_shadow_id FROM retained_ids
                    UNION ALL
                    SELECT latest.id, NULL::uuid FROM latest_ids latest
                    WHERE NOT EXISTS (SELECT 1 FROM retained_ids retained
                        WHERE retained.episode_key IS NOT DISTINCT FROM latest.episode_key)
                ), canonical_decisions AS MATERIALIZED (
                    SELECT decision.id, decision.run_id, decision.episode_key, decision.lane,
                           decision.as_of, decision.state, decision.strategy_revision_id,
                           decision.sample_eligible, decision.quarantine_reason, decision.calibration_cohort,
                           canonical.retained_shadow_id
                    FROM canonical_ids canonical
                    JOIN analysis.decision decision ON decision.id = canonical.id
                )
                SELECT decision.id::text AS decision_id, decision.episode_key,
                       decision.as_of AS available_at, decision.state, decision.strategy_revision_id,
                       decision.sample_eligible AS decision_sample_eligible,
                       decision.quarantine_reason AS decision_quarantine_reason,
                       decision.calibration_cohort, decision.retained_shadow_id,
                       run.id::text AS run_id, run.run_type, run.status AS run_status, run.input_cutoff,
                       jsonb_build_object('observation', run.inputs->'observation') AS inputs,
                       jsonb_build_object('experiment', shadow.metrics->'experiment') AS metrics,
                       jsonb_build_object('experiment', item.payload->'experiment',
                           'ticket', jsonb_build_object('experiment', item.payload->'ticket'->'experiment')) AS payload,
                       observation.scope, shadow.metrics->'ticket' = item.payload->'ticket' AS ticket_matches,
                       outcome.quarantine_reason AS outcome_quarantine_reason,
                       outcome.outcome_classification, outcome.maturity_state,
                       CASE WHEN shadow.id IS NOT NULL THEN outcome.current_return
                            ELSE coalesce(outcome.realized_exit_return, outcome.current_return) END AS realized_return,
                       run.status = 'succeeded' AND run.input_cutoff <= decision.as_of
                         AND decision.sample_eligible IS TRUE AND outcome.sample_eligible IS TRUE
                         AND decision.quarantine_reason IS NULL AND outcome.quarantine_reason IS NULL
                         AND outcome.lane = decision.lane AND outcome.episode_key = decision.episode_key
                         AND outcome.calibration_cohort = decision.calibration_cohort
                         AND outcome.outcome_classification = 'captured'
                         AND outcome.maturity_state IN ('mature', 'expired', 'closed')
                         AND outcome.observed_through BETWEEN decision.as_of AND %(reference)s
                         AND outcome.updated_at <= %(reference)s
                         AND coalesce(outcome.realized_exit_return, outcome.current_return) > '-Infinity'::double precision
                         AND coalesce(outcome.realized_exit_return, outcome.current_return) < 'Infinity'::double precision
                         AND ((shadow.id IS NULL AND outcome.promotion_eligible IS TRUE) OR (
                             shadow.status = 'closed' AND decision.state IN ('WATCH', 'SETUP', 'READY')
                             AND outcome.shadow_trade_id = shadow.id AND outcome.objective_version = %(version)s
                             AND outcome.current_return > '-Infinity'::double precision
                             AND outcome.current_return < 'Infinity'::double precision
                             AND decision.as_of < outcome.entry_fill_at AND outcome.entry_fill_at < outcome.exit_fill_at
                             AND outcome.entry_fill_at = shadow.entry_at AND outcome.exit_fill_at = shadow.exit_at
                             AND outcome.entry_fill_price = shadow.entry_price AND outcome.exit_fill_price = shadow.exit_price
                             AND outcome.entry_fill_price > 0 AND outcome.entry_fill_price < 'Infinity'::numeric
                             AND outcome.exit_fill_price >= 0 AND outcome.exit_fill_price < 'Infinity'::numeric
                             AND outcome.exit_fill_at <= outcome.observed_through
                             AND outcome.fee_total >= 0 AND outcome.fee_total < 'Infinity'::numeric
                             AND outcome.slippage_total >= 0 AND outcome.slippage_total < 'Infinity'::numeric
                         )) AS outcome_sample_eligible,
                       option_decision.probability_profit,
                       paper.status AS paper_status, paper.filled_at, paper.exit_at,
                       EXISTS (
                           SELECT 1 FROM app.publication publication
                           JOIN app.publication_content_item published ON published.publication_id = publication.id
                           WHERE publication.analysis_run_id = decision.run_id
                             AND publication.scope = 'options-radar'
                             AND publication.status IN ('published', 'superseded')
                             AND publication.published_at <= %(reference)s
                             AND published.model_name = 'option_radar_opportunity'
                             AND published.payload->>'decision_id' = decision.id::text
                       ) AS published
                FROM canonical_decisions decision
                JOIN analysis.run run ON run.id = decision.run_id
                LEFT JOIN analysis.shadow_trade shadow ON shadow.id = decision.retained_shadow_id
                LEFT JOIN app.publication observation
                    ON observation.id = CASE WHEN pg_input_is_valid(shadow.metrics->>'publication_id', 'uuid')
                        THEN (shadow.metrics->>'publication_id')::uuid END
                   AND observation.analysis_run_id = decision.run_id
                   AND observation.status IN ('published', 'superseded')
                   AND observation.published_at <= %(reference)s
                LEFT JOIN app.publication_content_item item ON item.publication_id = observation.id
                     AND item.model_name = 'option_paper_experiment'
                     AND item.payload->>'decision_id' = decision.id::text
                LEFT JOIN analysis.option_decision option_decision ON option_decision.decision_id = decision.id
                LEFT JOIN analysis.option_outcome outcome ON outcome.decision_id = decision.id
                LEFT JOIN LATERAL (
                    SELECT CASE WHEN filled_at > %(reference)s THEN 'staged'
                                WHEN exit_at > %(reference)s THEN 'entered' ELSE status END AS status,
                           CASE WHEN filled_at <= %(reference)s THEN filled_at END AS filled_at,
                           CASE WHEN exit_at <= %(reference)s THEN exit_at END AS exit_at
                    FROM app.paper_order
                    WHERE decision_id = decision.id AND paper_only IS TRUE
                      AND created_at <= %(reference)s
                    ORDER BY created_at DESC, id DESC LIMIT 1
                ) paper ON true
                ORDER BY decision.as_of, decision.id
                """,
                {"since": since, "reference": reference, "version": EXPERIMENT_VERSION},
            ).fetchall()
        prepared = []
        for row in rows:
            item = dict(row)
            if item["retained_shadow_id"] is not None:
                valid = (
                    item["run_status"] == "succeeded" and item["input_cutoff"] <= item["available_at"]
                    and item["ticket_matches"] is True
                    and observation_lineage_matches(item, revision_id=item["strategy_revision_id"])
                )
                item["published"] = valid
                if not valid:
                    item["outcome_sample_eligible"] = False
                    item["outcome_quarantine_reason"] = "incumbent_observation_lineage_invalid"
                if not has_current_scorecard_truth(item.get("calibration_cohort")):
                    item["outcome_sample_eligible"] = False
                    item["outcome_quarantine_reason"] = CURRENT_TRUTH_DEFECT
            item["outcome_sample_eligible"] = bool(item["outcome_sample_eligible"])
            if not item["outcome_sample_eligible"]:
                item["realized_return"] = None
            prepared.append(item)
        return _normalize_decision_rows(prepared)


def _normalize_decision_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        state = str(item.get("state") or "").upper()
        paper_status = str(item.get("paper_status") or "").lower()
        published = bool(item.get("published"))
        item["selection_stage"] = (
            "exited" if published and paper_status in {"exited", "invalidated"}
            else "filled" if published and paper_status in {"entered", "partial_exited"}
            else "ticketed" if published and (state == "READY" or paper_status == "staged")
            else "published" if published
            else "ranked_out" if state in {"REJECT", "REJECTED"}
            else "observed"
        )
        item["sample_eligible"] = bool(
            item.get("outcome_sample_eligible")
            if item.get("outcome_sample_eligible") is not None
            else item.get("decision_sample_eligible")
        )
        item["quarantine_reason"] = item.get("outcome_quarantine_reason") or item.get("decision_quarantine_reason")
        item["entry_fill_at"] = item.get("filled_at") if paper_status in {"entered", "partial_exited", "exited"} else None
        item["exit_fill_at"] = item.get("exit_at") if paper_status in {"exited", "invalidated"} else None
        if item.get("outcome_classification") is None:
            item["outcome_classification"] = "observing"
        normalized.append(item)
    return normalized


def _latest_by_episode(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("episode_key") or "")
        if not key:
            continue
        prior = latest.get(key)
        if prior is None or row["available_at"] >= prior["available_at"]:
            latest[key] = row
    return sorted(latest.values(), key=lambda row: row["available_at"])


def _scorecard(
    *,
    lane: str,
    as_of: datetime,
    window_days: int,
    raw_observation_count: int,
    episodes: list[dict[str, Any]],
    external_defects: dict[str, int] | None = None,
    externally_quarantined_episode_count: int = 0,
    excluded_legacy_observation_count: int = 0,
    excluded_legacy_independent_episode_count: int = 0,
) -> dict[str, Any]:
    # Keep the public computation safe if a future caller supplies raw rows.
    # The repository already does this reduction, but a scorecard must never
    # turn repeated captures into independent opportunities.
    source_rows = list(episodes)
    missing_episode_key_count = sum(1 for row in source_rows if not str(row.get("episode_key") or ""))
    episodes = _latest_by_episode(source_rows)
    all_independent_episodes = len(episodes)
    trusted_episodes: list[dict[str, Any]] = []
    defects: dict[str, int] = dict(external_defects or {})
    if missing_episode_key_count:
        defects["missing_episode_key"] = missing_episode_key_count
    for row in episodes:
        if has_current_scorecard_truth(row.get("calibration_cohort")):
            trusted_episodes.append(row)
        else:
            reason = (CURRENT_TRUTH_DEFECT if row.get("quarantine_reason") == CURRENT_TRUTH_DEFECT
                      else "legacy_or_unversioned_truth_contract")
            # The scope count includes all malformed current captures, including
            # this retained row; do not count it twice after episode reduction.
            if reason != CURRENT_TRUTH_DEFECT or reason not in (external_defects or {}):
                defects[reason] = defects.get(reason, 0) + 1
    episodes = trusted_episodes
    stages = {
        "observed_universe": len(episodes),
        "ranked_out_counterfactual": 0,
        "published_signal": 0,
        "ready_ticket": 0,
        "simulated_fill": 0,
        "closed_paper_trade": 0,
    }
    resolved: list[dict[str, Any]] = []
    for row in episodes:
        stage = str(row.get("selection_stage") or "observed")
        if stage == "ranked_out":
            stages["ranked_out_counterfactual"] += 1
        if stage in {"published", "ticketed", "filled", "exited"}:
            stages["published_signal"] += 1
        if stage in {"ticketed", "filled", "exited"}:
            stages["ready_ticket"] += 1
        if row.get("entry_fill_at") is not None:
            stages["simulated_fill"] += 1
        if row.get("exit_fill_at") is not None:
            stages["closed_paper_trade"] += 1
        reason = str(row.get("quarantine_reason") or row.get("data_status") or "")
        if reason in {"ok", "none"}:
            reason = ""
        if reason and reason not in NON_DEFECT_QUARANTINE_REASONS:
            defects[reason] = defects.get(reason, 0) + 1
        outcome = str(row.get("outcome_classification") or "observing")
        maturity = str(row.get("maturity_state") or "")
        is_resolved = outcome not in {"", "observing", "unmeasurable", "legacy_non_executable"}
        if maturity and maturity not in {"mature", "expired", "closed"}:
            is_resolved = False
        if is_resolved and bool(row.get("sample_eligible")) and not reason:
            resolved.append(row)

    resolved_returns = [float(row["realized_return"]) for row in resolved if row.get("realized_return") is not None]
    trading_days = len({row["available_at"].astimezone(UTC).date() for row in resolved})
    sample_ready = len(resolved_returns) >= MIN_RESOLVED_EPISODES and trading_days >= MIN_TRADING_DAYS
    expectancy = mean(resolved_returns) if sample_ready and resolved_returns else None
    lower_95 = _lower_95(resolved_returns) if sample_ready else None
    calibration = _calibration(resolved) if sample_ready else {
        "status": "collecting", "value": None, "metric": "brier_score_probability_profit",
    }
    gaps: list[str] = []
    if len(resolved_returns) < MIN_RESOLVED_EPISODES:
        gaps.append(f"need_{MIN_RESOLVED_EPISODES - len(resolved_returns)}_more_resolved_independent_episodes")
    if trading_days < MIN_TRADING_DAYS:
        gaps.append(f"need_{MIN_TRADING_DAYS - trading_days}_more_trading_days")
    if defects:
        gaps.append("quarantined_data_health_defects_present")
    if calibration["status"] != "ready":
        gaps.append("deterministic_calibration_baseline_missing")
    if lower_95 is not None and lower_95 <= 0:
        gaps.append("lower_95_expectancy_not_positive")

    funnel = {
        "observed": raw_observation_count,
        "independent": len(episodes),
        "published": stages["published_signal"],
        "ticketed": stages["ready_ticket"],
        "filled": stages["simulated_fill"],
        "closed": stages["closed_paper_trade"],
    }
    if not (
        funnel["closed"] <= funnel["filled"] <= funnel["ticketed"]
        <= funnel["published"] <= funnel["independent"] <= funnel["observed"]
    ):
        defects["funnel_order_violation"] = defects.get("funnel_order_violation", 0) + 1
        gaps.append("funnel_order_violation")

    integrity_status = "INVALID" if defects else ("REBUILDING" if not sample_ready else "VALID")
    ready = not gaps and integrity_status == "VALID"
    return {
        "lane": lane,
        "as_of": as_of.isoformat(),
        "window_days": window_days,
        "raw_observation_count": raw_observation_count,
        "independent_episode_count": len(episodes),
        "quarantined_independent_episode_count": (
            all_independent_episodes - len(episodes) + externally_quarantined_episode_count
        ),
        "excluded_legacy_observation_count": excluded_legacy_observation_count,
        "excluded_legacy_independent_episode_count": excluded_legacy_independent_episode_count,
        "missing_episode_key_count": missing_episode_key_count,
        "resolved_independent_episode_count": len(resolved_returns),
        "trading_day_count": trading_days,
        "stages": stages,
        "funnel": funnel,
        "expectancy": expectancy,
        "lower_95_expectancy": lower_95,
        "calibration": calibration,
        "data_health_defects": defects,
        "truth_contract": SCORECARD_TRUTH_VERSION,
        "integrity_status": integrity_status,
        "display_status": "INVALID / REBUILDING" if integrity_status == "INVALID" else integrity_status,
        "status": "READY_FOR_REVIEW" if ready else ("INVALID" if integrity_status == "INVALID" else "COLLECTING"),
        "gaps": gaps,
        "automatic_strategy_promotion": {
            "enabled": False,
            "eligible": False,
            "reason": "scorecard_invalid_or_rebuilding",
        },
    }


def _lower_95(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    return mean(values) - 1.96 * stdev(values) / sqrt(len(values))


def _calibration(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pairs = [
        (float(row["probability_profit"]), 1.0 if float(row.get("realized_return") or 0.0) > 0 else 0.0)
        for row in rows
        if row.get("probability_profit") is not None and row.get("realized_return") is not None
        and 0.0 <= float(row["probability_profit"]) <= 1.0
    ]
    if len(pairs) < MIN_RESOLVED_EPISODES:
        return {"status": "collecting", "value": None, "sample_size": len(pairs)}
    brier = mean((prediction - outcome) ** 2 for prediction, outcome in pairs)
    baseline = mean((0.5 - outcome) ** 2 for _, outcome in pairs)
    return {
        "status": "ready" if brier <= baseline else "worse_than_baseline",
        "metric": "brier_score_probability_profit",
        "value": brier,
        "baseline": baseline,
        "sample_size": len(pairs),
    }


scorecard_payload = _scorecard
