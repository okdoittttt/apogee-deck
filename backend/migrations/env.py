"""Alembic 실행 환경.

두 가지 경로로 실행된다.
1. CLI (`alembic upgrade head`, `alembic revision --autogenerate` 등): 여기서 엔진을 만들어 연결한다.
2. 앱 시작 시 (`app.db.run_migrations`): 앱이 넘겨준 연결(config.attributes["connection"])을 그대로 쓴다.

같은 DB 에 LangGraph checkpointer 테이블(checkpoints 등)이 있지만, 그 테이블은 LangGraph 가
스스로 관리한다. Alembic 은 app.models 에 정의된 테이블만 관리하고 나머지는 무시한다.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app import models  # noqa: F401  (모델을 metadata 에 등록)
from app.config import get_settings
from app.db import Base

config = context.config
target_metadata = Base.metadata

# 앱 안에서 실행될 때는 앱의 로깅 설정을 덮어쓰지 않는다.
if config.config_file_name is not None and not config.attributes.get("connection"):
    fileConfig(config.config_file_name)


def include_name(name: str | None, type_: str, parent_names: dict) -> bool:
    """autogenerate 대상: 우리 모델의 테이블만. (checkpointer 테이블을 '삭제'하자고 제안하지 않게)"""
    if type_ == "table":
        return name in target_metadata.tables
    return True


def _configure(connection: Connection | None = None, url: str | None = None) -> None:
    context.configure(
        connection=connection,
        url=url,
        target_metadata=target_metadata,
        include_name=include_name,
        compare_type=True,
        compare_server_default=True,
        literal_binds=connection is None,
        dialect_opts={"paramstyle": "named"} if connection is None else {},
    )


def run_migrations_offline() -> None:
    """DB 연결 없이 SQL 만 출력 (`alembic upgrade head --sql`)."""
    _configure(url=get_settings().database_url)
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = get_settings().database_url
    engine = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:  # 앱이 넘겨준 연결 (이미 이벤트 루프 안)
        do_run_migrations(connection)
    else:
        asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
