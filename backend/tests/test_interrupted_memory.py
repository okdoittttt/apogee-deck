"""중단된 응답과 모델 기억(checkpoint) 검증 (ROADMAP 4번).

응답 도중 끊기면(중지, 시간 초과, 앱 종료, 화면이 구독을 끊음, 오류) LangGraph 는 노드가 끝나지 않아
checkpoint 를 쓰지 않는다. 그대로 두면 모델은 다음 턴에 자기가 한 말을 모르고, checkpoint 에는 사용자 메시지가
연달아 남는다. 실행 엔진(``runtime.stream_reply``)이 진행 중이던 답을 중단 표시와 함께 기억에 넣는다.
"""

import asyncio
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.agents import chat
from app.core.config import Settings
from app.llm.fake import EchoChatModel
from app.orchestration import runtime
from app.orchestration.runtime import (
    INTERRUPTED_NOTE,
    Finished,
    ReasoningDelta,
    TextDelta,
    ToolFinished,
)

# 조각 하나마다 0.02초 쉬어서 답변 도중에 끊을 틈을 만든다.
SETTINGS = Settings(llm_provider="fake", fake_stream_delay_seconds=0.02)


@pytest.fixture
def graph(monkeypatch):
    graph = chat.build_graph(SETTINGS, InMemorySaver())
    monkeypatch.setitem(runtime._graphs, "chat", graph)
    return graph


async def _interrupt(thread_id: str, text: str, when) -> str:
    """응답을 시작하고 ``when(event, 지금까지의 이벤트)`` 가 참이 되면 끊는다 (연결이 끊긴 요청이 취소되는 것과 같다).

    Returns:
        끊기 전까지 화면에 보인 글자.
    """
    events: list = []
    reached = asyncio.Event()

    async def consume() -> None:
        async for event in runtime.stream_reply(thread_id, text, []):
            events.append(event)
            if when(event, events):
                reached.set()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(reached.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return "".join(e.text for e in events if isinstance(e, TextDelta))


def _first_text(event, _events) -> bool:
    return isinstance(event, TextDelta)


async def _messages(graph, thread_id: str):
    state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    return state.values.get("messages", [])


async def _finish(thread_id: str, text: str) -> str:
    events = [e async for e in runtime.stream_reply(thread_id, text, [])]
    return next(e for e in events if isinstance(e, Finished)).text


async def test_interrupted_reply_is_remembered(graph):
    """끊긴 답이 중단 표시와 함께 기억에 남고, 다음 턴은 사용자 → 응답 → 사용자 → 응답 순서다."""
    thread = f"t-{uuid.uuid4()}"
    shown = await _interrupt(thread, "긴 설명을 해 줘", _first_text)
    assert shown

    memory = await _messages(graph, thread)
    assert [type(m) for m in memory] == [HumanMessage, AIMessage]
    assert memory[-1].text == shown + INTERRUPTED_NOTE  # 보여 준 만큼 그대로 + 중단 표시

    reply = await _finish(thread, "방금 하던 거 이어서 해 줘")
    kinds = [type(m) for m in await _messages(graph, thread)]
    assert kinds == [HumanMessage, AIMessage, HumanMessage, AIMessage]
    assert reply.startswith("[echo · turn 2]")  # 다음 턴은 평소처럼 실행된다


async def test_only_the_unsaved_answer_is_added_after_a_tool_call(graph):
    """도구를 부른 앞 단계는 이미 기억에 있다. 두 번째 답에서 끊기면 그 글자만 넣는다 (중복 없음)."""
    thread = f"t-{uuid.uuid4()}"

    def second_answer(event, events):
        return isinstance(event, TextDelta) and any(isinstance(e, ToolFinished) for e in events)

    shown = await _interrupt(thread, f"{EchoChatModel.TOOL_MARKER} 지금 몇 시야?", second_answer)
    memory = await _messages(graph, thread)
    assert [type(m) for m in memory] == [HumanMessage, AIMessage, ToolMessage, AIMessage]
    first, last = memory[1], memory[-1]
    assert first.tool_calls and first.text.strip() == "시간을 확인해 볼게요."
    assert "시간을 확인해 볼게요" not in last.text  # 앞 단계 글자는 다시 넣지 않는다
    assert last.text.endswith(INTERRUPTED_NOTE)
    assert shown.rstrip().endswith(last.text.removesuffix(INTERRUPTED_NOTE).strip())


async def test_reply_cut_by_an_error_is_remembered(graph):
    thread = f"t-{uuid.uuid4()}"
    with pytest.raises(RuntimeError, match="FAIL_MARKER"):
        async for _ in runtime.stream_reply(thread, f"설명해 줘 {EchoChatModel.FAIL_MARKER}", []):
            pass
    memory = await _messages(graph, thread)
    assert [type(m) for m in memory] == [HumanMessage, AIMessage]
    assert (
        memory[-1].text == "[echo · " + INTERRUPTED_NOTE
    )  # fake 모델은 세 번째 단어에서 오류를 낸다


async def test_nothing_is_added_when_no_text_was_shown(graph):
    thread = f"t-{uuid.uuid4()}"

    def first_reasoning(event, _events):
        return isinstance(event, ReasoningDelta)

    shown = await _interrupt(thread, f"{EchoChatModel.THINK_MARKER} 생각해 봐", first_reasoning)
    assert shown == ""
    assert [type(m) for m in await _messages(graph, thread)] == [HumanMessage]  # 추론은 넣지 않는다


async def test_memory_failure_does_not_hide_the_interruption(graph, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("checkpoint down")

    monkeypatch.setattr(graph, "aupdate_state", broken)
    thread = f"t-{uuid.uuid4()}"
    # 기억에 넣지 못해도 원래의 취소가 그대로 전달된다 (_interrupt 가 CancelledError 를 확인한다)
    assert await _interrupt(thread, "긴 설명을 해 줘", _first_text)
    assert [type(m) for m in await _messages(graph, thread)] == [HumanMessage]
