"""채팅 API: SSE 스트리밍 채팅, 대화 조회/삭제, 이미지, 앱 정보.

요청 검증·저장·실행은 대화 서비스(``app.conversations``)가 하고, 여기서는 인증, 응답 형식,
SSE 변환만 한다.
"""

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, StreamingResponse

from app.api.deps import ApprovedUser, SessionDep
from app.api.schemas import (
    ChatRequest,
    ConversationDetail,
    ConversationOut,
    ConversationStatus,
    MessageOut,
    ModelList,
    ModelOut,
    document_out,
)
from app.conversations import service, turns
from app.conversations.service import ConversationError
from app.conversations.turns import StreamEvent
from app.core.config import get_settings
from app.documents.formats import SUPPORTED_EXTENSIONS
from app.documents.service import AttachError
from app.llm import model_name
from app.llm.catalog import available_models, default_model_id
from app.models import Conversation

router = APIRouter(prefix="/api")


def sse(event: str, data: dict[str, Any]) -> str:
    """Server-Sent Events 메시지 하나를 만든다.

    Args:
        event: 이벤트 이름 (``meta`` / ``delta`` / ``done`` / ``error`` 등).
        data: JSON 으로 보낼 데이터.

    Returns:
        ``event:`` / ``data:`` 줄과 빈 줄로 끝나는 SSE 문자열.
    """
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def sse_stream(events: AsyncIterator[StreamEvent]) -> AsyncIterator[str]:
    """이벤트 스트림을 SSE 문자열 스트림으로 바꾼다.

    이 스트림이 닫히면(연결 끊김) 안쪽 스트림도 바로 닫아서, 안쪽의 저장·해제 정리가 실행되게 한다.

    Args:
        events: 대화 서비스가 내보내는 이벤트.

    Yields:
        SSE 메시지 문자열.
    """
    async with aclosing(events):
        async for e in events:
            yield sse(e.event, e.data)


def _http_error(exc: ConversationError | AttachError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.get("/info")
async def info(user: ApprovedUser) -> dict[str, Any]:
    """현재 provider, 모델, 이미지 입력 가능 여부, 문서 첨부 제한을 알려준다 (프론트엔드 표시용).

    Args:
        user: 승인된 사용자.

    Returns:
        ``provider`` / ``model`` / ``vision`` / ``documents`` 키를 가진 딕셔너리. ``documents`` 는
        첨부 가능 여부(저장소 설정), 최대 크기, 메시지당 개수, 받는 확장자다.
    """
    settings = get_settings()
    return {
        "provider": settings.llm_provider,
        "model": model_name(settings),
        "vision": settings.enable_vision,
        "documents": {
            "enabled": settings.storage_enabled,
            "max_bytes": settings.document_max_bytes,
            "max_per_message": settings.max_documents_per_message,
            "extensions": list(SUPPORTED_EXTENSIONS),
        },
    }


@router.get("/models")
async def list_models(user: ApprovedUser) -> ModelList:
    """화면에서 고를 수 있는 모델 목록과 기본 모델을 돌려준다.

    API 키가 있는 provider 의 모델과, 실행 중인 Ollama 에 설치된 모델이 들어간다.

    Args:
        user: 승인된 사용자.

    Returns:
        모델 목록과 기본 모델 ID.
    """
    settings = get_settings()
    options = await available_models(settings)
    return ModelList(
        models=[
            ModelOut(
                id=o.id,
                provider=o.provider,
                provider_label=o.provider_label,
                model=o.model,
                label=o.label,
            )
            for o in options
        ],
        default=default_model_id(settings, options),
    )


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(session: SessionDep, user: ApprovedUser) -> list[Conversation]:
    """내 최근 대화 목록을 최신순으로 돌려준다 (최대 100개).

    Args:
        session: DB 세션.
        user: 승인된 사용자.

    Returns:
        대화 목록.
    """
    return await service.list_recent(session, user.id)


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: uuid.UUID, session: SessionDep, user: ApprovedUser
) -> ConversationDetail:
    """메시지를 포함한 대화 하나를 돌려준다.

    이미지 바이너리는 포함하지 않고 ID 만 내려준다. 이미지는 ``GET /api/images/{id}`` 로 받는다.
    첨부 문서는 본문 없이 정보만 내려준다.

    Args:
        conversation_id: 대화 ID.
        session: DB 세션.
        user: 승인된 사용자.

    Returns:
        대화와 메시지 목록, 그리고 응답 생성 중인지 여부.

    Raises:
        HTTPException: 대화가 없거나 내 것이 아니면 404.
    """
    try:
        view = await service.get_view(session, conversation_id, user.id)
    except ConversationError as exc:
        raise _http_error(exc) from exc
    conv = view.conversation
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
                model=m.model,
                documents=[document_out(d, chars) for d, chars in view.documents.get(m.id, [])],
            )
            for m in conv.messages
        ],
        running=view.running,
    )


@router.get("/conversations/{conversation_id}/status", response_model=ConversationStatus)
async def get_conversation_status(
    conversation_id: uuid.UUID, session: SessionDep, user: ApprovedUser
) -> ConversationStatus:
    """대화의 응답 생성 여부를 돌려준다.

    다른 창에서 생성 중인 응답이 끝났는지 화면이 주기적으로 확인할 때 쓴다.

    Args:
        conversation_id: 대화 ID.
        session: DB 세션.
        user: 승인된 사용자.

    Returns:
        실행 상태.

    Raises:
        HTTPException: 대화가 없거나 내 것이 아니면 404.
    """
    try:
        running = await service.is_running(session, conversation_id, user.id)
    except ConversationError as exc:
        raise _http_error(exc) from exc
    return ConversationStatus(running=running)


@router.get("/images/{image_id}")
async def get_image(image_id: uuid.UUID, session: SessionDep, user: ApprovedUser) -> Response:
    """첨부 이미지 원본을 돌려준다.

    이미지는 저장 후 바뀌지 않으므로 브라우저가 오래 캐시하도록 한다.

    Args:
        image_id: 이미지 ID.
        session: DB 세션.
        user: 승인된 사용자.

    Returns:
        이미지 바이너리 응답.

    Raises:
        HTTPException: 이미지가 없거나 내 대화의 이미지가 아니면 404.
    """
    try:
        data, mime_type = await service.read_image(session, image_id, user.id)
    except ConversationError as exc:
        raise _http_error(exc) from exc
    return Response(
        content=data,
        media_type=mime_type,
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: uuid.UUID, session: SessionDep, user: ApprovedUser
) -> None:
    """대화와 그 에이전트 상태(checkpoint), 첨부 문서와 이미지를 삭제한다.

    Args:
        conversation_id: 대화 ID.
        session: DB 세션.
        user: 승인된 사용자.

    Raises:
        HTTPException: 대화가 없거나 내 것이 아니면 404, 응답 생성 중이면 409.
    """
    try:
        await service.delete(session, conversation_id, user.id)
    except ConversationError as exc:
        raise _http_error(exc) from exc


@router.post("/chat")
async def chat(req: ChatRequest, session: SessionDep, user: ApprovedUser) -> StreamingResponse:
    """사용자 메시지를 저장하고 에이전트 응답을 SSE 로 스트리밍한다.

    SSE 이벤트는 ``meta`` (대화 ID) → 응답 진행 이벤트 (여러 번) → ``done`` (종료 사유, 모델)
    순서이고, 실패하면 ``error`` 를 보낸다. 응답 진행 이벤트는 ``delta`` (답변 텍스트),
    ``reasoning`` (추론 과정), ``tool_start`` / ``tool_end`` (도구 호출)이다. 화면에 보여준
    텍스트와 실행 단계는 assistant 메시지로 저장한다 (``turns.run_turn`` 참고). LLM 에 들어가는 대화 이력은
    checkpointer 가 관리하고, ``messages`` 테이블은 화면 표시용이다.

    같은 대화의 응답이 이미 생성 중이면(다른 창에서 보낸 경우) 메시지를 저장하지 않고
    409 로 거절한다. 같은 창 안의 추가 질문은 프론트엔드가 대기열로 처리한다.

    Args:
        req: 채팅 요청.
        session: DB 세션.
        user: 승인된 사용자. 새 대화의 주인이 된다.

    Returns:
        ``text/event-stream`` 스트리밍 응답.

    Raises:
        HTTPException: 검증·저장에 실패했을 때 (``service.start_turn`` 의 오류를 그대로 옮긴다).
    """
    try:
        turn = await service.start_turn(
            session,
            user.id,
            conversation_id=req.conversation_id,
            message=req.message,
            images=[(img.mime_type, img.to_bytes()) for img in req.images],
            document_ids=req.document_ids,
            model=req.model,
        )
    except (ConversationError, AttachError) as exc:
        raise _http_error(exc) from exc

    # 에이전트 응답 스트리밍 → 끝나면(또는 중단되면) assistant 메시지 저장, 생성 중 표시 해제
    return StreamingResponse(
        sse_stream(turns.run_turn(turn.conversation_id, turn.text, turn.images, turn.model)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
