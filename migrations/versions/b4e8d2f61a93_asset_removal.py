"""Deleted asset library files that posts or media jobs still use are kept, marked as removed."""
import sqlalchemy as sa
from alembic import op

revision = "b4e8d2f61a93"
down_revision = "f9c2a1e7d340"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("assets", sa.Column("removed_at", sa.DateTime(), nullable=True))
    op.add_column("assets", sa.Column("removed_by", sa.String(200), nullable=False, server_default=""))


def downgrade():
    op.drop_column("assets", "removed_by")
    op.drop_column("assets", "removed_at")
