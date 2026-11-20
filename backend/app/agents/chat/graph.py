"""기본 채팅 에이전트의 LangGraph 그래프.

::

    START ─(요약 필요)─▶ summarize ─┐
      └──────────────────────────▶ agent ──(tool_calls 있음)──▶ tools ─┐
                                     ▲                                  │
                                     └──────────────────────────────────┘
                                     └──(tool_calls 없음)──▶ END

대화 상태(messages, 요약)는 checkpointer 가 ``thread_id`` (= conversation_id) 별로 저장한다.
``summarize`` 는 대화 이력 예산이 있을 때(Ollama 등)만 그래프에 들어간다.
"""

import asyncio
import logging
from typing import Any, Literal

from langchain_core.messages import BaseMessage, SystemMessage
from langchain_core.runnables import RunnableLambda
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from app import prompts
from app.agents import summary
from app.agents.chat.tools import build_tools
from app.agents.context import (
    compact_old_tool_results,
    drop_foreign_reasoning,
    make_context_selector,
    make_token_counter,
    tag_model,
    today,
)
from app.agents.tooling import handle_tool_error
from app.config import Settings
from app.llm import build_chat_model, build_summary_model
from app.llm.catalog import model_id

logger = logging.getLogger(__name__)

AGENT_NODE = "agent"
SUMMARIZE_NODE = "summarize"
SUMMARY_TIMEOUT_SECONDS = 120.0
"""요약이 이보다 오래 걸리면 포기하고 요약 없이 답한다."""

STREAM_NODES = frozenset({AGENT_NODE})
"""이 노드들이 생성하는 토큰만 사용자에게 스트리밍한다 (요약 노드의 토큰은 보여주지 않는다)."""


class ChatState(MessagesState):
    """채팅 에이전트의 상태.

    Attributes:
        summary: 오래된 대화의 요약. 요약한 적이 없으면 상태에 없다.
        summarized_until: 요약에 포함된 마지막 메시지 ID. 이 다음 메시지부터 원문으로 보낸다.
    """

    summary: str
    summarized_until: str | None


def build_graph(settings: Settings, checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:
    """채팅 에이전트 그래프를 만든다.

    Args:
        settings: 앱 설정. 모델, 도구 사용 여부, 컨텍스트 예산을 정한다.
            예산이 있으면(``history_token_budget``) 대화 요약 노드를 추가한다.
        checkpointer: 대화 상태를 저장할 LangGraph checkpointer.

    Returns:
        컴파일된 그래프. ``enable_tools`` 가 꺼져 있으면 ``agent → END`` 만 있다.
    """
    tools = build_tools(settings) if settings.enable_tools else []
    model = build_chat_model(settings)
    if tools:
        model = model.bind_tools(tools)

    select_context = make_context_selector(settings)
    count_tokens = make_token_counter(settings)
    budget = settings.history_token_budget
    current_model = model_id(settings)
    summary_model = build_summary_model(settings) if budget is not None else None

    def model_view(messages: list[BaseMessage]) -> list[BaseMessage]:
        """모델이 보는 이력: 다른 모델의 추론을 빼고, 지난 턴의 도구 결과를 줄인다(설정으로 끌 수 있음).

        모델 입력과 요약 판단에 같이 쓴다. 요약 판단이 원본 크기로 세면, 지난 검색 결과 때문에
        요약이 필요 이상으로 일찍 일어나기 때문이다.
        """
        messages = drop_foreign_reasoning(messages, current_model)
        return compact_old_tool_results(messages) if settings.compact_tool_results else messages

    # 모델 호출을 한 단계(chain)로 감싼다. Langfuse 콜백은 이 단계의 metadata 에 있는
    # langfuse_prompt 를 보고, 그 아래 LLM 호출(generation)을 해당 프롬프트 버전에 연결한다.
    call_model = RunnableLambda(model.ainvoke, name="call_model")

    async def agent(state: ChatState) -> dict[str, Any]:
        """System 프롬프트 + (요약) + 최근 대화로 모델을 호출한다.

        system 프롬프트는 상태에 저장하지 않고 호출할 때마다 렌더링해서 앞에 붙인다.
        대화 이력 예산이 있고 요약이 있으면, 요약을 system 프롬프트 끝에 붙이고
        요약 이후의 메시지만 보낸다.

        Args:
            state: 그래프 상태 (전체 대화 이력과 요약).

        Returns:
            상태에 추가할 모델 응답 메시지.
        """
        prompt = await prompts.arender("chat/system", today=today(settings))
        text = prompt.text
        history = model_view(state["messages"])
        if budget is not None and state.get("summary"):
            summary_prompt = await prompts.arender(
                "chat/conversation_summary", summary=state["summary"]
            )
            text = f"{text}\n\n{summary_prompt.text}"
            history = summary.unsummarized(history, state.get("summarized_until"))
        system = SystemMessage(text)
        response = await call_model.ainvoke(
            [system, *select_context(system, history)],
            config={"metadata": prompt.trace_metadata()},
        )
        # 대화 도중 모델을 바꿨을 때 이 응답의 추론을 다른 모델에 보내지 않도록 모델을 기록한다.
        return {"messages": [tag_model(response, current_model)]}

    def route_start(state: ChatState) -> Literal["summarize", "agent"]:
        """요약이 필요하면 요약 노드로, 아니면 바로 agent 노드로 보낸다."""
        assert budget is not None
        plan = summary.plan_summary(
            model_view(state["messages"]), state.get("summarized_until"), budget, count_tokens
        )
        return SUMMARIZE_NODE if plan else AGENT_NODE

    async def summarize(state: ChatState) -> dict[str, Any]:
        """오래된 대화를 기존 요약에 합쳐서 새 요약을 만든다.

        실패하거나 오래 걸리면 경고만 남기고 상태를 바꾸지 않는다. 그러면 agent 노드는
        기존 요약과 잘라내기만으로 답한다. 요약 때문에 답변이 실패하면 안 되기 때문이다.

        Args:
            state: 그래프 상태.

        Returns:
            새 요약과 경계. 요약하지 못했으면 빈 딕셔너리.
        """
        assert budget is not None and summary_model is not None
        plan = summary.plan_summary(
            model_view(state["messages"]), state.get("summarized_until"), budget, count_tokens
        )
        if plan is None:
            return {}
        try:
            async with asyncio.timeout(SUMMARY_TIMEOUT_SECONDS):
                text = await summary.summarize(
                    summary_model,
                    "chat/summary",
                    state.get("summary", ""),
                    plan.messages,
                    budget,
                    count_tokens,
                )
        except Exception:
            logger.warning("conversation summary failed; continuing without it", exc_info=True)
            return {}
        return {"summary": text, "summarized_until": plan.until_id}

    builder = StateGraph(ChatState)
    builder.add_node(AGENT_NODE, agent)
    if budget is not None:
        builder.add_node(SUMMARIZE_NODE, summarize)
        builder.add_conditional_edges(START, route_start, [SUMMARIZE_NODE, AGENT_NODE])
        builder.add_edge(SUMMARIZE_NODE, AGENT_NODE)
    else:
        builder.add_edge(START, AGENT_NODE)

    if tools:
        # 도구가 실패해도 응답 전체가 실패하지 않게, 예외를 모델이 읽을 오류 결과로 바꾼다.
        builder.add_node("tools", ToolNode(tools, handle_tool_errors=handle_tool_error))
        builder.add_conditional_edges(AGENT_NODE, tools_condition)
        builder.add_edge("tools", AGENT_NODE)
    else:
        builder.add_edge(AGENT_NODE, END)

    return builder.compile(checkpointer=checkpointer)
