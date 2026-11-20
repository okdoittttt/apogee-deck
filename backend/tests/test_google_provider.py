"""Google(Gemini API) provider 연동 검증.

네트워크 없이 확인하기 위해 ``ChatGoogleGenerativeAI`` 의 비동기 클라이언트를 가짜로 바꾸고,
Gemini API 가 실제로 돌려주는 형식(``GenerateContentResponse``)의 응답을 흘려보낸다.
모델 객체, 메시지 변환, 그래프, 이벤트 처리는 실제 코드를 그대로 쓴다.
"""

import base64

import pytest
from google.genai import errors as genai_errors
from google.genai import types
from langchain_core.messages import AIMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.checkpoint.memory import InMemorySaver

from app.agents import chat
from app.core.config import GOOGLE_HISTORY_TOKENS, Settings
from app.llm import (
    GOOGLE_SUMMARY_MAX_TOKENS,
    build_chat_model,
    build_summary_model,
    describe_error,
    is_transient_error,
    model_name,
)
from app.orchestration import runtime
from app.orchestration.events import Finished, ReasoningDelta, TextDelta, ToolFinished, ToolStarted
from app.orchestration.runtime import _stop_reason

SETTINGS = Settings(llm_provider="google", google_api_key="test-key", enable_fetch_url=False)


def _response(*parts: types.Part, finish: bool = False) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=list(parts)),
                finish_reason=types.FinishReason.STOP if finish else None,
            )
        ],
        model_version="gemma-4-26b-a4b-it",
    )


class FakeModels:
    """``client.aio.models`` 대신 쓰는 가짜. 호출마다 준비한 응답 스트림을 하나씩 돌려준다."""

    def __init__(self, turns: list[list[types.GenerateContentResponse]]):
        self.turns = list(turns)
        self.requests: list[dict] = []

    async def generate_content_stream(self, **request):
        self.requests.append(request)
        chunks = self.turns.pop(0)

        async def stream():
            for chunk in chunks:
                yield chunk

        return stream()


@pytest.fixture
def fake_google(monkeypatch):
    def install(turns):
        models = FakeModels(turns)
        fake = type("FakeAio", (), {"models": models})()
        monkeypatch.setattr(ChatGoogleGenerativeAI, "async_client", property(lambda self: fake))
        return models

    return install


# --- 모델 생성과 설정 ---------------------------------------------------------------------------


def test_build_chat_model_uses_google_settings():
    model = build_chat_model(SETTINGS)
    assert isinstance(model, ChatGoogleGenerativeAI)
    assert model.model == "gemma-4-26b-a4b-it"
    assert model.include_thoughts is True
    assert model.max_output_tokens == SETTINGS.google_max_output_tokens
    assert model.max_retries == SETTINGS.google_max_retries
    assert model_name(SETTINGS) == "gemma-4-26b-a4b-it"


def test_summary_model_skips_thoughts():
    model = build_summary_model(SETTINGS)
    assert model.include_thoughts is False
    assert model.max_output_tokens == GOOGLE_SUMMARY_MAX_TOKENS


def test_google_has_history_budget_so_summary_is_enabled():
    assert SETTINGS.history_token_budget == GOOGLE_HISTORY_TOKENS
    assert Settings(llm_provider="google", context_max_tokens=5000).history_token_budget == 5000


@pytest.mark.parametrize(
    ("raw", "expected"), [("STOP", "end_turn"), ("MAX_TOKENS", "max_tokens"), ("SAFETY", "SAFETY")]
)
def test_finish_reason_is_normalized(raw, expected):
    assert _stop_reason(AIMessage("", response_metadata={"finish_reason": raw})) == expected


# --- 그래프 실행: 추론 → 도구 호출 → 답변 --------------------------------------------------------


async def test_reasoning_tool_call_and_answer(monkeypatch, fake_google):
    models = fake_google(
        [
            [
                _response(types.Part(text="시간을 확인해야 한다.", thought=True)),
                _response(
                    types.Part(
                        function_call=types.FunctionCall(
                            name="get_current_time", args={"timezone": "Asia/Seoul"}
                        )
                    ),
                    finish=True,
                ),
            ],
            [
                _response(types.Part(text="결과를 정리한다.", thought=True)),
                _response(types.Part(text="지금은 ")),
                _response(types.Part(text="오전 10시입니다."), finish=True),
            ],
        ]
    )
    graph = chat.build_graph(SETTINGS, InMemorySaver())
    monkeypatch.setitem(runtime._graphs, "chat", graph)

    events = [e async for e in runtime.stream_reply("g1", "지금 몇 시야?")]

    reasoning = "".join(e.text for e in events if isinstance(e, ReasoningDelta))
    assert "시간을 확인해야 한다." in reasoning and "결과를 정리한다." in reasoning
    started = next(e for e in events if isinstance(e, ToolStarted))
    finished = next(e for e in events if isinstance(e, ToolFinished))
    assert started.name == "get_current_time" and started.args == {"timezone": "Asia/Seoul"}
    assert finished.status == "done" and "KST" in finished.output

    # 추론 텍스트는 답변에 섞이지 않는다
    answer = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert answer == "지금은 오전 10시입니다."
    last = events[-1]
    assert isinstance(last, Finished)
    assert last.text == answer and last.stop_reason == "end_turn"

    # 요청 형식: system 프롬프트는 system_instruction 으로, 도구는 함수 선언으로 보낸다
    first, second = models.requests
    assert first["config"].system_instruction is not None
    declared = [f.name for t in first["config"].tools for f in t.function_declarations]
    assert declared == ["get_current_time", "read_document"]
    # 두 번째 호출에는 도구 호출과 그 결과가 이어서 들어간다
    parts = [p for c in second["contents"] for p in c.parts]
    assert any(p.function_call and p.function_call.name == "get_current_time" for p in parts)
    assert any(
        p.function_response and p.function_response.name == "get_current_time" for p in parts
    )


async def test_image_input_is_sent_as_inline_data(monkeypatch, fake_google):
    models = fake_google([[_response(types.Part(text="고양이입니다."), finish=True)]])
    graph = chat.build_graph(SETTINGS, InMemorySaver())
    monkeypatch.setitem(runtime._graphs, "chat", graph)

    png = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()
    events = [
        e
        async for e in runtime.stream_reply(
            "g2", "이게 뭐야?", [{"type": "image", "base64": png, "mime_type": "image/png"}]
        )
    ]

    assert events[-1].text == "고양이입니다."
    parts = [p for c in models.requests[0]["contents"] for p in c.parts]
    inline = [p.inline_data for p in parts if p.inline_data]
    assert inline and inline[0].mime_type == "image/png"


# --- 오류 문구 ----------------------------------------------------------------------------------


def _client_error(code: int, status: str, message: str) -> genai_errors.ClientError:
    return genai_errors.ClientError(
        code, {"error": {"code": code, "status": status, "message": message}}
    )


async def test_google_unavailable_is_transient(fake_google):
    """Google 이 503 UNAVAILABLE 을 돌려주면 혼잡 안내를 보내고 일시적 오류로 본다."""

    class FailingModels(FakeModels):
        async def generate_content_stream(self, **request):
            async def stream():
                raise genai_errors.ServerError(
                    503,
                    {
                        "error": {
                            "code": 503,
                            "status": "UNAVAILABLE",
                            "message": "The model is overloaded. Please try again later.",
                        }
                    },
                )
                yield  # pragma: no cover

            return stream()

    fake = type("FakeAio", (), {"models": FailingModels([])})()
    model = build_chat_model(SETTINGS)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ChatGoogleGenerativeAI, "async_client", property(lambda self: fake))
        with pytest.raises(Exception) as exc:
            async for _ in model.astream("hi"):
                pass

    described = describe_error(exc.value)
    assert described is not None and "혼잡" in described
    assert "overloaded" not in described
    assert is_transient_error(exc.value)


@pytest.mark.parametrize(
    ("code", "status", "message", "expected"),
    [
        (429, "RESOURCE_EXHAUSTED", "Quota exceeded", "사용 한도"),
        (400, "INVALID_ARGUMENT", "API key not valid. [reason: API_KEY_INVALID]", "API 키"),
        (403, "PERMISSION_DENIED", "denied", "API 키"),
        (404, "NOT_FOUND", "models/gemma-x is not found", "모델 이름"),
    ],
)
async def test_google_errors_are_described(fake_google, code, status, message, expected):
    class FailingModels(FakeModels):
        async def generate_content_stream(self, **request):
            async def stream():
                raise _client_error(code, status, message)
                yield  # pragma: no cover

            return stream()

    fake = type("FakeAio", (), {"models": FailingModels([])})()
    model = build_chat_model(SETTINGS)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ChatGoogleGenerativeAI, "async_client", property(lambda self: fake))
        with pytest.raises(Exception) as exc:
            async for _ in model.astream("hi"):
                pass

    described = describe_error(exc.value)
    assert described is not None and expected in described
    # 원문(키 사유, 모델 이름 등)은 화면 문구에 들어가지 않는다
    assert message not in described
