"""PostgreSQL read-model loader for API panel surfaces."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Iterable

from app.data_access.postgres_queries import OWNED_CORRELATIONS_QUERY
from app.data_access.postgres_source_queries import SOURCE_QUERIES
from app.data_access.user_state import portfolio_rows, thesis_monitor_rows, thesis_rows, watchlist_rows
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.jobs import JobRepository
from investment_panel.database.brokers import broker_status_rows
from investment_panel.database.agents import AgentRepository
from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.migrations import HEAD_REVISION


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
    "discovered_universe": """
        SELECT instrument.id AS instrument_id, instrument.symbol, instrument.name,
               instrument.asset_class, instrument.category,
               watchlist.watch_state, watchlist.notes,
               (position.instrument_id IS NOT NULL) AS is_owned
        FROM catalog.instrument instrument
        LEFT JOIN app.watchlist_item watchlist ON watchlist.instrument_id = instrument.id
        LEFT JOIN app.portfolio_position position ON position.instrument_id = instrument.id
        WHERE watchlist.instrument_id IS NOT NULL OR position.instrument_id IS NOT NULL
        ORDER BY (position.instrument_id IS NOT NULL) DESC, instrument.symbol
    """,
    "universe_screen": """
        SELECT instrument.symbol, instrument.name, instrument.asset_class, instrument.category,
               quote.price, quote.observed_at, watchlist.watch_state,
               CASE WHEN position.instrument_id IS NOT NULL THEN 'owned' ELSE 'watchlist' END AS universe_source,
               COALESCE(option_summary.actionable_count, 0) AS option_opportunities
        FROM catalog.instrument instrument
        LEFT JOIN app.watchlist_item watchlist ON watchlist.instrument_id = instrument.id
        LEFT JOIN app.portfolio_position position ON position.instrument_id = instrument.id
        LEFT JOIN LATERAL (
            SELECT price, observed_at FROM raw.quote
            WHERE instrument_id = instrument.id ORDER BY observed_at DESC LIMIT 1
        ) quote ON true
        LEFT JOIN LATERAL (
            SELECT count(*) AS actionable_count FROM analysis.decision
            WHERE instrument_id = instrument.id AND kind = 'option' AND state <> 'REJECT'
        ) option_summary ON true
        WHERE watchlist.instrument_id IS NOT NULL OR position.instrument_id IS NOT NULL
        ORDER BY (position.instrument_id IS NOT NULL) DESC, instrument.symbol
    """,
    "technicals": """
        WITH ranked AS (
            SELECT instrument.symbol, bar.observed_at, bar.close, bar.volume,
                   row_number() OVER (PARTITION BY instrument.id ORDER BY bar.observed_at DESC) AS rn
            FROM raw.price_bar bar JOIN catalog.instrument instrument ON instrument.id = bar.instrument_id
            WHERE bar.interval = '1d'
        )
        SELECT symbol, max(observed_at) AS as_of,
               max(close) FILTER (WHERE rn = 1) AS price,
               avg(close) FILTER (WHERE rn <= 20) AS sma_20,
               avg(close) FILTER (WHERE rn <= 50) AS sma_50,
               avg(close) FILTER (WHERE rn <= 200) AS sma_200,
               avg(volume) FILTER (WHERE rn <= 20) AS average_volume_20d,
               CASE WHEN avg(close) FILTER (WHERE rn <= 50) > 0
                    THEN max(close) FILTER (WHERE rn = 1) / (avg(close) FILTER (WHERE rn <= 50)) - 1 END AS distance_from_sma_50
        FROM ranked WHERE rn <= 200 GROUP BY symbol ORDER BY symbol
    """,
    "valuations": """
        SELECT DISTINCT ON (instrument.id, observation.metric_set)
               instrument.symbol, observation.metric_set, observation.period_end,
               observation.observed_at, observation.values, observation.source_id AS source
        FROM raw.fundamental_observation observation
        JOIN catalog.instrument instrument ON instrument.id = observation.instrument_id
        ORDER BY instrument.id, observation.metric_set, observation.observed_at DESC
    """,
    "liquidity": """
        SELECT instrument.symbol,
               max(quote.observed_at) AS as_of,
               avg((quote.ask - quote.bid) / NULLIF(quote.mid, 0)) AS average_option_spread_pct,
               sum(COALESCE(quote.open_interest, 0)) AS total_open_interest,
               sum(COALESCE(quote.volume, 0)) AS total_option_volume,
               count(*) AS contracts
        FROM raw.option_quote quote
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
               event.importance, event.verification_status, event.source_url, event.details
        FROM raw.market_event event
        LEFT JOIN catalog.instrument instrument ON instrument.id = event.instrument_id
        WHERE event.event_kind = 'earnings' ORDER BY event.starts_at
    """,
    "analyst_estimates": """
        SELECT instrument.symbol, observation.period_end, observation.observed_at,
               observation.values, observation.source_id AS source
        FROM raw.fundamental_observation observation
        JOIN catalog.instrument instrument ON instrument.id = observation.instrument_id
        WHERE observation.metric_set IN ('analyst_estimates', 'consensus')
        ORDER BY observation.observed_at DESC
    """,
    "research_packets": """
        SELECT instrument.symbol, item.id::text AS packet_id, item.observed_at AS generated_at,
               item.title, item.summary, item.url AS source_url, item.source_id AS source,
               item.metadata
        FROM raw.content_item_instrument link
        JOIN raw.content_item item ON item.id = link.content_item_id
        JOIN catalog.instrument instrument ON instrument.id = link.instrument_id
        ORDER BY item.observed_at DESC LIMIT 500
    """,
    "source_freshness": """
        SELECT source.id AS source_id, source.name AS source_name,
               source.family AS source_family, source.kind AS source_kind,
               run.status, run.finished_at AS refreshed_at, run.failure_detail,
               run.item_count, run.instrument_count AS ticker_count,
               CASE WHEN run.finished_at IS NULL THEN 'missing'
                    WHEN run.finished_at < now() - interval '2 days' THEN 'stale'
                    ELSE 'fresh' END AS freshness_status
        FROM ingest.source source
        LEFT JOIN LATERAL (
            SELECT status, finished_at, failure_detail, item_count, instrument_count
            FROM ingest.run WHERE source_id = source.id ORDER BY started_at DESC LIMIT 1
        ) run ON true ORDER BY source.family, source.id
    """,
    "ownership_consensus": """
        SELECT disclosure.trader_name, disclosure.filer_name, disclosure.event_date,
               disclosure.filed_date, holding->>'symbol' AS symbol,
               holding->>'name' AS issuer, (holding->>'value_thousands')::bigint AS value_thousands,
               disclosure.source_url, disclosure.details->>'accession_number' AS accession_number
        FROM raw.disclosure disclosure
        CROSS JOIN LATERAL jsonb_array_elements(COALESCE(disclosure.details->'holdings', '[]'::jsonb)) holding
        WHERE disclosure.source_type = '13f' AND holding->>'symbol' IS NOT NULL
        ORDER BY disclosure.event_date DESC, value_thousands DESC
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
               contract.expiration, contract.strike, contract.option_type,
               option_decision.premium_mid, option_decision.buy_under,
               feature.required_2x_price, feature.required_5x_price,
               feature.required_10x_price, feature.required_move_pct
        FROM analysis.option_decision option_decision
        JOIN analysis.decision decision ON decision.id = option_decision.decision_id
        JOIN analysis.option_feature feature
          ON feature.run_id = decision.run_id AND feature.contract_id = option_decision.contract_id
        JOIN catalog.option_contract contract ON contract.id = option_decision.contract_id
        JOIN catalog.instrument instrument ON instrument.id = decision.instrument_id
        ORDER BY decision.as_of DESC, decision.rank
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
        WHERE evaluation.evaluation_type = 'backtest' ORDER BY evaluation.evaluated_at DESC
    """,
    "strategy_forward_test_result": """
        SELECT evaluation.id::text, strategy.strategy_key AS strategy_version,
               evaluation.evaluated_at, evaluation.period_start, evaluation.period_end,
               evaluation.verdict, evaluation.metrics, evaluation.evidence AS raw
        FROM analysis.strategy_evaluation evaluation
        JOIN analysis.strategy_revision strategy ON strategy.id = evaluation.strategy_revision_id
        WHERE evaluation.evaluation_type IN ('forward_test', 'forward_shadow_test', 'shadow')
        ORDER BY evaluation.evaluated_at DESC
    """,
    "quotes": """
        SELECT DISTINCT ON (instrument.id) instrument.symbol, quote.observed_at,
               quote.price, quote.change_pct, quote.change_abs, quote.currency,
               quote.source_id AS source
        FROM raw.quote quote JOIN catalog.instrument instrument ON instrument.id = quote.instrument_id
        ORDER BY instrument.id, quote.observed_at DESC
    """,
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
        SELECT instrument.symbol, observation.period_end, observation.filed_at,
               observation.observed_at, observation.metric_set, observation.values,
               observation.source_id AS source
        FROM raw.fundamental_observation observation
        JOIN catalog.instrument instrument ON instrument.id = observation.instrument_id
        ORDER BY observation.observed_at DESC
    """,
    "catalysts": """
        SELECT catalyst.id::text, instrument.symbol, catalyst.starts_at, catalyst.title AS event,
               catalyst.expected_impact, catalyst.notes
        FROM app.catalyst catalyst
        LEFT JOIN catalog.instrument instrument ON instrument.id = catalyst.instrument_id
        ORDER BY catalyst.starts_at
    """,
    "disclosures": """
        SELECT disclosure.id::text, instrument.symbol, disclosure.source_type,
               disclosure.trader_name, disclosure.filer_name, disclosure.event_date,
               disclosure.filed_date, disclosure.action, disclosure.amount_text,
               disclosure.source_url, disclosure.details, disclosure.source_id AS source
        FROM raw.disclosure disclosure
        LEFT JOIN catalog.instrument instrument ON instrument.id = disclosure.instrument_id
        ORDER BY COALESCE(disclosure.event_date, disclosure.filed_date) DESC
    """,
    "news": """
        SELECT item.id::text, item.title, item.url, item.author, item.published_at,
               item.observed_at, item.summary, item.source_id AS source, item.metadata
        FROM raw.content_item item WHERE item.kind IN ('news', 'article', 'blog', 'social')
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
               strategy.revision AS version, strategy.created_at, strategy.status,
               strategy.parameters, strategy.promoted_at, strategy.supersedes_id
        FROM analysis.strategy_revision strategy ORDER BY strategy.strategy_key, strategy.revision DESC
    """,
    "broker_accounts": """
        SELECT snapshot.id::text, snapshot.source_id AS provider, snapshot.account_key AS account_id,
               snapshot.observed_at AS updated_at, snapshot.currency, snapshot.net_liquidation,
               snapshot.buying_power, snapshot.cash_balance, snapshot.details
        FROM raw.broker_account_snapshot snapshot ORDER BY snapshot.observed_at DESC
    """,
    "broker_positions": """
        SELECT account.source_id AS provider, account.account_key AS account_id,
               instrument.symbol, instrument.asset_class, position.quantity,
               position.average_cost, position.market_price, position.market_value,
               position.unrealized_pnl, account.observed_at AS updated_at, position.details
        FROM raw.broker_position_snapshot position
        JOIN raw.broker_account_snapshot account ON account.id = position.account_snapshot_id
        JOIN catalog.instrument instrument ON instrument.id = position.instrument_id
        ORDER BY account.observed_at DESC, instrument.symbol
    """,
    "paper_orders": """
        SELECT orders.id::text, decision.decision_key AS recommendation_id,
               instrument.symbol, orders.created_at, orders.side, orders.quantity,
               orders.limit_price, orders.status, orders.policy_result
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
        ORDER BY outcome.observed_through DESC
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
        ORDER BY outcome.observed_through DESC
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
        SELECT strategy.strategy_key AS strategy_version, decision.state,
               count(*) AS n, count(*) FILTER (WHERE outcome.maturity_state <> 'observing') AS mature_n,
               avg((outcome.time_to_2x_days IS NOT NULL)::integer) AS realized_p2x,
               avg((outcome.time_to_5x_days IS NOT NULL)::integer) AS realized_p5x,
               avg(outcome.peak_return) AS average_peak_return,
               min(decision.as_of) AS period_start, max(outcome.observed_through) AS period_end
        FROM analysis.option_outcome outcome
        JOIN analysis.decision decision ON decision.id = outcome.decision_id
        JOIN analysis.run run ON run.id = decision.run_id
        LEFT JOIN analysis.strategy_revision strategy ON strategy.id = decision.strategy_revision_id
        WHERE run.feature_versions->>'option' = 'option-professional-v2'
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
        ORDER BY task.created_at DESC
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
          AND run.feature_versions->>'option' = 'option-professional-v2'
        ORDER BY outcome.peak_return DESC
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
DIRECT_QUERIES.update(SOURCE_QUERIES)


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

RETIRED_EMPTY_MODELS = {
    "etf_premiums",
    "tradingview_symbol_search",
    "tradingview_watchlists",
    "tradingview_alerts",
    "tradingview_chart_state",
}

WATCHLIST_COMPAT_MODELS = {
    f"watchlist_{state}{suffix}"
    for state in ("watched", "unwatched")
    for suffix in (
        "", "_decision_queue", "_fundamentals", "_memos", "_options", "_portfolio",
        "_quotes", "_research_packets", "_screener", "_technicals", "_thesis_monitor", "_valuations",
    )
}

AGENT_MODELS = {
    "agent_thesis_request", "agent_thesis", "agent_thesis_validation",
    "agent_postmortem_request", "agent_postmortem",
}

PUBLICATION_MODELS = {
    "option_snapshot", "option_features", "option_radar_opportunity",
    "candidate_event", "option_radar_summary", "option_radar_symbol_summary",
    "option_action_queue", "option_calibration", "preopen_daily_brief",
    "daily_brief", "portfolio_risk_cards", "review_actions",
    "decision_queue", "decision_readiness", "symbol_decision_snapshots",
    "opportunities_ranked", "candidates", "feed_signals",
    "market_environment_assets", "market_environment_model",
    "market_valuation_reference_charts",
    "agent_recommendations",
}

SPECIAL_MODELS = {
    "portfolio", "manual_watchlist", "theses", "thesis_monitor",
    "refresh_jobs", "broker_status",
}


def load_postgres_tables(config: dict[str, Any], table_names: Iterable[str]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    requested = tuple(dict.fromkeys(table_names))
    runtime = runtime_for_config(config)
    tables = _published_tables(runtime, requested)
    if "portfolio" in requested:
        tables["portfolio"] = portfolio_rows(config)
    if "manual_watchlist" in requested:
        tables["manual_watchlist"] = watchlist_rows(config)
    if "theses" in requested:
        tables["theses"] = thesis_rows(config)
    if "thesis_monitor" in requested:
        tables["thesis_monitor"] = thesis_monitor_rows(config)
    if "refresh_jobs" in requested:
        tables["refresh_jobs"] = JobRepository(runtime).rows()
    if "broker_status" in requested:
        tables["broker_status"] = broker_status_rows(runtime)
    for name in AGENT_MODELS.intersection(requested):
        tables[name] = AgentRepository(runtime).rows(name)
    query_cache: dict[str, list[dict[str, Any]]] = {}
    with runtime.read() as connection:
        for name in requested:
            if name in tables:
                continue
            alias = MODEL_ALIASES.get(name)
            query = DIRECT_QUERIES.get(alias or name)
            if query:
                cache_key = alias or name
                if cache_key not in query_cache:
                    query_cache[cache_key] = [dict(row) for row in connection.execute(query).fetchall()]
                tables[name] = query_cache[cache_key]
            elif alias in PUBLICATION_MODELS:
                tables[name] = AnalysisRepository(runtime).publication_rows("today", alias)
            elif name in PUBLICATION_MODELS:
                tables[name] = []
            else:
                tables[name] = []
    supported = (
        set(DIRECT_QUERIES) | PUBLICATION_MODELS | SPECIAL_MODELS | AGENT_MODELS
        | set(MODEL_ALIASES) | RETIRED_EMPTY_MODELS | WATCHLIST_COMPAT_MODELS
    )
    unavailable = sorted(name for name in requested if name not in supported)
    retired = sorted(name for name in requested if name in RETIRED_EMPTY_MODELS or name in WATCHLIST_COMPAT_MODELS)
    metadata = {
        "database": "postgresql",
        "schema_revision": HEAD_REVISION,
        "loaded_at": datetime.now(UTC).isoformat(),
        "table_count": len(requested),
        "unavailable_models": unavailable,
        "retired_models": retired,
        "available_model_count": len(requested) - len(unavailable),
    }
    return tables, metadata


def _published_tables(runtime: Any, requested: tuple[str, ...]) -> dict[str, list[dict[str, Any]]]:
    if not requested:
        return {}
    with runtime.read() as connection:
        rows = connection.execute(
            """
            WITH latest AS (
                SELECT DISTINCT ON (item.model_name) item.model_name, publication.id
                FROM app.publication publication
                JOIN app.publication_item item ON item.publication_id = publication.id
                WHERE publication.status = 'published' AND item.model_name = ANY(%s)
                ORDER BY item.model_name, publication.published_at DESC
            )
            SELECT item.model_name, item.payload
            FROM latest
            JOIN app.publication_item item
              ON item.publication_id = latest.id AND item.model_name = latest.model_name
            ORDER BY item.model_name, item.rank
            """,
            [list(requested)],
        ).fetchall()
    output: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        output.setdefault(str(row["model_name"]), []).append(dict(row["payload"]))
    return output
