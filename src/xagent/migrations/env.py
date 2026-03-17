import os
import sys
from logging.config import fileConfig

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

# Load environment variables from .env file
load_dotenv()

# Add the parent directory to the path so we can import our modules
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from xagent.core.storage import get_default_db_url

# Import all models to ensure they are registered with Base.metadata
# Type checking is disabled for these imports as they are dynamically loaded by Alembic
# flake8: noqa: E402
from xagent.web import models as web_models  # noqa: F401
from xagent.web.models.database import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Use our models' MetaData for autogenerate support
target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    if url is None:
        # Respect DATABASE_URL environment variable
        url = os.getenv("DATABASE_URL")
        if url is None:
            url = get_default_db_url()

    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    # Check if connection is provided via config.attributes
    connection = config.attributes.get("connection", None)

    if connection is not None:
        # Use provided connection
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    else:
        # Fallback: create new connection using URL from config
        configuration = config.get_section(config.config_ini_section, {})
        if configuration.get("sqlalchemy.url") is None:
            # Respect DATABASE_URL environment variable
            url = os.getenv("DATABASE_URL")
            if url is None:
                url = get_default_db_url()
            configuration["sqlalchemy.url"] = url

        connectable = engine_from_config(
            configuration,
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )

        with connectable.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata)

            with context.begin_transaction():
                context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
