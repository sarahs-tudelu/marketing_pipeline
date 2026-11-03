import io

import pytest
from cryptography.fernet import Fernet
from PIL import Image

from social.config import Settings
from social.db import Base, make_engine, sessions
from social.facts import load_facts
from social.pipeline import bootstrap, draft, generate
from social.storage import ingest


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        media_dir=tmp_path / "media",
        watch_dir=tmp_path / "inbox",
        token_encryption_key=Fernet.generate_key().decode(),
        session_secret="test-session-secret",
    )


@pytest.fixture
def db(settings):
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    with sessions(engine)() as session:
        bootstrap(session)
        yield session
    engine.dispose()


@pytest.fixture
def assets(db, settings):
    stream = io.BytesIO()
    Image.new("RGB", (720, 720), "green").save(stream, format="JPEG", comment=b"provenance fixture")
    asset = ingest(
        db, settings, stream.getvalue(), "company.jpg", rights="Owned by company", tags={"product": "demo"}
    )
    db.commit()
    return [asset]


@pytest.fixture
def post(db, settings, assets):
    facts = load_facts(db, settings.facts_path)
    p = draft(db, "linkedin", "test", [a.id for a in assets], facts.id, product="demo")
    generate(db, settings, p)
    return p
