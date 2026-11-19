import logging
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


@asynccontextmanager
async def lifespan(_: FastAPI):
    await run_migrations()
    await runtime.startup()
    async with SessionLocal() as session:
        conversation_ids = {str(i) for i in await session.scalars(select(Conversation.id))}
    if pruned := await runtime.prune_orphan_threads(conversation_ids):
        logging.getLogger(__name__).info("pruned %d orphan checkpoint threads", pruned)
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
async def health():
    return {"status": "ok"}
