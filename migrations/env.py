from logging.config import fileConfig
from alembic import context
from sqlalchemy import engine_from_config,pool
from backend import config as settings,store,enterprise_state,bulk  # noqa: ensure model registration
cfg=context.config
cfg.set_main_option('sqlalchemy.url',settings.DATABASE_URL.replace('%','%%'))
target_metadata=store.Base.metadata

def run_migrations_online():
    with store.engine.connect() as connection:
        context.configure(connection=connection,target_metadata=target_metadata,
                          compare_type=True,transactional_ddl=True)
        with context.begin_transaction():context.run_migrations()

if context.is_offline_mode():
    raise RuntimeError('Only online migrations are supported; use a controlled DB migration task')
else:run_migrations_online()
