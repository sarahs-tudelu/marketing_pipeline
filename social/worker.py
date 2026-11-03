from contextlib import contextmanager
from pathlib import Path

import portalocker
from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import func, select, text

from .config import get_settings
from .connections import refresh_all
from .db import make_engine, now, sessions
from .models import Post
from .monitoring import alert, deliver_alerts, event, setup_logging
from .pipeline import bootstrap, generate, publish_due
from .storage import watch_folder


def advance_queue(db, settings):
    from .gate import run_gate
    from .models import Account, Control
    from .scheduler import schedule

    if not settings.auto_advance or db.get(Control, 1).killed:
        return
    posts = db.scalars(
        select(Post).where(Post.status.in_(["draft", "generated", "approved"])).limit(10)
    ).all()
    for post in posts:
        if db.get(Account, post.platform).paused:
            continue
        try:
            if post.status == "draft":
                generate(db, settings, post)
            if post.status == "generated":
                run_gate(db, settings, post)
            if post.status == "approved":
                schedule(db, settings, post)
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
    engine = make_engine(settings.database_url)
    factory = sessions(engine)
    with factory() as db:
        bootstrap(db)
    with worker_lock(engine):
        scheduler = BlockingScheduler(timezone="UTC")
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
