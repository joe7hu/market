"""Repair an interrupted option-relative-value index build."""

from alembic import op
from sqlalchemy import text


revision = "20260906_0131"
down_revision = "20260906_0130"
branch_labels = None
depends_on = None

_INDEX = "analysis.ix_option_relative_value_generation_contract"
_CREATE = (
    "CREATE INDEX CONCURRENTLY ix_option_relative_value_generation_contract "
    "ON analysis.option_relative_value (capture_generation_id, contract_id, id DESC)"
)


def upgrade() -> None:
    bind = op.get_bind()
    valid = bind.execute(
        text(
            """SELECT indexrel.indisvalid
               FROM pg_index indexrel
               JOIN pg_class index_class ON index_class.oid = indexrel.indexrelid
               JOIN pg_namespace index_schema ON index_schema.oid = index_class.relnamespace
               WHERE index_schema.nspname = 'analysis'
                 AND index_class.relname = 'ix_option_relative_value_generation_contract'"""
        )
    ).scalar_one_or_none()
    if valid is False:
        with op.get_context().autocommit_block():
            op.execute(f"DROP INDEX CONCURRENTLY {_INDEX}")
    if valid is not True:
        with op.get_context().autocommit_block():
            op.execute(_CREATE)


def downgrade() -> None:
    pass
