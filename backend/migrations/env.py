"""Alembic 실행 환경.

두 가지 경로로 실행된다.

1. CLI (``alembic upgrade head``, ``alembic revision --autogenerate`` 등): 여기서 엔진을 만들어
   연결한다.
2. 앱 시작 시 (``app.db.run_migrations``): 앱이 넘겨준 연결(``config.attributes["connection"]``)을
   그대로 쓴다.

같은 DB 에 LangGraph checkpointer 테이블(``checkpoints`` 등)이 있지만, 그 테이블은 LangGraph 가
스스로 관리한다. Alembic 은 ``app.models`` 에 정의된 테이블만 관리하고 나머지는 무시한다.
"""

import asyncio
from logging.config import fileConfig
from typing import Any

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


def include_name(name: str | None, type_: str, parent_names: dict[str, Any]) -> bool:
    """Autogenerate 비교 대상인지 판단한다.

    우리 모델의 테이블만 대상으로 삼아서, checkpointer 테이블을 "삭제하자"고 제안하지 않게 한다.

    Args:
        name: 객체 이름.
        type_: 객체 종류 (``"table"``, ``"column"``, ``"index"`` 등).
        parent_names: 상위 객체 이름 (Alembic 이 넘겨줌).

    Returns:
        테이블이면 ``app.models`` 에 정의된 것일 때만 ``True``, 그 밖의 객체는 ``True``.
    """
    if type_ == "table":
        return name in target_metadata.tables
    return True


def _configure(connection: Connection | None = None, url: str | None = None) -> None:
    """Alembic 컨텍스트를 설정한다 (연결이 없으면 offline 모드)."""
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
    """DB 연결 없이 SQL 만 출력한다 (``alembic upgrade head --sql``)."""
    _configure(url=get_settings().database_url)
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """주어진 연결로 마이그레이션을 실행한다.

    Args:
        connection: 동기 SQLAlchemy 연결.
    """
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """앱 설정의 DB URL 로 비동기 엔진을 만들어 마이그레이션을 실행한다 (CLI 경로)."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = get_settings().database_url
    engine = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    """DB 에 연결해 마이그레이션을 실행한다.

    앱이 넘겨준 연결이 있으면(이미 이벤트 루프 안) 그대로 쓰고, 없으면 새로 연결한다.
    """
    connection = config.attributes.get("connection")
    if connection is not None:
        do_run_migrations(connection)
    else:
        asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
