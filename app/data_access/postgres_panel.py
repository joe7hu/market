"""PostgreSQL read-model loader for API panel surfaces."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Iterable

from app.data_access.user_state import portfolio_rows, thesis_monitor_rows, thesis_rows, watchlist_rows
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.jobs import JobRepository
from investment_panel.database.brokers import broker_status_rows
from investment_panel.database.agents import AgentRepository
from investment_panel.database.migrations import HEAD_REVISION


DIRECT_QUERIES: dict[str, str] = {
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
    "sources": """
        SELECT source.id AS source_id, source.name, source.family, source.kind,
               source.origin, source.enabled, source.ingestion_mode, source.source_url,
               source.capabilities, source.config, source.updated_at
        FROM ingest.source source ORDER BY source.family, source.id
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
}


AGENT_MODELS = {
    "agent_thesis_request", "agent_thesis", "agent_thesis_validation",
    "agent_postmortem_request", "agent_postmortem",
}

PUBLICATION_MODELS = {
    "option_snapshot", "option_features", "option_radar_opportunity",
    "candidate_event", "option_radar_summary", "preopen_daily_brief",
    "daily_brief", "portfolio_risk_cards", "review_actions",
    "decision_queue", "decision_readiness", "symbol_decision_snapshots",
    "opportunities_ranked", "candidates", "feed_signals",
    "market_environment_assets", "market_environment_model",
    "market_valuation_reference_charts",
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
    with runtime.read() as connection:
        for name in requested:
            if name in tables:
                continue
            query = DIRECT_QUERIES.get(name)
            if query:
                tables[name] = [dict(row) for row in connection.execute(query).fetchall()]
            elif name in PUBLICATION_MODELS:
                tables[name] = []
            else:
                tables[name] = []
    supported = set(DIRECT_QUERIES) | PUBLICATION_MODELS | SPECIAL_MODELS | AGENT_MODELS
    unavailable = sorted(name for name in requested if name not in supported)
    metadata = {
        "database": "postgresql",
        "schema_revision": HEAD_REVISION,
        "loaded_at": datetime.now(UTC).isoformat(),
        "table_count": len(requested),
        "unavailable_models": unavailable,
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
