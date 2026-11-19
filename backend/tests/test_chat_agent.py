"""LLM 없이(fake 모델) 채팅 에이전트 그래프를 검증한다."""

from langchain_core.messages import SystemMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.agents import chat, runtime
from app.agents.runtime import Finished, TextDelta
from app.config import Settings


async def _run(conversation_id: str, text: str, images=None) -> tuple[str, Finished]:
    chunks = [c async for c in runtime.stream_reply(conversation_id, text, images)]
    streamed = "".join(c.text for c in chunks if isinstance(c, TextDelta))
    return streamed, chunks[-1]


async def test_checkpointer_keeps_history(monkeypatch):
    graph = chat.build_graph(Settings(llm_provider="fake"), InMemorySaver())
    monkeypatch.setitem(runtime._graphs, "chat", graph)

    streamed, done = await _run("t1", "안녕")
    assert "turn 1" in streamed and done.stop_reason == "end_turn"
    assert done.text == streamed  # UI 에 저장되는 텍스트 = 화면에 보인 텍스트

    streamed, _ = await _run("t1", "두 번째")
    assert "turn 2" in streamed


async def test_images_are_passed_to_model(monkeypatch):
    graph = chat.build_graph(Settings(llm_provider="fake"), InMemorySaver())
    monkeypatch.setitem(runtime._graphs, "chat", graph)

    streamed, _ = await _run("t2", "이게 뭐야?", [("image/png", "iVBORw0KGgo=")])
    assert "(+1 image)" in streamed


async def test_system_prompt_is_rendered_not_stored(monkeypatch):
    from app.llm.fake import EchoChatModel

    seen: list = []
    original = EchoChatModel._astream

    def spy(self, messages, *args, **kwargs):
        seen.append(messages)
        return original(self, messages, *args, **kwargs)

    monkeypatch.setattr(EchoChatModel, "_astream", spy)
    graph = chat.build_graph(Settings(llm_provider="fake"), InMemorySaver())
    monkeypatch.setitem(runtime._graphs, "chat", graph)
    await _run("t3", "hi")

    # 모델이 받은 입력의 첫 메시지는 렌더링된 system 프롬프트
    assert seen and isinstance(seen[0][0], SystemMessage)
    assert "Today's date is" in seen[0][0].text
    # 하지만 checkpointer 상태에는 저장되지 않는다
    state = await graph.aget_state({"configurable": {"thread_id": "t3"}})
    assert not any(isinstance(m, SystemMessage) for m in state.values["messages"])


async def test_trimming_keeps_recent_turns(monkeypatch):
    # 예산(700) - system 프롬프트(약 300) 안에 턴당 약 200 토큰짜리 대화가 2턴 이상 들어가지 않는다.
    graph = chat.build_graph(Settings(llm_provider="fake", context_max_tokens=700), InMemorySaver())
    monkeypatch.setitem(runtime._graphs, "chat", graph)
    for i in range(6):
        streamed, _ = await _run("t4", f"질문 {i} " + "가" * 400)
    # echo 모델은 자기가 본 사용자 턴 수를 출력한다 → 최근 1~2턴만 봤어야 한다
    assert "turn 1]" in streamed or "turn 2]" in streamed
    # checkpointer 에는 6턴이 모두 남아 있다
    state = await graph.aget_state({"configurable": {"thread_id": "t4"}})
    assert len(state.values["messages"]) == 12


async def test_delete_thread_removes_agent_state(monkeypatch):
    saver = InMemorySaver()
    graph = chat.build_graph(Settings(llm_provider="fake"), saver)
    monkeypatch.setitem(runtime._graphs, "chat", graph)
    monkeypatch.setattr(runtime, "_checkpointer", saver)
    config = {"configurable": {"thread_id": "t5"}}

    await _run("t5", "hi")
    await _run("keep", "hi")
    assert (await graph.aget_state(config)).values["messages"]

    await runtime.delete_thread("t5")
    assert not (await graph.aget_state(config)).values  # 상태가 비었다
    keep = await graph.aget_state({"configurable": {"thread_id": "keep"}})
    assert keep.values["messages"]  # 다른 대화는 그대로

    streamed, _ = await _run("t5", "again")  # 같은 id 로 새로 시작하면 1턴부터
    assert "turn 1" in streamed
