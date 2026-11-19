"""채팅 API: SSE 스트리밍 채팅, 대화 조회/삭제, 이미지, 앱 정보."""

import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.agents import runtime
from app.agents.runtime import Finished, TextDelta, stream_reply
from app.config import get_settings
from app.db import SessionDep, SessionLocal
from app.llm import model_name
from app.models import Conversation, Message, MessageImage
from app.schemas import ChatRequest, ConversationDetail, ConversationOut, MessageOut

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


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
        대화와 메시지 목록.

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
            )
            for m in conv.messages
        ],
    )


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
        HTTPException: 대화가 없으면 404.
    """
    conv = await session.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    await session.delete(conv)
    await session.commit()
    try:
        await runtime.delete_thread(str(conversation_id))
    except Exception:
        logger.exception("failed to delete checkpoint thread %s", conversation_id)


@router.post("/chat")
async def chat(req: ChatRequest, session: SessionDep) -> StreamingResponse:
    """사용자 메시지를 저장하고 에이전트 응답을 SSE 로 스트리밍한다.

    SSE 이벤트는 ``meta`` (대화 ID) → ``delta`` (텍스트 조각, 여러 번) → ``done``
    (종료 사유, 모델) 순서이고, 실패하면 ``error`` 를 보낸다. 응답이 끝나면 화면에 보여준
    텍스트를 assistant 메시지로 저장한다. LLM 에 들어가는 대화 이력은 checkpointer 가
    관리하고, ``messages`` 테이블은 화면 표시용이다.

    Args:
        req: 채팅 요청.
        session: DB 세션.

    Returns:
        ``text/event-stream`` 스트리밍 응답.

    Raises:
        HTTPException: 이미지 입력이 꺼져 있는데 이미지를 보내면 400, 대화가 없으면 404.
    """
    if req.images and not get_settings().enable_vision:
        raise HTTPException(status_code=400, detail="현재 모델은 이미지 입력을 지원하지 않습니다.")

    # 1) 대화 조회/생성 + 사용자 메시지(+이미지) 저장
    if req.conversation_id:
        conv = await session.get(Conversation, req.conversation_id)
        if conv is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
    else:
        conv = Conversation(title=req.message.strip()[:50] or "이미지")
        session.add(conv)
        await session.flush()

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
    images = [(img.mime_type, img.data) for img in req.images]
    conversation_id = conv.id

    # 2) 에이전트 응답 스트리밍 → 끝나면 assistant 메시지 저장
    async def event_stream() -> AsyncIterator[str]:
        yield sse("meta", {"conversation_id": str(conversation_id)})
        try:
            async for chunk in stream_reply(str(conversation_id), req.message, images):
                if isinstance(chunk, TextDelta):
                    yield sse("delta", {"text": chunk.text})
                elif isinstance(chunk, Finished):
                    if chunk.text:
                        # 응답 스트리밍이 끝난 뒤라 요청 스코프와 별개의 세션을 쓴다.
                        async with SessionLocal() as s:
                            s.add(
                                Message(
                                    conversation_id=conversation_id,
                                    role="assistant",
                                    content=chunk.text,
                                )
                            )
                            await s.commit()
                    yield sse("done", {"stop_reason": chunk.stop_reason, "model": chunk.model})
        except Exception as e:  # provider 마다 예외 타입이 달라 한 곳에서 처리
            logger.exception("Agent run failed")
            yield sse("error", {"message": f"LLM 호출 실패: {type(e).__name__}: {e}"[:500]})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
