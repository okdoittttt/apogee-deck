"""FastAPI 앱 진입점: 앱 생성, 미들웨어, 라우터, 시작/종료 처리."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from app.agents import runtime
from app.config import get_settings
from app.db import SessionLocal, run_migrations
from app.models import Conversation
from app.routers import chat

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """앱 시작/종료 처리.

    시작 시 DB 마이그레이션 적용 → 에이전트 런타임 시작 → UI 대화가 없는 checkpoint 정리
    순서로 실행하고, 종료 시 런타임을 정리한다.

    Yields:
        앱이 요청을 처리하는 동안 대기한다.
    """
    await run_migrations()
    await runtime.startup()
    async with SessionLocal() as session:
        conversation_ids = {str(i) for i in await session.scalars(select(Conversation.id))}
    if pruned := await runtime.prune_orphan_threads(conversation_ids):
        logger.info("pruned %d orphan checkpoint threads", pruned)
    yield
    await runtime.shutdown()


app = FastAPI(title="Agent API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.router)


@app.get("/health")
async def health() -> dict[str, str]:
    """헬스 체크.

    Returns:
        ``{"status": "ok"}``.
    """
    return {"status": "ok"}
