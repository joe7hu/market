"""Seek confirmed quote tips before hydrating price candidates.

Keep 0030's information-time and daily-session rules verbatim. A source's
older market quotes cannot win its information-time contest. Find the newest
eligible observation with ordered, confirmation-checked probes, then hydrate
only that observation's versions. Daily quotes still use the session-clock
path; they must not be ordered by their provider's nominal timestamp.
"""

from importlib import import_module

from alembic import op

revision = "20260922_0034"
down_revision = "20260921_0033"
branch_labels = None
depends_on = None

_PREVIOUS = "migrations.versions.20260921_0030_current_price_information_time"
_INDEXES = (
    ("ix_raw_quote_source_tip", "raw.quote", "instrument_id, source_id, observed_at DESC, available_at DESC", " INCLUDE (id, price)"),
    ("ix_raw_quote_history_source_tip", "raw.quote_history", "instrument_id, source_id, observed_at DESC, available_at DESC", " INCLUDE (id, price)"),
    ("ix_raw_quote_history_version_tip", "raw.quote_history", "id, available_at DESC", ""),
)


def _replace_once(sql: str, before: str, after: str) -> str:
    if sql.count(before) != 1:
        raise RuntimeError("0030 price-selector template changed; review 0034 explicitly")
    return sql.replace(before, after, 1)


def _confirmed(alias: str) -> str:
    return f"""EXISTS (
        SELECT 1 FROM raw.quote_fact_availability availability
        JOIN ingest.run price_run ON price_run.id = availability.ingest_run_id
        WHERE availability.fact_id = {alias}.id
          AND availability.fact_available_at = {alias}.available_at
          AND price_run.status IN ('succeeded', 'partial')
          AND price_run.finished_at IS NOT NULL
          AND price_run.finished_at <= p_as_of
    )"""


def _no_newer(alias: str) -> str:
    return f"""NOT EXISTS (
        SELECT 1 FROM (
            SELECT id, available_at FROM raw.quote
            WHERE id = {alias}.id AND available_at > {alias}.available_at AND available_at <= p_as_of
            UNION ALL
            SELECT id, available_at FROM raw.quote_history
            WHERE id = {alias}.id AND available_at > {alias}.available_at AND available_at <= p_as_of
        ) newer WHERE {_confirmed('newer')}
    )"""


def _function() -> str:
    sql = import_module(_PREVIOUS)._function(information_time_first=True)
    # Do not put LIMIT before confirmation: an unconfirmed correction must not
    # hide the older confirmed version, even when both tables share a fact ID.
    tips = f"""WITH source_quote_tip AS MATERIALIZED (
        SELECT instrument.id AS instrument_id, source.id AS source_id, tip.observed_at
        FROM catalog.instrument instrument
        CROSS JOIN ingest.source source
        CROSS JOIN LATERAL (
            SELECT fact.observed_at
            FROM (
                SELECT id, instrument_id, source_id, observed_at, available_at, price
                FROM raw.quote
                WHERE instrument_id = instrument.id AND source_id = source.id
                UNION ALL
                SELECT id, instrument_id, source_id, observed_at, available_at, price
                FROM raw.quote_history
                WHERE instrument_id = instrument.id AND source_id = source.id
            ) fact
            WHERE fact.available_at <= p_as_of AND fact.observed_at <= p_as_of
              AND fact.price > 0 AND {_confirmed('fact')}
              AND {_no_newer('fact')}
            ORDER BY fact.observed_at DESC, fact.available_at DESC
            LIMIT 1
        ) tip
        WHERE instrument.id = ANY(p_instrument_ids)
          AND source.enabled AND source.operational_state = 'active'
          AND source.kind NOT IN ('daily_bars', 'daily_quote')
    ), confirmed_quote AS MATERIALIZED ("""
    sql = _replace_once(sql, "WITH confirmed_quote AS MATERIALIZED (", tips)
    sql = _replace_once(sql, """SELECT * FROM raw.quote WHERE instrument_id = ANY(p_instrument_ids)
                UNION ALL
                SELECT * FROM raw.quote_history WHERE instrument_id = ANY(p_instrument_ids)""", f"""SELECT fact.* FROM raw.quote fact
                JOIN source_quote_tip tip USING (instrument_id, source_id, observed_at)
                WHERE {_no_newer('fact')}
                UNION ALL
                SELECT fact.* FROM raw.quote_history fact
                JOIN source_quote_tip tip USING (instrument_id, source_id, observed_at)
                WHERE {_no_newer('fact')}
                UNION ALL
                SELECT fact.* FROM raw.quote fact
                JOIN ingest.source source ON source.id = fact.source_id
                WHERE fact.instrument_id = ANY(p_instrument_ids)
                  AND source.kind IN ('daily_bars', 'daily_quote')
                UNION ALL
                SELECT fact.* FROM raw.quote_history fact
                JOIN ingest.source source ON source.id = fact.source_id
                WHERE fact.instrument_id = ANY(p_instrument_ids)
                  AND source.kind IN ('daily_bars', 'daily_quote')""")
    # Availability has PRIMARY KEY (fact_id, fact_available_at). Its single
    # projected confirmation needs no per-version correlated sort/LIMIT. Plain
    # joins allow PostgreSQL to choose a bulk/hash plan for the daily history.
    for table in ("quote", "price_bar"):
        sql = _replace_once(sql, f"""CROSS JOIN LATERAL (
                SELECT price_run.finished_at AS confirmed_at
                FROM raw.{table}_fact_availability availability
                JOIN ingest.run price_run ON price_run.id = availability.ingest_run_id
                    AND price_run.status IN ('succeeded', 'partial')
                    AND price_run.finished_at IS NOT NULL AND price_run.finished_at <= p_as_of
                WHERE availability.fact_id = fact.id
                    AND availability.fact_available_at = fact.available_at
                ORDER BY confirmed_at LIMIT 1
            ) confirmation""", f"""JOIN raw.{table}_fact_availability availability
              ON availability.fact_id = fact.id
             AND availability.fact_available_at = fact.available_at
            JOIN ingest.run confirmation ON confirmation.id = availability.ingest_run_id
             AND confirmation.status IN ('succeeded', 'partial')
             AND confirmation.finished_at IS NOT NULL AND confirmation.finished_at <= p_as_of""")
    if sql.count("fact.*, confirmation.confirmed_at") != 2:
        raise RuntimeError("0030 confirmation projection changed")
    return sql.replace("fact.*, confirmation.confirmed_at", "fact.*, confirmation.finished_at AS confirmed_at")


def upgrade() -> None:
    for name, table, columns, include in _INDEXES:
        op.execute(f"CREATE INDEX {name} ON {table} ({columns}){include}")
    op.execute(_function())


def downgrade() -> None:
    op.execute(import_module(_PREVIOUS)._function(information_time_first=True))
    for name, _, _, _ in reversed(_INDEXES):
        op.execute(f"DROP INDEX raw.{name}")
