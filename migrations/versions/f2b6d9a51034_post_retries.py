"""Link each failed post to one explicit retry while preserving its audit history."""

import sqlalchemy as sa
from alembic import op

revision = "f2b6d9a51034"
down_revision = "d3a7f1c58e42"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "post_retries",
        sa.Column("original_post_id", sa.String(36), sa.ForeignKey("posts.id"), primary_key=True),
        sa.Column("retry_post_id", sa.String(36), sa.ForeignKey("posts.id"), nullable=False, unique=True),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade():
    op.drop_table("post_retries")
