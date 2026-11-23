"""Navigation for reviewing posts and browsing their delivery history."""

from urllib.parse import urlencode

from sqlalchemy import func, select, tuple_

from .media_queue import DELIVERY_STATES, campaign_attempts, queue_posts
from .models import MediaJob, Post

LIST_PAGES = {
    "dashboard": ("/", "Overview"),
    "queues": ("/admin/queues", "Needs attention"),
    "media-queue": ("/admin/media-queue", "In progress"),
    "published-posts": ("/admin/published-posts", "Published posts"),
    "analytics": ("/admin/analytics", "Performance"),
}


def source_query(source, campaign_id=None):
    if source not in LIST_PAGES:
        return ""
    params = {"source": source}
    if campaign_id and source in {"media-queue", "published-posts"}:
        params["campaign_id"] = campaign_id
    return "?" + urlencode(params)


def delivery_group(post):
    if post.status == "approved":
        return "simulated" if post.simulation_only else "unscheduled"
    return "simulated" if post.dry_run else "real"


def published_location(post, campaign_id):
    return (
        list_location("published-posts", campaign_id)
        + f"#{delivery_group(post)}-{post.platform}"
    )


def list_location(source, campaign_id=None):
    path, _ = LIST_PAGES[source]
    if campaign_id and source in {"media-queue", "published-posts"}:
        path += "?" + urlencode({"campaign_id": campaign_id})
    return path


def workspace_navigation(db, post, params):
    source = params.get("source")
    query = source_query(source, params.get("campaign_id"))
    if not query:
        source = "published-posts" if post.status in DELIVERY_STATES else "media-queue"
    campaign_id = (
        params.get("campaign_id") if query and source in {"media-queue", "published-posts"} else None
    )
    back = list_location(source, campaign_id)
    if source == "published-posts" and post.status in DELIVERY_STATES:
        back = published_location(post, campaign_id)
    context = {
        "post_query": query,
        "back_url": back,
        "back_label": LIST_PAGES[source][1],
        "post_source": source,
    }
    if post.status != "awaiting_review":
        return context
    order = (Post.created_at, Post.id)
    slides = (
        queue_posts(
            Post.id,
            func.row_number().over(order_by=order).label("position"),
            func.count().over().label("total"),
            func.lag(Post.id).over(order_by=order).label("previous"),
            func.lead(Post.id).over(order_by=order).label("next"),
            campaign_id=campaign_id,
            review_only=True,
        )
        .order_by(None)
        .subquery()
    )
    slide = db.execute(select(slides).where(slides.c.id == post.id)).mappings().first()
    if slide:
        context["slides"] = dict(slide, query=query or source_query(source))
    return context


def next_review(db, post, campaign_id):
    query = queue_posts(Post.id, campaign_id=campaign_id, review_only=True)
    later = query.where(tuple_(Post.created_at, Post.id) > (post.created_at, post.id))
    return db.scalar(later.limit(1)) or db.scalar(query.limit(1))


def published_groups(db, campaign_id=None):
    query = select(
        Post.id,
        Post.campaign,
        Post.platform,
        Post.status,
        Post.dry_run,
        Post.simulation_only,
        Post.scheduled_at,
        Post.published_at,
        Post.live_url,
    ).where(Post.status.in_(DELIVERY_STATES))
    if campaign_id:
        query = query.where(Post.id.in_(select(MediaJob.post_id).where(campaign_attempts(campaign_id))))
    query = query.order_by(
        Post.platform, func.coalesce(Post.published_at, Post.scheduled_at, Post.created_at).desc(), Post.id
    )
    groups = {"unscheduled": {}, "real": {}, "simulated": {}}
    for post in db.execute(query):
        groups[delivery_group(post)].setdefault(post.platform, []).append(post)
    return groups
