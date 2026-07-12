"""PostgreSQL read models for source evidence and source-derived signals."""

SOURCE_QUERIES: dict[str, str] = {
    "source_catalog": """
        SELECT source.id, source.name AS label, source.family,
               COALESCE(latest.capability, source.kind) AS cadence_label,
               0 AS cadence_seconds, '' AS refresh_job, '2 days' AS stale_after,
               ARRAY[source.kind] AS source_types, false AS live_fetcher,
               CASE WHEN latest.status = 'failed' THEN 'bad'
                    WHEN latest.finished_at IS NULL THEN 'neutral'
                    WHEN latest.finished_at < now() - interval '2 days' THEN 'warn'
                    ELSE 'good' END AS tone,
               jsonb_build_object(
                   'provider', source.name,
                   'status', COALESCE(latest.status, 'not_loaded'),
                   'tone', CASE WHEN latest.status = 'failed' THEN 'bad'
                                WHEN latest.finished_at IS NULL THEN 'neutral'
                                WHEN latest.finished_at < now() - interval '2 days' THEN 'warn'
                                ELSE 'good' END,
                   'provider_status', COALESCE(latest.status, 'not_loaded'),
                   'last_observed_at', latest.finished_at,
                   'stale_after', '2 days',
                   'symbol_count', COALESCE(latest.instrument_count, 0),
                   'rate_limited', false,
                   'freshness_status', CASE WHEN latest.finished_at IS NULL THEN 'not_loaded'
                                            WHEN latest.finished_at < now() - interval '2 days' THEN 'stale'
                                            ELSE 'fresh' END,
                   'detail', COALESCE(latest.failure_detail, source.origin, '')
               ) AS primary,
               '[]'::jsonb AS fallback
        FROM ingest.source source
        LEFT JOIN LATERAL (
            SELECT run.capability, run.status, run.finished_at,
                   run.instrument_count, run.failure_detail
            FROM ingest.run run WHERE run.source_id = source.id
            ORDER BY run.started_at DESC LIMIT 1
        ) latest ON true
        ORDER BY source.family, source.id
    """,
    "source_ticker_rankings": """
        SELECT instrument.symbol AS ticker, instrument.symbol,
               count(*) AS source_item_count, count(*) AS signal_count,
               count(DISTINCT item.source_id) AS source_count,
               count(*) FILTER (WHERE signal.sentiment = 'bullish') AS bullish_count,
               count(*) FILTER (WHERE signal.sentiment = 'bearish') AS bearish_count,
               count(*) FILTER (WHERE signal.sentiment = 'bullish')
                 - count(*) FILTER (WHERE signal.sentiment = 'bearish') AS net_consensus,
               avg(signal.confidence) AS avg_confidence,
               array_agg(DISTINCT source.name ORDER BY source.name) AS source_names,
               max(item.observed_at) AS latest_at, max(item.observed_at) AS latest_evidence_at
        FROM raw.content_item_instrument link
        JOIN raw.content_item item ON item.id = link.content_item_id
        JOIN catalog.instrument instrument ON instrument.id = link.instrument_id
        JOIN ingest.source source ON source.id = item.source_id
        LEFT JOIN LATERAL (
            SELECT sentiment, confidence FROM analysis.source_signal signal
            WHERE signal.content_item_id = item.id AND signal.instrument_id = instrument.id
            ORDER BY signal.observed_at DESC LIMIT 1
        ) signal ON true
        GROUP BY instrument.symbol ORDER BY source_count DESC, source_item_count DESC
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
        GROUP BY source.id, source.name, source.family
        ORDER BY items_count DESC, source.name
    """,
    "feed_signals": """
        SELECT 'content:' || item.id AS id, item.title,
               COALESCE(signal.thesis, item.summary, item.title) AS thesis,
               signal.antithesis, signal.invalidation,
               source.name AS source, source.family AS source_family,
               item.kind AS source_type, item.observed_at AS date,
               ARRAY[instrument.symbol] AS symbols,
               instrument.symbol AS primary_symbol,
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
            WHERE signal.content_item_id = item.id AND signal.instrument_id = instrument.id
            ORDER BY signal.observed_at DESC LIMIT 1
        ) signal ON true
        ORDER BY item.observed_at DESC, item.id DESC LIMIT 48
    """,
    "sources": """
        SELECT source.id AS source_id, source.name AS source_name,
               source.family AS source_family, source.kind AS source_kind,
               source.origin, source.enabled, source.ingestion_mode, source.source_url,
               source.capabilities, source.config, source.updated_at,
               COALESCE(content.items_count, 0) AS items_count,
               COALESCE(content.tickers_count, 0) AS tickers_count,
               COALESCE(content.signals_count, 0) AS signals_count,
               latest.status AS latest_run_status, latest.finished_at AS latest_run_at,
               CASE WHEN latest.finished_at IS NULL THEN 'not_loaded'
                    WHEN latest.finished_at < now() - interval '2 days' THEN 'stale'
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
    "ticker_source_signals": """
        SELECT instrument.symbol AS ticker, instrument.symbol, item.source_id,
               COALESCE(signal.signal_type, item.kind) AS signal_type,
               COALESCE(signal.observed_at, item.observed_at) AS observed_at,
               COALESCE(signal.thesis, item.title) AS thesis,
               signal.antithesis, signal.sentiment, signal.direction,
               signal.confidence, signal.invalidation,
               item.summary, item.url AS source_url, link.relevance,
               COALESCE(signal.details, '{}'::jsonb) ||
                 jsonb_build_object('content_item_id', item.id, 'license_status', item.license_status) AS raw
        FROM raw.content_item_instrument link
        JOIN raw.content_item item ON item.id = link.content_item_id
        JOIN catalog.instrument instrument ON instrument.id = link.instrument_id
        LEFT JOIN LATERAL (
            SELECT signal.* FROM analysis.source_signal signal
            WHERE signal.content_item_id = item.id AND signal.instrument_id = instrument.id
            ORDER BY signal.observed_at DESC LIMIT 1
        ) signal ON true
        ORDER BY COALESCE(signal.observed_at, item.observed_at) DESC LIMIT 500
    """,
}
