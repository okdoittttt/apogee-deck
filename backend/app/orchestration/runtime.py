"""에이전트 실행 엔진: 에이전트 등록, checkpointer 생명주기, 스트리밍 실행.

어떤 에이전트가 있는지는 모른다. 앱이 시작할 때 ``startup`` 에 에이전트 목록(``app.agents.AGENTS``)을
넘겨 등록한다. 대화 서비스는 이 모듈의 ``stream_reply`` 로 에이전트를 실행한다.
"""

import logging
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import anyio
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app import prompts
from app.core import tracing
from app.core.config import Settings, get_settings
from app.llm.catalog import model_id, settings_for
from app.orchestration.events import (
    AgentEvent,
    Finished,
    ReasoningDelta,
    TextDelta,
    ToolFinished,
    ToolStarted,
    reasoning_text,
    tool_output_text,
    tool_sources,
)
from app.orchestration.tooling import RECURSION_LIMIT
from app.tools import ToolLabel, default_label

logger = logging.getLogger(__name__)

INTERRUPTED_NOTE = "\n\n[This reply was cut off before it was finished.]"
"""끊긴 응답을 모델 기억(checkpoint)에 넣을 때 붙이는 표시 (모델이 읽으므로 영어).

다음 턴에 모델이 앞 답이 중간에 끊겼다는 것을 알게 한다 ("이어서 해 줘" 면 끊긴 곳부터 잇는다).
"""

__all__ = ["AgentEvent", "AgentSpec", "Finished", "TextDelta", "register", "stream_reply"]


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


_agents: dict[str, AgentSpec] = {}
"""등록된 에이전트 (``register``)."""
_default_agent: str | None = None
_pool: AsyncConnectionPool | None = None
_checkpointer: BaseCheckpointSaver | None = None
_graphs: dict[str, CompiledStateGraph] = {}
"""그래프 캐시. 기본 모델은 에이전트 이름, 다른 모델은 ``"<에이전트>@<모델 ID>"`` 가 키다."""


def register(agents: Mapping[str, AgentSpec], default: str) -> None:
    """실행할 수 있는 에이전트를 등록한다. 이미 만든 그래프 캐시는 비운다.

    Args:
        agents: 에이전트 이름 → 정의.
        default: 에이전트를 지정하지 않은 실행에 쓸 에이전트 이름.

    Raises:
        ValueError: ``default`` 가 ``agents`` 에 없을 때.
    """
    global _default_agent
    if default not in agents:
        raise ValueError(f"default agent is not registered: {default}")
    _agents.clear()
    _agents.update(agents)
    _default_agent = default
    _graphs.clear()


async def startup(agents: Mapping[str, AgentSpec], default: str) -> None:
    """에이전트를 등록하고, 프롬프트를 검증하고, checkpointer 를 연결하고, 기본 모델 그래프를 만든다.

    다른 모델의 그래프는 처음 요청될 때 만든다 (``get_graph``).

    잘못된 프롬프트(빠진 include, 변수 불일치)는 첫 요청이 아니라 이 시점에 실패시킨다.
    ``prompt_sync_on_startup`` 이 켜져 있으면 git → Langfuse 프롬프트 동기화를 백그라운드로 시작한다.

    Args:
        agents: 등록할 에이전트 이름 → 정의.
        default: 에이전트를 지정하지 않은 실행에 쓸 에이전트 이름.

    Raises:
        PromptError: 프롬프트 검증에 실패했을 때.
        ValueError: ``default`` 가 ``agents`` 에 없을 때.
    """
    global _pool, _checkpointer
    register(agents, default)
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
    for name, spec in _agents.items():
        _graphs[name] = spec.build_graph(settings, _checkpointer)


def get_graph(agent: str, model: str | None = None) -> tuple[CompiledStateGraph, str]:
    """에이전트와 모델에 맞는 그래프를 돌려준다. 없으면 만들어서 캐시한다.

    모델마다 provider, 컨텍스트 예산(요약 노드 유무)이 달라서 그래프를 따로 만든다.
    대화 상태는 같은 checkpointer 에 있으므로 대화 도중에 모델을 바꿔도 이력이 이어진다.

    Args:
        agent: 등록된 에이전트 이름.
        model: 모델 ID. ``None`` 이면 설정의 기본 모델.

    Returns:
        ``(그래프, 실제로 쓰는 모델 ID)``.

    Raises:
        RuntimeError: 런타임이 시작되지 않았거나 알 수 없는 에이전트일 때.
        UnknownModelError: 모델 ID 형식이 잘못됐을 때.
    """
    settings = get_settings()
    default = model_id(settings)
    model = model or default
    key = agent if model == default else f"{agent}@{model}"
    if graph := _graphs.get(key):
        return graph, model
    spec = _agents.get(agent)
    if spec is None or _checkpointer is None:
        raise RuntimeError(f"agent not available: {agent} (runtime not started or unknown)")
    graph = spec.build_graph(settings_for(settings, model), _checkpointer)
    _graphs[key] = graph
    return graph, model


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
# Anthropic: stop_reason, Ollama: done_reason("stop"/"length"), Google: finish_reason("STOP"/"MAX_TOKENS"),
# OpenAI Responses API: status("completed"/"incomplete") + incomplete_details.reason.
_STOP_REASON_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "STOP": "end_turn",
    "MAX_TOKENS": "max_tokens",
}


def _stop_reason(msg: AIMessage) -> str | None:
    """응답 메타데이터에서 종료 사유를 꺼내 공통 이름으로 바꾼다."""
    meta = msg.response_metadata or {}
    raw = meta.get("stop_reason") or meta.get("done_reason") or meta.get("finish_reason")
    if raw is None and (status := meta.get("status")):
        details = meta.get("incomplete_details") or {}
        if status == "completed":
            raw = "end_turn"
        elif details.get("reason") == "max_output_tokens":
            raw = "max_tokens"
        else:
            raw = details.get("reason") or status
    return _STOP_REASON_MAP.get(raw, raw)


def build_user_message(text: str, images: list[dict[str, Any]]) -> HumanMessage:
    """텍스트와 이미지를 LangChain 표준 content block 으로 된 사용자 메시지로 만든다.

    이미지는 원본 대신 참조 블록(``app.images.image_ref_block``)으로 넣는다. 이 메시지가
    checkpoint 에 저장되기 때문이다. 모델을 부르기 직전에 원본으로 바꾸고, 그때 표준 블록이
    provider 별 형식(Anthropic ``source``/base64, Ollama ``images`` 필드)으로 자동 변환된다.
    이미지를 텍스트보다 앞에 둔다.

    Args:
        text: 사용자 메시지 텍스트.
        images: 이미지 블록 목록.

    Returns:
        이미지가 없으면 텍스트만, 있으면 이미지 + 텍스트 블록을 가진 ``HumanMessage``.
    """
    if not images:
        return HumanMessage(text)
    blocks: list[dict[str, Any]] = list(images)
    if text.strip():
        blocks.append({"type": "text", "text": text})
    return HumanMessage(content=blocks)


async def stream_reply(
    conversation_id: str,
    user_text: str,
    images: list[dict[str, Any]] | None = None,
    agent: str | None = None,
    model: str | None = None,
    context: Mapping[str, Any] | None = None,
    trace_id: str | None = None,
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
        images: 이미지 참조 블록 (``app.images.image_ref_block``).
        agent: 실행할 에이전트 이름. ``None`` 이면 등록할 때 정한 기본 에이전트.
        model: 모델 ID (``app.llm.catalog``). ``None`` 이면 설정의 기본 모델.
        context: 이번 턴에 에이전트에게 넘길 정보 (예: ``{"project": ProjectContext}``). 그래프의 노드와
            도구가 ``config["configurable"]`` 에서 읽는다. 문자열·숫자가 아닌 값은 checkpoint
            메타데이터에 저장되지 않는다.
        trace_id: Langfuse 트레이스 ID (``tracing.new_trace_id``). 응답에 단 피드백을 이 트레이스에 남긴다.

    Yields:
        일어난 순서대로 ``TextDelta`` / ``ReasoningDelta`` / ``ToolStarted`` / ``ToolFinished``,
        마지막에 ``Finished`` 하나.

    Raises:
        RuntimeError: 런타임이 시작되지 않았거나 알 수 없는 에이전트일 때.
        UnknownModelError: 모델 ID 형식이 잘못됐을 때.
    """
    agent = agent or _default_agent
    if agent is None:
        raise RuntimeError("no agent registered")
    graph, used_model = get_graph(agent, model)
    spec = _agents[agent]
    config = {
        "configurable": {**(context or {}), "thread_id": conversation_id},
        "callbacks": tracing.get_callbacks(trace_id),
        "metadata": {"langfuse_session_id": conversation_id, "agent": agent, "model": used_model},
        "run_name": agent,
        # 모델이 도구를 끝없이 부르지 않게 한 턴의 단계 수를 제한한다 (넘으면 GraphRecursionError).
        "recursion_limit": RECURSION_LIMIT,
    }

    shown: list[str] = []  # 화면에 보여준 답변 텍스트 → UI 용 messages 테이블에 그대로 저장
    current_msg_id: str | None = None
    # 아직 checkpoint 에 들어가지 않은 답변 글자 (진행 중인 모델 호출). 그 노드가 끝나면 비운다.
    pending: list[str] = []
    pending_node: str | None = None

    try:
        async for mode, payload in graph.astream(
            {"messages": [build_user_message(user_text, images or [])]},
            config,
            stream_mode=["messages", "updates"],
        ):
            if mode == "messages":
                chunk, meta = payload
                node = meta.get("langgraph_node")
                if node not in spec.stream_nodes:
                    continue
                if not isinstance(chunk, AIMessageChunk):
                    continue
                if reasoning := reasoning_text(chunk):
                    yield ReasoningDelta(reasoning)
                if text := chunk.text:
                    pending.append(text)
                    pending_node = node
                    if current_msg_id is not None and chunk.id != current_msg_id and shown:
                        text = "\n\n" + text
                    current_msg_id = chunk.id
                    shown.append(text)
                    yield TextDelta(text)
            elif mode == "updates":
                if _answered(payload, spec.stream_nodes):
                    pending.clear()  # 그 답은 checkpoint 에 들어갔다
                for event in _tool_events(payload, spec.tool_labels):
                    yield event
    except BaseException:
        # 중지·시간 초과·앱 종료(취소), 화면이 구독을 끊음(GeneratorExit), 오류: 진행 중이던 답을 기억에 넣는다.
        # 노드가 끝나야 checkpoint 를 쓰므로, 넣지 않으면 모델은 다음 턴에 자기가 한 말을 모른다.
        with anyio.CancelScope(shield=True):
            await _remember_interrupted(graph, conversation_id, "".join(pending), pending_node)
        raise

    state = await graph.aget_state(config)
    last = state.values["messages"][-1]
    stop = _stop_reason(last) if isinstance(last, AIMessage) else None
    yield Finished(text="".join(shown), stop_reason=stop, model=used_model)


def _answered(update: Mapping[str, Any], nodes: frozenset[str]) -> bool:
    """답변 노드가 끝나 그 답(AIMessage)이 상태에 들어갔는지."""
    return any(
        name in nodes
        and isinstance(node_update, Mapping)
        and any(isinstance(m, AIMessage) for m in node_update.get("messages") or [])
        for name, node_update in update.items()
    )


async def _remember_interrupted(
    graph: CompiledStateGraph, conversation_id: str, text: str, node: str | None
) -> None:
    """끊긴 답을 중단 표시와 함께 checkpoint 에 넣는다. 실패해도 기록만 남긴다 (원래 오류를 가리지 않게).

    그 답을 낸 노드(``as_node``)가 도구 호출 없이 답한 것으로 넣으므로, 다음 턴은 평소처럼 시작된다.

    Args:
        graph: 실행한 그래프.
        conversation_id: 대화 ID (checkpointer ``thread_id``).
        text: 끊기기 전까지의 답 (진행 중이던 모델 호출의 글자). 비었으면 아무것도 하지 않는다.
        node: 그 답을 내던 노드.
    """
    if not text.strip() or node is None:
        return
    try:
        await graph.aupdate_state(
            {"configurable": {"thread_id": conversation_id}},
            {"messages": [AIMessage(text + INTERRUPTED_NOTE)]},
            as_node=node,
        )
    except Exception:
        logger.exception("failed to save the interrupted reply of %s to memory", conversation_id)


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
                        sources=tool_sources(message.artifact),
                    )
                )
    return events
