"""LLM provider 선택.

모든 에이전트가 공용으로 쓴다. 에이전트는 provider 를 모르고 ``build_chat_model`` 이 돌려준
LangChain chat model 만 사용한다.
"""

from typing import NamedTuple

from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import BaseMessage
from langchain_core.runnables import Runnable

from app.core.config import Settings


def build_chat_model(settings: Settings) -> BaseChatModel:
    """설정의 ``llm_provider`` 에 맞는 LangChain chat model 을 만든다.

    Args:
        settings: 앱 설정.

    Returns:
        ``ChatAnthropic`` / ``ChatGoogleGenerativeAI`` / ``ChatOpenAI`` / ``ChatOllama`` /
        테스트용 ``EchoChatModel`` 중 하나.

    Raises:
        ValueError: 알 수 없는 provider 일 때.
    """
    if settings.llm_provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=settings.claude_model,
            api_key=settings.anthropic_api_key or None,
            max_tokens=settings.claude_max_tokens,
            effort=settings.claude_effort,
            # 추론 요약을 받아 화면의 "생각 과정" 에 보여준다 (원문은 제공되지 않음).
            thinking={"type": "adaptive", "display": "summarized"},
            # 대화가 길어질수록 이전 턴을 캐시에서 읽어 비용/지연을 줄인다.
            model_kwargs={"cache_control": {"type": "ephemeral"}},
            streaming=True,
        )

    if settings.llm_provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=settings.google_model,
            # 비어 있으면 None 으로 넘겨 SDK 가 GOOGLE_API_KEY 환경 변수를 찾게 한다.
            google_api_key=settings.google_api_key or None,
            max_output_tokens=settings.google_max_output_tokens,
            temperature=settings.google_temperature,
            # 추론 요약을 받아 화면의 "생각 과정" 에 보여준다 (``thinking`` 블록으로 온다).
            include_thoughts=settings.google_include_thoughts,
            timeout=settings.google_timeout_seconds,
            max_retries=settings.google_max_retries,
            streaming=True,
        )

    if settings.llm_provider == "openai":
        from langchain_openai import ChatOpenAI

        reasoning: dict[str, str] | None = None
        if settings.openai_reasoning_effort is not None:
            reasoning = {"effort": settings.openai_reasoning_effort}
            if settings.openai_reasoning_summary:
                # 추론 요약을 받아 화면의 "생각 과정" 에 보여준다 (원문은 제공되지 않음).
                reasoning["summary"] = "auto"
        return ChatOpenAI(
            model=settings.openai_model,
            api_key=settings.openai_api_key or None,
            # 추론 요약과 추론 이어가기는 Responses API 에서만 된다.
            use_responses_api=True,
            reasoning=reasoning,
            max_tokens=settings.openai_max_output_tokens,
            timeout=settings.openai_timeout_seconds,
            max_retries=settings.openai_max_retries,
            streaming=True,
        )

    if settings.llm_provider == "ollama":
        from langchain_ollama import ChatOllama

        return ChatOllama(
            model=settings.ollama_model,
            base_url=settings.ollama_base_url,
            reasoning=settings.ollama_reasoning,
            num_ctx=settings.ollama_num_ctx,
            num_predict=settings.ollama_num_predict,
            temperature=settings.ollama_temperature,
            keep_alive=settings.ollama_keep_alive,
            # 내부 httpx 클라이언트에 그대로 전달된다 (기본값은 타임아웃 없음).
            client_kwargs={"timeout": settings.ollama_timeout_seconds},
        )

    if settings.llm_provider == "fake":
        from app.llm.fake import EchoChatModel

        return EchoChatModel(stream_delay_seconds=settings.fake_stream_delay_seconds)

    raise ValueError(f"Unknown LLM_PROVIDER: {settings.llm_provider}")


SUMMARY_MAX_TOKENS = 1024
"""대화 요약의 최대 출력 토큰. 요약 프롬프트는 약 250 단어로 요청한다 (한국어는 단어당 토큰이 많다)."""

GOOGLE_SUMMARY_MAX_TOKENS = 4096
"""Google 대화 요약의 최대 출력 토큰. 추론을 끌 수 없는 모델은 추론도 이 한도를 쓰므로 여유를 둔다."""


def build_summary_model(settings: Settings) -> BaseChatModel:
    """대화 요약에 쓸 chat model 을 만든다.

    같은 provider 와 모델을 쓰되, Ollama 는 추론(thinking)을 끄고 출력 길이를 ``SUMMARY_MAX_TOKENS`` 로 정한다.
    추론 토큰이 시간과 출력 예산을 쓰지 않게 하기 위해서다. Google 은 추론 요약을 받지 않고
    출력 길이를 ``GOOGLE_SUMMARY_MAX_TOKENS`` 로 정한다. OpenAI 는 추론 요약을 받지 않고
    추론 강도를 낮춘다(추론을 쓰는 모델만).

    Args:
        settings: 앱 설정.

    Returns:
        도구를 바인딩하지 않은 chat model.
    """
    if settings.llm_provider == "ollama":
        settings = settings.model_copy(
            update={
                "ollama_reasoning": False,
                "ollama_num_predict": SUMMARY_MAX_TOKENS,
            }
        )
    elif settings.llm_provider == "google":
        settings = settings.model_copy(
            update={
                "google_include_thoughts": False,
                "google_max_output_tokens": GOOGLE_SUMMARY_MAX_TOKENS,
            }
        )
    elif settings.llm_provider == "openai":
        update: dict[str, object] = {"openai_reasoning_summary": False}
        if settings.openai_reasoning_effort is not None:
            update["openai_reasoning_effort"] = "low"
        settings = settings.model_copy(update=update)
    return build_chat_model(settings)


PROMPT_CACHE = {"type": "ephemeral"}
"""Anthropic 프롬프트 캐시 설정 (기본 5분 유지)."""


def with_prompt_cache(
    model: Runnable[LanguageModelInput, BaseMessage], settings: Settings
) -> Runnable[LanguageModelInput, BaseMessage]:
    """Anthropic 모델이면 요청마다 프롬프트 캐시를 켠다. 다른 provider 는 그대로 돌려준다.

    요청의 최상위 ``cache_control`` 을 쓰면 Anthropic 이 마지막 블록까지(도구 정의, system 프롬프트,
    대화 이력) 캐시하고, 다음 요청은 같은 앞부분을 캐시에서 읽는다. 프로젝트 문서 본문처럼 매 응답
    다시 보내는 큰 system 프롬프트와, 한 턴 안에서 도구를 여러 번 부를 때의 입력 비용을 줄인다.
    캐시에 쓰는 요청은 입력 단가가 1.25배, 읽는 요청은 0.1배다. 너무 짧은 프롬프트는 캐시되지 않는다.
    OpenAI 와 Gemini 는 같은 앞부분을 자동으로 캐시하므로 따로 켜지 않는다.

    Args:
        model: chat model (도구를 붙인 것도 된다).
        settings: 앱 설정 (provider 확인용).

    Returns:
        모델 호출에 캐시 설정을 붙인 Runnable, 또는 원래 모델.
    """
    if settings.llm_provider != "anthropic":
        return model
    return model.bind(cache_control=PROMPT_CACHE)


def model_name(settings: Settings) -> str:
    """화면과 응답 메타데이터에 표시할 모델 이름.

    Args:
        settings: 앱 설정.

    Returns:
        Claude/Google/Ollama 는 모델 이름, 그 외(fake)는 provider 이름.
    """
    return {
        "anthropic": settings.claude_model,
        "google": settings.google_model,
        "openai": settings.openai_model,
        "ollama": settings.ollama_model,
    }.get(settings.llm_provider, settings.llm_provider)


class _ErrorTypes(NamedTuple):
    """provider 별 예외를 성격에 따라 묶은 것."""

    timeouts: tuple[type[BaseException], ...]
    connections: tuple[type[BaseException], ...]
    auth: tuple[type[BaseException], ...]
    rate_limits: tuple[type[BaseException], ...]
    server_errors: tuple[type[BaseException], ...]


def _error_types() -> _ErrorTypes:
    """설치된 provider SDK 의 예외 클래스를 성격별로 모은다."""
    import httpx
    from langchain_core.exceptions import (
        ModelAPIError,
        ModelAuthenticationError,
        ModelConnectionError,
        ModelPermissionDeniedError,
        ModelRateLimitError,
        ModelTimeoutError,
    )

    # langchain-core 공통 예외(Google 등 provider 패키지가 HTTP 상태별로 던짐)와
    # 공통 예외를 쓰지 않는 SDK(anthropic, httpx)의 예외를 함께 본다.
    timeouts: tuple[type[BaseException], ...] = (
        TimeoutError,
        httpx.TimeoutException,
        ModelTimeoutError,
    )
    connections: tuple[type[BaseException], ...] = (
        ConnectionError,
        httpx.ConnectError,
        ModelConnectionError,
    )
    auth: tuple[type[BaseException], ...] = (ModelAuthenticationError, ModelPermissionDeniedError)
    rate_limits: tuple[type[BaseException], ...] = (ModelRateLimitError,)
    # 5xx: provider 서버가 혼잡하거나 일시적으로 쓸 수 없음 (Google 503 UNAVAILABLE 등).
    server_errors: tuple[type[BaseException], ...] = (ModelAPIError,)
    try:
        import anthropic

        # APITimeoutError 는 APIConnectionError 의 하위 클래스라 타임아웃을 먼저 확인한다.
        timeouts += (anthropic.APITimeoutError,)
        connections += (anthropic.APIConnectionError,)
        auth += (anthropic.AuthenticationError,)
        rate_limits += (anthropic.RateLimitError,)
        server_errors += (anthropic.InternalServerError, anthropic.OverloadedError)
    except ImportError:
        pass
    try:
        import openai

        # langchain-openai 는 보통 langchain-core 공통 예외로 바꿔서 던지지만, 원래 예외도 알아본다.
        timeouts += (openai.APITimeoutError,)
        connections += (openai.APIConnectionError,)
        auth += (openai.AuthenticationError, openai.PermissionDeniedError)
        rate_limits += (openai.RateLimitError,)
        server_errors += (openai.InternalServerError,)
    except ImportError:
        pass
    return _ErrorTypes(timeouts, connections, auth, rate_limits, server_errors)


def describe_error(error: BaseException) -> str | None:
    """사용자가 직접 조치할 수 있는 LLM/에이전트 오류를 화면용 문구로 바꾼다.

    예외 원문에는 내부 주소, 모델 이름, provider 응답이 들어 있을 수 있으므로 화면에 그대로
    보내지 않는다. 여기서 알아보지 못한 오류는 ``None`` 을 돌려주고, 호출하는 쪽이 일반 문구를 쓴다.

    Args:
        error: LLM 호출이나 에이전트 실행 중 발생한 예외.

    Returns:
        화면에 보여줄 문구. 알아볼 수 없는 오류면 ``None``.
    """
    from langchain_core.exceptions import (
        ContextOverflowError,
        ModelInvalidRequestError,
        ModelNotFoundError,
    )
    from langgraph.errors import GraphRecursionError

    types = _error_types()
    if _is_openai_out_of_credit(error):
        return "OpenAI 크레딧이 부족합니다. OpenAI 결제 화면에서 잔액을 확인해 주세요."
    if isinstance(error, GraphRecursionError):
        return "도구를 너무 여러 번 호출해서 응답을 중단했습니다. 질문을 나눠서 다시 시도해 주세요."
    if isinstance(error, types.rate_limits):
        return "LLM API 사용 한도를 넘었습니다. 잠시 후 다시 시도해 주세요."
    if isinstance(error, types.server_errors):
        return "LLM 서버가 일시적으로 혼잡합니다. 잠시 후 다시 시도해 주세요."
    if isinstance(error, ContextOverflowError):
        return (
            "대화가 모델이 한 번에 읽을 수 있는 길이를 넘었습니다. 새 대화에서 다시 시도해 주세요."
        )
    if isinstance(error, types.timeouts):
        return "LLM 응답 시간이 초과되었습니다. 잠시 후 다시 시도해 주세요."
    if isinstance(error, types.connections):
        return "LLM 서버에 연결할 수 없습니다. 모델 서버가 실행 중인지 확인해 주세요."
    # Gemini API 는 잘못된 키를 401 이 아니라 400(API_KEY_INVALID) 으로 돌려준다.
    if isinstance(error, types.auth) or (
        isinstance(error, ModelInvalidRequestError) and "API_KEY_INVALID" in str(error)
    ):
        return "LLM API 키가 올바르지 않습니다. 설정을 확인해 주세요."
    if isinstance(error, ModelNotFoundError):
        return "설정한 LLM 모델을 찾을 수 없습니다. 모델 이름을 확인해 주세요."
    return None


def is_transient_error(error: BaseException) -> bool:
    """잠시 기다렸다 다시 보내면 풀릴 수 있는 오류인지.

    provider 서버 혼잡(5xx), 사용 한도 초과(429), 응답 시간 초과가 해당한다. 화면은 이런 오류를
    오류 상자 대신 잠깐 떠 있는 알림으로 보여준다. OpenAI 크레딧 부족은 429 로 오지만
    기다려도 풀리지 않으므로 제외한다.

    Args:
        error: LLM 호출이나 에이전트 실행 중 발생한 예외.

    Returns:
        일시적인 오류면 ``True``.
    """
    if _is_openai_out_of_credit(error):
        return False
    types = _error_types()
    return isinstance(error, types.server_errors + types.rate_limits + types.timeouts)


def _is_openai_out_of_credit(error: BaseException) -> bool:
    """OpenAI 가 잔액 부족(429 ``insufficient_quota``)으로 거절했는지.

    OpenAI 는 분당 한도 초과와 잔액 부족을 둘 다 429 로 돌려주므로 오류 코드로 구분한다.
    잔액 부족은 기다려도 풀리지 않아서 다른 안내가 필요하다.
    """
    try:
        import openai
    except ImportError:
        return False
    if not isinstance(error, openai.RateLimitError):
        return False
    body = error.body if isinstance(error.body, dict) else {}
    detail = body.get("error", body)
    return isinstance(detail, dict) and detail.get("code") == "insufficient_quota"
