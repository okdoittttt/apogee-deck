"""Langfuse 트레이싱.

``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY`` / ``LANGFUSE_BASE_URL`` 환경 변수를 읽는다.
키가 비어 있으면 트레이싱은 꺼진다.
"""

from langchain_core.callbacks import BaseCallbackHandler

from app.core.config import get_settings


def get_callbacks() -> list[BaseCallbackHandler]:
    """그래프 실행 config 에 넣을 콜백 핸들러를 만든다.

    Returns:
        트레이싱이 켜져 있으면 Langfuse ``CallbackHandler`` 하나, 꺼져 있으면 빈 리스트.
    """
    if not get_settings().tracing_enabled:
        return []
    from langfuse.langchain import CallbackHandler

    return [CallbackHandler()]


def shutdown() -> None:
    """종료 시 아직 전송되지 않은 트레이스를 flush 한다."""
    if not get_settings().tracing_enabled:
        return
    from langfuse import get_client

    get_client().shutdown()
