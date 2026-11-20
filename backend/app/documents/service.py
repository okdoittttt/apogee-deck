"""첨부 문서 처리: 올리기, 백그라운드 파싱, 정리.

흐름: 올리기 → 크기·형식 검사 → 원본을 첨부 저장소에 저장 → DB 에 ``processing`` 으로 기록 →
백그라운드에서 별도 프로세스로 파싱 → ``ready`` / ``failed`` 로 갱신.
"""

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.core.config import Settings, get_settings
from app.core.db import SessionLocal
from app.core.storage import DOCUMENTS_PREFIX, ObjectStorage, get_storage
from app.documents.formats import DocumentKind, detect, safe_filename
from app.documents.parser import ParseError
from app.documents.reading import AttachedDocument, estimate_tokens, max_inline_chars
from app.documents.worker import parse_in_subprocess
from app.models import Document

logger = logging.getLogger(__name__)

INTERRUPTED_ERROR = "서버가 다시 시작되어 처리가 중단되었습니다. 다시 올려 주세요."
CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60

_tasks: set[asyncio.Task] = set()
"""실행 중인 파싱 작업. 참조를 들고 있지 않으면 작업이 중간에 사라질 수 있다."""
_parse_slots: asyncio.Semaphore | None = None


def storage_key(document_id: uuid.UUID) -> str:
    """문서 원본의 객체 키.

    Args:
        document_id: 문서 ID.

    Returns:
        ``"documents/<문서 ID>"``.
    """
    return f"{DOCUMENTS_PREFIX}{document_id}"


async def create_document(
    session: AsyncSession,
    storage: ObjectStorage,
    data: bytes,
    filename: str | None,
    user_id: uuid.UUID,
) -> Document:
    """문서를 검사하고 원본을 저장한 뒤 ``processing`` 상태로 기록한다.

    파싱은 하지 않는다. 기록한 뒤 ``schedule_parse`` 로 시작한다.

    Args:
        session: DB 세션. 이 함수가 commit 한다.
        storage: 첨부 저장소.
        data: 파일 내용. 크기 제한은 호출하는 쪽에서 먼저 확인한다.
        filename: 사용자가 올린 파일 이름.
        user_id: 올린 사용자.

    Returns:
        기록한 문서.

    Raises:
        UnsupportedDocumentError: 빈 파일, 지원하지 않는 형식, 손상된 파일일 때.
    """
    name = safe_filename(filename)
    fmt = detect(data, name)
    doc = Document(
        id=uuid.uuid4(),
        user_id=user_id,
        filename=name,
        kind=fmt.kind.value,
        mime_type=fmt.mime_type,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        status="processing",
    )
    doc.storage_key = storage_key(doc.id)
    await storage.put(doc.storage_key, data, fmt.mime_type)
    session.add(doc)
    await session.commit()
    return doc


def schedule_parse(document_id: uuid.UUID, data: bytes, kind: str, filename: str) -> asyncio.Task:
    """백그라운드에서 문서를 파싱한다.

    올릴 때 받은 내용을 그대로 넘겨서 저장소에서 다시 받지 않는다. 동시에 파싱하는 문서 수는
    ``DOCUMENT_PARSE_WORKERS`` 로 제한하고, 나머지는 차례를 기다린다.

    Args:
        document_id: 문서 ID.
        data: 파일 내용.
        kind: 문서 형식.
        filename: 파일 이름.

    Returns:
        파싱 작업. 테스트에서 끝날 때까지 기다릴 때 쓴다.
    """
    task = asyncio.create_task(_parse_and_save(document_id, data, kind, filename))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def _slots(settings: Settings) -> asyncio.Semaphore:
    global _parse_slots
    if _parse_slots is None:
        _parse_slots = asyncio.Semaphore(max(1, settings.document_parse_workers))
    return _parse_slots


async def _parse_and_save(document_id: uuid.UUID, data: bytes, kind: str, filename: str) -> None:
    settings = get_settings()
    values: dict = {"parsed_at": datetime.now(UTC)}
    try:
        async with _slots(settings):
            result = await parse_in_subprocess(
                data,
                DocumentKind(kind),
                filename,
                max_pages=settings.document_max_pages,
                timeout=settings.document_parse_timeout_seconds,
            )
    except ParseError as exc:
        values |= {"status": "failed", "error": str(exc)[:500]}
        logger.info("document %s failed to parse: %s", document_id, exc)
    except Exception:
        logger.exception("document %s parse task failed", document_id)
        values |= {"status": "failed", "error": "문서를 처리하다가 오류가 났습니다."}
    else:
        values |= {
            "status": "ready",
            "content": result.content,
            "page_count": result.page_count,
            "parsed_at": datetime.now(UTC),
        }
        logger.info(
            "document %s parsed (%s pages, %d chars)",
            document_id,
            result.page_count,
            len(result.content),
        )
    async with SessionLocal() as session:
        # 파싱하는 동안 문서가 지워졌으면 아무것도 갱신되지 않는다.
        await session.execute(
            update(Document)
            .where(Document.id == document_id, Document.status == "processing")
            .values(**values)
        )
        await session.commit()


async def mark_interrupted() -> int:
    """앞선 실행에서 파싱 중이던 문서를 실패로 표시한다. 앱이 시작할 때 부른다.

    Returns:
        실패로 바꾼 문서 수.
    """
    async with SessionLocal() as session:
        res = await session.execute(
            update(Document)
            .where(Document.status == "processing")
            .values(status="failed", error=INTERRUPTED_ERROR, parsed_at=datetime.now(UTC))
        )
        await session.commit()
        return res.rowcount or 0


async def delete_objects(keys: Iterable[str]) -> None:
    """문서 원본을 저장소에서 지운다. 실패해도 기록만 남긴다 (보관 기간이 지나면 저장소가 지운다).

    Args:
        keys: 지울 객체 키.
    """
    storage = get_storage()
    if storage is None:
        return
    for key in keys:
        try:
            await storage.delete(key)
        except Exception:
            logger.warning("failed to delete attachment object %s", key, exc_info=True)


async def cleanup_expired(now: datetime | None = None) -> int:
    """보관 기간이 지난 문서와, 올렸지만 보내지 않은 채 오래된 문서를 지운다.

    DB 기록(파싱 결과 포함)을 지우고 원본도 저장소에서 지운다.

    Args:
        now: 기준 시각. 테스트에서만 넘긴다.

    Returns:
        지운 문서 수.
    """
    settings = get_settings()
    now = now or datetime.now(UTC)
    expired = Document.created_at < now - timedelta(days=settings.attachment_retention_days)
    unsent = (Document.conversation_id.is_(None)) & (
        Document.created_at < now - timedelta(hours=settings.document_unsent_ttl_hours)
    )
    async with SessionLocal() as session:
        res = await session.execute(
            delete(Document).where(or_(expired, unsent)).returning(Document.storage_key)
        )
        keys = list(res.scalars())
        await session.commit()
    await delete_objects(keys)
    return len(keys)


async def cleanup_loop() -> None:
    """앱이 떠 있는 동안 하루에 한 번 ``cleanup_expired`` 를 실행한다. 시작하자마자 한 번 실행한다."""
    while True:
        try:
            if removed := await cleanup_expired():
                logger.info("removed %d expired documents", removed)
        except Exception:
            logger.exception("document cleanup failed")
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)


async def conversation_storage_keys(session: AsyncSession, conversation_id: uuid.UUID) -> list[str]:
    """대화에 속한 문서의 원본 객체 키. 대화를 지우기 전에 원본을 함께 지우려고 쓴다.

    Args:
        session: DB 세션.
        conversation_id: 대화 ID.

    Returns:
        객체 키 목록.
    """
    rows = await session.scalars(
        select(Document.storage_key).where(Document.conversation_id == conversation_id)
    )
    return list(rows)


# --- 메시지에 붙이기, 읽기 -------------------------------------------------------------------------


class AttachError(Exception):
    """메시지에 문서를 붙일 수 없다. 메시지는 사용자에게 그대로 보여준다.

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


async def check_attachable(
    session: AsyncSession, document_ids: list[uuid.UUID], user_id: uuid.UUID
) -> None:
    """문서들을 메시지에 붙일 수 있는지 확인한다 (내가 올렸고, 아직 보내지 않았고, 준비된 문서).

    대화를 만들거나 메시지를 저장하기 전에 불러서, 실패해도 아무것도 남지 않게 한다.

    Args:
        session: DB 세션.
        document_ids: 붙일 문서 ID.
        user_id: 보내는 사용자. 다른 사람이 올린 문서는 없는 문서로 본다.

    Raises:
        AttachError: 없는 문서(404), 이미 보낸 문서(409), 파싱 중(409), 파싱 실패(400)일 때.
    """
    if not document_ids:
        return
    rows = (
        await session.execute(
            select(Document.id, Document.filename, Document.status, Document.conversation_id).where(
                Document.id.in_(document_ids), Document.user_id == user_id
            )
        )
    ).all()
    found = {r.id: r for r in rows}
    for doc_id in document_ids:
        row = found.get(doc_id)
        if row is None:
            raise AttachError(404, "첨부한 문서를 찾을 수 없습니다. 다시 올려 주세요.")
        if row.conversation_id is not None:
            raise AttachError(409, f"이미 보낸 문서입니다: {row.filename}")
        if row.status == "processing":
            raise AttachError(409, f"아직 문서를 읽는 중입니다: {row.filename}")
        if row.status != "ready":
            raise AttachError(400, f"읽지 못한 문서는 보낼 수 없습니다: {row.filename}")


async def attach_to_message(
    session: AsyncSession,
    document_ids: list[uuid.UUID],
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
) -> None:
    """문서들을 대화와 메시지에 연결한다. commit 은 호출하는 쪽에서 메시지 저장과 함께 한다.

    그 사이에 다른 요청이 같은 문서를 먼저 보냈으면 연결하지 않고 실패한다.

    Args:
        session: DB 세션.
        document_ids: 붙일 문서 ID (``check_attachable`` 로 확인한 것).
        conversation_id: 대화 ID.
        message_id: 사용자 메시지 ID. 메시지가 세션에 flush 되어 있어야 한다.

    Raises:
        AttachError: 다른 요청이 먼저 보냈거나 그 사이에 지워졌을 때 (409).
    """
    if not document_ids:
        return
    res = await session.execute(
        update(Document)
        .where(
            Document.id.in_(document_ids),
            Document.conversation_id.is_(None),
            Document.status == "ready",
        )
        .values(conversation_id=conversation_id, message_id=message_id)
    )
    if res.rowcount != len(set(document_ids)):
        raise AttachError(409, "첨부한 문서를 붙이지 못했습니다. 다시 올려 주세요.")


async def load_attachments(
    session: AsyncSession, document_ids: list[uuid.UUID], inline_max_tokens: int
) -> list[AttachedDocument]:
    """모델에게 줄 첨부 목록을 만든다. 짧은 문서는 본문을 함께 읽는다.

    Args:
        session: DB 세션.
        document_ids: 메시지에 붙인 문서 ID (보낸 순서).
        inline_max_tokens: 본문을 바로 넣을 문서의 토큰 상한. 0 이면 본문을 넣지 않는다.

    Returns:
        보낸 순서대로의 첨부 문서.
    """
    if not document_ids:
        return []
    limit = max_inline_chars(inline_max_tokens)
    length = func.char_length(Document.content)
    rows = (
        await session.execute(
            select(
                Document.id,
                Document.filename,
                Document.kind,
                Document.page_count,
                length.label("chars"),
                # 길면 본문을 읽지 않는다 (수 MB 일 수 있다)
                case((length <= limit, Document.content), else_=None).label("content"),
            ).where(Document.id.in_(document_ids))
        )
    ).all()
    by_id = {r.id: r for r in rows}
    docs = []
    for doc_id in document_ids:
        if (r := by_id.get(doc_id)) is None:
            continue
        content = r.content
        if content is not None and estimate_tokens(content) > inline_max_tokens:
            content = None
        docs.append(
            AttachedDocument(
                id=str(r.id),
                filename=r.filename,
                kind=r.kind,
                page_count=r.page_count,
                char_count=r.chars or 0,
                content=content,
            )
        )
    return docs


async def read_chunk(
    document_id: str, conversation_id: str, offset: int, max_chars: int
) -> tuple[AttachedDocument, str, int] | None:
    """대화에 붙은 문서 본문의 일부를 읽는다. 본문 전체를 불러오지 않고 필요한 부분만 읽는다.

    Args:
        document_id: 문서 ID (문자열, 모델이 넘긴 값).
        conversation_id: 지금 대화 ID. 다른 대화의 문서는 읽을 수 없다.
        offset: 읽을 첫 글자 위치 (0부터).
        max_chars: 읽을 최대 글자 수.

    Returns:
        ``(문서 정보, 읽은 본문, 전체 글자 수)``. 이 대화에 없는 문서이거나 준비되지 않았으면 ``None``.
    """
    try:
        doc_uuid, conv_uuid = uuid.UUID(document_id.strip()), uuid.UUID(conversation_id)
    except (ValueError, AttributeError):
        return None
    async with SessionLocal() as session:
        row = (
            await session.execute(
                select(
                    Document.id,
                    Document.filename,
                    Document.kind,
                    Document.page_count,
                    func.char_length(Document.content).label("chars"),
                    func.substr(Document.content, max(offset, 0) + 1, max_chars).label("chunk"),
                ).where(
                    Document.id == doc_uuid,
                    Document.conversation_id == conv_uuid,
                    Document.status == "ready",
                )
            )
        ).first()
    if row is None:
        return None
    doc = AttachedDocument(
        id=str(row.id),
        filename=row.filename,
        kind=row.kind,
        page_count=row.page_count,
        char_count=row.chars or 0,
    )
    return doc, row.chunk or "", row.chars or 0


async def documents_by_message(
    session: AsyncSession, conversation_id: uuid.UUID
) -> dict[uuid.UUID, list[tuple[Document, int | None]]]:
    """대화의 첨부 문서를 메시지별로 묶는다 (화면에 파일 칩을 그릴 때). 본문은 읽지 않는다.

    Args:
        session: DB 세션.
        conversation_id: 대화 ID.

    Returns:
        메시지 ID → ``(문서, 본문 글자 수)`` 목록 (올린 순서).
    """
    rows = (
        await session.execute(
            select(Document, func.char_length(Document.content))
            .options(defer(Document.content))
            .where(Document.conversation_id == conversation_id)
            .order_by(Document.created_at)
        )
    ).all()
    grouped: dict[uuid.UUID, list[tuple[Document, int | None]]] = {}
    for doc, chars in rows:
        if doc.message_id is not None:
            grouped.setdefault(doc.message_id, []).append((doc, chars))
    return grouped
