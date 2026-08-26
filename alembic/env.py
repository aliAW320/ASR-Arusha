import asyncio
from logging.config import fileConfig
from pathlib import Path
import sys

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config


project_root = Path(__file__).resolve().parents[1]
backend_root = project_root / "fast_Backend"
sys.path.insert(0, str(backend_root if backend_root.exists() else project_root))

from app.config import get_settings  # noqa: E402
from app.database import Base  # noqa: E402
from app import models  # noqa: E402,F401


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)
target_metadata = Base.metadata
ENUM_CHECK_CONSTRAINTS = {
    "external_integration_kind",
    "meeting_artifact_type",
    "meeting_member_role",
    "model_runtime_kind",
    "model_task_type",
    "processing_attempt_status",
    "processing_parameter_type",
    "processing_stage",
    "result_artifact_type",
    "user_role",
    "voice_status",
}


def include_object(object_, name, type_, reflected, compare_to):
    # PostgreSQL reflects string-backed Enum CHECKs as standalone constraints,
    # while SQLAlchemy keeps them attached to Enum types. Ignore only this
    # known reflection mismatch; all other constraints remain drift-checked.
    if (
        type_ == "check_constraint"
        and reflected
        and compare_to is None
        and name in ENUM_CHECK_CONSTRAINTS
    ):
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
