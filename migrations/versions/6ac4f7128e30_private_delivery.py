"""Bind Not public posts to simulated delivery and correct outstanding reservations."""

import sqlalchemy as sa
from alembic import op

from social.delivery_privacy import restrict_private_posts

revision = "6ac4f7128e30"
down_revision = "e3ab61c84570"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "posts", sa.Column("simulation_only", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.create_index("ix_media_jobs_post_id", "media_jobs", ["post_id"])
    restrict_private_posts(op.get_bind())


def downgrade():
    op.drop_index("ix_media_jobs_post_id", table_name="media_jobs")
    op.drop_column("posts", "simulation_only")
