"""중단된 응답과 모델 기억(checkpoint) 재현 (ROADMAP 4번).

응답 도중 연결이 끊기면(또는 중지하면) 화면용 messages 테이블에는 부분 응답이 저장되지만, LangGraph 는
노드가 끝나야 checkpoint 를 쓰므로 모델의 기억에는 그 응답이 없다. 다음 턴에 모델은 자기가 한 말을 모르고,
checkpoint 에는 사용자 메시지가 두 번 연속으로 남는다.

지금 동작을 확인하는 테스트와, 고친 뒤의 동작을 적은 테스트(``xfail``, 고치면 통과로 바뀐다)를 함께 둔다.
"""

import asyncio
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.agents import chat
from app.core.config import Settings
from app.orchestration import runtime
from app.orchestration.runtime import Finished, TextDelta

# 조각 하나마다 0.02초 쉬어서 답변 도중에 끊을 틈을 만든다.
SETTINGS = Settings(llm_provider="fake", fake_stream_delay_seconds=0.02)


@pytest.fixture
def graph(monkeypatch):
    graph = chat.build_graph(SETTINGS, InMemorySaver())
    monkeypatch.setitem(runtime._graphs, "chat", graph)
    return graph


async def _interrupt_after_first_text(thread_id: str, text: str) -> str:
    """응답을 시작하고 첫 글자 조각이 나오면 끊는다 (연결이 끊긴 요청이 취소되는 것과 같다).

    Returns:
        끊기 전까지 화면에 보인 글자.
    """
    shown: list[str] = []
    first_text = asyncio.Event()

    async def consume() -> None:
        async for event in runtime.stream_reply(thread_id, text, []):
            if isinstance(event, TextDelta):
                shown.append(event.text)
                first_text.set()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(first_text.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return "".join(shown)


async def _messages(graph, thread_id: str):
    state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    return state.values.get("messages", [])


async def _finish(thread_id: str, text: str) -> str:
    events = [e async for e in runtime.stream_reply(thread_id, text, [])]
    return next(e for e in events if isinstance(e, Finished)).text


async def test_interrupted_reply_is_missing_from_model_memory_today(graph):
    """지금 동작: 끊긴 응답은 checkpoint 에 없고, 다음 턴에 사용자 메시지가 연달아 남는다."""
    thread = f"t-{uuid.uuid4()}"
    shown = await _interrupt_after_first_text(thread, "긴 설명을 해 줘")
    assert shown  # 화면에는 응답 일부가 보였다 (messages 테이블에는 이것이 저장된다)

    memory = await _messages(graph, thread)
    assert [type(m).__name__ for m in memory] == ["HumanMessage"]  # 응답은 기억에 없다

    reply = await _finish(thread, "방금 하던 거 이어서 해 줘")
    memory = await _messages(graph, thread)
    assert [type(m).__name__ for m in memory] == ["HumanMessage", "HumanMessage", "AIMessage"]
    assert reply.startswith("[echo · turn 2]")  # 모델은 사용자 턴 둘만 보고, 자기 답은 모른다


@pytest.mark.xfail(strict=True, reason="ROADMAP 4번: 중단된 응답을 checkpoint 에 넣기 전까지 실패")
async def test_interrupted_reply_is_remembered(graph):
    """고친 뒤: 끊긴 응답이 중단 표시와 함께 기억에 남고, 다음 턴은 사용자 → 응답 → 사용자 순서다."""
    thread = f"t-{uuid.uuid4()}"
    shown = await _interrupt_after_first_text(thread, "긴 설명을 해 줘")

    memory = await _messages(graph, thread)
    assert isinstance(memory[-1], AIMessage)
    assert memory[-1].text.startswith(shown)  # 보여 준 만큼은 그대로 기억한다

    await _finish(thread, "방금 하던 거 이어서 해 줘")
    kinds = [type(m) for m in await _messages(graph, thread)]
    assert kinds == [HumanMessage, AIMessage, HumanMessage, AIMessage]
