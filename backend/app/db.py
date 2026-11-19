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


class Base(DeclarativeBase):
    pass


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


BACKEND_DIR = Path(__file__).resolve().parent.parent
# Alembic 도입 전(create_all) 스키마와 같은 리비전. 기존 DB 를 Alembic 관리로 편입할 때 쓴다.
BASELINE_REVISION = "0001"


def _alembic_config(connection: Connection) -> AlembicConfig:
    cfg = AlembicConfig(str(BACKEND_DIR / "alembic.ini"))
    cfg.attributes["connection"] = connection  # env.py 가 이 연결을 그대로 쓴다
    return cfg


def _upgrade(connection: Connection) -> None:
    cfg = _alembic_config(connection)
    tables = set(inspect(connection).get_table_names())
    if "alembic_version" not in tables and "conversations" in tables:
        # Alembic 도입 전에 create_all 로 만든 DB: 테이블은 이미 있으므로 기준 리비전으로 표시만 한다.
        logger.warning("existing schema without alembic_version → stamping %s", BASELINE_REVISION)
        command.stamp(cfg, BASELINE_REVISION)
    command.upgrade(cfg, "head")


async def run_migrations() -> None:
    """앱 시작 시 DB 스키마를 최신 마이그레이션으로 맞춘다 (uvicorn 워커 1개 기준)."""
    async with engine.begin() as conn:
        await conn.run_sync(_upgrade)
        revision = await conn.scalar(text("select version_num from alembic_version"))
    logger.info("database schema at revision %s", revision)
