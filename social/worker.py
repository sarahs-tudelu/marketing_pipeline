from contextlib import contextmanager
from pathlib import Path

import portalocker
from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import case, func, select, text

from .automation import can_automate
from .config import get_settings
from .connections import import_meta_from_env, refresh_all
from .db import make_engine, now, sessions
from .lifecycle import PostConflict
from .models import MediaJob, Post, PostRetry
from .monitoring import alert, deliver_alerts, event, setup_logging
from .pipeline import bootstrap, generate, publish_due
from .storage import watch_folder


def advance_queue(db, settings):
    from .gate import run_gate
    from .models import Account
    from .scheduler import schedule

    if not can_automate(db, settings, requested=True):
        return
    active = ["draft", "generated", "approved"]
    requested = set(db.scalars(
        select(MediaJob.post_id).join(Post, Post.id == MediaJob.post_id).where(
            MediaJob.state == "complete", MediaJob.brief["workflow"].as_string() == "immediate",
            Post.status.in_(active),
        )
    ))
    retries = set(db.scalars(select(PostRetry.retry_post_id).join(Post, Post.id == PostRetry.retry_post_id).where(
        Post.status.in_(active),
    )))
    requested.update(retries)
    query = select(Post).where(Post.status.in_(active))
    if not can_automate(db, settings):
        query = query.where(Post.id.in_(requested))
    posts = db.scalars(query.order_by(case((Post.id.in_(requested), 0), else_=1), Post.created_at).limit(10)).all()
    for post in posts:
        explicit = post.id in requested
        if not can_automate(db, settings, requested=explicit):
            continue
        if db.get(Account, post.platform).paused:
            continue
        try:
            if post.status == "draft":
                generate(db, settings, post)
            if post.status == "generated" and can_automate(db, settings, requested=explicit):
                run_gate(db, settings, post, **({"live": not post.dry_run} if post.id in retries else {}))
            if post.status == "approved" and can_automate(db, settings, requested=explicit):
                schedule(db, settings, post)
        except PostConflict:
            db.rollback()  # The admin or another transaction is already handling this post.
        except ValueError:
            db.rollback()  # Capacity/config errors leave the approved post available.
            alert(
                db,
                "scheduling",
                f"No eligible schedule for post {post.id}; inspect caps, mix and configuration",
                f"schedule:{post.id}:{now().date()}",
            )
            db.commit()


@contextmanager
def worker_lock(engine):
    if engine.dialect.name == "postgresql":
        with engine.connect() as connection:
            if not connection.scalar(text("SELECT pg_try_advisory_lock(805148229)")):
                raise RuntimeError("Another publishing worker is active")
            try:
                yield
            finally:
                connection.execute(text("SELECT pg_advisory_unlock(805148229)"))
    else:
        Path("data").mkdir(exist_ok=True)
        with portalocker.Lock("data/worker.lock", timeout=0):
            yield


def tick(factory, settings):
    with factory() as db:
        refresh_all(db, settings)
        watch_folder(db, settings)
        from .media_studio import activate_planned_jobs, process_jobs, replenish_campaigns

        activate_planned_jobs(db, settings)
        replenish_campaigns(db, settings)
        process_jobs(db, settings)
        advance_queue(db, settings)
        publish_due(db, settings)
        held = db.scalar(select(func.count()).select_from(Post).where(Post.status == "held"))
        if held >= settings.held_alert_threshold:
            alert(db, "held_buildup", f"{held} posts are held for correction", "held:" + str(now().date()))
            db.commit()
        if settings.analytics_enabled:
            from .analytics import collect

            collect(db, settings)
        deliver_alerts(db, settings)


def run():
    settings = get_settings()
    setup_logging()
    engine = make_engine(settings.database_dsn)
    factory = sessions(engine)
    with worker_lock(engine):
        with factory() as db:
            bootstrap(db)
            import_meta_from_env(db, settings)
        scheduler = BlockingScheduler(timezone="UTC")
        from .drive_import import tick as drive_tick

        scheduler.add_job(
            drive_tick,
            "interval",
            seconds=settings.worker_interval_seconds,
            args=[factory, settings],
            max_instances=1,
            coalesce=True,
            next_run_time=now(),
        )
        scheduler.add_job(
            tick,
            "interval",
            seconds=settings.worker_interval_seconds,
            args=[factory, settings],
            max_instances=1,
            coalesce=True,
            next_run_time=now(),
        )
        event("worker_started", interval=settings.worker_interval_seconds)
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            scheduler.shutdown(wait=True)
