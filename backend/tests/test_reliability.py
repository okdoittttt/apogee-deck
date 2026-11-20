"""운영 중 장애 상황에 대한 보완 검증: 끊긴 도구 호출, 프롬프트 조회 블로킹, 타임아웃, 중단된 응답 저장."""

import asyncio
import json
import threading
import uuid

import anyio
import httpx
import pytest
from langchain_core.exceptions import ModelAPIError, ModelRateLimitError
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app import prompts
from app.api.routers import chat as chat_router
from app.conversations import turns
from app.core.config import Settings
from app.llm import build_chat_model, describe_error, is_transient_error
from app.orchestration.context import (
    INTERRUPTED_TOOL_RESULT,
    close_dangling_tool_calls,
    make_context_selector,
)
from app.orchestration.runtime import Finished, TextDelta
from app.prompts import remote

# --- A. 결과 없이 끊긴 도구 호출 ------------------------------------------------------------------


def _tool_call(call_id: str) -> dict:
    return {"id": call_id, "name": "get_current_time", "args": {}}


def test_dangling_tool_call_gets_result_right_after_it():
    history = [
        HumanMessage("지금 몇 시야?"),
        AIMessage("", tool_calls=[_tool_call("t1")]),
        HumanMessage("됐어, 다른 질문"),  # 중지 후 이어서 보낸 메시지
    ]
    fixed = close_dangling_tool_calls(history)
    assert [type(m).__name__ for m in fixed] == [
        "HumanMessage",
        "AIMessage",
        "ToolMessage",
        "HumanMessage",
    ]
    assert fixed[2].tool_call_id == "t1"
    assert fixed[2].content == INTERRUPTED_TOOL_RESULT
    assert len(history) == 3  # 원본(상태)은 바꾸지 않는다


def test_answered_tool_calls_are_left_alone():
    history = [
        HumanMessage("q"),
        AIMessage("", tool_calls=[_tool_call("t1"), _tool_call("t2")]),
        ToolMessage("10:00", tool_call_id="t1"),
    ]
    fixed = close_dangling_tool_calls(history)
    stubs = [m for m in fixed if isinstance(m, ToolMessage) and m.tool_call_id == "t2"]
    assert len(stubs) == 1  # 결과가 빠진 t2 만 채운다
    assert sum(isinstance(m, ToolMessage) and m.tool_call_id == "t1" for m in fixed) == 1


def test_selector_repairs_before_trimming():
    history = [
        HumanMessage("q"),
        AIMessage("", tool_calls=[_tool_call("t1")]),
        HumanMessage("next"),
    ]
    for provider in ("anthropic", "ollama"):  # 자르지 않는 경우 / 자르는 경우 모두
        selected = make_context_selector(Settings(llm_provider=provider))(
            SystemMessage("sys"), history
        )
        calls = {c["id"] for m in selected if isinstance(m, AIMessage) for c in m.tool_calls}
        results = {m.tool_call_id for m in selected if isinstance(m, ToolMessage)}
        assert calls <= results


# --- B. 프롬프트 조회가 이벤트 루프를 막지 않음 -----------------------------------------------------


async def test_arender_runs_lookup_off_the_event_loop(monkeypatch):
    seen_threads: list[str] = []

    def fake_fetch(name):
        seen_threads.append(threading.current_thread().name)
        # Langfuse 를 못 쓰는 상황 → 로컬 파일

    settings = Settings(
        langfuse_public_key="pk", langfuse_secret_key="sk", prompt_source="langfuse"
    )
    monkeypatch.setattr(prompts, "get_settings", lambda: settings)
    monkeypatch.setattr(remote, "fetch", fake_fetch)

    p = await prompts.arender("chat/system", today="x")
    assert p.source == "file" and "x" in p.text
    assert seen_threads and seen_threads[0] != threading.main_thread().name


# --- C. Ollama 타임아웃 -------------------------------------------------------------------------


def test_ollama_client_has_timeout():
    model = build_chat_model(Settings(llm_provider="ollama", ollama_timeout_seconds=300))
    timeout = model._async_client._client.timeout
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == 300 and timeout.connect == 300


# --- E. 중단된 응답 저장 ------------------------------------------------------------------------


def _events(raw: list[str]) -> list[tuple[str, dict]]:
    out = []
    for item in raw:
        event = item.split("\n", 1)[0].removeprefix("event: ")
        data = json.loads(item.split("data: ", 1)[1])
        out.append((event, data))
    return out


def _patch_stream(monkeypatch, steps):
    """stream_reply 를 steps 대로 동작하는 가짜로 바꾸고, 저장된 내용을 모은다."""
    saved: list[str] = []

    async def fake_stream_reply(conversation_id, text, images, **kwargs):
        for step in steps:
            if step == "hang":
                await asyncio.sleep(30)
            elif step == "boom":
                raise RuntimeError("model crashed")
            elif isinstance(step, BaseException):
                raise step
            else:
                yield step

    async def fake_save(conversation_id, content, steps=None, model=None):
        await asyncio.sleep(0)  # 실제 DB 저장처럼 await 가 있다
        saved.append(content)

    monkeypatch.setattr(turns, "stream_reply", fake_stream_reply)
    monkeypatch.setattr(turns, "save_assistant_message", fake_save)
    return saved


async def test_finished_reply_is_saved_once(monkeypatch):
    saved = _patch_stream(
        monkeypatch,
        [TextDelta("안녕"), TextDelta("하세요"), Finished("안녕하세요", "end_turn", "fake")],
    )
    events = _events(
        [e async for e in chat_router.sse_stream(turns.run_turn(uuid.uuid4(), "hi", []))]
    )
    assert [e for e, _ in events] == ["meta", "delta", "delta", "done"]
    assert saved == ["안녕하세요"]


async def test_error_after_partial_text_saves_with_error_mark(monkeypatch):
    saved = _patch_stream(monkeypatch, [TextDelta("부분 응답"), "boom"])
    events = _events(
        [e async for e in chat_router.sse_stream(turns.run_turn(uuid.uuid4(), "hi", []))]
    )
    assert events[-1][0] == "error"
    assert saved == ["부분 응답" + turns.ERROR_SUFFIX]


async def test_error_event_hides_exception_details(monkeypatch, caplog):
    _patch_stream(monkeypatch, ["boom"])
    with caplog.at_level("ERROR"):
        events = _events(
            [e async for e in chat_router.sse_stream(turns.run_turn(uuid.uuid4(), "hi", []))]
        )
    event, data = events[-1]
    assert event == "error"
    # 예외 타입과 원문은 화면에 보내지 않는다.
    assert "RuntimeError" not in data["message"]
    assert "model crashed" not in data["message"]
    # 같은 오류 ID 로 로그에서 원문을 찾을 수 있다.
    assert data["error_id"] in data["message"]
    record = next(r for r in caplog.records if data["error_id"] in r.getMessage())
    assert "model crashed" in str(record.exc_info[1])


async def test_unknown_error_is_not_transient(monkeypatch):
    _patch_stream(monkeypatch, ["boom"])
    events = _events(
        [e async for e in chat_router.sse_stream(turns.run_turn(uuid.uuid4(), "hi", []))]
    )
    assert events[-1][1]["transient"] is False


async def test_server_error_event_is_transient(monkeypatch):
    _patch_stream(monkeypatch, [TextDelta("부분"), ModelAPIError("503 UNAVAILABLE")])
    events = _events(
        [e async for e in chat_router.sse_stream(turns.run_turn(uuid.uuid4(), "hi", []))]
    )
    event, data = events[-1]
    assert event == "error"
    assert data["transient"] is True
    assert "혼잡" in data["message"]
    assert "UNAVAILABLE" not in data["message"]


@pytest.mark.parametrize(
    ("error", "transient"),
    [
        (ModelAPIError("503"), True),
        (ModelRateLimitError("429"), True),
        (httpx.ReadTimeout("timed out"), True),
        (ConnectionError("refused"), False),
        (RuntimeError("model crashed"), False),
    ],
)
def test_is_transient_error(error, transient):
    assert is_transient_error(error) is transient


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ConnectionError("Failed to connect to Ollama"), "연결할 수 없습니다"),
        (httpx.ConnectError("connection refused"), "연결할 수 없습니다"),
        (httpx.ReadTimeout("timed out"), "시간이 초과"),
        (TimeoutError(), "시간이 초과"),
        (RuntimeError("model crashed"), None),
    ],
)
def test_describe_error_maps_actionable_errors(error, expected):
    message = describe_error(error)
    if expected is None:
        assert message is None
    else:
        assert expected in message


async def test_error_without_text_saves_nothing(monkeypatch):
    saved = _patch_stream(monkeypatch, ["boom"])
    [e async for e in chat_router.sse_stream(turns.run_turn(uuid.uuid4(), "hi", []))]
    assert saved == []


async def test_client_disconnect_saves_partial_text(monkeypatch):
    """연결이 끊기면 서버(Starlette)는 anyio 로 요청을 취소한다. 그 상황을 그대로 재현한다."""
    saved = _patch_stream(monkeypatch, [TextDelta("여기까지 "), TextDelta("보임"), "hang"])
    received: list[str] = []

    async def consume():
        async for event in chat_router.sse_stream(turns.run_turn(uuid.uuid4(), "hi", [])):
            received.append(event)

    async with anyio.create_task_group() as tg:
        tg.start_soon(consume)
        while len(received) < 3:  # meta + delta 2개를 받은 뒤
            await anyio.sleep(0.01)
        tg.cancel_scope.cancel()  # 사용자가 중지 → 요청 취소

    assert saved == ["여기까지 보임" + turns.INTERRUPTED_SUFFIX]


async def test_empty_reply_saves_nothing(monkeypatch):
    saved = _patch_stream(monkeypatch, [Finished("", "end_turn", "fake")])
    events = _events(
        [e async for e in chat_router.sse_stream(turns.run_turn(uuid.uuid4(), "hi", []))]
    )
    assert [e for e, _ in events] == ["meta", "done"]
    assert saved == []
