"""프로젝트 서비스: 만들기·고치기·조회, 프로젝트 문서 올리기·지우기, 에이전트에게 줄 프로젝트 정보.

HTTP 를 모른다. 사용자에게 보여줄 실패는 ``ProjectError`` (상태 코드 포함)로 던진다. 프로젝트 삭제는
대화 기록(checkpoint)도 지워야 해서 대화 서비스(``app.conversations.service.delete_project``)가 맡는다.
"""

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.core.config import get_settings
from app.core.storage import ObjectNotFoundError, ObjectStorage
from app.documents import elements as el
from app.documents import service as documents
from app.documents.formats import DocumentKind
from app.documents.reading import AttachedDocument
from app.models import Conversation, Document, DocumentChunk, DocumentElement, Project


class ProjectError(Exception):
    """프로젝트 요청을 처리할 수 없다. 메시지는 사용자에게 그대로 보여준다.

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


async def get_own(session: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID) -> Project:
    """내 프로젝트를 읽는다. 없거나 다른 사람의 프로젝트면 404 (존재 여부도 숨긴다).

    Args:
        session: DB 세션.
        project_id: 프로젝트 ID.
        user_id: 요청한 사용자 ID.

    Returns:
        프로젝트.

    Raises:
        ProjectError: 프로젝트가 없거나 내 것이 아니면 404.
    """
    project = await session.get(Project, project_id)
    if project is None or project.user_id != user_id:
        raise ProjectError(404, "Project not found")
    return project


async def list_projects(session: AsyncSession, user_id: uuid.UUID) -> list[Project]:
    """내 프로젝트 목록을 최근에 바꾼 순으로 돌려준다.

    Args:
        session: DB 세션.
        user_id: 사용자 ID.

    Returns:
        프로젝트 목록.
    """
    result = await session.scalars(
        select(Project).where(Project.user_id == user_id).order_by(Project.updated_at.desc())
    )
    return list(result.all())


async def create(
    session: AsyncSession, user_id: uuid.UUID, name: str, instructions: str
) -> Project:
    """프로젝트를 만든다.

    Args:
        session: DB 세션. 이 함수가 commit 한다.
        user_id: 주인이 될 사용자 ID.
        name: 이름 (앞뒤 공백을 뺀다).
        instructions: 지시문.

    Returns:
        만든 프로젝트.
    """
    project = Project(user_id=user_id, name=name.strip(), instructions=instructions.strip())
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


async def update(
    session: AsyncSession,
    project: Project,
    *,
    name: str | None = None,
    instructions: str | None = None,
) -> Project:
    """프로젝트 이름이나 지시문을 바꾼다. 지시문은 다음 응답부터 적용된다.

    Args:
        session: DB 세션. 이 함수가 commit 한다.
        project: 바꿀 프로젝트 (``get_own`` 으로 확인한 것).
        name: 새 이름. ``None`` 이면 그대로 둔다.
        instructions: 새 지시문. ``None`` 이면 그대로 둔다.

    Returns:
        바꾼 프로젝트.
    """
    if name is not None:
        project.name = name.strip()
    if instructions is not None:
        project.instructions = instructions.strip()
    await session.commit()
    await session.refresh(project)
    return project


async def list_documents(
    session: AsyncSession, project_id: uuid.UUID
) -> list[tuple[Document, int | None]]:
    """프로젝트 문서와 본문 글자 수를 올린 순으로 돌려준다. 본문은 읽지 않는다.

    Args:
        session: DB 세션.
        project_id: 프로젝트 ID.

    Returns:
        ``(문서, 본문 글자 수)`` 목록.
    """
    rows = await session.execute(
        select(Document, func.char_length(Document.content))
        .options(defer(Document.content))
        .where(Document.project_id == project_id)
        .order_by(Document.created_at)
    )
    return [(doc, chars) for doc, chars in rows.all()]


async def list_conversations(session: AsyncSession, project_id: uuid.UUID) -> list[Conversation]:
    """프로젝트 안의 대화를 최신순으로 돌려준다.

    Args:
        session: DB 세션.
        project_id: 프로젝트 ID.

    Returns:
        대화 목록.
    """
    result = await session.scalars(
        select(Conversation)
        .where(Conversation.project_id == project_id)
        .order_by(Conversation.created_at.desc())
    )
    return list(result.all())


async def usage(session: AsyncSession, project_id: uuid.UUID) -> int:
    """프로젝트가 쓰고 있는 용량 (준비된 문서의 추정 토큰 수 합계).

    Args:
        session: DB 세션.
        project_id: 프로젝트 ID.

    Returns:
        토큰 수 합계.
    """
    return await documents.project_usage(session, project_id)


async def add_document(
    session: AsyncSession,
    storage: ObjectStorage,
    project: Project,
    data: bytes,
    filename: str | None,
) -> Document:
    """프로젝트에 문서를 올린다. 파싱은 호출하는 쪽에서 ``documents.schedule_parse`` 로 시작한다.

    문서의 분량은 파싱해야 알 수 있으므로, 여기서는 이미 꽉 찬 프로젝트만 거절한다. 파싱이 끝났을 때
    용량을 넘으면 그 문서는 실패로 표시된다.

    Args:
        session: DB 세션. 이 함수가 commit 한다.
        storage: 첨부 저장소.
        project: 문서를 넣을 프로젝트 (``get_own`` 으로 확인한 것).
        data: 파일 내용. 크기 제한은 호출하는 쪽에서 먼저 확인한다.
        filename: 사용자가 올린 파일 이름.

    Returns:
        ``processing`` 상태로 기록한 문서.

    Raises:
        ProjectError: 프로젝트 용량이 이미 꽉 찼으면 409.
        UnsupportedDocumentError: 빈 파일, 지원하지 않는 형식, 손상된 파일일 때.
    """
    if await usage(session, project.id) >= get_settings().project_max_tokens:
        raise ProjectError(409, documents.PROJECT_FULL_ERROR)
    return await documents.create_document(
        session, storage, data, filename, project.user_id, project_id=project.id
    )


async def delete_document(session: AsyncSession, project: Project, document_id: uuid.UUID) -> None:
    """프로젝트 문서를 지운다 (DB 기록과 원본, 그림 원본).

    Args:
        session: DB 세션. 이 함수가 commit 한다.
        project: 프로젝트 (``get_own`` 으로 확인한 것).
        document_id: 문서 ID.

    Raises:
        ProjectError: 이 프로젝트의 문서가 아니면 404.
    """
    doc = await session.get(Document, document_id, options=[defer(Document.content)])
    if doc is None or doc.project_id != project.id:
        raise ProjectError(404, "Document not found")
    keys = [doc.storage_key, *await documents.element_image_keys(session, Document.id == doc.id)]
    await session.delete(doc)
    await session.commit()
    await documents.delete_objects(keys)


async def reprocess_document(
    session: AsyncSession, storage: ObjectStorage, project: Project, document_id: uuid.UUID
) -> tuple[Document, bytes]:
    """프로젝트 문서를 처음부터 다시 처리할 준비를 한다 (파서를 고친 뒤 다시 적재할 때).

    저장해 둔 원본을 읽고, 이전 처리 결과(요소, 청크, 그림 원본, 본문)를 지운 뒤 ``processing`` 으로 되돌린다.
    실제 파싱은 호출하는 쪽이 ``documents.schedule_parse`` 로 시작한다 (처음 올릴 때와 같은 흐름).

    Args:
        session: DB 세션. 이 함수가 commit 한다.
        storage: 첨부 저장소.
        project: 프로젝트 (``get_own`` 으로 확인한 것).
        document_id: 문서 ID.

    Returns:
        ``(processing 상태의 문서, 원본 바이트)``.

    Raises:
        ProjectError: 이 프로젝트의 문서가 아니거나 원본이 없으면 404, 처리 중인 문서면 409.
    """
    doc = await session.get(Document, document_id, options=[defer(Document.content)])
    if doc is None or doc.project_id != project.id:
        raise ProjectError(404, "Document not found")
    if doc.status in documents.BUSY_STATUSES:
        raise ProjectError(409, "처리 중인 문서는 끝난 뒤에 다시 처리할 수 있습니다.")
    try:
        data = await storage.get(doc.storage_key)
    except ObjectNotFoundError as exc:
        raise ProjectError(404, "원본 파일이 없어 다시 처리할 수 없습니다.") from exc
    keys = await documents.element_image_keys(session, Document.id == doc.id)
    await session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc.id))
    await session.execute(delete(DocumentElement).where(DocumentElement.document_id == doc.id))
    await session.execute(
        sql_update(Document)
        .where(Document.id == doc.id)
        .values(
            status="processing",
            error=None,
            content=None,
            page_count=None,
            token_count=None,
            image_count=None,
            image_fallback_count=None,
            parsed_at=None,
        )
    )
    await session.commit()
    await session.refresh(doc)
    # 새 그림은 같은 객체 키(문서 안의 순서)로 다시 올라오므로, 파싱을 시작하기 전에 지운다.
    await documents.delete_objects(keys)
    return doc, data


async def read_element_image(
    session: AsyncSession, storage: ObjectStorage, project: Project, element_id: uuid.UUID
) -> tuple[bytes, str]:
    """프로젝트 문서 속 그림 원본을 읽는다 (검색 결과 출처 카드에 보여줄 때).

    Args:
        session: DB 세션.
        storage: 첨부 저장소.
        project: 프로젝트 (``get_own`` 으로 확인한 것).
        element_id: 그림 요소 ID.

    Returns:
        ``(그림 바이트, MIME 타입)``.

    Raises:
        ProjectError: 이 프로젝트의 그림이 아니거나 원본이 없으면 404.
    """
    row = (
        await session.execute(
            select(DocumentElement.image_key, DocumentElement.image_mime)
            .join(Document, Document.id == DocumentElement.document_id)
            .where(DocumentElement.id == element_id, Document.project_id == project.id)
        )
    ).first()
    if row is None or not row.image_key:
        raise ProjectError(404, "Image not found")
    try:
        data = await storage.get(row.image_key)
    except ObjectNotFoundError as exc:
        raise ProjectError(404, "Image not found") from exc
    return data, row.image_mime or "application/octet-stream"


@dataclass(frozen=True)
class ViewBlock:
    """문서 보기 패널의 블록 하나 (저장한 요소 하나).

    Attributes:
        id: 요소 ID.
        kind: ``text`` | ``table`` | ``image``.
        page: 쪽 번호.
        heading: 제목 경로.
        content: 본문, 표 Markdown, 그림 설명 또는 그림 속 OCR 글자.
        described: 그림 설명을 만들었는지.
        has_image: 그림 원본이 저장돼 있는지.
    """

    id: uuid.UUID
    kind: str
    page: int | None
    heading: str | None
    content: str
    described: bool
    has_image: bool


@dataclass(frozen=True)
class ViewHighlight:
    """문서 보기 패널에서 강조할 근거 위치.

    Attributes:
        block_ids: 강조할 블록 ID. 찾지 못하면 빈 목록 (그때는 ``page`` 로 스크롤한다).
        text: 블록 안에서 한 번 더 표시할 청크 본문 (긴 본문을 나눈 조각일 때만).
        page: 청크의 첫 쪽 번호.
    """

    block_ids: list[uuid.UUID]
    text: str | None
    page: int | None


@dataclass(frozen=True)
class DocumentViewData:
    """프로젝트 문서의 추출 내용.

    Attributes:
        document: 문서.
        blocks: 문서 순서대로의 블록.
        highlight: 강조할 위치. 청크를 주지 않았거나 이 문서의 청크가 아니면 ``None``.
    """

    document: Document
    blocks: list[ViewBlock]
    highlight: ViewHighlight | None


async def document_view(
    session: AsyncSession,
    project: Project,
    document_id: uuid.UUID,
    chunk_id: uuid.UUID | None = None,
) -> DocumentViewData:
    """프로젝트 문서의 추출 내용을 블록으로 돌려준다 (문서 보기 패널).

    문서 본문(``elements.compose_content``)과 같은 규칙으로 블록을 고른다. 그래서 패널에 보이는 내용이
    모델이 읽은 내용과 같다. PDF 는 표 내용이 본문에 이미 있어서 표 블록을 뺀다.

    Args:
        session: DB 세션.
        project: 프로젝트 (``get_own`` 으로 확인한 것).
        document_id: 문서 ID.
        chunk_id: 강조할 검색 결과 청크 ID. 없으면 강조하지 않는다.

    Returns:
        문서, 블록, 강조할 위치.

    Raises:
        ProjectError: 이 프로젝트의 문서가 아니면 404.
    """
    doc = await session.scalar(
        select(Document)
        .options(defer(Document.content))
        .where(Document.id == document_id, Document.project_id == project.id)
    )
    if doc is None:
        raise ProjectError(404, "Document not found")
    skip_tables = doc.kind == DocumentKind.PDF
    rows = await session.execute(
        select(
            DocumentElement.id,
            DocumentElement.kind,
            DocumentElement.page,
            DocumentElement.heading,
            DocumentElement.content,
            DocumentElement.image_key,
            DocumentElement.image_status,
        )
        .where(DocumentElement.document_id == doc.id)
        .order_by(DocumentElement.position)
    )
    blocks = [
        ViewBlock(
            r.id,
            r.kind,
            r.page,
            r.heading,
            r.content.strip(),
            r.image_status == "described",
            bool(r.image_key),
        )
        for r in rows
        if el.is_shown(r.kind, r.content, skip_tables)
    ]
    highlight = None
    if chunk_id is not None:
        chunk = await session.scalar(
            select(DocumentChunk).where(
                DocumentChunk.id == chunk_id, DocumentChunk.document_id == doc.id
            )
        )
        if chunk is not None:
            highlight = _highlight(chunk, blocks)
    return DocumentViewData(doc, blocks, highlight)


def _squash(text: str) -> str:
    """띄어쓰기·줄바꿈을 뺀 글자 (청크를 만들 때 공백이 바뀌어도 같은 내용으로 본다)."""
    return re.sub(r"\s+", "", text)


def _highlight(chunk: DocumentChunk, blocks: list[ViewBlock]) -> ViewHighlight:
    """청크가 가리키는 블록을 찾는다.

    청크에 요소 ID 가 있으면 그것을 쓰고, 없으면(열을 추가하기 전에 만든 청크) 같은 쪽 범위에서
    내용이 겹치는 블록을 찾는다. PDF 표 청크처럼 보이지 않는 요소를 가리키면 블록 없이 쪽만 준다.
    """
    shown = {b.id: b for b in blocks}
    if chunk.element_ids:
        ids = [i for i in chunk.element_ids if i in shown]
    elif chunk.image_element_id is not None:
        ids = [chunk.image_element_id] if chunk.image_element_id in shown else []
    else:
        target = _squash(chunk.content)
        pages = (chunk.page_start, chunk.page_end or chunk.page_start)
        ids = [
            b.id
            for b in blocks
            if b.kind == chunk.kind
            and (pages[0] is None or (b.page is not None and pages[0] <= b.page <= pages[1]))
            and (body := _squash(b.content))
            and (body in target or target in body)
        ]
    text = None
    # 긴 본문을 나눈 조각이면 블록 안의 그 부분을 한 번 더 표시한다.
    if (
        chunk.kind == "text"
        and len(ids) == 1
        and _squash(shown[ids[0]].content) != _squash(chunk.content)
    ):
        text = chunk.content
    return ViewHighlight(ids, text, chunk.page_start)


@dataclass(frozen=True)
class ProjectContext:
    """에이전트에게 줄 프로젝트 정보.

    Attributes:
        id: 프로젝트 ID. ``read_document`` 가 이 프로젝트의 문서를 읽게 할 때 쓴다.
        name: 프로젝트 이름.
        instructions: 지시문.
        documents: 읽을 수 있는(준비된) 프로젝트 문서. 프로젝트가 작으면 ``content`` 에 본문이 있다.
        total_tokens: 준비된 문서의 추정 토큰 수 합계.
    """

    id: uuid.UUID
    name: str
    instructions: str
    documents: list[AttachedDocument]
    total_tokens: int = 0

    def inline_documents(self, limit: int) -> bool:
        """문서 본문을 모두 프롬프트에 넣을지: 합계가 ``limit`` 이하이고 본문을 읽어 왔을 때.

        Args:
            limit: 본문을 넣을 토큰 상한 (모델마다 다르다, ``Settings.project_inline_limit``).

        Returns:
            본문을 모두 넣으면 ``True``.
        """
        return (
            bool(self.documents)
            and self.total_tokens <= limit
            and all(d.content is not None for d in self.documents)
        )


async def load_context(
    session: AsyncSession, project_id: uuid.UUID, content_max_tokens: int = 0
) -> ProjectContext | None:
    """에이전트에게 줄 프로젝트 정보를 읽는다. 문서는 준비된 것만, 올린 순으로 담는다.

    문서 합계가 ``content_max_tokens`` 이하면 본문도 읽어 온다. 실제로 본문을 넣을지는 모델에 따라
    에이전트가 정한다 (``ProjectContext.inline_documents``).

    Args:
        session: DB 세션.
        project_id: 프로젝트 ID.
        content_max_tokens: 본문까지 읽어 올 문서 합계의 상한. 0 이면 읽지 않는다.

    Returns:
        프로젝트 정보. 프로젝트가 없으면 ``None``.
    """
    project = await session.get(Project, project_id)
    if project is None:
        return None
    ready = (Document.project_id == project_id) & (Document.status == "ready")
    # 토큰 수가 없는 문서(기록 전에 올린 것)는 크다고 보고 본문을 읽지 않는다.
    total = int(
        await session.scalar(
            select(
                func.coalesce(
                    func.sum(func.coalesce(Document.token_count, content_max_tokens + 1)), 0
                )
            ).where(ready)
        )
        or 0
    )
    with_content = 0 < total <= content_max_tokens
    columns = [
        Document.id,
        Document.filename,
        Document.kind,
        Document.page_count,
        func.char_length(Document.content).label("chars"),
    ]
    if with_content:
        columns.append(Document.content)
    rows = (
        await session.execute(select(*columns).where(ready).order_by(Document.created_at))
    ).all()
    descriptions = await _image_descriptions(session, [r.id for r in rows]) if with_content else {}
    docs = [
        AttachedDocument(
            id=str(r.id),
            filename=r.filename,
            kind=r.kind,
            page_count=r.page_count,
            char_count=r.chars or 0,
            content=_with_descriptions(r.content, descriptions.get(r.id, []))
            if with_content
            else None,
        )
        for r in rows
    ]
    return ProjectContext(
        id=project.id,
        name=project.name,
        instructions=project.instructions,
        documents=docs,
        total_tokens=total,
    )


async def _image_descriptions(
    session: AsyncSession, document_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    """문서별 그림 설명 (설명을 만든 그림만, 문서 순서). 본문에는 그림 속 글자만 있어서 따로 붙인다."""
    if not document_ids:
        return {}
    rows = await session.execute(
        select(
            DocumentElement.document_id,
            DocumentElement.page,
            DocumentElement.heading,
            DocumentElement.content,
        )
        .where(
            DocumentElement.document_id.in_(document_ids),
            DocumentElement.image_status == "described",
        )
        .order_by(DocumentElement.document_id, DocumentElement.position)
    )
    found: dict[uuid.UUID, list[str]] = {}
    for r in rows.all():
        where = ", ".join(x for x in (f"p.{r.page}" if r.page else "", r.heading or "") if x)
        found.setdefault(r.document_id, []).append(
            f"[그림{f' ({where})' if where else ''}] {r.content}"
        )
    return found


def _with_descriptions(content: str | None, descriptions: list[str]) -> str | None:
    """본문 뒤에 그림 설명을 붙인다."""
    if content is None or not descriptions:
        return content
    return f"{content.rstrip()}\n\n## 그림 설명\n\n" + "\n\n".join(descriptions)
