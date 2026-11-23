"""장기 기억 검증: 저장·검색 규칙, 사용자 격리, 기억 도구, 그래프의 기억 주입."""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from app.agents import chat
from app.agents.chat.graph import hidden_tools
from app.api.routers import memory as memory_router
from app.api.schemas import MemoryIn, MemorySettingsIn, MemoryUpdate, ProfileIn
from app.conversations import turns
from app.core.config import Settings
from app.llm.embeddings import HashingEmbeddings
from app.llm.fake import EchoChatModel
from app.memory import service
from app.memory import store as memory_store
from app.memory.service import MemoryInputError, MemoryNotFoundError
from app.tools.memory import MEMORY_TOOL_NAMES as MEMORY
from app.tools.memory import NO_USER, make_forget, make_remember

DIMS = 256
SETTINGS = Settings(llm_provider="fake", embedding_provider="fake", embedding_dimensions=DIMS)


def _store() -> InMemoryStore:
    return InMemoryStore(
        index={"dims": DIMS, "embed": HashingEmbeddings(DIMS), "fields": ["content"]}
    )


@pytest.fixture
def store():
    return _store()


# --- 서비스 ---------------------------------------------------------------------------------------


async def test_add_update_delete_memory(store):
    memory = await service.add_memory(store, "u1", "  채식주의자다  ", "preferences")
    assert (memory.content, memory.category, len(memory.id)) == ("채식주의자다", "preferences", 8)

    updated = await service.update_memory(store, "u1", memory.id, content="고기도 먹는다")
    assert (updated.content, updated.category) == ("고기도 먹는다", "preferences")
    assert [m.content for m in await service.list_memories(store, "u1")] == ["고기도 먹는다"]

    await service.delete_memory(store, "u1", memory.id)
    assert await service.list_memories(store, "u1") == []
    with pytest.raises(MemoryNotFoundError):
        await service.delete_memory(store, "u1", memory.id)


async def test_memories_are_separate_per_user(store):
    mine = await service.add_memory(store, "u1", "서울에 산다", "personal")
    assert await service.list_memories(store, "u2") == []
    assert await service.search_memories(store, "u2", "서울", 5) == []
    with pytest.raises(MemoryNotFoundError):  # 다른 사용자의 ID 로는 고치거나 지울 수 없다
        await service.update_memory(store, "u2", mine.id, content="부산에 산다")
    with pytest.raises(MemoryNotFoundError):
        await service.delete_memory(store, "u2", mine.id)
    assert [m.content for m in await service.list_memories(store, "u1")] == ["서울에 산다"]


@pytest.mark.parametrize(
    ("content", "category"),
    [
        ("", "other"),
        ("   ", "other"),
        ("가" * (service.MAX_MEMORY_CHARS + 1), "other"),
        ("ok", "x"),
    ],
)
async def test_invalid_memory_is_rejected(store, content, category):
    with pytest.raises(MemoryInputError):
        await service.add_memory(store, "u1", content, category)


async def test_memory_count_is_limited(store, monkeypatch):
    monkeypatch.setattr(service, "MAX_MEMORIES", 2)
    await service.add_memory(store, "u1", "하나", "other")
    await service.add_memory(store, "u1", "둘", "other")
    with pytest.raises(MemoryInputError, match="already 2"):
        await service.add_memory(store, "u1", "셋", "other")


async def test_profile_set_replace_and_clear(store):
    assert (await service.get_profile(store, "u1")).text == ""
    await service.set_profile(store, "u1", "이름: 오케이\n하는 일: 백엔드 개발자")
    saved = await service.set_profile(store, "u1", "이름: 오케이\n하는 일: 데이터 엔지니어")
    assert saved.text.endswith("데이터 엔지니어") and saved.updated_at is not None
    with pytest.raises(MemoryInputError):
        await service.set_profile(store, "u1", "가" * (service.MAX_PROFILE_CHARS + 1))
    assert (await service.set_profile(store, "u1", "")).text == ""
    assert (await service.get_profile(store, "u1")).text == ""


async def test_search_ranks_related_memories_and_drops_unrelated(store):
    await service.add_memory(store, "u1", "매운 음식을 좋아한다", "preferences")
    await service.add_memory(store, "u1", "파이썬 백엔드 개발자다", "work")
    found = await service.search_memories(store, "u1", "매운 음식 추천", 5)
    assert found[0].content == "매운 음식을 좋아한다"
    assert found[0].score is not None
    strict = await service.search_memories(store, "u1", "매운 음식 추천", 5, min_score=0.99)
    assert strict == []  # 기준보다 덜 관련된 기억은 넣지 않는다


async def test_search_without_embeddings_returns_recent_memories():
    store = InMemoryStore()  # 임베딩 없음 (키가 없을 때)
    await service.add_memory(store, "u1", "첫 번째", "other")
    await service.add_memory(store, "u1", "두 번째", "other")
    found = await service.search_memories(store, "u1", "아무거나", 1, min_score=0.9)
    assert [m.content for m in found] == ["두 번째"]


async def test_delete_all_removes_profile_and_memories(store):
    await service.set_profile(store, "u1", "이름: 오케이")
    await service.add_memory(store, "u1", "하나", "other")
    await service.add_memory(store, "u2", "남의 것", "other")
    assert await service.delete_all(store, "u1") == 1
    assert (await service.get_profile(store, "u1")).text == ""
    assert await service.list_memories(store, "u1") == []
    assert len(await service.list_memories(store, "u2")) == 1


# --- 도구 ----------------------------------------------------------------------------------------


def _tool_call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_call", "id": "call_1", "name": name, "args": args}


async def test_tools_need_a_user_from_the_server(store):
    remember = make_remember(SETTINGS)
    no_user = await remember.ainvoke(
        _tool_call("remember", {"content": "x", "category": "other", "store": store}),
        config={"configurable": {}},
    )
    assert no_user.content == NO_USER
    off = await remember.ainvoke(
        _tool_call("remember", {"content": "x", "category": "other", "store": store}),
        config={"configurable": {"user_id": "u1", "memory_enabled": False}},
    )
    assert off.content == NO_USER
    assert await service.list_memories(store, "u1") == []


async def test_remember_and_forget_tools(store):
    config = {"configurable": {"user_id": "u1"}}
    saved = await make_remember(SETTINGS).ainvoke(
        _tool_call(
            "remember", {"content": "채식주의자다", "category": "preferences", "store": store}
        ),
        config=config,
    )
    [memory] = await service.list_memories(store, "u1")
    assert saved.content == f"Saved memory [{memory.id}] (preferences): 채식주의자다"

    updated = await make_remember(SETTINGS).ainvoke(
        _tool_call(
            "remember",
            {
                "content": "고기도 먹는다",
                "category": "preferences",
                "memory_id": memory.id,
                "store": store,
            },
        ),
        config=config,
    )
    assert updated.content.startswith(f"Updated memory [{memory.id}]")

    missing = await make_forget(SETTINGS).ainvoke(
        _tool_call("forget", {"memory_id": "nope", "store": store}), config=config
    )
    assert "No memory" in missing.content
    await make_forget(SETTINGS).ainvoke(
        _tool_call("forget", {"memory_id": memory.id, "store": store}), config=config
    )
    assert await service.list_memories(store, "u1") == []


# --- 그래프 ---------------------------------------------------------------------------------------


class ModelInputs(AsyncCallbackHandler):
    """모델에 실제로 보낸 메시지를 모은다."""

    def __init__(self) -> None:
        self.calls: list[list] = []

    async def on_chat_model_start(self, serialized, messages, **kwargs) -> None:
        self.calls.append(messages[0])


def _config(user_id: str | None, inputs: ModelInputs | None = None, **extra) -> dict[str, Any]:
    configurable = {"thread_id": f"t-{uuid.uuid4()}", **extra}
    if user_id is not None:
        configurable["user_id"] = user_id
    return {"configurable": configurable, "callbacks": [inputs] if inputs else []}


async def _ask(graph, text: str, config) -> str:
    result = await graph.ainvoke({"messages": [HumanMessage(text)]}, config)
    return result["messages"][-1].text


async def test_memory_saved_in_one_conversation_is_used_in_another(store):
    graph = chat.build_graph(SETTINGS, InMemorySaver(), store)
    await _ask(graph, f"{EchoChatModel.REMEMBER_MARKER} 매운 음식을 좋아한다", _config("u1"))
    [memory] = await service.list_memories(store, "u1")
    assert memory.content == "매운 음식을 좋아한다"

    inputs = ModelInputs()
    config = _config("u1", inputs)
    reply = await _ask(graph, "매운 음식 메뉴 추천해 줘", config)
    assert memory.content in reply  # echo 모델이 본 사용자 메시지 앞에 기억이 붙어 있다
    assert f"[{memory.id}]" in reply

    # 기억은 보낼 때만 붙이고, 대화 기록(checkpoint)의 사용자 메시지는 그대로다.
    state = await graph.aget_state(config)
    assert state.values["messages"][0].text == "매운 음식 메뉴 추천해 줘"
    system = inputs.calls[0][0]
    assert isinstance(system, SystemMessage) and memory.content not in system.text


async def test_profile_is_always_in_the_system_prompt(store):
    await service.set_profile(store, "u1", "이름: 오케이\n하는 일: 백엔드 개발자")
    graph = chat.build_graph(SETTINGS, InMemorySaver(), store)
    inputs = ModelInputs()
    await _ask(graph, "안녕", _config("u1", inputs))
    system = inputs.calls[0][0]
    assert "<user_profile>\n이름: 오케이\n하는 일: 백엔드 개발자\n</user_profile>" in system.text
    assert "<memories>" not in inputs.calls[0][-1].text  # 기억이 없으면 블록도 없다

    other = ModelInputs()
    await _ask(graph, "안녕", _config("u2", other))
    assert "<user_profile>\n(empty)\n</user_profile>" in other.calls[0][0].text  # 다른 사용자


async def test_turned_off_memory_is_not_used_or_saved(store):
    await service.set_profile(store, "u1", "이름: 오케이")
    await service.add_memory(store, "u1", "매운 음식을 좋아한다", "preferences")
    graph = chat.build_graph(SETTINGS, InMemorySaver(), store)
    inputs = ModelInputs()
    config = _config("u1", inputs, memory_enabled=False)
    reply = await _ask(graph, f"{EchoChatModel.REMEMBER_MARKER} 매운 음식", config)
    assert reply.startswith("[echo")  # 기억 도구가 없어서 부르지 않는다
    assert "<memories>" not in reply
    assert "user_profile" not in inputs.calls[0][0].text
    assert len(await service.list_memories(store, "u1")) == 1


@pytest.mark.parametrize(
    ("use_memory", "configurable", "hidden"),
    [
        (True, {"user_id": "u1", "project": object()}, set()),
        (True, {"user_id": "u1"}, {"search_project"}),
        (True, {"user_id": "u1", "memory_enabled": False, "project": object()}, MEMORY),
        (False, {"user_id": "u1"}, MEMORY | {"search_project"}),
        (True, {}, MEMORY | {"search_project"}),  # 사용자를 모르면 기억 도구도 없다
    ],
)
def test_tools_unavailable_this_turn_are_hidden(use_memory, configurable, hidden):
    config = {"configurable": configurable}
    assert hidden_tools(config, use_memory=use_memory) == hidden


async def test_without_a_store_the_graph_has_no_memory():
    graph = chat.build_graph(SETTINGS, InMemorySaver())
    assert "recall" not in graph.nodes
    reply = await _ask(graph, f"{EchoChatModel.REMEMBER_MARKER} 매운 음식", _config("u1"))
    assert reply.startswith("[echo")


async def test_recall_failure_does_not_break_the_reply(store, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("store down")

    monkeypatch.setattr(service, "get_profile", broken)
    graph = chat.build_graph(SETTINGS, InMemorySaver(), store)
    assert (await _ask(graph, "안녕", _config("u1"))).startswith("[echo · turn 1] 안녕")


# --- 기억 관리 API ---------------------------------------------------------------------------------


@pytest.fixture
def api_store(store):
    memory_store.set_store(store)
    yield store
    memory_store.set_store(None)


def _user(enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), status="approved", memory_enabled=enabled)


async def test_api_manages_only_my_memories(api_store):
    me, other = _user(), _user()
    created = await memory_router.add_memory(
        MemoryIn(content="매운 음식을 좋아한다", category="preferences"), me
    )
    await memory_router.set_profile(ProfileIn(text="이름: 오케이"), me)

    overview = await memory_router.overview(me)
    assert overview.available and overview.enabled
    assert overview.profile.text == "이름: 오케이"
    assert [(m.id, m.category) for m in overview.memories] == [(created.id, "preferences")]
    assert (await memory_router.overview(other)).memories == []

    for call in (
        memory_router.update_memory(created.id, MemoryUpdate(content="남의 것"), other),
        memory_router.delete_memory(created.id, other),
    ):
        with pytest.raises(HTTPException) as exc:
            await call
        assert exc.value.status_code == 404

    updated = await memory_router.update_memory(created.id, MemoryUpdate(category="other"), me)
    assert (updated.content, updated.category) == ("매운 음식을 좋아한다", "other")
    await memory_router.delete_memory(created.id, me)
    await memory_router.delete_all(me)
    overview = await memory_router.overview(me)
    assert overview.memories == [] and overview.profile.text == ""


async def test_api_rejects_too_many_memories(api_store, monkeypatch):
    monkeypatch.setattr(service, "MAX_MEMORIES", 1)
    me = _user()
    await memory_router.add_memory(MemoryIn(content="하나"), me)
    with pytest.raises(HTTPException) as exc:
        await memory_router.add_memory(MemoryIn(content="둘"), me)
    assert exc.value.status_code == 422


async def test_api_without_a_store():
    memory_store.set_store(None)
    overview = await memory_router.overview(_user())
    assert not overview.available
    with pytest.raises(HTTPException) as exc:
        await memory_router.add_memory(MemoryIn(content="하나"), _user())
    assert exc.value.status_code == 503


async def test_api_turns_memory_on_and_off():
    executed = []

    class Session:
        async def execute(self, statement):
            executed.append(statement.compile().params)

        async def commit(self):
            executed.append("commit")

    me = _user()
    assert await memory_router.set_enabled(MemorySettingsIn(enabled=False), Session(), me) == {
        "enabled": False
    }
    assert executed[0]["memory_enabled"] is False and executed[1] == "commit"


async def test_turn_passes_the_user_and_memory_setting_to_the_agent(monkeypatch):
    async def memory_enabled(user_id):
        return False

    monkeypatch.setattr(turns, "_memory_enabled", memory_enabled)
    uid = uuid.uuid4()
    assert await turns._agent_context(None, uid) == {"user_id": str(uid), "memory_enabled": False}
    assert await turns._agent_context(None) == {}  # 사용자를 모르면 기억을 쓰지 않는다
