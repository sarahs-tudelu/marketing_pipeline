"""Runway media jobs and recurring campaign briefs."""

import sqlalchemy as sa
from alembic import op

revision = "9b61d8c4a201"
down_revision = "e5c45d4caf74"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "media_campaigns",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("brief", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "media_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("campaign_id", sa.String(36), sa.ForeignKey("media_campaigns.id")),
        sa.Column("batch_key", sa.String(200), nullable=False, unique=True),
        sa.Column("platform", sa.String(20), nullable=False),
        sa.Column("brief", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("stage", sa.String(20), nullable=False),
        sa.Column("task_id", sa.String(100), nullable=False),
        sa.Column("task_history", sa.JSON(), nullable=False),
        sa.Column("source_asset_ids", sa.JSON(), nullable=False),
        sa.Column("output_asset_id", sa.String(36), sa.ForeignKey("assets.id")),
        sa.Column("post_id", sa.String(36), sa.ForeignKey("posts.id")),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("submitted_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_media_jobs_state", "media_jobs", ["state"])


def downgrade():
    op.drop_index("ix_media_jobs_state", table_name="media_jobs")
    op.drop_table("media_jobs")
    op.drop_table("media_campaigns")
