"""DB 엔진, 세션, 마이그레이션.

앱 테이블(``app.models``)은 Alembic 으로 관리한다. 같은 DB 의 LangGraph checkpointer
테이블은 LangGraph 가 스스로 관리하므로 여기서 다루지 않는다.
"""

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated

from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import Depends
from sqlalchemy import Connection, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

logger = logging.getLogger(__name__)

engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

BACKEND_DIR = Path(__file__).resolve().parent.parent
# Alembic 도입 전(create_all) 스키마와 같은 리비전. 기존 DB 를 Alembic 관리로 편입할 때 쓴다.
BASELINE_REVISION = "0001"


class Base(DeclarativeBase):
    """모든 ORM 모델의 기반 클래스. ``Base.metadata`` 가 Alembic 의 비교 대상이다."""


async def get_session() -> AsyncIterator[AsyncSession]:
    """요청 단위 DB 세션을 제공하는 FastAPI 의존성.

    Yields:
        요청이 끝나면 닫히는 ``AsyncSession``.
    """
    async with SessionLocal() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _alembic_config(connection: Connection) -> AlembicConfig:
    """앱의 DB 연결을 그대로 쓰는 Alembic 설정을 만든다."""
    cfg = AlembicConfig(str(BACKEND_DIR / "alembic.ini"))
    cfg.attributes["connection"] = connection  # migrations/env.py 가 이 연결을 쓴다
    return cfg


def _upgrade(connection: Connection) -> None:
    """스키마를 최신 리비전으로 올린다. Alembic 도입 전 DB 는 기준 리비전으로 표시한다."""
    cfg = _alembic_config(connection)
    tables = set(inspect(connection).get_table_names())
    if "alembic_version" not in tables and "conversations" in tables:
        # create_all 로 만든 DB: 테이블은 이미 있으므로 기준 리비전으로 표시만 한다.
        logger.warning("existing schema without alembic_version → stamping %s", BASELINE_REVISION)
        command.stamp(cfg, BASELINE_REVISION)
    command.upgrade(cfg, "head")


async def run_migrations() -> None:
    """앱 시작 시 DB 스키마를 최신 마이그레이션으로 맞춘다.

    uvicorn 워커가 하나라고 가정한다. 여러 프로세스가 동시에 실행하면 충돌할 수 있다.
    """
    async with engine.begin() as conn:
        await conn.run_sync(_upgrade)
        revision = await conn.scalar(text("select version_num from alembic_version"))
    logger.info("database schema at revision %s", revision)
