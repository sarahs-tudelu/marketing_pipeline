from alembic import context

from social import models  # noqa: F401
from social.config import Settings
from social.db import Base, make_engine

config = context.config
if context.is_offline_mode():
    context.configure(url=Settings().database_dsn, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = make_engine(Settings().database_dsn)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()
