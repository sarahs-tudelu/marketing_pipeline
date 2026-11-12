"""Allow an explicit per-platform live setting, retaining environment defaults."""

import sqlalchemy as sa
from alembic import op

revision = "b7d2e9a40316"
down_revision = "9b61d8c4a201"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("accounts", sa.Column("live_enabled", sa.Boolean(), nullable=True))


def downgrade():
    op.drop_column("accounts", "live_enabled")
