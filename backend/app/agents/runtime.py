"""에이전트 레지스트리, 체크포인터 생명주기, 스트리밍 실행.

라우터는 이 모듈의 stream_reply 만 사용한다. 새 에이전트는 app/agents/<이름>/ 에
build_graph / STREAM_NODES 를 만들고 아래 AGENTS 에 등록하면 된다.
"""

import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app import prompts, tracing
from app.agents import chat
from app.config import Settings, get_settings
from app.llm import model_name

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentSpec:
    build_graph: Callable[[Settings, BaseCheckpointSaver], CompiledStateGraph]
    stream_nodes: frozenset[str]  # 이 노드들이 생성한 토큰만 사용자에게 스트리밍


AGENTS: dict[str, AgentSpec] = {
    "chat": AgentSpec(chat.build_graph, chat.STREAM_NODES),
}
DEFAULT_AGENT = "chat"

_pool: AsyncConnectionPool | None = None
_checkpointer: BaseCheckpointSaver | None = None
_graphs: dict[str, CompiledStateGraph] = {}


async def startup() -> None:
    global _pool, _checkpointer
    settings = get_settings()
    # 잘못된 프롬프트(빠진 include, 변수 불일치)는 첫 요청이 아니라 기동 시점에 실패시킨다.
    logger.info("prompts ok: %s", prompts.validate_all())
    if settings.prompt_sync_on_startup and settings.tracing_enabled:
        from app.prompts import remote

        remote.sync_in_background()  # git → Langfuse (Langfuse 기동을 기다리며 재시도)

    _pool = AsyncConnectionPool(
        settings.checkpointer_db_url,
        max_size=10,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        # 빌려줄 때 연결을 확인해서, Postgres 재시작 후 끊긴 연결을 자동으로 교체한다.
        check=AsyncConnectionPool.check_connection,
    )
    await _pool.open()
    _checkpointer = AsyncPostgresSaver(_pool)
    # checkpoint 테이블은 LangGraph 가 자체 마이그레이션으로 관리한다 (Alembic 관리 대상 아님).
    await _checkpointer.setup()
    for name, spec in AGENTS.items():
        _graphs[name] = spec.build_graph(settings, _checkpointer)


async def delete_thread(conversation_id: str) -> None:
    """대화의 에이전트 상태(checkpoint)를 모두 지운다."""
    if _checkpointer is None:
        raise RuntimeError("agent runtime not started")
    await _checkpointer.adelete_thread(conversation_id)


async def prune_orphan_threads(keep: set[str]) -> int:
    """UI 대화가 없는 checkpoint thread 를 지운다 (삭제 중 실패 등으로 남은 데이터 정리)."""
    if _pool is None:
        raise RuntimeError("agent runtime not started")
    async with _pool.connection() as conn:
        rows = await (await conn.execute("SELECT DISTINCT thread_id FROM checkpoints")).fetchall()
    orphans = [r["thread_id"] for r in rows if r["thread_id"] not in keep]
    for thread_id in orphans:
        await delete_thread(thread_id)
    return len(orphans)


async def shutdown() -> None:
    if _pool is not None:
        await _pool.close()
    tracing.shutdown()


@dataclass
class TextDelta:
    text: str


@dataclass
class Finished:
    text: str
    stop_reason: str | None
    model: str


# provider 별 종료 사유를 공통 이름으로 맞춘다 (프론트엔드는 이 값만 본다).
_STOP_REASON_MAP = {"stop": "end_turn", "length": "max_tokens"}


def _stop_reason(msg: AIMessage) -> str | None:
    meta = msg.response_metadata or {}
    raw = meta.get("stop_reason") or meta.get("done_reason")
    return _STOP_REASON_MAP.get(raw, raw)


def build_user_message(text: str, images: list[tuple[str, str]]) -> HumanMessage:
    """텍스트 + 이미지(mime_type, base64)를 LangChain 표준 content block 으로 만든다.

    표준 블록은 provider 별 형식(Anthropic source/base64, Ollama images 필드)으로 자동 변환된다.
    """
    if not images:
        return HumanMessage(text)
    blocks: list[dict] = [
        {"type": "image", "base64": data, "mime_type": mime} for mime, data in images
    ]
    if text.strip():
        blocks.append({"type": "text", "text": text})
    return HumanMessage(content=blocks)


async def stream_reply(
    conversation_id: str,
    user_text: str,
    images: list[tuple[str, str]] | None = None,
    agent: str = DEFAULT_AGENT,
) -> AsyncIterator[TextDelta | Finished]:
    graph = _graphs.get(agent)
    if graph is None:
        raise RuntimeError(f"agent not available: {agent} (runtime not started or unknown)")
    stream_nodes = AGENTS[agent].stream_nodes
    config = {
        "configurable": {"thread_id": conversation_id},
        "callbacks": tracing.get_callbacks(),
        "metadata": {"langfuse_session_id": conversation_id, "agent": agent},
        "run_name": agent,
    }

    # 화면에 보여준 텍스트를 그대로 모아 UI용 messages 테이블에 저장한다.
    # (tool 호출이 끼면 agent 노드가 여러 번 실행되므로 턴 사이에 줄바꿈을 넣는다)
    shown: list[str] = []
    current_msg_id: str | None = None

    # 이전 대화는 checkpointer 가 들고 있으므로 새 사용자 메시지만 넣는다.
    async for chunk, meta in graph.astream(
        {"messages": [build_user_message(user_text, images or [])]}, config, stream_mode="messages"
    ):
        if meta.get("langgraph_node") not in stream_nodes or not isinstance(chunk, AIMessageChunk):
            continue
        text = chunk.text
        if not text:
            continue
        if current_msg_id is not None and chunk.id != current_msg_id and shown:
            text = "\n\n" + text
        current_msg_id = chunk.id
        shown.append(text)
        yield TextDelta(text)

    state = await graph.aget_state(config)
    last = state.values["messages"][-1]
    stop = _stop_reason(last) if isinstance(last, AIMessage) else None
    yield Finished(text="".join(shown), stop_reason=stop, model=model_name(get_settings()))
