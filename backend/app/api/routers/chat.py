"""채팅 API: SSE 스트리밍 채팅, 대화 조회/삭제, 이미지, 앱 정보.

요청 검증·저장·실행은 대화 서비스(``app.conversations``)가 하고, 여기서는 인증, 응답 형식,
SSE 변환만 한다.
"""

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException
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
from app.conversations import runs, service, turns
from app.conversations.service import ConversationError
from app.conversations.turns import StreamEvent
from app.core.config import get_settings
from app.documents.formats import SUPPORTED_EXTENSIONS
from app.documents.service import AttachError
from app.llm import model_name
from app.llm.catalog import available_models, default_model_id
from app.models import Conversation

router = APIRouter(prefix="/api")


def sse(event: str, data: dict[str, Any], event_id: int | None = None) -> str:
    """Server-Sent Events 메시지 하나를 만든다.

    Args:
        event: 이벤트 이름 (``meta`` / ``delta`` / ``done`` / ``error`` 등).
        data: JSON 으로 보낼 데이터.
        event_id: 이벤트 번호. 있으면 ``id:`` 줄을 넣는다 (다시 붙을 때 ``Last-Event-ID`` 로 보낸다).

    Returns:
        ``id:`` / ``event:`` / ``data:`` 줄과 빈 줄로 끝나는 SSE 문자열.
    """
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def sse_numbered(events: AsyncIterator[tuple[int, StreamEvent]]) -> AsyncIterator[str]:
    """번호가 붙은 실행 이벤트(``RunStream.follow``)를 ``id:`` 가 있는 SSE 로 바꾼다.

    연결이 끊기면 구독만 닫는다. 실행은 백그라운드에서 계속된다.

    Args:
        events: ``(번호, 이벤트)``.

    Yields:
        SSE 메시지 문자열.
    """
    async with aclosing(events):
        async for event_id, e in events:
            yield sse(e.event, e.data, event_id)


async def sse_stream(events: AsyncIterator[StreamEvent]) -> AsyncIterator[str]:
    """이벤트 스트림을 SSE 문자열 스트림으로 바꾼다.

    이 스트림이 닫히면(연결 끊김) 안쪽 스트림(실행 구독)도 바로 닫는다. 실행 자체는 백그라운드에서 계속된다.

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


@router.get("/conversations/{conversation_id}/stream")
async def stream_conversation(
    conversation_id: uuid.UUID,
    session: SessionDep,
    user: ApprovedUser,
    after: int = 0,
    last_event_id: Annotated[str | None, Header()] = None,
) -> Response:
    """진행 중인(또는 막 끝난) 응답에 다시 붙는다 (새로고침, 다른 창, 끊긴 연결 다시 잇기).

    이벤트는 ``POST /api/chat`` 과 같고 ``id:`` 로 번호가 붙는다. ``Last-Event-ID`` 헤더(또는 ``after``)의
    번호 다음부터 보내서, 다시 붙어도 이벤트가 빠지거나 겹치지 않는다.

    Args:
        conversation_id: 대화 ID.
        session: DB 세션.
        user: 승인된 사용자.
        after: 이미 받은 마지막 이벤트 번호.
        last_event_id: 이미 받은 마지막 이벤트 번호 (브라우저 EventSource 가 보내는 헤더).

    Returns:
        ``text/event-stream`` 스트리밍 응답. 실행 기록이 없으면(응답 생성 중이 아니고 최근에 끝난 실행도
        없음, 서버 재시작) 204.

    Raises:
        HTTPException: 대화가 없거나 내 것이 아니면 404.
    """
    try:
        await service.get_own(session, conversation_id, user.id)
    except ConversationError as exc:
        raise _http_error(exc) from exc
    run = runs.get_stream(str(conversation_id))
    if run is None:
        return Response(status_code=204)
    start = max(after, int(last_event_id) if (last_event_id or "").isdigit() else 0)
    return StreamingResponse(
        sse_numbered(run.follow(start)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/conversations/{conversation_id}/stop", status_code=204)
async def stop_conversation(
    conversation_id: uuid.UUID, session: SessionDep, user: ApprovedUser
) -> None:
    """진행 중인 응답을 멈춘다. 그때까지의 응답은 중단 표시와 함께 저장된다.

    저장과 생성 중 표시 해제가 끝난 뒤에 돌려주므로, 화면은 바로 대기 메시지를 보낼 수 있다.
    구독 중인 다른 창에는 ``stopped`` 이벤트가 간다. 생성 중이 아니면 아무 일도 하지 않는다.

    Args:
        conversation_id: 대화 ID.
        session: DB 세션.
        user: 승인된 사용자.

    Raises:
        HTTPException: 대화가 없거나 내 것이 아니면 404.
    """
    try:
        await service.get_own(session, conversation_id, user.id)
    except ConversationError as exc:
        raise _http_error(exc) from exc
    await turns.stop_reply(conversation_id)


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
    순서이고, 실패하면 ``error``, 중지하면 ``stopped`` 를 보낸다. 이벤트마다 ``id:`` 로 번호가 붙는다
    (끊기면 ``GET /api/conversations/{id}/stream`` 으로 그 다음부터 다시 받는다). 응답 진행 이벤트는 ``delta`` (답변 텍스트),
    ``reasoning`` (추론 과정), ``tool_start`` / ``tool_end`` (도구 호출)이다. 화면에 보여준
    텍스트와 실행 단계는 assistant 메시지로 저장한다 (``turns.run_turn`` 참고). LLM 에 들어가는 대화 이력은
    checkpointer 가 관리하고, ``messages`` 테이블은 화면 표시용이다.

    응답은 백그라운드 작업에서 만들고(``turns.start_reply``) 이 응답은 그 실행을 구독한다. 연결이 끊겨도
    응답은 끝까지 만들어져 저장된다.

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
            project_id=req.project_id,
        )
    except (ConversationError, AttachError) as exc:
        raise _http_error(exc) from exc

    # 응답은 백그라운드에서 만든다 (끝나면 assistant 메시지 저장, 생성 중 표시 해제).
    # 이 응답은 그 실행을 구독할 뿐이라, 연결이 끊겨도(새로고침, 탭 닫기) 실행은 끝까지 돈다.
    run = turns.start_reply(
        turn.conversation_id, turn.text, turn.images, turn.model, project_id=turn.project_id
    )
    return StreamingResponse(
        sse_numbered(run.follow()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
