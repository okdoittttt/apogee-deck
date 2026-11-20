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

from app import images as chat_images
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
from app.auth.deps import ApprovedUser
from app.config import get_settings
from app.db import SessionDep, SessionLocal
from app.documents import reading
from app.documents import service as documents
from app.documents.formats import SUPPORTED_EXTENSIONS
from app.llm import describe_error, model_name
from app.llm.catalog import available_models, default_model_id
from app.models import Conversation, Message, MessageImage, User
from app.routers.documents import document_out
from app.runs import InMemoryRunRegistry, RunRegistry
from app.schemas import (
    ChatRequest,
    ConversationDetail,
    ConversationOut,
    ConversationStatus,
    MessageOut,
    ModelList,
    ModelOut,
)
from app.storage import ObjectNotFoundError, get_storage

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


async def _own_conversation(
    session: SessionDep, conversation_id: uuid.UUID, user: User, **kwargs: Any
) -> Conversation:
    """내 대화를 읽는다. 없거나 다른 사람의 대화면 404 (존재 여부도 숨긴다).

    Args:
        session: DB 세션.
        conversation_id: 대화 ID.
        user: 요청한 사용자.
        **kwargs: ``session.get`` 에 넘길 옵션 (관계 미리 읽기 등).

    Returns:
        대화.

    Raises:
        HTTPException: 대화가 없거나 내 것이 아니면 404.
    """
    conv = await session.get(Conversation, conversation_id, **kwargs)
    if conv is None or conv.user_id != user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv


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
    result = await session.scalars(
        select(Conversation)
        .where(Conversation.user_id == user.id)
        .order_by(Conversation.created_at.desc())
        .limit(100)
    )
    return list(result.all())


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
    conv = await _own_conversation(
        session,
        conversation_id,
        user,
        options=[
            selectinload(Conversation.messages)
            .selectinload(Message.images)
            .load_only(MessageImage.id, MessageImage.position)
        ],
    )
    attached = await documents.documents_by_message(session, conv.id)
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
                documents=[document_out(d, chars) for d, chars in attached.get(m.id, [])],
            )
            for m in conv.messages
        ],
        running=await run_registry.is_running(str(conv.id)),
    )


@router.get("/conversations/{conversation_id}/status", response_model=ConversationStatus)
async def get_conversation_status(
    conversation_id: uuid.UUID, session: SessionDep, user: ApprovedUser
) -> ConversationStatus:
    """대화의 응답 생성 여부를 돌려준다.

    다른 창에서 생성 중인 응답이 끝났는지 화면이 주기적으로 확인할 때 쓴다.
    메시지를 불러오지 않아서 가볍다.

    Args:
        conversation_id: 대화 ID.
        session: DB 세션.
        user: 승인된 사용자.

    Returns:
        실행 상태.

    Raises:
        HTTPException: 대화가 없거나 내 것이 아니면 404.
    """
    await _own_conversation(session, conversation_id, user)
    return ConversationStatus(running=await run_registry.is_running(str(conversation_id)))


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
    img = await session.scalar(
        select(MessageImage)
        .join(Message, Message.id == MessageImage.message_id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(MessageImage.id == image_id, Conversation.user_id == user.id)
    )
    storage = get_storage()
    if img is None or storage is None:
        raise HTTPException(status_code=404, detail="Image not found")
    try:
        data = await storage.get(img.storage_key)
    except ObjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Image not found") from exc
    return Response(
        content=data,
        media_type=img.mime_type,
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: uuid.UUID, session: SessionDep, user: ApprovedUser
) -> None:
    """대화와 그 에이전트 상태(checkpoint), 첨부 문서와 이미지를 삭제한다.

    checkpoint 삭제가 실패해도 대화 삭제는 유지되고, 남은 checkpoint 는 다음 기동 시 정리된다.
    첨부 문서와 이미지의 DB 기록은 대화와 함께 지워지고, 원본은 저장소에서 지운다. 문서 원본은
    지우지 못해도 보관 기간이 지나면 저장소가 지우지만, 이미지 원본은 기록만 남는다.

    Args:
        conversation_id: 대화 ID.
        session: DB 세션.
        user: 승인된 사용자.

    Raises:
        HTTPException: 대화가 없거나 내 것이 아니면 404, 응답 생성 중이면 409.
    """
    conv = await _own_conversation(session, conversation_id, user)
    if await run_registry.is_running(str(conversation_id)):
        raise HTTPException(status_code=409, detail="응답 생성 중인 대화는 삭제할 수 없습니다.")
    document_keys = await documents.conversation_storage_keys(session, conversation_id)
    image_keys = list(
        await session.scalars(
            select(MessageImage.storage_key)
            .join(Message, Message.id == MessageImage.message_id)
            .where(Message.conversation_id == conversation_id)
        )
    )
    await session.delete(conv)
    await session.commit()
    await documents.delete_objects(document_keys)
    await chat_images.delete_images(get_storage(), image_keys)
    try:
        await runtime.delete_thread(str(conversation_id))
    except Exception:
        logger.exception("failed to delete checkpoint thread %s", conversation_id)


@router.post("/chat")
async def chat(req: ChatRequest, session: SessionDep, user: ApprovedUser) -> StreamingResponse:
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
        user: 승인된 사용자. 새 대화의 주인이 된다.

    Returns:
        ``text/event-stream`` 스트리밍 응답.

    첨부 문서(``document_ids``)는 사용자 메시지와 같은 트랜잭션에서 이 대화와 메시지에 연결한다.
    모델에게는 메시지 뒤에 첨부 목록을 붙여 보내고(짧은 문서는 본문 포함), 긴 문서는
    ``read_document`` 도구로 읽게 한다. 화면에 저장하는 메시지 텍스트에는 붙이지 않는다.

    Raises:
        HTTPException: 이미지 입력이 꺼져 있는데 이미지를 보내면 400, 지금 쓸 수 없는 모델이면 400,
            문서가 너무 많으면 400, 붙일 수 없는 문서면 400/404/409 (``documents.check_attachable``),
            대화가 없거나 내 것이 아니면 404, 같은 대화의 응답이 생성 중이면 409.
    """
    settings = get_settings()
    if req.images and not settings.enable_vision:
        raise HTTPException(status_code=400, detail="현재 모델은 이미지 입력을 지원하지 않습니다.")
    storage = get_storage()
    if req.images and storage is None:
        raise HTTPException(
            status_code=503, detail="첨부 저장소가 설정되지 않아 이미지를 보낼 수 없습니다."
        )
    if req.model and req.model not in {o.id for o in await available_models(settings)}:
        raise HTTPException(
            status_code=400,
            detail="선택한 모델을 지금 쓸 수 없습니다. 모델 목록을 새로고침해 주세요.",
        )
    if len(req.document_ids) > settings.max_documents_per_message:
        raise HTTPException(
            status_code=400,
            detail=f"문서는 한 번에 {settings.max_documents_per_message}개까지 보낼 수 있습니다.",
        )
    try:
        await documents.check_attachable(session, req.document_ids, user.id)
    except documents.AttachError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    # 1) 대화 조회/생성 → 생성 중 표시 (이미 생성 중이면 저장 전에 거절)
    if req.conversation_id:
        conv = await _own_conversation(session, req.conversation_id, user)
    else:
        conv = Conversation(title=_new_title(req), user_id=user.id)
        session.add(conv)
        await session.flush()
    if not await run_registry.try_acquire(str(conv.id)):
        raise HTTPException(status_code=409, detail=BUSY_DETAIL)

    # 2) 이미지 원본을 저장소에 올리고, 사용자 메시지 저장, 문서 연결.
    #    실패하면 올린 이미지를 지우고 생성 중 표시를 되돌린다.
    image_rows: list[MessageImage] = []
    try:
        if req.images:
            assert storage is not None
            image_rows = await chat_images.store_images(
                storage, [(img.mime_type, img.to_bytes()) for img in req.images]
            )
        user_message = Message(
            conversation_id=conv.id,
            role="user",
            content=req.message,
            images=image_rows,
        )
        session.add(user_message)
        await session.flush()
        await documents.attach_to_message(session, req.document_ids, conv.id, user_message.id)
        attachments = await documents.load_attachments(
            session, req.document_ids, settings.document_inline_max_tokens
        )
        await session.commit()
    except documents.AttachError as exc:
        await session.rollback()
        await chat_images.delete_images(storage, [r.storage_key for r in image_rows])
        await run_registry.release(str(conv.id))
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except BaseException:
        await chat_images.delete_images(storage, [r.storage_key for r in image_rows])
        await run_registry.release(str(conv.id))
        raise
    images = [chat_images.image_ref_block(row) for row in image_rows]
    model_text = reading.with_attachments(
        req.message, reading.attachment_block(attachments, can_read=settings.enable_tools)
    )

    # 3) 에이전트 응답 스트리밍 → 끝나면(또는 중단되면) assistant 메시지 저장, 생성 중 표시 해제
    return StreamingResponse(
        stream_and_save(conv.id, model_text, images, req.model),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _new_title(req: ChatRequest) -> str:
    """새 대화의 제목: 메시지 앞부분, 없으면 첨부 종류."""
    if title := req.message.strip()[:50]:
        return title
    return "문서" if req.document_ids else "이미지"


# 응답이 끝까지 오지 못했을 때 저장하는 텍스트 뒤에 붙이는 표시 (프론트엔드와 같은 문구).
INTERRUPTED_SUFFIX = "\n\n_(응답이 중단되었습니다.)_"
ERROR_SUFFIX = "\n\n_(오류로 응답이 중단되었습니다.)_"


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


async def stream_and_save(
    conversation_id: uuid.UUID,
    text: str,
    images: list[dict[str, Any]],
    model: str | None = None,
) -> AsyncIterator[str]:
    """에이전트 응답을 SSE 이벤트로 스트리밍하고, 화면에 보여준 텍스트를 저장한다.

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
        conversation_id: 대화 ID. 호출 전에 ``run_registry`` 에서 생성 중으로 표시되어 있어야 한다.
        text: 모델에게 보낼 사용자 메시지 텍스트 (첨부 목록 포함).
        images: 이미지 참조 블록 (``app.images.image_ref_block``).
        model: 응답할 모델 ID. ``None`` 이면 설정의 기본 모델.

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
        async for event in stream_reply(run_id, text, images, model=model):
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
                    await run_registry.release(run_id)
                yield sse("done", {"stop_reason": event.stop_reason, "model": event.model})
    except Exception as e:  # provider 마다 예외 타입이 달라 한 곳에서 처리
        # 예외 원문은 로그에만 남기고, 화면에는 로그에서 찾을 수 있는 오류 ID 를 보낸다.
        error_id = uuid.uuid4().hex[:8]
        logger.exception("Agent run failed (error_id=%s)", error_id)
        with anyio.CancelScope(shield=True):
            outcome = "error"
            recorder.finalize("error")
            await _save_partial(conversation_id, shown, recorder, ERROR_SUFFIX, model)
            await run_registry.release(run_id)
        message = describe_error(e) or "응답을 만드는 중 오류가 발생했습니다."
        yield sse("error", {"message": f"{message} (오류 ID: {error_id})", "error_id": error_id})
    finally:
        # 연결이 끊기면 이 요청은 취소된 상태라, 보호하지 않으면 정리 중 await 도 취소된다.
        with anyio.CancelScope(shield=True):
            if outcome == "interrupted":
                recorder.finalize("interrupted")
                await _save_partial(conversation_id, shown, recorder, INTERRUPTED_SUFFIX, model)
            # 어떤 경로로 끝나든 생성 중 표시는 반드시 해제한다 (이미 해제됐으면 아무 일 없음).
            await run_registry.release(run_id)


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
