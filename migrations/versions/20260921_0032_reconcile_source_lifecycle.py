"""Apply the registered standby contracts to legacy source rows."""

from alembic import op


revision = "20260921_0032"
down_revision = "20260921_0031"
branch_labels = None
depends_on = None


_STANDBY_SOURCES = (
    "ibkr", "moomoo", "ibkr_options", "fred", "treasury",
    "trading_economics", "alphavantage", "robinhood_history_full", "coinmetrics", "sec_13f",
)

_SOURCE_IDS = ", ".join(f"'{source}'" for source in _STANDBY_SOURCES)


def upgrade() -> None:
    op.execute(
        """
        UPDATE ingest.source
           SET operational_state = 'standby', updated_at = now()
         WHERE id IN ({_SOURCE_IDS})
           AND operational_state = 'active'
        """.format(_SOURCE_IDS=_SOURCE_IDS),
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE ingest.source
           SET operational_state = 'active', updated_at = now()
         WHERE id IN ({_SOURCE_IDS})
           AND operational_state = 'standby'
        """.format(_SOURCE_IDS=_SOURCE_IDS),
    )
