"""FastAPI 앱 진입점: 앱 생성, 미들웨어, 라우터, 시작/종료 처리."""

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.agents import AGENTS, DEFAULT_AGENT
from app.api.routers import admin, approvals, auth, chat
from app.api.routers import documents as documents_router
from app.api.routers import memory as memory_router
from app.api.routers import projects as projects_router
from app.conversations import runs
from app.core.config import get_settings
from app.core.db import SessionLocal, run_migrations
from app.core.storage import get_storage
from app.documents import service as documents
from app.models import Conversation
from app.orchestration import runtime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class _RedactApprovalTokens(logging.Filter):
    """접근 로그에서 승인·거절 링크의 토큰을 가린다 (로그를 본 사람이 링크를 쓰지 못하게)."""

    _pattern = re.compile(r"(/api/auth/approvals/)[^\s?\"]+")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                self._pattern.sub(r"\1***", a) if isinstance(a, str) else a for a in record.args
            )
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactApprovalTokens())


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """앱 시작/종료 처리.

    시작 시 DB 마이그레이션 적용 → 첨부 저장소 준비 → 파싱 중이던 문서를 실패로 표시 →
    에이전트 런타임 시작 → UI 대화가 없는 checkpoint 정리 순서로 실행하고, 문서 정리 작업(하루 한 번)을
    띄운다. 종료 시 정리 작업, 진행 중인 응답(중단으로 저장), 런타임을 정리한다.

    첨부 저장소를 준비하지 못해도 앱은 시작한다 (첨부만 쓸 수 없다).

    Yields:
        앱이 요청을 처리하는 동안 대기한다.
    """
    await run_migrations()
    await prepare_storage()
    if interrupted := await documents.mark_interrupted():
        logger.info("marked %d interrupted document parses as failed", interrupted)
    await runtime.startup(AGENTS, DEFAULT_AGENT)
    async with SessionLocal() as session:
        conversation_ids = {str(i) for i in await session.scalars(select(Conversation.id))}
    if pruned := await runtime.prune_orphan_threads(conversation_ids):
        logger.info("pruned %d orphan checkpoint threads", pruned)
    cleanup = asyncio.create_task(documents.cleanup_loop())
    # 검색 기능 전에 올린 프로젝트 문서의 청크를 만든다 (임베딩이 있어 시간이 걸리므로 뒤에서).
    backfill = asyncio.create_task(documents.index_missing_documents())
    yield
    backfill.cancel()
    cleanup.cancel()
    # 진행 중인 응답을 중단으로 저장한 뒤(checkpointer 를 쓸 수 있을 때) 런타임을 닫는다.
    await runs.shutdown_runs()
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


app = FastAPI(title="okdoitttt API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_credentials=True,  # 화면(다른 포트)이 세션 쿠키를 붙여 요청한다
    allow_methods=["*"],
    allow_headers=["*"],
)

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@app.middleware("http")
async def check_origin(request: Request, call_next):
    """상태를 바꾸는 요청이 다른 사이트에서 왔으면 거절한다 (CSRF 방어).

    세션 쿠키는 ``SameSite=Lax`` 라 다른 사이트의 POST 에는 붙지 않지만, 한 번 더 막는다.
    브라우저는 이런 요청에 ``Origin`` 을 붙인다. ``Origin`` 이 없는 요청(curl 등)은 쿠키를
    자동으로 붙이지 않으므로 통과시킨다.
    """
    origin = request.headers.get("origin")
    if request.method in UNSAFE_METHODS and origin:
        settings = get_settings()
        allowed = {*settings.cors_origin_list, settings.backend_public_url.rstrip("/")}
        if origin.rstrip("/") not in allowed:
            return JSONResponse({"detail": "허용되지 않은 출처의 요청입니다."}, status_code=403)
    return await call_next(request)


app.include_router(auth.router)
app.include_router(approvals.router)
app.include_router(admin.router)
app.include_router(chat.router)
app.include_router(documents_router.router)
app.include_router(projects_router.router)
app.include_router(memory_router.router)


@app.get("/health")
async def health() -> dict[str, str]:
    """헬스 체크.

    Returns:
        ``{"status": "ok"}``.
    """
    return {"status": "ok"}
