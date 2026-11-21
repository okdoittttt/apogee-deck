"""백그라운드 응답 실행 검증: 연결이 끊겨도 끝까지, 여러 구독자, 시간 상한, 앱 종료, 기록 보관."""

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.api.routers import chat as chat_router
from app.api.schemas import ChatRequest
from app.conversations import runs, turns
from app.conversations.runs import InMemoryRunRegistry
from app.orchestration.runtime import Finished, TextDelta

USER = SimpleNamespace(id=uuid.uuid4(), status="approved")


@pytest.fixture
def registry(monkeypatch):
    reg = InMemoryRunRegistry(stale_after_seconds=60)
    monkeypatch.setattr(runs, "run_registry", reg)
    return reg


@pytest.fixture
def saved(monkeypatch):
    out: list[str] = []

    async def fake_save(conversation_id, content, steps=None, model=None):
        out.append(content)

    monkeypatch.setattr(turns, "save_assistant_message", fake_save)
    return out


def _reply(monkeypatch, *steps, gate: asyncio.Event | None = None):
    """``stream_reply`` 를 바꾼다. ``"wait"`` 는 ``gate`` 가 열릴 때까지, ``"hang"`` 은 오래 멈춘다."""

    async def fake_stream_reply(conversation_id, text, images, **kwargs):
        for step in steps:
            if step == "wait":
                await gate.wait()
            elif step == "hang":
                await asyncio.sleep(30)
            else:
                yield step

    monkeypatch.setattr(turns, "stream_reply", fake_stream_reply)


async def _start(registry, cid=None):
    cid = cid or uuid.uuid4()
    await registry.try_acquire(str(cid))
    return cid, turns.start_reply(cid, "hi", [])


async def test_reply_finishes_and_is_saved_after_the_viewer_leaves(monkeypatch, registry, saved):
    gate = asyncio.Event()
    _reply(
        monkeypatch,
        TextDelta("앞부분 "),
        "wait",
        TextDelta("뒷부분"),
        Finished("앞부분 뒷부분", "end_turn", "fake"),
        gate=gate,
    )
    cid, run = await _start(registry)

    # 화면이 첫 조각을 받고 떠난다 (새로고침, 탭 닫기)
    async for event in run.follow_events():
        if event.event == "delta":
            break
    gate.set()
    await asyncio.wait_for(run.task, timeout=5)

    assert saved == ["앞부분 뒷부분"]  # 중단 표시 없이 끝까지 저장됐다
    assert [e.event for e in run.events][-1] == "done"
    assert not await registry.is_running(str(cid))


async def test_late_and_multiple_viewers_get_the_same_events(monkeypatch, registry, saved):
    gate = asyncio.Event()
    _reply(
        monkeypatch,
        TextDelta("a"),
        "wait",
        TextDelta("b"),
        Finished("ab", "end_turn", "fake"),
        gate=gate,
    )
    cid, run = await _start(registry)

    async def collect(after=0):
        return [(i, e.event) for i, e in [x async for x in run.follow(after)]]

    first = asyncio.create_task(collect())
    await asyncio.sleep(0.01)
    second = asyncio.create_task(collect())  # 생성 중에 붙은 다른 창
    gate.set()
    a, b = await asyncio.gather(first, second)
    assert a == b == [(1, "meta"), (2, "delta"), (3, "delta"), (4, "done")]
    # 끝난 뒤 붙어도 기록이 남아 있고, 받은 번호 다음부터 받을 수 있다
    assert await collect(after=2) == [(3, "delta"), (4, "done")]
    assert runs.get_stream(str(cid)) is run


async def test_run_is_stopped_after_max_seconds(monkeypatch, registry, saved):
    monkeypatch.setattr(turns.get_settings(), "run_max_seconds", 0.05)
    _reply(monkeypatch, TextDelta("오래 걸리는 "), "hang")
    cid, run = await _start(registry)
    await asyncio.wait_for(run.task, timeout=5)

    assert saved == ["오래 걸리는 " + turns.INTERRUPTED_SUFFIX]  # 그때까지의 응답은 중단으로 저장
    last = run.events[-1]
    assert last.event == "error" and "응답 시간이 너무 길어" in last.data["message"]
    assert not await registry.is_running(str(cid))


async def test_shutdown_saves_running_replies_as_interrupted(monkeypatch, registry, saved):
    _reply(monkeypatch, TextDelta("진행 중"), "hang")
    cid, run = await _start(registry)
    async for event in run.follow_events():
        if event.event == "delta":
            break
    await runs.shutdown_runs()

    assert run.task.done() and run.done
    assert saved == ["진행 중" + turns.INTERRUPTED_SUFFIX]
    assert not await registry.is_running(str(cid))
    assert runs.get_stream(str(cid)) is None


async def test_finished_runs_are_forgotten_after_retention(monkeypatch, registry, saved):
    monkeypatch.setattr(runs, "RETAIN_SECONDS", 0.01)
    _reply(monkeypatch, Finished("", "end_turn", "fake"))
    cid, run = await _start(registry)
    await run.task
    assert runs.get_stream(str(cid)) is run
    await asyncio.sleep(0.05)
    assert runs.get_stream(str(cid)) is None


async def test_chat_endpoint_streams_a_background_run(monkeypatch, registry, saved):
    gate = asyncio.Event()
    _reply(
        monkeypatch,
        TextDelta("안녕"),
        "wait",
        Finished("안녕하세요", "end_turn", "fake"),
        gate=gate,
    )
    monkeypatch.setattr(
        chat_router.service,
        "start_turn",
        _fake_start_turn(registry),
    )
    response = await chat_router.chat(ChatRequest(message="hi"), None, USER)

    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
        if "event: delta" in chunk:
            break
    assert chunks[0].startswith("id: 1\nevent: meta")  # 이벤트마다 번호가 붙는다
    await response.body_iterator.aclose()  # 연결이 끊긴다

    [run] = list(runs._streams.values())
    assert not run.done  # 구독을 끊어도 실행은 계속된다
    gate.set()
    await asyncio.wait_for(run.task, timeout=5)
    assert saved == ["안녕하세요"]


def _fake_start_turn(registry):
    async def start_turn(session, user_id, **kwargs):
        cid = uuid.uuid4()
        await registry.try_acquire(str(cid))
        return SimpleNamespace(
            conversation_id=cid, text="hi", images=[], model=None, project_id=None
        )

    return start_turn


# --- 중지와 다시 붙기 ------------------------------------------------------------------------------


async def test_stop_saves_partial_releases_and_tells_viewers(monkeypatch, registry, saved):
    _reply(monkeypatch, TextDelta("멈출 응답"), "hang")
    cid, run = await _start(registry)
    seen: list[str] = []

    async def watch():  # 다른 창
        async for event in run.follow_events():
            seen.append(event.event)

    viewer = asyncio.create_task(watch())
    while "delta" not in seen:
        await asyncio.sleep(0.01)

    assert await turns.stop_reply(cid) is True
    # 돌려받은 시점에 이미 저장·해제가 끝났다 (화면이 대기 메시지를 바로 보낼 수 있다)
    assert saved == ["멈출 응답" + turns.INTERRUPTED_SUFFIX]
    assert not await registry.is_running(str(cid))
    await asyncio.wait_for(viewer, timeout=5)
    assert seen[-1] == "stopped"
    assert await turns.stop_reply(cid) is False  # 이미 끝난 실행


async def test_stop_without_a_run_does_nothing(registry):
    assert await turns.stop_reply(uuid.uuid4()) is False


@pytest.fixture
def owned(monkeypatch):
    async def get_own(session, conversation_id, user_id):
        return SimpleNamespace(id=conversation_id, user_id=user_id)

    monkeypatch.setattr(chat_router.service, "get_own", get_own)


async def _body(response) -> str:
    return "".join([chunk async for chunk in response.body_iterator])


async def test_stream_endpoint_resumes_after_last_event_id(monkeypatch, registry, saved, owned):
    _reply(monkeypatch, TextDelta("a"), TextDelta("b"), Finished("ab", "end_turn", "fake"))
    cid, run = await _start(registry)
    await run.task

    response = await chat_router.stream_conversation(cid, None, USER, last_event_id="2")
    body = await _body(response)
    assert body.startswith('id: 3\nevent: delta\ndata: {"text": "b"}\n\n')
    assert "id: 4\nevent: done" in body
    assert "id: 2\n" not in body  # 이미 받은 이벤트는 다시 보내지 않는다

    again = await chat_router.stream_conversation(cid, None, USER, after=3)
    assert (await _body(again)).startswith("id: 4\nevent: done")


async def test_stream_endpoint_without_a_run_is_204(registry, owned):
    response = await chat_router.stream_conversation(uuid.uuid4(), None, USER)
    assert response.status_code == 204


async def test_stream_and_stop_endpoints_check_ownership(monkeypatch, registry):
    async def not_mine(session, conversation_id, user_id):
        raise chat_router.ConversationError(404, "Conversation not found")

    monkeypatch.setattr(chat_router.service, "get_own", not_mine)
    for call in (chat_router.stream_conversation, chat_router.stop_conversation):
        with pytest.raises(chat_router.HTTPException) as exc:
            await call(uuid.uuid4(), None, USER)
        assert exc.value.status_code == 404


async def test_stop_endpoint_stops_the_run(monkeypatch, registry, saved, owned):
    _reply(monkeypatch, TextDelta("x"), "hang")
    cid, run = await _start(registry)
    async for event in run.follow_events():
        if event.event == "delta":
            break
    await chat_router.stop_conversation(cid, None, USER)
    assert run.done and saved == ["x" + turns.INTERRUPTED_SUFFIX]
