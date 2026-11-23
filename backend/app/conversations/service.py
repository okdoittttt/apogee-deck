"""대화 서비스: 대화 조회·삭제, 프로젝트 삭제, 이미지 원본 읽기, 새 턴 준비(검증 + 사용자 메시지 저장).

HTTP 를 모른다. 사용자에게 보여줄 실패는 ``ConversationError`` (상태 코드 포함)로 던지고, API 계층이
응답으로 바꾼다. 문서를 붙일 수 없을 때는 문서 서비스의 ``AttachError`` 가 그대로 올라간다.
"""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import images as chat_images
from app.conversations import runs
from app.core.config import get_settings
from app.core.storage import ObjectNotFoundError, get_storage
from app.documents import reading
from app.documents import service as documents
from app.llm.catalog import available_models
from app.models import Conversation, Document, Message, MessageImage
from app.orchestration import runtime
from app.projects import service as projects

logger = logging.getLogger(__name__)

BUSY_DETAIL = "이전 응답이 아직 생성 중입니다. 완료된 후 다시 시도해 주세요."


class ConversationError(Exception):
    """대화 요청을 처리할 수 없다. 메시지는 사용자에게 그대로 보여준다.

    Attributes:
        status_code: 돌려줄 HTTP 상태 코드.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        """오류를 만든다.

        Args:
            status_code: HTTP 상태 코드.
            detail: 사용자에게 보여줄 문구.
        """
        super().__init__(detail)
        self.status_code = status_code


def _not_found() -> ConversationError:
    return ConversationError(404, "Conversation not found")


async def get_own(
    session: AsyncSession, conversation_id: uuid.UUID, user_id: uuid.UUID, **kwargs: Any
) -> Conversation:
    """내 대화를 읽는다. 없거나 다른 사람의 대화면 404 (존재 여부도 숨긴다).

    Args:
        session: DB 세션.
        conversation_id: 대화 ID.
        user_id: 요청한 사용자 ID.
        **kwargs: ``session.get`` 에 넘길 옵션 (관계 미리 읽기 등).

    Returns:
        대화.

    Raises:
        ConversationError: 대화가 없거나 내 것이 아니면 404.
    """
    conv = await session.get(Conversation, conversation_id, **kwargs)
    if conv is None or conv.user_id != user_id:
        raise _not_found()
    return conv


async def list_recent(session: AsyncSession, user_id: uuid.UUID) -> list[Conversation]:
    """내 최근 대화 목록을 최신순으로 돌려준다 (최대 100개).

    Args:
        session: DB 세션.
        user_id: 사용자 ID.

    Returns:
        대화 목록.
    """
    result = await session.scalars(
        select(Conversation)
        .where(Conversation.user_id == user_id)
        .order_by(Conversation.created_at.desc())
        .limit(100)
    )
    return list(result.all())


@dataclass(frozen=True)
class ConversationView:
    """화면에 보여줄 대화 하나.

    Attributes:
        conversation: 메시지와 이미지 ID 를 미리 읽은 대화.
        documents: 메시지 ID → ``(첨부 문서, 본문 글자 수)`` 목록.
        running: 응답 생성 중인지.
    """

    conversation: Conversation
    documents: dict[uuid.UUID, list[tuple[Document, int | None]]]
    running: bool


async def get_view(
    session: AsyncSession, conversation_id: uuid.UUID, user_id: uuid.UUID
) -> ConversationView:
    """메시지, 이미지 ID, 첨부 문서 정보를 포함한 대화 하나를 읽는다.

    이미지 바이너리와 문서 본문은 읽지 않는다.

    Args:
        session: DB 세션.
        conversation_id: 대화 ID.
        user_id: 요청한 사용자 ID.

    Returns:
        대화와 첨부 정보, 응답 생성 중인지 여부.

    Raises:
        ConversationError: 대화가 없거나 내 것이 아니면 404.
    """
    conv = await get_own(
        session,
        conversation_id,
        user_id,
        options=[
            selectinload(Conversation.messages)
            .selectinload(Message.images)
            .load_only(MessageImage.id, MessageImage.position)
        ],
    )
    return ConversationView(
        conversation=conv,
        documents=await documents.documents_by_message(session, conv.id),
        running=await runs.run_registry.is_running(str(conv.id)),
    )


async def set_feedback(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    user_id: uuid.UUID,
    value: int | None,
    comment: str | None = None,
) -> Message:
    """내 대화의 응답에 피드백(👍/👎, 의견)을 저장한다. ``value`` 가 ``None`` 이면 피드백을 지운다.

    Langfuse 점수는 호출하는 쪽이 남긴다 (``tracing.record_feedback``, 메시지의 ``trace_id``).

    Args:
        session: DB 세션.
        conversation_id: 대화 ID.
        message_id: assistant 메시지 ID.
        user_id: 요청한 사용자 ID.
        value: 1 (👍), -1 (👎), ``None`` (취소).
        comment: 남긴 의견. 피드백을 지우면 함께 지운다.

    Returns:
        저장한 메시지.

    Raises:
        ConversationError: 대화가 없거나 내 것이 아니거나, 그 대화의 응답 메시지가 아니면 404.
    """
    await get_own(session, conversation_id, user_id)
    message = await session.get(Message, message_id)
    if message is None or message.conversation_id != conversation_id or message.role != "assistant":
        raise ConversationError(404, "Message not found")
    message.feedback = value
    message.feedback_comment = ((comment or "").strip() or None) if value is not None else None
    await session.commit()
    return message


async def is_running(session: AsyncSession, conversation_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """내 대화의 응답이 생성 중인지 돌려준다. 메시지를 불러오지 않아서 가볍다.

    Args:
        session: DB 세션.
        conversation_id: 대화 ID.
        user_id: 요청한 사용자 ID.

    Returns:
        응답 생성 중인지.

    Raises:
        ConversationError: 대화가 없거나 내 것이 아니면 404.
    """
    await get_own(session, conversation_id, user_id)
    return await runs.run_registry.is_running(str(conversation_id))


async def read_image(
    session: AsyncSession, image_id: uuid.UUID, user_id: uuid.UUID
) -> tuple[bytes, str]:
    """내 대화에 첨부된 이미지 원본을 읽는다.

    Args:
        session: DB 세션.
        image_id: 이미지 ID.
        user_id: 요청한 사용자 ID.

    Returns:
        ``(원본 바이트, MIME 타입)``.

    Raises:
        ConversationError: 이미지가 없거나 내 대화의 이미지가 아니면 404.
    """
    img = await session.scalar(
        select(MessageImage)
        .join(Message, Message.id == MessageImage.message_id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(MessageImage.id == image_id, Conversation.user_id == user_id)
    )
    storage = get_storage()
    if img is None or storage is None:
        raise ConversationError(404, "Image not found")
    try:
        data = await storage.get(img.storage_key)
    except ObjectNotFoundError as exc:
        raise ConversationError(404, "Image not found") from exc
    return data, img.mime_type


async def delete(session: AsyncSession, conversation_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """대화와 그 에이전트 상태(checkpoint), 첨부 문서와 이미지를 삭제한다.

    checkpoint 삭제가 실패해도 대화 삭제는 유지되고, 남은 checkpoint 는 다음 기동 시 정리된다.
    첨부 문서와 이미지의 DB 기록은 대화와 함께 지워지고, 원본은 저장소에서 지운다. 문서 원본은
    지우지 못해도 보관 기간이 지나면 저장소가 지우지만, 이미지 원본은 기록만 남는다.

    Args:
        session: DB 세션.
        conversation_id: 대화 ID.
        user_id: 요청한 사용자 ID.

    Raises:
        ConversationError: 대화가 없거나 내 것이 아니면 404, 응답 생성 중이면 409.
    """
    conv = await get_own(session, conversation_id, user_id)
    if await runs.run_registry.is_running(str(conversation_id)):
        raise ConversationError(409, "응답 생성 중인 대화는 삭제할 수 없습니다.")
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
    await _delete_threads([conversation_id])


async def _delete_threads(conversation_ids: Sequence[uuid.UUID]) -> None:
    """대화들의 checkpoint 를 지운다. 실패하면 기록만 남긴다 (다음 기동 때 정리된다)."""
    for conversation_id in conversation_ids:
        try:
            await runtime.delete_thread(str(conversation_id))
        except Exception:
            logger.exception("failed to delete checkpoint thread %s", conversation_id)


async def delete_project(session: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """프로젝트와 그 안의 대화·문서를 모두 지운다.

    DB 기록은 프로젝트와 함께 지워지고(CASCADE), 프로젝트 문서·대화 첨부 문서·이미지 원본은
    저장소에서, 대화 기록은 checkpoint 에서 지운다.

    Args:
        session: DB 세션.
        project_id: 프로젝트 ID.
        user_id: 요청한 사용자 ID.

    Raises:
        ProjectError: 프로젝트가 없거나 내 것이 아니면 404.
        ConversationError: 프로젝트 안의 대화가 응답 생성 중이면 409.
    """
    project = await projects.get_own(session, project_id, user_id)
    conversation_ids = list(
        await session.scalars(select(Conversation.id).where(Conversation.project_id == project_id))
    )
    for conversation_id in conversation_ids:
        if await runs.run_registry.is_running(str(conversation_id)):
            raise ConversationError(
                409, "응답 생성 중인 대화가 있어 프로젝트를 삭제할 수 없습니다."
            )
    in_project = (Document.project_id == project_id) | Document.conversation_id.in_(
        conversation_ids
    )
    document_keys = list(await session.scalars(select(Document.storage_key).where(in_project)))
    document_keys += await documents.element_image_keys(session, Document.project_id == project_id)
    image_keys = list(
        await session.scalars(
            select(MessageImage.storage_key)
            .join(Message, Message.id == MessageImage.message_id)
            .where(Message.conversation_id.in_(conversation_ids))
        )
    )
    await session.delete(project)
    await session.commit()
    await documents.delete_objects(document_keys)
    await chat_images.delete_images(get_storage(), image_keys)
    await _delete_threads(conversation_ids)


@dataclass(frozen=True)
class Turn:
    """저장을 마치고 에이전트 실행만 남은 턴. ``turns.run_turn`` 에 넘긴다.

    Attributes:
        conversation_id: 대화 ID. 생성 중으로 표시되어 있다.
        text: 모델에게 보낼 사용자 메시지 텍스트 (첨부 목록 포함).
        images: 이미지 참조 블록 (``app.images.image_ref_block``).
        model: 응답할 모델 ID. ``None`` 이면 설정의 기본 모델.
        project_id: 대화가 속한 프로젝트 ID. 프로젝트 밖의 대화는 ``None``.
    """

    conversation_id: uuid.UUID
    text: str
    images: list[dict[str, Any]]
    model: str | None
    project_id: uuid.UUID | None = None


def new_title(message: str, has_documents: bool) -> str:
    """새 대화의 제목: 메시지 앞부분, 없으면 첨부 종류.

    Args:
        message: 사용자 메시지.
        has_documents: 문서를 첨부했는지.

    Returns:
        대화 제목.
    """
    if title := message.strip()[:50]:
        return title
    return "문서" if has_documents else "이미지"


async def start_turn(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    conversation_id: uuid.UUID | None,
    message: str,
    images: Sequence[tuple[str, bytes]],
    document_ids: Sequence[uuid.UUID],
    model: str | None,
    project_id: uuid.UUID | None = None,
) -> Turn:
    """새 턴을 검증하고, 대화를 생성 중으로 표시하고, 사용자 메시지를 저장한다.

    같은 대화의 응답이 이미 생성 중이면(다른 창에서 보낸 경우) 메시지를 저장하지 않고 거절한다.
    이미지 원본은 저장소에 올리고, 첨부 문서는 사용자 메시지와 같은 트랜잭션에서 이 대화와 메시지에
    연결한다. 저장이 실패하면 올린 이미지를 지우고 생성 중 표시를 되돌린다.

    모델에게는 메시지 뒤에 첨부 목록을 붙여 보내고(짧은 문서는 본문 포함), 긴 문서는
    ``read_document`` 도구로 읽게 한다. 화면에 저장하는 메시지 텍스트에는 붙이지 않는다.

    Args:
        session: DB 세션.
        user_id: 요청한 사용자 ID. 새 대화의 주인이 된다.
        conversation_id: 이어갈 대화 ID. ``None`` 이면 새 대화를 만든다.
        message: 사용자 메시지.
        images: ``(MIME 타입, 원본 바이트)`` 목록.
        document_ids: 붙일 문서 ID.
        model: 응답할 모델 ID. ``None`` 이면 설정의 기본 모델.
        project_id: 새 대화를 만들 프로젝트 ID. 이어가는 대화에는 쓰지 않는다 (대화의 프로젝트를 따른다).

    Returns:
        에이전트 실행에 넘길 턴.

    Raises:
        ConversationError: 이미지 입력이 꺼져 있으면 400, 저장소가 없어 이미지를 받을 수 없으면 503,
            지금 쓸 수 없는 모델이면 400, 문서가 너무 많으면 400, 대화나 프로젝트가 없거나 내 것이
            아니면 404, 같은 대화의 응답이 생성 중이면 409.
        AttachError: 붙일 수 없는 문서일 때 (``documents.check_attachable``).
    """
    settings = get_settings()
    if images and not settings.enable_vision:
        raise ConversationError(400, "현재 모델은 이미지 입력을 지원하지 않습니다.")
    storage = get_storage()
    if images and storage is None:
        raise ConversationError(503, "첨부 저장소가 설정되지 않아 이미지를 보낼 수 없습니다.")
    if model and model not in {o.id for o in await available_models(settings)}:
        raise ConversationError(
            400, "선택한 모델을 지금 쓸 수 없습니다. 모델 목록을 새로고침해 주세요."
        )
    if len(document_ids) > settings.max_documents_per_message:
        raise ConversationError(
            400, f"문서는 한 번에 {settings.max_documents_per_message}개까지 보낼 수 있습니다."
        )
    await documents.check_attachable(session, document_ids, user_id)

    # 1) 대화 조회/생성 → 생성 중 표시 (이미 생성 중이면 저장 전에 거절)
    if conversation_id:
        conv = await get_own(session, conversation_id, user_id)
    else:
        if project_id is not None:
            try:
                await projects.get_own(session, project_id, user_id)
            except projects.ProjectError as exc:
                raise ConversationError(404, "Project not found") from exc
        conv = Conversation(
            title=new_title(message, bool(document_ids)), user_id=user_id, project_id=project_id
        )
        session.add(conv)
        await session.flush()
    if not await runs.run_registry.try_acquire(str(conv.id)):
        raise ConversationError(409, BUSY_DETAIL)

    # 2) 이미지 원본을 저장소에 올리고, 사용자 메시지 저장, 문서 연결.
    #    실패하면 올린 이미지를 지우고 생성 중 표시를 되돌린다.
    image_rows: list[MessageImage] = []
    try:
        if images:
            assert storage is not None
            image_rows = await chat_images.store_images(storage, images)
        user_message = Message(
            conversation_id=conv.id,
            role="user",
            content=message,
            images=image_rows,
        )
        session.add(user_message)
        await session.flush()
        await documents.attach_to_message(session, document_ids, conv.id, user_message.id)
        attachments = await documents.load_attachments(
            session, document_ids, settings.document_inline_max_tokens
        )
        await session.commit()
    except documents.AttachError:
        await session.rollback()
        await chat_images.delete_images(storage, [r.storage_key for r in image_rows])
        await runs.run_registry.release(str(conv.id))
        raise
    except BaseException:
        await chat_images.delete_images(storage, [r.storage_key for r in image_rows])
        await runs.run_registry.release(str(conv.id))
        raise
    return Turn(
        conversation_id=conv.id,
        text=reading.with_attachments(
            message, reading.attachment_block(attachments, can_read=settings.enable_tools)
        ),
        images=[chat_images.image_ref_block(row) for row in image_rows],
        model=model,
        project_id=conv.project_id,
    )
