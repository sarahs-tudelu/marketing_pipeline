"""Remember completed media jobs dismissed from In progress."""
import sqlalchemy as sa
from alembic import op

revision = "e3ab61c84570"
down_revision = "b4e8d2f61a93"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("media_jobs", sa.Column("dismissed_at", sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column("media_jobs", "dismissed_at")
