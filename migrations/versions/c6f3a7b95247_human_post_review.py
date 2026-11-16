"""Require human review for user-created posts, including existing pending drafts."""

from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "c6f3a7b95247"
down_revision = "b9d2f5a84136"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("posts", sa.Column("human_review_required", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("posts", sa.Column("human_approved_digest", sa.String(64), nullable=False, server_default=""))
    connection = op.get_bind()
    posts = sa.table("posts", sa.column("id"), sa.column("status"), sa.column("scheduled_at"), sa.column("next_attempt_at"), sa.column("human_review_required"))
    jobs = sa.table("media_jobs", sa.column("post_id"), sa.column("campaign_id"))
    connection.execute(posts.update().where(posts.c.id.in_(sa.select(jobs.c.post_id).where(jobs.c.campaign_id.is_not(None)))).values(human_review_required=False))
    history = sa.table("transitions", sa.column("post_id"), sa.column("from_status"), sa.column("to_status"), sa.column("reason"), sa.column("actor"), sa.column("created_at", sa.DateTime()))
    pending = connection.execute(sa.select(posts.c.id, posts.c.status).where(posts.c.human_review_required == sa.true(), posts.c.status.in_(["approved", "scheduled"]))).all()
    for post_id, status in pending:
        connection.execute(history.insert().values(post_id=post_id, from_status=status, to_status="awaiting_review", reason="Human review required for existing user-created post", actor="migration", created_at=datetime.now(timezone.utc).replace(tzinfo=None)))
        connection.execute(posts.update().where(posts.c.id == post_id).values(status="awaiting_review", scheduled_at=None, next_attempt_at=None))


def downgrade():
    # Never resume pending delivery as a side effect of rolling back the schema.
    op.execute("UPDATE posts SET status='held' WHERE status='awaiting_review'")
    op.drop_column("posts", "human_approved_digest")
    op.drop_column("posts", "human_review_required")
