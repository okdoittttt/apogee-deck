"""채팅 API: SSE 스트리밍 채팅, 대화 조회/삭제, 이미지, 앱 정보."""

import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import anyio
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.agents import runtime
from app.agents.events import (
    Finished,
    ReasoningDelta,
    StepRecorder,
    TextDelta,
    ToolFinished,
    ToolStarted,
)
from app.agents.runtime import stream_reply
from app.config import get_settings
from app.db import SessionDep, SessionLocal
from app.llm import model_name
from app.models import Conversation, Message, MessageImage
from app.runs import InMemoryRunRegistry, RunRegistry
from app.schemas import (
    ChatRequest,
    ConversationDetail,
    ConversationOut,
    ConversationStatus,
    MessageOut,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

# 대화당 응답 생성을 하나로 제한한다 (다른 창에서 같은 대화에 보낸 요청은 거절).
run_registry: RunRegistry = InMemoryRunRegistry(get_settings().run_stale_seconds)
BUSY_DETAIL = "이전 응답이 아직 생성 중입니다. 완료된 후 다시 시도해 주세요."


def sse(event: str, data: dict[str, Any]) -> str:
    """Server-Sent Events 메시지 하나를 만든다.

    Args:
        event: 이벤트 이름 (``meta`` / ``delta`` / ``done`` / ``error``).
        data: JSON 으로 보낼 데이터.

    Returns:
        ``event:`` / ``data:`` 줄과 빈 줄로 끝나는 SSE 문자열.
    """
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/info")
async def info() -> dict[str, Any]:
    """현재 provider, 모델, 이미지 입력 가능 여부를 알려준다 (프론트엔드 표시용).

    Returns:
        ``provider`` / ``model`` / ``vision`` 키를 가진 딕셔너리.
    """
    settings = get_settings()
    return {
        "provider": settings.llm_provider,
        "model": model_name(settings),
        "vision": settings.enable_vision,
    }


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(session: SessionDep) -> list[Conversation]:
    """최근 대화 목록을 최신순으로 돌려준다 (최대 100개).

    Args:
        session: DB 세션.

    Returns:
        대화 목록.
    """
    result = await session.scalars(
        select(Conversation).order_by(Conversation.created_at.desc()).limit(100)
    )
    return list(result.all())


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(conversation_id: uuid.UUID, session: SessionDep) -> ConversationDetail:
    """메시지를 포함한 대화 하나를 돌려준다.

    이미지 바이너리는 포함하지 않고 ID 만 내려준다. 이미지는 ``GET /api/images/{id}`` 로 받는다.

    Args:
        conversation_id: 대화 ID.
        session: DB 세션.

    Returns:
        대화와 메시지 목록, 그리고 응답 생성 중인지 여부.

    Raises:
        HTTPException: 대화가 없으면 404.
    """
    conv = await session.get(
        Conversation,
        conversation_id,
        options=[
            selectinload(Conversation.messages)
            .selectinload(Message.images)
            .load_only(MessageImage.id, MessageImage.position)
        ],
    )
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return ConversationDetail(
        id=conv.id,
        title=conv.title,
        created_at=conv.created_at,
        messages=[
            MessageOut(
                id=m.id,
                role=m.role,
                content=m.content,
                created_at=m.created_at,
                image_ids=[img.id for img in m.images],
                steps=m.steps or [],
            )
            for m in conv.messages
        ],
        running=await run_registry.is_running(str(conv.id)),
    )


@router.get("/conversations/{conversation_id}/status", response_model=ConversationStatus)
async def get_conversation_status(
    conversation_id: uuid.UUID, session: SessionDep
) -> ConversationStatus:
    """대화의 응답 생성 여부를 돌려준다.

    다른 창에서 생성 중인 응답이 끝났는지 화면이 주기적으로 확인할 때 쓴다.
    메시지를 불러오지 않아서 가볍다.

    Args:
        conversation_id: 대화 ID.
        session: DB 세션.

    Returns:
        실행 상태.

    Raises:
        HTTPException: 대화가 없으면 404.
    """
    if await session.get(Conversation, conversation_id) is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return ConversationStatus(running=await run_registry.is_running(str(conversation_id)))


@router.get("/images/{image_id}")
async def get_image(image_id: uuid.UUID, session: SessionDep) -> Response:
    """첨부 이미지 원본을 돌려준다.

    이미지는 저장 후 바뀌지 않으므로 브라우저가 오래 캐시하도록 한다.

    Args:
        image_id: 이미지 ID.
        session: DB 세션.

    Returns:
        이미지 바이너리 응답.

    Raises:
        HTTPException: 이미지가 없으면 404.
    """
    img = await session.get(MessageImage, image_id)
    if img is None:
        raise HTTPException(status_code=404, detail="Image not found")
    return Response(
        content=img.data,
        media_type=img.mime_type,
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: uuid.UUID, session: SessionDep) -> None:
    """대화와 그 에이전트 상태(checkpoint)를 삭제한다.

    checkpoint 삭제가 실패해도 대화 삭제는 유지되고, 남은 checkpoint 는 다음 기동 시 정리된다.

    Args:
        conversation_id: 대화 ID.
        session: DB 세션.

    Raises:
        HTTPException: 대화가 없으면 404, 응답 생성 중이면 409.
    """
    conv = await session.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if await run_registry.is_running(str(conversation_id)):
        raise HTTPException(status_code=409, detail="응답 생성 중인 대화는 삭제할 수 없습니다.")
    await session.delete(conv)
    await session.commit()
    try:
        await runtime.delete_thread(str(conversation_id))
    except Exception:
        logger.exception("failed to delete checkpoint thread %s", conversation_id)


@router.post("/chat")
async def chat(req: ChatRequest, session: SessionDep) -> StreamingResponse:
    """사용자 메시지를 저장하고 에이전트 응답을 SSE 로 스트리밍한다.

    SSE 이벤트는 ``meta`` (대화 ID) → 응답 진행 이벤트 (여러 번) → ``done`` (종료 사유, 모델)
    순서이고, 실패하면 ``error`` 를 보낸다. 응답 진행 이벤트는 ``delta`` (답변 텍스트),
    ``reasoning`` (추론 과정), ``tool_start`` / ``tool_end`` (도구 호출)이다. 화면에 보여준
    텍스트와 실행 단계는 assistant 메시지로 저장한다 (``stream_and_save`` 참고). LLM 에 들어가는 대화 이력은
    checkpointer 가 관리하고, ``messages`` 테이블은 화면 표시용이다.

    같은 대화의 응답이 이미 생성 중이면(다른 창에서 보낸 경우) 메시지를 저장하지 않고
    409 로 거절한다. 같은 창 안의 추가 질문은 프론트엔드가 대기열로 처리한다.

    Args:
        req: 채팅 요청.
        session: DB 세션.

    Returns:
        ``text/event-stream`` 스트리밍 응답.

    Raises:
        HTTPException: 이미지 입력이 꺼져 있는데 이미지를 보내면 400, 대화가 없으면 404,
            같은 대화의 응답이 생성 중이면 409.
    """
    if req.images and not get_settings().enable_vision:
        raise HTTPException(status_code=400, detail="현재 모델은 이미지 입력을 지원하지 않습니다.")

    # 1) 대화 조회/생성 → 생성 중 표시 (이미 생성 중이면 저장 전에 거절)
    if req.conversation_id:
        conv = await session.get(Conversation, req.conversation_id)
        if conv is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
    else:
        conv = Conversation(title=req.message.strip()[:50] or "이미지")
        session.add(conv)
        await session.flush()
    if not await run_registry.try_acquire(str(conv.id)):
        raise HTTPException(status_code=409, detail=BUSY_DETAIL)

    # 2) 사용자 메시지(+이미지) 저장. 실패하면 생성 중 표시를 되돌린다.
    try:
        session.add(
            Message(
                conversation_id=conv.id,
                role="user",
                content=req.message,
                images=[
                    MessageImage(position=i, mime_type=img.mime_type, data=img.to_bytes())
                    for i, img in enumerate(req.images)
                ],
            )
        )
        await session.commit()
    except BaseException:
        await run_registry.release(str(conv.id))
        raise
    images = [(img.mime_type, img.data) for img in req.images]

    # 3) 에이전트 응답 스트리밍 → 끝나면(또는 중단되면) assistant 메시지 저장, 생성 중 표시 해제
    return StreamingResponse(
        stream_and_save(conv.id, req.message, images),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# 응답이 끝까지 오지 못했을 때 저장하는 텍스트 뒤에 붙이는 표시 (프론트엔드와 같은 문구).
INTERRUPTED_SUFFIX = "\n\n_(응답이 중단되었습니다.)_"
ERROR_SUFFIX = "\n\n_(오류로 응답이 중단되었습니다.)_"


async def save_assistant_message(
    conversation_id: uuid.UUID, content: str, steps: list[dict[str, Any]] | None
) -> None:
    """Assistant 메시지를 저장한다.

    응답 스트리밍 도중이나 끝난 뒤에 호출되므로 요청 스코프와 별개의 세션을 쓴다.

    Args:
        conversation_id: 대화 ID.
        content: 저장할 답변 텍스트.
        steps: 실행 단계 (추론 과정, 도구 호출). 없으면 ``None``.
    """
    async with SessionLocal() as s:
        s.add(
            Message(conversation_id=conversation_id, role="assistant", content=content, steps=steps)
        )
        await s.commit()


async def stream_and_save(
    conversation_id: uuid.UUID, text: str, images: list[tuple[str, str]]
) -> AsyncIterator[str]:
    """에이전트 응답을 SSE 이벤트로 스트리밍하고, 화면에 보여준 텍스트를 저장한다.

    끝까지 응답하면 전체 텍스트와 실행 단계를 저장한다. 중간에 끝나면 그때까지 보여준
    텍스트에 표시를 붙이고, 실행 중이던 도구는 중단/오류로 바꿔 저장해서, 새로고침해도
    화면에 보였던 내용이 남게 한다.

    - 오류: ``error`` 이벤트를 보내고 ``ERROR_SUFFIX`` 를 붙인다.
    - 사용자가 중지했거나 연결이 끊김: 요청이 취소되므로 이벤트는 보낼 수 없고
      ``INTERRUPTED_SUFFIX`` 를 붙인다. 취소된 요청 안에서도 저장이 끝나도록 저장을 보호한다.

    응답이 진행되는 동안 생성 중 표시를 갱신하고, ``done`` / ``error`` 이벤트를 보내기
    **직전에** 해제한다. 이벤트를 받은 화면이 대기 메시지를 바로 보내도 거절되지 않게 하기 위해서다.
    저장과 해제는 연결이 끊겨도 끝까지 실행되도록 보호하고, 정리 단계에서 한 번 더 해제한다.

    Args:
        conversation_id: 대화 ID. 호출 전에 ``run_registry`` 에서 생성 중으로 표시되어 있어야 한다.
        text: 사용자 메시지 텍스트.
        images: ``(mime_type, base64)`` 목록.

    Yields:
        SSE 이벤트 문자열 (``meta`` → ``delta`` / ``reasoning`` / ``tool_start`` /
        ``tool_end``... → ``done`` 또는 ``error``).
    """
    run_id = str(conversation_id)
    shown: list[str] = []
    recorder = StepRecorder()
    outcome = "interrupted"  # 정상 종료나 오류로 바뀌지 않은 채 빠져나가면 중단으로 본다
    try:
        yield sse("meta", {"conversation_id": run_id})
        async for event in stream_reply(run_id, text, images):
            recorder.apply(event)
            if not isinstance(event, Finished):
                await run_registry.touch(run_id)
            if isinstance(event, TextDelta):
                shown.append(event.text)
                yield sse("delta", {"text": event.text})
            elif isinstance(event, ReasoningDelta):
                yield sse("reasoning", {"text": event.text})
            elif isinstance(event, ToolStarted):
                yield sse(
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
                yield sse(
                    "tool_end", {"id": event.id, "output": event.output, "status": event.status}
                )
            elif isinstance(event, Finished):
                # 저장과 해제는 도중에 연결이 끊겨도 끝까지 실행되게 보호한다.
                # (보호하지 않으면 저장 직후 취소되어 해제가 빠지고, 대화가 "생성 중" 으로 남는다)
                with anyio.CancelScope(shield=True):
                    outcome = "finished"
                    recorder.finalize("finished")
                    if event.text or recorder.steps:
                        await save_assistant_message(
                            conversation_id, event.text, recorder.to_json()
                        )
                    await run_registry.release(run_id)
                yield sse("done", {"stop_reason": event.stop_reason, "model": event.model})
    except Exception as e:  # provider 마다 예외 타입이 달라 한 곳에서 처리
        logger.exception("Agent run failed")
        with anyio.CancelScope(shield=True):
            outcome = "error"
            recorder.finalize("error")
            await _save_partial(conversation_id, shown, recorder, ERROR_SUFFIX)
            await run_registry.release(run_id)
        yield sse("error", {"message": f"LLM 호출 실패: {type(e).__name__}: {e}"[:500]})
    finally:
        # 연결이 끊기면 이 요청은 취소된 상태라, 보호하지 않으면 정리 중 await 도 취소된다.
        with anyio.CancelScope(shield=True):
            if outcome == "interrupted":
                recorder.finalize("interrupted")
                await _save_partial(conversation_id, shown, recorder, INTERRUPTED_SUFFIX)
            # 어떤 경로로 끝나든 생성 중 표시는 반드시 해제한다 (이미 해제됐으면 아무 일 없음).
            await run_registry.release(run_id)


async def _save_partial(
    conversation_id: uuid.UUID, shown: list[str], recorder: StepRecorder, suffix: str
) -> None:
    """끝까지 오지 못한 응답을 표시와 함께 저장한다. 보여준 것이 없으면 저장하지 않는다."""
    if not shown and not recorder.steps:
        return
    content = "".join(shown) + suffix if shown else suffix.strip()
    try:
        await save_assistant_message(conversation_id, content, recorder.to_json())
    except Exception:
        logger.exception("failed to save partial reply for %s", conversation_id)
