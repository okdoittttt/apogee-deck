"""Langfuse 트레이싱. LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_BASE_URL 환경 변수를 읽는다."""

from langchain_core.callbacks import BaseCallbackHandler

from app.config import get_settings


def get_callbacks() -> list[BaseCallbackHandler]:
    if not get_settings().tracing_enabled:
        return []
    from langfuse.langchain import CallbackHandler

    return [CallbackHandler()]


def shutdown() -> None:
    """종료 시 아직 전송되지 않은 트레이스를 flush."""
    if not get_settings().tracing_enabled:
        return
    from langfuse import get_client

    get_client().shutdown()
