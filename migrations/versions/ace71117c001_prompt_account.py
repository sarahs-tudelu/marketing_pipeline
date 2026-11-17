"""Record the authenticated account that submitted the original prompt."""
import sqlalchemy as sa
from alembic import op

revision = "ace71117c001"
down_revision = "d8e4b2c61709"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("posts", sa.Column("prompted_by", sa.String(200), nullable=True))
    op.add_column("posts", sa.Column("prompted_by_label", sa.String(320), nullable=True))
    op.create_index("ix_posts_prompted_by", "posts", ["prompted_by"])


def downgrade():
    op.drop_index("ix_posts_prompted_by", table_name="posts")
    op.drop_column("posts", "prompted_by_label")
    op.drop_column("posts", "prompted_by")
