"""Persist Google Drive folder configuration, scan progress and imported versions."""

import sqlalchemy as sa
from alembic import op

revision = "a8c1e4f73025"
down_revision = "f2b6d9a51034"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "drive_sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("folder_id", sa.String(200), nullable=False),
        sa.Column("folder_name", sa.String(255), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("recursive", sa.Boolean(), nullable=False),
        sa.Column("defaults", sa.JSON(), nullable=False),
        sa.Column("scan_state", sa.JSON(), nullable=False),
        sa.Column("next_poll_at", sa.DateTime()),
        sa.Column("last_checked_at", sa.DateTime()),
        sa.Column("last_completed_at", sa.DateTime()),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
    )
    op.create_table(
        "drive_imports",
        sa.Column("file_id", sa.String(200), primary_key=True),
        sa.Column("folder_id", sa.String(200), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("asset_id", sa.String(36), sa.ForeignKey("assets.id")),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("checked_at", sa.DateTime(), nullable=False),
        sa.Column("retry_at", sa.DateTime()),
    )
    op.create_index("ix_drive_imports_asset_id", "drive_imports", ["asset_id"])


def downgrade():
    op.drop_index("ix_drive_imports_asset_id", table_name="drive_imports")
    op.drop_table("drive_imports")
    op.drop_table("drive_sources")
