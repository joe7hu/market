"""Persistence and learning reads for versioned ticker decisions."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from math import isfinite
from typing import Any, Iterable, Mapping
from uuid import UUID

from psycopg.types.json import Jsonb

from investment_panel.core.decision import (
    AvailabilityStatus,
    DecisionResolutionV2,
    ExpressionKind,
    Horizon,
    OUTCOME_ATTRIBUTION_CONTRACT_VERSION,
    OUTCOME_ATTRIBUTION_EVALUATION_VERSION,
    OutcomeAttribution,
    OutcomeAttributionState,
    OutcomeEvidence,
    OutcomeEvidenceState,
    OpportunityEpisode,
    PaperExecutionOutcome,
    PortfolioImpact,
    TickerDecision,
    TradeExpression,
    TradePlan,
    InputLineage,
    MarketStateSnapshot,
    bind_trade_plan,
    capital_action_from_resolution,
    evaluate_ticker_policy,
    outcome_attribution_stable_key,
    resolution_from_legacy,
    is_us_market_day,
    market_evidence_for_decision,
    portfolio_impacts_from_persisted,
    portfolio_impact_from_persisted,
    trade_expression_identity,
    trade_plan_from_persisted,
    valid_outcome_error_type,
)
from investment_panel.core.options_recovery import FEE_PER_CONTRACT_LEG
from investment_panel.database.options_paper_quotes import is_credit_structure, package_price
from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.runtime import API_PROFILE, DatabaseRuntime, JOB_PROFILE


HORIZON_SESSIONS = {
    Horizon.TACTICAL: (1, 5, 20),
    Horizon.FUNDAMENTAL: (63, 126, 252),
}
STOCK_COST_MODEL_VERSION = "stock-close-estimated-cost-v1"
STOCK_COST_PER_SIDE_BPS = 10.0
TICKER_RANKING_SCOPE = "ticker-opportunity-ranking"


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
        if str(row.get("contract_version") or "") != OUTCOME_ATTRIBUTION_CONTRACT_VERSION:
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
    try:
        units = {
            (str(row.get("horizon") or "").upper(), int(row.get("horizon_sessions") or 0))
            for row in matches
        }
    except (TypeError, ValueError):
        return [], "outcome_attribution_units_incomplete"
    expected_units = {
        (horizon.value, sessions)
        for horizon, sessions_list in HORIZON_SESSIONS.items()
        for sessions in sessions_list
    }
    if units != expected_units:
        return [], "outcome_attribution_units_incomplete"
    return sorted(
        matches,
        key=lambda row: (str(row.get("horizon") or ""), int(row.get("horizon_sessions") or 0)),
    ), None


_PEER_RETURN_QUERY = """
WITH peer_bars AS MATERIALIZED (
    SELECT bar.instrument_id, bar.trading_date, bar.close, bar.available_at
    FROM raw.confirmed_price_bar bar
    WHERE bar.instrument_id = ANY(
        ARRAY(
            SELECT instrument.id
            FROM catalog.instrument instrument
            WHERE instrument.symbol = ANY(%s)
        )
    )
      AND bar.interval = '1d'
), entry_prices AS (
    SELECT DISTINCT ON (bar.instrument_id)
           bar.instrument_id, bar.close
    FROM peer_bars bar
    WHERE bar.trading_date <= %s
      AND bar.available_at <= %s
    ORDER BY bar.instrument_id, bar.trading_date DESC, bar.available_at DESC
), mark_prices AS (
    SELECT DISTINCT ON (bar.instrument_id)
           bar.instrument_id, bar.close
    FROM peer_bars bar
    WHERE bar.trading_date = %s
      AND bar.available_at <= %s
    ORDER BY bar.instrument_id, bar.available_at DESC
)
SELECT avg(mark_prices.close / entry_prices.close - 1) AS return
FROM entry_prices JOIN mark_prices USING (instrument_id)
WHERE entry_prices.close > 0
"""


class TickerDecisionRepository:
    def __init__(self, runtime: DatabaseRuntime) -> None:
        self.runtime = runtime

    def publish(self, decision: TickerDecision) -> dict[str, Any]:
        """Publish an immutable decision revision and its input manifest."""

        payload = decision.model_dump(mode="json")
        payload["input_manifest"] = {
            **dict(payload.get("input_manifest") or {}),
            "instrument_state_snapshot": payload.get("instrument_state_snapshot"),
            "alpha_signals": payload.get("alpha_signals") or [],
            "opportunity_rank": payload.get("opportunity_rank"),
            "trade_plan": payload.get("trade_plan"),
        }
        with self.runtime.transaction(JOB_PROFILE) as connection:
            instrument = connection.execute(
                "SELECT id FROM catalog.instrument WHERE symbol = %s LIMIT 1",
                [decision.ticker],
            ).fetchone()
            if instrument is None:
                raise ValueError("ticker instrument is not in the catalog")
            plan = decision.trade_plan
            typed_signals = tuple(signal for signal in decision.alpha_signals if getattr(signal, "contract_version", None) == "alpha-signal.v1")
            if plan is not None and typed_signals and str(getattr(plan, "eligibility", "")).upper().endswith("ACTIONABLE") and str(getattr(plan, "selected_expression_kind", "")).upper() == "STOCK":
                forecast_ids = {
                    str(value or "") for value in (
                        getattr(plan, "strategy_forecast_id", None),
                        getattr(decision.opportunity_rank, "strategy_forecast_id", None),
                        *(getattr(signal, "strategy_forecast_id", None) for signal in decision.alpha_signals),
                    ) if str(value or "").strip()
                }
                if len(forecast_ids) != 1:
                    raise ValueError("actionable stock path requires exactly one strategy forecast")
                persisted = connection.execute(
                    """SELECT count(*) AS count FROM analysis.strategy_forecast
                       WHERE id = %s AND instrument_id = %s AND status = 'available'""",
                    [next(iter(forecast_ids)), instrument["id"]],
                ).fetchone()["count"]
                if persisted != 1:
                    raise ValueError("actionable stock path requires one persisted model-owned strategy forecast")
            row = connection.execute(
                """
                INSERT INTO analysis.ticker_decision (
                    instrument_id, decision_revision, contract_version, as_of,
                    published_at, input_hash, code_version, experiment_id,
                    tactical, fundamental, capital_action, resolution, policy_version,
                    opportunity_episode_id, opportunity_cutoff, opportunity_episode, risk_policy,
                    expressions, selected_expression, data_requests,
                    learning_history, input_manifest, market_state_publication_id,
                    market_state_snapshot, portfolio_impacts, risk_policy_snapshot, status
                ) VALUES (
                    %s, %s, %s, %s, now(), %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, 'published'
                )
                ON CONFLICT (instrument_id, decision_revision) DO NOTHING
                RETURNING id::text
                """,
                [
                    instrument["id"], decision.decision_revision,
                    decision.decision_contract_version, decision.as_of,
                    decision.input_manifest.input_hash,
                    decision.input_manifest.code_version,
                    decision.input_manifest.experiment_id,
                    Jsonb(payload["tactical"]), Jsonb(payload["fundamental"]),
                    Jsonb(payload["capital_action"]), Jsonb(payload["resolution"]),
                    decision.policy_version, decision.opportunity_episode.episode_id,
                    decision.opportunity_episode.cutoff,
                    Jsonb(payload["opportunity_episode"]), Jsonb(payload["risk_policy"]),
                    Jsonb(payload["expressions"]), Jsonb(payload.get("selected_expression")),
                    Jsonb(payload["data_requests"]), Jsonb(payload["learning_history"]),
                    Jsonb(payload["input_manifest"]),
                    _uuid_or_none(decision.market_state_publication_id),
                    Jsonb(payload.get("market_state_snapshot") or {}),
                    Jsonb(payload.get("portfolio_impacts") or {}),
                    Jsonb(payload.get("risk_policy_snapshot") or {}),
                ],
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT id::text FROM analysis.ticker_decision WHERE instrument_id = %s AND decision_revision = %s",
                    [instrument["id"], decision.decision_revision],
                ).fetchone()
            decision_id = str(row["id"])
            connection.execute(
                """
                UPDATE analysis.ticker_decision
                SET status = 'superseded'
                WHERE instrument_id = %s AND id <> %s::uuid AND status = 'published' AND as_of < %s
                """,
                [instrument["id"], decision_id, decision.as_of],
            )
            self._store_manifest(connection, decision_id, decision)
            for request in decision.data_requests:
                connection.execute(
                    """
                    INSERT INTO analysis.ticker_data_request (ticker_decision_id, field, ticker, request)
                    VALUES (%s::uuid, %s, %s, %s)
                    ON CONFLICT (ticker_decision_id, field) DO UPDATE SET request = EXCLUDED.request
                    """,
                    [decision_id, request.field, request.ticker, Jsonb(request.model_dump(mode="json"))],
                )
        return {"status": "published", "ticker_decision_id": decision_id, "decision_revision": decision.decision_revision}

    def latest(self, ticker: str) -> TickerDecision | None:
        rows = self._current_decision_rows(
            reference=datetime.now(UTC), ticker=ticker.strip().upper(),
        )
        if not rows:
            return None
        try:
            return _decision_from_row(rows[0])
        except (TypeError, ValueError, KeyError):
            # Legacy rows remain readable through the raw panel model, but a
            # malformed row must not block a new canonical publication.
            return None

    def _current_decision_rows(
        self, *, reference: datetime, ticker: str | None = None,
    ) -> list[dict[str, Any]]:
        """Read the one canonical current row per ticker at a bounded time."""

        with self.runtime.read() as connection:
            rows = connection.execute(
                """
                WITH current_candidates AS (
                    SELECT instrument.symbol AS ticker, decision.contract_version,
                           decision.as_of, decision.decision_revision,
                           decision.tactical, decision.fundamental, decision.capital_action,
                           decision.resolution, decision.policy_version,
                           decision.opportunity_episode_id, decision.opportunity_cutoff,
                           decision.opportunity_episode, decision.risk_policy, decision.expressions,
                           decision.selected_expression, decision.data_requests,
                           decision.learning_history, decision.input_manifest,
                           decision.market_state_publication_id::text,
                           decision.market_state_snapshot, decision.portfolio_impacts,
                           decision.risk_policy_snapshot,
                           decision.published_at, decision.created_at, decision.id,
                           count(*) OVER (
                               PARTITION BY decision.instrument_id, decision.as_of, decision.published_at
                           ) AS authority_count,
                           count(*) OVER (
                               PARTITION BY decision.opportunity_episode_id
                           ) AS opportunity_authority_count
                    FROM analysis.ticker_decision decision
                    JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
                    WHERE decision.status = 'published'
                      AND decision.contract_version = 'ticker-decision.v1'
                      AND NULLIF(BTRIM(decision.decision_revision), '') IS NOT NULL
                      AND NULLIF(BTRIM(decision.code_version), '') IS NOT NULL
                      AND NULLIF(BTRIM(decision.experiment_id), '') IS NOT NULL
                      AND NULLIF(BTRIM(decision.opportunity_episode_id), '') IS NOT NULL
                      AND decision.as_of <= %s
                      AND decision.published_at IS NOT NULL
                      AND decision.published_at <= %s
                      AND jsonb_typeof(decision.tactical) = 'object'
                      AND jsonb_typeof(decision.fundamental) = 'object'
                      AND jsonb_typeof(decision.capital_action) = 'object'
                      AND jsonb_typeof(decision.risk_policy) = 'object'
                      AND jsonb_typeof(decision.expressions) = 'object'
                      AND jsonb_typeof(decision.input_manifest) = 'object'
                )
                SELECT DISTINCT ON (ticker) ticker, contract_version,
                       as_of, decision_revision,
                       tactical, fundamental, capital_action,
                       resolution, policy_version,
                       opportunity_episode_id, opportunity_cutoff,
                       opportunity_episode, risk_policy, expressions,
                       selected_expression, data_requests,
                       learning_history, input_manifest,
                       market_state_publication_id,
                       market_state_snapshot, portfolio_impacts,
                       risk_policy_snapshot, published_at
                FROM current_candidates
                WHERE authority_count = 1
                  AND opportunity_authority_count = 1
                  AND (%s::text IS NULL OR ticker = %s)
                ORDER BY ticker, as_of DESC, published_at DESC, created_at DESC, id DESC
                """,
                [reference, reference, ticker, ticker],
            ).fetchall()
        return [dict(row) for row in rows]

    def decision_funnel(
        self, *, now: datetime | None = None, action_queue: Iterable[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        """Summarize the current backend-owned ticker decision lane."""

        reference = _utc(now or datetime.now(UTC))
        analysis = AnalysisRepository(self.runtime)
        try:
            alpha_rows, rank_rows, plan_rows = self._current_funnel_publication_rows(reference=reference)
        except TypeError as exc:
            if "unexpected keyword argument 'reference'" not in str(exc):
                raise
            alpha_rows, rank_rows, plan_rows = self._current_funnel_publication_rows()
        supplied_action_queue = list(action_queue)
        derived_action_queue: list[dict[str, Any]] = []
        decisions: list[dict[str, Any]] = []
        market_publications: dict[str, dict[str, Any] | None] = {}
        for row in self._current_funnel_rows(reference=reference):
            ticker = str(row.get("ticker") or "").strip().upper()
            compact_contract_valid = True
            fast_cash = row.get("funnel_fast_path") is True
            try:
                stock_expression = TradeExpression.model_validate(row.get("stock_expression"))
                if stock_expression.kind is not ExpressionKind.STOCK or stock_expression.ticker.strip().upper() != ticker:
                    raise ValueError("stock expression does not match its ticker decision")
                stock_expression_projection = {
                    "availability_status": stock_expression.availability_status.value,
                    "blockers": list(stock_expression.blockers),
                }
            except (KeyError, TypeError, ValueError):
                compact_contract_valid = False
                stock_expression = None
                stock_expression_projection = {
                    "availability_status": AvailabilityStatus.ERROR.value,
                    "blockers": ["stock_expression_invalid"],
                }
            if fast_cash:
                stock_impact = None
                stock_impact_projection = {
                    "availability_status": AvailabilityStatus.MISSING.value,
                    "blockers": ["portfolio_context_missing"],
                }
            else:
                try:
                    stock_impact = PortfolioImpact.model_validate(portfolio_impact_from_persisted(
                        row.get("stock_portfolio_impact"), ticker=ticker,
                    ))
                    if (
                        stock_impact.expression_kind is not ExpressionKind.STOCK
                        or stock_impact.ticker.strip().upper() != ticker
                        or stock_impact.opportunity_episode_id != row.get("opportunity_episode_id")
                        or stock_impact.decision_revision != row.get("decision_revision")
                        or stock_impact.risk_policy_version != row.get("policy_version")
                        or _utc(stock_impact.cutoff) != _utc(row["as_of"])
                        or stock_impact.market_state_publication_id
                        != (str(row.get("market_state_publication_id") or "") or None)
                        or stock_expression is None
                        or stock_impact.expression_identity != trade_expression_identity(stock_expression)
                    ):
                        raise ValueError("stock portfolio impact does not match its ticker decision")
                    stock_impact_projection = {
                        "availability_status": stock_impact.availability_status.value,
                        "blockers": list(stock_impact.blockers),
                    }
                except (KeyError, TypeError, ValueError):
                    compact_contract_valid = False
                    stock_impact = None
                    stock_impact_projection = {
                        "availability_status": AvailabilityStatus.ERROR.value,
                        "blockers": ["stock_portfolio_impact_invalid"],
                    }
            try:
                resolution = DecisionResolutionV2.model_validate(row.get("resolution"))
                if (
                    resolution.decision_revision != row.get("decision_revision")
                    or resolution.policy_version != row.get("policy_version")
                    or (
                        resolution.ticker is not None
                        and resolution.ticker.strip().upper() != ticker
                    )
                ):
                    raise ValueError("resolution does not match its ticker decision")
                resolution_projection = {
                    "trade_plan_id": resolution.trade_plan_id,
                    "eligibility": resolution.eligibility.value,
                    "action": resolution.action.value,
                    "blockers": list(resolution.blockers),
                }
            except (KeyError, TypeError, ValueError):
                compact_contract_valid = False
                resolution = None
                resolution_projection = {
                    "trade_plan_id": None,
                    "eligibility": "BLOCKED",
                    "action": "NO_TRADE",
                    "blockers": ["decision_resolution_invalid"],
                }
            selected = row.get("selected_expression")
            selected_mapping = selected if isinstance(selected, Mapping) else None
            if selected is None:
                decision_selected_kind: ExpressionKind | None = None
                decision_selected_horizon: Horizon | None = Horizon.FUNDAMENTAL
            else:
                try:
                    if selected_mapping is None:
                        raise ValueError("selected expression must be an object")
                    decision_selected_kind = ExpressionKind(str(selected_mapping.get("kind")))
                    decision_selected_horizon = Horizon(str(selected_mapping.get("horizon")))
                except (TypeError, ValueError):
                    compact_contract_valid = False
                    decision_selected_kind = None
                    decision_selected_horizon = None
            raw_episode = row.get("opportunity_episode")
            episode_intentionally_omitted = (
                row.get("funnel_candidate_required") is False
                and raw_episode is None
            )
            episode_for_validation = raw_episode
            if row.get("impact_lineage_match") is not None:
                episode_cutoff = _parse_datetime(row.get("as_of"))
                lineage = raw_episode.get("input_lineage") if isinstance(raw_episode, Mapping) else None
                lineage_valid = bool(
                    episode_cutoff is not None
                    and isinstance(raw_episode, Mapping)
                    and row.get("opportunity_lineage_valid") is not False
                    and _funnel_lineage_is_valid(
                        lineage,
                        episode_id=str(row.get("opportunity_episode_id") or ""),
                        decision_revision=str(row.get("decision_revision") or ""),
                        policy_version=str(row.get("policy_version") or ""),
                        cutoff=episode_cutoff,
                    )
                )
                if lineage_valid:
                    episode_for_validation = raw_episode
                else:
                    episode_for_validation = None
            try:
                if episode_intentionally_omitted:
                    opportunity_episode = None
                elif episode_for_validation is None:
                    raise ValueError("opportunity episode lineage is invalid")
                else:
                    opportunity_episode = OpportunityEpisode.model_validate(episode_for_validation)
                    episode_selected_kind = (
                        opportunity_episode.selected_expression.kind
                        if opportunity_episode.selected_expression is not None
                        else None
                    )
                    episode_selected_horizon = (
                        opportunity_episode.selected_expression.horizon
                        if opportunity_episode.selected_expression is not None
                        else None
                    )
                    if (
                        opportunity_episode.episode_id != row.get("opportunity_episode_id")
                        or opportunity_episode.ticker.strip().upper() != ticker
                        or opportunity_episode.decision_revision != row.get("decision_revision")
                        or opportunity_episode.policy_version != row.get("policy_version")
                        or _utc(opportunity_episode.cutoff) != _utc(row["as_of"])
                        or row.get("opportunity_cutoff_match") is not True
                        or row.get("opportunity_expressions_match") is not True
                        or row.get("opportunity_selected_expression_match") is not True
                        or stock_expression is None
                        or opportunity_episode.expressions.get(ExpressionKind.STOCK)
                        != stock_expression
                        or episode_selected_kind is not decision_selected_kind
                        or (
                            episode_selected_kind is not None
                            and episode_selected_horizon is not decision_selected_horizon
                        )
                    ):
                        raise ValueError("opportunity episode does not match its ticker decision")
            except (KeyError, TypeError, ValueError):
                compact_contract_valid = False
                opportunity_episode = None
            episode_authority_valid = opportunity_episode is not None or episode_intentionally_omitted
            impact_lineage_valid = (
                fast_cash
                or episode_intentionally_omitted
                or (
                    opportunity_episode is not None
                    and bool(opportunity_episode.input_lineage)
                    and (
                        row.get("impact_lineage_match") is True
                        or (
                            row.get("impact_lineage_match") is None
                            and stock_impact is not None
                            and tuple(stock_impact.input_lineage)
                            == tuple(opportunity_episode.input_lineage)
                        )
                    )
                )
            )
            if not episode_authority_valid or (stock_impact is None and not fast_cash) or not impact_lineage_valid:
                compact_contract_valid = False
                stock_impact = None
                stock_impact_projection = {
                    "availability_status": AvailabilityStatus.ERROR.value,
                    "blockers": ["stock_portfolio_impact_invalid"],
                }
            if opportunity_episode is not None:
                selected_expression = opportunity_episode.selected_expression
                selected_kind = (
                    selected_expression.kind
                    if selected_expression is not None
                    else ExpressionKind.CASH
                )
                selected_horizon = (
                    selected_expression.horizon
                    if selected_expression is not None
                    else Horizon.FUNDAMENTAL
                )
            else:
                selected_kind = (
                    decision_selected_kind
                    if decision_selected_kind is not None
                    else ExpressionKind.CASH
                    if selected is None
                    else None
                )
                selected_horizon = decision_selected_horizon
            market_publication_id = str(row.get("market_state_publication_id") or "") or None
            decision_cutoff = _parse_datetime(row.get("as_of"))
            snapshot = None
            if market_publication_id and decision_cutoff is not None:
                if market_publication_id not in market_publications:
                    market_publications[market_publication_id] = analysis.publication_by_id(
                        "market", market_publication_id,
                    )
                snapshot = _market_snapshot_from_exact_publication(
                    market_publications[market_publication_id],
                    publication_id=market_publication_id,
                    decision_cutoff=decision_cutoff,
                )
            if (
                stock_impact is not None
                and snapshot is not None
                and stock_impact.market_snapshot_id != snapshot.snapshot_id
            ):
                compact_contract_valid = False
                stock_impact_projection = {
                    "availability_status": AvailabilityStatus.ERROR.value,
                    "blockers": ["stock_portfolio_impact_invalid"],
                }
            assessment = (
                market_evidence_for_decision(snapshot, selected_kind, selected_horizon)
                if selected_kind is not None and selected_horizon is not None
                else None
            )
            cash_selected = selected_kind is ExpressionKind.CASH
            facts_available = bool(
                compact_contract_valid
                and episode_authority_valid
                and (
                    cash_selected
                    or (
                        snapshot is not None
                        and market_publication_id
                        and market_publication_id == snapshot.publication_id
                        and assessment is not None
                        and not assessment.blocking_dimensions
                    )
                )
            )
            decisions.append({
                "ticker": ticker,
                "opportunity_episode": (
                    {"present": True} if opportunity_episode is not None else None
                ),
                "market_state_publication_id": market_publication_id,
                "expressions": {"STOCK": stock_expression_projection},
                "portfolio_impacts": {"STOCK": stock_impact_projection},
                "resolution": resolution_projection,
                "published_at": row["published_at"],
                "point_in_time_facts_available": facts_available,
                "point_in_time_fact_blockers": (
                    []
                    if facts_available
                    else ["ticker_decision_contract_invalid"]
                    if not compact_contract_valid
                    else list(assessment.blockers) if assessment is not None and assessment.blockers
                    else list(snapshot.blockers) if snapshot is not None and snapshot.blockers
                    else ["point_in_time_facts_unavailable"]
                ),
            })
            if not supplied_action_queue and facts_available:
                action = _funnel_action_queue_row(
                    row=row,
                    ticker=ticker,
                    opportunity_episode=opportunity_episode,
                    selected_kind=selected_kind,
                    stock_expression=stock_expression,
                    stock_impact=stock_impact,
                    resolution=resolution,
                    rank_rows=rank_rows,
                    plan_rows=plan_rows,
                ) if compact_contract_valid else None
                if action is not None:
                    derived_action_queue.append(action)
        return decision_funnel_payload(
            decisions, alpha_rows, rank_rows, plan_rows,
            action_queue_rows=supplied_action_queue or derived_action_queue,
            now=reference,
        )

    def _current_funnel_publication_rows(
        self,
        *,
        reference: datetime | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Read only current ranking-publication fields used by the funnel."""

        model_names = ["alpha_signal", "opportunity_rank", "trade_plan"]
        publication_cutoff = _utc(reference or datetime.now(UTC))
        with self.runtime.snapshot(API_PROFILE) as connection:
            has_projection = connection.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM app.current_publication_item item
                    JOIN app.publication publication
                      ON publication.id = item.publication_id
                    JOIN analysis.run run
                      ON run.id = publication.analysis_run_id
                    WHERE item.scope = %s
                      AND item.model_name = ANY(%s)
                      AND publication.status = 'published'
                      AND publication.published_at <= %s
                      AND run.input_cutoff <= %s
                      AND publication.published_at > run.input_cutoff
                ) AS exists
                """,
                [TICKER_RANKING_SCOPE, model_names, publication_cutoff, publication_cutoff],
            ).fetchone()["exists"]
            if has_projection:
                rows = connection.execute(
                    """
                    SELECT item.model_name,
                           coalesce(payload.payload->>'ticker', payload.payload->>'symbol')
                               AS ticker,
                           payload.payload->>'availability_status' AS availability_status,
                           payload.payload->'blockers' AS blockers,
                           payload.payload->'trade_rank' AS trade_rank,
                           payload.payload->>'primary_blocker' AS primary_blocker,
                           payload.payload->>'trade_rank_unavailable_reason'
                               AS trade_rank_unavailable_reason,
                           payload.payload->>'eligibility' AS eligibility,
                           payload.payload->>'ranking_version' AS ranking_version,
                           payload.payload->>'decision_revision' AS decision_revision,
                           payload.payload->>'opportunity_episode_id' AS opportunity_episode_id,
                           payload.payload->>'policy_version' AS policy_version,
                           payload.payload->>'selected_expression_kind' AS selected_expression_kind,
                           payload.payload->>'selected_expression_identity' AS selected_expression_identity,
                           payload.payload->>'rank_id' AS rank_id,
                           payload.payload->>'portfolio_impact_id' AS portfolio_impact_id,
                           payload.payload->>'market_state_publication_id' AS market_state_publication_id,
                           payload.payload->'evaluated_universe_complete' AS evaluated_universe_complete,
                           payload.payload->>'trade_utility' AS trade_utility,
                           payload.payload->>'trade_plan_id' AS trade_plan_id,
                           publication.id::text AS publication_id,
                           publication.published_at
                    FROM app.current_publication_item item
                    JOIN app.publication_payload payload
                      ON payload.content_hash = item.content_hash
                    JOIN app.publication publication
                      ON publication.id = item.publication_id
                    JOIN analysis.run run
                      ON run.id = publication.analysis_run_id
                    WHERE item.scope = %s
                      AND item.model_name = ANY(%s)
                      AND publication.status = 'published'
                      AND publication.published_at <= %s
                      AND run.input_cutoff <= %s
                      AND publication.published_at > run.input_cutoff
                    ORDER BY item.model_name, item.rank
                    """,
                    [TICKER_RANKING_SCOPE, model_names, publication_cutoff, publication_cutoff],
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    WITH chosen_publication AS MATERIALIZED (
                        SELECT publication.id, publication.bundle_id, publication.published_at,
                               run.input_cutoff
                        FROM app.publication publication
                        JOIN analysis.run run
                          ON run.id = publication.analysis_run_id
                        WHERE publication.scope = %s
                          AND publication.status IN ('published', 'superseded')
                          AND publication.published_at IS NOT NULL
                          AND publication.published_at <= %s
                          AND run.input_cutoff <= %s
                          AND publication.published_at > run.input_cutoff
                        ORDER BY run.input_cutoff DESC, publication.published_at DESC,
                                 publication.created_at DESC, publication.id DESC
                        LIMIT 1
                    ), source_rows AS MATERIALIZED (
                        SELECT item.model_name, item.rank,
                               chosen.id::text AS publication_id, chosen.published_at,
                               payload.payload
                        FROM chosen_publication chosen
                        JOIN app.publication_bundle_item item
                          ON item.bundle_id = chosen.bundle_id
                        JOIN app.publication_payload payload
                          ON payload.content_hash = item.content_hash
                        WHERE chosen.bundle_id IS NOT NULL
                          AND item.model_name = ANY(%s)
                        UNION ALL
                        SELECT item.model_name, item.rank,
                               chosen.id::text AS publication_id, chosen.published_at,
                               item.payload
                        FROM chosen_publication chosen
                        JOIN app.publication_item item
                          ON item.publication_id = chosen.id
                        WHERE chosen.bundle_id IS NULL
                          AND item.model_name = ANY(%s)
                    )
                    SELECT source.model_name,
                           coalesce(source.payload->>'ticker', source.payload->>'symbol')
                               AS ticker,
                           source.payload->>'availability_status' AS availability_status,
                           source.payload->'blockers' AS blockers,
                           source.payload->'trade_rank' AS trade_rank,
                           source.payload->>'primary_blocker'
                               AS primary_blocker,
                           source.payload->>'trade_rank_unavailable_reason'
                               AS trade_rank_unavailable_reason,
                           source.payload->>'eligibility' AS eligibility,
                           source.payload->>'ranking_version' AS ranking_version,
                           source.payload->>'decision_revision' AS decision_revision,
                           source.payload->>'opportunity_episode_id' AS opportunity_episode_id,
                           source.payload->>'policy_version' AS policy_version,
                           source.payload->>'selected_expression_kind' AS selected_expression_kind,
                           source.payload->>'selected_expression_identity' AS selected_expression_identity,
                           source.payload->>'rank_id' AS rank_id,
                           source.payload->>'portfolio_impact_id' AS portfolio_impact_id,
                           source.payload->>'market_state_publication_id' AS market_state_publication_id,
                           source.payload->'evaluated_universe_complete' AS evaluated_universe_complete,
                           source.payload->>'trade_utility' AS trade_utility,
                           source.payload->>'trade_plan_id' AS trade_plan_id,
                           source.publication_id,
                           source.published_at
                    FROM source_rows source
                    ORDER BY source.model_name, source.rank
                    """,
                    [
                        TICKER_RANKING_SCOPE, publication_cutoff, publication_cutoff,
                        model_names, model_names,
                    ],
                ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {
            "alpha_signal": [],
            "opportunity_rank": [],
            "trade_plan": [],
        }
        for row in rows:
            projected = {
                key: row.get(key)
                for key in (
                    "ticker",
                    "availability_status",
                    "blockers",
                    "trade_rank",
                    "primary_blocker",
                    "trade_rank_unavailable_reason",
                    "eligibility",
                    "ranking_version",
                    "decision_revision",
                    "opportunity_episode_id",
                    "policy_version",
                    "selected_expression_kind",
                    "selected_expression_identity",
                    "rank_id",
                    "portfolio_impact_id",
                    "market_state_publication_id",
                    "evaluated_universe_complete",
                    "trade_utility",
                    "trade_plan_id",
                    "publication_id",
                )
                if row.get(key) is not None
            }
            if row["published_at"] is not None:
                projected["publication_published_at"] = row["published_at"].isoformat()
            grouped[str(row["model_name"])].append(projected)
        return (
            grouped["alpha_signal"],
            grouped["opportunity_rank"],
            grouped["trade_plan"],
        )

    def _current_funnel_rows(self, *, reference: datetime) -> list[dict[str, Any]]:
        """Read only the compact current fields used by the decision funnel."""

        with self.runtime.read() as connection:
            rows = connection.execute(
                """
                WITH candidate_keys AS (
                    SELECT decision.id, instrument.symbol AS ticker,
                           decision.instrument_id, decision.as_of,
                           decision.published_at, decision.created_at,
                           count(*) OVER (
                               PARTITION BY decision.instrument_id, decision.as_of, decision.published_at
                           ) AS authority_count,
                           count(*) OVER (
                               PARTITION BY decision.opportunity_episode_id
                           ) AS opportunity_authority_count,
                           row_number() OVER (
                               PARTITION BY decision.instrument_id
                               ORDER BY decision.as_of DESC, decision.published_at DESC,
                                        decision.created_at DESC, decision.id DESC
                           ) AS current_row
                    FROM analysis.ticker_decision decision
                    JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
                    WHERE decision.status = 'published'
                      AND decision.contract_version = 'ticker-decision.v1'
                      AND NULLIF(BTRIM(decision.decision_revision), '') IS NOT NULL
                      AND NULLIF(BTRIM(decision.code_version), '') IS NOT NULL
                      AND NULLIF(BTRIM(decision.experiment_id), '') IS NOT NULL
                      AND NULLIF(BTRIM(decision.opportunity_episode_id), '') IS NOT NULL
                      AND decision.as_of <= %s
                      AND decision.published_at IS NOT NULL
                      AND decision.published_at <= %s
                )
                SELECT candidate.ticker, decision.as_of, decision.published_at,
                       decision.decision_revision, decision.policy_version,
                       decision.opportunity_episode_id,
                       compact_candidate.fast_cash AS funnel_fast_path,
                       funnel_candidate.required AS funnel_candidate_required,
                       CASE WHEN funnel_candidate.required
                                      AND episode_lineage.valid
                                      AND episode_lineage.within_limit
                            THEN jsonb_build_object(
                                'contract_version', decision.opportunity_episode->'contract_version',
                                'episode_id', decision.opportunity_episode->'episode_id',
                                'ticker', decision.opportunity_episode->'ticker',
                                'decision_revision', decision.opportunity_episode->'decision_revision',
                                'policy_version', decision.opportunity_episode->'policy_version',
                                'cutoff', decision.opportunity_episode->'cutoff',
                                'input_lineage', decision.opportunity_episode->'input_lineage',
                                'expressions', decision.opportunity_episode->'expressions',
                                'selected_expression', decision.opportunity_episode->'selected_expression'
                            )
                       END AS opportunity_episode,
                       CASE WHEN funnel_candidate.required
                            THEN episode_lineage.valid
                       END AS opportunity_lineage_valid,
                       CASE WHEN funnel_candidate.required
                            THEN decision.opportunity_cutoff = decision.as_of
                       END AS opportunity_cutoff_match,
                       CASE WHEN funnel_candidate.required
                                  AND episode_lineage.valid
                                  AND episode_lineage.within_limit
                            THEN decision.opportunity_episode->'expressions' = decision.expressions
                       END AS opportunity_expressions_match,
                       CASE WHEN funnel_candidate.required
                                  AND episode_lineage.valid
                                  AND episode_lineage.within_limit
                            THEN decision.opportunity_episode->'selected_expression'
                                 IS NOT DISTINCT FROM decision.selected_expression
                       END AS opportunity_selected_expression_match,
                       CASE WHEN decision.selected_expression IS NULL THEN NULL
                           ELSE jsonb_strip_nulls(jsonb_build_object(
                               'kind', decision.selected_expression->'kind',
                               'horizon', decision.selected_expression->'horizon'
                           ))
                       END AS selected_expression,
                       coalesce(decision.expressions->'STOCK', decision.expressions->'stock')
                           AS stock_expression,
                       CASE WHEN compact_candidate.fast_cash THEN NULL
                            ELSE CASE
                                WHEN lower(coalesce(
                                    impact.stock_impact->>'availability',
                                    impact.stock_impact->>'availability_status',
                                    ''
                                )) = 'available'
                                THEN CASE WHEN octet_length(impact.stock_impact::text) <= 262144
                                     THEN jsonb_set(
                                         impact.stock_impact,
                                         '{input_lineage}',
                                         CASE WHEN episode_lineage.valid
                                                   AND episode_lineage.within_limit
                                              THEN decision.opportunity_episode->'input_lineage'
                                              ELSE '[]'::jsonb
                                         END,
                                         false
                                     )
                                END
                                WHEN octet_length(coalesce(
                                    impact.stock_impact->'blockers', '[]'::jsonb
                                )::text) <= 8192
                                THEN jsonb_build_object(
                                    'contract_version', impact.stock_impact->'contract_version',
                                    'impact_id', impact.stock_impact->'impact_id',
                                    'ticker', impact.stock_impact->'ticker',
                                    'symbol', impact.stock_impact->'symbol',
                                    'instrument_symbol', impact.stock_impact->'instrument_symbol',
                                    'opportunity_episode_id', impact.stock_impact->'opportunity_episode_id',
                                    'expression_kind', impact.stock_impact->'expression_kind',
                                    'expression', impact.stock_impact->'expression',
                                    'expression_identity', impact.stock_impact->'expression_identity',
                                    'decision_revision', impact.stock_impact->'decision_revision',
                                    'risk_policy_version', impact.stock_impact->'risk_policy_version',
                                    'policy_version', impact.stock_impact->'policy_version',
                                    'market_snapshot_id', impact.stock_impact->'market_snapshot_id',
                                    'snapshot_id', impact.stock_impact->'snapshot_id',
                                    'market_state_publication_id', impact.stock_impact->'market_state_publication_id',
                                    'cutoff', impact.stock_impact->'cutoff',
                                    'availability', impact.stock_impact->'availability',
                                    'availability_status', impact.stock_impact->'availability_status',
                                    'blockers', coalesce(impact.stock_impact->'blockers', '[]'::jsonb)
                                )
                            END
                       END AS stock_portfolio_impact,
                       CASE WHEN funnel_candidate.required
                                  AND decision.opportunity_episode->'input_lineage'
                                  = coalesce(
                                      decision.portfolio_impacts->'STOCK',
                                      decision.portfolio_impacts->'stock'
                                  )->'input_lineage'
                            THEN true
                            WHEN funnel_candidate.required THEN false
                       END AS impact_lineage_match,
                       CASE WHEN compact_candidate.fast_cash
                            THEN jsonb_build_object(
                                'contract_version', decision.resolution->'contract_version',
                                'lifecycle', decision.resolution->'lifecycle',
                                'eligibility', decision.resolution->'eligibility',
                                'authorization_mode', decision.resolution->'authorization_mode',
                                'data_quality', decision.resolution->'data_quality',
                                'action', decision.resolution->'action',
                                'trade_plan_id', decision.resolution->'trade_plan_id',
                                'primary_blocker', decision.resolution->'primary_blocker',
                                'blockers', coalesce(decision.resolution->'blockers', '[]'::jsonb),
                                'next_action', decision.resolution->'next_action',
                                'policy_version', decision.resolution->'policy_version',
                                'decision_revision', decision.resolution->'decision_revision',
                                'ticker', decision.resolution->'ticker'
                            )
                            WHEN octet_length(decision.resolution::text) <= 196608
                            THEN jsonb_build_object(
                                'contract_version', decision.resolution->'contract_version',
                                'lifecycle', decision.resolution->'lifecycle',
                                'eligibility', decision.resolution->'eligibility',
                                'status', decision.resolution->'status',
                                'authorization_mode', decision.resolution->'authorization_mode',
                                'authorization', decision.resolution->'authorization',
                                'data_quality', decision.resolution->'data_quality',
                                'data_quality_status', decision.resolution->'data_quality_status',
                                'action', decision.resolution->'action',
                                'trade_plan_id', decision.resolution->'trade_plan_id',
                                'primary_blocker', decision.resolution->'primary_blocker',
                                'blockers', CASE WHEN octet_length(coalesce(
                                    decision.resolution->'blockers', '[]'::jsonb
                                )::text) <= 8192
                                    THEN coalesce(decision.resolution->'blockers', '[]'::jsonb)
                                    ELSE '["decision_resolution_invalid"]'::jsonb
                                END,
                                'next_action', decision.resolution->'next_action',
                                'entry', CASE WHEN lower(coalesce(
                                    decision.resolution->>'eligibility', decision.resolution->>'status', ''
                                )) = 'actionable' THEN decision.resolution->'entry' END,
                                'size', CASE WHEN lower(coalesce(
                                    decision.resolution->>'eligibility', decision.resolution->>'status', ''
                                )) = 'actionable' THEN decision.resolution->'size' END,
                                'invalidation', CASE WHEN lower(coalesce(
                                    decision.resolution->>'eligibility', decision.resolution->>'status', ''
                                )) = 'actionable' THEN decision.resolution->'invalidation' END,
                                'exit', CASE WHEN lower(coalesce(
                                    decision.resolution->>'eligibility', decision.resolution->>'status', ''
                                )) = 'actionable' THEN decision.resolution->'exit' END,
                                'ttl', CASE WHEN lower(coalesce(
                                    decision.resolution->>'eligibility', decision.resolution->>'status', ''
                                )) = 'actionable' THEN decision.resolution->'ttl' END,
                                'portfolio_context', CASE WHEN lower(coalesce(
                                    decision.resolution->>'eligibility', decision.resolution->>'status', ''
                                )) = 'actionable' THEN decision.resolution->'portfolio_context' END,
                                'policy_version', decision.resolution->'policy_version',
                                'policy_revision', decision.resolution->'policy_revision',
                                'decision_revision', decision.resolution->'decision_revision',
                                'revision', decision.resolution->'revision',
                                'ticker', decision.resolution->'ticker'
                            )
                       END AS resolution,
                       decision.market_state_publication_id::text
                FROM candidate_keys candidate
                JOIN analysis.ticker_decision decision ON decision.id = candidate.id
                CROSS JOIN LATERAL (
                    SELECT lower(coalesce(decision.capital_action->>'action', ''))
                               IN ('avoid', 'no_trade', 'cash')
                               AND lower(coalesce(decision.selected_expression->>'kind', 'cash')) = 'cash'
                               AND lower(coalesce(
                                   coalesce(
                                       decision.expressions->'STOCK',
                                       decision.expressions->'stock'
                                   )->>'availability',
                                   coalesce(
                                       decision.expressions->'STOCK',
                                       decision.expressions->'stock'
                                   )->>'availability_status',
                                   ''
                               )) <> 'available'
                           AS fast_cash
                ) compact_candidate
                CROSS JOIN LATERAL (
                    SELECT CASE WHEN compact_candidate.fast_cash THEN NULL
                                ELSE coalesce(
                                    decision.portfolio_impacts->'STOCK',
                                    decision.portfolio_impacts->'stock'
                                )
                           END AS stock_impact
                ) impact
                CROSS JOIN LATERAL (
                    SELECT CASE WHEN compact_candidate.fast_cash THEN false ELSE (
                        lower(coalesce(
                            coalesce(
                                decision.expressions->'STOCK',
                                decision.expressions->'stock'
                            )->>'availability',
                            coalesce(
                                decision.expressions->'STOCK',
                                decision.expressions->'stock'
                            )->>'availability_status',
                            ''
                        )) = 'available'
                        OR lower(coalesce(
                            impact.stock_impact->>'availability',
                            impact.stock_impact->>'availability_status',
                            ''
                        )) = 'available'
                        OR lower(coalesce(decision.selected_expression->>'kind', '')) NOT IN ('', 'cash')
                        OR lower(coalesce(
                            decision.resolution->>'eligibility',
                            decision.resolution->>'status',
                            ''
                        )) <> 'blocked'
                    ) END AS required
                ) funnel_candidate
                CROSS JOIN LATERAL (
                    SELECT CASE WHEN funnel_candidate.required THEN CASE
                        WHEN jsonb_typeof(decision.opportunity_episode->'input_lineage') = 'array'
                        THEN jsonb_array_length(decision.opportunity_episode->'input_lineage') > 0
                             AND jsonb_typeof(decision.opportunity_episode->'input_lineage'->0) = 'object'
                        ELSE false
                    END END AS valid,
                    CASE WHEN funnel_candidate.required
                         THEN octet_length(decision.opportunity_episode::text) <= 262144
                    END AS within_limit
                ) episode_lineage
                WHERE candidate.current_row = 1
                  AND candidate.authority_count = 1
                  AND candidate.opportunity_authority_count = 1
                """,
                [reference, reference],
            ).fetchall()
        return sorted(
            (dict(row) for row in rows),
            key=lambda row: str(row.get("ticker") or ""),
        )

    def refresh_outcomes(
        self,
        *,
        now: datetime | None = None,
        limit: int = 2_000,
        symbols: Iterable[str] | None = None,
    ) -> dict[str, int]:
        reference = _utc(now or datetime.now(UTC))
        selected = (
            None
            if symbols is None
            else sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})
        )
        if selected == []:
            return {"evaluated": 0, "updated": 0, "resolved": 0}
        filters = ["decision.status IN ('published', 'superseded')"]
        parameters: list[Any] = []
        if selected is not None:
            filters.append("instrument.symbol = ANY(%s)")
            parameters.append(selected)
        filters.append("decision.as_of <= %s")
        parameters.append(reference)
        parameters.append(max(1, min(int(limit), 10_000)))
        with self.runtime.read(JOB_PROFILE) as connection:
            decisions = connection.execute(
                f"""
                SELECT decision.id::text AS decision_id, instrument.id AS instrument_id,
                       instrument.symbol AS ticker, decision.as_of,
                       decision.contract_version, decision.decision_revision,
                       decision.tactical, decision.fundamental,
                       decision.capital_action, decision.resolution,
                       decision.policy_version, decision.opportunity_episode_id,
                       decision.opportunity_cutoff, decision.opportunity_episode,
                       decision.risk_policy, decision.expressions,
                       decision.selected_expression, decision.data_requests,
                       decision.learning_history, decision.input_manifest,
                       decision.market_state_publication_id::text,
                       decision.market_state_snapshot, decision.portfolio_impacts,
                       decision.risk_policy_snapshot
                FROM analysis.ticker_decision decision
                JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
                WHERE {" AND ".join(filters)}
                ORDER BY decision.as_of, decision.id
                LIMIT %s
                """,
                parameters,
            ).fetchall()
        updated = 0
        resolved = 0
        for decision in decisions:
            row = dict(decision)
            for horizon, sessions in HORIZON_SESSIONS.items():
                for horizon_sessions in sessions:
                    outcome = self._evaluate(row, horizon, horizon_sessions, reference)
                    selected_expression = dict(row.get("selected_expression") or {})
                    plan, plan_blocker = plan_authority(row)
                    outcome["trade_plan_id"] = plan.trade_plan_id if plan else None
                    outcome["plan_authority"] = "canonical" if plan else "legacy_or_invalid"
                    outcome["plan_blocker"] = plan_blocker
                    outcome["selected_expression_identity"] = plan.selected_expression_identity if plan else None
                    outcome["evaluation_cutoff"] = reference
                    self._store_outcome(
                        row["decision_id"], horizon, horizon_sessions, outcome,
                        selected_expression=str(selected_expression.get("kind") or "STOCK"),
                    )
                    updated += 1
                    resolved += int(outcome["state"] == "resolved")
        return {"evaluated": len(decisions), "updated": updated, "resolved": resolved}

    def publish_outcome_attributions(
        self,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Publish the complete plan-bound outcome set for the canonical scope."""

        reference = _utc(now or datetime.now(UTC))
        with self.runtime.read(JOB_PROFILE) as connection:
            rows = connection.execute(
                """
                SELECT decision.id::text AS decision_id, instrument.id AS instrument_id,
                       instrument.symbol AS ticker, decision.as_of,
                       decision.contract_version, decision.decision_revision,
                       decision.tactical, decision.fundamental,
                       decision.capital_action, decision.resolution,
                       decision.policy_version, decision.opportunity_episode_id,
                       decision.opportunity_cutoff, decision.opportunity_episode,
                       decision.risk_policy, decision.expressions,
                       decision.selected_expression, decision.data_requests,
                       decision.learning_history, decision.input_manifest,
                       decision.market_state_publication_id::text,
                       decision.market_state_snapshot, decision.portfolio_impacts,
                       decision.risk_policy_snapshot,
                       outcome.id::text AS outcome_id, outcome.horizon,
                       outcome.horizon_sessions, outcome.state,
                       outcome.measured_through, outcome.selected_expression AS outcome_selected_expression,
                       outcome.selected_return, outcome.stock_counterfactual_return,
                       outcome.alternate_counterfactual_return, outcome.cash_return,
                       outcome.sector_return, outcome.market_return, outcome.error_type,
                       outcome.mistake_card, outcome.available_at, outcome.metadata,
                       outcome.updated_at
                FROM analysis.ticker_decision decision
                JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
                LEFT JOIN analysis.ticker_outcome outcome
                  ON outcome.ticker_decision_id = decision.id
                WHERE decision.status IN ('published', 'superseded')
                  AND decision.as_of <= %s
                ORDER BY decision.as_of, decision.id, outcome.horizon, outcome.horizon_sessions
                """,
                [reference],
            ).fetchall()
            paper_rows = connection.execute(
                """
                SELECT paper.id::text AS paper_order_id,
                       paper.policy_result->>'trade_plan_id' AS trade_plan_id,
                       paper.status, paper.paper_only, paper.quantity,
                       paper.actual_fill_price, paper.filled_at, paper.filled_quantity,
                       paper.exit_at, paper.exit_price, paper.exited_quantity,
                       paper.fees, paper.entry_slippage, paper.exit_slippage,
                       paper.expression_kind, paper.structure, paper.created_at,
                       paper.updated_at,
                       paper.reserved_collateral, paper.max_loss, paper.policy_result,
                       paper.policy_result->>'entry_fill_count' AS entry_fill_count,
                       paper.policy_result->>'exit_fill_count' AS exit_fill_count,
                       (
                         SELECT CASE
                           WHEN count(*) = 0 THEN NULL
                           WHEN min(contract.multiplier) = max(contract.multiplier)
                             THEN min(contract.multiplier)
                           ELSE 0
                         END
                         FROM app.paper_order_leg leg
                         JOIN catalog.option_contract contract ON contract.id = leg.contract_id
                         WHERE leg.paper_order_id = paper.id
                       ) AS contract_multiplier
                FROM app.paper_order paper
                WHERE paper.lane = 'ticker'
                  AND paper.paper_only = TRUE
                  AND paper.policy_result->>'trade_plan_id' IS NOT NULL
                ORDER BY paper.policy_result->>'trade_plan_id', paper.created_at, paper.id
                """
            ).fetchall()

        by_decision: dict[str, list[dict[str, Any]]] = {}
        for raw in rows:
            by_decision.setdefault(str(raw["decision_id"]), []).append(dict(raw))
        paper_by_plan: dict[str, list[dict[str, Any]]] = {}
        for raw in paper_rows:
            paper_by_plan.setdefault(str(raw["trade_plan_id"]), []).append(dict(raw))

        attributions: list[OutcomeAttribution] = []
        blockers: list[str] = []
        legacy_exclusion_reasons: list[str] = []
        excluded_legacy = 0
        evaluated = 0
        seen_units: set[str] = set()
        for decision_rows in by_decision.values():
            evaluated += 1
            decision_row = decision_rows[0]
            plan, plan_blocker = plan_authority(decision_row)
            if plan is None:
                excluded_legacy += 1
                if plan_blocker:
                    legacy_exclusion_reasons.append(plan_blocker)
                continue
            if plan_blocker:
                blockers.append(plan_blocker)
                continue
            outcome_rows = [row for row in decision_rows if row["outcome_id"] is not None]
            by_horizon: dict[tuple[str, int], list[dict[str, Any]]] = {}
            for outcome in outcome_rows:
                key = (str(outcome["horizon"]), int(outcome["horizon_sessions"]))
                by_horizon.setdefault(key, []).append(outcome)
            expected = {(horizon.value, sessions) for horizon in HORIZON_SESSIONS for sessions in HORIZON_SESSIONS[horizon]}
            missing = expected - set(by_horizon)
            if missing:
                blockers.append(f"outcome_units_missing:{plan.trade_plan_id}")
                continue
            unexpected = set(by_horizon) - expected
            if unexpected:
                blockers.append(f"outcome_units_unexpected:{plan.trade_plan_id}")
                continue
            if any(len(items) != 1 for items in by_horizon.values()):
                blockers.append(f"outcome_units_duplicated:{plan.trade_plan_id}")
                continue
            paper, paper_blocker = paper_execution_for_plan(
                paper_by_plan.get(plan.trade_plan_id, []), reference,
            )
            if paper_blocker:
                blockers.append(f"{paper_blocker}:{plan.trade_plan_id}")
                continue
            for key in sorted(expected):
                outcome = by_horizon[key][0]
                stable_key = outcome_attribution_stable_key(plan.trade_plan_id, key[0], key[1])
                if stable_key in seen_units:
                    blockers.append(f"stable_unit_duplicated:{stable_key}")
                    continue
                attribution = _build_outcome_attribution(
                    plan, outcome, evaluation_cutoff=reference, paper_execution=paper,
                )
                if attribution is None:
                    blockers.append(f"outcome_authority_invalid:{stable_key}")
                    continue
                seen_units.add(stable_key)
                attributions.append(attribution)

        unique_blockers = list(dict.fromkeys(blockers))
        result: dict[str, Any] = {
            "status": "blocked" if unique_blockers or not attributions else "ok",
            "publication_status": "not_published" if unique_blockers or not attributions else "published",
            "evaluated_count": evaluated,
            "published_count": len(attributions) if not unique_blockers else 0,
            "excluded_legacy_count": excluded_legacy,
            "excluded_legacy_reasons": _blocker_counts(legacy_exclusion_reasons),
            "blockers": unique_blockers or (["no_plan_bound_attributions"] if not attributions else []),
            "blockers_by_reason": _blocker_counts(unique_blockers),
            "paper_only": True,
            "live_order_submission": False,
            "paper_orders": 0,
        }
        if unique_blockers or not attributions:
            return result

        models = [item.model_dump(mode="json") for item in attributions]
        analysis = AnalysisRepository(self.runtime)
        run_id = analysis.start_run(
            "ticker_outcome_attribution",
            input_cutoff=reference,
            code_version=attributions[0].evaluation_version,
            inputs={"outcome_attributions": models},
            feature_versions={"outcome_attribution": attributions[0].evaluation_version},
        )
        publication_id = analysis.publish(
            run_id,
            "ticker-outcome-attribution",
            {"outcome_attribution": models},
            validation={
                "scope": "ticker-outcome-attribution",
                "model": "outcome_attribution",
                "complete": True,
                "paper_only": True,
                "live_order_submission": False,
            },
            complete_run_summary={
                "evaluated_count": evaluated,
                "published_count": len(attributions),
                "excluded_legacy_count": excluded_legacy,
                "excluded_legacy_reasons": _blocker_counts(legacy_exclusion_reasons),
                "blockers": [],
                "paper_orders": 0,
            },
        )
        result.update({
            "attribution_publication_id": str(publication_id),
            "outcome_attribution_publication_id": str(publication_id),
        })
        return result

    def learning_surface(self, ticker: str) -> dict[str, Any]:
        decision = self.latest(ticker)
        canonical = AnalysisRepository(self.runtime).publication_rows(
            "ticker-outcome-attribution", "outcome_attribution", include_lineage=True,
        )
        current, attribution_blocker = select_current_outcome_attributions(
            canonical, decision.model_dump(mode="json") if decision else {},
        )
        outcomes = [_attribution_surface_row(row) for row in current]
        strategy_learning = evaluate_ticker_policy(current, canonical_only=True)
        if attribution_blocker:
            strategy_learning["blockers"] = list(dict.fromkeys([
                *strategy_learning.get("blockers", []), attribution_blocker,
            ]))
            strategy_learning["automatic_promotion"] = False
            strategy_learning["status"] = "collecting"
        if not decision or not outcomes:
            return {
                "independent_episode_count": 0,
                "disagreement": {},
                "expression_tournament": [],
                "mistake_cards": [],
                "strategy_learning": strategy_learning,
                "outcome_attributions": outcomes,
                "outcome_authority": "outcome-attribution.v1",
            }
        tactical = decision.tactical.model_dump(mode="json")
        fundamental = decision.fundamental.model_dump(mode="json")
        expressions = {
            kind.value if hasattr(kind, "value") else str(kind): value.model_dump(mode="json")
            for kind, value in decision.expressions.items()
        }
        return {
            "independent_episode_count": len({str(row.get("ticker_decision_id")) for row in outcomes}),
            "independent_horizon_episode_count": len({
                (str(row.get("ticker_decision_id")), str(row.get("horizon"))) for row in outcomes
            }),
            "effective_sample_count": len(outcomes),
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
                    "horizon": row["horizon"],
                    "horizon_sessions": row["horizon_sessions"],
                    "error_type": row["error_type"],
                    "card": row["mistake_card"] or {},
                }
                for row in outcomes if row["error_type"] or row["mistake_card"]
            ],
            "strategy_learning": strategy_learning,
            "outcome_attributions": outcomes,
            "outcome_authority": "outcome-attribution.v1",
        }

    def _store_manifest(self, connection: Any, decision_id: str, decision: TickerDecision) -> None:
        inputs = decision.input_manifest.inputs or {
            "decision_composer": [{
                "source": "deterministic-composer",
                "source_version": decision.input_manifest.code_version,
                "available_at": decision.as_of,
                "input_hash": decision.input_manifest.input_hash,
            }],
        }
        for field, values in inputs.items():
            for value in values if isinstance(values, list) else [values]:
                row = dict(value) if isinstance(value, dict) else {}
                available = _parse_datetime(row.get("available_at") or row.get("as_of"))
                if available is None or available > decision.as_of:
                    continue
                source = str(row.get("source") or row.get("source_id") or "unknown")
                connection.execute(
                    """
                    INSERT INTO analysis.ticker_input_manifest (
                        ticker_decision_id, field, source_id, source_version,
                        event_at, published_at, available_at, received_at, revision,
                        license, original_value, revised_value
                    ) VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    [
                        decision_id, field, source,
                        str(row.get("source_version") or row.get("version") or "") or None,
                        _parse_datetime(row.get("event_at") or row.get("event_time")),
                        _parse_datetime(row.get("published_at") or row.get("publication_time")),
                        available,
                        _parse_datetime(row.get("received_at") or row.get("receipt_time")),
                        str(row.get("revision") or "") or None,
                        str(row.get("license") or "") or None,
                        Jsonb(_jsonable(row.get("original_value") if row.get("original_value") is not None else row)),
                        Jsonb(_jsonable(row.get("revised_value") if row.get("revised_value") is not None else row)),
                    ],
                )

    def _evaluate(self, decision: dict[str, Any], horizon: Horizon, sessions: int, reference: datetime) -> dict[str, Any]:
        with self.runtime.read(JOB_PROFILE) as connection:
            entry_quote = connection.execute(
                """
                SELECT price, observed_at, available_at
                FROM raw.confirmed_quote
                WHERE instrument_id = %s
                  AND observed_at <= %s AND available_at <= %s
                ORDER BY observed_at DESC, available_at DESC
                LIMIT 1
                """,
                [decision["instrument_id"], decision["as_of"], decision["as_of"]],
            ).fetchone()
            entry = connection.execute(
                """
                SELECT close, available_at, observed_at
                FROM raw.confirmed_price_bar
                WHERE instrument_id = %s AND interval = '1d'
                  AND trading_date <= %s::date AND available_at <= %s
                ORDER BY trading_date DESC, available_at DESC LIMIT 1
                """,
                [decision["instrument_id"], decision["as_of"], decision["as_of"]],
            ).fetchone()
            trend_reference = connection.execute(
                """
                WITH one_bar_per_day AS (
                    SELECT DISTINCT ON (trading_date)
                           close, trading_date, observed_at, available_at, source_id
                    FROM raw.confirmed_price_bar
                    WHERE instrument_id = %s AND interval = '1d'
                      AND trading_date <= %s::date AND available_at <= %s
                    ORDER BY trading_date, available_at DESC, observed_at DESC, source_id
                )
                SELECT close, trading_date
                FROM one_bar_per_day
                ORDER BY trading_date DESC
                OFFSET 20 LIMIT 1
                """,
                [decision["instrument_id"], decision["as_of"], decision["as_of"]],
            ).fetchone()
            sector = connection.execute(
                """
                SELECT sector, delisted_at, delisting_price,
                       delisting_available_at, delisting_source
                FROM catalog.instrument
                WHERE id = %s
                """,
                [decision["instrument_id"]],
            ).fetchone()
            regime = connection.execute(
                """
                SELECT feature.trend_state, feature.volatility_state,
                       feature.as_of, feature.feature_version,
                       analysis_run.input_cutoff
                FROM analysis.symbol_feature feature
                JOIN analysis.run analysis_run ON analysis_run.id = feature.run_id
                WHERE feature.instrument_id = %s
                  AND feature.as_of <= %s
                  AND analysis_run.input_cutoff <= %s
                  AND feature.trend_state <> 'unavailable'
                  AND feature.data_quality_status <> 'unavailable'
                ORDER BY feature.as_of DESC, analysis_run.input_cutoff DESC, feature.id DESC
                LIMIT 1
                """,
                [decision["instrument_id"], decision["as_of"], decision["as_of"]],
            ).fetchone()
            benchmark = connection.execute(
                """
                SELECT exact_membership
                FROM analysis.ticker_benchmark_snapshot
                WHERE benchmark_key = 'market-equity-etf' AND available_at <= %s
                ORDER BY as_of DESC LIMIT 1
                """,
                [decision["as_of"]],
            ).fetchone()
            marks = connection.execute(
                """
                SELECT close, available_at, observed_at, trading_date
                FROM raw.confirmed_price_bar
                WHERE instrument_id = %s AND interval = '1d'
                  AND trading_date > %s::date AND available_at <= %s
                  AND (%s::timestamptz IS NULL OR trading_date <= %s::date)
                ORDER BY trading_date, available_at
                LIMIT %s
                """,
                [
                    decision["instrument_id"], decision["as_of"], reference,
                    sector["delisted_at"] if sector else None,
                    sector["delisted_at"] if sector else None,
                    sessions,
                ],
            ).fetchall()
            delisting_status = _delisting_status(sector, reference)
            terminal_mark = _terminal_delisting_mark(
                connection,
                instrument_id=decision["instrument_id"],
                lifecycle=sector,
                as_of=decision["as_of"],
                reference=reference,
            )
            if terminal_mark is not None and delisting_status == "delisted":
                delisting_status = "delisted_terminal"
        if entry is None or (not marks and terminal_mark is None):
            return {
                "state": "unmeasurable", "available_at": None,
                "observed_through": None,
                "selected_return": None, "stock_return": None,
                "alternate_counterfactual_return": None,
                "sector_return": None, "market_return": None,
                "cash_return": 0.0, "trend_counterfactual_return": None,
                "cost_adjusted_selected_return": None,
                "cost_adjusted_stock_counterfactual_return": None,
                "cost_adjusted_cash_return": 0.0,
                "expression_returns": {"CASH": 0.0}, "expression_marks": {},
                "error_type": None, "mistake_card": {},
                "learning_metadata": _learning_metadata(
                    as_of=decision["as_of"],
                    measured_through=decision["as_of"],
                    reference=reference,
                    sector=sector["sector"] if sector else None,
                    regime=regime,
                    delisting_status=delisting_status,
                    decision=decision,
                ),
            }
        entry_price = float(entry_quote["price"]) if entry_quote is not None else float(entry["close"])
        entry_date = entry_quote["observed_at"].date() if entry_quote is not None else entry["observed_at"].date()
        mark = terminal_mark or marks[-1]
        stock_return = float(mark["close"]) / entry_price - 1
        stock_cost_adjusted = _stock_cost_adjusted_return(stock_return)
        stock_mark = {
            "status": "delisted_terminal" if terminal_mark is not None else "estimated",
            "gross_return": stock_return,
            "cost_adjusted_return": stock_cost_adjusted,
            "cost_model_version": STOCK_COST_MODEL_VERSION,
            "cost_basis": "confirmed close with conservative round-trip execution allowance",
            "cost_per_side_bps": STOCK_COST_PER_SIDE_BPS,
            "evidence_state": "ESTIMATED",
            "entry_price": entry_price,
            "mark_price": float(mark["close"]),
            "entry_observed_at": entry["observed_at"],
            "entry_available_at": entry["available_at"],
            "observed_at": mark["observed_at"],
            "available_at": mark["available_at"],
        }
        trend_return = None
        if trend_reference is not None and float(trend_reference["close"] or 0) > 0:
            trend_return = stock_return if entry_price > float(trend_reference["close"]) else 0.0
        sector_symbols = self._peer_symbols(sector["sector"] if sector else None)
        market_members = list((benchmark or {}).get("exact_membership") or []) if benchmark else []
        sector_return = self._peer_return(
            sector_symbols, entry_date, mark["trading_date"], decision["as_of"], reference,
        )
        market_return = self._peer_return(
            market_members or ["SPY", "QQQ"], entry_date, mark["trading_date"], decision["as_of"], reference,
        )
        expression_returns, expression_marks = self._expression_returns(
            decision=decision,
            as_of=decision["as_of"],
            mark_date=mark["trading_date"],
            reference=reference,
        )
        expression_returns["STOCK"] = stock_return
        expression_marks["STOCK"] = stock_mark
        expression_marks["CASH"] = {
            "status": "measured",
            "gross_return": 0.0,
            "cost_adjusted_return": 0.0,
            "cost_model_version": "cash-zero-cost-v1",
            "evidence_state": "DERIVED",
        }
        fundamental = dict(decision["fundamental"] or {})
        stance = str(fundamental.get("stance") or "NEUTRAL")
        action = str((decision["capital_action"] or {}).get("action") or "")
        selected = dict(decision.get("selected_expression") or {})
        selected_kind = str(selected.get("kind") or "STOCK")
        selected_return = expression_returns.get(selected_kind)
        if action in {"AVOID", "WAIT_FOR_PRICE"}:
            selected_return = 0.0
        expression_costs = {
            kind: _number(metadata.get("cost_adjusted_return"))
            for kind, metadata in expression_marks.items()
            if _number(metadata.get("cost_adjusted_return")) is not None
        }
        cost_adjusted_selected = 0.0 if action in {"AVOID", "WAIT_FOR_PRICE"} else expression_costs.get(selected_kind)
        preferred_view = fundamental if stance != "NEUTRAL" else dict(decision["tactical"] or {})
        alternate_kind = str(preferred_view.get("alternate_expression") or "CASH")
        state = "resolved" if terminal_mark is not None or len(marks) >= sessions else "observing"
        error: str | None = None
        mistake_card: dict[str, Any] = {}
        if state == "resolved":
            error, mistake_card = _classify_mistake(
                stance=stance,
                action=action,
                selected_kind=selected_kind,
                selected_return=selected_return,
                stock_return=stock_return,
                alternate_return=expression_returns.get(alternate_kind),
            )
        return {
            "state": state,
            "available_at": mark["available_at"],
            "observed_through": mark["observed_at"],
            "selected_return": selected_return,
            "stock_return": stock_return,
            "cash_return": 0.0,
            "trend_counterfactual_return": trend_return,
            "cost_adjusted_selected_return": cost_adjusted_selected,
            "cost_adjusted_stock_counterfactual_return": stock_cost_adjusted,
            "cost_adjusted_cash_return": 0.0,
            "alternate_counterfactual_return": expression_returns.get(alternate_kind),
            "sector_return": sector_return,
            "market_return": market_return,
            "expression_returns": expression_returns,
            "expression_marks": expression_marks,
            "alternate_expression": alternate_kind,
            "learning_metadata": _learning_metadata(
                as_of=decision["as_of"],
                measured_through=mark["observed_at"],
                reference=reference,
                sector=sector["sector"] if sector else None,
                regime=regime,
                delisting_status=delisting_status,
                decision=decision,
            ),
            "error_type": error,
            "mistake_card": mistake_card,
        }

    def _expression_returns(
        self,
        *,
        decision: dict[str, Any],
        as_of: datetime,
        mark_date: Any,
        reference: datetime,
    ) -> tuple[dict[str, float | None], dict[str, dict[str, Any]]]:
        """Evaluate options against later executable quotes for this ticker."""

        expressions = dict(decision.get("expressions") or {})
        returns: dict[str, float | None] = {"CASH": 0.0}
        marks: dict[str, dict[str, Any]] = {}
        with self.runtime.read(JOB_PROFILE) as connection:
            for raw_kind, raw_expression in expressions.items():
                kind = str(raw_kind)
                if kind in {"STOCK", "CASH"}:
                    continue
                value, metadata = self._option_expression_return(
                    connection,
                    dict(raw_expression or {}),
                    as_of=as_of,
                    mark_date=mark_date,
                    reference=reference,
                )
                returns[kind] = value
                marks[kind] = metadata
        return returns, marks

    def _option_expression_return(
        self,
        connection: Any,
        expression: dict[str, Any],
        *,
        as_of: datetime,
        mark_date: Any,
        reference: datetime,
    ) -> tuple[float | None, dict[str, Any]]:
        """Use the next feasible quote package; never use a midpoint fallback."""

        legs = [dict(leg) for leg in expression.get("legs") or [] if isinstance(leg, dict)]
        contract_ids: list[int] = []
        for leg in legs:
            try:
                contract_ids.append(int(str(leg.get("contract_id") or "")))
            except (TypeError, ValueError):
                return None, {
                    "status": "unmeasurable", "evidence_state": OutcomeEvidenceState.UNMEASURABLE.value,
                    "reason": "contract_id_missing",
                }
        if not legs or len(contract_ids) != len(legs):
            return None, {
                "status": "unmeasurable", "evidence_state": OutcomeEvidenceState.UNMEASURABLE.value,
                "reason": "option_legs_missing",
            }
        entry_package = package_price(legs, phase="entry")
        if entry_package is None or entry_package <= 0:
            return None, {
                "status": "unmeasurable", "evidence_state": OutcomeEvidenceState.UNMEASURABLE.value,
                "reason": "entry_executable_quote_missing",
            }
        rows = connection.execute(
            """
            WITH valid_quotes AS (
                SELECT DISTINCT ON (quote.snapshot_id, quote.contract_id)
                       quote.snapshot_id, quote.contract_id, quote.bid, quote.ask,
                       quote.bid_size, quote.ask_size, quote.observed_at,
                       quote.available_at, contract.multiplier
                FROM raw.option_quote quote
                JOIN raw.option_snapshot snapshot ON snapshot.id = quote.snapshot_id
                JOIN catalog.option_contract contract ON contract.id = quote.contract_id
                WHERE quote.contract_id = ANY(%s::bigint[])
                  AND quote.observed_at > %s
                  AND quote.observed_at::date <= %s
                  AND quote.available_at <= %s
                  AND snapshot.capture_state IN ('complete', 'partial')
                  AND quote.bid IS NOT NULL AND quote.ask IS NOT NULL
                  AND quote.bid > 0 AND quote.ask >= quote.bid
                ORDER BY quote.snapshot_id, quote.contract_id,
                         quote.observed_at DESC, quote.available_at DESC, quote.id DESC
            ), complete_snapshots AS (
                SELECT snapshot_id, max(observed_at) AS observed_at,
                       max(available_at) AS available_at
                FROM valid_quotes
                GROUP BY snapshot_id
                HAVING count(DISTINCT contract_id) = %s
            )
            SELECT valid_quotes.*
            FROM valid_quotes
            JOIN complete_snapshots USING (snapshot_id)
            ORDER BY complete_snapshots.observed_at DESC,
                     complete_snapshots.available_at DESC, valid_quotes.contract_id
            """,
            [contract_ids, as_of, mark_date, reference, len(contract_ids)],
        ).fetchall()
        snapshots: dict[Any, dict[int, Any]] = {}
        for row in rows:
            snapshots.setdefault(row["snapshot_id"], {})[int(row["contract_id"])] = row
        complete = [
            by_contract
            for by_contract in snapshots.values()
            if len(by_contract) == len(contract_ids)
        ]
        if not complete:
            return None, {
                "status": "unmeasurable",
                "evidence_state": OutcomeEvidenceState.UNMEASURABLE.value,
                "reason": "next_feasible_option_quote_missing",
                "contracts_found": sorted({int(row["contract_id"]) for row in rows}),
            }
        by_contract = complete[0]
        mark_legs = [
            {
                **leg,
                "bid": by_contract[int(leg["contract_id"])]["bid"],
                "ask": by_contract[int(leg["contract_id"])]["ask"],
                "bid_size": by_contract[int(leg["contract_id"])]["bid_size"],
                "ask_size": by_contract[int(leg["contract_id"])]["ask_size"],
            }
            for leg in legs
        ]
        mark_package = package_price(mark_legs, phase="exit")
        if mark_package is None:
            return None, {
                "status": "unmeasurable", "evidence_state": OutcomeEvidenceState.UNMEASURABLE.value,
                "reason": "exit_executable_quote_missing",
            }
        max_loss = _number(expression.get("max_loss_per_unit"))
        multiplier = max(1, int(by_contract[next(iter(by_contract))]["multiplier"] or 100))
        denominator = max_loss if max_loss and max_loss > 0 else entry_package * multiplier
        kind = str(expression.get("kind") or "").lower()
        gross_pnl = (
            (entry_package - mark_package) * multiplier
            if is_credit_structure(kind)
            else (mark_package - entry_package) * multiplier
        )
        fees = FEE_PER_CONTRACT_LEG * len(legs) * 2
        return float(gross_pnl / denominator), {
            "status": "measured",
            "evidence_state": OutcomeEvidenceState.OBSERVED.value,
            "entry_package": entry_package,
            "mark_package": mark_package,
            "multiplier": multiplier,
            "gross_return": gross_pnl / denominator,
            "fees": fees,
            "cost_adjusted_return": (gross_pnl - fees) / denominator,
            "cost_model_version": "option-executable-quotes-fees-v1",
            "mark_quote_time": max(row["observed_at"] for row in by_contract.values()),
            "mark_available_at": max(row["available_at"] for row in by_contract.values()),
            "observed_through": max(row["observed_at"] for row in by_contract.values()),
            "available_at": max(row["available_at"] for row in by_contract.values()),
            "snapshot_id": str(next(iter(by_contract.values()))["snapshot_id"]),
            "contract_ids": contract_ids,
        }

    def _peer_symbols(self, sector: Any) -> list[str]:
        if not sector:
            return []
        with self.runtime.read(JOB_PROFILE) as connection:
            rows = connection.execute(
                """
                SELECT symbol FROM catalog.instrument
                WHERE asset_class = 'equity' AND sector = %s
                ORDER BY symbol
                """,
                [sector],
            ).fetchall()
        return [str(row["symbol"]).upper() for row in rows if row["symbol"]]

    def _peer_return(
        self,
        symbols: list[str],
        entry_date: Any,
        mark_date: Any,
        as_of: datetime,
        reference: datetime,
    ) -> float | None:
        if not symbols:
            return None
        with self.runtime.read(JOB_PROFILE) as connection:
            row = connection.execute(
                _PEER_RETURN_QUERY,
                [symbols, entry_date, as_of, mark_date, reference],
            ).fetchone()
        return _number(row["return"]) if row else None

    def _store_outcome(
        self,
        decision_id: str,
        horizon: Horizon,
        sessions: int,
        outcome: dict[str, Any],
        *,
        selected_expression: str,
    ) -> None:
        with self.runtime.transaction(JOB_PROFILE) as connection:
            connection.execute(
                """
                INSERT INTO analysis.ticker_outcome (
                    ticker_decision_id, horizon, horizon_sessions, state,
                    measured_through, selected_expression, selected_return,
                    stock_counterfactual_return, alternate_counterfactual_return,
                    cash_return, sector_return,
                    market_return, error_type, mistake_card, available_at,
                    metadata, updated_at
                ) VALUES (
                    %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()
                )
                ON CONFLICT (ticker_decision_id, horizon, horizon_sessions) DO UPDATE SET
                    state = EXCLUDED.state, measured_through = EXCLUDED.measured_through,
                    selected_return = EXCLUDED.selected_return,
                    stock_counterfactual_return = EXCLUDED.stock_counterfactual_return,
                    alternate_counterfactual_return = EXCLUDED.alternate_counterfactual_return,
                    cash_return = EXCLUDED.cash_return, error_type = EXCLUDED.error_type,
                    sector_return = EXCLUDED.sector_return, market_return = EXCLUDED.market_return,
                    mistake_card = EXCLUDED.mistake_card, available_at = EXCLUDED.available_at,
                    metadata = EXCLUDED.metadata, updated_at = now()
                """,
                [
                    decision_id, horizon.value, sessions, outcome["state"],
                    outcome.get("observed_through") or outcome.get("available_at"), selected_expression,
                    outcome["selected_return"], outcome["stock_return"],
                    outcome.get("alternate_counterfactual_return"),
                    outcome.get("cash_return", 0.0),
                    outcome.get("sector_return"), outcome.get("market_return"),
                    outcome["error_type"], Jsonb(outcome["mistake_card"]),
                    outcome["available_at"], Jsonb({
                        "episode_unit": "ticker_decision",
                        "selected_expression": selected_expression,
                        "alternate_expression": outcome.get("alternate_expression"),
                        "expression_returns": _jsonable(outcome.get("expression_returns") or {}),
                        "expression_marks": _jsonable(outcome.get("expression_marks") or {}),
                        "trend_counterfactual_return": outcome.get("trend_counterfactual_return"),
                        "cost_adjusted_selected_return": outcome.get("cost_adjusted_selected_return"),
                        "cost_adjusted_stock_counterfactual_return": outcome.get("cost_adjusted_stock_counterfactual_return"),
                        "cost_adjusted_cash_return": outcome.get("cost_adjusted_cash_return", 0.0),
                        "cost_model_version": outcome.get("cost_model_version") or "mixed-expression-cost-model-v1",
                        "observed_through": _jsonable(outcome.get("observed_through")),
                        "available_at": _jsonable(outcome.get("available_at")),
                        "evaluation_cutoff": _jsonable(outcome.get("evaluation_cutoff")),
                        "trade_plan_id": outcome.get("trade_plan_id"),
                        "selected_expression_identity": outcome.get("selected_expression_identity"),
                        "plan_authority": outcome.get("plan_authority") or "legacy_or_invalid",
                        "plan_blocker": outcome.get("plan_blocker"),
                        **dict(outcome.get("learning_metadata") or {}),
                    }),
                ],
            )


def plan_authority(row: Any) -> tuple[TradePlan | None, str | None]:
    """Resolve the persisted plan and its copied ranking authority exactly once."""

    try:
        decision = _decision_from_row(row)
    except (TypeError, ValueError, KeyError):
        return None, "ticker_decision_lineage_invalid"
    plan = decision.trade_plan
    if plan is None:
        return None, "trade_plan_missing"
    if plan.eligibility != "ACTIONABLE" or plan.availability_status is not AvailabilityStatus.AVAILABLE:
        return plan, plan.primary_blocker or "trade_plan_unavailable"
    if not plan.publication_id:
        return plan, "trade_plan_publication_missing"
    manifest = dict(row.get("input_manifest") or {}) if hasattr(row, "get") else {}
    rank = manifest.get("opportunity_rank")
    if not isinstance(rank, dict):
        return plan, "opportunity_rank_missing"
    ranking_publication_id = str(
        rank.get("ranking_publication_id") or rank.get("publication_id") or ""
    )
    if ranking_publication_id != plan.publication_id:
        return plan, "ranking_publication_mismatch"
    exact_rank_fields = {
        "rank_id": plan.rank_id,
        "alpha_signal_id": plan.alpha_signal_id,
        "portfolio_impact_id": plan.portfolio_impact_id,
        "market_snapshot_id": plan.market_snapshot_id,
        "market_state_publication_id": plan.market_state_publication_id,
        "selected_expression_kind": plan.selected_expression_kind.value,
        "selected_expression_identity": plan.selected_expression_identity,
        "opportunity_episode_id": plan.opportunity_episode_id,
        "decision_revision": plan.decision_revision,
        "policy_version": plan.policy_version,
    }
    for field, expected in exact_rank_fields.items():
        if not expected or str(rank.get(field) or "") != str(expected):
            return plan, f"trade_plan_{field}_mismatch"
    signals = [
        signal for signal in manifest.get("alpha_signals") or []
        if isinstance(signal, dict) and str(signal.get("signal_id") or "") == str(plan.alpha_signal_id)
    ]
    if len(signals) != 1:
        return plan, "alpha_signal_missing_or_duplicated"
    signal = signals[0]
    signal_forecast_id = str(signal.get("strategy_forecast_id") or "")
    plan_forecast_id = str(plan.strategy_forecast_id or "")
    rank_forecast_id = str(rank.get("strategy_forecast_id") or "")
    if signal.get("contract_version") == "alpha-signal.v1" and not signal_forecast_id:
        return plan, "trade_plan_strategy_forecast_missing"
    if (plan_forecast_id or rank_forecast_id or signal_forecast_id) and not (
        plan_forecast_id and rank_forecast_id and signal_forecast_id
        and plan_forecast_id == rank_forecast_id == signal_forecast_id
    ):
        return plan, "trade_plan_strategy_forecast_mismatch"
    return plan, None


def paper_execution_for_plan(
    rows: list[dict[str, Any]], reference: datetime,
) -> tuple[PaperExecutionOutcome | None, str | None]:
    if len(rows) > 1:
        return None, "paper_execution_duplicated"
    if not rows:
        return None, None
    row = rows[0]
    available_at = _parse_datetime(row.get("updated_at") or row.get("created_at"))
    if available_at is not None and available_at > reference:
        return None, "paper_execution_future_available"
    filled = _number(row.get("filled_quantity"))
    exited = _number(row.get("exited_quantity"))
    fill_price = _number(row.get("actual_fill_price"))
    exit_price = _number(row.get("exit_price"))
    fill_at = _parse_datetime(row.get("filled_at"))
    exit_at = _parse_datetime(row.get("exit_at"))
    realized_gross: float | None = None
    realized_net: float | None = None
    expression_kind = str(row.get("expression_kind") or "").upper()
    structure = str(row.get("structure") or "").lower()
    option_expression = expression_kind in {"CALL", "PUT", "DEBIT_SPREAD", "CASH_SECURED_PUT"} or structure in {
        "long_call", "long_put", "debit_spread", "cash_secured_put",
    }
    multiplier = 1.0 if not option_expression else _number(row.get("contract_multiplier"))
    if option_expression and (multiplier is None or multiplier <= 0):
        return None, "paper_execution_multiplier_missing"
    policy = row.get("policy_result") if isinstance(row.get("policy_result"), dict) else {}
    entry_fill_count = _integer(
        row.get("entry_fill_count") if row.get("entry_fill_count") not in (None, "")
        else policy.get("entry_fill_count")
    )
    exit_fill_count = _integer(
        row.get("exit_fill_count") if row.get("exit_fill_count") not in (None, "")
        else policy.get("exit_fill_count")
    )
    if (entry_fill_count is not None and entry_fill_count > 1) or (
        exit_fill_count is not None and exit_fill_count > 1
    ):
        return None, "paper_execution_multiple_fills"
    if (
        str(row.get("status") or "").lower() == "exited"
        and filled and filled > 0 and exited and exited >= filled
        and fill_price is not None and fill_price > 0 and exit_price is not None
    ):
        gross_pnl = (
            (fill_price - exit_price) * multiplier * filled
            if is_credit_structure(str(row.get("structure") or "").lower())
            else (exit_price - fill_price) * multiplier * filled
        )
        if is_credit_structure(structure):
            per_unit_collateral = _number(row.get("max_loss")) or _number(policy.get("max_loss_per_unit"))
            collateral = _number(row.get("reserved_collateral")) or (
                per_unit_collateral * filled if per_unit_collateral is not None else None
            )
            if collateral is None or collateral <= 0:
                return None, "paper_execution_collateral_missing"
            denominator = collateral
        else:
            denominator = fill_price * multiplier * filled
        realized_gross = gross_pnl / denominator
        realized_net = (gross_pnl - (_number(row.get("fees")) or 0.0)) / denominator
    try:
        execution = PaperExecutionOutcome.model_validate({
            "trade_plan_id": str(row.get("trade_plan_id") or ""),
            "paper_order_id": row.get("paper_order_id"),
            "status": str(row.get("status") or "MISSING").upper(),
            "evidence_state": OutcomeEvidenceState.OBSERVED.value,
            "paper_only": bool(row.get("paper_only", True)),
            "entry_filled_at": fill_at,
            "exit_at": exit_at,
            "entry_fill_price": fill_price,
            "exit_price": exit_price,
            "filled_quantity": filled,
            "exited_quantity": exited,
            "fees": _number(row.get("fees")),
            "entry_slippage": _number(row.get("entry_slippage")),
            "exit_slippage": _number(row.get("exit_slippage")),
            "contract_multiplier": multiplier,
            "entry_fill_count": entry_fill_count,
            "exit_fill_count": exit_fill_count,
            "realized_gross_return": realized_gross,
            "realized_net_return": realized_net,
            "observed_through": max(
                (value for value in (fill_at, exit_at, available_at) if value is not None),
                default=None,
            ),
            "available_at": available_at,
        })
    except (TypeError, ValueError):
        return None, "paper_execution_invalid"
    return execution, None


def _paper_execution_matches_horizon(
    plan: TradePlan, execution: PaperExecutionOutcome, horizon_sessions: int,
) -> bool:
    if execution.exit_at is None:
        return False
    first_day = _utc(plan.cutoff).date() + timedelta(days=1)
    last_day = _utc(execution.exit_at).date()
    elapsed = 0
    current = first_day
    while current <= last_day:
        elapsed += int(is_us_market_day(current))
        current += timedelta(days=1)
    return elapsed == int(horizon_sessions)


def _build_outcome_attribution(
    plan: TradePlan,
    outcome: dict[str, Any],
    *,
    evaluation_cutoff: datetime,
    paper_execution: PaperExecutionOutcome | None,
) -> OutcomeAttribution | None:
    metadata = dict(outcome.get("metadata") or {})
    marks = dict(metadata.get("expression_marks") or {})
    returns = dict(metadata.get("expression_returns") or {})
    selected_kind = str(
        outcome.get("outcome_selected_expression")
        or outcome.get("selected_expression")
        or metadata.get("selected_expression")
        or plan.selected_expression_kind.value
    ).upper()
    if selected_kind != plan.selected_expression_kind.value:
        return None
    selected_identity = metadata.get("selected_expression_identity")
    if str(selected_identity or "") != plan.selected_expression_identity:
        return None
    observed_through = _parse_datetime(
        outcome.get("measured_through") or metadata.get("observed_through")
    )
    available_at = _parse_datetime(outcome.get("available_at") or metadata.get("available_at"))
    expression_kinds = sorted(set(returns) | set(marks) | {"STOCK", "CASH", selected_kind})
    expression_values = {
        str(kind).upper(): _number(
            returns.get(kind) if kind in returns else returns.get(str(kind).upper())
        )
        for kind in expression_kinds
    }
    if expression_values.get(selected_kind) is None:
        expression_values[selected_kind] = _number(outcome.get("selected_return"))
    all_expressions = {
        str(kind).upper(): _evidence_from_mark(
            str(kind), marks.get(kind) or marks.get(str(kind).upper()),
            gross_return=expression_values[str(kind).upper()],
            default_observed=observed_through,
            default_available=available_at,
        )
        for kind in expression_kinds
    }
    stock = all_expressions.get("STOCK") or _evidence_from_mark(
        "STOCK", marks.get("STOCK"),
        gross_return=_number(outcome.get("stock_counterfactual_return")),
        default_observed=observed_through, default_available=available_at,
    )
    cash = all_expressions.get("CASH") or _evidence_from_mark(
        "CASH", marks.get("CASH"), gross_return=_number(outcome.get("cash_return")) or 0.0,
        default_observed=None, default_available=None,
    )
    special = {
        "STOCK": stock,
        "CASH": cash,
        "TREND": _counterfactual_evidence("TREND", metadata.get("trend_counterfactual_return"), observed_through, available_at),
        "SECTOR": _counterfactual_evidence("SECTOR", outcome.get("sector_return"), observed_through, available_at),
        "MARKET": _counterfactual_evidence("MARKET", outcome.get("market_return"), observed_through, available_at),
    }
    alternate_kind = str(metadata.get("alternate_expression") or "CASH").upper()
    special["ALTERNATE_EXPRESSION"] = all_expressions.get(alternate_kind) or _evidence_from_mark(
        alternate_kind, marks.get(alternate_kind),
        gross_return=_number(outcome.get("alternate_counterfactual_return")),
        default_observed=observed_through, default_available=available_at,
    )
    for evidence in (*all_expressions.values(), *special.values()):
        if evidence.gross_return is not None and evidence.kind != "CASH" and (
            evidence.observed_at is None or evidence.available_at is None
        ):
            return None
    selected_evidence = all_expressions.get(selected_kind) or _evidence_from_mark(
        selected_kind, marks.get(selected_kind),
        gross_return=_number(outcome.get("selected_return")),
        default_observed=observed_through, default_available=available_at,
    )
    realized = bool(
        paper_execution is not None
        and paper_execution.status == "EXITED"
        and paper_execution.entry_fill_count == 1
        and paper_execution.exit_fill_count == 1
        and paper_execution.entry_filled_at is not None
        and paper_execution.exit_at is not None
        and paper_execution.entry_fill_price is not None
        and paper_execution.exit_price is not None
        and paper_execution.filled_quantity
        and paper_execution.exited_quantity
        and paper_execution.realized_gross_return is not None
        and paper_execution.realized_net_return is not None
    )
    state = str(outcome.get("state") or "unmeasurable").upper()
    horizon_realized = bool(
        realized and paper_execution is not None and _paper_execution_matches_horizon(
            plan, paper_execution, int(outcome["horizon_sessions"]),
        )
    )
    sample_eligible = state == OutcomeAttributionState.RESOLVED.value and horizon_realized and selected_kind != "CASH"
    fill_history_proven = bool(
        paper_execution is not None
        and paper_execution.entry_fill_count == 1
        and paper_execution.exit_fill_count == 1
    )
    primary_blocker = None if sample_eligible else (
        "paper_execution_evidence_missing" if paper_execution is None
        else "paper_execution_fill_history_unproven" if not fill_history_proven
        else "paper_execution_not_exited" if not realized
        else "paper_execution_horizon_mismatch" if not horizon_realized
        else "outcome_not_resolved" if state != OutcomeAttributionState.RESOLVED.value
        else "cash_is_not_executable"
    )
    base: dict[str, Any] = {
        "contract_version": OUTCOME_ATTRIBUTION_CONTRACT_VERSION,
        "stable_unit_key": outcome_attribution_stable_key(plan.trade_plan_id, outcome["horizon"], outcome["horizon_sessions"]),
        "publication_id": None,
        "evaluation_version": OUTCOME_ATTRIBUTION_EVALUATION_VERSION,
        "ticker": plan.ticker,
        "trade_plan_id": plan.trade_plan_id,
        "trade_plan_publication_id": plan.publication_id,
        "opportunity_episode_id": plan.opportunity_episode_id,
        "decision_revision": plan.decision_revision,
        "policy_version": plan.policy_version,
        "selected_expression_kind": selected_kind,
        "selected_expression_identity": plan.selected_expression_identity,
        "rank_id": plan.rank_id,
        "alpha_signal_id": plan.alpha_signal_id,
        "portfolio_impact_id": plan.portfolio_impact_id,
        "market_snapshot_id": plan.market_snapshot_id,
        "market_state_publication_id": plan.market_state_publication_id,
        "decision_cutoff": plan.cutoff,
        "evaluation_cutoff": evaluation_cutoff,
        "decision_input_lineage": plan.input_lineage,
        "horizon": outcome["horizon"],
        "horizon_sessions": outcome["horizon_sessions"],
        "state": state,
        "observed_through": observed_through,
        "available_at": available_at,
        "outcome_evidence": tuple(all_expressions[kind] for kind in sorted(all_expressions)),
        "selected_evidence": selected_evidence,
        "selected_gross_return": _number(outcome.get("selected_return")),
        "selected_net_return": _number(metadata.get("cost_adjusted_selected_return")),
        "realized_gross_return": paper_execution.realized_gross_return if horizon_realized else None,
        "realized_net_return": paper_execution.realized_net_return if horizon_realized else None,
        "counterfactuals": special,
        "all_expression_counterfactuals": all_expressions,
        "cost_model_version": str(metadata.get("cost_model_version") or "mixed-expression-cost-model-v1"),
        "evidence_state": selected_evidence.evidence_state,
        "paper_execution": paper_execution,
        "sample_eligible": sample_eligible,
        "promotion_eligible": sample_eligible,
        "primary_blocker": primary_blocker,
        "next_action": (
            "Use this resolved paper execution for promotion evidence."
            if sample_eligible
            else "Complete an exact paper fill and exit for this TradePlan before using the outcome for promotion."
        ),
        "mistake_classification": outcome.get("error_type"),
        "mistake_card": outcome.get("mistake_card") or {},
        "learning_metadata": {
            **metadata,
            "canonical_authority": True,
            "sample_eligible": sample_eligible,
            "promotion_eligible": sample_eligible,
        },
    }
    try:
        return OutcomeAttribution.model_validate(base)
    except (TypeError, ValueError, KeyError):
        return None


def _evidence_from_mark(
    kind: str,
    mark: Any,
    *,
    gross_return: float | None,
    default_observed: datetime | None,
    default_available: datetime | None,
) -> OutcomeEvidence:
    details = dict(mark) if isinstance(mark, dict) else {}
    status = str(details.get("status") or ("measured" if gross_return is not None else "unmeasurable"))
    missing = gross_return is None and status.lower() in {"unmeasurable", "missing"}
    observed = _parse_datetime(
        details.get("observed_at") or details.get("observed_through") or details.get("mark_quote_time")
    )
    available = _parse_datetime(details.get("available_at") or details.get("mark_available_at"))
    if observed is None and gross_return is not None and kind.upper() != "CASH":
        observed = default_observed
    if available is None and gross_return is not None and kind.upper() != "CASH":
        available = default_available
    return OutcomeEvidence.model_validate({
        "evidence_id": str(details.get("evidence_id") or f"{kind.lower()}-outcome"),
        "kind": kind.upper(),
        "source_id": details.get("source_id") or (
            "raw.option_quote" if kind.upper() not in {"STOCK", "CASH"} else "confirmed_price_bar"
        ),
        "source_version": details.get("source_version") or details.get("snapshot_id"),
        "observed_at": observed,
        "observed_through": observed,
        "available_at": available,
        "gross_return": None if missing else gross_return,
        "cost_adjusted_return": _number(details.get("cost_adjusted_return")),
        "cost_model_version": details.get("cost_model_version"),
        "evidence_state": str(details.get("evidence_state") or (
            OutcomeEvidenceState.UNMEASURABLE.value if missing else OutcomeEvidenceState.DERIVED.value
        )).upper(),
        "status": status,
        "details": details,
    })


def _counterfactual_evidence(
    kind: str,
    value: Any,
    observed_through: datetime | None,
    available_at: datetime | None,
) -> OutcomeEvidence:
    number = _number(value)
    return OutcomeEvidence.model_validate({
        "evidence_id": f"{kind.lower()}-counterfactual",
        "kind": kind,
        "source_id": "derived-counterfactual",
        "observed_at": observed_through if number is not None else None,
        "observed_through": observed_through if number is not None else None,
        "available_at": available_at if number is not None else None,
        "gross_return": number,
        "cost_adjusted_return": number,
        "cost_model_version": "counterfactual-no-execution-cost-v1",
        "evidence_state": OutcomeEvidenceState.DERIVED.value if number is not None else OutcomeEvidenceState.MISSING.value,
        "status": "measured" if number is not None else "unmeasurable",
        "details": {"counterfactual": True},
    })


def _blocker_counts(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        reason = str(value).split(":", 1)[0]
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def _attribution_surface_row(row: dict[str, Any]) -> dict[str, Any]:
    counterfactuals = dict(row.get("counterfactuals") or {})
    expressions = dict(row.get("all_expression_counterfactuals") or {})
    metadata = dict(row.get("learning_metadata") or {})
    metadata["expression_returns"] = {
        str(kind): _number((value or {}).get("gross_return"))
        for kind, value in expressions.items() if isinstance(value, dict)
    }
    stock = dict(counterfactuals.get("STOCK") or {})
    cash = dict(counterfactuals.get("CASH") or {})
    trend = dict(counterfactuals.get("TREND") or {})
    metadata.update({
        "cost_adjusted_selected_return": _number(row.get("selected_net_return")),
        "cost_adjusted_stock_counterfactual_return": _number(stock.get("cost_adjusted_return")),
        "cost_adjusted_cash_return": _number(cash.get("cost_adjusted_return")),
        "trend_counterfactual_return": _number(trend.get("cost_adjusted_return")),
    })
    return {
        "outcome_attribution_id": row.get("outcome_attribution_id"),
        "trade_plan_id": row.get("trade_plan_id"),
        "ticker_decision_id": row.get("trade_plan_id"),
        "ticker": row.get("ticker"),
        "as_of": row.get("decision_cutoff"),
        "horizon": str(row.get("horizon") or "").lower(),
        "horizon_sessions": row.get("horizon_sessions"),
        "state": str(row.get("state") or "").lower(),
        "selected_return": row.get("selected_gross_return"),
        "stock_counterfactual_return": stock.get("gross_return"),
        "alternate_counterfactual_return": dict(counterfactuals.get("ALTERNATE_EXPRESSION") or {}).get("gross_return"),
        "cash_return": cash.get("gross_return"),
        "metadata": metadata,
        "scenarios": metadata.get("scenarios") or [],
        "error_type": valid_outcome_error_type(row.get("mistake_classification")),
        "mistake_card": row.get("mistake_card") or {},
    }


def _decision_from_row(row: Any) -> TickerDecision:
    resolution = resolution_from_legacy(dict(row))
    ticker = str(row["ticker"]).strip().upper()
    manifest = dict(row["input_manifest"] or {})
    portfolio_impacts = portfolio_impacts_from_persisted(
        row.get("portfolio_impacts") if hasattr(row, "get") else {},
        ticker=ticker,
    )
    trade_plan_value = trade_plan_from_persisted(manifest.get("trade_plan"), ticker=ticker)
    trade_plan = TradePlan.model_validate(trade_plan_value) if trade_plan_value is not None else None
    decision = TickerDecision.model_validate({
        "decision_contract_version": row["contract_version"],
        "ticker": ticker,
        "as_of": row["as_of"],
        "decision_revision": row["decision_revision"],
        "tactical": row["tactical"],
        "fundamental": row["fundamental"],
        "capital_action": capital_action_from_resolution(resolution),
        "resolution": resolution,
        "policy_version": (row.get("policy_version") if hasattr(row, "get") else row["policy_version"])
            or resolution.policy_version,
        "risk_policy": row["risk_policy"],
        "expressions": row["expressions"],
        "selected_expression": row["selected_expression"],
        "data_requests": row["data_requests"],
        "learning_history": row["learning_history"],
        "input_manifest": manifest,
        "market_state_publication_id": row.get("market_state_publication_id") if hasattr(row, "get") else None,
        "market_state_snapshot": row.get("market_state_snapshot") if hasattr(row, "get") else None,
        "portfolio_impacts": portfolio_impacts,
        "risk_policy_snapshot": row.get("risk_policy_snapshot") if hasattr(row, "get") else None,
        "opportunity_episode": (
            row.get("opportunity_episode") if hasattr(row, "get") else None
        ) or None,
        "instrument_state_snapshot": manifest.get("instrument_state_snapshot"),
        "alpha_signals": manifest.get("alpha_signals") or [],
        "opportunity_rank": manifest.get("opportunity_rank"),
        "trade_plan": None,
    })
    return bind_trade_plan(decision, trade_plan) if trade_plan is not None else decision


def _uuid_or_none(value: Any) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _first_statement(values: Any) -> str | None:
    if not isinstance(values, list) or not values:
        return None
    first = values[0]
    return str(first.get("statement") or "") if isinstance(first, dict) else str(first)


def _classify_mistake(
    *,
    stance: str,
    action: str,
    selected_kind: str,
    selected_return: float | None,
    stock_return: float,
    alternate_return: float | None,
) -> tuple[str | None, dict[str, Any]]:
    """Classify one deterministic episode without treating missing marks as loss."""

    if action == "AVOID" and stock_return > 0:
        # Opportunity cost is not one of the seven evidenced Phase 7 error
        # types. Keep it unavailable rather than leaking a legacy label into
        # the authoritative outcome taxonomy.
        return None, {}
    if stance == "BULLISH" and stock_return < 0 or stance == "BEARISH" and stock_return > 0:
        # A price move alone is not persisted evidence for a taxonomy label.
        return None, {}
    if selected_kind not in {"STOCK", "CASH"} and selected_return is None:
        # An untradeable expression does not prove any Phase 7 error class.
        return None, {}
    if selected_kind not in {"STOCK", "CASH"} and selected_return is not None and stock_return > selected_return + 0.05:
        # A counterfactual return difference alone is not persisted evidence.
        return None, {}
    return None, {}


def _stock_cost_adjusted_return(gross_return: float) -> float:
    """Apply a visible, conservative estimated round-trip stock cost."""

    one_side = STOCK_COST_PER_SIDE_BPS / 10_000.0
    return (1.0 + gross_return) * (1.0 - one_side) ** 2 - 1.0


def _delisting_status(lifecycle: Any, reference: datetime) -> str:
    if not lifecycle:
        return "lifecycle_missing"
    delisted_at = lifecycle.get("delisted_at")
    if delisted_at is None or _utc(delisted_at) > _utc(reference):
        return "active"
    return "delisted"


def _terminal_delisting_mark(
    connection: Any,
    *,
    instrument_id: int,
    lifecycle: Any,
    as_of: datetime,
    reference: datetime,
) -> dict[str, Any] | None:
    """Return a point-in-time terminal mark for an explicitly delisted ticker."""

    if not lifecycle or lifecycle.get("delisted_at") is None:
        return None
    delisted_at = _utc(lifecycle["delisted_at"])
    if delisted_at <= _utc(as_of) or delisted_at > _utc(reference):
        return None
    price = _number(lifecycle.get("delisting_price"))
    available_at = lifecycle.get("delisting_available_at")
    if price is not None and price > 0 and available_at is not None and _utc(available_at) <= _utc(reference):
        return {
            "close": price,
            "available_at": available_at,
            "observed_at": delisted_at,
            "trading_date": delisted_at.date(),
        }
    row = connection.execute(
        """
        SELECT close, available_at, observed_at, trading_date
        FROM raw.confirmed_price_bar
        WHERE instrument_id = %s AND interval = '1d'
          AND trading_date > %s::date
          AND trading_date <= %s::date
          AND available_at <= %s
        ORDER BY trading_date DESC, available_at DESC, observed_at DESC
        LIMIT 1
        """,
        [instrument_id, as_of, delisted_at, reference],
    ).fetchone()
    return dict(row) if row is not None else None


def _learning_metadata(
    *,
    as_of: datetime,
    measured_through: datetime,
    reference: datetime,
    sector: Any,
    regime: Any,
    delisting_status: str,
    decision: dict[str, Any],
) -> dict[str, Any]:
    start = _utc(as_of).date()
    end = _utc(measured_through).date()
    age_days = max(0, (_utc(reference).date() - start).days)
    sample = "canary" if age_days <= 30 else "forward" if age_days <= 120 else "historical"
    tactical = dict(decision.get("tactical") or {})
    fundamental = dict(decision.get("fundamental") or {})
    decision_regime = (
        tactical.get("market_regime")
        or fundamental.get("market_regime")
        or tactical.get("regime")
        or fundamental.get("regime")
    )
    regime_row = dict(regime or {})
    regime_slice = str(decision_regime or "").strip()
    if not regime_slice:
        trend_state = str(regime_row.get("trend_state") or "").strip().lower()
        volatility_state = str(regime_row.get("volatility_state") or "").strip().lower()
        if trend_state in {"trend_up", "trend_down", "range", "transition"} and volatility_state in {
            "low", "normal", "high", "unstable",
        }:
            regime_slice = f"{trend_state}:{volatility_state}"
    regime_slice = regime_slice or "unknown"
    sector_slice = str(sector or "").strip() or "unknown"
    return {
        "sample": sample,
        # The disjoint policy samples are partitioned by decision-origin date.
        # Keep the realized outcome interval separately; using it as the
        # split interval makes rolling online episodes overlap by construction.
        "sample_start": start.isoformat(),
        "sample_end": start.isoformat(),
        "sample_definition": "decision-origin-age-windows-v2",
        "outcome_start": min(start, end).isoformat(),
        "outcome_end": max(start, end).isoformat(),
        "delistings_handled": delisting_status in {"active", "delisted_terminal"},
        "delisting_status": delisting_status,
        "sector_slice": sector_slice,
        "regime_slice": regime_slice,
        "scenarios": fundamental.get("scenarios") or tactical.get("scenarios") or [],
        # Outcome refresh is an online, forward-only evaluator. No outcome is
        # used before its decision as_of, so the purge/embargo condition is
        # satisfied without claiming a backtest fit that did not occur.
        "purge_embargo_verified": _utc(measured_through) > _utc(as_of),
        "purge_embargo_policy": "online-forward-only-no-fit-v1",
        # One deterministic active policy is evaluated here. There is no
        # hidden model sweep whose best result would need correction.
        "multiple_trial_correction": "single-policy-no-trial-selection-v1",
        "point_in_time_defect": False,
        "outcome_selector": "confirmed_price_bar-available_at-v1",
    }


def decision_funnel_payload(
    decisions: list[dict[str, Any]],
    alpha_rows: list[dict[str, Any]],
    rank_rows: list[dict[str, Any]],
    plan_rows: list[dict[str, Any]],
    *,
    action_queue_rows: list[dict[str, Any]] | None = None,
    now: datetime,
) -> dict[str, Any]:
    symbols = sorted({
        str(row.get("ticker") or row.get("symbol") or "").upper()
        for rows in (decisions, alpha_rows, rank_rows, plan_rows)
        for row in rows
        if str(row.get("ticker") or row.get("symbol") or "").strip()
    })
    total = len(symbols)
    decision_by_symbol = {str(row.get("ticker") or "").upper(): row for row in decisions}
    alpha_by_symbol = {str(row.get("ticker") or "").upper(): row for row in alpha_rows}
    rank_by_symbol = {str(row.get("ticker") or "").upper(): row for row in rank_rows}
    plan_by_symbol = {str(row.get("ticker") or "").upper(): row for row in plan_rows}
    queue_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for row in action_queue_rows or ():
        symbol = str(row.get("ticker") or "").upper()
        if symbol and row.get("source") == "capital_action":
            queue_by_symbol.setdefault(symbol, []).append(row)

    def details(stage: str, symbol: str) -> tuple[bool, list[str]]:
        decision = decision_by_symbol.get(symbol, {})
        if stage == "point_in_time_facts":
            available = decision.get("point_in_time_facts_available") is True
            blockers = list(decision.get("point_in_time_fact_blockers") or ())
            return available, blockers or ([] if available else ["point_in_time_facts_unavailable"])
        if stage == "qualified_stock_alpha":
            row = alpha_by_symbol.get(symbol, {})
            available = row.get("availability_status") == "available"
            return available, list(row.get("blockers") or (["qualified_stock_alpha_missing"] if not available else []))
        if stage == "stock_expression":
            expressions = decision.get("expressions") if isinstance(decision.get("expressions"), Mapping) else {}
            row = expressions.get("STOCK") or expressions.get("stock") or {}
            available = isinstance(row, Mapping) and row.get("availability_status") == "available"
            blockers = list(row.get("blockers") or ()) if isinstance(row, Mapping) else []
            return available, blockers or ([] if available else ["stock_expression_unavailable"])
        if stage == "portfolio_impact":
            impacts = decision.get("portfolio_impacts") if isinstance(decision.get("portfolio_impacts"), Mapping) else {}
            row = impacts.get("STOCK") or impacts.get("stock") or {}
            available = isinstance(row, Mapping) and row.get("availability_status") == "available"
            blockers = list(row.get("blockers") or ()) if isinstance(row, Mapping) else []
            return available, blockers or ([] if available else ["stock_portfolio_impact_unavailable"])
        if stage == "trade_rank":
            row = rank_by_symbol.get(symbol, {})
            available = row.get("availability_status") == "available" and row.get("trade_rank") is not None
            blocker = row.get("primary_blocker") or row.get("trade_rank_unavailable_reason")
            return available, [] if available else [str(blocker or "trade_rank_unavailable")]
        if stage == "decision_resolution":
            row = decision.get("resolution") if isinstance(decision.get("resolution"), Mapping) else {}
            available = row.get("eligibility") == "ACTIONABLE" and row.get("action") not in {"NO_TRADE", "AVOID"}
            return available, list(row.get("blockers") or ([] if available else ["decision_resolution_blocked"]))
        if stage == "action_queue":
            rows = queue_by_symbol.get(symbol, [])
            available = any(
                row.get("lifecycle_state") == "actionable"
                and row.get("selected_expression") == "STOCK"
                and isinstance(row.get("trade_plan"), Mapping)
                for row in rows
            )
            blockers = [
                str(row.get("primary_blocker"))
                for row in rows
                if row.get("primary_blocker")
            ]
            return available, blockers or ([] if available else ["action_queue_unavailable"])
        row = plan_by_symbol.get(symbol, {})
        available = row.get("availability_status") == "available" and row.get("eligibility") == "ACTIONABLE"
        blockers = list(row.get("blockers") or ([] if available else ["trade_plan_unavailable"]))
        return available, blockers

    owners = {
        "point_in_time_facts": ("ticker-decisions", "Refresh source facts and MarketState."),
        "qualified_stock_alpha": ("strategy-governance", "Publish a passed OOS ticker-stock-alpha revision."),
        "stock_expression": ("ticker-decisions", "Refresh the ticker decision inputs."),
        "portfolio_impact": ("portfolio-impact", "Refresh the point-in-time portfolio book."),
        "trade_rank": ("ticker-ranking", "Publish the ticker opportunity ranking."),
        "decision_resolution": ("decision-policy", "Resolve the selected expression against CASH."),
        "trade_plan": ("ticker-decisions", "Publish a complete paper TradePlan."),
        "action_queue": ("today-action-queue", "Refresh ticker decisions and /api/today."),
    }
    stages = []
    for stage in owners:
        passed: list[str] = []
        failed: list[tuple[str, str]] = []
        for symbol in symbols:
            available, blockers = details(stage, symbol)
            if available:
                passed.append(symbol)
            else:
                failed.extend((str(blocker), symbol) for blocker in blockers or [f"{stage}_unavailable"])
        counts = Counter(reason for reason, _symbol in failed)
        top_blockers = [
            {
                "reason": reason,
                "count": count,
                "affected_symbols": sorted({symbol for item, symbol in failed if item == reason})[:20],
            }
            for reason, count in counts.most_common(5)
        ]
        owner, retry = owners[stage]
        stages.append({
            "stage": stage,
            "count": len(passed),
            "total": total,
            "percentage": len(passed) / total if total else 0.0,
            "unavailable_count": total - len(passed),
            "affected_symbols": sorted({symbol for _reason, symbol in failed})[:20],
            "top_blockers": top_blockers,
            "owner": owner,
            "retry": retry,
        })
    published_values = [
        parsed
        for rows in (alpha_rows, rank_rows, plan_rows, decisions)
        for row in rows
        if (parsed := _parse_datetime(row.get("publication_published_at") or row.get("published_at"))) is not None
    ]
    published_at = max(published_values, default=None)
    policy_version = next(
        (str(row.get("ranking_version")) for row in rank_rows if row.get("ranking_version")),
        "ticker-opportunity-ranking.v1",
    )
    return {
        "policy_version": policy_version,
        "generated_at": now,
        "published_at": published_at,
        "age_seconds": max(0.0, (now - published_at).total_seconds()) if published_at else None,
        "total": total,
        "actionable": stages[-1]["count"] if stages else 0,
        "stages": stages,
    }


def _market_snapshot_from_exact_publication(
    publication: Mapping[str, Any] | None,
    *,
    publication_id: str,
    decision_cutoff: datetime,
) -> MarketStateSnapshot | None:
    """Validate one exact MarketState publication for a decision cutoff."""

    if (
        publication is None
        or str(publication.get("publication_id") or "") != publication_id
        or publication.get("publication_scope") != "market"
        or publication.get("publication_status") not in {"published", "superseded"}
    ):
        return None
    input_cutoff = _parse_datetime(publication.get("input_cutoff"))
    published_at = _parse_datetime(publication.get("published_at"))
    reference = _utc(decision_cutoff)
    if (
        input_cutoff is None
        or published_at is None
        or published_at <= input_cutoff
        or input_cutoff > reference
        or published_at > reference
    ):
        return None
    models = publication.get("models")
    rows = models.get("market_state_snapshot") if isinstance(models, Mapping) else None
    if not isinstance(rows, (list, tuple)) or len(rows) != 1:
        return None
    try:
        snapshot = MarketStateSnapshot.model_validate(rows[0])
    except (TypeError, ValueError):
        return None
    if (
        snapshot.publication_id not in {None, "", publication_id}
        or _utc(snapshot.input_cutoff) != input_cutoff
        or _utc(snapshot.as_of) != input_cutoff
    ):
        return None
    raw_source_lineage = publication.get("source_lineage") or ()
    if not isinstance(raw_source_lineage, (list, tuple)):
        return None
    try:
        source_lineage = tuple(InputLineage.model_validate(item) for item in raw_source_lineage)
    except (TypeError, ValueError):
        return None
    lineages = list(snapshot.input_lineage)
    for dimensions in snapshot.horizons.values():
        for dimension in dimensions:
            lineages.extend(dimension.lineage)
    if snapshot.coverage_matrix is not None:
        for row in snapshot.coverage_matrix.rows:
            lineages.extend(row.input_lineage)
    for lineage in (*source_lineage, *lineages):
        if (
            _parse_datetime(lineage.available_at) is None
            or _parse_datetime(lineage.available_at) > input_cutoff
            or _parse_datetime(lineage.cutoff) != input_cutoff
        ):
            return None
    return snapshot.model_copy(update={"publication_id": publication_id})


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    if not value:
        return None
    try:
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None


def _funnel_action_queue_row(
    *,
    row: Mapping[str, Any],
    ticker: str,
    opportunity_episode: OpportunityEpisode | None,
    selected_kind: ExpressionKind | None,
    stock_expression: TradeExpression | None,
    stock_impact: PortfolioImpact | None,
    resolution: DecisionResolutionV2 | None,
    rank_rows: list[dict[str, Any]],
    plan_rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Derive the action-queue stage from the same compact current authority."""

    if (
        opportunity_episode is None
        or selected_kind is not ExpressionKind.STOCK
        or stock_expression is None
        or stock_expression.availability_status is not AvailabilityStatus.AVAILABLE
        or stock_impact is None
        or stock_impact.availability_status is not AvailabilityStatus.AVAILABLE
        or resolution is None
        or resolution.eligibility.value != "ACTIONABLE"
        or resolution.action.value in {"NO_TRADE", "AVOID"}
    ):
        return None
    revision = str(row.get("decision_revision") or "")
    episode_id = str(row.get("opportunity_episode_id") or "")
    policy_version = str(row.get("policy_version") or "")
    expression_identity = trade_expression_identity(stock_expression)
    rank_matches = [
        candidate for candidate in rank_rows
        if str(candidate.get("ticker") or candidate.get("symbol") or "").strip().upper() == ticker
        and str(candidate.get("decision_revision") or "") == revision
        and str(candidate.get("opportunity_episode_id") or "") == episode_id
    ]
    plan_matches = [
        candidate for candidate in plan_rows
        if str(candidate.get("ticker") or candidate.get("symbol") or "").strip().upper() == ticker
        and str(candidate.get("decision_revision") or "") == revision
        and str(candidate.get("opportunity_episode_id") or "") == episode_id
    ]
    if len(rank_matches) != 1 or len(plan_matches) != 1:
        return None
    rank = rank_matches[0]
    plan = plan_matches[0]
    publication_id = str(rank.get("publication_id") or "")
    plan_id = str(plan.get("trade_plan_id") or "")
    if (
        not publication_id
        or str(plan.get("publication_id") or "") != publication_id
        or str(rank.get("policy_version") or "") != policy_version
        or str(plan.get("policy_version") or "") != policy_version
        or str(rank.get("selected_expression_kind") or "") != "STOCK"
        or str(plan.get("selected_expression_kind") or "") != "STOCK"
        or str(rank.get("selected_expression_identity") or "") != expression_identity
        or str(plan.get("selected_expression_identity") or "") != expression_identity
        or str(rank.get("portfolio_impact_id") or "") != stock_impact.impact_id
        or str(plan.get("portfolio_impact_id") or "") != stock_impact.impact_id
        or str(rank.get("market_state_publication_id") or "")
            != str(stock_impact.market_state_publication_id or "")
        or str(plan.get("market_state_publication_id") or "")
            != str(stock_impact.market_state_publication_id or "")
        or str(resolution.trade_plan_id or "") != plan_id
        or not plan_id
        or str(plan.get("availability_status") or "").lower() != "available"
        or str(plan.get("eligibility") or "").upper() != "ACTIONABLE"
        or str(rank.get("availability_status") or "").lower() != "available"
        or not _funnel_bool(rank.get("evaluated_universe_complete"))
    ):
        return None
    try:
        trade_rank = int(rank.get("trade_rank"))
        trade_utility = float(rank.get("trade_utility"))
    except (TypeError, ValueError):
        return None
    if trade_rank <= 0 or not isfinite(trade_utility) or trade_utility <= 0:
        return None
    return {
        "ticker": ticker,
        "source": "capital_action",
        "lifecycle_state": "actionable",
        "selected_expression": "STOCK",
        "trade_plan": {"trade_plan_id": plan_id},
    }


def _funnel_bool(value: Any) -> bool:
    return value is True or str(value or "").strip().lower() == "true"


def _funnel_lineage_timestamp(value: Any) -> datetime | None:
    if isinstance(value, (datetime, str)):
        return _cached_funnel_lineage_timestamp(value)
    return _parse_funnel_lineage_timestamp(value)


@lru_cache(maxsize=4096)
def _cached_funnel_lineage_timestamp(value: datetime | str) -> datetime | None:
    return _parse_funnel_lineage_timestamp(value)


def _parse_funnel_lineage_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value) if value.tzinfo is not None else None
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return _utc(parsed) if parsed.tzinfo is not None else None


def _funnel_lineage_is_valid(
    value: Any,
    *,
    episode_id: str,
    decision_revision: str,
    policy_version: str,
    cutoff: datetime,
) -> bool:
    if not isinstance(value, (list, tuple)) or not value:
        return False
    identities: set[tuple[Any, ...]] = set()
    for item in value:
        if not isinstance(item, dict):
            return False
        field = item.get("field")
        source_id = item.get("source_id", item.get("source"))
        source_version = item.get("source_version", item.get("version"))
        if not isinstance(field, str) or not field:
            return False
        if not isinstance(source_id, str) or not source_id:
            return False
        if source_version is not None and not isinstance(source_version, str):
            return False
        for name in ("revision", "opportunity_episode_id", "decision_revision", "policy_version"):
            if item.get(name) is not None and not isinstance(item[name], str):
                return False
        timestamps: dict[str, datetime | None] = {}
        for name in ("event_at", "published_at", "available_at", "received_at", "cutoff"):
            if name in item and item[name] is not None:
                timestamp = _funnel_lineage_timestamp(item[name])
                if timestamp is None:
                    return False
                timestamps[name] = timestamp
        available_at = timestamps.get("available_at")
        if available_at is None or available_at > cutoff:
            return False
        if item.get("opportunity_episode_id", item.get("episode_id")) not in (None, episode_id):
            return False
        if item.get("decision_revision") not in (None, decision_revision):
            return False
        if item.get("policy_version") not in (None, policy_version):
            return False
        lineage_cutoff = timestamps.get("cutoff")
        if item.get("cutoff") is not None and lineage_cutoff != cutoff:
            return False
        identity = (
            field,
            source_id,
            source_version,
            available_at,
            item.get("revision"),
            lineage_cutoff,
        )
        if identity in identities:
            return False
        identities.add(identity)
    return True


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number.is_integer() else None


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


__all__ = [
    "HORIZON_SESSIONS", "TickerDecisionRepository", "decision_funnel_payload",
    "paper_execution_for_plan", "plan_authority",
    "select_current_outcome_attributions",
]
