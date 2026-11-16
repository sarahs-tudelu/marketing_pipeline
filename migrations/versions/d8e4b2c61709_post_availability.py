"""Track platform availability separately from delivery and link reviewed reposts."""

import sqlalchemy as sa
from alembic import op

revision = "d8e4b2c61709"
down_revision = "c6f3a7b95247"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "post_availability",
        sa.Column("post_id", sa.String(36), sa.ForeignKey("posts.id"), primary_key=True),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("checked_at", sa.DateTime()),
        sa.Column("last_seen_at", sa.DateTime()),
        sa.Column("unavailable_since", sa.DateTime()),
        sa.Column("next_check_at", sa.DateTime()),
        sa.Column("public_ids", sa.JSON(), nullable=False),
    )
    op.create_index("ix_post_availability_next_check_at", "post_availability", ["next_check_at"])
    op.create_table(
        "availability_checks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("post_id", sa.String(36), sa.ForeignKey("posts.id"), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("checked_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_availability_checks_post_id", "availability_checks", ["post_id"])
    op.create_table(
        "post_reposts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("original_post_id", sa.String(36), sa.ForeignKey("posts.id"), nullable=False),
        sa.Column("repost_post_id", sa.String(36), sa.ForeignKey("posts.id"), nullable=False, unique=True),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_post_reposts_original_post_id", "post_reposts", ["original_post_id"])


def downgrade():
    for table in ("post_reposts", "availability_checks", "post_availability"):
        op.drop_table(table)
