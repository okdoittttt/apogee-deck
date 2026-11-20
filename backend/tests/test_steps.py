"""실행 단계(추론 과정, 도구 호출) 표시와 저장 검증."""

import json
import uuid

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk
from langgraph.checkpoint.memory import InMemorySaver

from app.agents import chat
from app.api.routers import chat as chat_router
from app.api.schemas import MessageOut
from app.conversations import runs, turns
from app.conversations.runs import InMemoryRunRegistry
from app.core.config import Settings
from app.orchestration import runtime
from app.orchestration.events import (
    MAX_TOOL_OUTPUT_CHARS,
    Finished,
    ReasoningDelta,
    StepRecorder,
    TextDelta,
    ToolFinished,
    ToolLabel,
    ToolStarted,
    reasoning_text,
    tool_output_text,
)

# --- 추론 텍스트 꺼내기 (provider 별 형식) ------------------------------------------------------


def test_reasoning_from_anthropic_thinking_block():
    chunk = AIMessageChunk(content=[{"type": "thinking", "thinking": "단계별로 ", "index": 0}])
    assert reasoning_text(chunk) == "단계별로 "
    assert chunk.text == ""  # 추론은 답변 텍스트에 섞이지 않는다


def test_reasoning_from_ollama_additional_kwargs():
    chunk = AIMessageChunk("", additional_kwargs={"reasoning_content": "생각 중"})
    assert reasoning_text(chunk) == "생각 중"


def test_reasoning_from_standard_block_and_none():
    assert reasoning_text(AIMessage(content=[{"type": "reasoning", "reasoning": "r"}])) == "r"
    assert reasoning_text(AIMessageChunk("그냥 답변")) == ""


def test_tool_output_is_truncated():
    out = tool_output_text("x" * (MAX_TOOL_OUTPUT_CHARS + 50))
    assert out.startswith("x" * MAX_TOOL_OUTPUT_CHARS) and "50자 생략" in out


# --- 실행 단계 기록 -----------------------------------------------------------------------------

LABEL = ToolLabel(running="시간을 확인하는 중", done="시간 확인함")


def test_recorder_merges_reasoning_and_tracks_tools():
    rec = StepRecorder()
    for event in [
        ReasoningDelta("생각 "),
        ReasoningDelta("중"),
        TextDelta("답변은 기록하지 않음"),
        ToolStarted("c1", "get_current_time", {"timezone": "Asia/Seoul"}, LABEL),
        ToolFinished("c1", "10:00", "done"),
        ReasoningDelta("결과 확인"),
    ]:
        rec.apply(event)
    assert [s["type"] for s in rec.steps] == ["reasoning", "tool", "reasoning"]
    assert rec.steps[0]["text"] == "생각 중"
    assert rec.steps[1] | {} == {
        "type": "tool",
        "id": "c1",
        "name": "get_current_time",
        "label": "시간을 확인하는 중",
        "done_label": "시간 확인함",
        "args": {"timezone": "Asia/Seoul"},
        "output": "10:00",
        "status": "done",
    }
    # 저장 형식은 API 스키마와 같다
    MessageOut(
        id=uuid.uuid4(),
        role="assistant",
        content="",
        created_at="2026-01-01T00:00:00Z",
        steps=rec.to_json(),
    )


@pytest.mark.parametrize(
    ("outcome", "status"), [("interrupted", "interrupted"), ("error", "error")]
)
def test_recorder_finalizes_running_tools(outcome, status):
    rec = StepRecorder()
    rec.apply(ToolStarted("c1", "t", {}, LABEL))
    rec.finalize(outcome)
    assert rec.steps[0]["status"] == status


def test_recorder_empty_is_none():
    assert StepRecorder().to_json() is None


# --- 그래프 실행: 이벤트 순서 --------------------------------------------------------------------


async def test_stream_reply_emits_reasoning_and_tool_events(monkeypatch):
    graph = chat.build_graph(Settings(llm_provider="fake"), InMemorySaver())
    monkeypatch.setitem(runtime._graphs, "chat", graph)

    events = [e async for e in runtime.stream_reply("s1", "[[think]] [[tool]] 지금 몇 시야?")]
    kinds = [type(e).__name__ for e in events]

    first = {k: kinds.index(k) for k in ("ReasoningDelta", "ToolStarted", "ToolFinished")}
    assert first["ReasoningDelta"] < first["ToolStarted"] < first["ToolFinished"]
    assert kinds[-1] == "Finished"
    started = next(e for e in events if isinstance(e, ToolStarted))
    finished = next(e for e in events if isinstance(e, ToolFinished))
    assert started.id == finished.id and started.label == chat.TOOL_LABELS["get_current_time"]
    assert finished.status == "done" and "KST" in finished.output
    # 도구 결과를 받은 뒤의 답변에 결과가 들어간다
    assert "도구 결과:" in events[-1].text
    # 추론 텍스트는 답변 텍스트에 섞이지 않는다
    assert "생각하는" not in events[-1].text


# --- API: SSE 이벤트와 저장 ----------------------------------------------------------------------


@pytest.fixture
def capture(monkeypatch):
    saved: list[tuple[str, list | None]] = []

    async def fake_save(conversation_id, content, steps=None, model=None):
        saved.append((content, steps))

    monkeypatch.setattr(turns, "save_assistant_message", fake_save)
    monkeypatch.setattr(runs, "run_registry", InMemoryRunRegistry(60))
    return saved


def _patch_stream(monkeypatch, steps):
    async def fake_stream_reply(conversation_id, text, images, **kwargs):
        for step in steps:
            if step == "boom":
                raise RuntimeError("model crashed")
            yield step

    monkeypatch.setattr(turns, "stream_reply", fake_stream_reply)


def _events(raw):
    return [
        (item.split("\n", 1)[0].removeprefix("event: "), json.loads(item.split("data: ", 1)[1]))
        for item in raw
    ]


async def test_sse_events_and_saved_steps(monkeypatch, capture):
    _patch_stream(
        monkeypatch,
        [
            ReasoningDelta("생각"),
            ToolStarted("c1", "get_current_time", {"timezone": "Asia/Seoul"}, LABEL),
            ToolFinished("c1", "10:00", "done"),
            TextDelta("10시입니다"),
            Finished("10시입니다", "end_turn", "fake"),
        ],
    )
    events = _events(
        [e async for e in chat_router.sse_stream(turns.run_turn(uuid.uuid4(), "hi", []))]
    )
    assert [e for e, _ in events] == [
        "meta",
        "reasoning",
        "tool_start",
        "tool_end",
        "delta",
        "done",
    ]
    assert events[2][1] == {
        "id": "c1",
        "name": "get_current_time",
        "label": "시간을 확인하는 중",
        "done_label": "시간 확인함",
        "args": {"timezone": "Asia/Seoul"},
    }
    content, steps = capture[0]
    assert content == "10시입니다"
    assert [s["type"] for s in steps] == ["reasoning", "tool"] and steps[1]["status"] == "done"


async def test_error_during_tool_saves_steps_with_error(monkeypatch, capture):
    _patch_stream(monkeypatch, [ToolStarted("c1", "t", {}, LABEL), "boom"])
    [e async for e in chat_router.sse_stream(turns.run_turn(uuid.uuid4(), "hi", []))]
    content, steps = capture[0]
    # 답변 텍스트가 없어도 실행 단계가 있으면 저장한다
    assert content == turns.ERROR_SUFFIX.strip()
    assert steps[0]["status"] == "error"


async def test_tool_sources_are_sent_and_saved(monkeypatch, capture):
    sources = [{"title": "기사", "url": "https://news.example/1", "published_date": "2026-11-19"}]
    _patch_stream(
        monkeypatch,
        [
            ToolStarted("c1", "web_search", {"query": "q"}, LABEL),
            ToolFinished("c1", "results", "done", sources),
            TextDelta("답"),
            Finished("답", "end_turn", "fake"),
        ],
    )
    events = _events(
        [e async for e in chat_router.sse_stream(turns.run_turn(uuid.uuid4(), "hi", []))]
    )
    tool_end = next(data for name, data in events if name == "tool_end")
    assert tool_end["sources"] == sources
    _, steps = capture[0]
    assert steps[0]["sources"] == sources
    # 저장 형식은 API 스키마와 같고, 출처가 없던 예전 단계도 읽힌다
    message = MessageOut(
        id=uuid.uuid4(),
        role="assistant",
        content="",
        created_at="2026-01-01T00:00:00Z",
        steps=steps,
    )
    assert message.steps[0].sources[0].url == "https://news.example/1"
    old = {k: v for k, v in steps[0].items() if k != "sources"}
    assert (
        MessageOut(
            id=uuid.uuid4(),
            role="assistant",
            content="",
            created_at="2026-01-01T00:00:00Z",
            steps=[old],
        )
        .steps[0]
        .sources
        == []
    )
