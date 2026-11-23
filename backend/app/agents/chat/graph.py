"""기본 채팅 에이전트의 LangGraph 그래프.

::

    START ─▶ (recall) ─(요약 필요)─▶ summarize ─┐
                └─────────────────────────────▶ (decide) ─▶ agent ──(tool_calls 있음)──▶ tools ─┐
                                                               ▲                                  │
                                                               └──────────────────────────────────┘
                                                               └──(tool_calls 없음)──▶ END

대화 상태(messages, 요약)는 checkpointer 가 ``thread_id`` (= conversation_id) 별로 저장한다.
``summarize`` 는 대화 이력 예산이 있을 때(Ollama 등)만 그래프에 들어간다.

장기 기억(사용자 프로필과 기억)은 Store 가 사용자별로 저장한다. Store 를 받으면 ``recall`` 노드가 턴마다 한 번
프로필과 이번 메시지에 관련된 기억을 읽고, agent 노드가 프로필은 system 프롬프트에, 기억은 사용자 메시지 앞에
붙인다. 기억은 모델에 보낼 때만 붙이고 checkpoint 의 메시지는 바꾸지 않는다. 매 턴 달라지는 내용을 system
프롬프트에 넣으면 그 뒤의 대화 이력 전체가 프롬프트 캐시에서 빠지기 때문이다.

도구 결정(``enable_tool_decision``)을 켜면 ``decide`` 노드가 턴마다 한 번 결정 모델(d3)에게 이번 메시지에 필요한
도구 그룹을 묻고, agent 는 켜진 그룹의 도구만 모델에게 준다 (``app.orchestration.decision``).
"""

import asyncio
import logging
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable, RunnableConfig, RunnableLambda
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.store.base import BaseStore

from app import prompts
from app.core.config import Settings
from app.core.storage import get_storage
from app.documents.reading import document_list
from app.images import resolve_images
from app.llm import build_chat_model, build_summary_model, with_prompt_cache
from app.llm.catalog import model_id
from app.memory import service as memory
from app.orchestration import decision, summary
from app.orchestration.citations import number_sources
from app.orchestration.context import (
    compact_old_tool_results,
    drop_foreign_reasoning,
    make_context_selector,
    make_token_counter,
    tag_model,
    today,
)
from app.orchestration.tooling import handle_tool_error
from app.tools import build_tools, tool_group
from app.tools.documents import PROJECT_TOOL_NAMES
from app.tools.memory import MEMORY_TOOL_NAMES

logger = logging.getLogger(__name__)

AGENT_NODE = "agent"
SUMMARIZE_NODE = "summarize"
RECALL_NODE = "recall"
DECIDE_NODE = "decide"
SUMMARY_TIMEOUT_SECONDS = 120.0
"""요약이 이보다 오래 걸리면 포기하고 요약 없이 답한다."""

STREAM_NODES = frozenset({AGENT_NODE})
"""이 노드들이 생성하는 토큰만 사용자에게 스트리밍한다 (요약 노드의 토큰은 보여주지 않는다)."""

TOOL_NAMES = (
    "get_current_time",
    "web_search",
    "fetch_url",
    "read_document",
    "search_project",
    "remember",
    "forget",
    "update_profile",
    "search_memories",
)
"""이 에이전트가 쓰는 도구 (``app.tools`` 레지스트리의 이름). 설정상 쓸 수 없는 도구는 빠진다."""


class ChatState(MessagesState):
    """채팅 에이전트의 상태.

    Attributes:
        summary: 오래된 대화의 요약. 요약한 적이 없으면 상태에 없다.
        summarized_until: 요약에 포함된 마지막 메시지 ID. 이 다음 메시지부터 원문으로 보낸다.
        profile: 이번 턴에 읽은 사용자 프로필 (``recall`` 노드). 장기 기억을 쓰지 않으면 상태에 없다.
        recalled: 이번 턴의 사용자 메시지와 관련 있는 기억 (모델에게 보여줄 줄 목록). 없으면 빈 문자열.
        tool_groups: 도구 결정으로 이 대화에서 지금까지 켜진 도구 그룹 (누적). 도구 결정을 쓰지 않으면 상태에 없다.
        active_groups: 이번 턴에 모델에게 줄 도구 그룹 (``decide`` 노드가 매 턴 정한다). ``None`` 이면 도구를 모두 준다.
    """

    summary: str
    summarized_until: str | None
    profile: str
    recalled: str
    tool_groups: list[str]
    active_groups: list[str] | None


def memory_user(config: RunnableConfig) -> str | None:
    """이번 턴에 장기 기억을 쓸 사용자 ID. 사용자를 모르거나 사용자가 기억을 껐으면 ``None``.

    Args:
        config: 실행 설정. 서버가 ``user_id`` 와 ``memory_enabled`` 를 넘긴다.

    Returns:
        사용자 ID, 또는 ``None``.
    """
    configurable = config.get("configurable") or {}
    if configurable.get("memory_enabled") is False or not configurable.get("user_id"):
        return None
    return str(configurable["user_id"])


def hidden_tools(config: RunnableConfig, *, use_memory: bool) -> frozenset[str]:
    """이번 턴에 모델에게 보여주지 않을 도구 이름.

    도구 정의도 매 요청 프롬프트에 들어가므로 이번 턴에 쓸 수 없는 도구는 빼서, 토큰을 아끼고 모델이 헛되이
    부르지 않게 한다. 기억을 쓰지 않는 턴이면 기억 도구를, 프로젝트 밖의 대화면 프로젝트 검색 도구를 뺀다.
    대화의 프로젝트는 바뀌지 않으므로 한 대화 안에서는 (기억 설정을 바꾸지 않는 한) 도구 목록이 같아서
    프롬프트 캐시가 유지된다.

    Args:
        config: 실행 설정. 서버가 ``user_id``, ``memory_enabled``, ``project`` 를 넘긴다.
        use_memory: 그래프가 장기 기억을 쓰는지 (Store 가 있고 ``enable_memory`` 가 켜짐).

    Returns:
        뺄 도구 이름.
    """
    hidden: set[str] = set()
    if not use_memory or memory_user(config) is None:
        hidden |= MEMORY_TOOL_NAMES
    if (config.get("configurable") or {}).get("project") is None:
        hidden |= PROJECT_TOOL_NAMES
    return frozenset(hidden)


def with_recalled(messages: list[BaseMessage], block: str) -> list[BaseMessage]:
    """마지막 사용자 메시지 앞에 기억 블록을 붙인 복사본을 돌려준다 (원본 메시지는 바꾸지 않는다).

    Args:
        messages: 모델에 보낼 메시지 (system 제외).
        block: 붙일 기억 블록.

    Returns:
        기억을 붙인 메시지 목록. 사용자 메시지가 없으면 그대로.
    """
    for i in range(len(messages) - 1, -1, -1):
        message = messages[i]
        if isinstance(message, HumanMessage):
            content = message.content
            if isinstance(content, str):
                new_content: str | list = f"{block}\n\n{content}"
            else:
                new_content = [{"type": "text", "text": block}, *content]
            return [
                *messages[:i],
                message.model_copy(update={"content": new_content}),
                *messages[i + 1 :],
            ]
    return messages


def build_graph(
    settings: Settings, checkpointer: BaseCheckpointSaver, store: BaseStore | None = None
) -> CompiledStateGraph:
    """채팅 에이전트 그래프를 만든다.

    Args:
        settings: 앱 설정. 모델, 도구 사용 여부, 컨텍스트 예산을 정한다.
            예산이 있으면(``history_token_budget``) 대화 요약 노드를 추가한다.
        checkpointer: 대화 상태를 저장할 LangGraph checkpointer.
        store: 장기 기억 Store. 있고 ``enable_memory`` 가 켜져 있으면 ``recall`` 노드와 기억 도구를 추가한다.

    ``enable_tool_decision`` 이 켜져 있고 도구가 있으면 agent 앞에 ``decide`` 노드를 추가한다.

    Returns:
        컴파일된 그래프. ``enable_tools`` 가 꺼져 있으면 ``agent → END`` 만 있다.
    """
    use_memory = store is not None and settings.enable_memory
    tools = build_tools(settings, TOOL_NAMES) if settings.enable_tools else []
    if not use_memory:
        tools = [t for t in tools if t.name not in MEMORY_TOOL_NAMES]
    chat_model = build_chat_model(settings)
    use_decision = settings.enable_tool_decision and bool(tools)

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

    callers: dict[frozenset[str], Runnable] = {}

    def call_model(hidden: frozenset[str]) -> Runnable:
        """``hidden`` 을 뺀 도구를 붙인 모델 호출 단계 (조합마다 처음 쓸 때 한 번 만든다).

        모델 호출을 한 단계(chain)로 감싼다. Langfuse 콜백은 이 단계의 metadata 에 있는
        langfuse_prompt 를 보고, 그 아래 LLM 호출(generation)을 해당 프롬프트 버전에 연결한다.
        """
        if hidden not in callers:
            visible = [t for t in tools if t.name not in hidden]
            bound = chat_model.bind_tools(visible) if visible else chat_model
            callers[hidden] = RunnableLambda(
                with_prompt_cache(bound, settings).ainvoke, name="call_model"
            )
        return callers[hidden]

    async def recall(
        state: ChatState, config: RunnableConfig, *, store: BaseStore
    ) -> dict[str, Any]:
        """사용자 프로필과 이번 메시지에 관련된 기억을 읽는다 (턴마다 한 번).

        한 턴 안에서 도구를 여러 번 불러도 다시 검색하지 않아서, 임베딩 호출이 늘지 않고 모델에 보내는
        앞부분도 같게 유지된다(프롬프트 캐시). 실패하면 경고만 남기고 기억 없이 답한다.

        Args:
            state: 그래프 상태.
            config: 실행 설정 (사용자 ID, 기억 사용 여부).
            store: 장기 기억 Store (그래프가 주입한다).

        Returns:
            이번 턴의 프로필과 관련 기억.
        """
        user_id = memory_user(config)
        if user_id is None:
            return {"profile": "", "recalled": ""}
        human = [m for m in state["messages"] if isinstance(m, HumanMessage)]
        query = human[-1].text if human else ""
        try:
            profile = await memory.get_profile(store, user_id)
            found = await memory.search_memories(
                store,
                user_id,
                query,
                settings.memory_recall_limit,
                min_score=settings.memory_min_score,
            )
        except Exception:
            logger.warning("memory recall failed; answering without memory", exc_info=True)
            return {"profile": "", "recalled": ""}
        return {
            "profile": profile.text,
            "recalled": "\n".join(memory.format_memory(m) for m in found),
        }

    async def ask_decision_model(request: dict[str, Any]) -> dict[str, float]:
        return await decision.ask_d3(settings, request["state"], request["questions"])

    # Langfuse 트레이스에 결정 모델의 입력(상태, 질문)과 출력(그룹별 확률)이 한 단계로 남게 감싼다.
    traced_ask = RunnableLambda(ask_decision_model, name="tool_decision")

    async def ask(state: dict[str, Any], questions: dict[str, str]) -> dict[str, float]:
        return await traced_ask.ainvoke({"state": state, "questions": questions})

    async def decide(state: ChatState, config: RunnableConfig) -> dict[str, Any]:
        """결정 모델에게 이번 턴에 필요한 도구 그룹을 묻는다 (턴마다 한 번, 도구 실행 뒤에는 다시 묻지 않는다).

        이번 턴에 쓸 수 없는 도구(기억을 끈 사용자, 프로젝트 밖)의 그룹은 묻지 않는다.

        Args:
            state: 그래프 상태 (대화 이력, 누적 그룹).
            config: 실행 설정.

        Returns:
            누적 그룹과 이번 턴에 줄 그룹.
        """
        hidden = hidden_tools(config, use_memory=use_memory)
        candidates = {tool_group(t.name) for t in tools if t.name not in hidden}
        result = await decision.decide(
            settings, state["messages"], candidates, state.get("tool_groups") or [], ask
        )
        return {
            "tool_groups": list(result.groups),
            "active_groups": None if result.active is None else list(result.active),
        }

    async def agent(state: ChatState, config: RunnableConfig) -> dict[str, Any]:
        """System 프롬프트 + (프로젝트 정보) + (요약) + 최근 대화로 모델을 호출한다.

        system 프롬프트는 상태에 저장하지 않고 호출할 때마다 렌더링해서 앞에 붙인다.
        프로젝트 안의 대화면 프로젝트 지시문과 문서 목록을 붙인다 (``config["configurable"]["project"]``,
        ``app.projects.service.ProjectContext``). 대화 이력 예산이 있고 요약이 있으면, 요약을
        system 프롬프트 끝에 붙이고 요약 이후의 메시지만 보낸다. 장기 기억을 쓰는 사용자면 기억 지침과
        프로필을 system 프롬프트에 붙이고, 이번 턴의 관련 기억(``recall`` 노드)을 마지막 사용자 메시지 앞에 붙인다.
        이번 턴에 쓸 수 없는 도구(기억을 끈 사용자의 기억 도구, 프로젝트 밖 대화의 프로젝트 검색)와, 도구 결정을
        쓰면 이번 턴에 켜지지 않은 그룹의 도구는 빼고 호출한다.

        Args:
            state: 그래프 상태 (전체 대화 이력과 요약).
            config: 실행 설정. 이번 턴의 프로젝트 정보가 들어 있을 수 있다.

        Returns:
            상태에 추가할 모델 응답 메시지.
        """
        prompt = await prompts.arender("chat/system", today=today(settings))
        text = prompt.text
        if (project := (config.get("configurable") or {}).get("project")) is not None:
            project_prompt = await prompts.arender(
                "chat/project",
                name=project.name,
                instructions=project.instructions or "(none)",
                documents=document_list(
                    project.documents,
                    with_content=project.inline_documents(settings.project_inline_limit),
                )
                or "(none)",
            )
            text = f"{text}\n\n{project_prompt.text}"
        user_id = memory_user(config) if use_memory else None
        if user_id is not None:
            memory_prompt = await prompts.arender(
                "chat/memory", profile=state.get("profile") or "(empty)"
            )
            text = f"{text}\n\n{memory_prompt.text}"
        history = model_view(state["messages"])
        if budget is not None and state.get("summary"):
            summary_prompt = await prompts.arender(
                "chat/conversation_summary", summary=state["summary"]
            )
            text = f"{text}\n\n{summary_prompt.text}"
            history = summary.unsummarized(history, state.get("summarized_until"))
        system = SystemMessage(text)
        # checkpoint 에는 이미지 참조만 있으므로 보낼 메시지의 이미지만 저장소에서 원본으로 바꾼다.
        context = await resolve_images(get_storage(), list(select_context(system, history)))
        if user_id is not None and state.get("recalled"):
            recalled = await prompts.arender("chat/recalled_memories", memories=state["recalled"])
            context = with_recalled(context, recalled.text)
        hidden = hidden_tools(config, use_memory=use_memory)
        if use_decision and (active := state.get("active_groups")) is not None:
            hidden |= {t.name for t in tools if tool_group(t.name) not in active}
        response = await call_model(hidden).ainvoke(
            [system, *context],
            config={"metadata": prompt.trace_metadata()},
        )
        # 대화 도중 모델을 바꿨을 때 이 응답의 추론을 다른 모델에 보내지 않도록 모델을 기록한다.
        return {"messages": [tag_model(response, current_model)]}

    # agent 로 가는 길목. 도구 결정을 쓰면 agent 앞에 decide 를 거친다 (도구 실행 뒤에는 agent 로 바로 간다).
    before_agent = DECIDE_NODE if use_decision else AGENT_NODE

    def route_start(state: ChatState) -> str:
        """요약이 필요하면 요약 노드로, 아니면 바로 agent(또는 decide) 노드로 보낸다."""
        assert budget is not None
        plan = summary.plan_summary(
            model_view(state["messages"]), state.get("summarized_until"), budget, count_tokens
        )
        return SUMMARIZE_NODE if plan else before_agent

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
    if use_decision:
        builder.add_node(DECIDE_NODE, decide)
        builder.add_edge(DECIDE_NODE, AGENT_NODE)
    first = START
    if use_memory:
        builder.add_node(RECALL_NODE, recall)
        builder.add_edge(START, RECALL_NODE)
        first = RECALL_NODE
    if budget is not None:
        builder.add_node(SUMMARIZE_NODE, summarize)
        builder.add_conditional_edges(first, route_start, [SUMMARIZE_NODE, before_agent])
        builder.add_edge(SUMMARIZE_NODE, before_agent)
    else:
        builder.add_edge(first, before_agent)

    if tools:
        # 도구가 실패해도 응답 전체가 실패하지 않게, 예외를 모델이 읽을 오류 결과로 바꾼다.
        tool_node = ToolNode(tools, handle_tool_errors=handle_tool_error)

        async def run_tools(state: ChatState, config: RunnableConfig) -> dict[str, Any]:
            # 출처 번호는 도구가 아니라 여기서 한 턴 단위로 매긴다 (동시에 실행된 도구도 번호가 겹치지 않게).
            result = await tool_node.ainvoke(state, config)
            return {"messages": number_sources(state["messages"], result["messages"])}

        builder.add_node("tools", run_tools)
        builder.add_conditional_edges(AGENT_NODE, tools_condition)
        builder.add_edge("tools", AGENT_NODE)
    else:
        builder.add_edge(AGENT_NODE, END)

    return builder.compile(checkpointer=checkpointer, store=store if use_memory else None)
