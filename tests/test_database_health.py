import logging
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from social.app import create_app
from social.db import make_engine, schema_gaps


def test_health_detects_unapplied_availability_migration_and_recovers(settings):
    config = Config("alembic.ini")
    with patch("social.config.Settings", return_value=settings):
        command.upgrade(config, "c6f3a7b95247")
        engine = make_engine(settings.database_dsn)
        try:
            with TestClient(create_app(settings)) as client:
                response = client.get("/health")
                assert response.status_code == 503
                assert "migration required" in response.json()["detail"]
                assert schema_gaps(engine) == {
                    # Usage tables came later, with f9c2a1e7d340.
                    "missing_tables": ["availability_checks", "post_availability", "post_reposts",
                                       "provider_costs", "provider_syncs", "usage_records"],
                    # Added after the availability tables, by ace71117c001 (prompt account) and
                    # b4e8d2f61a93 (deleted library files kept for posts).
                    "missing_columns": {"assets": ["removed_at", "removed_by"],
                                        "media_jobs": ["dismissed_at"],
                                        "posts": ["prompted_by", "prompted_by_label", "simulation_only"]},
                }
                with engine.connect() as connection:
                    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "c6f3a7b95247"
                    controls_before = connection.execute(text("SELECT * FROM controls")).all()

                command.upgrade(config, "head")
                command.upgrade(config, "head")  # Repeating the release step is safe.

                assert client.get("/health").json() == {"status": "ok"}
                assert client.get("/").status_code == 200
                assert client.get("/api/media-jobs").status_code == 200
                with engine.connect() as connection:
                    # Every later migration is applied too, not just the availability one.
                    head = ScriptDirectory.from_config(config).get_current_head()
                    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == head
                    assert connection.execute(text("SELECT * FROM controls")).all() == controls_before
                command.check(config)
        finally:
            engine.dispose()


def test_health_detects_missing_column(db, settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200
        with db.get_bind().begin() as connection:
            connection.execute(text("ALTER TABLE post_availability DROP COLUMN reason"))
        assert schema_gaps(db.get_bind()) == {
            "missing_tables": [],
            "missing_columns": {"post_availability": ["reason"]},
        }
        assert client.get("/health").status_code == 503


def test_empty_database_is_not_healthy(settings):
    engine = make_engine(settings.database_dsn)
    try:
        gaps = schema_gaps(engine)
        assert "controls" in gaps["missing_tables"]
        assert "posts" in gaps["missing_tables"]
    finally:
        engine.dispose()


def test_health_database_error_hides_connection_and_query_details(db, settings, monkeypatch, caplog):
    def unavailable(_):
        raise OperationalError("sensitive query", {"password": "private-secret"}, RuntimeError("private-host"))

    with TestClient(create_app(settings)) as client:
        monkeypatch.setattr("social.app.schema_gaps", unavailable)
        with caplog.at_level(logging.INFO, logger="social"):
            response = client.get("/health")
        assert response.status_code == 503
        assert response.json() == {"status": "not_ready", "detail": "Database unavailable."}
        assert "OperationalError" in caplog.text
        for private in ("sensitive query", "private-secret", "private-host"):
            assert private not in response.text + caplog.text
