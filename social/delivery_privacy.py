"""Apply the Not public restriction to an existing campaign and its outstanding deliveries."""

import sqlalchemy as sa

from .db import now
from .models import MediaCampaign, MediaJob


def requires_simulation(db, post):
    if post.simulation_only:
        return True
    # Jobs completed by an older worker may predate the per-post restriction.
    source = (
        sa.select(MediaJob.id)
        .outerjoin(MediaCampaign, MediaCampaign.id == MediaJob.campaign_id)
        .where(
            MediaJob.post_id == post.id,
            (MediaJob.brief["rights"].as_string() == "not_public")
            | (MediaCampaign.brief["rights"].as_string() == "not_public"),
        )
    )
    return db.scalar(source.limit(1)) is not None


def restrict_private_posts(connection, campaign_id=None):
    """Also used by migration 6ac4f7128e30. Use its column names independently of the current ORM."""
    col = sa.column
    jobs = sa.table("media_jobs", col("post_id"), col("campaign_id"), col("brief", sa.JSON))
    campaigns = sa.table("media_campaigns", col("id"), col("brief", sa.JSON))
    posts = sa.table("posts", col("id"), col("simulation_only"))
    retries = sa.table("post_retries", col("original_post_id"), col("retry_post_id"))
    reposts = sa.table("post_reposts", col("original_post_id"), col("repost_post_id"))

    seeds = sa.select(jobs.c.post_id).where(jobs.c.post_id.is_not(None))
    if campaign_id is not None:
        seeds = seeds.where(jobs.c.campaign_id == campaign_id)
    else:
        private_campaigns = sa.select(campaigns.c.id).where(
            campaigns.c.brief["rights"].as_string() == "not_public"
        )
        seeds = seeds.where(
            (jobs.c.brief["rights"].as_string() == "not_public") | jobs.c.campaign_id.in_(private_campaigns)
        )
    # Follow explicit post relationships, including older replacement jobs without a campaign ID.
    edges = sa.union_all(
        sa.select(
            jobs.c.brief[("replaces", "post_id")].as_string().label("parent"), jobs.c.post_id.label("child")
        ).where(jobs.c.post_id.is_not(None), jobs.c.brief[("replaces", "post_id")].as_string().is_not(None)),
        sa.select(retries.c.original_post_id, retries.c.retry_post_id),
        sa.select(reposts.c.original_post_id, reposts.c.repost_post_id),
    ).subquery()
    private = seeds.cte("private_posts", recursive=True)
    private = private.union(sa.select(edges.c.child).join(private, edges.c.parent == private.c.post_id))
    connection.execute(
        posts.update()
        .where(posts.c.simulation_only.is_(False), posts.c.id.in_(sa.select(private.c.post_id)))
        .values(simulation_only=True)
    )
    reconcile_reservations(connection)


def reconcile_reservations(connection, post_id=None):
    col = sa.column
    posts = sa.table(
        "posts", col("id"), col("status"), col("simulation_only"), col("dry_run"), col("remote_id"),
        col("live_url"), col("published_at"), col("error"),
    )
    requests = sa.table("requests", col("post_id"), col("dry_run"))
    history = sa.table(
        "transitions", col("post_id"), col("from_status"), col("to_status"), col("reason"),
        col("actor"), col("created_at"),
    )
    live_request = sa.select(requests.c.post_id).where(requests.c.dry_run.is_(False))
    pending = (
        posts.c.simulation_only.is_(True)
        & (posts.c.dry_run.is_(False) | posts.c.id.in_(live_request))
        & posts.c.status.in_(["approved", "scheduled", "publishing"])
    )
    if post_id is not None:
        pending &= posts.c.id == post_id
    uncertain = (
        (posts.c.status == "publishing")
        | posts.c.id.in_(live_request)
        | posts.c.published_at.is_not(None)
        | (posts.c.remote_id != "")
        | (posts.c.live_url != "")
        | posts.c.error.startswith("ambiguous:")
    )
    hold = "ambiguous: Not public campaign. Inspect prior live delivery before taking further action."
    changed = sa.select(
        posts.c.id,
        posts.c.status,
        sa.case((uncertain, "held"), else_=posts.c.status),
        sa.case((uncertain, hold), else_="Not public campaign; reservation changed to simulated delivery."),
        sa.literal("campaign privacy"),
        sa.literal(now()),
    ).where(pending)
    connection.execute(history.insert().from_select(list(history.c), changed))
    connection.execute(posts.update().where(pending, uncertain).values(status="held", error=hold))
    connection.execute(posts.update().where(pending).values(dry_run=True))
