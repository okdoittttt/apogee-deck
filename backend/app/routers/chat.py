import json
import logging
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.agents.runtime import Finished, TextDelta, stream_reply
from app.config import get_settings
from app.db import SessionDep, SessionLocal
from app.llm import model_name
from app.models import Conversation, Message, MessageImage
from app.schemas import ChatRequest, ConversationDetail, ConversationOut, MessageOut

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/info")
async def info():
    """프론트엔드 표시용: 현재 provider / 모델 / 이미지 입력 가능 여부."""
    settings = get_settings()
    return {
        "provider": settings.llm_provider,
        "model": model_name(settings),
        "vision": settings.enable_vision,
    }


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(session: SessionDep):
    result = await session.scalars(
        select(Conversation).order_by(Conversation.created_at.desc()).limit(100)
    )
    return result.all()


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(conversation_id: uuid.UUID, session: SessionDep):
    conv = await session.get(
        Conversation,
        conversation_id,
        # 이미지 바이너리는 제외하고 id 만 내려준다 (이미지는 /api/images/{id} 로 따로 로드)
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
async def get_image(image_id: uuid.UUID, session: SessionDep):
    img = await session.get(MessageImage, image_id)
    if img is None:
        raise HTTPException(status_code=404, detail="Image not found")
    # 이미지는 저장 후 바뀌지 않으므로 브라우저가 오래 캐시해도 된다.
    return Response(
        content=img.data,
        media_type=img.mime_type,
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: uuid.UUID, session: SessionDep):
    conv = await session.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    await session.delete(conv)
    await session.commit()


@router.post("/chat")
async def chat(req: ChatRequest, session: SessionDep):
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

    # 2) LangGraph 에이전트 응답을 SSE로 스트리밍하고, 끝나면 assistant 메시지 저장
    #    (LLM 에 들어가는 대화 이력은 checkpointer 가 관리. messages 테이블은 UI 표시용)
    async def event_stream():
        yield sse("meta", {"conversation_id": str(conversation_id)})
        try:
            async for chunk in stream_reply(str(conversation_id), req.message, images):
                if isinstance(chunk, TextDelta):
                    yield sse("delta", {"text": chunk.text})
                elif isinstance(chunk, Finished):
                    if chunk.text:
                        # 스트리밍 응답이 끝난 뒤이므로 요청 스코프와 별개의 세션을 쓴다.
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
