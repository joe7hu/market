"""Use one price-writer lock and index exact option retention lookups."""
from importlib import import_module

from alembic import op

revision = "20260928_0043"
down_revision = "20260927_0042"
branch_labels = None
depends_on = None

_MARK_FUNCTIONS = ("refresh_paper_current_marks", "refresh_paper_option_marks")
_WRITER_RELATIONS = (
    "ingest.run", "raw.quote", "raw.quote_history", "raw.price_bar", "raw.price_bar_history",
    "raw.quote_confirmation", "raw.price_bar_confirmation",
    "raw.quote_fact_availability", "raw.price_bar_fact_availability",
    "raw.option_snapshot", "raw.option_capture_generation", "raw.option_quote",
)
_LOCK = "            PERFORM pg_advisory_xact_lock(hashtextextended('raw.option_quote.partition', 0));\n            mark_as_of := clock_timestamp();\n"


def _mark_locks(enabled: bool) -> None:
    for name in _MARK_FUNCTIONS:
        definition = op.get_bind().exec_driver_sql(
            f"SELECT pg_get_functiondef('analysis.{name}(bigint[])'::regprocedure)"
        ).scalar_one()
        updated = definition.replace("BEGIN\n", "BEGIN\n" + _LOCK, 1) if enabled else definition.replace(_LOCK, "", 1)
        if updated == definition:
            raise RuntimeError("mark function did not match its migration contract")
        op.execute(updated)


def _projection_views() -> None:
    previous = import_module("migrations.versions.20260927_0042_storage_access_paths")
    # Expose the two model names before the opaque function so unrelated model
    # predicates remove this branch without computing historical option bundles.
    models = "CROSS JOIN (VALUES ('option_snapshot'::text), ('option_features'::text)) model(model_name)"
    op.execute("CREATE OR REPLACE VIEW app.publication_content_item AS " + previous._PHYSICAL_HISTORY + f"""
        UNION ALL
        SELECT publication.id, model.model_name, projected.stable_key,
               projected.rank, projected.instrument_id, projected.payload
        FROM app.publication publication
        JOIN app.publication_bundle bundle ON bundle.id = publication.bundle_id
        {models}
        CROSS JOIN LATERAL app.option_bundle_projection(bundle.id) projected
        WHERE bundle.scope = 'options-radar' AND bundle.projection_version = 'option-subsets-v1'
          AND projected.model_name = model.model_name
    """)
    op.execute("CREATE OR REPLACE VIEW app.current_publication_item_read AS " + previous._PHYSICAL_CURRENT + f"""
        UNION ALL
        SELECT current_item.scope, current_item.publication_id,
               model.model_name, projected.stable_key, projected.rank,
               projected.instrument_id, projected.content_hash::character(64),
               projected.decision_payload_hash, projected.payload
        FROM (SELECT DISTINCT scope, publication_id FROM app.current_publication_item
              WHERE model_name = 'candidate_event') current_item
        JOIN app.publication publication ON publication.id = current_item.publication_id
        {models}
        CROSS JOIN LATERAL app.option_bundle_projection(publication.bundle_id) projected
        WHERE projected.model_name = model.model_name
    """)


def upgrade() -> None:
    op.execute("""CREATE INDEX ix_option_decision_retention_quote
        ON analysis.option_decision (snapshot_id, quote_observed_at, contract_id)""")
    _mark_locks(True)
    _projection_views()
    # ponytail: reuse the existing global snapshot lock; ordered instrument and
    # contract locks are the upgrade if price ingestion throughput requires it.
    op.execute("""
        CREATE FUNCTION analysis.lock_price_mark_writers() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'pg_catalog'
        AS $$ BEGIN
            PERFORM pg_advisory_xact_lock(hashtextextended('raw.option_quote.partition', 0));
            RETURN NULL;
        END; $$;
        REVOKE ALL ON FUNCTION analysis.lock_price_mark_writers() FROM PUBLIC;
    """)
    for relation in _WRITER_RELATIONS:
        actions = "UPDATE OR DELETE" if relation == "ingest.run" else "INSERT OR UPDATE OR DELETE"
        op.execute(f"""CREATE TRIGGER price_mark_writer_lock BEFORE {actions} ON {relation}
            FOR EACH STATEMENT EXECUTE FUNCTION analysis.lock_price_mark_writers()""")


def downgrade() -> None:
    previous = import_module("migrations.versions.20260927_0042_storage_access_paths")
    # Restore 0042's view bodies without rebuilding its indexes or function.
    op.execute("""CREATE OR REPLACE VIEW app.publication_content_item AS """ + previous._PHYSICAL_HISTORY + """
        UNION ALL SELECT publication.id, projected.model_name, projected.stable_key,
               projected.rank, projected.instrument_id, projected.payload
        FROM app.publication publication JOIN app.publication_bundle bundle ON bundle.id = publication.bundle_id
        CROSS JOIN LATERAL app.option_bundle_projection(bundle.id) projected
        WHERE bundle.scope = 'options-radar' AND bundle.projection_version = 'option-subsets-v1'
    """)
    op.execute("CREATE OR REPLACE VIEW app.current_publication_item_read AS " + previous._PHYSICAL_CURRENT + """
        UNION ALL SELECT current_item.scope, current_item.publication_id,
               projected.model_name, projected.stable_key, projected.rank, projected.instrument_id,
               projected.content_hash::character(64), projected.decision_payload_hash, projected.payload
        FROM (SELECT DISTINCT scope, publication_id FROM app.current_publication_item
              WHERE model_name = 'candidate_event') current_item
        JOIN app.publication publication ON publication.id = current_item.publication_id
        CROSS JOIN LATERAL app.option_bundle_projection(publication.bundle_id) projected
    """)
    for relation in reversed(_WRITER_RELATIONS):
        op.execute(f"DROP TRIGGER price_mark_writer_lock ON {relation}")
    op.execute("DROP FUNCTION analysis.lock_price_mark_writers()")
    _mark_locks(False)
    op.execute("DROP INDEX analysis.ix_option_decision_retention_quote")
