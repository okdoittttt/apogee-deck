"""채팅 이미지 검증: 저장소 저장, checkpoint 참조, 모델 호출 직전 원본 복원, 삭제."""

import base64
import uuid
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from app import images
from app.agents import chat, runtime
from app.agents.chat import graph as chat_graph
from app.agents.runtime import TextDelta
from app.config import Settings
from app.models import MessageImage
from app.storage import ObjectNotFoundError

PNG = b"\x89PNG fake image bytes"


class FakeStorage:
    """메모리에 객체를 두는 첨부 저장소. ``fail_on`` 키를 올리면 실패한다."""

    def __init__(self, fail_on: str | None = None):
        self.objects: dict[str, bytes] = {}
        self.fail_on = fail_on

    async def put(self, key, data, content_type):
        if key == self.fail_on:
            raise OSError("storage down")
        self.objects[key] = data

    async def get(self, key):
        if key not in self.objects:
            raise ObjectNotFoundError(key)
        return self.objects[key]

    async def delete(self, key):
        self.objects.pop(key, None)


def ref(image_id: uuid.UUID, mime="image/png") -> dict:
    return images.image_ref_block(MessageImage(id=image_id, mime_type=mime))


# --- 저장 ---------------------------------------------------------------------------------------


async def test_store_images_puts_originals_under_images_prefix():
    storage = FakeStorage()
    rows = await images.store_images(storage, [("image/png", PNG), ("image/jpeg", b"jpg")])
    assert [r.position for r in rows] == [0, 1]
    assert all(r.storage_key == f"images/{r.id}" for r in rows)
    assert storage.objects == {rows[0].storage_key: PNG, rows[1].storage_key: b"jpg"}


async def test_store_images_cleans_up_when_one_fails(monkeypatch):
    storage = FakeStorage()
    ids = iter([uuid.UUID(int=1), uuid.UUID(int=2)])
    monkeypatch.setattr(images.uuid, "uuid4", lambda: next(ids))
    storage.fail_on = f"images/{uuid.UUID(int=2)}"
    with pytest.raises(OSError):
        await images.store_images(storage, [("image/png", PNG), ("image/png", PNG)])
    assert storage.objects == {}  # 먼저 올라간 이미지도 지운다


async def test_delete_images_ignores_missing_storage_and_errors():
    await images.delete_images(None, ["images/x"])

    class Broken(FakeStorage):
        async def delete(self, key):
            raise OSError("down")

    await images.delete_images(Broken(), ["images/x"])  # 예외를 밖으로 내지 않는다


# --- 참조 블록과 복원 ---------------------------------------------------------------------------


def test_ref_block_has_no_image_data():
    image_id = uuid.uuid4()
    block = ref(image_id)
    assert block == {
        "type": "image",
        "mime_type": "image/png",
        "extras": {"image_id": str(image_id)},
    }


async def test_resolve_replaces_refs_with_base64_without_touching_state():
    storage = FakeStorage()
    image_id = uuid.uuid4()
    storage.objects[images.storage_key(image_id)] = PNG
    original = HumanMessage(content=[ref(image_id), {"type": "text", "text": "이게 뭐야?"}])
    answer = AIMessage("고양이")

    resolved = await images.resolve_images(storage, [original, answer])

    assert resolved[0].content[0] == {
        "type": "image",
        "base64": base64.b64encode(PNG).decode(),
        "mime_type": "image/png",
    }
    assert resolved[0].content[1] == {"type": "text", "text": "이게 뭐야?"}
    assert resolved[1] is answer
    assert "extras" in original.content[0]  # checkpoint 상태의 메시지는 그대로


async def test_resolve_missing_image_becomes_note():
    resolved = await images.resolve_images(
        FakeStorage(), [HumanMessage(content=[ref(uuid.uuid4())])]
    )
    assert resolved[0].content == [{"type": "text", "text": images.MISSING_IMAGE_TEXT}]
    resolved = await images.resolve_images(None, [HumanMessage(content=[ref(uuid.uuid4())])])
    assert resolved[0].content[0]["type"] == "text"


async def test_resolve_leaves_inline_images_and_plain_messages():
    inline = HumanMessage(content=[{"type": "image", "base64": "QUJD", "mime_type": "image/png"}])
    plain = HumanMessage("안녕")
    messages = [inline, plain]
    assert await images.resolve_images(FakeStorage(), messages) is messages


# --- 그래프: checkpoint 에는 참조만, 모델에는 원본 -----------------------------------------------


async def test_checkpoint_keeps_reference_and_model_gets_original(monkeypatch):
    from app.llm.fake import EchoChatModel

    storage = FakeStorage()
    image_id = uuid.uuid4()
    storage.objects[images.storage_key(image_id)] = PNG
    monkeypatch.setattr(chat_graph, "get_storage", lambda: storage)

    seen: list = []
    original = EchoChatModel._astream

    def spy(self, messages, *args, **kwargs):
        seen.append(messages)
        return original(self, messages, *args, **kwargs)

    monkeypatch.setattr(EchoChatModel, "_astream", spy)
    graph = chat.build_graph(Settings(llm_provider="fake"), InMemorySaver())
    monkeypatch.setitem(runtime._graphs, "chat", graph)

    for text in ["이게 뭐야?", "다시 봐 줘"]:
        chunks = [
            c
            async for c in runtime.stream_reply(
                "img1", text, [ref(image_id)] if text == "이게 뭐야?" else None
            )
        ]
    assert "turn 2" in "".join(c.text for c in chunks if isinstance(c, TextDelta))

    # 두 번째 턴에도 모델은 첫 턴의 이미지 원본을 받는다
    first_turn_blocks = seen[-1][1].content
    assert first_turn_blocks[0]["base64"] == base64.b64encode(PNG).decode()

    # checkpoint 에는 원본 없이 참조만 남는다
    state = await graph.aget_state({"configurable": {"thread_id": "img1"}})
    stored = state.values["messages"][0].content[0]
    assert "base64" not in stored and stored["extras"] == {"image_id": str(image_id)}


# --- 원본 받기 ----------------------------------------------------------------------------------


async def test_get_image_reads_from_storage(monkeypatch):
    from app.routers import chat as chat_router

    storage = FakeStorage()
    row = SimpleNamespace(storage_key="images/abc", mime_type="image/png")
    storage.objects["images/abc"] = PNG

    class Session:
        async def scalar(self, stmt):
            return row

    monkeypatch.setattr(chat_router, "get_storage", lambda: storage)
    res = await chat_router.get_image(uuid.uuid4(), Session(), SimpleNamespace(id=uuid.uuid4()))
    assert res.body == PNG and res.media_type == "image/png"

    storage.objects.clear()  # 원본이 없으면 404
    with pytest.raises(Exception) as exc:
        await chat_router.get_image(uuid.uuid4(), Session(), SimpleNamespace(id=uuid.uuid4()))
    assert exc.value.status_code == 404
