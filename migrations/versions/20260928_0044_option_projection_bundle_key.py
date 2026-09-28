"""Apply bundle filters before the opaque option projection function."""
from alembic import op

revision = "20260928_0044"
down_revision = "20260928_0043"
branch_labels = None
depends_on = None


def _view(bundle_key: str) -> None:
    op.execute(f"""CREATE OR REPLACE VIEW app.option_publication_projection AS
        SELECT {bundle_key} AS bundle_id, projected.model_name, projected.stable_key,
               projected.rank, projected.instrument_id,
               projected.content_hash::character(64) AS content_hash,
               projected.canonical_publication_id, projected.decision_payload_hash,
               projected.payload FROM app.publication_bundle bundle
        CROSS JOIN LATERAL app.option_bundle_projection(bundle.id) projected
        WHERE bundle.scope = 'options-radar'
          AND bundle.projection_version = 'option-subsets-v1'
    """)


def upgrade() -> None:
    _view("bundle.id")


def downgrade() -> None:
    _view("projected.bundle_id")
