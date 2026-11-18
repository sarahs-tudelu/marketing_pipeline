from collections import Counter, defaultdict, deque
from datetime import timedelta

from sqlalchemy import select

from .adapters import adapter
from .config import profiles
from .db import now
from .models import Engagement, Post, RequestRecord
from .monitoring import alert


def flatten_insights(payload):
    result = {}
    for metric in payload.get("data", []):
        values = metric.get("values", [])
        result[metric["name"]] = values[-1]["value"] if values else metric.get("total_value", {}).get("value")
    return result


def fetch(db, settings, post, client=None):
    if post.dry_run:
        return {"simulated": True, "views": 0, "interactions": 0}
    a = adapter(settings, db, post, client)
    if not a.connected():
        raise ValueError("Account not connected")
    remote = post.remote_id
    if post.platform == "instagram":
        return flatten_insights(
            a.direct(
                "GET", f"{a.base}/{remote}/insights", params={"metric": "views,reach,total_interactions"}
            )
        )
    if post.platform == "facebook" and db.scalar(
        select(RequestRecord.id).where(RequestRecord.post_id == post.id, RequestRecord.step == "reel-start")
    ):
        # Reels are video objects; Meta documents these lifetime Reels metrics on video_insights.
        return flatten_insights(
            a.direct(
                "GET",
                f"{a.base}/{remote}/video_insights",
                params={
                    "metric": "fb_reels_total_plays,post_impressions_unique,"
                    "post_video_likes_by_reaction_type,post_video_social_actions"
                },
            )
        )
    if post.platform == "facebook":
        return flatten_insights(
            a.direct(
                "GET",
                f"{a.base}/{remote}/insights",
                params={"metric": "post_media_view,post_reactions_by_type_total"},
            )
        )
    if post.platform == "linkedin":
        key = "ugcPosts" if remote.startswith("urn:li:ugcPost:") else "shares"
        result = a.direct(
            "GET",
            f"{a.base}/organizationalEntityShareStatistics",
            params={"q": "organizationalEntity", "organizationalEntity": a.owner, key: f"List({remote})"},
        )
        rows = result.get("elements", [])
        return rows[0]["totalShareStatistics"] if rows else {"unavailable": True}
    if post.platform == "pinterest":
        return {"unavailable": True, "reason": "Pinterest metrics aren't collected yet"}
    if post.platform == "x":
        return a.direct("GET", f"{a.base}/tweets/{remote}", params={"tweet.fields": "public_metrics"})[
            "data"
        ]["public_metrics"]
    status = a.get_status(remote)["data"]
    ids = status.get("publicaly_available_post_id", [])
    if not ids:
        return {"unavailable": True, "reason": "No public TikTok video yet"}
    result = a.direct(
        "POST",
        f"{a.base}/video/query/",
        params={"fields": "id,view_count,like_count,comment_count,share_count"},
        json={"filters": {"video_ids": [str(id) for id in ids]}},
    )
    return {"videos": result["data"]["videos"]}


def collect(db, settings):
    at = now()
    recently_collected = (
        select(Engagement.id)
        .where(Engagement.post_id == Post.id, Engagement.fetched_at > at - timedelta(hours=24))
        .exists()
    )
    due = select(Post).where(
        Post.status == "published", Post.published_at >= at - timedelta(days=90), ~recently_collected
    )
    for post_id in db.scalars(due.with_only_columns(Post.id)).all():
        # The CLI and worker can both collect while a slow platform request is in flight.
        post = db.scalar(due.where(Post.id == post_id))
        if post is None:
            continue
        try:
            metrics = fetch(db, settings, post)
            db.add(Engagement(post_id=post.id, metrics=metrics, simulated=post.dry_run))
        except Exception as exc:
            alert(
                db,
                "analytics_failure",
                f"{post.platform}: insights unavailable for {post.id} ({type(exc).__name__}); check insights permissions",
                f"insights:{post.id}:{now().date()}",
            )
        db.commit()


def published_with_metrics():
    """One row per published post, including posts that have never been measured."""
    latest_id = (
        select(Engagement.id)
        .where(Engagement.post_id == Post.id)
        .order_by(Engagement.fetched_at.desc(), Engagement.id.desc())
        .limit(1)
        .correlate(Post)
        .scalar_subquery()
    )
    return (
        select(Post, Engagement)
        .outerjoin(Engagement, Engagement.id == latest_id)
        .where(Post.status == "published")
    )


def report(db, settings):
    rows = []
    measured = Counter()
    recent_rates = defaultdict(lambda: deque(maxlen=10))
    query = (
        published_with_metrics()
        .with_only_columns(
            Post.id, Post.platform, Post.caption, Post.dry_run, Engagement.metrics, Engagement.simulated
        )
        .order_by(Post.published_at)
    )
    for row in db.execute(query.execution_options(yield_per=500)):
        rows.append(
            {
                "post_id": row.id,
                "platform": row.platform,
                "caption": row.caption,
                "simulated": row.dry_run,
                "metrics": row.metrics,
            }
        )
        if row.metrics is not None and not row.simulated and not row.metrics.get("unavailable"):
            measured[row.platform] += 1
            rate = engagement_rate(row.metrics)
            if rate is not None:
                recent_rates[row.platform].append(rate)
    recommendations = []
    for platform, profile in profiles(settings).items():
        rates = list(recent_rates[platform])
        if len(rates) < 10:
            suggestion = (
                "Keep current cadence; fewer than 10 live posts have comparable views and engagement."
            )
        else:
            recent = sum(rates[-5:]) / 5
            previous = sum(rates[-10:-5]) / 5
            if previous > 0 and recent < previous * 0.75:
                suggestion = "Engagement per view fell over 25% across the latest five measured posts. Consider one fewer promotional slot per week and compare content quality before changing frequency."
            elif previous > 0 and recent > previous * 1.25:
                suggestion = "Engagement per view rose over 25% across the latest five measured posts. Keep the cap and test a different allowed posting time."
            else:
                suggestion = "Engagement per view is broadly stable. Keep the current cadence and test content mix within the existing cap."
        recommendations.append(
            {
                "platform": platform,
                "measured_posts": measured[platform],
                "hard_cap": profile.hard_cap,
                "suggestion": suggestion,
                "automatically_applied": False,
            }
        )
    return {"posts": rows, "recommendations": recommendations}


def engagement_rate(metrics):
    views, actions = reach(metrics)
    return actions / views if views else None


def reach(metrics):
    """(views, interactions) from one platform's metrics; (0, 0) when it reports neither."""
    if "videos" in metrics:
        rows = metrics["videos"]
        views = sum(m.get("view_count", 0) for m in rows)
        actions = sum(sum(m.get(k, 0) for k in ("like_count", "comment_count", "share_count")) for m in rows)
    else:
        views = next(
            (
                metrics[k]
                for k in ("views", "impressionCount", "impression_count", "post_media_view")
                if isinstance(metrics.get(k), (int, float))
            ),
            0,
        )
        actions = metrics.get("total_interactions")
        if actions is None:
            actions = sum(
                metrics.get(k, 0)
                for k in (
                    "likeCount",
                    "commentCount",
                    "shareCount",
                    "clickCount",
                    "like_count",
                    "reply_count",
                    "retweet_count",
                    "quote_count",
                )
            )
            actions += sum(metrics.get("post_reactions_by_type_total", {}).values())
    return views or 0, actions or 0
