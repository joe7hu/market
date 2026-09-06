"""Current option panels and bounded transition predecessor reads."""
from __future__ import annotations

from typing import Any


def current_option_query(name: str, *, scoped: bool = False) -> str:
    # History already identifies its symbol. Only legacy radar snapshots need
    # quote membership. Split generation reads so their partial index can be used.
    # OFFSET 0 retains parameterized index probes instead of a quote-history join.
    # Ordered candidates stop at the first match; recorded membership never probes quotes.
    scope = "AND instrument.symbol = ANY(%s::text[])" if scoped else ""
    query = f"""
        WITH requested_instruments AS MATERIALIZED (
            SELECT instrument.id, instrument.symbol FROM catalog.instrument instrument
            WHERE EXISTS (SELECT 1 FROM catalog.option_contract owned
                          WHERE owned.underlying_instrument_id = instrument.id)
            {scope}
        ), eligible_snapshots AS MATERIALIZED (
            SELECT candidate.*, ingest_run.finished_at AS available_at,
                   ARRAY(SELECT upper(value) FROM jsonb_array_elements_text(
                       CASE WHEN jsonb_typeof(ingest_run.summary->'symbols_requested') = 'array'
                            THEN ingest_run.summary->'symbols_requested' ELSE '[]'::jsonb END
                   ) requested(value)) AS requested_symbols
            FROM raw.option_snapshot candidate
            JOIN ingest.source source ON source.id = candidate.source_id
              AND source.enabled AND source.operational_state = 'active'
            JOIN ingest.run ingest_run ON ingest_run.id = candidate.ingest_run_id
              AND ingest_run.status IN ('succeeded', 'partial')
              AND ingest_run.finished_at <= now()
            WHERE candidate.capture_state = 'complete' AND candidate.observed_at <= now()
              AND (COALESCE(candidate.history_symbol, '') = '' OR candidate.history_symbol IN
                   (SELECT symbol FROM requested_instruments))
        ), latest AS MATERIALIZED (
            SELECT instrument.id AS instrument_id, snapshot.*
            FROM requested_instruments instrument
            JOIN LATERAL (
                SELECT candidate.*
                FROM (
                    SELECT candidate.* FROM eligible_snapshots candidate
                    WHERE candidate.history_symbol = instrument.symbol
                       OR (COALESCE(candidate.history_symbol, '') = ''
                           AND (cardinality(candidate.requested_symbols) = 0
                                OR instrument.symbol = ANY(candidate.requested_symbols)))
                    ORDER BY candidate.observed_at DESC,
                             CASE candidate.source_id WHEN 'robinhood' THEN 0 WHEN 'ibkr' THEN 1 ELSE 2 END,
                             candidate.id DESC
                    OFFSET 0
                ) candidate
                WHERE CASE
                    WHEN COALESCE(candidate.history_symbol, '') <> ''
                         OR cardinality(candidate.requested_symbols) > 0 THEN true
                    ELSE EXISTS (
                        SELECT 1
                        FROM (SELECT id FROM catalog.option_contract
                              WHERE underlying_instrument_id = instrument.id OFFSET 0) owned
                        JOIN LATERAL (
                            SELECT 1 FROM raw.option_quote member
                            WHERE member.snapshot_id = candidate.id AND member.contract_id = owned.id
                            LIMIT 1
                        ) membership ON true
                    )
                END
                LIMIT 1
            ) snapshot ON true
        ), current_quotes AS MATERIALIZED (
            SELECT DISTINCT ON (snapshot.id, quote.contract_id)
                   instrument.symbol, contract.expiration, contract.strike, contract.option_type,
                   quote.snapshot_id, quote.contract_id, quote.bid, quote.ask, quote.mid, quote.last,
                   quote.volume, quote.open_interest, quote.provider_iv, quote.provider_delta,
                   quote.provider_gamma, quote.provider_theta, quote.provider_vega, quote.observed_at,
                   snapshot.source_id, snapshot.ingest_run_id, snapshot.available_at
            FROM latest snapshot
            JOIN requested_instruments instrument ON instrument.id = snapshot.instrument_id
            JOIN LATERAL (
                SELECT quote.* FROM raw.option_quote quote
                WHERE snapshot.latest_complete_generation_id IS NULL AND quote.snapshot_id = snapshot.id
                  AND quote.observed_at <= now()
                UNION ALL
                SELECT quote.* FROM raw.option_quote quote
                WHERE quote.capture_generation_id = snapshot.latest_complete_generation_id
                  AND quote.snapshot_id = snapshot.id AND quote.observed_at <= now()
                OFFSET 0
            ) quote ON true
            JOIN catalog.option_contract contract ON contract.id = quote.contract_id
              AND contract.underlying_instrument_id = instrument.id
            ORDER BY snapshot.id, quote.contract_id, quote.observed_at DESC
        )
    """
    if name == "options_chain":
        return query + """
            SELECT symbol, expiration AS expiry, strike, option_type, bid, ask, mid, last,
                   volume, open_interest, provider_iv AS iv, provider_delta AS delta,
                   provider_gamma AS gamma, provider_theta AS theta, provider_vega AS vega,
                   observed_at, source_id AS source, contract_id::text AS contract_symbol,
                   snapshot_id, ingest_run_id::text AS source_version, available_at,
                   count(*) OVER () AS __panel_total_count
            FROM current_quotes
            ORDER BY symbol, expiration, strike, option_type, contract_id
        """
    if name == "options_expiries":
        return query + """
            SELECT symbol, expiration AS expiry, max(observed_at) AS observed_at,
                   source_id AS source, snapshot_id, max(available_at) AS available_at,
                   count(*) OVER () AS __panel_total_count
            FROM current_quotes
            GROUP BY symbol, expiration, source_id, snapshot_id
            ORDER BY symbol, expiration
        """
    if name != "vol_surface_features":
        raise ValueError(f"unsupported option panel: {name}")
    return query + """
        SELECT symbol AS ticker, symbol, expiration,
               avg(provider_iv) FILTER (WHERE option_type = 'call') AS call_iv,
               avg(provider_iv) FILTER (WHERE option_type = 'put') AS put_iv,
               avg(provider_iv) FILTER (WHERE option_type = 'put')
                 - avg(provider_iv) FILTER (WHERE option_type = 'call') AS put_call_skew,
               max(observed_at) AS as_of, count(*) AS contracts,
               snapshot_id, source_id AS source, max(available_at) AS available_at,
               count(*) OVER () AS __panel_total_count
        FROM current_quotes
        GROUP BY symbol, expiration, snapshot_id, source_id
        ORDER BY as_of DESC, symbol, expiration
    """


TRANSITION_QUERY = """
        WITH recent AS MATERIALIZED (
            SELECT decision.id, decision.instrument_id, decision.as_of, decision.state,
                   decision.score, option_decision.contract_id
            FROM analysis.decision decision
            JOIN analysis.option_decision option_decision ON option_decision.decision_id = decision.id
            ORDER BY decision.as_of DESC, decision.id DESC
            LIMIT %s
        )
        SELECT recent.id::text AS candidate_event_id, instrument.symbol AS ticker,
               recent.as_of AS transitioned_at, recent.state AS to_state,
               previous.state AS from_state, recent.score
        FROM recent
        JOIN catalog.instrument instrument ON instrument.id = recent.instrument_id
        LEFT JOIN LATERAL (
            SELECT predecessor.state
            FROM analysis.option_decision prior_option
            JOIN analysis.decision predecessor ON predecessor.id = prior_option.decision_id
            WHERE prior_option.contract_id = recent.contract_id
              AND (predecessor.as_of, predecessor.id) < (recent.as_of, recent.id)
            ORDER BY predecessor.as_of DESC, predecessor.id DESC
            LIMIT 1
        ) previous ON true
        ORDER BY recent.as_of DESC, recent.id DESC
    """


def transition_rows(connection: Any, *, limit: int) -> list[dict[str, Any]]:
    rows = connection.execute(TRANSITION_QUERY, [limit]).fetchall()
    return [dict(row) for row in rows]
