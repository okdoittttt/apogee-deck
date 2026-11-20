"""한 턴의 응답 실행: 에이전트 응답을 이벤트로 내보내고, 화면에 보여준 내용을 저장한다.

이벤트는 전송 방식(SSE 등)을 모르는 ``StreamEvent`` 로 내보낸다. API 계층이 SSE 로 바꿔 보낸다.
"""

import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any, NamedTuple

import anyio

from app.conversations import runs
from app.core.db import SessionLocal
from app.llm import describe_error
from app.models import Message
from app.orchestration.events import (
    Finished,
    ReasoningDelta,
    StepRecorder,
    TextDelta,
    ToolFinished,
    ToolStarted,
)
from app.orchestration.runtime import stream_reply

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
) -> None:
    """Assistant 메시지를 저장한다.

    응답 스트리밍 도중이나 끝난 뒤에 호출되므로 요청 스코프와 별개의 세션을 쓴다.

    Args:
        conversation_id: 대화 ID.
        content: 저장할 답변 텍스트.
        steps: 실행 단계 (추론 과정, 도구 호출). 없으면 ``None``.
        model: 응답한 모델 ID.
    """
    async with SessionLocal() as s:
        s.add(
            Message(
                conversation_id=conversation_id,
                role="assistant",
                content=content,
                steps=steps,
                model=model,
            )
        )
        await s.commit()


async def run_turn(
    conversation_id: uuid.UUID,
    text: str,
    images: list[dict[str, Any]],
    model: str | None = None,
) -> AsyncIterator[StreamEvent]:
    """에이전트 응답을 이벤트로 내보내고, 화면에 보여준 텍스트를 저장한다.

    끝까지 응답하면 전체 텍스트와 실행 단계를 저장한다. 중간에 끝나면 그때까지 보여준
    텍스트에 표시를 붙이고, 실행 중이던 도구는 중단/오류로 바꿔 저장해서, 새로고침해도
    화면에 보였던 내용이 남게 한다.

    - 오류: ``error`` 이벤트를 보내고 ``ERROR_SUFFIX`` 를 붙인다. 이벤트에는 예외 원문 대신
      일반 문구와 오류 ID 를 넣고, 원문은 같은 오류 ID 로 로그에 남긴다.
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

    Yields:
        ``meta`` → ``delta`` / ``reasoning`` / ``tool_start`` / ``tool_end``... → ``done`` 또는
        ``error`` 이벤트.
    """
    run_id = str(conversation_id)
    shown: list[str] = []
    recorder = StepRecorder()
    outcome = "interrupted"  # 정상 종료나 오류로 바뀌지 않은 채 빠져나가면 중단으로 본다
    try:
        yield StreamEvent("meta", {"conversation_id": run_id})
        async for event in stream_reply(run_id, text, images, model=model):
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
                    if event.text or recorder.steps:
                        await save_assistant_message(
                            conversation_id, event.text, recorder.to_json(), event.model
                        )
                    await runs.run_registry.release(run_id)
                yield StreamEvent("done", {"stop_reason": event.stop_reason, "model": event.model})
    except Exception as e:  # provider 마다 예외 타입이 달라 한 곳에서 처리
        # 예외 원문은 로그에만 남기고, 화면에는 로그에서 찾을 수 있는 오류 ID 를 보낸다.
        error_id = uuid.uuid4().hex[:8]
        logger.exception("Agent run failed (error_id=%s)", error_id)
        with anyio.CancelScope(shield=True):
            outcome = "error"
            recorder.finalize("error")
            await _save_partial(conversation_id, shown, recorder, ERROR_SUFFIX, model)
            await runs.run_registry.release(run_id)
        message = describe_error(e) or "응답을 만드는 중 오류가 발생했습니다."
        yield StreamEvent(
            "error", {"message": f"{message} (오류 ID: {error_id})", "error_id": error_id}
        )
    finally:
        # 연결이 끊기면 이 요청은 취소된 상태라, 보호하지 않으면 정리 중 await 도 취소된다.
        with anyio.CancelScope(shield=True):
            if outcome == "interrupted":
                recorder.finalize("interrupted")
                await _save_partial(conversation_id, shown, recorder, INTERRUPTED_SUFFIX, model)
            # 어떤 경로로 끝나든 생성 중 표시는 반드시 해제한다 (이미 해제됐으면 아무 일 없음).
            await runs.run_registry.release(run_id)


async def _save_partial(
    conversation_id: uuid.UUID,
    shown: list[str],
    recorder: StepRecorder,
    suffix: str,
    model: str | None,
) -> None:
    """끝까지 오지 못한 응답을 표시와 함께 저장한다. 보여준 것이 없으면 저장하지 않는다."""
    if not shown and not recorder.steps:
        return
    content = "".join(shown) + suffix if shown else suffix.strip()
    try:
        await save_assistant_message(conversation_id, content, recorder.to_json(), model)
    except Exception:
        logger.exception("failed to save partial reply for %s", conversation_id)
