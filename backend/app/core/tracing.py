"""Langfuse 트레이싱과 사용자 피드백 점수.

``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY`` / ``LANGFUSE_BASE_URL`` 환경 변수를 읽는다.
키가 비어 있으면 트레이싱은 꺼진다.

응답 한 턴은 트레이스 하나다. 트레이스 ID 를 미리 만들어(``new_trace_id``) 콜백에 넘기고 assistant 메시지에도
저장해서, 나중에 그 응답에 단 👍/👎 를 같은 트레이스의 점수로 남긴다(``record_feedback``). 프롬프트 버전이 그 아래
LLM 호출에 연결되어 있으므로, Langfuse 에서 프롬프트 버전별 점수를 비교할 수 있다.
"""

import logging
import uuid

from langchain_core.callbacks import BaseCallbackHandler

from app.core.config import get_settings

logger = logging.getLogger(__name__)

FEEDBACK_SCORE = "user_feedback"
"""사용자 피드백 점수 이름 (Langfuse). 👍 은 1, 👎 는 0 (BOOLEAN)."""


def new_trace_id() -> str | None:
    """응답 한 턴의 트레이스 ID. 트레이싱이 꺼져 있으면 ``None``.

    Returns:
        32자리 소문자 16진수 (Langfuse 트레이스 ID 형식).
    """
    return uuid.uuid4().hex if get_settings().tracing_enabled else None


def get_callbacks(trace_id: str | None = None) -> list[BaseCallbackHandler]:
    """그래프 실행 config 에 넣을 콜백 핸들러를 만든다.

    Args:
        trace_id: 이 실행의 트레이스 ID (``new_trace_id``). 없으면 Langfuse 가 만든다.

    Returns:
        트레이싱이 켜져 있으면 Langfuse ``CallbackHandler`` 하나, 꺼져 있으면 빈 리스트.
    """
    if not get_settings().tracing_enabled:
        return []
    from langfuse.langchain import CallbackHandler

    if trace_id:
        return [CallbackHandler(trace_context={"trace_id": trace_id})]
    return [CallbackHandler()]


def feedback_score_id(message_id: uuid.UUID) -> str:
    """메시지 피드백 점수의 ID. 메시지마다 하나라서 피드백을 바꾸면 같은 점수를 고친다."""
    return f"feedback-{message_id}"


def record_feedback(
    trace_id: str, message_id: uuid.UUID, value: int | None, comment: str | None
) -> None:
    """응답에 단 피드백을 그 트레이스의 점수로 남긴다 (블로킹, 스레드에서 부른다).

    Args:
        trace_id: 응답의 트레이스 ID.
        message_id: assistant 메시지 ID (점수 ID 를 만든다).
        value: 1 (👍), -1 (👎), ``None`` (피드백 취소 → 점수를 지운다).
        comment: 남긴 의견.
    """
    if not get_settings().tracing_enabled:
        return
    from langfuse import get_client

    client = get_client()
    score_id = feedback_score_id(message_id)
    if value is None:
        client.api.scores.delete(score_id)
        return
    client.create_score(
        name=FEEDBACK_SCORE,
        value=1 if value > 0 else 0,
        trace_id=trace_id,
        score_id=score_id,
        data_type="BOOLEAN",
        comment=comment or None,
    )
    client.flush()


def shutdown() -> None:
    """종료 시 아직 전송되지 않은 트레이스를 flush 한다."""
    if not get_settings().tracing_enabled:
        return
    from langfuse import get_client

    get_client().shutdown()
