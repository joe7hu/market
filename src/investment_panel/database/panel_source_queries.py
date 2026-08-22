"""PostgreSQL panel read models for source evidence and signals."""

from investment_panel.database.source_health import SOURCE_HEALTH_QUERY

CANONICAL_SYMBOL = "regexp_replace(upper(instrument.symbol), '[.]+$', '')"
SOURCE_ROOT = "CASE WHEN source.kind = 'news' THEN lower(source.name) ELSE source.id END"
SOURCE_DISPLAY_NAME = "CASE WHEN source.kind = 'news' THEN lower(source.name) ELSE source.name END"
FEED_EXCLUDED_KINDS = (
    "'analyst_estimate', 'crypto_fundamental', 'earnings_event', "
    "'equity_fundamental', 'market_screener', 'trader_portfolio_model'"
)

SOURCE_UNIVERSE_CTES = f"""
    WITH canonical_instruments AS (
        SELECT DISTINCT ON (canonical_symbol)
               instrument.id, canonical_symbol AS symbol, instrument.name,
               instrument.asset_class, instrument.category
        FROM (
            SELECT instrument.*,
                   regexp_replace(upper(instrument.symbol), '[.]+$', '') AS canonical_symbol
            FROM catalog.instrument instrument
        ) instrument
        WHERE canonical_symbol <> ''
        ORDER BY canonical_symbol,
                 (upper(instrument.symbol) = canonical_symbol) DESC,
                 instrument.updated_at DESC, instrument.id
    ), eligible_source_rows AS (
        SELECT {CANONICAL_SYMBOL} AS symbol, {SOURCE_ROOT} AS source_root,
               {SOURCE_DISPLAY_NAME} AS source_name,
               COALESCE(item.published_at, item.observed_at) AS evidence_at
        FROM raw.content_item_instrument link
        JOIN raw.content_item item ON item.id = link.content_item_id
        JOIN catalog.instrument instrument ON instrument.id = link.instrument_id
        JOIN ingest.source source ON source.id = item.source_id
        WHERE source.enabled AND source.operational_state = 'active'
          AND item.kind NOT IN ({FEED_EXCLUDED_KINDS})
          AND item.observed_at <= now()
          AND COALESCE(item.published_at, item.observed_at) <= now()
          AND COALESCE(item.published_at, item.observed_at) >= now() - interval '45 days'
        UNION ALL
        SELECT {CANONICAL_SYMBOL} AS symbol, {SOURCE_ROOT} AS source_root,
               {SOURCE_DISPLAY_NAME} AS source_name,
               COALESCE(disclosure.filed_date, disclosure.event_date)::timestamptz AS evidence_at
        FROM raw.disclosure disclosure
        JOIN catalog.instrument instrument ON instrument.id = disclosure.instrument_id
        JOIN ingest.source source ON source.id = disclosure.source_id
        WHERE source.enabled AND source.operational_state = 'active'
          AND COALESCE(disclosure.filed_date, disclosure.event_date) <= current_date
          AND COALESCE(disclosure.filed_date, disclosure.event_date) >= current_date - 180
        UNION ALL
        SELECT {CANONICAL_SYMBOL} AS symbol, {SOURCE_ROOT} AS source_root,
               {SOURCE_DISPLAY_NAME} AS source_name, event.starts_at AS evidence_at
        FROM raw.market_event event
        JOIN catalog.instrument instrument ON instrument.id = event.instrument_id
        JOIN ingest.source source ON source.id = event.source_id
        WHERE source.enabled AND source.operational_state = 'active'
          AND event.starts_at >= now() AND event.starts_at < now() + interval '90 days'
    ), source_counts_by_root AS (
        SELECT symbol, source_root, max(source_name) AS source_name,
               count(*) AS item_count, max(evidence_at) AS latest_at
        FROM eligible_source_rows
        WHERE symbol <> ''
        GROUP BY symbol, source_root
    ), source_evidence AS (
        SELECT symbol, jsonb_object_agg(source_root, item_count) AS source_counts,
               array_agg(source_name ORDER BY source_name) AS source_names,
               count(*)::int AS source_count, sum(item_count)::int AS source_item_count,
               max(latest_at) AS latest_source_timestamp
        FROM source_counts_by_root
        GROUP BY symbol
    )
"""

SOURCE_UNIVERSE_QUERIES = {
    "discovered_universe": SOURCE_UNIVERSE_CTES + """
        SELECT instrument.id AS instrument_id, instrument.symbol, instrument.name,
               instrument.asset_class, instrument.category,
               CASE WHEN position.instrument_id IS NOT NULL THEN 'owned'
                    WHEN watchlist.watch_state IS NOT NULL THEN watchlist.watch_state
                    ELSE 'candidate' END AS watch_state,
               watchlist.notes, (position.instrument_id IS NOT NULL) AS is_owned,
               COALESCE(evidence.source_counts, '{}'::jsonb) AS source_counts,
               COALESCE(evidence.source_names, ARRAY[]::text[]) AS source_names,
               COALESCE(evidence.source_count, 0) AS source_count,
               COALESCE(evidence.source_item_count, 0) AS source_item_count,
               evidence.latest_source_timestamp,
               evidence.latest_source_timestamp AS latest_observed_at,
               CASE WHEN position.instrument_id IS NOT NULL THEN ARRAY['portfolio']::text[]
                    WHEN watchlist.watch_state IS NOT NULL THEN ARRAY['manual watchlist']::text[]
                    ELSE ARRAY['source evidence']::text[] END AS inclusion_reasons,
               CASE WHEN position.instrument_id IS NOT NULL OR watchlist.watch_state IS NOT NULL
                    THEN 'eligible' WHEN COALESCE(evidence.source_count, 0) >= 2
                    THEN 'eligible' ELSE 'source_thin' END AS eligibility_status,
               COALESCE(evidence.source_count, 0)::double precision AS evidence_score,
               COALESCE(evidence.source_item_count, 0)::double precision AS discovery_score,
               (position.instrument_id IS NOT NULL OR watchlist.watch_state IS NOT NULL
                   OR COALESCE(evidence.source_count, 0) >= 2)
                   AS decision_universe_member,
               row_number() OVER (
                   ORDER BY (position.instrument_id IS NOT NULL) DESC,
                            (watchlist.watch_state IS NOT NULL) DESC,
                            COALESCE(evidence.source_count, 0) DESC,
                            COALESCE(evidence.source_item_count, 0) DESC,
                            evidence.latest_source_timestamp DESC NULLS LAST,
                            instrument.symbol
               ) AS universe_rank
        FROM canonical_instruments instrument
        LEFT JOIN app.watchlist_item watchlist ON watchlist.instrument_id = instrument.id
        LEFT JOIN app.portfolio_position position ON position.instrument_id = instrument.id
        LEFT JOIN source_evidence evidence ON evidence.symbol = instrument.symbol
        WHERE (watchlist.instrument_id IS NOT NULL OR position.instrument_id IS NOT NULL
               OR evidence.symbol IS NOT NULL)
          AND (position.instrument_id IS NOT NULL OR watchlist.watch_state IS DISTINCT FROM 'excluded')
        ORDER BY universe_rank
    """,
    "universe_screen": SOURCE_UNIVERSE_CTES + """
        SELECT instrument.symbol, instrument.name, instrument.asset_class, instrument.category,
               quote.price, quote.observed_at,
               CASE WHEN position.instrument_id IS NOT NULL THEN 'owned'
                    WHEN watchlist.watch_state IS NOT NULL THEN watchlist.watch_state
                    ELSE 'candidate' END AS watch_state,
               CASE WHEN position.instrument_id IS NOT NULL THEN 'owned'
                    WHEN watchlist.watch_state IS NOT NULL THEN 'watchlist'
                    ELSE 'source_evidence' END AS universe_source,
               COALESCE(evidence.source_counts, '{}'::jsonb) AS source_counts,
               COALESCE(evidence.source_names, ARRAY[]::text[]) AS source_names,
               COALESCE(evidence.source_count, 0) AS source_count,
               COALESCE(evidence.source_item_count, 0) AS source_item_count,
               evidence.latest_source_timestamp,
               COALESCE(option_summary.actionable_count, 0) AS option_opportunities,
               (market.values->>'market_cap')::double precision AS market_cap,
               (market.values->>'price_to_sales')::double precision AS ps_ratio,
               CASE WHEN (market.values->>'trailing_pe')::double precision > 0
                    THEN (market.values->>'trailing_pe')::double precision END AS pe_ratio,
               CASE WHEN (market.values->>'trailing_pe')::double precision > 0 THEN 'reported'
                    WHEN (market.values->>'profit_margin')::double precision <= 0 THEN 'not_meaningful'
                    ELSE 'missing' END AS pe_status,
               CASE WHEN (market.values->>'forward_pe')::double precision > 0
                    THEN (market.values->>'forward_pe')::double precision END AS forward_pe,
               CASE WHEN (market.values->>'forward_pe')::double precision > 0 THEN 'reported'
                    WHEN (market.values->>'forward_pe')::double precision <= 0 THEN 'not_meaningful'
                    ELSE 'missing' END AS forward_pe_status,
               COALESCE(
                   (market.values->>'revenue_growth')::double precision,
                   (sec.values->>'revenue_growth')::double precision
               ) AS revenue_growth_yoy,
               COALESCE(
                   (market.values->>'fcf_yield')::double precision,
                   CASE WHEN (market.values->>'market_cap')::double precision > 0
                        THEN (sec.values->>'free_cash_flow')::double precision
                             / (market.values->>'market_cap')::double precision END
               ) AS fcf_yield,
               COALESCE(
                   CASE WHEN (market.values->>'total_revenue')::double precision > 0
                        THEN (market.values->>'free_cash_flow')::double precision
                             / (market.values->>'total_revenue')::double precision END,
                   (sec.values->>'fcf_margin')::double precision
               ) AS fcf_margin,
               (market.values->>'return_on_invested_capital')::double precision * 100 AS roic
        FROM canonical_instruments instrument
        LEFT JOIN app.watchlist_item watchlist ON watchlist.instrument_id = instrument.id
        LEFT JOIN app.portfolio_position position ON position.instrument_id = instrument.id
        LEFT JOIN source_evidence evidence ON evidence.symbol = instrument.symbol
        LEFT JOIN LATERAL (
            SELECT price, observed_at FROM raw.quote
            WHERE instrument_id = instrument.id ORDER BY observed_at DESC LIMIT 1
        ) quote ON true
        LEFT JOIN LATERAL (
            SELECT count(*) AS actionable_count FROM analysis.decision
            WHERE instrument_id = instrument.id AND kind = 'option' AND state <> 'REJECT'
        ) option_summary ON true
        LEFT JOIN LATERAL (
            SELECT values FROM raw.fundamental_observation
            WHERE instrument_id = instrument.id AND metric_set = 'market_metrics'
            ORDER BY observed_at DESC LIMIT 1
        ) market ON true
        LEFT JOIN LATERAL (
            SELECT values FROM raw.fundamental_observation
            WHERE instrument_id = instrument.id AND metric_set = 'sec_fundamentals'
            ORDER BY observed_at DESC LIMIT 1
        ) sec ON true
        WHERE (watchlist.instrument_id IS NOT NULL OR position.instrument_id IS NOT NULL
               OR evidence.symbol IS NOT NULL)
          AND (position.instrument_id IS NOT NULL OR watchlist.watch_state IS DISTINCT FROM 'excluded')
        ORDER BY (position.instrument_id IS NOT NULL) DESC,
                 (watchlist.watch_state IS NOT NULL) DESC,
                 COALESCE(evidence.source_count, 0) DESC,
                 COALESCE(evidence.source_item_count, 0) DESC,
                 evidence.latest_source_timestamp DESC NULLS LAST, instrument.symbol
    """,
}

SOURCE_QUERIES: dict[str, str] = {
    "source_catalog": SOURCE_HEALTH_QUERY,
    "source_ticker_rankings": SOURCE_UNIVERSE_CTES + """
        SELECT evidence.symbol AS ticker, evidence.symbol,
               evidence.source_item_count, evidence.source_item_count AS signal_count,
               evidence.source_count,
               COALESCE(sentiment.bullish_count, 0) AS bullish_count,
               COALESCE(sentiment.bearish_count, 0) AS bearish_count,
               COALESCE(sentiment.bullish_count, 0)
                 - COALESCE(sentiment.bearish_count, 0) AS net_consensus,
               sentiment.avg_confidence,
               evidence.source_names, evidence.latest_source_timestamp AS latest_at,
               evidence.latest_source_timestamp AS latest_evidence_at,
               evidence.source_counts
        FROM source_evidence evidence
        LEFT JOIN LATERAL (
            SELECT count(*) FILTER (WHERE signal.sentiment = 'bullish') AS bullish_count,
                   count(*) FILTER (WHERE signal.sentiment = 'bearish') AS bearish_count,
                   avg(signal.confidence) AS avg_confidence
            FROM analysis.source_signal signal
            JOIN catalog.instrument instrument ON instrument.id = signal.instrument_id
            WHERE regexp_replace(upper(instrument.symbol), '[.]+$', '') = evidence.symbol
              AND signal.observed_at <= now()
        ) sentiment ON true
        ORDER BY evidence.source_count DESC, evidence.source_item_count DESC,
                 evidence.latest_source_timestamp DESC
    """,
    "source_consensus": """
        SELECT source.id AS source_id, source.name AS source_name,
               source.family AS content_type, count(DISTINCT item.id) AS items_count,
               count(DISTINCT instrument.id) AS tickers_count,
               count(*) FILTER (WHERE signal.sentiment = 'bullish')
                 - count(*) FILTER (WHERE signal.sentiment = 'bearish') AS net_consensus,
               array_agg(DISTINCT instrument.symbol)
                 FILTER (WHERE signal.sentiment = 'bullish') AS bullish_symbols,
               array_agg(DISTINCT instrument.symbol)
                 FILTER (WHERE signal.sentiment = 'bearish') AS bearish_symbols,
               max(item.observed_at) AS latest_at, 'loaded' AS recommendation
        FROM raw.content_item_instrument link
        JOIN raw.content_item item ON item.id = link.content_item_id
        JOIN catalog.instrument instrument ON instrument.id = link.instrument_id
        JOIN ingest.source source ON source.id = item.source_id
        LEFT JOIN LATERAL (
            SELECT sentiment FROM analysis.source_signal signal
            WHERE signal.content_item_id = item.id AND signal.instrument_id = instrument.id
            ORDER BY signal.observed_at DESC LIMIT 1
        ) signal ON true
        WHERE source.enabled AND source.operational_state = 'active'
          AND item.observed_at <= now()
          AND COALESCE(item.published_at, item.observed_at) <= now()
        GROUP BY source.id, source.name, source.family
        ORDER BY items_count DESC, source.name
    """,
    "feed_signals": f"""
        WITH content_events AS (
            SELECT 'content:' || item.id AS id, {SOURCE_ROOT} AS source_root,
                   item.title, COALESCE(signal.thesis, item.summary, item.title) AS thesis,
                   signal.antithesis, signal.invalidation,
                   {SOURCE_DISPLAY_NAME} AS source,
                   CASE WHEN source.family IN ('social', 'private_graph') THEN 'thesis'
                        WHEN source.family = 'estimates' THEN 'research'
                        ELSE source.family END AS source_family,
                   item.kind AS source_type,
                   COALESCE(item.published_at, item.observed_at) AS date,
                   array_agg(DISTINCT {CANONICAL_SYMBOL} ORDER BY {CANONICAL_SYMBOL}) AS symbols,
                   COALESCE(signal.sentiment, 'neutral') AS sentiment,
                   signal.direction, signal.confidence,
                   COALESCE(signal.details->'evidence_refs',
                            CASE WHEN item.url IS NULL THEN '[]'::jsonb
                                 ELSE jsonb_build_array(item.url) END) AS evidence_refs,
                   signal.details->'risks' AS risks, item.url AS source_url
            FROM raw.content_item_instrument link
            JOIN raw.content_item item ON item.id = link.content_item_id
            JOIN catalog.instrument instrument ON instrument.id = link.instrument_id
            JOIN ingest.source source ON source.id = item.source_id
            LEFT JOIN LATERAL (
                SELECT signal.thesis, signal.antithesis, signal.invalidation,
                       signal.sentiment, signal.direction, signal.confidence, signal.details
                FROM analysis.source_signal signal
                WHERE signal.content_item_id = item.id
                ORDER BY signal.observed_at DESC, signal.confidence DESC NULLS LAST LIMIT 1
            ) signal ON true
            WHERE source.enabled AND source.operational_state = 'active'
              AND item.kind NOT IN ({FEED_EXCLUDED_KINDS})
              AND item.observed_at <= now()
              AND COALESCE(item.published_at, item.observed_at) <= now()
            GROUP BY item.id, item.source_id, item.title, item.summary, item.url,
                     item.kind, item.published_at, item.observed_at,
                     source.id, source.name, source.family, source.kind,
                     signal.thesis, signal.antithesis,
                     signal.invalidation, signal.sentiment, signal.direction,
                     signal.confidence, signal.details
        ), disclosure_events AS (
            SELECT 'disclosure:' || disclosure.id AS id, {SOURCE_ROOT} AS source_root,
                   concat_ws(' ', COALESCE(disclosure.trader_name, disclosure.filer_name, source.name),
                             disclosure.action, {CANONICAL_SYMBOL}) AS title,
                   concat_ws(' ', disclosure.action, disclosure.amount_text, {CANONICAL_SYMBOL}) AS thesis,
                   NULL::text AS antithesis, NULL::text AS invalidation,
                   {SOURCE_DISPLAY_NAME} AS source, 'filing'::text AS source_family,
                   disclosure.source_type,
                   COALESCE(disclosure.filed_date, disclosure.event_date)::timestamptz AS date,
                   ARRAY[{CANONICAL_SYMBOL}] AS symbols, 'neutral'::text AS sentiment,
                   NULL::text AS direction, NULL::double precision AS confidence,
                   CASE WHEN disclosure.source_url IS NULL THEN '[]'::jsonb
                        ELSE jsonb_build_array(disclosure.source_url) END AS evidence_refs,
                   '[]'::jsonb AS risks, disclosure.source_url
            FROM raw.disclosure disclosure
            JOIN catalog.instrument instrument ON instrument.id = disclosure.instrument_id
            JOIN ingest.source source ON source.id = disclosure.source_id
            WHERE source.enabled AND source.operational_state = 'active'
              AND COALESCE(disclosure.filed_date, disclosure.event_date) <= current_date
        ), ranked AS (
            SELECT event.*,
                   row_number() OVER (PARTITION BY source_root ORDER BY date DESC, id DESC) AS source_rank
            FROM (SELECT * FROM content_events UNION ALL SELECT * FROM disclosure_events) event
        )
        SELECT id, title, thesis, antithesis, invalidation, source, source_family,
               source_type, date, symbols, symbols[1] AS primary_symbol,
               sentiment, direction, confidence, evidence_refs, risks, source_url
        FROM ranked
        WHERE source_rank <= 8
        ORDER BY date DESC, id DESC
        LIMIT 48
    """,
    "sources": """
        SELECT source.id AS source_id, source.name AS source_name,
               source.family AS source_family, source.kind AS source_kind,
               source.origin, source.enabled, source.ingestion_mode, source.source_url,
               source.operational_state, source.health_owner, source.freshness_seconds,
               source.capabilities, source.config, source.updated_at,
               COALESCE(content.items_count, 0) AS items_count,
               COALESCE(content.tickers_count, 0) AS tickers_count,
               COALESCE(content.signals_count, 0) AS signals_count,
               latest.status AS latest_run_status, latest.finished_at AS latest_run_at,
               CASE WHEN NOT source.enabled THEN 'disabled'
                    WHEN source.operational_state = 'archived' THEN 'archived'
                    WHEN source.operational_state = 'standby' THEN 'standby'
                    WHEN source.freshness_seconds IS NULL THEN 'uncontracted'
                    WHEN latest.finished_at IS NULL THEN 'not_loaded'
                    WHEN latest.finished_at < now() - make_interval(secs => source.freshness_seconds) THEN 'stale'
                    ELSE 'fresh' END AS freshness
        FROM ingest.source source
        LEFT JOIN LATERAL (
            SELECT count(DISTINCT item.id) AS items_count,
                   count(DISTINCT link.instrument_id) AS tickers_count,
                   count(DISTINCT signal.id) AS signals_count
            FROM raw.content_item item
            LEFT JOIN raw.content_item_instrument link ON link.content_item_id = item.id
            LEFT JOIN analysis.source_signal signal ON signal.content_item_id = item.id
            WHERE item.source_id = source.id
        ) content ON true
        LEFT JOIN LATERAL (
            SELECT run.status, run.finished_at FROM ingest.run run
            WHERE run.source_id = source.id ORDER BY run.started_at DESC LIMIT 1
        ) latest ON true
        ORDER BY source.family, source.id
    """,
    "ticker_source_signals": f"""
        WITH signal_rows AS (
            SELECT {CANONICAL_SYMBOL} AS ticker, {CANONICAL_SYMBOL} AS symbol,
                   item.source_id, {SOURCE_DISPLAY_NAME} AS source_name,
                   CASE WHEN source.family IN ('social', 'private_graph') THEN 'thesis'
                        ELSE source.family END AS source_family,
                   COALESCE(signal.signal_type, item.kind) AS signal_type,
                   COALESCE(signal.observed_at, item.observed_at) AS observed_at,
                   COALESCE(signal.thesis, item.title) AS thesis,
                   signal.antithesis, signal.sentiment, signal.direction,
                   signal.confidence, signal.invalidation,
                   item.summary, item.url AS source_url, link.relevance,
                   COALESCE(signal.details, '{{}}'::jsonb) ||
                     jsonb_build_object('content_item_id', item.id,
                                        'license_status', item.license_status) AS raw,
                   row_number() OVER (
                       PARTITION BY {CANONICAL_SYMBOL}, {SOURCE_ROOT}
                       ORDER BY COALESCE(signal.observed_at, item.observed_at) DESC, item.id DESC
                   ) AS source_rank
            FROM raw.content_item_instrument link
            JOIN raw.content_item item ON item.id = link.content_item_id
            JOIN catalog.instrument instrument ON instrument.id = link.instrument_id
            JOIN ingest.source source ON source.id = item.source_id
            LEFT JOIN LATERAL (
                SELECT signal.* FROM analysis.source_signal signal
                WHERE signal.content_item_id = item.id AND signal.instrument_id = instrument.id
                ORDER BY signal.observed_at DESC LIMIT 1
            ) signal ON true
            WHERE source.enabled AND source.operational_state = 'active'
              AND item.observed_at <= now()
              AND COALESCE(item.published_at, item.observed_at) <= now()
        )
        SELECT ticker, symbol, source_id, source_name, source_family, signal_type,
               observed_at, thesis, antithesis, sentiment, direction, confidence,
               invalidation, summary, source_url, relevance, raw
        FROM signal_rows
        WHERE source_rank <= 5
        ORDER BY observed_at DESC
        LIMIT 2000
    """,
}
