"""같은 대화에 대한 동시 요청 처리 검증: 실행 상태 기록, 거절, 해제 시점."""

import asyncio
import uuid
from types import SimpleNamespace

import anyio
import pytest
from fastapi import HTTPException

from app.api.routers import chat as chat_router
from app.api.schemas import ChatRequest
from app.conversations import runs, service, turns
from app.conversations.runs import InMemoryRunRegistry
from app.orchestration.runtime import Finished, TextDelta

# --- 실행 상태 기록 -----------------------------------------------------------------------------


async def test_registry_allows_one_run_per_conversation():
    reg = InMemoryRunRegistry(stale_after_seconds=60)
    assert await reg.try_acquire("c1")
    assert not await reg.try_acquire("c1")  # 같은 대화는 두 번째가 거절
    assert await reg.try_acquire("c2")  # 다른 대화는 상관없음
    await reg.release("c1")
    assert not await reg.is_running("c1")
    assert await reg.try_acquire("c1")


async def test_registry_expires_runs_without_heartbeat():
    reg = InMemoryRunRegistry(stale_after_seconds=0.05)
    assert await reg.try_acquire("c1")
    await asyncio.sleep(0.03)
    await reg.touch("c1")  # 진행 중이면 갱신되어 유지된다
    await asyncio.sleep(0.03)
    assert await reg.is_running("c1")
    await asyncio.sleep(0.06)  # 갱신이 끊기면(해제 누락) 일정 시간 후 풀린다
    assert not await reg.is_running("c1")
    assert await reg.try_acquire("c1")


# --- API: 거절은 메시지를 저장하기 전에 ---------------------------------------------------------


USER = SimpleNamespace(id=uuid.uuid4(), status="approved")


class FakeSession:
    """대화가 존재한다고(USER 의 대화) 가정하고, 저장 시도를 기록하는 가짜 DB 세션."""

    def __init__(self):
        self.added: list = []
        self.commits = 0

    async def get(self, model, ident, options=None):
        return SimpleNamespace(id=ident, user_id=USER.id)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1

    async def delete(self, obj):
        pass


@pytest.fixture
def registry(monkeypatch):
    reg = InMemoryRunRegistry(stale_after_seconds=60)
    monkeypatch.setattr(runs, "run_registry", reg)
    return reg


async def test_chat_rejects_busy_conversation_without_saving(registry):
    cid = uuid.uuid4()
    await registry.try_acquire(str(cid))  # 다른 창에서 생성 중
    session = FakeSession()

    with pytest.raises(HTTPException) as exc:
        await chat_router.chat(ChatRequest(conversation_id=cid, message="끼어들기"), session, USER)

    assert exc.value.status_code == 409
    assert exc.value.detail == service.BUSY_DETAIL
    assert session.added == [] and session.commits == 0


async def test_chat_marks_conversation_running(registry):
    cid = uuid.uuid4()
    session = FakeSession()
    response = await chat_router.chat(ChatRequest(conversation_id=cid, message="hi"), session, USER)
    assert session.commits == 1
    assert await registry.is_running(str(cid))
    await response.body_iterator.aclose()  # 스트림을 시작하지 않고 닫아도
    await registry.release(str(cid))


async def test_delete_rejected_while_running(registry):
    cid = uuid.uuid4()
    await registry.try_acquire(str(cid))
    with pytest.raises(HTTPException) as exc:
        await chat_router.delete_conversation(cid, FakeSession(), USER)
    assert exc.value.status_code == 409


async def test_status_endpoint(registry):
    cid = uuid.uuid4()
    assert (await chat_router.get_conversation_status(cid, FakeSession(), USER)).running is False
    await registry.try_acquire(str(cid))
    assert (await chat_router.get_conversation_status(cid, FakeSession(), USER)).running is True


# --- 해제 시점: done/error 를 보내기 전에, 중단되면 정리 단계에서 -------------------------------


def _patch_stream(monkeypatch, steps):
    async def fake_stream_reply(conversation_id, text, images, **kwargs):
        for step in steps:
            if step == "hang":
                await asyncio.sleep(30)
            elif step == "boom":
                raise RuntimeError("model crashed")
            else:
                yield step

    async def fake_save(conversation_id, content, steps=None, model=None):
        await asyncio.sleep(0)

    monkeypatch.setattr(turns, "stream_reply", fake_stream_reply)
    monkeypatch.setattr(turns, "save_assistant_message", fake_save)


@pytest.mark.parametrize(
    ("steps", "terminal"),
    [
        ([TextDelta("a"), Finished("a", "end_turn", "fake")], "done"),
        ([TextDelta("a"), "boom"], "error"),
    ],
)
async def test_released_before_terminal_event(monkeypatch, registry, steps, terminal):
    _patch_stream(monkeypatch, steps)
    cid = uuid.uuid4()
    await registry.try_acquire(str(cid))
    async for event in turns.run_turn(cid, "hi", []):
        if event.event == terminal:
            # 화면이 이 이벤트를 받자마자 대기 메시지를 보내도 거절되지 않아야 한다
            assert not await registry.is_running(str(cid))
    assert not await registry.is_running(str(cid))


async def test_released_when_client_disconnects(monkeypatch, registry):
    _patch_stream(monkeypatch, [TextDelta("a"), "hang"])
    cid = uuid.uuid4()
    await registry.try_acquire(str(cid))
    received: list[str] = []

    async def consume():
        async for event in turns.run_turn(cid, "hi", []):
            received.append(event)

    async with anyio.create_task_group() as tg:
        tg.start_soon(consume)
        while len(received) < 2:
            await anyio.sleep(0.01)
        assert await registry.is_running(str(cid))
        tg.cancel_scope.cancel()

    assert not await registry.is_running(str(cid))


async def test_disconnect_during_final_save_still_releases(monkeypatch, registry):
    """정상 종료 후 저장 중에 연결이 끊겨도 저장과 해제가 끝까지 실행된다 (E2E 에서 발견한 버그)."""
    saving = anyio.Event()
    saved: list[str] = []

    async def fake_stream_reply(conversation_id, text, images, **kwargs):
        yield TextDelta("a")
        yield Finished("a", "end_turn", "fake")

    async def slow_save(conversation_id, content, steps=None, model=None):
        saving.set()
        await anyio.sleep(0.1)  # 저장 중에 취소가 들어온다
        saved.append(content)

    monkeypatch.setattr(turns, "stream_reply", fake_stream_reply)
    monkeypatch.setattr(turns, "save_assistant_message", slow_save)
    cid = uuid.uuid4()
    await registry.try_acquire(str(cid))

    async def consume():
        async for _ in turns.run_turn(cid, "hi", []):
            pass

    async with anyio.create_task_group() as tg:
        tg.start_soon(consume)
        await saving.wait()
        tg.cancel_scope.cancel()  # 저장 도중 새로고침/중지

    assert saved == ["a"]
    assert not await registry.is_running(str(cid))
