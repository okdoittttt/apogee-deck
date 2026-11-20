"""화면에서 모델을 고르는 기능 검증: 모델 목록, 모델별 그래프, 대화 도중 모델 전환."""

import uuid

import pytest
from fastapi import HTTPException
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.agents import runtime
from app.agents.context import MODEL_TAG, drop_foreign_reasoning, tag_model
from app.agents.events import Finished
from app.config import Settings
from app.llm import catalog
from app.llm.catalog import (
    ModelOption,
    UnknownModelError,
    available_models,
    default_model_id,
    model_id,
    parse_model_id,
    settings_for,
)
from app.routers import chat as chat_router
from app.schemas import ChatRequest

# --- 모델 ID ------------------------------------------------------------------------------------


def test_parse_model_id_keeps_colons_in_model_name():
    assert parse_model_id("ollama:qwen3.5:9b") == ("ollama", "qwen3.5:9b")
    assert parse_model_id("google:gemma-4-31b-it") == ("google", "gemma-4-31b-it")


@pytest.mark.parametrize("bad", ["", "gemma", "google:", "openai:gpt"])
def test_parse_model_id_rejects_bad_ids(bad):
    with pytest.raises(UnknownModelError):
        parse_model_id(bad)


def test_settings_for_switches_provider_and_model():
    base = Settings(llm_provider="ollama", google_api_key="k")
    s = settings_for(base, "google:gemma-4-31b-it")
    assert s.llm_provider == "google" and s.google_model == "gemma-4-31b-it"
    assert s.google_api_key == "k"  # 나머지 설정은 그대로
    assert model_id(s) == "google:gemma-4-31b-it"
    assert model_id(settings_for(base, "ollama:qwen3.5:9b")) == "ollama:qwen3.5:9b"


# --- 모델 목록 ----------------------------------------------------------------------------------


@pytest.fixture
def ollama(monkeypatch):
    def install(names):
        async def fake(settings):
            return names

        monkeypatch.setattr(catalog, "_ollama_models", fake)

    return install


async def test_lists_only_usable_models(ollama):
    ollama([])
    assert await available_models(Settings(llm_provider="ollama")) == []

    ollama(["qwen3.5:9b"])
    options = await available_models(Settings(llm_provider="ollama", google_api_key="k"))
    assert [o.id for o in options] == [
        "google:gemma-4-26b-a4b-it",
        "google:gemma-4-31b-it",
        "ollama:qwen3.5:9b",
    ]
    assert options[0].label == "Gemma 4 26B A4B" and options[0].provider_label == "Google"
    assert options[2].label == "qwen3.5:9b"  # 표시 이름이 없으면 모델 이름


async def test_configured_model_is_always_listed(ollama):
    ollama([])
    s = Settings(google_api_key="k", google_model="gemma-x", google_models="gemma-4-31b-it")
    assert [o.model for o in await available_models(s)] == ["gemma-x", "gemma-4-31b-it"]


async def test_fake_model_listed_only_when_default(ollama):
    ollama([])
    assert [o.id for o in await available_models(Settings(llm_provider="fake"))] == ["fake:fake"]


def test_default_model_prefers_configured_then_first():
    options = [ModelOption("google", "gemma-4-26b-a4b-it"), ModelOption("ollama", "qwen3.5:9b")]
    s = Settings(llm_provider="ollama", ollama_model="qwen3.5:9b")
    assert default_model_id(s, options) == "ollama:qwen3.5:9b"
    # 설정한 모델을 지금 쓸 수 없으면(Ollama 꺼짐 등) 목록의 첫 모델
    assert default_model_id(s, options[:1]) == "google:gemma-4-26b-a4b-it"
    assert default_model_id(s, []) is None


async def test_ollama_unreachable_returns_empty(monkeypatch):
    monkeypatch.setattr(catalog, "_ollama_cache", None)
    s = Settings(ollama_base_url="http://127.0.0.1:9")  # 닫힌 포트
    assert await catalog._ollama_models(s) == []


async def test_models_endpoint(ollama, monkeypatch):
    ollama(["qwen3.5:9b"])
    monkeypatch.setattr(
        chat_router,
        "get_settings",
        lambda: Settings(llm_provider="ollama", ollama_model="qwen3.5:9b", google_api_key="k"),
    )
    res = await chat_router.list_models()
    assert res.default == "ollama:qwen3.5:9b"
    assert [m.id for m in res.models][-1] == "ollama:qwen3.5:9b"


async def test_chat_rejects_unavailable_model(ollama, monkeypatch):
    ollama([])
    monkeypatch.setattr(chat_router, "get_settings", lambda: Settings(llm_provider="ollama"))
    with pytest.raises(HTTPException) as exc:
        await chat_router.chat(ChatRequest(message="hi", model="ollama:qwen3.5:9b"), None)
    assert exc.value.status_code == 400


# --- 모델별 그래프 ------------------------------------------------------------------------------


async def test_stream_reply_uses_selected_model(monkeypatch):
    monkeypatch.setattr(runtime, "_checkpointer", InMemorySaver())
    monkeypatch.setattr(runtime, "_graphs", {})
    monkeypatch.setattr(runtime, "get_settings", lambda: Settings(llm_provider="anthropic"))

    events = [e async for e in runtime.stream_reply("m1", "안녕", model="fake:fake")]

    finished = events[-1]
    assert isinstance(finished, Finished) and finished.model == "fake:fake"
    assert "chat@fake:fake" in runtime._graphs  # 기본 모델이 아니면 따로 만들어 캐시한다
    graph, used = runtime.get_graph("chat", "fake:fake")
    assert used == "fake:fake" and graph is runtime._graphs["chat@fake:fake"]
    # 응답에 모델이 기록된다 (대화 도중 모델을 바꿀 때 추론을 걸러내는 기준)
    state = await graph.aget_state({"configurable": {"thread_id": "m1"}})
    assert state.values["messages"][-1].response_metadata[MODEL_TAG] == "fake:fake"


# --- 대화 도중 모델 전환 ------------------------------------------------------------------------


def test_drop_foreign_reasoning():
    claude = tag_model(
        AIMessage(
            content=[
                {"type": "thinking", "thinking": "생각", "signature": "sig"},
                {"type": "text", "text": "답변"},
            ],
            tool_calls=[{"name": "t", "args": {}, "id": "c1"}],
        ),
        "anthropic:claude-opus-5-5",
    )
    qwen = AIMessage(
        "답변2", id=str(uuid.uuid4()), additional_kwargs={"reasoning_content": "생각2"}
    )  # 모델을 기록하기 전의 메시지
    history = [HumanMessage("q"), claude, qwen]

    # 같은 모델로 이어가면 그대로
    same = drop_foreign_reasoning(history, "anthropic:claude-opus-5-5")
    assert same[1] is claude

    # 다른 모델로 바꾸면 추론만 빠지고 답변과 도구 호출은 남는다
    out = drop_foreign_reasoning(history, "google:gemma-4-26b-a4b-it")
    assert out[1].content == [{"type": "text", "text": "답변"}]
    assert out[1].tool_calls == claude.tool_calls
    assert out[2].content == "답변2" and "reasoning_content" not in out[2].additional_kwargs
    assert out[2].id == qwen.id  # 요약 경계를 ID 로 찾으므로 ID 는 유지
    # 원본은 바뀌지 않는다
    assert claude.content[0]["type"] == "thinking"
    assert qwen.additional_kwargs == {"reasoning_content": "생각2"}


def test_drop_foreign_reasoning_keeps_messages_without_reasoning():
    plain = AIMessage("그냥 답변")
    assert drop_foreign_reasoning([plain], "google:x")[0] is plain
