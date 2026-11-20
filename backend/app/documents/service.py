"""문서 처리: 올리기, 백그라운드 파싱, 정리, 대화 첨부와 읽기.

흐름: 올리기 → 크기·형식 검사 → 원본을 첨부 저장소에 저장 → DB 에 ``processing`` 으로 기록 →
백그라운드에서 별도 프로세스로 파싱 → ``ready`` / ``failed`` 로 갱신.

대화 첨부 문서와 프로젝트 문서(``project_id`` 가 있음)가 같은 흐름을 쓴다. 프로젝트 문서는 보관 기간
정리를 받지 않고, 메시지에 붙일 수 없으며, 파싱이 끝날 때 프로젝트 용량을 확인한다.
"""

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.core.config import Settings, get_settings
from app.core.db import SessionLocal
from app.core.storage import DOCUMENTS_PREFIX, PROJECTS_PREFIX, ObjectStorage, get_storage
from app.documents import chunking, keywords
from app.documents import elements as el
from app.documents.formats import DocumentKind, detect, safe_filename
from app.documents.parser import ParsedDocument, ParseError
from app.documents.reading import AttachedDocument, estimate_tokens, max_inline_chars
from app.documents.worker import parse_in_subprocess
from app.llm import embeddings, vision
from app.models import Document, DocumentChunk, DocumentElement

logger = logging.getLogger(__name__)

INTERRUPTED_ERROR = "서버가 다시 시작되어 처리가 중단되었습니다. 다시 올려 주세요."
BUSY_STATUSES = ("processing", "analyzing", "indexing")
"""아직 처리 중인 문서 상태 (파싱 중, 그림 설명 중, 검색 준비 중)."""
PROJECT_FULL_ERROR = (
    "프로젝트 용량을 넘어서 추가할 수 없습니다. 다른 문서를 지운 뒤 다시 올려 주세요."
)
CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60

_tasks: set[asyncio.Task] = set()
"""실행 중인 파싱 작업. 참조를 들고 있지 않으면 작업이 중간에 사라질 수 있다."""
_parse_slots: asyncio.Semaphore | None = None


def storage_key(document_id: uuid.UUID, project_id: uuid.UUID | None = None) -> str:
    """문서 원본의 객체 키.

    대화 첨부는 ``documents/`` 아래에 두어 보관 기간 lifecycle 을 받고, 프로젝트 문서는
    ``projects/`` 아래에 두어 받지 않는다.

    Args:
        document_id: 문서 ID.
        project_id: 프로젝트 문서면 프로젝트 ID.

    Returns:
        ``"documents/<문서 ID>"`` 또는 ``"projects/<프로젝트 ID>/documents/<문서 ID>"``.
    """
    if project_id is not None:
        return f"{PROJECTS_PREFIX}{project_id}/documents/{document_id}"
    return f"{DOCUMENTS_PREFIX}{document_id}"


async def create_document(
    session: AsyncSession,
    storage: ObjectStorage,
    data: bytes,
    filename: str | None,
    user_id: uuid.UUID,
    project_id: uuid.UUID | None = None,
) -> Document:
    """문서를 검사하고 원본을 저장한 뒤 ``processing`` 상태로 기록한다.

    파싱은 하지 않는다. 기록한 뒤 ``schedule_parse`` 로 시작한다.

    Args:
        session: DB 세션. 이 함수가 commit 한다.
        storage: 첨부 저장소.
        data: 파일 내용. 크기 제한은 호출하는 쪽에서 먼저 확인한다.
        filename: 사용자가 올린 파일 이름.
        user_id: 올린 사용자.
        project_id: 프로젝트 문서면 프로젝트 ID (호출하는 쪽에서 내 프로젝트인지 확인한다).

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
        project_id=project_id,
        filename=name,
        kind=fmt.kind.value,
        mime_type=fmt.mime_type,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        status="processing",
    )
    doc.storage_key = storage_key(doc.id, project_id)
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
    async with SessionLocal() as session:
        project_id = await session.scalar(
            select(Document.project_id).where(Document.id == document_id)
        )
    values: dict = {"parsed_at": datetime.now(UTC)}
    try:
        async with _slots(settings):
            result = await parse_in_subprocess(
                data,
                DocumentKind(kind),
                filename,
                max_pages=settings.document_max_pages,
                timeout=settings.document_parse_timeout_seconds,
                # 프로젝트 문서는 검색용으로 요소(본문·표·그림)까지 만든다.
                structured=project_id is not None,
            )
    except ParseError as exc:
        values |= {"status": "failed", "error": str(exc)[:500]}
        logger.info("document %s failed to parse: %s", document_id, exc)
    except Exception:
        logger.exception("document %s parse task failed", document_id)
        values |= {"status": "failed", "error": "문서를 처리하다가 오류가 났습니다."}
    else:
        logger.info(
            "document %s parsed (%s pages, %d chars, %s elements)",
            document_id,
            result.page_count,
            len(result.content),
            len(result.elements) if result.elements is not None else "no",
        )
        if project_id is not None:
            await _ingest_project_document(document_id, project_id, filename, result)
            return
        values |= {
            "status": "ready",
            "content": result.content,
            "page_count": result.page_count,
            "token_count": estimate_tokens(result.content),
        }
    async with SessionLocal() as session:
        # 파싱하는 동안 문서가 지워졌으면 아무것도 갱신되지 않는다.
        await session.execute(
            update(Document)
            .where(Document.id == document_id, Document.status == "processing")
            .values(**values)
        )
        await session.commit()


# --- 프로젝트 문서 적재: 요소 저장 → 그림 설명 → 용량 확인 ------------------------------------------


def element_image_key(project_id: uuid.UUID, document_id: uuid.UUID, position: int) -> str:
    """프로젝트 문서 그림 원본의 객체 키 (보관 기간 규칙을 받지 않는 ``projects/`` 아래).

    Args:
        project_id: 프로젝트 ID.
        document_id: 문서 ID.
        position: 문서 안의 요소 순서.

    Returns:
        ``"projects/<프로젝트>/images/<문서>/<순서>"``.
    """
    return f"{PROJECTS_PREFIX}{project_id}/images/{document_id}/{position}"


async def _ingest_project_document(
    document_id: uuid.UUID, project_id: uuid.UUID, filename: str, result: ParsedDocument
) -> None:
    """파싱한 프로젝트 문서를 적재한다.

    1. 그림 원본을 저장소에 올리고 요소를 저장한다. 그림이 있으면 문서를 ``analyzing`` 으로 바꾼다.
    2. 그림을 vision 모델로 설명한다. 실패한 그림은 그림 속 OCR 글자로 대신한다 (문서는 실패하지 않는다).
    3. 본문과 그림 설명의 토큰 수로 프로젝트 용량을 확인한다 (넘으면 ``failed``).
    4. 요소로 검색용 청크를 만들고 임베딩해서 저장한 뒤 ``ready`` 로 바꾼다 (``index_document``).

    도중에 문서가 지워지면 올린 그림을 지우고 멈춘다.
    """
    settings = get_settings()
    storage = get_storage()
    elements = el.limit_images(result.elements or [], settings.vision_max_images_per_document)
    if storage is None:  # 프로젝트 문서는 저장소가 있어야 올릴 수 있지만, 그 사이 끊겼을 수 있다
        elements = [e for e in elements if e.kind != "image"]
    # ORM 객체는 commit 뒤에 만료되므로, 뒤에서 쓸 값(ID, 객체 키)은 따로 들고 있는다.
    plan: list[tuple[uuid.UUID, el.Element, str | None]] = []
    try:
        for position, element in enumerate(elements):
            key = None
            if element.image is not None and storage is not None:
                key = element_image_key(project_id, document_id, position)
                await storage.put(key, element.image.data, element.image.mime_type)
            plan.append((uuid.uuid4(), element, key))
        rows = [
            DocumentElement(
                id=element_id,
                document_id=document_id,
                position=position,
                kind=element.kind,
                page=element.page,
                heading=element.heading[:500] if element.heading else None,
                content=element.content,
                image_key=key,
                image_mime=element.image.mime_type if key and element.image else None,
                image_status="pending" if key else None,
                token_count=estimate_tokens(element.content),
            )
            for position, (element_id, element, key) in enumerate(plan)
        ]
        images = [(element_id, element) for element_id, element, key in plan if key]
        async with SessionLocal() as session:
            res = await session.execute(
                update(Document)
                .where(Document.id == document_id, Document.status == "processing")
                .values(
                    status="analyzing" if images else "processing",
                    content=result.content,
                    page_count=result.page_count,
                    image_count=len(images),
                )
            )
            if not res.rowcount:  # 파싱하는 동안 지워졌다
                await session.rollback()
                await delete_objects([k for _, _, k in plan if k])
                return
            session.add_all(rows)
            await session.commit()
    except Exception:
        logger.exception("failed to store elements of document %s", document_id)
        await delete_objects([k for _, _, k in plan if k])
        await _mark_failed(document_id, "문서를 처리하다가 오류가 났습니다.")
        return

    described = await _describe_images(filename, images)
    await _finish_project_document(
        document_id,
        project_id,
        [element_id for element_id, _ in images],
        [k for _, _, k in plan if k],
        len(plan),
        described,
    )


async def _describe_images(
    filename: str, images: list[tuple[uuid.UUID, el.Element]]
) -> dict[uuid.UUID, str]:
    """그림들을 vision 모델로 설명한다. 실패한 그림은 결과에 없다.

    Args:
        filename: 문서 이름 (설명의 맥락).
        images: ``(요소 ID, 그림 요소)`` 목록.

    Returns:
        요소 ID → 설명.
    """
    if not images:
        return {}
    settings = get_settings()
    try:
        model = vision.build_vision_model(settings)
    except vision.VisionUnavailableError as exc:
        logger.warning("image descriptions skipped: %s", exc)
        return {}
    slots = asyncio.Semaphore(max(1, settings.vision_concurrency))

    async def describe(element_id: uuid.UUID, element: el.Element) -> tuple[uuid.UUID, str]:
        assert element.image is not None
        context = "\n".join(
            line
            for line in (
                f"Document: {filename}",
                f"Section: {element.heading}" if element.heading else "",
                f"Page: {element.page}" if element.page else "",
                # 작은 라벨·숫자(예: 막대 끝의 "-6")는 낮은 해상도로 보면 놓치기 쉬워서 OCR 글자를 함께 준다.
                f"OCR text of this image (line order does not follow the layout): "
                f"{' / '.join(line for line in element.image.ocr_text.splitlines() if line.strip())}"
                if element.image.ocr_text
                else "",
            )
            if line
        )
        async with slots:
            try:
                text = await vision.describe_image(
                    model, element.image.data, element.image.mime_type, context
                )
            except Exception:
                logger.warning("failed to describe image %s", element_id, exc_info=True)
                text = ""
        return element_id, text

    results = await asyncio.gather(*(describe(i, e) for i, e in images))
    return {element_id: text for element_id, text in results if text}


async def _finish_project_document(
    document_id: uuid.UUID,
    project_id: uuid.UUID,
    image_ids: list[uuid.UUID],
    image_keys: list[str],
    element_count: int,
    described: dict[uuid.UUID, str],
) -> None:
    """그림 설명을 저장하고, 요소로 본문을 다시 만들고, 용량을 확인한 뒤 검색 준비로 넘긴다.

    본문(``documents.content``)은 파서가 만든 것 대신 요소(본문·표·그림 설명)로 다시 만든다
    (``elements.compose_content``). 그래야 문서 읽기(``read_document``), 작은 프로젝트의 본문 넣기, 검색이
    모두 같은 내용을 본다. 용량을 넘으면 ``failed`` 로 바꾸고 요소와 그림 원본을 지운다.
    """
    settings = get_settings()
    async with SessionLocal() as session:
        content = await _compose_document_content(session, document_id, described)
        total = estimate_tokens(content)
        used = await project_usage(session, project_id, exclude=document_id)
        if used + total > settings.project_max_tokens:
            await session.execute(
                delete(DocumentElement).where(DocumentElement.document_id == document_id)
            )
            await session.execute(
                update(Document)
                .where(Document.id == document_id, Document.status.in_(BUSY_STATUSES))
                .values(status="failed", error=PROJECT_FULL_ERROR, parsed_at=datetime.now(UTC))
            )
            await session.commit()
            await delete_objects(image_keys)
            return
        for element_id in image_ids:
            text = described.get(element_id)
            values: dict[str, Any] = (
                {"content": text, "image_status": "described", "token_count": estimate_tokens(text)}
                if text
                else {"image_status": "fallback"}
            )
            await session.execute(
                update(DocumentElement).where(DocumentElement.id == element_id).values(**values)
            )
        res = await session.execute(
            update(Document)
            .where(Document.id == document_id, Document.status.in_(BUSY_STATUSES))
            .values(
                status="indexing",
                content=content,
                token_count=total,
                image_fallback_count=len(image_ids) - len(described),
            )
        )
        await session.commit()
    if not res.rowcount:  # 설명하는 동안 지워졌다 (요소는 함께 지워졌고 그림 원본만 남았다)
        await delete_objects(image_keys)
        return
    logger.info(
        "project document %s analyzed (%d elements, %d/%d images described, %d tokens)",
        document_id,
        element_count,
        len(described),
        len(image_ids),
        total,
    )
    await index_document(document_id)


async def _compose_document_content(
    session: AsyncSession, document_id: uuid.UUID, described: dict[uuid.UUID, str]
) -> str:
    """저장한 요소와 새로 만든 그림 설명으로 문서 본문을 만든다 (``elements.compose_content``)."""
    kind = await session.scalar(select(Document.kind).where(Document.id == document_id))
    rows = await session.execute(
        select(
            DocumentElement.id,
            DocumentElement.kind,
            DocumentElement.content,
            DocumentElement.page,
            DocumentElement.heading,
        )
        .where(DocumentElement.document_id == document_id)
        .order_by(DocumentElement.position)
    )
    parts = [
        el.ContentPart(
            r.kind,
            described.get(r.id, r.content),
            r.page,
            r.heading,
            described=r.id in described,
        )
        for r in rows
    ]
    # PDF 는 표 내용이 본문에 행 단위로 이미 들어 있다.
    return el.compose_content(parts, skip_tables=kind == DocumentKind.PDF)


# --- 검색 준비: 요소 → 청크 → 임베딩 ----------------------------------------------------------------


async def index_document(document_id: uuid.UUID) -> None:
    """``indexing`` 상태인 프로젝트 문서의 청크를 만들어 저장하고 ``ready`` 로 바꾼다.

    임베딩에 실패하면(키 없음, API 오류) 청크만 임베딩 없이 저장하고 문서는 준비됨으로 둔다.
    빠진 임베딩은 검색할 때 다시 만든다 (``app.projects.search``). 도중에 문서가 지워지면 아무것도 남기지 않는다.

    Args:
        document_id: 문서 ID.
    """
    try:
        async with SessionLocal() as session:
            doc = (
                await session.execute(
                    select(Document.project_id, Document.filename, Document.content).where(
                        Document.id == document_id, Document.status == "indexing"
                    )
                )
            ).first()
            if doc is None or doc.project_id is None:
                return
            chunks = await _document_chunks(session, document_id, doc.content)
        rows = await _chunk_rows(document_id, doc.project_id, doc.filename, chunks)
        async with SessionLocal() as session:
            res = await session.execute(
                update(Document)
                .where(Document.id == document_id, Document.status == "indexing")
                .values(status="ready", parsed_at=datetime.now(UTC))
            )
            if not res.rowcount:  # 그 사이 지워졌다
                await session.rollback()
                return
            session.add_all(rows)
            await session.commit()
    except Exception:
        logger.exception("failed to index document %s", document_id)
        await _mark_failed(document_id, "문서를 처리하다가 오류가 났습니다.")
        return
    logger.info(
        "project document %s ready (%d chunks, %d embedded)",
        document_id,
        len(rows),
        sum(1 for r in rows if r.embedding is not None),
    )


async def _document_chunks(
    session: AsyncSession, document_id: uuid.UUID, content: str | None
) -> list[chunking.Chunk]:
    """문서의 요소로 청크를 만든다. 요소가 없는 문서(구조화 적재 전에 올린 것)는 본문으로 만든다."""
    rows = await session.execute(
        select(
            DocumentElement.id,
            DocumentElement.kind,
            DocumentElement.content,
            DocumentElement.page,
            DocumentElement.heading,
        )
        .where(DocumentElement.document_id == document_id)
        .order_by(DocumentElement.position)
    )
    sources = [chunking.SourceElement(r.id, r.kind, r.content, r.page, r.heading) for r in rows]
    if not sources and content:
        sources = [chunking.SourceElement(uuid.uuid4(), "text", content)]
    settings = get_settings()
    return chunking.make_chunks(
        sources,
        chunking.ChunkSizes(
            settings.chunk_target_tokens, settings.chunk_max_tokens, settings.chunk_overlap_tokens
        ),
    )


async def _chunk_rows(
    document_id: uuid.UUID, project_id: uuid.UUID, filename: str, chunks: list[chunking.Chunk]
) -> list[DocumentChunk]:
    """청크를 임베딩해서 저장할 행으로 만든다 (키워드 검색 토큰 포함). 임베딩에 실패하면 임베딩 없이 만든다."""
    settings = get_settings()
    texts = [chunking.embedding_text(filename, c) for c in chunks]
    vectors: list[list[float]] | None = None
    if chunks:
        try:
            model = embeddings.build_embeddings(settings)
            vectors = await model.aembed_documents(texts)
        except embeddings.EmbeddingUnavailableError as exc:
            logger.warning("embedding skipped for document %s: %s", document_id, exc)
        except Exception:
            # 빠진 임베딩은 검색할 때 다시 만든다.
            logger.warning("embedding failed for document %s", document_id, exc_info=True)
    model_name = embeddings.model_id(settings) if vectors is not None else None
    return [
        DocumentChunk(
            document_id=document_id,
            project_id=project_id,
            position=position,
            kind=chunk.kind,
            page_start=chunk.page_start,
            page_end=chunk.page_end,
            heading=chunk.heading[:500] if chunk.heading else None,
            content=chunk.content,
            image_element_id=chunk.image_element_id,
            token_count=chunk.token_count,
            embedding=vectors[position] if vectors is not None else None,
            embedding_model=model_name,
            search_vector=func.to_tsvector("simple", keywords.keyword_document(texts[position])),
        )
        for position, chunk in enumerate(chunks)
    ]


async def index_missing_documents() -> int:
    """청크가 없는 준비된 프로젝트 문서(검색 기능 전에 올린 것)의 청크를 만든다. 앱이 시작할 때 부른다.

    Returns:
        청크를 만든 문서 수.
    """
    async with SessionLocal() as session:
        has_chunks = select(DocumentChunk.id).where(DocumentChunk.document_id == Document.id)
        ids = list(
            await session.scalars(
                select(Document.id).where(
                    Document.project_id.is_not(None),
                    Document.status == "ready",
                    ~has_chunks.exists(),
                )
            )
        )
    count = 0
    for document_id in ids:
        async with SessionLocal() as session:
            res = await session.execute(
                update(Document)
                .where(Document.id == document_id, Document.status == "ready")
                .values(status="indexing")
            )
            await session.commit()
        if res.rowcount:
            await index_document(document_id)
            count += 1
    return count


async def _mark_failed(document_id: uuid.UUID, error: str) -> None:
    async with SessionLocal() as session:
        await session.execute(
            update(Document)
            .where(Document.id == document_id, Document.status.in_(BUSY_STATUSES))
            .values(status="failed", error=error, parsed_at=datetime.now(UTC))
        )
        await session.commit()


async def element_image_keys(session: AsyncSession, document_filter: Any) -> list[str]:
    """조건에 맞는 문서들의 그림 원본 객체 키. 문서를 지우기 전에 원본을 함께 지우려고 쓴다.

    Args:
        session: DB 세션.
        document_filter: ``Document`` 에 대한 조건 (예: ``Document.project_id == pid``).

    Returns:
        객체 키 목록.
    """
    rows = await session.scalars(
        select(DocumentElement.image_key)
        .join(Document, Document.id == DocumentElement.document_id)
        .where(document_filter, DocumentElement.image_key.is_not(None))
    )
    return [k for k in rows if k]


async def project_usage(
    session: AsyncSession, project_id: uuid.UUID, exclude: uuid.UUID | None = None
) -> int:
    """프로젝트가 쓰고 있는 용량: 준비된 문서의 추정 토큰 수 합계.

    Args:
        session: DB 세션.
        project_id: 프로젝트 ID.
        exclude: 합계에서 뺄 문서 ID (방금 파싱한 문서).

    Returns:
        토큰 수 합계.
    """
    query = select(func.coalesce(func.sum(Document.token_count), 0)).where(
        Document.project_id == project_id, Document.status == "ready"
    )
    if exclude is not None:
        query = query.where(Document.id != exclude)
    return int(await session.scalar(query) or 0)


async def mark_interrupted() -> int:
    """앞선 실행에서 파싱 중이던 문서를 실패로 표시한다. 앱이 시작할 때 부른다.

    Returns:
        실패로 바꾼 문서 수.
    """
    async with SessionLocal() as session:
        res = await session.execute(
            update(Document)
            .where(Document.status.in_(BUSY_STATUSES))
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

    DB 기록(파싱 결과 포함)을 지우고 원본도 저장소에서 지운다. 프로젝트 문서는 지우지 않는다.

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
            delete(Document)
            .where(Document.project_id.is_(None), or_(expired, unsent))
            .returning(Document.storage_key)
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
    """문서들을 메시지에 붙일 수 있는지 확인한다 (내가 올렸고, 아직 보내지 않았고, 준비된 첨부 문서).

    프로젝트 문서는 메시지에 붙이지 않는다 (없는 문서로 본다).

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
                Document.id.in_(document_ids),
                Document.user_id == user_id,
                Document.project_id.is_(None),
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
        if row.status in BUSY_STATUSES:
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
            Document.project_id.is_(None),
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
    document_id: str,
    conversation_id: str,
    offset: int,
    max_chars: int,
    project_id: uuid.UUID | None = None,
) -> tuple[AttachedDocument, str, int] | None:
    """대화에 붙은 문서나 프로젝트 문서의 본문 일부를 읽는다. 본문 전체를 불러오지 않고 필요한 부분만 읽는다.

    Args:
        document_id: 문서 ID (문자열, 모델이 넘긴 값).
        conversation_id: 지금 대화 ID. 다른 대화의 첨부 문서는 읽을 수 없다.
        offset: 읽을 첫 글자 위치 (0부터).
        max_chars: 읽을 최대 글자 수.
        project_id: 지금 대화가 속한 프로젝트 ID (서버가 정한 값). 있으면 그 프로젝트의 문서도 읽는다.

    Returns:
        ``(문서 정보, 읽은 본문, 전체 글자 수)``. 읽을 수 있는 문서가 아니거나 준비되지 않았으면 ``None``.
    """
    try:
        doc_uuid, conv_uuid = uuid.UUID(document_id.strip()), uuid.UUID(conversation_id)
    except (ValueError, AttributeError):
        return None
    readable = Document.conversation_id == conv_uuid
    if project_id is not None:
        readable = or_(readable, Document.project_id == project_id)
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
                    readable,
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
