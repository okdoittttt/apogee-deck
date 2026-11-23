"""응답 피드백 검증: 트레이스 ID 연결, DB 저장, Langfuse 점수, API."""

import sys
import uuid
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.api.routers import chat as chat_router
from app.api.schemas import FeedbackIn
from app.conversations import service, turns
from app.conversations.service import ConversationError
from app.core import tracing
from app.models import Message
from app.orchestration.runtime import Finished, TextDelta

USER = SimpleNamespace(id=uuid.uuid4(), status="approved")


@pytest.fixture
def tracing_on(monkeypatch):
    settings = SimpleNamespace(tracing_enabled=True)
    monkeypatch.setattr(tracing, "get_settings", lambda: settings)
    monkeypatch.setattr(chat_router, "get_settings", lambda: settings)
    return settings


class FakeLangfuse:
    def __init__(self):
        self.scores, self.deleted, self.flushed = [], [], 0
        self.api = SimpleNamespace(scores=SimpleNamespace(delete=self.deleted.append))

    def create_score(self, **kwargs):
        self.scores.append(kwargs)

    def flush(self):
        self.flushed += 1


@pytest.fixture
def langfuse(monkeypatch):
    client = FakeLangfuse()
    monkeypatch.setitem(sys.modules, "langfuse", SimpleNamespace(get_client=lambda: client))
    return client


# --- Langfuse 점수 ---------------------------------------------------------------------------------


def test_feedback_becomes_a_boolean_score_on_the_reply_trace(tracing_on, langfuse):
    message_id = uuid.uuid4()
    tracing.record_feedback("a" * 32, message_id, 1, None)
    tracing.record_feedback("a" * 32, message_id, -1, "출처가 틀렸어요")
    assert langfuse.scores == [
        {
            "name": "user_feedback",
            "value": 1,
            "trace_id": "a" * 32,
            "score_id": f"feedback-{message_id}",  # 같은 응답은 같은 점수를 고친다
            "data_type": "BOOLEAN",
            "comment": None,
        },
        {
            "name": "user_feedback",
            "value": 0,
            "trace_id": "a" * 32,
            "score_id": f"feedback-{message_id}",
            "data_type": "BOOLEAN",
            "comment": "출처가 틀렸어요",
        },
    ]
    tracing.record_feedback("a" * 32, message_id, None, None)
    assert langfuse.deleted == [f"feedback-{message_id}"]  # 취소하면 점수를 지운다


def test_trace_ids_are_made_only_when_tracing_is_on(monkeypatch, tracing_on):
    trace_id = tracing.new_trace_id()
    assert len(trace_id) == 32 and int(trace_id, 16) >= 0
    tracing_on.tracing_enabled = False
    assert tracing.new_trace_id() is None
    assert tracing.get_callbacks("b" * 32) == []


def test_callback_handler_uses_the_given_trace_id(monkeypatch, tracing_on):
    made = []

    class Handler:
        def __init__(self, **kwargs):
            made.append(kwargs)

    monkeypatch.setitem(sys.modules, "langfuse.langchain", SimpleNamespace(CallbackHandler=Handler))
    tracing.get_callbacks("c" * 32)
    tracing.get_callbacks()
    assert made == [{"trace_context": {"trace_id": "c" * 32}}, {}]


# --- 응답 저장: 트레이스 ID 와 메시지 ID -------------------------------------------------------------


async def test_reply_is_saved_with_its_trace_and_done_carries_the_message_id(monkeypatch):
    seen, saved = {}, {}
    message_id = uuid.uuid4()

    async def fake_stream_reply(conversation_id, text, images, **kwargs):
        seen.update(kwargs)
        yield TextDelta("답")
        yield Finished("답", "end_turn", "fake")

    async def fake_save(conversation_id, content, steps=None, model=None, trace_id=None):
        saved["trace_id"] = trace_id
        return message_id

    monkeypatch.setattr(turns, "stream_reply", fake_stream_reply)
    monkeypatch.setattr(turns, "save_assistant_message", fake_save)
    monkeypatch.setattr(turns.tracing, "new_trace_id", lambda: "d" * 32)
    events = [e async for e in turns.run_turn(uuid.uuid4(), "hi", [])]

    assert seen["trace_id"] == saved["trace_id"] == "d" * 32  # 실행과 저장이 같은 트레이스
    assert events[-1].event == "done" and events[-1].data["message_id"] == str(message_id)


# --- 서비스 ---------------------------------------------------------------------------------------


class FakeSession:
    def __init__(self, conversation, message):
        self.conversation, self.message, self.commits = conversation, message, 0

    async def get(self, model, key, **kwargs):
        if model.__name__ == "Conversation":
            return self.conversation if key == self.conversation.id else None
        return self.message if self.message is not None and key == self.message.id else None

    async def commit(self):
        self.commits += 1


def _setup(role="assistant", owner=None):
    conv = SimpleNamespace(id=uuid.uuid4(), user_id=owner or USER.id)
    message = Message(
        id=uuid.uuid4(), conversation_id=conv.id, role=role, content="답", trace_id="e" * 32
    )
    return conv, message, FakeSession(conv, message)


async def test_feedback_is_saved_and_cleared():
    conv, message, session = _setup()
    await service.set_feedback(session, conv.id, message.id, USER.id, -1, "  너무 길어요  ")
    assert (message.feedback, message.feedback_comment) == (-1, "너무 길어요")
    await service.set_feedback(session, conv.id, message.id, USER.id, None, "무시됨")
    assert (message.feedback, message.feedback_comment) == (None, None)  # 취소하면 의견도 지운다
    assert session.commits == 2


@pytest.mark.parametrize("case", ["other_user", "user_message", "other_conversation"])
async def test_feedback_only_on_my_replies(case):
    conv, message, session = _setup(
        role="user" if case == "user_message" else "assistant",
        owner=uuid.uuid4() if case == "other_user" else None,
    )
    conversation_id = uuid.uuid4() if case == "other_conversation" else conv.id
    if case == "other_conversation":
        session.conversation = SimpleNamespace(id=conversation_id, user_id=USER.id)
    with pytest.raises(ConversationError) as exc:
        await service.set_feedback(session, conversation_id, message.id, USER.id, 1)
    assert exc.value.status_code == 404


# --- API ------------------------------------------------------------------------------------------


async def test_api_saves_feedback_and_scores_it_after_responding(monkeypatch, tracing_on):
    conv, message, session = _setup()
    background = BackgroundTasks()
    out = await chat_router.put_feedback(
        conv.id, message.id, FeedbackIn(value=1, comment="좋아요"), session, USER, background
    )
    assert (out.feedback, out.comment, out.scored) == (1, "좋아요", True)
    [task] = background.tasks
    assert task.args == ("e" * 32, message.id, 1, "좋아요")


async def test_api_skips_scoring_without_a_trace(monkeypatch, tracing_on):
    conv, message, session = _setup()
    message.trace_id = None  # 이 기능 전의 응답
    background = BackgroundTasks()
    out = await chat_router.put_feedback(
        conv.id, message.id, FeedbackIn(value=-1), session, USER, background
    )
    assert out.feedback == -1 and out.scored is False and not background.tasks


async def test_api_maps_not_found(tracing_on):
    conv, message, session = _setup(owner=uuid.uuid4())
    with pytest.raises(HTTPException) as exc:
        await chat_router.put_feedback(
            conv.id, message.id, FeedbackIn(value=1), session, USER, BackgroundTasks()
        )
    assert exc.value.status_code == 404


async def test_score_failure_is_only_logged(monkeypatch, caplog):
    def broken(*args):
        raise RuntimeError("langfuse down")

    monkeypatch.setattr(chat_router.tracing, "record_feedback", broken)
    await chat_router._record_feedback("f" * 32, uuid.uuid4(), 1, None)
    assert "failed to record feedback score" in caplog.text
