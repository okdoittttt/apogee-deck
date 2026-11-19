"""에이전트 레지스트리, checkpointer 생명주기, 스트리밍 실행.

라우터는 이 모듈의 ``stream_reply`` 만 사용한다. 새 에이전트는 ``app/agents/<이름>/`` 에
``build_graph`` / ``STREAM_NODES`` 를 만들고 아래 ``AGENTS`` 에 등록하면 된다.
"""

import logging
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app import prompts, tracing
from app.agents import chat
from app.agents.events import (
    AgentEvent,
    Finished,
    ReasoningDelta,
    TextDelta,
    ToolFinished,
    ToolLabel,
    ToolStarted,
    default_label,
    reasoning_text,
    tool_output_text,
)
from app.agents.tooling import RECURSION_LIMIT
from app.config import Settings, get_settings
from app.llm import model_name

logger = logging.getLogger(__name__)

__all__ = ["AGENTS", "AgentEvent", "AgentSpec", "Finished", "TextDelta", "stream_reply"]


@dataclass(frozen=True)
class AgentSpec:
    """레지스트리에 등록하는 에이전트 정의.

    Attributes:
        build_graph: ``(settings, checkpointer)`` 를 받아 컴파일된 그래프를 만드는 함수.
        stream_nodes: 이 노드들이 생성한 토큰만 사용자에게 스트리밍한다.
        tool_labels: 도구 이름 → 화면 표시 문구. 없는 도구는 기본 문구를 쓴다.
    """

    build_graph: Callable[[Settings, BaseCheckpointSaver], CompiledStateGraph]
    stream_nodes: frozenset[str]
    tool_labels: Mapping[str, ToolLabel] = field(default_factory=dict)


AGENTS: dict[str, AgentSpec] = {
    "chat": AgentSpec(chat.build_graph, chat.STREAM_NODES, chat.TOOL_LABELS),
}
DEFAULT_AGENT = "chat"

_pool: AsyncConnectionPool | None = None
_checkpointer: BaseCheckpointSaver | None = None
_graphs: dict[str, CompiledStateGraph] = {}


async def startup() -> None:
    """프롬프트를 검증하고, checkpointer 를 연결하고, 등록된 에이전트 그래프를 만든다.

    잘못된 프롬프트(빠진 include, 변수 불일치)는 첫 요청이 아니라 이 시점에 실패시킨다.
    ``prompt_sync_on_startup`` 이 켜져 있으면 git → Langfuse 프롬프트 동기화를 백그라운드로 시작한다.

    Raises:
        PromptError: 프롬프트 검증에 실패했을 때.
    """
    global _pool, _checkpointer
    settings = get_settings()
    logger.info("prompts ok: %s", prompts.validate_all())
    if settings.prompt_sync_on_startup and settings.tracing_enabled:
        from app.prompts import remote

        remote.sync_in_background()

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


async def shutdown() -> None:
    """Checkpointer 연결 풀을 닫고 남은 트레이스를 전송한다."""
    if _pool is not None:
        await _pool.close()
    tracing.shutdown()


async def delete_thread(conversation_id: str) -> None:
    """대화의 에이전트 상태(checkpoint)를 모두 지운다.

    Args:
        conversation_id: 대화 ID (= checkpointer ``thread_id``).

    Raises:
        RuntimeError: 런타임이 시작되지 않았을 때.
    """
    if _checkpointer is None:
        raise RuntimeError("agent runtime not started")
    await _checkpointer.adelete_thread(conversation_id)


async def prune_orphan_threads(keep: set[str]) -> int:
    """UI 대화가 없는 checkpoint thread 를 지운다.

    대화 삭제 도중 checkpoint 삭제가 실패해서 남은 데이터를 정리하는 용도다.

    Args:
        keep: 남겨둘 thread ID (현재 존재하는 대화 ID).

    Returns:
        삭제한 thread 수.

    Raises:
        RuntimeError: 런타임이 시작되지 않았을 때.
    """
    if _pool is None:
        raise RuntimeError("agent runtime not started")
    async with _pool.connection() as conn:
        rows = await (await conn.execute("SELECT DISTINCT thread_id FROM checkpoints")).fetchall()
    orphans = [r["thread_id"] for r in rows if r["thread_id"] not in keep]
    for thread_id in orphans:
        await delete_thread(thread_id)
    return len(orphans)


# provider 별 종료 사유를 공통 이름으로 맞춘다 (프론트엔드는 이 값만 본다).
_STOP_REASON_MAP = {"stop": "end_turn", "length": "max_tokens"}


def _stop_reason(msg: AIMessage) -> str | None:
    """응답 메타데이터에서 종료 사유를 꺼내 공통 이름으로 바꾼다."""
    meta = msg.response_metadata or {}
    raw = meta.get("stop_reason") or meta.get("done_reason")
    return _STOP_REASON_MAP.get(raw, raw)


def build_user_message(text: str, images: list[tuple[str, str]]) -> HumanMessage:
    """텍스트와 이미지를 LangChain 표준 content block 으로 된 사용자 메시지로 만든다.

    표준 블록은 provider 별 형식(Anthropic ``source``/base64, Ollama ``images`` 필드)으로
    자동 변환된다. 이미지를 텍스트보다 앞에 둔다.

    Args:
        text: 사용자 메시지 텍스트.
        images: ``(mime_type, base64)`` 목록.

    Returns:
        이미지가 없으면 텍스트만, 있으면 이미지 + 텍스트 블록을 가진 ``HumanMessage``.
    """
    if not images:
        return HumanMessage(text)
    blocks: list[dict[str, Any]] = [
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
) -> AsyncIterator[AgentEvent]:
    """에이전트를 실행하고 응답과 실행 단계를 스트리밍한다.

    이전 대화는 checkpointer 가 들고 있으므로 새 사용자 메시지만 그래프에 넣는다.
    LangGraph 의 두 가지 스트림을 함께 받는다.

    - ``messages``: 모델이 생성하는 토큰 → 답변 텍스트(``TextDelta``), 추론(``ReasoningDelta``)
    - ``updates``: 노드가 끝날 때의 상태 변경 → 도구 호출 결정(``ToolStarted``),
      도구 결과(``ToolFinished``)

    tool 호출이 끼면 agent 노드가 여러 번 실행되므로, 답변 메시지가 바뀔 때 줄바꿈을 넣는다.

    Args:
        conversation_id: 대화 ID (= checkpointer ``thread_id``, Langfuse session ID).
        user_text: 사용자 메시지 텍스트.
        images: ``(mime_type, base64)`` 목록.
        agent: 실행할 에이전트 이름 (``AGENTS`` 의 키).

    Yields:
        일어난 순서대로 ``TextDelta`` / ``ReasoningDelta`` / ``ToolStarted`` / ``ToolFinished``,
        마지막에 ``Finished`` 하나.

    Raises:
        RuntimeError: 런타임이 시작되지 않았거나 알 수 없는 에이전트일 때.
    """
    graph = _graphs.get(agent)
    if graph is None:
        raise RuntimeError(f"agent not available: {agent} (runtime not started or unknown)")
    spec = AGENTS[agent]
    config = {
        "configurable": {"thread_id": conversation_id},
        "callbacks": tracing.get_callbacks(),
        "metadata": {"langfuse_session_id": conversation_id, "agent": agent},
        "run_name": agent,
        # 모델이 도구를 끝없이 부르지 않게 한 턴의 단계 수를 제한한다 (넘으면 GraphRecursionError).
        "recursion_limit": RECURSION_LIMIT,
    }

    shown: list[str] = []  # 화면에 보여준 답변 텍스트 → UI 용 messages 테이블에 그대로 저장
    current_msg_id: str | None = None

    async for mode, payload in graph.astream(
        {"messages": [build_user_message(user_text, images or [])]},
        config,
        stream_mode=["messages", "updates"],
    ):
        if mode == "messages":
            chunk, meta = payload
            if meta.get("langgraph_node") not in spec.stream_nodes:
                continue
            if not isinstance(chunk, AIMessageChunk):
                continue
            if reasoning := reasoning_text(chunk):
                yield ReasoningDelta(reasoning)
            if text := chunk.text:
                if current_msg_id is not None and chunk.id != current_msg_id and shown:
                    text = "\n\n" + text
                current_msg_id = chunk.id
                shown.append(text)
                yield TextDelta(text)
        elif mode == "updates":
            for event in _tool_events(payload, spec.tool_labels):
                yield event

    state = await graph.aget_state(config)
    last = state.values["messages"][-1]
    stop = _stop_reason(last) if isinstance(last, AIMessage) else None
    yield Finished(text="".join(shown), stop_reason=stop, model=model_name(get_settings()))


def _tool_events(
    update: Mapping[str, Any], labels: Mapping[str, ToolLabel]
) -> list[ToolStarted | ToolFinished]:
    """노드 업데이트에서 도구 호출 결정과 도구 결과를 찾아 이벤트로 바꾼다."""
    events: list[ToolStarted | ToolFinished] = []
    for node_update in update.values():
        if not isinstance(node_update, Mapping):
            continue
        for message in node_update.get("messages") or []:
            if isinstance(message, AIMessage):
                events.extend(
                    ToolStarted(
                        id=call["id"],
                        name=call["name"],
                        args=call["args"],
                        label=labels.get(call["name"]) or default_label(call["name"]),
                    )
                    for call in message.tool_calls
                )
            elif isinstance(message, ToolMessage):
                events.append(
                    ToolFinished(
                        id=message.tool_call_id,
                        output=tool_output_text(message.content),
                        status="error" if message.status == "error" else "done",
                    )
                )
    return events
