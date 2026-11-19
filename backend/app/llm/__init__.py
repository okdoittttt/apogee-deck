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

        return EchoChatModel()

    raise ValueError(f"Unknown LLM_PROVIDER: {settings.llm_provider}")


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
