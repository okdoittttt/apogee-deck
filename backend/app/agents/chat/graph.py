"""기본 채팅 에이전트의 LangGraph 그래프.

::

    START → agent ──(tool_calls 있음)──▶ tools ─┐
              ▲                                  │
              └──────────────────────────────────┘
              └──(tool_calls 없음)──▶ END

대화 상태(messages)는 checkpointer 가 ``thread_id`` (= conversation_id) 별로 저장한다.
"""

from typing import Any

from langchain_core.messages import SystemMessage
from langchain_core.runnables import RunnableLambda
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from app import prompts
from app.agents.chat.tools import TOOLS
from app.agents.context import make_context_selector, today
from app.config import Settings
from app.llm import build_chat_model

AGENT_NODE = "agent"
STREAM_NODES = frozenset({AGENT_NODE})
"""이 노드들이 생성하는 토큰만 사용자에게 스트리밍한다."""


def build_graph(settings: Settings, checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:
    """채팅 에이전트 그래프를 만든다.

    Args:
        settings: 앱 설정. 모델, 도구 사용 여부, 컨텍스트 예산을 정한다.
        checkpointer: 대화 상태를 저장할 LangGraph checkpointer.

    Returns:
        컴파일된 그래프. ``enable_tools`` 가 꺼져 있으면 ``agent → END`` 만 있다.
    """
    tools = TOOLS if settings.enable_tools else []
    model = build_chat_model(settings)
    if tools:
        model = model.bind_tools(tools)

    select_context = make_context_selector(settings)

    # 모델 호출을 한 단계(chain)로 감싼다. Langfuse 콜백은 이 단계의 metadata 에 있는
    # langfuse_prompt 를 보고, 그 아래 LLM 호출(generation)을 해당 프롬프트 버전에 연결한다.
    call_model = RunnableLambda(model.ainvoke, name="call_model")

    async def agent(state: MessagesState) -> dict[str, Any]:
        """System 프롬프트 + 최근 대화로 모델을 호출한다.

        system 프롬프트는 상태에 저장하지 않고 호출할 때마다 렌더링해서 앞에 붙인다.

        Args:
            state: 그래프 상태 (전체 대화 이력).

        Returns:
            상태에 추가할 모델 응답 메시지.
        """
        prompt = prompts.render("chat/system", today=today(settings))
        system = SystemMessage(prompt.text)
        metadata: dict[str, Any] = {"prompt_name": prompt.name, "prompt_source": prompt.source}
        if prompt.langfuse_prompt is not None:
            metadata |= {
                "langfuse_prompt": prompt.langfuse_prompt,
                "prompt_version": prompt.version,
            }
        response = await call_model.ainvoke(
            [system, *select_context(system, state["messages"])],
            config={"metadata": metadata},
        )
        return {"messages": [response]}

    builder = StateGraph(MessagesState)
    builder.add_node(AGENT_NODE, agent)
    builder.add_edge(START, AGENT_NODE)

    if tools:
        builder.add_node("tools", ToolNode(tools))
        builder.add_conditional_edges(AGENT_NODE, tools_condition)
        builder.add_edge("tools", AGENT_NODE)
    else:
        builder.add_edge(AGENT_NODE, END)

    return builder.compile(checkpointer=checkpointer)
