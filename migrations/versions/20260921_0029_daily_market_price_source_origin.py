"""Record the live daily-price provider change under the existing source ID."""

from alembic import op


revision = "20260921_0029"
down_revision = "20260920_0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE ingest.source
           SET origin = 'Yahoo chart and Coinbase Exchange daily candles', updated_at = now()
         WHERE id = 'daily-market-prices'
           AND origin = 'Yahoo chart and CoinGecko'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE ingest.source
           SET origin = 'Yahoo chart and CoinGecko', updated_at = now()
         WHERE id = 'daily-market-prices'
           AND origin = 'Yahoo chart and Coinbase Exchange daily candles'
        """
    )
