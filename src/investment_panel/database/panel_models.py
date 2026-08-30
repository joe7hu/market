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

__all__ = ["load_postgres_tables"]
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
        SELECT instrument.symbol,
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
    tables = (
        _published_tables(runtime, requested, row_limits=query_row_limits)
        if query_row_limits
        else _published_tables(runtime, requested)
    )
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
        live_tables = portfolio_intelligence_tables(config, **intelligence_options)
        for name in requested_intelligence:
            tables[name] = live_tables.get(name, [])
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
    with runtime.read(runtime_profile) as connection:
        for name in requested:
            if name in tables:
                continue
            if name == "ticker_policy_learning":
                canonical = AnalysisRepository(runtime).publication_rows(
                    "ticker-outcome-attribution", "outcome_attribution", include_lineage=True,
                )
                governance_rows = connection.execute(
                    """
                    SELECT evaluation_type AS stage, verdict, metrics, evidence,
                           evaluated_at, available_at, period_start, period_end
                    FROM analysis.strategy_evaluation evaluation
                    JOIN analysis.strategy_revision strategy
                      ON strategy.id = evaluation.strategy_revision_id
                    WHERE strategy.status = 'active'
                    ORDER BY evaluation.evaluated_at DESC, evaluation.id DESC
                    """
                ).fetchall()
                tables[name] = [{
                    "authority": "outcome-attribution.v1",
                    "episodes": canonical,
                    "governance_evaluations": [dict(row) for row in governance_rows],
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
                        query_cache[cache_key] = rows[:limit] if limit else rows
                    elif policy.custom_loader == "current_quotes":
                        query_cache[cache_key] = current_quote_rows(
                            connection,
                            symbols=query_symbol_filter if symbol_scoped else None,
                            limit=limit,
                        )
                    elif policy.custom_loader == "technicals":
                        rows = technical_rows(
                            connection,
                            symbols=query_symbol_filter if symbol_scoped else None,
                        )
                        query_cache[cache_key] = rows[:limit] if limit else rows
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
                        bounded_query = f"SELECT * FROM ({selected_query}) AS daily_research_rows"
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
    metadata = {
        "database": "postgresql",
        "schema_revision": HEAD_REVISION,
        "loaded_at": datetime.now(UTC).isoformat(),
        "table_count": len(requested),
        "unavailable_models": unavailable,
        "retired_models": [],
        "available_model_count": len(requested) - len(unavailable),
    }
    return tables, metadata


def _normalized_symbols(symbols: set[str] | None) -> list[str] | None:
    if symbols is None:
        return None
    return sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})


def _liquidity_rows(
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
        WITH latest AS (
            SELECT instrument.id AS instrument_id, max(snapshot.observed_at) AS observed_at
            FROM raw.option_quote quote
            JOIN raw.option_snapshot snapshot ON snapshot.id = quote.snapshot_id
            JOIN catalog.option_contract contract ON contract.id = quote.contract_id
            JOIN catalog.instrument instrument ON instrument.id = contract.underlying_instrument_id
            {filter_sql}
            GROUP BY instrument.id
        )
        SELECT instrument.symbol,
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
        JOIN latest ON latest.instrument_id = instrument.id
                    AND latest.observed_at = snapshot.observed_at
        GROUP BY instrument.symbol
        ORDER BY instrument.symbol
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
        {filter_sql}
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
