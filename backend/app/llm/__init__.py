"""LLM provider 선택.

모든 에이전트가 공용으로 쓴다. 에이전트는 provider 를 모르고 ``build_chat_model`` 이 돌려준
LangChain chat model 만 사용한다.
"""

from langchain_core.language_models import BaseChatModel

from app.config import Settings


def build_chat_model(settings: Settings) -> BaseChatModel:
    """설정의 ``llm_provider`` 에 맞는 LangChain chat model 을 만든다.

    Args:
        settings: 앱 설정.

    Returns:
        ``ChatAnthropic`` / ``ChatGoogleGenerativeAI`` / ``ChatOllama`` / 테스트용 ``EchoChatModel``
        중 하나.

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
    출력 길이를 ``GOOGLE_SUMMARY_MAX_TOKENS`` 로 정한다.

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
    return build_chat_model(settings)


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
        "ollama": settings.ollama_model,
    }.get(settings.llm_provider, settings.llm_provider)


def describe_error(error: BaseException) -> str | None:
    """사용자가 직접 조치할 수 있는 LLM/에이전트 오류를 화면용 문구로 바꾼다.

    예외 원문에는 내부 주소, 모델 이름, provider 응답이 들어 있을 수 있으므로 화면에 그대로
    보내지 않는다. 여기서 알아보지 못한 오류는 ``None`` 을 돌려주고, 호출하는 쪽이 일반 문구를 쓴다.

    Args:
        error: LLM 호출이나 에이전트 실행 중 발생한 예외.

    Returns:
        화면에 보여줄 문구. 알아볼 수 없는 오류면 ``None``.
    """
    import httpx
    from langchain_core.exceptions import (
        ContextOverflowError,
        ModelAuthenticationError,
        ModelConnectionError,
        ModelInvalidRequestError,
        ModelNotFoundError,
        ModelPermissionDeniedError,
        ModelRateLimitError,
        ModelTimeoutError,
    )
    from langgraph.errors import GraphRecursionError

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
    try:
        import anthropic

        # APITimeoutError 는 APIConnectionError 의 하위 클래스라 타임아웃을 먼저 확인한다.
        timeouts += (anthropic.APITimeoutError,)
        connections += (anthropic.APIConnectionError,)
        auth += (anthropic.AuthenticationError,)
        rate_limits += (anthropic.RateLimitError,)
    except ImportError:
        pass

    if isinstance(error, GraphRecursionError):
        return "도구를 너무 여러 번 호출해서 응답을 중단했습니다. 질문을 나눠서 다시 시도해 주세요."
    if isinstance(error, rate_limits):
        return "LLM API 사용 한도를 넘었습니다. 잠시 후 다시 시도해 주세요."
    if isinstance(error, ContextOverflowError):
        return (
            "대화가 모델이 한 번에 읽을 수 있는 길이를 넘었습니다. 새 대화에서 다시 시도해 주세요."
        )
    if isinstance(error, timeouts):
        return "LLM 응답 시간이 초과되었습니다. 잠시 후 다시 시도해 주세요."
    if isinstance(error, connections):
        return "LLM 서버에 연결할 수 없습니다. 모델 서버가 실행 중인지 확인해 주세요."
    # Gemini API 는 잘못된 키를 401 이 아니라 400(API_KEY_INVALID) 으로 돌려준다.
    if isinstance(error, auth) or (
        isinstance(error, ModelInvalidRequestError) and "API_KEY_INVALID" in str(error)
    ):
        return "LLM API 키가 올바르지 않습니다. 설정을 확인해 주세요."
    if isinstance(error, ModelNotFoundError):
        return "설정한 LLM 모델을 찾을 수 없습니다. 모델 이름을 확인해 주세요."
    return None
