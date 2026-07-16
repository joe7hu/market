"""Track when price facts became available to point-in-time readers.

Revision ID: 20260716_0009
Revises: 20260715_0008
"""

from __future__ import annotations

from alembic import op


revision = "20260716_0009"
down_revision = "20260715_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE catalog.instrument ADD COLUMN market_timezone TEXT "
        "NOT NULL DEFAULT 'America/New_York'"
    )
    op.execute(
        """
        UPDATE catalog.instrument SET market_timezone = CASE
            WHEN symbol IN ('5803','8035','9984','NI225','TOPIX') OR symbol LIKE '%.T' THEN 'Asia/Tokyo'
            WHEN symbol IN ('000660','005380','005930','373220','KOSPI') OR symbol LIKE '%.KS' THEN 'Asia/Seoul'
            WHEN symbol IN ('HINDALCO','NIFTY','ZEEL','BPCL') OR symbol LIKE '%.NS' THEN 'Asia/Kolkata'
            WHEN symbol = 'HSI' OR symbol LIKE '%.HK' THEN 'Asia/Hong_Kong'
            WHEN symbol IN ('LPK','RWE') OR symbol LIKE '%.DE' THEN 'Europe/Berlin'
            WHEN symbol IN ('SIVE','SIVE.') OR symbol LIKE '%.ST' THEN 'Europe/Stockholm'
            WHEN symbol = 'SOI' OR symbol LIKE '%.PA' THEN 'Europe/Paris'
            WHEN symbol LIKE '%.L' THEN 'Europe/London'
            WHEN symbol LIKE '%.AX' THEN 'Australia/Sydney'
            WHEN symbol LIKE '%.V' THEN 'America/Toronto'
            WHEN symbol = 'MMC' OR symbol LIKE '%.VI' THEN 'Europe/Vienna'
            WHEN symbol = 'BOURSA' OR symbol LIKE '%.KW' THEN 'Asia/Kuwait'
            WHEN symbol = 'QNBK' OR symbol LIKE '%.QA' THEN 'Asia/Qatar'
            WHEN symbol = '399300' OR symbol LIKE '%.SZ' THEN 'Asia/Shanghai'
            WHEN symbol = 'KNOX' THEN 'America/Toronto'
            WHEN symbol = 'TASI' OR symbol LIKE '%.SR' THEN 'Asia/Riyadh'
            WHEN symbol LIKE '%-USD' OR symbol IN (
                'BNBUSD','BTCUSD','ETHUSD','HYPEUSD','SOLUSD','XLMUSD','XRPUSD',
                'USDJPY','USDKRW','USDMYR','USDPHP','USDSGD','USDTHB'
            ) THEN 'UTC'
            ELSE market_timezone END
        """
    )
    for table in ("price_bar", "quote"):
        op.execute(f"ALTER TABLE raw.{table} ADD COLUMN available_at TIMESTAMPTZ")
        op.execute(
            f"""
            UPDATE raw.{table} fact
            SET available_at = coalesce(price_run.finished_at, clock_timestamp())
            FROM ingest.run price_run
            WHERE price_run.id = fact.ingest_run_id
            """
        )
        op.execute(f"ALTER TABLE raw.{table} ALTER COLUMN available_at SET DEFAULT clock_timestamp()")
        op.execute(f"ALTER TABLE raw.{table} ALTER COLUMN available_at SET NOT NULL")
    op.execute("CREATE TABLE raw.price_bar_history (LIKE raw.price_bar INCLUDING DEFAULTS)")
    op.execute("CREATE TABLE raw.quote_history (LIKE raw.quote INCLUDING DEFAULTS)")
    op.execute(
        """
        CREATE TABLE raw.price_bar_confirmation (
            fact_id BIGINT NOT NULL,
            fact_available_at TIMESTAMPTZ NOT NULL,
            ingest_run_id UUID NOT NULL REFERENCES ingest.run(id),
            confirmed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (fact_id, fact_available_at, ingest_run_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE raw.quote_confirmation (
            fact_id BIGINT NOT NULL,
            fact_available_at TIMESTAMPTZ NOT NULL,
            ingest_run_id UUID NOT NULL REFERENCES ingest.run(id),
            confirmed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (fact_id, fact_available_at, ingest_run_id)
        )
        """
    )
    op.execute(
        """
        INSERT INTO raw.price_bar_confirmation (fact_id, fact_available_at, ingest_run_id)
        SELECT id, available_at, ingest_run_id FROM raw.price_bar
        """
    )
    op.execute(
        """
        INSERT INTO raw.quote_confirmation (fact_id, fact_available_at, ingest_run_id)
        SELECT id, available_at, ingest_run_id FROM raw.quote
        """
    )
    op.execute(
        "CREATE INDEX ix_raw_price_bar_history_asof "
        "ON raw.price_bar_history (instrument_id, trading_date, available_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_raw_quote_history_asof "
        "ON raw.quote_history (instrument_id, observed_at, available_at DESC)"
    )
    op.execute(
        """
        CREATE VIEW raw.confirmed_price_bar AS
        SELECT DISTINCT ON (fact.instrument_id, fact.source_id, fact.interval, fact.observed_at) fact.*
        FROM (
            SELECT * FROM raw.price_bar
            UNION ALL
            SELECT * FROM raw.price_bar_history
        ) fact
        WHERE EXISTS (
            SELECT 1 FROM raw.price_bar_confirmation confirmation
            JOIN ingest.run price_run ON price_run.id = confirmation.ingest_run_id
            WHERE confirmation.fact_id = fact.id
              AND confirmation.fact_available_at = fact.available_at
              AND price_run.status IN ('succeeded', 'partial')
              AND price_run.finished_at IS NOT NULL
        )
        ORDER BY fact.instrument_id, fact.source_id, fact.interval, fact.observed_at,
                 fact.available_at DESC
        """
    )
    op.execute(
        """
        CREATE VIEW raw.confirmed_quote AS
        SELECT DISTINCT ON (fact.instrument_id, fact.source_id, fact.observed_at) fact.*
        FROM (
            SELECT * FROM raw.quote
            UNION ALL
            SELECT * FROM raw.quote_history
        ) fact
        WHERE EXISTS (
            SELECT 1 FROM raw.quote_confirmation confirmation
            JOIN ingest.run price_run ON price_run.id = confirmation.ingest_run_id
            WHERE confirmation.fact_id = fact.id
              AND confirmation.fact_available_at = fact.available_at
              AND price_run.status IN ('succeeded', 'partial')
              AND price_run.finished_at IS NOT NULL
        )
        ORDER BY fact.instrument_id, fact.source_id, fact.observed_at, fact.available_at DESC
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW raw.confirmed_quote")
    op.execute("DROP VIEW raw.confirmed_price_bar")
    op.execute("DROP TABLE raw.quote_confirmation")
    op.execute("DROP TABLE raw.price_bar_confirmation")
    op.execute("DROP TABLE raw.quote_history")
    op.execute("DROP TABLE raw.price_bar_history")
    op.execute("ALTER TABLE raw.quote DROP COLUMN available_at")
    op.execute("ALTER TABLE raw.price_bar DROP COLUMN available_at")
    op.execute("ALTER TABLE catalog.instrument DROP COLUMN market_timezone")
