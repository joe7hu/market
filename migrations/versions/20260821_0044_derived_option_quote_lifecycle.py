"""Let recomputable option detail outlive detached raw quote partitions.

The option feature and option decision rows retain the quote identity and
timestamp as analytical evidence, but their lifecycle is owned by
``analysis.run``.  A foreign key to the raw partitioned quote table would
prevent the seven-day hot partition policy from detaching immutable history
while a 30-day publication is still retained.
"""

from __future__ import annotations

from alembic import op


revision = "20260821_0044"
down_revision = "20260821_0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE analysis.option_feature
            DROP CONSTRAINT IF EXISTS option_feature_snapshot_id_contract_id_quote_observed_at_fkey;
        ALTER TABLE analysis.option_decision
            DROP CONSTRAINT IF EXISTS option_decision_snapshot_id_contract_id_quote_observed_at_fkey;
        """
    )


def downgrade() -> None:
    # Keep downgrade reversible for an isolated schema.  NOT VALID avoids a
    # historical scan when an older raw partition was already archived; a
    # controlled recovery can validate it after restoring that partition.
    op.execute(
        """
        ALTER TABLE analysis.option_feature
            ADD CONSTRAINT option_feature_snapshot_id_contract_id_quote_observed_at_fkey
            FOREIGN KEY (snapshot_id, contract_id, quote_observed_at)
            REFERENCES raw.option_quote(snapshot_id, contract_id, observed_at)
            NOT VALID;
        ALTER TABLE analysis.option_decision
            ADD CONSTRAINT option_decision_snapshot_id_contract_id_quote_observed_at_fkey
            FOREIGN KEY (snapshot_id, contract_id, quote_observed_at)
            REFERENCES raw.option_quote(snapshot_id, contract_id, observed_at)
            NOT VALID;
        """
    )
