"""Separate optional asset owner from the Public/Not public usage-rights choice."""

import sqlalchemy as sa
from alembic import op

revision = "d3a7f1c58e42"
down_revision = "c4e8b6a92017"
branch_labels = None
depends_on = None


def upgrade():
    # Existing free-text rights stay unchanged; they display as unclassified.
    op.add_column("assets", sa.Column("owner", sa.String(200), nullable=False, server_default=""))


def downgrade():
    op.drop_column("assets", "owner")
