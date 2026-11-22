"""한 턴의 응답 실행: 에이전트 응답을 이벤트로 내보내고, 화면에 보여준 내용을 저장한다.

이벤트는 전송 방식(SSE 등)을 모르는 ``StreamEvent`` 로 내보낸다. API 계층이 SSE 로 바꿔 보낸다.
"""

import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any, NamedTuple

import anyio
from sqlalchemy import select

from app.conversations import runs
from app.core import tracing
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.llm import describe_error, is_transient_error
from app.models import Message, User
from app.orchestration.events import (
    Finished,
    ReasoningDelta,
    StepRecorder,
    TextDelta,
    ToolFinished,
    ToolStarted,
)
from app.orchestration.runtime import stream_reply
from app.projects import service as projects

logger = logging.getLogger(__name__)

# 응답이 끝까지 오지 못했을 때 저장하는 텍스트 뒤에 붙이는 표시 (프론트엔드와 같은 문구).
INTERRUPTED_SUFFIX = "\n\n_(응답이 중단되었습니다.)_"
ERROR_SUFFIX = "\n\n_(오류로 응답이 중단되었습니다.)_"


class StreamEvent(NamedTuple):
    """화면으로 보낼 이벤트 하나.

    Attributes:
        event: 이벤트 이름 (``meta`` / ``delta`` / ``reasoning`` / ``tool_start`` / ``tool_end`` /
            ``done`` / ``error``).
        data: JSON 으로 보낼 데이터.
    """

    event: str
    data: dict[str, Any]


async def save_assistant_message(
    conversation_id: uuid.UUID,
    content: str,
    steps: list[dict[str, Any]] | None,
    model: str | None = None,
    trace_id: str | None = None,
) -> uuid.UUID:
    """Assistant 메시지를 저장한다.

    응답 스트리밍 도중이나 끝난 뒤에 호출되므로 요청 스코프와 별개의 세션을 쓴다.

    Args:
        conversation_id: 대화 ID.
        content: 저장할 답변 텍스트.
        steps: 실행 단계 (추론 과정, 도구 호출). 없으면 ``None``.
        model: 응답한 모델 ID.
        trace_id: 이 응답의 Langfuse 트레이스 ID (피드백 점수를 남길 곳).

    Returns:
        저장한 메시지 ID (화면이 피드백을 보낼 때 쓴다).
    """
    message = Message(
        conversation_id=conversation_id,
        role="assistant",
        content=content,
        steps=steps,
        model=model,
        trace_id=trace_id,
    )
    async with SessionLocal() as s:
        s.add(message)
        await s.commit()
    return message.id


async def run_turn(
    conversation_id: uuid.UUID,
    text: str,
    images: list[dict[str, Any]],
    model: str | None = None,
    project_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> AsyncIterator[StreamEvent]:
    """에이전트 응답을 이벤트로 내보내고, 화면에 보여준 텍스트를 저장한다.

    끝까지 응답하면 전체 텍스트와 실행 단계를 저장한다. 중간에 끝나면 그때까지 보여준
    텍스트에 표시를 붙이고, 실행 중이던 도구는 중단/오류로 바꿔 저장해서, 새로고침해도
    화면에 보였던 내용이 남게 한다.

    - 오류: ``error`` 이벤트를 보내고 ``ERROR_SUFFIX`` 를 붙인다. 이벤트에는 예외 원문 대신
      일반 문구와 오류 ID 를 넣고, 원문은 같은 오류 ID 로 로그에 남긴다. 서버 혼잡처럼
      잠시 후 다시 보내면 풀릴 수 있는 오류면 ``transient`` 를 ``True`` 로 보낸다.
    - 사용자가 중지했거나 연결이 끊김: 요청이 취소되므로 이벤트는 보낼 수 없고
      ``INTERRUPTED_SUFFIX`` 를 붙인다. 취소된 요청 안에서도 저장이 끝나도록 저장을 보호한다.

    응답이 진행되는 동안 생성 중 표시를 갱신하고, ``done`` / ``error`` 이벤트를 보내기
    **직전에** 해제한다. 이벤트를 받은 화면이 대기 메시지를 바로 보내도 거절되지 않게 하기 위해서다.
    저장과 해제는 연결이 끊겨도 끝까지 실행되도록 보호하고, 정리 단계에서 한 번 더 해제한다.

    Args:
        conversation_id: 대화 ID. 호출 전에 ``runs.run_registry`` 에서 생성 중으로 표시되어 있어야 한다.
        text: 모델에게 보낼 사용자 메시지 텍스트 (첨부 목록 포함).
        images: 이미지 참조 블록 (``app.images.image_ref_block``).
        model: 응답할 모델 ID. ``None`` 이면 설정의 기본 모델.
        project_id: 대화가 속한 프로젝트 ID. 있으면 프로젝트 정보(지시문, 문서 목록)를 에이전트에게 준다.
        user_id: 대화의 주인. 있으면 에이전트가 그 사용자의 장기 기억을 쓴다 (사용자가 기억을 껐으면 쓰지 않는다).

    Yields:
        ``meta`` → ``delta`` / ``reasoning`` / ``tool_start`` / ``tool_end``... → ``done`` 또는
        ``error`` 이벤트.
    """
    run_id = str(conversation_id)
    trace_id = tracing.new_trace_id()  # 이 응답의 트레이스 (피드백 점수를 여기에 남긴다)
    shown: list[str] = []
    recorder = StepRecorder()
    outcome = "interrupted"  # 정상 종료나 오류로 바뀌지 않은 채 빠져나가면 중단으로 본다
    try:
        yield StreamEvent("meta", {"conversation_id": run_id})
        context = await _agent_context(project_id, user_id)
        async for event in stream_reply(
            run_id, text, images, model=model, context=context, trace_id=trace_id
        ):
            recorder.apply(event)
            if not isinstance(event, Finished):
                await runs.run_registry.touch(run_id)
            if isinstance(event, TextDelta):
                shown.append(event.text)
                yield StreamEvent("delta", {"text": event.text})
            elif isinstance(event, ReasoningDelta):
                yield StreamEvent("reasoning", {"text": event.text})
            elif isinstance(event, ToolStarted):
                yield StreamEvent(
                    "tool_start",
                    {
                        "id": event.id,
                        "name": event.name,
                        "label": event.label.running,
                        "done_label": event.label.done,
                        "args": event.args,
                    },
                )
            elif isinstance(event, ToolFinished):
                yield StreamEvent(
                    "tool_end",
                    {
                        "id": event.id,
                        "output": event.output,
                        "status": event.status,
                        "sources": event.sources,
                    },
                )
            elif isinstance(event, Finished):
                # 저장과 해제는 도중에 연결이 끊겨도 끝까지 실행되게 보호한다.
                # (보호하지 않으면 저장 직후 취소되어 해제가 빠지고, 대화가 "생성 중" 으로 남는다)
                with anyio.CancelScope(shield=True):
                    outcome = "finished"
                    recorder.finalize("finished")
                    message_id = None
                    if event.text or recorder.steps:
                        message_id = await save_assistant_message(
                            conversation_id, event.text, recorder.to_json(), event.model, trace_id
                        )
                    await runs.run_registry.release(run_id)
                yield StreamEvent(
                    "done",
                    {
                        "stop_reason": event.stop_reason,
                        "model": event.model,
                        # 화면이 이 응답에 피드백을 보낼 때 쓴다
                        "message_id": str(message_id) if message_id else None,
                    },
                )
    except Exception as e:  # provider 마다 예외 타입이 달라 한 곳에서 처리
        # 예외 원문은 로그에만 남기고, 화면에는 로그에서 찾을 수 있는 오류 ID 를 보낸다.
        error_id = uuid.uuid4().hex[:8]
        logger.exception("Agent run failed (error_id=%s)", error_id)
        with anyio.CancelScope(shield=True):
            outcome = "error"
            recorder.finalize("error")
            await _save_partial(conversation_id, shown, recorder, ERROR_SUFFIX, model, trace_id)
            await runs.run_registry.release(run_id)
        message = describe_error(e) or "응답을 만드는 중 오류가 발생했습니다."
        yield StreamEvent(
            "error",
            {
                "message": f"{message} (오류 ID: {error_id})",
                "error_id": error_id,
                # 잠시 후 다시 보내면 풀릴 수 있는 오류 (화면은 알림으로 짧게 보여준다)
                "transient": is_transient_error(e),
            },
        )
    finally:
        # 연결이 끊기면 이 요청은 취소된 상태라, 보호하지 않으면 정리 중 await 도 취소된다.
        with anyio.CancelScope(shield=True):
            if outcome == "interrupted":
                recorder.finalize("interrupted")
                await _save_partial(
                    conversation_id, shown, recorder, INTERRUPTED_SUFFIX, model, trace_id
                )
            # 어떤 경로로 끝나든 생성 중 표시는 반드시 해제한다 (이미 해제됐으면 아무 일 없음).
            await runs.run_registry.release(run_id)


def start_reply(
    conversation_id: uuid.UUID,
    text: str,
    images: list[dict[str, Any]],
    model: str | None = None,
    project_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> runs.RunStream:
    """응답 실행(``run_turn``)을 백그라운드 작업으로 시작한다. 화면 연결이 끊겨도 끝까지 돌고 저장된다.

    Args:
        conversation_id: 대화 ID. 호출 전에 ``runs.run_registry`` 에서 생성 중으로 표시되어 있어야 한다.
        text: 모델에게 보낼 사용자 메시지 텍스트 (첨부 목록 포함).
        images: 이미지 참조 블록.
        model: 응답할 모델 ID.
        project_id: 대화가 속한 프로젝트 ID.
        user_id: 대화의 주인 (장기 기억).

    Returns:
        실행 기록. 화면은 ``follow`` / ``follow_events`` 로 구독한다.
    """
    max_seconds = get_settings().run_max_seconds
    timeout = StreamEvent(
        "error",
        {
            "message": (
                f"응답 시간이 너무 길어 멈췄습니다 (최대 {max_seconds / 60:.0f}분). "
                "그때까지의 응답은 저장했습니다."
            ),
            "transient": False,
        },
    )
    return runs.start_run(
        str(conversation_id),
        run_turn(conversation_id, text, images, model, project_id=project_id, user_id=user_id),
        max_seconds=max_seconds,
        timeout_event=timeout,
    )


async def stop_reply(conversation_id: uuid.UUID) -> bool:
    """진행 중인 응답을 멈춘다. 그때까지의 응답은 중단 표시와 함께 저장된다.

    Args:
        conversation_id: 대화 ID.

    Returns:
        멈춘 응답이 있었으면 ``True``.
    """
    return await runs.cancel_run(str(conversation_id), StreamEvent("stopped", {}))


async def _memory_enabled(user_id: uuid.UUID) -> bool:
    """사용자가 장기 기억을 켜 두었는지."""
    async with SessionLocal() as s:
        return bool(await s.scalar(select(User.memory_enabled).where(User.id == user_id)))


async def _agent_context(
    project_id: uuid.UUID | None, user_id: uuid.UUID | None = None
) -> dict[str, Any]:
    """에이전트에게 넘길 턴 정보.

    사용자를 알면 사용자 ID 와 장기 기억 사용 여부(``user_id``, ``memory_enabled``)를, 프로젝트 대화면 이번 턴
    시작 시점의 프로젝트 정보(``project``)를 담는다.
    """
    context: dict[str, Any] = {}
    if user_id is not None:
        context["user_id"] = str(user_id)
        context["memory_enabled"] = await _memory_enabled(user_id)
    if project_id is None:
        return context
    settings = get_settings()
    # 모델마다 본문을 넣는 기준이 달라서(Ollama 는 작다) 큰 기준까지 본문을 읽어 두고 에이전트가 고른다.
    content_max = max(settings.project_inline_max_tokens, settings.ollama_project_inline_max_tokens)
    async with SessionLocal() as s:
        project = await projects.load_context(s, project_id, content_max)
    if project is not None:
        context["project"] = project
    return context


async def _save_partial(
    conversation_id: uuid.UUID,
    shown: list[str],
    recorder: StepRecorder,
    suffix: str,
    model: str | None,
    trace_id: str | None = None,
) -> None:
    """끝까지 오지 못한 응답을 표시와 함께 저장한다. 보여준 것이 없으면 저장하지 않는다."""
    if not shown and not recorder.steps:
        return
    content = "".join(shown) + suffix if shown else suffix.strip()
    try:
        await save_assistant_message(conversation_id, content, recorder.to_json(), model, trace_id)
    except Exception:
        logger.exception("failed to save partial reply for %s", conversation_id)
