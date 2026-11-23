"""OpenAI provider 연동 검증 (네트워크 없이 모델 설정, 응답 형식 처리, 오류 문구를 확인)."""

import httpx
import openai
import pytest
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_openai import ChatOpenAI

from app.core.config import OPENAI_HISTORY_TOKENS, Settings
from app.llm import build_chat_model, build_summary_model, describe_error, model_name
from app.llm.catalog import available_models
from app.orchestration.events import reasoning_text
from app.orchestration.runtime import _stop_reason

SETTINGS = Settings(llm_provider="openai", openai_api_key="sk-test")


def test_build_chat_model_uses_responses_api_with_reasoning_summary():
    model = build_chat_model(SETTINGS)
    assert isinstance(model, ChatOpenAI)
    assert model.model_name == "gpt-6-luna"
    assert model.use_responses_api is True
    assert model.reasoning == {"effort": "low", "summary": "auto"}
    assert model.max_tokens == SETTINGS.openai_max_output_tokens
    assert model_name(SETTINGS) == "gpt-6-luna"
    assert SETTINGS.history_token_budget == OPENAI_HISTORY_TOKENS


def test_reasoning_settings_can_be_turned_off():
    no_summary = build_chat_model(SETTINGS.model_copy(update={"openai_reasoning_summary": False}))
    assert no_summary.reasoning == {"effort": "low"}
    # 추론을 지원하지 않는 모델이면 추론 설정을 아예 보내지 않는다
    plain = build_chat_model(SETTINGS.model_copy(update={"openai_reasoning_effort": None}))
    assert plain.reasoning is None


def test_summary_model_skips_reasoning_summary():
    model = build_summary_model(SETTINGS.model_copy(update={"openai_reasoning_effort": "high"}))
    assert model.reasoning == {"effort": "low"}


async def test_openai_models_are_listed_first(monkeypatch):
    from app.llm import catalog

    async def no_ollama(settings):
        return []

    monkeypatch.setattr(catalog, "_ollama_models", no_ollama)
    ids = [
        o.id for o in await available_models(SETTINGS.model_copy(update={"google_api_key": "k"}))
    ]
    assert ids[:3] == ["openai:gpt-6-luna", "openai:gpt-5.6-luna", "openai:gpt-6.1-terra"]
    assert ids[3].startswith("google:")


# --- 응답 형식 -----------------------------------------------------------------------------------


def test_reasoning_summary_chunks_become_reasoning_text():
    """langchain-openai 가 Responses API 스트림을 바꾼 조각 형식 그대로 확인한다."""

    def chunk(index, text):
        summary = [{"index": index, "type": "summary_text", "text": text}]
        return AIMessageChunk(content=[{"type": "reasoning", "summary": summary, "index": 0}])

    pieces = [
        chunk(0, ""),
        chunk(0, "날씨를 "),
        chunk(0, "검색한다."),
        chunk(1, ""),
        chunk(1, "답한다."),
    ]
    assert "".join(reasoning_text(c) for c in pieces) == "날씨를 검색한다.\n\n답한다."
    assert all(c.text == "" for c in pieces)  # 추론은 답변 텍스트에 섞이지 않는다


@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        ({"status": "completed"}, "end_turn"),
        (
            {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
            "max_tokens",
        ),
        (
            {"status": "incomplete", "incomplete_details": {"reason": "content_filter"}},
            "content_filter",
        ),
        ({"finish_reason": "length"}, "max_tokens"),  # Chat Completions API
    ],
)
def test_stop_reason(meta, expected):
    assert _stop_reason(AIMessage("", response_metadata=meta)) == expected


# --- 오류 문구 ----------------------------------------------------------------------------------


def _rate_limit(code: str) -> openai.RateLimitError:
    body = {"error": {"message": "msg", "type": code, "code": code}}
    response = httpx.Response(
        429, request=httpx.Request("POST", "https://api.openai.com/v1/responses")
    )
    return openai.RateLimitError("msg", response=response, body=body)


def test_out_of_credit_is_distinguished_from_rate_limit():
    assert "크레딧" in describe_error(_rate_limit("insufficient_quota"))
    assert "사용 한도" in describe_error(_rate_limit("rate_limit_exceeded"))
