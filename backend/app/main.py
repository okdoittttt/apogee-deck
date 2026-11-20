"""FastAPI 앱 진입점: 앱 생성, 미들웨어, 라우터, 시작/종료 처리."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from app.agents import runtime
from app.config import get_settings
from app.db import SessionLocal, run_migrations
from app.documents import service as documents
from app.models import Conversation
from app.routers import chat
from app.routers import documents as documents_router
from app.storage import get_storage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """앱 시작/종료 처리.

    시작 시 DB 마이그레이션 적용 → 첨부 저장소 준비 → 파싱 중이던 문서를 실패로 표시 →
    에이전트 런타임 시작 → UI 대화가 없는 checkpoint 정리 순서로 실행하고, 문서 정리 작업(하루 한 번)을
    띄운다. 종료 시 정리 작업과 런타임을 정리한다.

    첨부 저장소를 준비하지 못해도 앱은 시작한다 (첨부만 쓸 수 없다).

    Yields:
        앱이 요청을 처리하는 동안 대기한다.
    """
    await run_migrations()
    await prepare_storage()
    if interrupted := await documents.mark_interrupted():
        logger.info("marked %d interrupted document parses as failed", interrupted)
    await runtime.startup()
    async with SessionLocal() as session:
        conversation_ids = {str(i) for i in await session.scalars(select(Conversation.id))}
    if pruned := await runtime.prune_orphan_threads(conversation_ids):
        logger.info("pruned %d orphan checkpoint threads", pruned)
    cleanup = asyncio.create_task(documents.cleanup_loop())
    yield
    cleanup.cancel()
    await runtime.shutdown()


async def prepare_storage() -> None:
    """첨부 저장소의 버킷과 보관 기간 규칙을 준비한다. 실패하면 기록만 남긴다."""
    storage = get_storage()
    if storage is None:
        logger.info("attachment storage is not configured; attachments are disabled")
        return
    try:
        await storage.ensure_bucket()
    except Exception:
        logger.exception("attachment storage is not available; attachments will fail")
    else:
        logger.info(
            "attachment storage ready (bucket=%s, retention=%d days)",
            storage.bucket,
            storage.retention_days,
        )


app = FastAPI(title="Agent API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.router)
app.include_router(documents_router.router)


@app.get("/health")
async def health() -> dict[str, str]:
    """헬스 체크.

    Returns:
        ``{"status": "ok"}``.
    """
    return {"status": "ok"}
