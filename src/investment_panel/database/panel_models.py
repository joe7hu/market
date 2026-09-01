"""PostgreSQL panel model catalog and retrieval policies."""
from __future__ import annotations

from datetime import UTC, datetime
import inspect
from typing import Any, Iterable, Mapping

from investment_panel.core.config import AppConfig
from investment_panel.database.panel_queries import OWNED_CORRELATIONS_QUERY, build_query_policies
from investment_panel.database.panel_source_queries import SOURCE_QUERIES, SOURCE_UNIVERSE_QUERIES
from investment_panel.database.event_panel_models import EVENT_DIRECT_QUERIES
from investment_panel.database.portfolio_intelligence import portfolio_intelligence_tables
from investment_panel.database.thesis import thesis_monitor_rows, thesis_rows
from investment_panel.database.user_state import portfolio_rows, watchlist_rows
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.jobs import JobRepository
from investment_panel.database.brokers import broker_status_rows
from investment_panel.database.agents import AgentRepository
from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.migrations import HEAD_REVISION
from investment_panel.database.panel_watchlist import TECHNICALS_QUERY, options_ticker_signal_rows, technical_rows
from investment_panel.database.options_recovery_read import RecoveryReadRepository
from investment_panel.database.panel_publications import published_tables
from investment_panel.database.current_quotes import current_quote_rows
from investment_panel.database.superinvestor_portfolios import superinvestor_portfolios
from investment_panel.database.runtime import API_PROFILE, RuntimeProfile

__all__ = ["load_postgres_tables", "today_authority_pages"]
RECOVERY_MODELS = frozenset({
    "option_recovery_funnel", "option_recovery_event", "option_recovery_opportunity",
    "option_recovery_family_performance", "option_recovery_agent_provenance", "option_recovery_health",
})
RESEARCH_PACKETS_BASE_QUERY = """
    SELECT instrument.symbol, item.id::text AS packet_id, item.observed_at AS generated_at,
           item.published_at,
           item.title, item.summary, item.url AS source_url, item.source_id AS source,
           item.metadata, ingest_run.finished_at AS available_at,
           ingest_run.id::text AS source_version, item.id::text AS revision
    FROM raw.content_item_instrument link
    JOIN raw.content_item item ON item.id = link.content_item_id
    JOIN catalog.instrument instrument ON instrument.id = link.instrument_id
    JOIN ingest.source source ON source.id = item.source_id
       AND source.enabled AND source.operational_state = 'active'
    JOIN ingest.run ingest_run ON ingest_run.id = item.ingest_run_id
       AND ingest_run.finished_at IS NOT NULL
    ORDER BY item.observed_at DESC
"""

# Keep the seam stable for test callers while implementation ownership stays in
# the focused publication module.
_published_tables = published_tables

TODAY_RESEARCH_RANK_VALID_SQL = """
    opportunity_rank->>'research_rank' ~ '^[1-9][0-9]*$'
    AND length(opportunity_rank->>'research_rank') <= 9
    AND pg_input_is_valid(opportunity_rank->>'research_rank', 'integer')
"""


TODAY_ACTION_ORDER_SQL = f"""
    CASE
      WHEN opportunity_rank->>'trade_rank' ~ '^[1-9][0-9]*$'
       AND length(opportunity_rank->>'trade_rank') <= 9
       AND pg_input_is_valid(opportunity_rank->>'trade_rank', 'integer')
      THEN (opportunity_rank->>'trade_rank')::integer
    END ASC NULLS LAST,
    CASE
      WHEN {TODAY_RESEARCH_RANK_VALID_SQL}
      THEN (opportunity_rank->>'research_rank')::integer
    END ASC NULLS LAST,
    ticker,
    as_of DESC,
    published_at DESC,
    ticker_decision_id DESC
"""


DIRECT_QUERIES: dict[str, str] = {
    "options_radar_health": """
        SELECT publication.id::text AS publication_id,
               publication.published_at,
               EXTRACT(EPOCH FROM (now() - publication.published_at)) / 60 AS publication_age_minutes,
               active.strategy_key AS champion_strategy,
               active.revision AS champion_revision,
               challenger.strategy_key AS challenger_strategy,
               challenger.status AS challenger_status,
               COALESCE(outcomes.resolved_outcomes, 0) AS resolved_outcomes,
               COALESCE(outcomes.outcome_coverage, 0) AS outcome_coverage,
               COALESCE(canary.canary_sample, 0) AS canary_sample,
               publication.validation->>'rollback_reason' AS rollback_reason
        FROM (SELECT 1) anchor
        LEFT JOIN LATERAL (
            SELECT * FROM app.publication
            WHERE scope = 'options-radar' AND status = 'published'
            ORDER BY published_at DESC LIMIT 1
        ) publication ON true
        LEFT JOIN LATERAL (
            SELECT strategy_key, revision FROM analysis.strategy_revision
            WHERE authority_group = 'options-radar-core' AND status = 'active' LIMIT 1
        ) active ON true
        LEFT JOIN LATERAL (
            SELECT strategy_key, status FROM analysis.strategy_revision
            WHERE authority_group = 'options-radar-core'
              AND status IN ('candidate', 'testing', 'approved')
            ORDER BY created_at DESC LIMIT 1
        ) challenger ON true
        LEFT JOIN LATERAL (
            SELECT count(*) FILTER (WHERE outcome.maturity_state IN ('mature', 'expired')) AS resolved_outcomes,
                   count(outcome.decision_id)::double precision / NULLIF(count(decision.id), 0) AS outcome_coverage
            FROM analysis.decision decision
            LEFT JOIN analysis.option_outcome outcome ON outcome.decision_id = decision.id
            WHERE decision.kind = 'option'
        ) outcomes ON true
        LEFT JOIN LATERAL (
            SELECT max((metrics->'proposed'->>'sample_size')::int) AS canary_sample
            FROM analysis.strategy_evaluation WHERE evaluation_type = 'canary'
        ) canary ON true
    """,
    **SOURCE_UNIVERSE_QUERIES,
    "technicals": TECHNICALS_QUERY,
    "valuations": """
        SELECT instrument.symbol, observation.metric_set, observation.period_start,
               observation.period_end,
               observation.filed_at, observation.observed_at, observation.values,
               observation.source_id AS source, ingest_run.finished_at AS available_at,
               ingest_run.id::text AS source_version, observation.id::text AS revision
        FROM raw.fundamental_observation observation
        JOIN catalog.instrument instrument ON instrument.id = observation.instrument_id
        JOIN ingest.source source ON source.id = observation.source_id
          AND source.enabled AND source.operational_state = 'active'
        JOIN ingest.run ingest_run ON ingest_run.id = observation.ingest_run_id
          AND ingest_run.finished_at IS NOT NULL
        ORDER BY instrument.symbol, observation.observed_at DESC, ingest_run.finished_at DESC
    """,
    "liquidity": """
        SELECT symbol,
               max(quote.observed_at) AS as_of,
               max(ingest_run.finished_at) AS available_at,
               avg((quote.ask - quote.bid) / NULLIF(quote.mid, 0)) AS average_option_spread_pct,
               sum(COALESCE(quote.open_interest, 0)) AS total_open_interest,
               sum(COALESCE(quote.volume, 0)) AS total_option_volume,
               count(*) AS contracts
        FROM raw.option_quote quote
        JOIN raw.option_snapshot snapshot ON snapshot.id = quote.snapshot_id
        JOIN ingest.run ingest_run ON ingest_run.id = snapshot.ingest_run_id
          AND ingest_run.finished_at IS NOT NULL
        JOIN catalog.option_contract contract ON contract.id = quote.contract_id
        JOIN catalog.instrument instrument ON instrument.id = contract.underlying_instrument_id
        JOIN LATERAL (
            SELECT max(snapshot.observed_at) AS observed_at FROM raw.option_snapshot snapshot
            JOIN raw.option_quote latest_quote ON latest_quote.snapshot_id = snapshot.id
            JOIN catalog.option_contract latest_contract ON latest_contract.id = latest_quote.contract_id
            WHERE latest_contract.underlying_instrument_id = instrument.id
        ) latest ON latest.observed_at = quote.observed_at
        GROUP BY instrument.symbol ORDER BY instrument.symbol
    """,
    "earnings": """
        SELECT event.id::text, instrument.symbol, event.starts_at, event.title AS event,
               event.importance, event.verification_status, event.source_url, event.details,
               ingest_run.finished_at AS available_at,
               ingest_run.id::text AS source_version, event.id::text AS revision
        FROM raw.market_event event
        LEFT JOIN catalog.instrument instrument ON instrument.id = event.instrument_id
        JOIN ingest.source source ON source.id = event.source_id
          AND source.enabled AND source.operational_state = 'active'
        JOIN ingest.run ingest_run ON ingest_run.id = event.ingest_run_id
          AND ingest_run.finished_at IS NOT NULL
        WHERE event.event_kind = 'earnings'
          AND ingest_run.finished_at IS NOT NULL
        ORDER BY event.starts_at, ingest_run.finished_at
    """,
    "analyst_estimates": """
        SELECT instrument.symbol, observation.period_start, observation.period_end,
               observation.observed_at,
               observation.values, observation.source_id AS source,
               ingest_run.finished_at AS available_at,
               ingest_run.id::text AS source_version, observation.id::text AS revision
        FROM raw.fundamental_observation observation
        JOIN catalog.instrument instrument ON instrument.id = observation.instrument_id
        JOIN ingest.source source ON source.id = observation.source_id
          AND source.enabled AND source.operational_state = 'active'
        JOIN ingest.run ingest_run ON ingest_run.id = observation.ingest_run_id
          AND ingest_run.finished_at IS NOT NULL
        WHERE observation.metric_set IN ('analyst_estimates', 'consensus')
        ORDER BY observation.observed_at DESC, ingest_run.finished_at DESC
    """,
    "research_packets": RESEARCH_PACKETS_BASE_QUERY + " LIMIT 500",
    "source_freshness": """
        SELECT source.id AS source_id, source.name AS source_name,
               source.family AS source_family, source.kind AS source_kind,
               source.operational_state, source.health_owner, source.freshness_seconds,
               run.status, run.finished_at AS refreshed_at, run.finished_at AS available_at,
               run.id::text AS source_version, source.id AS revision, run.failure_detail,
               run.item_count, run.instrument_count AS ticker_count,
               CASE WHEN NOT source.enabled THEN 'disabled'
                    WHEN source.operational_state = 'archived' THEN 'archived'
                    WHEN source.operational_state = 'standby' THEN 'standby'
                    WHEN source.freshness_seconds IS NULL THEN 'uncontracted'
                    WHEN run.finished_at IS NULL THEN 'missing'
                    WHEN run.finished_at < now() - make_interval(secs => source.freshness_seconds) THEN 'stale'
                    ELSE 'fresh' END AS freshness_status
        FROM ingest.source source
        LEFT JOIN LATERAL (
            SELECT id, status, finished_at, failure_detail, item_count, instrument_count
            FROM ingest.run WHERE source_id = source.id ORDER BY started_at DESC LIMIT 1
        ) run ON true ORDER BY source.family, source.id
    """,
    "ownership_consensus": """
        SELECT disclosure.trader_name, disclosure.filer_name, disclosure.event_date,
               disclosure.filed_date, holding->>'symbol' AS symbol,
               holding->>'name' AS issuer,
               CASE WHEN holding ? 'value_usd' THEN (holding->>'value_usd')::bigint
                    WHEN disclosure.event_date >= DATE '2023-01-03' THEN (holding->>'value_thousands')::bigint
                    ELSE (holding->>'value_thousands')::bigint * 1000 END AS value_usd,
               disclosure.source_url, disclosure.details->>'accession_number' AS accession_number,
               ingest_run.finished_at AS available_at,
               ingest_run.id::text AS source_version, disclosure.id::text AS revision
        FROM raw.disclosure disclosure
        CROSS JOIN LATERAL jsonb_array_elements(COALESCE(disclosure.details->'holdings', '[]'::jsonb)) holding
        JOIN ingest.run ingest_run ON ingest_run.id = disclosure.ingest_run_id
          AND ingest_run.finished_at IS NOT NULL
        WHERE disclosure.source_type = '13f' AND holding->>'symbol' IS NOT NULL
        ORDER BY disclosure.event_date DESC, value_usd DESC, ingest_run.finished_at DESC
    """,
    "options_provider_capabilities": """
        SELECT id AS provider, name, enabled, capabilities, updated_at
        FROM ingest.source WHERE capabilities ? 'option_quotes' ORDER BY id
    """,
    "options_ticker_signals": """
        SELECT instrument.symbol AS ticker, instrument.symbol, decision.state,
               count(*) AS contract_count, max(decision.score) AS best_score,
               max(decision.as_of) AS as_of
        FROM analysis.decision decision
        JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
        WHERE decision.kind = 'option' GROUP BY instrument.symbol, decision.state
        ORDER BY best_score DESC
    """,
    "options_payoff_scenarios": """
        SELECT decision.id::text AS candidate_event_id, instrument.symbol AS ticker,
               contract.id::text AS contract_id, contract.expiration, contract.strike, contract.option_type,
               option_quote.bid, option_quote.ask, option_quote.bid_size, option_quote.ask_size,
               option_quote.observed_at AS quote_observed_at,
               option_decision.premium_mid, option_decision.entry_price,
               option_decision.buy_under, option_decision.structure,
               option_decision.synthetic_legs AS legs, option_decision.max_loss,
               option_decision.expected_value, option_decision.probability_profit,
               option_decision.details,
               feature.required_2x_price, feature.required_5x_price,
               feature.required_10x_price, feature.required_move_pct,
               COALESCE(option_quote.available_at, decision.as_of) AS available_at,
               option_quote.id::text AS source_version, option_quote.id::text AS revision
        FROM analysis.option_decision option_decision
        JOIN analysis.decision decision ON decision.id = option_decision.decision_id
        JOIN analysis.option_feature feature
          ON feature.run_id = decision.run_id AND feature.contract_id = option_decision.contract_id
        JOIN catalog.option_contract contract ON contract.id = option_decision.contract_id
        JOIN raw.option_quote option_quote
          ON option_quote.snapshot_id = option_decision.snapshot_id
         AND option_quote.contract_id = option_decision.contract_id
         AND option_quote.observed_at = option_decision.quote_observed_at
        JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
        ORDER BY decision.as_of DESC, decision.rank
    """,
    "ticker_decisions": """
        WITH current_candidates AS (
            SELECT decision.id::text AS ticker_decision_id,
                   instrument.symbol AS ticker, instrument.symbol,
                   decision.decision_revision, decision.contract_version,
                   decision.as_of, decision.published_at, decision.published_at AS available_at,
                   decision.input_hash,
                   decision.code_version, decision.experiment_id,
                   decision.tactical, decision.fundamental, decision.capital_action,
                   decision.resolution, decision.policy_version,
                   decision.opportunity_episode_id, decision.opportunity_cutoff,
                   decision.opportunity_episode, decision.risk_policy, decision.expressions,
                   decision.selected_expression, decision.data_requests,
                   decision.learning_history, decision.input_manifest,
                   decision.market_state_publication_id::text,
                   decision.market_state_snapshot, decision.portfolio_impacts,
                   decision.risk_policy_snapshot,
                   decision.status, decision.created_at,
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
              AND decision.as_of <= now()
              AND decision.published_at IS NOT NULL
              AND decision.published_at <= now()
              AND jsonb_typeof(decision.tactical) = 'object'
              AND jsonb_typeof(decision.fundamental) = 'object'
              AND jsonb_typeof(decision.capital_action) = 'object'
              AND jsonb_typeof(decision.risk_policy) = 'object'
              AND jsonb_typeof(decision.expressions) = 'object'
              AND jsonb_typeof(decision.input_manifest) = 'object'
        )
        SELECT ticker_decision_id, ticker, symbol,
               decision_revision, contract_version,
               as_of, published_at, available_at,
               input_hash, code_version, experiment_id,
               tactical, fundamental, capital_action,
               resolution, policy_version,
               opportunity_episode_id, opportunity_cutoff,
               opportunity_episode, risk_policy, expressions,
               selected_expression, data_requests,
               learning_history, input_manifest,
               market_state_publication_id,
               market_state_snapshot, portfolio_impacts,
               risk_policy_snapshot, status
        FROM current_candidates
        WHERE current_row = 1
          AND authority_count = 1
          AND opportunity_authority_count = 1
        ORDER BY as_of DESC, published_at DESC, created_at DESC, ticker_decision_id DESC
    """,
    "today_ticker_actions": f"""
        WITH current_candidates AS (
            SELECT decision.id::text AS ticker_decision_id,
                   instrument.symbol AS ticker, instrument.symbol,
                   decision.decision_revision, decision.as_of,
                   decision.published_at, decision.published_at AS available_at,
                   decision.input_hash,
                   CASE WHEN octet_length(decision.capital_action::text) <= 4096
                        THEN decision.capital_action END AS capital_action,
                   CASE WHEN octet_length(decision.resolution::text) <= 196608
                        THEN decision.resolution END AS resolution,
                   decision.policy_version, decision.opportunity_episode_id,
                   CASE WHEN octet_length(decision.selected_expression::text) <= 8192
                        THEN decision.selected_expression END AS selected_expression,
                   CASE WHEN octet_length((decision.input_manifest->'opportunity_rank')::text) <= 196608
                        THEN decision.input_manifest->'opportunity_rank' END AS opportunity_rank,
                   CASE WHEN octet_length((decision.input_manifest->'trade_plan')::text) <= 327680
                        THEN decision.input_manifest->'trade_plan' END AS trade_plan,
                   decision.created_at,
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
              AND decision.as_of <= now()
              AND decision.published_at IS NOT NULL
              AND decision.published_at <= now()
              AND jsonb_typeof(decision.capital_action) = 'object'
              AND jsonb_typeof(decision.input_manifest) = 'object'
        ),
        current_authority AS (
            SELECT *
            FROM current_candidates
            WHERE current_row = 1
              AND authority_count = 1
              AND opportunity_authority_count = 1
        )
        SELECT ticker_decision_id, ticker, symbol, decision_revision, as_of,
               published_at, available_at, input_hash, capital_action, resolution,
               policy_version, opportunity_episode_id, selected_expression,
               opportunity_rank, trade_plan,
               count(*) FILTER (
                   WHERE jsonb_typeof(opportunity_rank) = 'object'
               ) OVER () AS opportunity_rank_count,
               count(*) FILTER (
                   WHERE jsonb_typeof(trade_plan) = 'object'
               ) OVER () AS trade_plan_count,
               count(*) FILTER (
                   WHERE jsonb_typeof(trade_plan) IS DISTINCT FROM 'object'
                     AND (
                         jsonb_typeof(opportunity_rank) IS DISTINCT FROM 'object'
                         OR (
                             {TODAY_RESEARCH_RANK_VALID_SQL}
                         ) IS NOT TRUE
                     )
                     AND COALESCE(capital_action->>'owned', 'false') <> 'true'
               ) OVER () AS missing_plan_count
        FROM current_authority
        ORDER BY {TODAY_ACTION_ORDER_SQL}
    """,
    "ticker_outcomes": """
        SELECT outcome.ticker_decision_id::text, instrument.symbol AS ticker,
               instrument.symbol,
               outcome.horizon, outcome.horizon_sessions, outcome.state,
               outcome.measured_through, outcome.selected_expression,
               outcome.selected_return, outcome.stock_counterfactual_return,
               outcome.alternate_counterfactual_return, outcome.cash_return,
               outcome.sector_return, outcome.market_return, outcome.error_type,
               outcome.mistake_card, outcome.available_at, outcome.metadata,
               decision.fundamental->'scenarios' AS scenarios,
               outcome.updated_at
        FROM analysis.ticker_outcome outcome
        JOIN analysis.ticker_decision decision ON decision.id = outcome.ticker_decision_id
        JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
        ORDER BY outcome.measured_through DESC NULLS LAST, outcome.updated_at DESC
    """,
    "ticker_policy_learning": """
        WITH ranked_outcomes AS (
            SELECT outcome.*,
                   row_number() OVER (
                       PARTITION BY outcome.ticker_decision_id, outcome.horizon
                       ORDER BY outcome.horizon_sessions DESC, outcome.updated_at DESC, outcome.id DESC
                   ) AS horizon_rank
            FROM analysis.ticker_outcome outcome
            WHERE outcome.state = 'resolved'
        ), bounded_episodes AS (
            SELECT outcome.*, decision.id AS decision_id, decision.as_of,
                   instrument.symbol AS ticker, decision.fundamental->'scenarios' AS scenarios
            FROM ranked_outcomes outcome
            JOIN analysis.ticker_decision decision ON decision.id = outcome.ticker_decision_id
            JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
            WHERE outcome.horizon_rank = 1
            ORDER BY decision.as_of DESC, decision.id DESC, outcome.horizon
            LIMIT 10000
        )
        SELECT COALESCE(
            jsonb_agg(
                jsonb_build_object(
                    'ticker_decision_id', decision_id::text,
                    'ticker', ticker,
                    'as_of', as_of,
                    'horizon', outcome.horizon,
                    'horizon_sessions', outcome.horizon_sessions,
                    'state', outcome.state,
                    'selected_return', outcome.selected_return,
                    'stock_counterfactual_return', outcome.stock_counterfactual_return,
                    'metadata', outcome.metadata,
                    'scenarios', scenarios
                )
                ORDER BY as_of, decision_id, outcome.horizon
            ), '[]'::jsonb
        ) AS episodes
        FROM bounded_episodes outcome
    """,
    "ticker_benchmark_snapshot": """
        SELECT benchmark_key, as_of, available_at, membership_hash,
               member_count, source_id, source_version, exact_membership,
               coverage
        FROM analysis.ticker_benchmark_snapshot
        ORDER BY as_of DESC
    """,
    "shadow_trade": """
        SELECT trade.id::text, trade.decision_id::text AS candidate_event_id,
               instrument.symbol AS ticker, trade.entry_at, trade.entry_price,
               trade.exit_at, trade.exit_price, trade.status, trade.metrics
        FROM analysis.shadow_trade trade
        JOIN analysis.decision decision ON decision.id = trade.decision_id
        JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
        ORDER BY trade.entry_at DESC
    """,
    "strategy_backtest_result": """
        SELECT evaluation.id::text, strategy.strategy_key AS strategy_version,
               evaluation.evaluated_at, evaluation.period_start, evaluation.period_end,
               evaluation.verdict, evaluation.metrics, evaluation.evidence AS raw
        FROM analysis.strategy_evaluation evaluation
        JOIN analysis.strategy_revision strategy ON strategy.id = evaluation.strategy_revision_id
        WHERE evaluation.evaluation_type = 'backtest'
        ORDER BY evaluation.evaluated_at DESC, evaluation.id DESC
    """,
    "strategy_forward_test_result": """
        SELECT evaluation.id::text, strategy.strategy_key AS strategy_version,
               evaluation.evaluated_at, evaluation.period_start, evaluation.period_end,
               evaluation.verdict, evaluation.metrics, evaluation.evidence AS raw
        FROM analysis.strategy_evaluation evaluation
        JOIN analysis.strategy_revision strategy ON strategy.id = evaluation.strategy_revision_id
        WHERE evaluation.evaluation_type IN ('forward_test', 'forward_shadow_test', 'shadow')
        ORDER BY evaluation.evaluated_at DESC, evaluation.id DESC
    """,
    "quotes": "SELECT NULL::text AS symbol WHERE false",
    "options_chain": """
        WITH latest_symbol_snapshot AS (
            SELECT DISTINCT ON (instrument.id)
                   instrument.id AS instrument_id, snapshot.id AS snapshot_id
            FROM raw.option_snapshot snapshot
            JOIN raw.option_quote quote ON quote.snapshot_id = snapshot.id
            JOIN catalog.option_contract contract ON contract.id = quote.contract_id
            JOIN catalog.instrument instrument ON instrument.id = contract.underlying_instrument_id
            ORDER BY instrument.id, snapshot.observed_at DESC,
                     CASE snapshot.source_id WHEN 'robinhood' THEN 0 WHEN 'ibkr' THEN 1 ELSE 2 END,
                     snapshot.id DESC
        )
        SELECT instrument.symbol, contract.expiration AS expiry, contract.strike,
               contract.option_type, quote.bid, quote.ask, quote.mid, quote.last,
               quote.volume, quote.open_interest, quote.provider_iv AS iv,
               quote.provider_delta AS delta, quote.provider_gamma AS gamma,
               quote.provider_theta AS theta, quote.provider_vega AS vega,
               quote.observed_at, snapshot.source_id AS source,
               contract.id::text AS contract_symbol
        FROM raw.option_quote quote
        JOIN raw.option_snapshot snapshot ON snapshot.id = quote.snapshot_id
        JOIN catalog.option_contract contract ON contract.id = quote.contract_id
        JOIN catalog.instrument instrument ON instrument.id = contract.underlying_instrument_id
        JOIN latest_symbol_snapshot latest
          ON latest.snapshot_id = snapshot.id AND latest.instrument_id = instrument.id
        ORDER BY instrument.symbol, contract.expiration, contract.strike, contract.option_type
    """,
    "options_expiries": """
        WITH latest_symbol_snapshot AS (
            SELECT DISTINCT ON (instrument.id)
                   instrument.id AS instrument_id, snapshot.id AS snapshot_id
            FROM raw.option_snapshot snapshot
            JOIN raw.option_quote quote ON quote.snapshot_id = snapshot.id
            JOIN catalog.option_contract contract ON contract.id = quote.contract_id
            JOIN catalog.instrument instrument ON instrument.id = contract.underlying_instrument_id
            ORDER BY instrument.id, snapshot.observed_at DESC,
                     CASE snapshot.source_id WHEN 'robinhood' THEN 0 WHEN 'ibkr' THEN 1 ELSE 2 END,
                     snapshot.id DESC
        )
        SELECT instrument.symbol, contract.expiration AS expiry,
               max(quote.observed_at) AS observed_at, snapshot.source_id AS source
        FROM raw.option_quote quote
        JOIN raw.option_snapshot snapshot ON snapshot.id = quote.snapshot_id
        JOIN catalog.option_contract contract ON contract.id = quote.contract_id
        JOIN catalog.instrument instrument ON instrument.id = contract.underlying_instrument_id
        JOIN latest_symbol_snapshot latest
          ON latest.snapshot_id = snapshot.id AND latest.instrument_id = instrument.id
        GROUP BY instrument.symbol, contract.expiration, snapshot.source_id
        ORDER BY instrument.symbol, contract.expiration
    """,
    "fundamentals": """
        SELECT instrument.symbol, observation.period_start, observation.period_end, observation.filed_at,
               observation.observed_at, observation.metric_set, observation.values,
               observation.source_id AS source, ingest_run.finished_at AS available_at,
               ingest_run.id::text AS source_version, observation.id::text AS revision
        FROM raw.fundamental_observation observation
        JOIN catalog.instrument instrument ON instrument.id = observation.instrument_id
        JOIN ingest.source source ON source.id = observation.source_id
          AND source.enabled AND source.operational_state = 'active'
        JOIN ingest.run ingest_run ON ingest_run.id = observation.ingest_run_id
          AND ingest_run.finished_at IS NOT NULL
        ORDER BY observation.observed_at DESC, ingest_run.finished_at DESC
    """,
    "catalysts": """
        SELECT catalyst.id::text, instrument.symbol, catalyst.starts_at, catalyst.title AS event,
               catalyst.expected_impact, catalyst.notes, catalyst.confidence,
               catalyst.source_id AS source, catalyst.version,
               catalyst.created_at AS available_at, catalyst.id::text AS revision
        FROM app.catalyst catalyst
        LEFT JOIN catalog.instrument instrument ON instrument.id = catalyst.instrument_id
        WHERE catalyst.status = 'current'
        ORDER BY catalyst.starts_at
    """,
    "disclosures": """
        SELECT disclosure.id::text, instrument.symbol, disclosure.source_type,
               disclosure.trader_name, disclosure.filer_name, disclosure.event_date,
               disclosure.filed_date, disclosure.action, disclosure.amount_text,
               disclosure.source_url, disclosure.details, disclosure.source_id AS source,
               ingest_run.finished_at AS available_at,
               ingest_run.id::text AS source_version, disclosure.id::text AS revision
        FROM raw.disclosure disclosure
        LEFT JOIN catalog.instrument instrument ON instrument.id = disclosure.instrument_id
        JOIN ingest.source source ON source.id = disclosure.source_id
          AND source.enabled AND source.operational_state = 'active'
        JOIN ingest.run ingest_run ON ingest_run.id = disclosure.ingest_run_id
          AND ingest_run.finished_at IS NOT NULL
        ORDER BY COALESCE(disclosure.event_date, disclosure.filed_date) DESC, ingest_run.finished_at DESC
    """,
    "news": """
        SELECT item.id::text, item.title, item.url, item.author, item.published_at,
               item.observed_at, item.summary, item.source_id AS source, item.metadata,
               ingest_run.finished_at AS available_at,
               ingest_run.id::text AS source_version, item.id::text AS revision
        FROM raw.content_item item
        JOIN ingest.source source ON source.id = item.source_id
          AND source.enabled AND source.operational_state = 'active'
        JOIN ingest.run ingest_run ON ingest_run.id = item.ingest_run_id
          AND ingest_run.finished_at IS NOT NULL
        WHERE item.kind IN ('news', 'article', 'blog', 'social')
        ORDER BY COALESCE(item.published_at, item.observed_at) DESC LIMIT 500
    """,
    "source_items": """
        SELECT item.id::text, item.source_id, item.source_key, item.kind, item.title,
               item.url, item.author, item.published_at, item.observed_at, item.summary,
               item.metadata
        FROM raw.content_item item ORDER BY item.observed_at DESC LIMIT 500
    """,
    "source_runs": """
        SELECT run.source_id, run.id::text AS run_id, run.capability, run.started_at,
               run.finished_at, run.status, run.item_count, run.instrument_count AS ticker_count,
               run.failure_detail, run.summary
        FROM ingest.run run ORDER BY run.started_at DESC LIMIT 200
    """,
    "provider_runs": """
        SELECT run.id::text, run.source_id AS provider, run.capability, run.started_at,
               run.finished_at, run.status, run.item_count, run.failure_detail, run.summary
        FROM ingest.run run ORDER BY run.started_at DESC LIMIT 200
    """,
    "source_health": """
        SELECT source.id AS source_id, source.name, source.enabled,
               run.status, run.started_at, run.finished_at,
               run.failure_detail, run.item_count, run.instrument_count AS ticker_count
        FROM ingest.source source
        LEFT JOIN LATERAL (
            SELECT status, started_at, finished_at, failure_detail, item_count, instrument_count
            FROM ingest.run WHERE source_id = source.id ORDER BY started_at DESC LIMIT 1
        ) run ON true
        ORDER BY source.family, source.id
    """,
    "option_strategy_versions": """
        SELECT strategy.id, strategy.strategy_key AS strategy_version, strategy.name AS strategy_name,
               strategy.revision AS version, strategy.created_at, strategy.created_at AS available_at,
               strategy.id::text AS source_version, strategy.revision AS revision, strategy.status,
               strategy.parameters, strategy.promoted_at, strategy.supersedes_id
        FROM analysis.strategy_revision strategy ORDER BY strategy.strategy_key, strategy.revision DESC
    """,
    "broker_accounts": """
        SELECT snapshot.id::text, snapshot.source_id AS provider, snapshot.account_key AS account_id,
               snapshot.observed_at AS updated_at, snapshot.currency, snapshot.net_liquidation,
               snapshot.buying_power, snapshot.cash_balance, snapshot.details,
               ingest_run.finished_at AS available_at,
               ingest_run.id::text AS source_version, snapshot.id::text AS revision
        FROM raw.broker_account_snapshot snapshot
        JOIN ingest.run ingest_run ON ingest_run.id = snapshot.ingest_run_id
          AND ingest_run.finished_at IS NOT NULL
        ORDER BY snapshot.observed_at DESC, ingest_run.finished_at DESC
    """,
    "broker_positions": """
        SELECT account.source_id AS provider, account.account_key AS account_id,
               instrument.symbol, instrument.asset_class, position.quantity,
               position.average_cost, position.market_price, position.market_value,
               position.unrealized_pnl, account.observed_at AS updated_at, position.details,
               ingest_run.finished_at AS available_at,
               ingest_run.id::text AS source_version, position.id::text AS revision
        FROM raw.broker_position_snapshot position
        JOIN raw.broker_account_snapshot account ON account.id = position.account_snapshot_id
        JOIN ingest.run ingest_run ON ingest_run.id = account.ingest_run_id
          AND ingest_run.finished_at IS NOT NULL
        JOIN catalog.instrument instrument ON instrument.id = position.instrument_id
        ORDER BY account.observed_at DESC, ingest_run.finished_at DESC, instrument.symbol
    """,
    "paper_orders": """
        SELECT orders.id::text, decision.decision_key AS recommendation_id,
               instrument.symbol, orders.created_at, orders.side, orders.quantity,
               orders.limit_price, orders.status, orders.lane, orders.policy_result,
               orders.policy_snapshot, orders.actual_fill_price, orders.filled_at,
               orders.exit_price, orders.exit_at, orders.fees, orders.ticket_version,
               orders.ticket_snapshot
        FROM app.paper_order orders
        LEFT JOIN analysis.decision decision ON decision.id = orders.decision_id
        JOIN catalog.instrument instrument ON instrument.id = orders.instrument_id
        ORDER BY orders.created_at DESC
    """,
    "trade_journal": """
        SELECT journal.id::text AS journal_id, journal.created_at, instrument.symbol AS ticker,
               journal.action, journal.quantity, journal.price, journal.rationale AS notes,
               journal.details
        FROM app.trade_journal journal
        JOIN catalog.instrument instrument ON instrument.id = journal.instrument_id
        ORDER BY journal.created_at DESC
    """,
    "radar_alert": """
        SELECT alert.id::text AS alert_id, alert.decision_id::text, instrument.symbol AS ticker,
               alert.created_at, alert.alert_type, alert.severity, alert.title,
               alert.detail, alert.acknowledged_at, alert.resolution_reason
        FROM app.alert alert
        LEFT JOIN catalog.instrument instrument ON instrument.id = alert.instrument_id
        ORDER BY alert.created_at DESC
    """,
    "candidate_event_mark": """
        SELECT decision.id::text AS candidate_event_id, decision.id::text AS event_id,
               instrument.symbol AS ticker, outcome.observed_through AS mark_time,
               decision.state AS candidate_state, outcome.current_return,
               outcome.peak_return AS max_return_since_alert,
               outcome.max_drawdown, outcome.maturity_state AS outcome_status,
               outcome.time_to_2x_days, outcome.time_to_5x_days, outcome.time_to_10x_days
        FROM analysis.option_outcome outcome
        JOIN analysis.decision decision ON decision.id = outcome.decision_id
        JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
        ORDER BY outcome.observed_through DESC, decision.id DESC
    """,
    "candidate_event_attribution": """
        SELECT decision.id::text AS candidate_event_id, decision.id::text AS event_id,
               instrument.symbol AS ticker, outcome.observed_through AS attributed_at,
               CASE WHEN outcome.peak_return >= 9 THEN 'winner_10x'
                    WHEN outcome.peak_return >= 4 THEN 'winner_5x'
                    WHEN outcome.peak_return >= 1 THEN 'winner_2x'
                    WHEN outcome.current_return < 0 THEN 'loser'
                    ELSE 'open' END AS label,
               outcome.current_return, outcome.peak_return, outcome.max_drawdown,
               outcome.stock_move_effect, outcome.iv_effect, outcome.theta_effect,
               outcome.spread_effect, outcome.unexplained_effect
        FROM analysis.option_outcome outcome
        JOIN analysis.decision decision ON decision.id = outcome.decision_id
        JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
        ORDER BY outcome.observed_through DESC, decision.id DESC
    """,
    "option_attribution": """
        SELECT decision.id::text AS candidate_event_id, decision.id::text AS event_id,
               instrument.symbol AS ticker, outcome.observed_through AS attributed_at,
               CASE WHEN outcome.peak_return >= 9 THEN 'winner_10x'
                    WHEN outcome.peak_return >= 4 THEN 'winner_5x'
                    WHEN outcome.peak_return >= 1 THEN 'winner_2x'
                    WHEN outcome.current_return < 0 THEN 'loser'
                    ELSE 'open' END AS label,
               outcome.current_return, outcome.peak_return, outcome.max_drawdown,
               outcome.stock_move_effect, outcome.iv_effect, outcome.theta_effect,
               outcome.spread_effect, outcome.unexplained_effect
        FROM analysis.option_outcome outcome
        JOIN analysis.decision decision ON decision.id = outcome.decision_id
        JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
        ORDER BY outcome.observed_through DESC
    """,
    "conviction_calibration": """
        SELECT NULL::text AS strategy_version, NULL::integer AS bin_index WHERE false
    """,
    "strategy_cohort_result": """
        SELECT coalesce(strategy.strategy_key, '') || ':' || decision.state AS stable_key,
               strategy.strategy_key AS strategy_version, decision.state,
               count(*) AS n, count(*) FILTER (WHERE outcome.maturity_state <> 'observing') AS mature_n,
               avg((outcome.time_to_2x_days IS NOT NULL)::integer) AS realized_p2x,
               avg((outcome.time_to_5x_days IS NOT NULL)::integer) AS realized_p5x,
               avg(outcome.peak_return) AS average_peak_return,
               min(decision.as_of) AS period_start, max(outcome.observed_through) AS period_end
        FROM analysis.option_outcome outcome
        JOIN analysis.decision decision ON decision.id = outcome.decision_id
        JOIN analysis.run run ON run.id = decision.run_id
        LEFT JOIN analysis.strategy_revision strategy ON strategy.id = decision.strategy_revision_id
        WHERE run.feature_versions->>'option' = 'option-professional-v3-ticket'
          AND decision.state <> 'REJECTED'
        GROUP BY strategy.strategy_key, decision.state
        ORDER BY strategy.strategy_key, decision.state
    """,
    "instrument_market_identity": """
        SELECT instrument.id AS instrument_id, instrument.symbol, instrument.name,
               instrument.asset_class, instrument.category, instrument.sector, instrument.industry,
               alias.exchange, alias.currency, alias.provider, alias.external_symbol,
               alias.metadata, instrument.updated_at
        FROM catalog.instrument instrument
        LEFT JOIN LATERAL (
            SELECT * FROM catalog.instrument_alias
            WHERE instrument_id = instrument.id ORDER BY id LIMIT 1
        ) alias ON true ORDER BY instrument.symbol
    """,
    "vol_surface_features": """
        SELECT instrument.symbol AS ticker, contract.expiration,
               avg(quote.provider_iv) FILTER (WHERE contract.option_type = 'call') AS call_iv,
               avg(quote.provider_iv) FILTER (WHERE contract.option_type = 'put') AS put_iv,
               avg(quote.provider_iv) FILTER (WHERE contract.option_type = 'put')
                 - avg(quote.provider_iv) FILTER (WHERE contract.option_type = 'call') AS put_call_skew,
               max(quote.observed_at) AS as_of, count(*) AS contracts
        FROM raw.option_quote quote
        JOIN catalog.option_contract contract ON contract.id = quote.contract_id
        JOIN catalog.instrument instrument ON instrument.id = contract.underlying_instrument_id
        GROUP BY instrument.symbol, contract.expiration
        ORDER BY as_of DESC, instrument.symbol, contract.expiration
    """,
    "exploration_gate_report": """
        SELECT run.id::text AS analysis_run_id, strategy.strategy_key AS strategy_version,
               instrument.symbol AS ticker, summary.gate_code,
               summary.reject_count, summary.sampled_decision_keys, run.started_at
        FROM analysis.reject_summary summary
        JOIN analysis.run run ON run.id = summary.run_id
        LEFT JOIN analysis.strategy_revision strategy ON strategy.id = summary.strategy_revision_id
        LEFT JOIN catalog.instrument instrument ON instrument.id = summary.instrument_id
        ORDER BY run.started_at DESC, summary.reject_count DESC
    """,
    "strategy_mutation_proposal": """
        SELECT task.id::text AS proposal_id, task.created_at, task.updated_at,
               task.status, task.request, task.result AS raw, task.validation
        FROM analysis.agent_task task
        WHERE task.task_kind = 'strategy_mutation_proposal'
        ORDER BY task.created_at DESC, task.id DESC
    """,
    "missed_winner_event": """
        SELECT decision.id::text AS candidate_event_id, instrument.symbol AS ticker,
               decision.as_of AS snapshot_time, outcome.observed_through,
               outcome.current_return, outcome.peak_return AS max_return_since_alert,
               decision.state AS prior_state,
               CASE WHEN outcome.peak_return >= 9 THEN '10x'
                    ELSE '5x' END AS outcome_type
        FROM analysis.option_outcome outcome
        JOIN analysis.decision decision ON decision.id = outcome.decision_id
        JOIN analysis.run run ON run.id = decision.run_id
        JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
        WHERE outcome.peak_return >= 4 AND decision.state NOT IN ('FIRE', 'READY')
          AND run.feature_versions->>'option' = 'option-professional-v3-ticket'
        ORDER BY outcome.peak_return DESC, decision.id DESC
    """,
    "radar_state_transition": """
        SELECT decision.id::text AS candidate_event_id, instrument.symbol AS ticker,
               decision.as_of AS transitioned_at, decision.state AS to_state,
               lag(decision.state) OVER (
                   PARTITION BY option_decision.contract_id ORDER BY decision.as_of
               ) AS from_state, decision.score
        FROM analysis.decision decision
        JOIN analysis.option_decision option_decision ON option_decision.decision_id = decision.id
        JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
        ORDER BY decision.as_of DESC
    """,
    "correlations": """
        WITH returns AS (
            SELECT instrument.id, instrument.symbol, bar.trading_date,
                   bar.close / lag(bar.close) OVER (PARTITION BY instrument.id
                       ORDER BY bar.trading_date) - 1 AS daily_return
            FROM raw.price_bar bar
            JOIN catalog.instrument instrument ON instrument.id = bar.instrument_id
            WHERE bar.interval = '1d' AND bar.trading_date >= current_date - 200
        )
        SELECT left_side.symbol, right_side.symbol AS peer_symbol, count(*) AS observations, corr(left_side.daily_return, right_side.daily_return) AS correlation
        FROM returns left_side JOIN returns right_side ON right_side.id > left_side.id
          AND right_side.trading_date = left_side.trading_date
        WHERE left_side.daily_return IS NOT NULL AND right_side.daily_return IS NOT NULL
        GROUP BY left_side.symbol, right_side.symbol HAVING count(*) >= 20
        ORDER BY abs(corr(left_side.daily_return, right_side.daily_return)) DESC LIMIT 500
    """,
    "owned_correlations": OWNED_CORRELATIONS_QUERY,
}


DIRECT_QUERIES.update({**SOURCE_QUERIES, **EVENT_DIRECT_QUERIES})


def today_authority_pages(
    config: AppConfig,
    *,
    decision_offset: int = 0,
    rank_offset: int = 0,
    plan_offset: int = 0,
    decision_limit: int = 100,
    rank_limit: int = 3,
    plan_limit: int = 3,
    batch_size: int = 25,
) -> Iterable[list[dict[str, Any]]]:
    """Stream bounded Today pages, counts, and validation from one snapshot."""

    safe_decision_offset = max(0, min(int(decision_offset), 10_000))
    safe_rank_offset = max(0, min(int(rank_offset), 10_000))
    safe_plan_offset = max(0, min(int(plan_offset), 10_000))
    safe_decision_limit = max(
        1, min(int(decision_limit), 10_003 - safe_decision_offset),
    )
    safe_rank_limit = max(
        1, min(int(rank_limit), 10_003 - safe_rank_offset),
    )
    safe_plan_limit = max(
        1, min(int(plan_limit), 10_003 - safe_plan_offset),
    )
    safe_decision_end = safe_decision_offset + safe_decision_limit
    safe_rank_end = safe_rank_offset + safe_rank_limit
    safe_plan_end = safe_plan_offset + safe_plan_limit
    safe_batch_size = max(1, min(int(batch_size), 100))
    query = f"""
        WITH current_candidates AS (
            SELECT decision.id AS decision_id,
                   decision.id::text AS ticker_decision_id,
                   instrument.symbol AS ticker, instrument.symbol,
                   decision.decision_revision, decision.as_of,
                   decision.published_at,
                   decision.published_at AS available_at,
                   decision.input_hash,
                   CASE WHEN octet_length(decision.capital_action::text) <= 4096
                        THEN decision.capital_action END AS capital_action,
                   decision.policy_version, decision.opportunity_episode_id,
                   CASE WHEN octet_length(decision.selected_expression::text) <= 8192
                        THEN decision.selected_expression END AS selected_expression,
                   CASE
                       WHEN jsonb_typeof(
                                decision.input_manifest->'opportunity_rank'
                            ) = 'object'
                        AND octet_length((
                                decision.input_manifest->'opportunity_rank'
                            )::text) <= 196608
                       THEN (decision.input_manifest->'opportunity_rank') - ARRAY[
                           'eligible_universe', 'input_lineage', 'utility'
                       ]
                   END AS opportunity_rank,
                   COALESCE(
                       jsonb_typeof(decision.input_manifest->'trade_plan') = 'object'
                       AND octet_length((
                           decision.input_manifest->'trade_plan'
                       )::text) <= 327680,
                       false
                   ) AS trade_plan_present,
                   decision.created_at,
                   count(*) OVER (
                       PARTITION BY decision.instrument_id, decision.as_of,
                                    decision.published_at
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
            JOIN catalog.instrument instrument
              ON instrument.id = decision.instrument_id
            WHERE decision.status = 'published'
              AND decision.contract_version = 'ticker-decision.v1'
              AND NULLIF(BTRIM(decision.decision_revision), '') IS NOT NULL
              AND NULLIF(BTRIM(decision.code_version), '') IS NOT NULL
              AND NULLIF(BTRIM(decision.experiment_id), '') IS NOT NULL
              AND NULLIF(BTRIM(decision.opportunity_episode_id), '') IS NOT NULL
              AND decision.as_of <= now()
              AND decision.published_at IS NOT NULL
              AND decision.published_at <= now()
              AND jsonb_typeof(decision.capital_action) = 'object'
              AND jsonb_typeof(decision.input_manifest) = 'object'
        ), current_authority AS (
            SELECT *
            FROM current_candidates
            WHERE current_row = 1
              AND authority_count = 1
              AND opportunity_authority_count = 1
        ), positioned_actions AS (
            SELECT current_authority.*,
                   row_number() OVER (
                       ORDER BY {TODAY_ACTION_ORDER_SQL}
                   ) AS decision_position,
                   count(*) FILTER (
                       WHERE jsonb_typeof(opportunity_rank) = 'object'
                   ) OVER (
                       ORDER BY {TODAY_ACTION_ORDER_SQL}
                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                   ) AS opportunity_rank_position,
                   count(*) FILTER (
                       WHERE trade_plan_present
                   ) OVER (
                       ORDER BY {TODAY_ACTION_ORDER_SQL}
                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                   ) AS trade_plan_position,
                   CASE WHEN ({TODAY_RESEARCH_RANK_VALID_SQL})
                        THEN true ELSE false END AS research_rank_present
            FROM current_authority
        ), authority_validated AS (
            SELECT positioned_actions.*,
                   CASE WHEN COALESCE(
                              positioned_actions.capital_action->>'owned',
                              'false'
                          ) <> 'true'
                          AND jsonb_typeof(
                              positioned_actions.opportunity_rank
                          ) = 'object'
                          AND (
                              positioned_actions.trade_plan_present
                              OR positioned_actions.research_rank_present
                          )
                    THEN jsonb_build_object(
                        'ticker',
                            positioned_actions.opportunity_rank->'ticker',
                        'symbol',
                            positioned_actions.opportunity_rank->'symbol',
                        'decision_revision',
                            positioned_actions.opportunity_rank
                                ->'decision_revision',
                        'opportunity_episode_id',
                            positioned_actions.opportunity_rank
                                ->'opportunity_episode_id',
                        'ranking_publication_id',
                            positioned_actions.opportunity_rank
                                ->'ranking_publication_id',
                        'publication_id',
                            positioned_actions.opportunity_rank
                                ->'publication_id',
                        'rank_id',
                            positioned_actions.opportunity_rank->'rank_id',
                        'selected_expression_identity',
                            positioned_actions.opportunity_rank
                                ->'selected_expression_identity',
                        'portfolio_impact_id',
                            positioned_actions.opportunity_rank
                                ->'portfolio_impact_id',
                        'market_state_publication_id',
                            positioned_actions.opportunity_rank
                                ->'market_state_publication_id'
                    ) END AS validation_rank,
                   CASE WHEN COALESCE(
                              positioned_actions.capital_action->>'owned',
                              'false'
                          ) <> 'true'
                          AND jsonb_typeof(
                              positioned_actions.opportunity_rank
                          ) = 'object'
                          AND NOT positioned_actions.research_rank_present
                          AND positioned_actions.trade_plan_present
                    THEN COALESCE(
                        validation_payload.trade_plan->>'contract_version'
                            = 'trade-plan.v1'
                        AND NULLIF(BTRIM(
                            validation_payload.trade_plan->>'trade_plan_id'
                        ), '') IS NOT NULL
                        AND validation_payload.trade_plan->>'trade_plan_id'
                            = stored_decision.resolution->>'trade_plan_id'
                        AND UPPER(BTRIM(
                            validation_payload.trade_plan->>'ticker'
                        )) = positioned_actions.ticker
                        AND validation_payload.trade_plan->>'decision_revision'
                            = positioned_actions.decision_revision
                        AND validation_payload.trade_plan
                            ->>'opportunity_episode_id'
                            = positioned_actions.opportunity_episode_id
                        AND validation_payload.trade_plan->>'policy_version'
                            = positioned_actions.policy_version
                        AND validation_payload.trade_plan->'cutoff'
                            = stored_decision.opportunity_episode->'cutoff'
                        AND jsonb_typeof(
                            validation_payload.trade_plan->'input_lineage'
                        ) = 'array'
                        AND jsonb_array_length(
                            validation_payload.trade_plan->'input_lineage'
                        ) > 0
                        AND validation_payload.trade_plan->'input_lineage'
                            = stored_decision.opportunity_episode->'input_lineage'
                        AND jsonb_typeof(
                            validation_payload.trade_plan->'selected_expression'
                        ) = 'object'
                        AND validation_payload.trade_plan->'selected_expression'
                            = stored_decision.selected_expression
                        AND validation_payload.trade_plan
                            ->>'selected_expression_kind'
                            = validation_payload.trade_plan
                                ->'selected_expression'->>'kind'
                        AND UPPER(BTRIM(
                            validation_payload.trade_plan
                                ->'selected_expression'->>'ticker'
                        )) = positioned_actions.ticker
                        AND validation_payload.trade_plan->>'publication_id'
                            = COALESCE(
                                NULLIF(BTRIM(
                                    positioned_actions.opportunity_rank
                                        ->>'ranking_publication_id'
                                ), ''),
                                NULLIF(BTRIM(
                                    positioned_actions.opportunity_rank
                                        ->>'publication_id'
                                ), '')
                            )
                        AND NULLIF(BTRIM(
                            validation_payload.trade_plan->>'rank_id'
                        ), '') = NULLIF(BTRIM(
                            positioned_actions.opportunity_rank->>'rank_id'
                        ), '')
                        AND NULLIF(BTRIM(
                            validation_payload.trade_plan
                                ->>'selected_expression_identity'
                        ), '') = NULLIF(BTRIM(
                            positioned_actions.opportunity_rank
                                ->>'selected_expression_identity'
                        ), '')
                        AND NULLIF(BTRIM(
                            validation_payload.trade_plan
                                ->>'portfolio_impact_id'
                        ), '') IS NOT NULL
                        AND NULLIF(BTRIM(
                            positioned_actions.opportunity_rank
                                ->>'portfolio_impact_id'
                        ), '') IS NOT NULL
                        AND NULLIF(BTRIM(
                            validation_payload.trade_plan
                                ->>'portfolio_impact_id'
                        ), '') = NULLIF(BTRIM(
                            positioned_actions.opportunity_rank
                                ->>'portfolio_impact_id'
                        ), '')
                        AND NULLIF(BTRIM(
                            validation_payload.trade_plan
                                ->>'market_state_publication_id'
                        ), '') IS NOT NULL
                        AND NULLIF(BTRIM(
                            positioned_actions.opportunity_rank
                                ->>'market_state_publication_id'
                        ), '') IS NOT NULL
                        AND NULLIF(BTRIM(
                            validation_payload.trade_plan
                                ->>'market_state_publication_id'
                        ), '') = NULLIF(BTRIM(
                            positioned_actions.opportunity_rank
                                ->>'market_state_publication_id'
                        ), '')
                        AND (
                            (
                                validation_payload.trade_plan->>'eligibility'
                                    = 'BLOCKED'
                                AND validation_payload.trade_plan
                                    ->>'selected_expression_kind' = 'CASH'
                                AND validation_payload.trade_plan->>'action'
                                    = 'NO_TRADE'
                                AND validation_payload.trade_plan
                                    ->>'authorization_mode' = 'NONE'
                                AND NULLIF(BTRIM(
                                    validation_payload.trade_plan
                                        ->>'primary_blocker'
                                ), '') IS NOT NULL
                                AND jsonb_typeof(
                                    validation_payload.trade_plan->'blockers'
                                ) = 'array'
                                AND validation_payload.trade_plan->'blockers'
                                    ? (validation_payload.trade_plan
                                        ->>'primary_blocker')
                            )
                            OR (
                                validation_payload.trade_plan->>'eligibility'
                                    = 'ACTIONABLE'
                                AND validation_payload.trade_plan
                                    ->>'selected_expression_kind' <> 'CASH'
                                AND validation_payload.trade_plan->>'action'
                                    NOT IN ('NO_TRADE', 'AVOID')
                                AND validation_payload.trade_plan
                                    ->>'authorization_mode'
                                    IN ('ADVISORY', 'PAPER')
                            )
                        ),
                        false
                    )
               END AS validation_plan_valid,
               COALESCE(
                   positioned_actions.capital_action->>'owned', 'false'
               ) <> 'true'
                   AND jsonb_typeof(positioned_actions.opportunity_rank)
                       IS DISTINCT FROM 'object'
                   AND positioned_actions.trade_plan_present
                   AS invalid_without_rank,
               COALESCE(
                   positioned_actions.capital_action->>'owned', 'false'
               ) <> 'true'
                   AND jsonb_typeof(positioned_actions.opportunity_rank) = 'object'
                   AND positioned_actions.research_rank_present
                   AS raw_research_rank_present,
               COALESCE(
                   positioned_actions.capital_action->>'owned', 'false'
               ) <> 'true'
                   AND (
                       positioned_actions.trade_plan_present
                       OR (
                           jsonb_typeof(
                               positioned_actions.opportunity_rank
                           ) = 'object'
                           AND positioned_actions.research_rank_present
                       )
                   ) AS needs_missing_plan_validation
            FROM positioned_actions
            LEFT JOIN analysis.ticker_decision stored_decision
              ON stored_decision.id = positioned_actions.decision_id
            CROSS JOIN LATERAL (
                SELECT stored_decision.input_manifest->'trade_plan' AS trade_plan
            ) validation_payload
        ), authority_rows AS (
            SELECT authority_validated.*,
                   CASE WHEN validation_rank IS NULL THEN false
                        ELSE COALESCE(
                            UPPER(COALESCE(
                                NULLIF(validation_rank->>'ticker', ''),
                                NULLIF(validation_rank->>'symbol', ''),
                                ''
                            )) = ticker
                            AND COALESCE(
                                validation_rank->>'decision_revision', ''
                            ) = decision_revision
                            AND COALESCE(
                                validation_rank->>'opportunity_episode_id', ''
                            ) = opportunity_episode_id
                            AND (
                                NULLIF(BTRIM(
                                    validation_rank->>'ranking_publication_id'
                                ), '') IS NULL
                                OR NULLIF(BTRIM(
                                    validation_rank->>'publication_id'
                                ), '') IS NULL
                                OR validation_rank->>'ranking_publication_id'
                                    = validation_rank->>'publication_id'
                            ),
                            false
                        )
                   END AS validation_rank_valid
            FROM authority_validated
        ), authority_totals AS (
            SELECT count(*) AS ticker_decision_count,
                   count(*) FILTER (
                       WHERE jsonb_typeof(opportunity_rank) = 'object'
                   ) AS opportunity_rank_count,
                   count(*) FILTER (
                       WHERE trade_plan_present
                   ) AS trade_plan_count,
                   count(*) FILTER (
                       WHERE NOT trade_plan_present
                         AND (
                             jsonb_typeof(opportunity_rank)
                                 IS DISTINCT FROM 'object'
                             OR NOT research_rank_present
                         )
                         AND COALESCE(capital_action->>'owned', 'false') <> 'true'
                   ) AS missing_plan_count,
                   count(*) FILTER (
                       WHERE needs_missing_plan_validation
                         AND (
                             NOT validation_rank_valid
                             OR (
                                 NOT raw_research_rank_present
                                 AND validation_plan_valid IS NOT TRUE
                             )
                         )
                   ) AS missing_plan_correction_count
            FROM authority_rows
        )
        SELECT positioned_actions.decision_position,
               authority_totals.ticker_decision_count,
               authority_totals.opportunity_rank_count,
               authority_totals.trade_plan_count,
               authority_totals.missing_plan_count,
               authority_totals.missing_plan_correction_count,
               CASE WHEN positioned_actions.decision_position
                              > {safe_decision_offset}
                          AND positioned_actions.decision_position
                              <= {safe_decision_end}
                    THEN jsonb_strip_nulls(jsonb_build_object(
                        'ticker_decision_id', positioned_actions.ticker_decision_id,
                        'ticker', positioned_actions.ticker,
                        'symbol', positioned_actions.symbol,
                        'decision_revision', positioned_actions.decision_revision,
                        'as_of', positioned_actions.as_of,
                        'published_at', positioned_actions.published_at,
                        'available_at', positioned_actions.available_at,
                        'input_hash', positioned_actions.input_hash,
                        'capital_action', positioned_actions.capital_action,
                            'resolution', CASE
                            WHEN octet_length(stored_decision.resolution::text)
                                 <= 196608
                            THEN stored_decision.resolution END,
                        'policy_version', positioned_actions.policy_version,
                        'opportunity_episode_id',
                            positioned_actions.opportunity_episode_id,
                        'selected_expression',
                            positioned_actions.selected_expression
                    )) END AS ticker_decision,
               CASE WHEN positioned_actions.decision_position
                              > {safe_decision_offset}
                          AND positioned_actions.decision_position
                              <= {safe_decision_end}
                          AND jsonb_typeof(positioned_actions.opportunity_rank)
                              = 'object'
                    THEN positioned_actions.validation_rank
               END AS decision_opportunity_rank,
               CASE WHEN positioned_actions.decision_position
                              > {safe_decision_offset}
                          AND positioned_actions.decision_position
                              <= {safe_decision_end}
                          AND positioned_actions.trade_plan_present
                    THEN stored_decision.input_manifest->'trade_plan'
               END AS decision_trade_plan,
               CASE WHEN jsonb_typeof(
                            positioned_actions.opportunity_rank
                         ) = 'object'
                          AND positioned_actions.opportunity_rank_position
                              > {safe_rank_offset}
                          AND positioned_actions.opportunity_rank_position
                              <= {safe_rank_end}
                    THEN positioned_actions.opportunity_rank
               END AS opportunity_rank_page,
               CASE WHEN positioned_actions.trade_plan_present
                          AND positioned_actions.trade_plan_position
                              > {safe_plan_offset}
                          AND positioned_actions.trade_plan_position
                              <= {safe_plan_end}
                    THEN stored_decision.input_manifest->'trade_plan'
               END AS trade_plan_page,
               positioned_actions.ticker,
               positioned_actions.decision_revision,
               positioned_actions.opportunity_episode_id,
               CASE WHEN COALESCE(
                              positioned_actions.capital_action->>'owned',
                              'false'
                          ) <> 'true'
                          AND jsonb_typeof(
                              positioned_actions.opportunity_rank
                          ) = 'object'
                          AND (
                              positioned_actions.trade_plan_present
                              OR positioned_actions.research_rank_present
                          )
                    THEN jsonb_build_object(
                        'ticker',
                            positioned_actions.opportunity_rank->'ticker',
                        'symbol',
                            positioned_actions.opportunity_rank->'symbol',
                        'decision_revision',
                            positioned_actions.opportunity_rank
                                ->'decision_revision',
                        'opportunity_episode_id',
                            positioned_actions.opportunity_rank
                                ->'opportunity_episode_id',
                        'ranking_publication_id',
                            positioned_actions.opportunity_rank
                                ->'ranking_publication_id',
                        'publication_id',
                            positioned_actions.opportunity_rank
                                ->'publication_id',
                        'rank_id',
                            positioned_actions.opportunity_rank->'rank_id',
                        'selected_expression_identity',
                            positioned_actions.opportunity_rank
                                ->'selected_expression_identity',
                        'portfolio_impact_id',
                            positioned_actions.opportunity_rank
                                ->'portfolio_impact_id',
                        'market_state_publication_id',
                            positioned_actions.opportunity_rank
                                ->'market_state_publication_id'
                    ) END AS validation_rank,
               CASE WHEN COALESCE(
                              positioned_actions.capital_action->>'owned',
                              'false'
                          ) <> 'true'
                          AND jsonb_typeof(
                              positioned_actions.opportunity_rank
                          ) = 'object'
                          AND NOT positioned_actions.research_rank_present
                          AND positioned_actions.trade_plan_present
                    THEN COALESCE(
                        validation_payload.trade_plan->>'contract_version'
                            = 'trade-plan.v1'
                        AND NULLIF(BTRIM(
                            validation_payload.trade_plan->>'trade_plan_id'
                        ), '') IS NOT NULL
                        AND validation_payload.trade_plan->>'trade_plan_id'
                            = stored_decision.resolution->>'trade_plan_id'
                        AND UPPER(BTRIM(
                            validation_payload.trade_plan->>'ticker'
                        )) = positioned_actions.ticker
                        AND validation_payload.trade_plan->>'decision_revision'
                            = positioned_actions.decision_revision
                        AND validation_payload.trade_plan
                            ->>'opportunity_episode_id'
                            = positioned_actions.opportunity_episode_id
                        AND validation_payload.trade_plan->>'policy_version'
                            = positioned_actions.policy_version
                        AND validation_payload.trade_plan->'cutoff'
                            = stored_decision.opportunity_episode->'cutoff'
                        AND jsonb_typeof(
                            validation_payload.trade_plan->'input_lineage'
                        ) = 'array'
                        AND jsonb_array_length(
                            validation_payload.trade_plan->'input_lineage'
                        ) > 0
                        AND validation_payload.trade_plan->'input_lineage'
                            = stored_decision.opportunity_episode->'input_lineage'
                        AND jsonb_typeof(
                            validation_payload.trade_plan->'selected_expression'
                        ) = 'object'
                        AND validation_payload.trade_plan->'selected_expression'
                            = stored_decision.selected_expression
                        AND validation_payload.trade_plan
                            ->>'selected_expression_kind'
                            = validation_payload.trade_plan
                                ->'selected_expression'->>'kind'
                        AND UPPER(BTRIM(
                            validation_payload.trade_plan
                                ->'selected_expression'->>'ticker'
                        )) = positioned_actions.ticker
                        AND validation_payload.trade_plan->>'publication_id'
                            = COALESCE(
                                NULLIF(BTRIM(
                                    positioned_actions.opportunity_rank
                                        ->>'ranking_publication_id'
                                ), ''),
                                NULLIF(BTRIM(
                                    positioned_actions.opportunity_rank
                                        ->>'publication_id'
                                ), '')
                            )
                        AND NULLIF(BTRIM(
                            validation_payload.trade_plan->>'rank_id'
                        ), '') = NULLIF(BTRIM(
                            positioned_actions.opportunity_rank->>'rank_id'
                        ), '')
                        AND NULLIF(BTRIM(
                            validation_payload.trade_plan
                                ->>'selected_expression_identity'
                        ), '') = NULLIF(BTRIM(
                            positioned_actions.opportunity_rank
                                ->>'selected_expression_identity'
                        ), '')
                        AND NULLIF(BTRIM(
                            validation_payload.trade_plan
                                ->>'portfolio_impact_id'
                        ), '') IS NOT NULL
                        AND NULLIF(BTRIM(
                            positioned_actions.opportunity_rank
                                ->>'portfolio_impact_id'
                        ), '') IS NOT NULL
                        AND NULLIF(BTRIM(
                            validation_payload.trade_plan
                                ->>'portfolio_impact_id'
                        ), '') = NULLIF(BTRIM(
                            positioned_actions.opportunity_rank
                                ->>'portfolio_impact_id'
                        ), '')
                        AND NULLIF(BTRIM(
                            validation_payload.trade_plan
                                ->>'market_state_publication_id'
                        ), '') IS NOT NULL
                        AND NULLIF(BTRIM(
                            positioned_actions.opportunity_rank
                                ->>'market_state_publication_id'
                        ), '') IS NOT NULL
                        AND NULLIF(BTRIM(
                            validation_payload.trade_plan
                                ->>'market_state_publication_id'
                        ), '') = NULLIF(BTRIM(
                            positioned_actions.opportunity_rank
                                ->>'market_state_publication_id'
                        ), '')
                        AND (
                            (
                                validation_payload.trade_plan->>'eligibility'
                                    = 'BLOCKED'
                                AND validation_payload.trade_plan
                                    ->>'selected_expression_kind' = 'CASH'
                                AND validation_payload.trade_plan->>'action'
                                    = 'NO_TRADE'
                                AND validation_payload.trade_plan
                                    ->>'authorization_mode' = 'NONE'
                                AND NULLIF(BTRIM(
                                    validation_payload.trade_plan
                                        ->>'primary_blocker'
                                ), '') IS NOT NULL
                                AND jsonb_typeof(
                                    validation_payload.trade_plan->'blockers'
                                ) = 'array'
                                AND validation_payload.trade_plan->'blockers'
                                    ? (validation_payload.trade_plan
                                        ->>'primary_blocker')
                            )
                            OR (
                                validation_payload.trade_plan->>'eligibility'
                                    = 'ACTIONABLE'
                                AND validation_payload.trade_plan
                                    ->>'selected_expression_kind' <> 'CASH'
                                AND validation_payload.trade_plan->>'action'
                                    NOT IN ('NO_TRADE', 'AVOID')
                                AND validation_payload.trade_plan
                                    ->>'authorization_mode'
                                    IN ('ADVISORY', 'PAPER')
                            )
                        ),
                        false
                    )
               END AS validation_plan_valid,
               COALESCE(
                   positioned_actions.capital_action->>'owned', 'false'
               ) <> 'true'
                   AND jsonb_typeof(positioned_actions.opportunity_rank)
                       IS DISTINCT FROM 'object'
                   AND positioned_actions.trade_plan_present
                   AS invalid_without_rank,
               COALESCE(
                   positioned_actions.capital_action->>'owned', 'false'
               ) <> 'true'
                   AND jsonb_typeof(positioned_actions.opportunity_rank) = 'object'
                   AND positioned_actions.research_rank_present
                   AS raw_research_rank_present,
               COALESCE(
                   positioned_actions.capital_action->>'owned', 'false'
               ) <> 'true'
                   AND (
                       positioned_actions.trade_plan_present
                       OR (
                           jsonb_typeof(
                               positioned_actions.opportunity_rank
                           ) = 'object'
                           AND positioned_actions.research_rank_present
                       )
                   ) AS needs_missing_plan_validation
        FROM authority_totals
        LEFT JOIN authority_rows AS positioned_actions
          ON (
              positioned_actions.decision_position > {safe_decision_offset}
              AND positioned_actions.decision_position <= {safe_decision_end}
             )
          OR (
              positioned_actions.opportunity_rank_position > {safe_rank_offset}
              AND positioned_actions.opportunity_rank_position <= {safe_rank_end}
             )
          OR (
              positioned_actions.trade_plan_position > {safe_plan_offset}
              AND positioned_actions.trade_plan_position <= {safe_plan_end}
             )
        LEFT JOIN analysis.ticker_decision stored_decision
          ON stored_decision.id = positioned_actions.decision_id
        CROSS JOIN LATERAL (
            SELECT stored_decision.input_manifest->'trade_plan' AS trade_plan
        ) validation_payload
        ORDER BY positioned_actions.decision_position
    """
    runtime = runtime_for_config(config)
    with runtime.snapshot(API_PROFILE) as connection:
        with connection.cursor(name="today_authority") as cursor:
            cursor.execute(query)
            while rows := cursor.fetchmany(safe_batch_size):
                yield [dict(row) for row in rows]


MODEL_ALIASES = {
    "screener": "universe_screen",
    "signals": "ticker_source_signals",
    "earnings_setups": "earnings",
    "stock_features": "technicals",
    "sepa": "technicals",
    "ticker_memos": "research_packets",
    "opportunity_sources": "ticker_source_signals",
    "options_expiry_signals": "options_expiries",
    "shadow_trade_mark": "candidate_event_mark",
    "correlation_edges": "owned_correlations",
    "exposure_clusters": "owned_correlations",
    "symbol_decision_snapshot": "symbol_decision_snapshots",
}


QUERY_POLICIES = build_query_policies(DIRECT_QUERIES)

AGENT_MODELS = {
    "agent_thesis_request", "agent_thesis", "agent_thesis_validation",
    "agent_postmortem_request", "agent_postmortem",
}

PUBLICATION_MODELS = {
    "option_snapshot", "option_features", "option_radar_opportunity",
    "option_discovery_candidate", "option_gate_result",
    "candidate_event", "option_radar_summary", "option_radar_symbol_summary",
    "option_action_queue", "option_calibration", "preopen_daily_brief",
    "daily_brief", "portfolio_risk_cards", "review_actions",
    "decision_queue", "decision_readiness", "symbol_decision_snapshots",
    "opportunities_ranked", "candidates", "feed_signals",
    "market_environment_assets", "market_environment_model",
    "market_valuation_reference_charts", "market_state_snapshot", "coverage_matrix",
    "instrument_state_snapshot", "alpha_signal", "opportunity_rank", "trade_plan",
    "outcome_attribution",
}

SPECIAL_MODELS = {
    "portfolio", "manual_watchlist", "theses", "thesis_monitor",
    "refresh_jobs", "broker_status",
    "portfolio_summary", "portfolio_performance", "portfolio_transactions",
    "correlation_edges", "exposure_clusters", "portfolio_risk_cards", "review_actions",
    "paper_orders", "superinvestor_portfolios",
    *RECOVERY_MODELS,
}


def load_postgres_tables(
    config: AppConfig,
    table_names: Iterable[str],
    *,
    query_row_limits: Mapping[str, int] | None = None,
    query_symbol_filter: set[str] | None = None,
    runtime_profile: RuntimeProfile = API_PROFILE,
    portfolio_summary_include_performance: bool = True,
    thesis_monitor_include_current_prices: bool = True,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    requested = tuple(dict.fromkeys(table_names))
    runtime = runtime_for_config(config)
    published_counts: dict[str, int] = {}
    intelligence_counts: dict[str, int] = {}
    publication_options: dict[str, Any] = {}
    if query_row_limits:
        publication_options.update(row_limits=query_row_limits, total_counts=published_counts)
    if query_symbol_filter is not None:
        publication_options["symbols"] = query_symbol_filter
    publication_requested = tuple(name for name in requested if name in PUBLICATION_MODELS)
    tables = _published_tables(runtime, publication_requested, **publication_options)
    if {"option_radar_opportunity", "option_radar_summary"}.intersection(tables):
        from investment_panel.database.option_ticket_read import reconcile_loaded_radar_tables

        reconcile_loaded_radar_tables(runtime, tables, config)
    intelligence_models = {
        "portfolio",
        "portfolio_summary", "portfolio_performance", "portfolio_transactions",
        "correlation_edges", "exposure_clusters", "portfolio_risk_cards", "review_actions",
    }
    requested_intelligence = intelligence_models.intersection(requested)
    if requested_intelligence == {"portfolio"}:
        # The position list is the inexpensive primitive; do not construct the
        # intelligence bundle for callers that only need holdings.
        tables["portfolio"] = portfolio_rows(config)
    elif requested_intelligence:
        # Keep existing test/facade seams compatible while the concrete owner
        # accepts the selected model set for bounded production reads.
        intelligence_signature = inspect.signature(portfolio_intelligence_tables).parameters
        intelligence_options: dict[str, Any] = {}
        if "models" in intelligence_signature:
            intelligence_options["models"] = requested_intelligence
        if "include_performance" in intelligence_signature:
            intelligence_options["include_performance"] = portfolio_summary_include_performance
        if "row_limits" in intelligence_signature:
            intelligence_options["row_limits"] = query_row_limits
        if "total_counts" in intelligence_signature:
            intelligence_options["total_counts"] = intelligence_counts
        if "symbols" in intelligence_signature:
            intelligence_options["symbols"] = query_symbol_filter
        live_tables = portfolio_intelligence_tables(config, **intelligence_options)
        for name in requested_intelligence:
            tables[name] = live_tables.get(name, [])
            published_counts.pop(name, None)
    if "manual_watchlist" in requested:
        tables["manual_watchlist"] = watchlist_rows(config)
    if "theses" in requested:
        tables["theses"] = thesis_rows(config)
    if "thesis_monitor" in requested:
        tables["thesis_monitor"] = thesis_monitor_rows(
            config,
            symbols=query_symbol_filter,
            include_current_prices=thesis_monitor_include_current_prices,
        )
    if "refresh_jobs" in requested:
        tables["refresh_jobs"] = JobRepository(runtime).rows()
    if "broker_status" in requested:
        tables["broker_status"] = broker_status_rows(runtime)
    requested_recovery = RECOVERY_MODELS.intersection(requested)
    if requested_recovery:
        recovery_settings = config.analysis.options_decision_system
        tables.update(
            RecoveryReadRepository(
                runtime,
                recovery_paper_actions_enabled=recovery_settings.recovery_paper_actions_enabled,
            ).panel_models(requested_recovery)
        )
    for name in AGENT_MODELS.intersection(requested):
        tables[name] = AgentRepository(runtime).rows(name)
    query_cache: dict[tuple[str, int | None, bool], list[dict[str, Any]]] = {}
    query_cache_counts: dict[tuple[str, int | None, bool], int] = {}
    query_counts: dict[str, int] = {}
    with runtime.read(runtime_profile) as connection:
        for name in requested:
            if name in tables:
                continue
            if name == "ticker_policy_learning":
                canonical = AnalysisRepository(runtime).publication_rows(
                    "ticker-outcome-attribution", "outcome_attribution", include_lineage=True,
                )
                active_strategies = connection.execute(
                    "SELECT id, strategy_key, revision FROM analysis.strategy_revision "
                    "WHERE authority_group = 'options-radar-core' AND status = 'active' "
                    "ORDER BY id"
                ).fetchall()
                governance_rows = []
                governance_authority = "unavailable"
                governance_strategy_id = None
                governance_strategy_key = None
                governance_strategy_revision = None
                if len(active_strategies) == 1:
                    governance_strategy_id = int(active_strategies[0]["id"])
                    governance_strategy_key = str(active_strategies[0]["strategy_key"])
                    governance_strategy_revision = int(active_strategies[0]["revision"])
                    governance_rows = connection.execute(
                        """
                        SELECT evaluation_type AS stage, verdict, metrics, evidence,
                               evaluated_at, available_at, period_start, period_end
                        FROM analysis.strategy_evaluation
                        WHERE strategy_revision_id = %s
                        ORDER BY evaluated_at DESC, id DESC
                        """,
                        [governance_strategy_id],
                    ).fetchall()
                    governance_authority = "available"
                tables[name] = [{
                    "authority": "outcome-attribution.v1",
                    "episodes": canonical,
                    "governance_evaluations": [dict(row) for row in governance_rows],
                    "governance_authority": governance_authority,
                    "governance_strategy_revision_id": governance_strategy_id,
                    "governance_strategy_key": governance_strategy_key,
                    "governance_strategy_revision": governance_strategy_revision,
                }]
                continue
            if name == "superinvestor_portfolios":
                tables[name] = superinvestor_portfolios(connection)
                continue
            alias = MODEL_ALIASES.get(name)
            policy = QUERY_POLICIES.get(alias or name)
            if policy:
                limit = int((query_row_limits or {}).get(name) or 0) or None
                symbol_scoped = query_symbol_filter is not None and policy.symbol_scoped
                cache_key = (alias or name, limit, symbol_scoped)
                if cache_key not in query_cache:
                    if policy.custom_loader == "options_ticker_signals":
                        rows = options_ticker_signal_rows(connection, symbols=query_symbol_filter if symbol_scoped else None)
                        query_cache_counts[cache_key] = len(rows)
                        query_cache[cache_key] = rows[:limit] if limit else rows
                    elif policy.custom_loader == "current_quotes":
                        rows = current_quote_rows(
                            connection,
                            symbols=query_symbol_filter if symbol_scoped else None,
                            # A symbol-scoped quote read is bounded by the
                            # requested instruments. Count it before the
                            # defensive row limit so pagination stays exact.
                            limit=None if symbol_scoped else limit,
                        )
                        if symbol_scoped:
                            query_cache_counts[cache_key] = len(rows)
                        query_cache[cache_key] = rows[:limit] if limit else rows
                    elif policy.custom_loader == "technicals":
                        rows = technical_rows(
                            connection,
                            symbols=query_symbol_filter if symbol_scoped else None,
                        )
                        query_cache_counts[cache_key] = len(rows)
                        query_cache[cache_key] = rows[:limit] if limit else rows
                    elif policy.custom_loader == "universe_screen":
                        rows = _universe_screen_rows(connection, limit=limit)
                        query_cache_counts[cache_key] = int(
                            rows[0].get("__panel_total_count") or 0
                        ) if rows else 0
                        query_cache[cache_key] = rows
                    elif policy.custom_loader == "liquidity":
                        query_cache[cache_key] = _liquidity_rows(
                            connection,
                            symbols=query_symbol_filter if symbol_scoped else None,
                            limit=limit,
                        )
                    elif policy.custom_loader == "options_payoff_scenarios":
                        query_cache[cache_key] = _options_payoff_scenario_rows(
                            connection,
                            symbols=query_symbol_filter if symbol_scoped else None,
                            limit=limit,
                        )
                    elif policy.custom_loader == "options_expiries":
                        query_cache[cache_key] = _options_expiry_rows(
                            connection,
                            symbols=query_symbol_filter if symbol_scoped else None,
                            limit=limit,
                        )
                    else:
                        selected_query = RESEARCH_PACKETS_BASE_QUERY if symbol_scoped and (alias or name) == "research_packets" else policy.query
                        selected_columns = (
                            "daily_research_rows.*, count(*) OVER () AS __panel_total_count"
                            if limit
                            else "daily_research_rows.*"
                        )
                        bounded_query = f"SELECT {selected_columns} FROM ({selected_query}) AS daily_research_rows"
                        parameters: list[Any] = []
                        conditions: list[str] = []
                        if policy.exclude_future_rows and (alias or name) in {"catalysts", "earnings"}:
                            conditions.append("daily_research_rows.starts_at >= current_date")
                        if policy.exclude_future_rows and (alias or name) == "research_packets":
                            conditions.append("COALESCE(daily_research_rows.published_at, daily_research_rows.generated_at) <= now()")
                        if symbol_scoped:
                            symbol_clause = "UPPER(daily_research_rows.symbol) = ANY(%s)"
                            if policy.allow_symbol_less:
                                symbol_clause = f"({symbol_clause} OR daily_research_rows.symbol IS NULL)"
                            conditions.append(symbol_clause)
                            parameters.append(sorted(query_symbol_filter))
                        if conditions:
                            bounded_query += " WHERE " + " AND ".join(conditions)
                        if policy.chronological:
                            bounded_query += " ORDER BY daily_research_rows.starts_at"
                        if limit:
                            bounded_query += f" LIMIT {limit}"
                        result = connection.execute(bounded_query, parameters) if parameters else connection.execute(bounded_query)
                        query_cache[cache_key] = [dict(row) for row in result.fetchall()]
                tables[name] = query_cache[cache_key]
                if cache_key in query_cache_counts:
                    query_counts[name] = query_cache_counts[cache_key]
            elif alias in PUBLICATION_MODELS:
                tables[name] = AnalysisRepository(runtime).publication_rows(
                    "today", alias, include_lineage=True,
                )
            elif name in PUBLICATION_MODELS:
                tables[name] = []
            else:
                tables[name] = []
    supported = (
        set(QUERY_POLICIES) | PUBLICATION_MODELS | SPECIAL_MODELS | AGENT_MODELS
        | set(MODEL_ALIASES)
    )
    unavailable = sorted(name for name in requested if name not in supported)
    table_counts = {name: len(rows) for name, rows in tables.items()}
    table_counts.update(published_counts)
    table_counts.update(intelligence_counts)
    table_counts.update(query_counts)
    for name, rows in tables.items():
        for row in rows:
            total_count = row.get("__panel_total_count")
            if isinstance(total_count, int) and not isinstance(total_count, bool):
                table_counts[name] = total_count
                break
    for rows in tables.values():
        for row in rows:
            row.pop("__panel_total_count", None)
    metadata = {
        "database": "postgresql",
        "schema_revision": HEAD_REVISION,
        "loaded_at": datetime.now(UTC).isoformat(),
        "table_count": len(requested),
        "unavailable_models": unavailable,
        "retired_models": [],
        "available_model_count": len(requested) - len(unavailable),
        "table_counts": table_counts,
    }
    return tables, metadata


def _normalized_symbols(symbols: set[str] | None) -> list[str] | None:
    if symbols is None:
        return None
    return sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})


def _universe_screen_rows(
    connection: Any,
    *,
    limit: int | None,
) -> list[dict[str, Any]]:
    candidate_limit = 2_147_483_647 if limit is None else max(1, min(int(limit), 10_500))
    query = SOURCE_UNIVERSE_QUERIES["universe_screen"].replace(
        "__CANDIDATE_LIMIT__", str(candidate_limit), 1,
    )
    result = connection.execute(query)
    return [dict(row) for row in result.fetchall()]


def _liquidity_rows(
    connection: Any,
    *,
    symbols: set[str] | None,
    limit: int | None,
) -> list[dict[str, Any]]:
    normalized = _normalized_symbols(symbols)
    if normalized == []:
        return []
    symbol_scope_cte = (
        "requested_symbols AS (SELECT unnest(%s::text[]) AS symbol),"
        if normalized is not None
        else ""
    )
    history_filter_sql = (
        "AND snapshot.history_symbol IN (SELECT symbol FROM requested_symbols)"
        if normalized is not None
        else ""
    )
    instrument_filter_sql = (
        "AND instrument.symbol IN (SELECT symbol FROM requested_symbols)"
        if normalized is not None
        else ""
    )
    params: list[Any] = [normalized] if normalized is not None else []
    query = f"""
        WITH {symbol_scope_cte}
        latest_history_snapshots AS MATERIALIZED (
            SELECT DISTINCT ON (snapshot.history_symbol)
                   snapshot.id, snapshot.history_symbol, snapshot.ingest_run_id,
                   snapshot.slot_at, snapshot.observed_at
            FROM raw.option_snapshot snapshot
            WHERE snapshot.history_symbol <> ''
              AND snapshot.collection_profile IN ('history_full', 'event_strip')
              AND snapshot.capture_state = 'complete'
              {history_filter_sql}
            ORDER BY snapshot.history_symbol, snapshot.slot_at DESC NULLS LAST,
                     snapshot.observed_at DESC, snapshot.id DESC
        ), latest_radar_snapshot AS MATERIALIZED (
            SELECT snapshot.id, NULL::text AS history_symbol,
                   snapshot.ingest_run_id, snapshot.slot_at, snapshot.observed_at
            FROM raw.option_snapshot snapshot
            WHERE snapshot.collection_profile = 'radar'
              AND snapshot.capture_state = 'complete'
            ORDER BY snapshot.observed_at DESC, snapshot.id DESC
            LIMIT 1
        ), latest_snapshots AS MATERIALIZED (
            SELECT id, history_symbol, ingest_run_id, slot_at, observed_at
            FROM latest_history_snapshots
            UNION ALL
            SELECT id, history_symbol, ingest_run_id, slot_at, observed_at
            FROM latest_radar_snapshot
        ), ranked_quotes AS MATERIALIZED (
            SELECT instrument.id AS instrument_id, instrument.symbol,
                   quote.observed_at AS as_of,
                   ingest_run.finished_at AS available_at,
                   (quote.ask - quote.bid) / NULLIF(quote.mid, 0)
                       AS spread_pct,
                   COALESCE(quote.open_interest, 0) AS open_interest,
                   COALESCE(quote.volume, 0) AS option_volume,
                   DENSE_RANK() OVER (
                       PARTITION BY instrument.id
                       ORDER BY COALESCE(snapshot.slot_at, snapshot.observed_at)
                                    DESC NULLS LAST,
                                snapshot.observed_at DESC, snapshot.id DESC
                   ) AS snapshot_rank
            FROM latest_snapshots snapshot
            JOIN ingest.run ingest_run ON ingest_run.id = snapshot.ingest_run_id
              AND ingest_run.finished_at IS NOT NULL
            JOIN raw.option_quote quote ON quote.snapshot_id = snapshot.id
            JOIN catalog.option_contract contract ON contract.id = quote.contract_id
            JOIN catalog.instrument instrument
              ON instrument.id = contract.underlying_instrument_id
            {instrument_filter_sql}
        )
        SELECT symbol,
               max(as_of) AS as_of,
               max(available_at) AS available_at,
               avg(spread_pct) AS average_option_spread_pct,
               sum(open_interest) AS total_open_interest,
               sum(option_volume) AS total_option_volume,
               count(*) AS contracts
        FROM ranked_quotes
        WHERE snapshot_rank = 1
        GROUP BY symbol
        ORDER BY symbol
    """
    if limit:
        query += " LIMIT %s"
        params.append(limit)
    result = connection.execute(query, params)
    return [dict(row) for row in result.fetchall()]


def _options_payoff_scenario_rows(
    connection: Any,
    *,
    symbols: set[str] | None,
    limit: int | None,
) -> list[dict[str, Any]]:
    normalized = _normalized_symbols(symbols)
    if normalized == []:
        return []
    filter_sql = "WHERE instrument.symbol = ANY(%s)" if normalized is not None else ""
    params: list[Any] = [normalized] if normalized is not None else []
    query = f"""
        WITH candidate_instruments AS MATERIALIZED (
            SELECT instrument.id, instrument.symbol
            FROM catalog.instrument instrument
            {filter_sql}
        ), candidate_decisions AS MATERIALIZED (
            SELECT decision.id, decision.run_id, decision.as_of,
                   decision.rank, decision.instrument_id
            FROM candidate_instruments instrument
            JOIN analysis.decision decision ON decision.instrument_id = instrument.id
            WHERE EXISTS (
                SELECT 1
                FROM analysis.option_decision option_decision
                WHERE option_decision.decision_id = decision.id
            )
            ORDER BY decision.as_of DESC, decision.rank, decision.id DESC
        )
        SELECT decision.id::text AS candidate_event_id, instrument.symbol AS ticker,
               contract.id::text AS contract_id, contract.expiration, contract.strike, contract.option_type,
               option_quote.bid, option_quote.ask, option_quote.bid_size, option_quote.ask_size,
               option_quote.observed_at AS quote_observed_at,
               option_decision.premium_mid, option_decision.entry_price,
               option_decision.buy_under, option_decision.structure,
               option_decision.synthetic_legs AS legs, option_decision.max_loss,
               option_decision.expected_value, option_decision.probability_profit,
               option_decision.details,
               feature.required_2x_price, feature.required_5x_price,
               feature.required_10x_price, feature.required_move_pct,
               COALESCE(option_quote.available_at, decision.as_of) AS available_at,
               option_quote.id::text AS source_version, option_quote.id::text AS revision
        FROM candidate_decisions decision
        JOIN candidate_instruments instrument ON instrument.id = decision.instrument_id
        JOIN analysis.option_decision option_decision ON option_decision.decision_id = decision.id
        JOIN LATERAL (
            SELECT feature.required_2x_price, feature.required_5x_price,
                   feature.required_10x_price, feature.required_move_pct
            FROM analysis.option_feature feature
            WHERE feature.run_id = decision.run_id
              AND feature.snapshot_id = option_decision.snapshot_id
              AND feature.contract_id = option_decision.contract_id
            ORDER BY feature.id DESC
            LIMIT 1
        ) feature ON true
        JOIN catalog.option_contract contract ON contract.id = option_decision.contract_id
        JOIN raw.option_quote option_quote
          ON option_quote.snapshot_id = option_decision.snapshot_id
         AND option_quote.contract_id = option_decision.contract_id
         AND option_quote.observed_at = option_decision.quote_observed_at
        ORDER BY decision.as_of DESC, decision.rank
    """
    if limit:
        query += " LIMIT %s"
        params.append(limit)
    result = connection.execute(query, params)
    return [dict(row) for row in result.fetchall()]


def _options_expiry_rows(
    connection: Any,
    *,
    symbols: set[str] | None,
    limit: int | None,
) -> list[dict[str, Any]]:
    normalized = _normalized_symbols(symbols)
    if normalized == []:
        return []
    filter_sql = "WHERE instrument.symbol = ANY(%s)" if normalized is not None else ""
    params: list[Any] = [normalized] if normalized is not None else []
    query = f"""
        WITH latest_symbol_snapshot AS (
            SELECT DISTINCT ON (instrument.id)
                   instrument.id AS instrument_id, snapshot.id AS snapshot_id
            FROM raw.option_quote quote
            JOIN raw.option_snapshot snapshot ON snapshot.id = quote.snapshot_id
            JOIN catalog.option_contract contract ON contract.id = quote.contract_id
            JOIN catalog.instrument instrument ON instrument.id = contract.underlying_instrument_id
            {filter_sql}
            ORDER BY instrument.id, snapshot.observed_at DESC,
                     CASE snapshot.source_id WHEN 'robinhood' THEN 0 WHEN 'ibkr' THEN 1 ELSE 2 END,
                     snapshot.id DESC
        )
        SELECT instrument.symbol, contract.expiration AS expiry,
               max(quote.observed_at) AS observed_at, snapshot.source_id AS source
        FROM raw.option_quote quote
        JOIN raw.option_snapshot snapshot ON snapshot.id = quote.snapshot_id
        JOIN catalog.option_contract contract ON contract.id = quote.contract_id
        JOIN catalog.instrument instrument ON instrument.id = contract.underlying_instrument_id
        JOIN latest_symbol_snapshot latest
          ON latest.snapshot_id = snapshot.id AND latest.instrument_id = instrument.id
        GROUP BY instrument.symbol, contract.expiration, snapshot.source_id
        ORDER BY instrument.symbol, contract.expiration
    """
    if limit:
        query += " LIMIT %s"
        params.append(limit)
    result = connection.execute(query, params)
    return [dict(row) for row in result.fetchall()]
