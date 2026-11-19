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
        ``ChatAnthropic`` / ``ChatOllama`` / 테스트용 ``EchoChatModel`` 중 하나.

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


def build_summary_model(settings: Settings) -> BaseChatModel:
    """대화 요약에 쓸 chat model 을 만든다.

    같은 provider 와 모델을 쓰되, Ollama 는 추론(thinking)을 끄고 출력 길이를 ``SUMMARY_MAX_TOKENS`` 로 정한다.
    추론 토큰이 시간과 출력 예산을 쓰지 않게 하기 위해서다.

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
    return build_chat_model(settings)


def model_name(settings: Settings) -> str:
    """화면과 응답 메타데이터에 표시할 모델 이름.

    Args:
        settings: 앱 설정.

    Returns:
        Claude/Ollama 는 모델 이름, 그 외(fake)는 provider 이름.
    """
    return {
        "anthropic": settings.claude_model,
        "ollama": settings.ollama_model,
    }.get(settings.llm_provider, settings.llm_provider)


def describe_error(error: BaseException) -> str | None:
    """사용자가 직접 조치할 수 있는 LLM 오류를 화면용 문구로 바꾼다.

    예외 원문에는 내부 주소, 모델 이름, provider 응답이 들어 있을 수 있으므로 화면에 그대로
    보내지 않는다. 여기서 알아보지 못한 오류는 ``None`` 을 돌려주고, 호출하는 쪽이 일반 문구를 쓴다.

    Args:
        error: LLM 호출 중 발생한 예외.

    Returns:
        화면에 보여줄 문구. 알아볼 수 없는 오류면 ``None``.
    """
    import httpx

    timeouts: tuple[type[BaseException], ...] = (TimeoutError, httpx.TimeoutException)
    connections: tuple[type[BaseException], ...] = (ConnectionError, httpx.ConnectError)
    auth: tuple[type[BaseException], ...] = ()
    try:
        import anthropic

        # APITimeoutError 는 APIConnectionError 의 하위 클래스라 타임아웃을 먼저 확인한다.
        timeouts += (anthropic.APITimeoutError,)
        connections += (anthropic.APIConnectionError,)
        auth += (anthropic.AuthenticationError,)
    except ImportError:
        pass

    if isinstance(error, timeouts):
        return "LLM 응답 시간이 초과되었습니다. 잠시 후 다시 시도해 주세요."
    if isinstance(error, connections):
        return "LLM 서버에 연결할 수 없습니다. 모델 서버가 실행 중인지 확인해 주세요."
    if isinstance(error, auth):
        return "LLM API 키가 올바르지 않습니다. 설정을 확인해 주세요."
    return None
