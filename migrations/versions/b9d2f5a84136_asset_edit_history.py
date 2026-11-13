"""Record metadata edits without rewriting stored media."""

import sqlalchemy as sa
from alembic import op

revision = "b9d2f5a84136"
down_revision = "a8c1e4f73025"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "asset_edits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("asset_id", sa.String(36), sa.ForeignKey("assets.id"), nullable=False),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("before", sa.JSON(), nullable=False),
        sa.Column("after", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_asset_edits_asset_id", "asset_edits", ["asset_id"])


def downgrade():
    op.drop_index("ix_asset_edits_asset_id", table_name="asset_edits")
    op.drop_table("asset_edits")
