"""Select confirmed price versions by stable fact identity at each cutoff.

These readers preserve the raw quote/bar selection contract. The current-price
owner adds source ranking and bar fallback; the daily-price Python owner adds
canonical-source and positive-price rules. Neither is a drop-in version reader.
"""

from alembic import op
from sqlalchemy import text

revision = "20260907_0006"
down_revision = "20260907_0005"
branch_labels = None
depends_on = None


def _selection(kind: str, *, historical: bool) -> str:
    # Keep immutable scope columns in the version key so scoped view callers
    # can push their predicates through DISTINCT ON to the raw fact indexes.
    identity = 'fact.instrument_id, fact.source_id' + (', fact."interval"' if kind == "price_bar" else '')
    identity += ', fact.id' if historical else ', fact.observed_at'
    scope = "WHERE p_instrument_ids IS NULL OR instrument_id = ANY(p_instrument_ids)" if historical else ""
    cutoff = "WHERE fact.available_at <= p_as_of AND price_run.finished_at <= p_as_of" if historical else ""
    return f"""
        SELECT DISTINCT ON ({identity}) fact.*
        FROM (
            SELECT * FROM raw.{kind} {scope}
            UNION ALL
            SELECT * FROM raw.{kind}_history {scope}
        ) fact
        JOIN raw.{kind}_fact_availability availability
          ON availability.fact_id = fact.id AND availability.fact_available_at = fact.available_at
        JOIN ingest.run price_run
          ON price_run.id = availability.ingest_run_id
         AND price_run.status IN ('succeeded', 'partial')
         AND price_run.finished_at IS NOT NULL
        {cutoff}
        ORDER BY {identity}, fact.available_at DESC
    """


def upgrade() -> None:
    connection = op.get_bind()
    for kind in ("quote", "price_bar"):
        owner = connection.execute(text(
            "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = to_regclass(:name)"
        ), {"name": f"raw.confirmed_{kind}"}).scalar_one()
        quoted_owner = connection.dialect.identifier_preparer.quote(owner)
        signature = f"raw.confirmed_{kind}_at(timestamptz, bigint[])"
        op.execute(f"""
            CREATE FUNCTION raw.confirmed_{kind}_at(p_as_of timestamptz, p_instrument_ids bigint[])
            RETURNS SETOF raw.{kind} LANGUAGE sql STABLE SECURITY INVOKER
            AS $selection${_selection(kind, historical=True)}$selection$
        """)
        op.execute(f"ALTER FUNCTION {signature} OWNER TO {quoted_owner}")
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO market_app")
        # CREATE OR REPLACE retains the existing view owner, columns and ACL.
        op.execute(f"""
            CREATE OR REPLACE VIEW raw.confirmed_{kind} AS
            SELECT * FROM raw.confirmed_{kind}_at(now(), NULL::bigint[])
        """)


def downgrade() -> None:
    for kind in ("quote", "price_bar"):
        op.execute(f"CREATE OR REPLACE VIEW raw.confirmed_{kind} AS {_selection(kind, historical=False)}")
        op.execute(f"DROP FUNCTION raw.confirmed_{kind}_at(timestamptz, bigint[])")
