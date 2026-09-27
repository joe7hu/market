"""Bound option projections by bundle and index immutable payload references.

No evidence is changed, pruned or physically rewritten. Index construction is
explicit migration work; stop writers and check headroom before deploying.
"""
from alembic import op

revision = "20260927_0042"
down_revision = "20260927_0041"
branch_labels = None
depends_on = None

_OPTION_SUBSETS = {
    "option_snapshot": ("snapshot_time", "ticker", "underlying_price", "expiration", "strike",
                        "option_type", "bid", "ask", "mid", "volume", "open_interest", "iv",
                        "delta", "dte", "spread_pct", "data_source", "contract_id", "raw"),
    "option_features": ("snapshot_time", "contract_id", "ticker", "required_2x_price",
                        "required_5x_price", "required_10x_price", "required_move_pct",
                        "liquidity_score", "convexity_score", "raw"),
}


def _projection_sql(bundle_filter: str = "") -> str:
    projected = " UNION ALL ".join(
        "SELECT candidate.bundle_id, '" + model + "'::text AS model_name, "
        "candidate.contract_id AS stable_key, "
        "dense_rank() OVER (PARTITION BY candidate.bundle_id ORDER BY candidate.first_rank)::integer AS rank, "
        "NULL::bigint AS instrument_id, NULL::character(64) AS content_hash, "
        "NULL::uuid AS canonical_publication_id, NULL::text AS decision_payload_hash, "
        "jsonb_build_object(" + ", ".join(
            "'" + key + "', candidate.payload->'" + key + "'" for key in keys
        ) + ") AS payload FROM candidate WHERE candidate.last_rank = 1"
        for model, keys in _OPTION_SUBSETS.items()
    )
    return """WITH candidate AS MATERIALIZED (
        SELECT item.bundle_id, item.payload->>'contract_id' AS contract_id,
               item.payload, min(item.rank) OVER (
                 PARTITION BY item.bundle_id, item.payload->>'contract_id') AS first_rank,
               row_number() OVER (
                 PARTITION BY item.bundle_id, item.payload->>'contract_id'
                 ORDER BY item.rank DESC) AS last_rank
        FROM app.publication_bundle_item_read item
        JOIN app.publication_bundle bundle ON bundle.id = item.bundle_id
        WHERE bundle.scope = 'options-radar'
          AND bundle.projection_version = 'option-subsets-v1'
          AND item.model_name = 'candidate_event'
          AND item.payload->>'contract_id' IS NOT NULL
          """ + bundle_filter + ") " + projected


_INDEXES = (
    ("app.publication_bundle_item", "publication_bundle_decision_payload_idx", "decision_payload_hash"),
    ("app.current_publication_item", "current_publication_decision_payload_idx", "decision_payload_hash"),
    ("app.publication_bundle_item", "publication_bundle_content_hash_idx", "content_hash"),
    ("app.current_publication_item", "current_publication_content_hash_idx", "content_hash"),
)

_PHYSICAL_CURRENT = """
    SELECT item.*, COALESCE(payload.payload, evidence.payload) AS payload
    FROM app.current_publication_item item
    LEFT JOIN app.publication_payload payload ON payload.content_hash = item.content_hash
    LEFT JOIN analysis.decision_input_payload evidence
      ON evidence.content_hash = item.decision_payload_hash
"""
_PHYSICAL_HISTORY = """
    SELECT item.publication_id, item.model_name, item.stable_key,
           item.rank, item.instrument_id, item.payload
    FROM app.publication_item item
    JOIN app.publication publication ON publication.id = item.publication_id
    WHERE publication.bundle_id IS NULL
    UNION ALL
    SELECT publication.id, item.model_name, item.stable_key,
           item.rank, item.instrument_id, item.payload
    FROM app.publication publication
    JOIN app.publication_bundle_item_read item ON item.bundle_id = publication.bundle_id
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '2s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    for relation, name, column in _INDEXES:
        op.execute(f"CREATE INDEX {name} ON {relation} ({column}) WHERE {column} IS NOT NULL")
    # Keep the bundle predicate INSIDE the multiply referenced/windowed CTE.
    # SET search_path also prevents SQL-function inlining from moving it out.
    op.execute("""CREATE FUNCTION app.option_bundle_projection(p_bundle_id uuid)
        RETURNS TABLE (bundle_id uuid, model_name text, stable_key text, rank integer,
                       instrument_id bigint, content_hash character(64),
                       canonical_publication_id uuid, decision_payload_hash text, payload jsonb)
        LANGUAGE sql STABLE STRICT SET search_path = pg_catalog AS $projection$
        """ + _projection_sql("AND bundle.id = p_bundle_id") + "$projection$;")
    op.execute("""
        REVOKE ALL ON FUNCTION app.option_bundle_projection(uuid) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION app.option_bundle_projection(uuid) TO market_app;
        CREATE OR REPLACE VIEW app.option_publication_projection AS
          SELECT projected.* FROM app.publication_bundle bundle
          CROSS JOIN LATERAL app.option_bundle_projection(bundle.id) projected
          WHERE bundle.scope = 'options-radar'
            AND bundle.projection_version = 'option-subsets-v1';
    """)
    op.execute("CREATE OR REPLACE VIEW app.current_publication_item_read AS " + _PHYSICAL_CURRENT + """
        UNION ALL
        SELECT current_item.scope, current_item.publication_id,
               projected.model_name, projected.stable_key, projected.rank,
               projected.instrument_id, projected.content_hash,
               projected.decision_payload_hash, projected.payload
        FROM (SELECT DISTINCT scope, publication_id FROM app.current_publication_item
              WHERE model_name = 'candidate_event') current_item
        JOIN app.publication publication ON publication.id = current_item.publication_id
        CROSS JOIN LATERAL app.option_bundle_projection(publication.bundle_id) projected
    """)
    op.execute("CREATE OR REPLACE VIEW app.publication_content_item AS " + _PHYSICAL_HISTORY + """
        UNION ALL
        SELECT publication.id, projected.model_name, projected.stable_key,
               projected.rank, projected.instrument_id, projected.payload
        FROM app.publication publication
        JOIN app.publication_bundle bundle ON bundle.id = publication.bundle_id
        CROSS JOIN LATERAL app.option_bundle_projection(bundle.id) projected
        WHERE bundle.scope = 'options-radar'
          AND bundle.projection_version = 'option-subsets-v1'
    """)


def downgrade() -> None:
    # Restore 0041's view contracts before dropping the function dependency.
    op.execute("CREATE OR REPLACE VIEW app.option_publication_projection AS " + _projection_sql())
    op.execute("CREATE OR REPLACE VIEW app.current_publication_item_read AS " + _PHYSICAL_CURRENT + """
        UNION ALL
        SELECT current_item.scope, current_item.publication_id,
               projected.model_name, projected.stable_key, projected.rank,
               projected.instrument_id, projected.content_hash,
               projected.decision_payload_hash, projected.payload
        FROM app.option_publication_projection projected
        JOIN app.publication publication ON publication.bundle_id = projected.bundle_id
        JOIN (SELECT DISTINCT scope, publication_id
              FROM app.current_publication_item WHERE model_name = 'candidate_event') current_item
          ON current_item.publication_id = publication.id
    """)
    op.execute("CREATE OR REPLACE VIEW app.publication_content_item AS " + _PHYSICAL_HISTORY + """
        UNION ALL
        SELECT publication.id, projected.model_name, projected.stable_key,
               projected.rank, projected.instrument_id, projected.payload
        FROM app.publication publication
        JOIN app.option_publication_projection projected ON projected.bundle_id = publication.bundle_id
    """)
    op.execute("DROP FUNCTION app.option_bundle_projection(uuid)")
    for _, name, _ in reversed(_INDEXES):
        op.execute(f"DROP INDEX app.{name}")
