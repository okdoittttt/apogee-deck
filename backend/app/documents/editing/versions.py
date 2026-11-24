"""문서 버전(원본 → 수정 2 → 수정 3)의 목록, 직전 버전과 비교, 예전 버전으로 되돌리기.

화면(API)에서 쓴다. 버전 사슬은 같은 대화 안에서 ``source_document_id`` 로 이어진다. 되돌리기는 예전
버전을 지우지 않고, 그 버전의 파일을 복사한 새 수정본을 가장 새 버전 뒤에 만든다 (에이전트가 다음에 예전
버전을 고치려 하면 "더 새 버전이 있음" 안내로 새 버전을 쓰게 된다).
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.core.storage import ObjectStorage
from app.documents import service as documents
from app.documents.editing import text
from app.documents.editing.compare import Block, Unit, compare
from app.documents.editing.docx import DocxDocument
from app.documents.editing.service import EDITABLE_KINDS, latest_revision, revision_filename
from app.documents.formats import DocumentKind
from app.documents.service import storage_key
from app.models import Document

_MAX_CHAIN = 1000
"""사슬을 따라갈 최대 단계 (끝없이 이어지는 일은 없지만 안전하게 끊는다)."""


class VersionError(Exception):
    """버전 작업을 할 수 없다 (화면에 보여줄 한국어 메시지).

    Attributes:
        status: 알맞은 HTTP 상태 코드.
    """

    def __init__(self, message: str, status: int = 409) -> None:
        """오류를 만든다.

        Args:
            message: 사용자에게 보여줄 설명.
            status: HTTP 상태 코드.
        """
        super().__init__(message)
        self.status = status


async def version_chain(session: AsyncSession, doc: Document) -> list[Document]:
    """``doc`` 이 든 버전 사슬 (맨 처음 문서부터 가장 새 수정본까지).

    ``doc`` 의 조상은 ``source_document_id`` 를 거슬러 올라가 찾고, 뒤 버전은 ``latest_revision`` 과 같은
    규칙(갈래가 있으면 가장 최근 것)으로 따라간다. 대화가 없는 문서(프로젝트 문서 등)는 자기 하나다.

    Args:
        session: DB 세션.
        doc: 기준 문서.

    Returns:
        오래된 순서의 문서들 (본문 ``content`` 는 읽지 않는다).
    """
    chain = [doc]
    if doc.conversation_id is None:
        return chain
    current = doc
    for _ in range(_MAX_CHAIN):
        if current.source_document_id is None:
            break
        parent = await session.scalar(
            select(Document)
            .options(defer(Document.content))
            .where(
                Document.id == current.source_document_id,
                Document.conversation_id == doc.conversation_id,
            )
        )
        if parent is None:
            break
        chain.insert(0, parent)
        current = parent
    current = doc
    for _ in range(_MAX_CHAIN):
        child = await session.scalar(
            select(Document)
            .options(defer(Document.content))
            .where(
                Document.source_document_id == current.id,
                Document.conversation_id == doc.conversation_id,
            )
            .order_by(Document.created_at.desc())
            .limit(1)
        )
        if child is None:
            break
        chain.append(child)
        current = child
    return chain


@dataclass(frozen=True)
class Comparison:
    """직전 버전과 비교한 결과.

    Attributes:
        previous: 직전 버전 (없으면 ``None``).
        blocks: 비교 블록 (직전 버전이 없으면 빈 목록).
        unit: 비교 단위 (``paragraph`` | ``line``).
    """

    previous: Document | None
    blocks: list[Block]
    unit: str


def units_of(kind: str, data: bytes) -> tuple[list[Unit], str]:
    """원본 파일에서 비교할 단위를 뽑는다. DOCX 는 글자가 있는 문단, 텍스트 형식은 줄.

    Args:
        kind: 문서 형식.
        data: 원본 파일 내용.

    Returns:
        ``(단위들, "paragraph" 또는 "line")``.

    Raises:
        EditError: 파일을 열 수 없을 때.
    """
    if kind == DocumentKind.DOCX:
        paragraphs = DocxDocument.from_bytes(data).paragraphs
        units = [Unit(f"p{p.number}", p.text, p.location) for p in paragraphs if p.text.strip()]
        return units, "paragraph"
    decoded, _ = text.decode(data)
    lines = decoded.split("\n")
    if decoded.endswith("\n"):
        lines.pop()
    return [Unit(f"L{n}", line) for n, line in enumerate(lines, 1)], "line"


async def compare_with_previous(
    session: AsyncSession, storage: ObjectStorage, doc: Document
) -> Comparison:
    """문서를 직전 버전(``source_document_id``)과 비교한다.

    Args:
        session: DB 세션.
        storage: 첨부 저장소.
        doc: 비교할 문서 (새 버전).

    Returns:
        비교 결과. 직전 버전이 없으면 ``previous`` 가 ``None`` 이고 블록이 없다.

    Raises:
        VersionError: 비교할 수 없는 형식일 때 (415).
        ObjectNotFoundError: 원본 파일이 저장소에서 지워졌을 때.
        EditError: 파일을 열 수 없을 때.
    """
    if doc.kind not in EDITABLE_KINDS:
        raise VersionError("이 형식은 버전 비교를 지원하지 않습니다.", 415)
    previous = None
    if doc.source_document_id is not None:
        previous = await session.scalar(
            select(Document)
            .options(defer(Document.content))
            .where(
                Document.id == doc.source_document_id,
                Document.conversation_id == doc.conversation_id,
            )
        )
    unit = "paragraph" if doc.kind == DocumentKind.DOCX else "line"
    if previous is None:
        return Comparison(None, [], unit)
    new_units, unit = units_of(doc.kind, await storage.get(doc.storage_key))
    old_units, _ = units_of(previous.kind, await storage.get(previous.storage_key))
    return Comparison(previous, compare(old_units, new_units), unit)


async def restore_version(session: AsyncSession, storage: ObjectStorage, doc: Document) -> Document:
    """예전 버전의 파일을 복사해 가장 새 버전 뒤에 새 수정본을 만든다. 예전 버전들은 그대로 둔다.

    같은 파일이므로 파싱 결과(본문·쪽 수)도 복사한다. 원래 버전이 아직 준비되지 않았으면 다시 파싱한다.
    새 행은 사용자가 만든 것(``created_by="user"``)으로 기록한다.

    Args:
        session: DB 세션. 이 함수가 commit 한다.
        storage: 첨부 저장소.
        doc: 되돌릴 버전 (본문 ``content`` 까지 읽은 행).

    Returns:
        새로 만든 수정본.

    Raises:
        VersionError: 대화 문서가 아니거나(409) 이미 가장 새 버전일 때(409).
        ObjectNotFoundError: 원본 파일이 저장소에서 지워졌을 때.
    """
    if doc.conversation_id is None or doc.project_id is not None:
        raise VersionError("대화에서 만든 문서만 되돌릴 수 있습니다.")
    latest = await latest_revision(session, doc)
    if latest is None:
        raise VersionError("이미 가장 새 버전입니다.")
    data = await storage.get(doc.storage_key)
    revision = latest.revision + 1
    ready = doc.status == "ready"
    now = datetime.now(UTC)
    copy = Document(
        id=uuid.uuid4(),
        user_id=doc.user_id,
        conversation_id=doc.conversation_id,
        source_document_id=latest.id,
        revision=revision,
        created_by="user",
        filename=revision_filename(latest.filename, revision),
        kind=doc.kind,
        mime_type=doc.mime_type,
        size_bytes=len(data),
        sha256=doc.sha256,
        status="ready" if ready else "processing",
        content=doc.content if ready else None,
        page_count=doc.page_count if ready else None,
        token_count=doc.token_count if ready else None,
        parsed_at=now if ready else None,
        created_at=now,  # 세션이 commit 뒤에 다시 읽지 않으므로 (expire_on_commit=False) 직접 둔다
    )
    copy.storage_key = storage_key(copy.id)
    await storage.put(copy.storage_key, data, copy.mime_type)
    session.add(copy)
    await session.commit()
    if not ready:
        documents.schedule_parse(copy.id, data, copy.kind, copy.filename)
    return copy
