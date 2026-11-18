"""Record paid AI requests and the providers' billed daily costs for Settings → Usage."""
import sqlalchemy as sa
from alembic import op

revision = "f9c2a1e7d340"
down_revision = "ace71117c001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "usage_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("purpose", sa.String(60), nullable=False),
        sa.Column("model", sa.String(120), nullable=False),
        sa.Column("units", sa.JSON(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("estimated", sa.Boolean(), nullable=False),
        sa.Column("request_id", sa.String(120), nullable=False),
        sa.Column("post_id", sa.String(36), nullable=True),
        sa.Column("media_job_id", sa.String(36), nullable=True),
        sa.Column("platform", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    for column in ("provider", "request_id", "post_id", "media_job_id", "created_at"):
        op.create_index(f"ix_usage_records_{column}", "usage_records", [column])
    op.create_table(
        "provider_costs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("item", sa.String(200), nullable=False),
        sa.Column("amount_usd", sa.Float(), nullable=False),
        sa.Column("units", sa.JSON(), nullable=False),
        sa.Column("synced_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("provider", "day", "item"),
    )
    op.create_index("ix_provider_costs_day", "provider_costs", ["day"])
    op.create_table(
        "provider_syncs",
        sa.Column("provider", sa.String(20), primary_key=True),
        sa.Column("synced_at", sa.DateTime(), nullable=True),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
    )


def downgrade():
    op.drop_table("provider_syncs")
    op.drop_index("ix_provider_costs_day", table_name="provider_costs")
    op.drop_table("provider_costs")
    for column in ("provider", "request_id", "post_id", "media_job_id", "created_at"):
        op.drop_index(f"ix_usage_records_{column}", table_name="usage_records")
    op.drop_table("usage_records")
