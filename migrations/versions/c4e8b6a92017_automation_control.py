"""Persist the automatic-post workflow switch, inheriting AUTO_ADVANCE initially."""

import sqlalchemy as sa
from alembic import op

revision = "c4e8b6a92017"
down_revision = "b7d2e9a40316"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("controls", sa.Column("automation_enabled", sa.Boolean(), nullable=True))


def downgrade():
    op.drop_column("controls", "automation_enabled")
