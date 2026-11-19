"""LLM provider 선택. 모든 에이전트가 공용으로 쓴다 (에이전트는 provider 를 모른다)."""

from langchain_core.language_models import BaseChatModel

from app.config import Settings


def build_chat_model(settings: Settings) -> BaseChatModel:
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
        )

    if settings.llm_provider == "fake":
        from app.llm.fake import EchoChatModel

        return EchoChatModel()

    raise ValueError(f"Unknown LLM_PROVIDER: {settings.llm_provider}")


def model_name(settings: Settings) -> str:
    return {
        "anthropic": settings.claude_model,
        "ollama": settings.ollama_model,
    }.get(settings.llm_provider, settings.llm_provider)
