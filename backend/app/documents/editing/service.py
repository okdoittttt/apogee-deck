"""수정본·새 문서 만들기.

- 수정본: 원본 확인 → 원본 바이트 읽기 → 수정 적용·검사 → 수정본 저장 → 문서 행 추가.
- 새 문서: 이름·내용 검사 → 저장 → 문서 행 추가 (``create_new_document``).

수정본과 새 문서는 ``documents`` 의 새 행이다 (``created_by == "agent"``). 아래 설명은 둘 다 같다.
수정본은 원본과 같은 대화에 속하고 어느 메시지에도 붙지 않으며
(``message_id`` 가 ``None``), 원본은 바꾸지 않는다. 그래서 읽기(``read_document``)·다운로드·대화 삭제·
보관 기간 정리가 원본과 똑같이 동작한다.

텍스트 형식은 파싱이 글자 그대로라서 따로 파싱하지 않고 바로 ``ready`` 로 기록한다. 그래서 수정이 끝나자마자
다음 ``read_document`` 가 새 내용을 읽는다. DOCX 수정본은 본문(``content``)이 파싱 결과(Markdown, 그림 OCR)라서
업로드와 같은 파싱을 돌리고, 시간 제한 안에서 끝나기를 기다린다. 넘기면 수정본은 저장한 채 ``processing`` 으로
두고 파싱은 백그라운드에서 이어진다.

오류(``EditError``)의 메시지는 에이전트에게 도구 결과로 그대로 전달되므로 영어로 쓴다.
"""

import asyncio
import hashlib
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.core.storage import ObjectNotFoundError, ObjectStorage
from app.documents import service as documents
from app.documents.editing import text
from app.documents.editing.docx import DEFAULT_AUTHOR, DocxDocument
from app.documents.editing.text import Change, Edit, EditError, TextShape
from app.documents.formats import MIME_TYPES, DocumentKind, safe_filename
from app.documents.service import storage_key
from app.models import Conversation, Document

TEXT_KINDS = frozenset({DocumentKind.TXT, DocumentKind.MD, DocumentKind.CSV})
"""글자 그대로 고치는 텍스트 형식."""

EDITABLE_KINDS = TEXT_KINDS | {DocumentKind.DOCX}
"""지금 고칠 수 있는 형식."""

NEW_DOCUMENT_KINDS = {
    "txt": DocumentKind.TXT,
    "md": DocumentKind.MD,
    "markdown": DocumentKind.MD,
    "csv": DocumentKind.CSV,
}
"""새로 만들 수 있는 확장자 → 형식."""

FILENAME_LIMIT = 255
"""파일 이름의 최대 글자 수 (``documents.filename`` 의 길이)."""

_REVISION_SUFFIX = re.compile(r"\s*\(수정 \d+\)$")
"""수정본 이름 끝에 붙는 `` (수정 N)``. 수정본을 다시 고칠 때 떼고 새 번호를 붙인다."""


@dataclass(frozen=True)
class Revision:
    """만든 수정본.

    Attributes:
        id: 수정본 문서 ID.
        filename: 수정본 파일 이름 (예: ``"보고서 (수정 2).md"``).
        revision: 버전 번호 (원본 1, 고칠 때마다 1씩 커진다).
        source_document_id: 수정본을 만든 직전 문서 ID.
        kind: 문서 형식 (원본과 같다).
        size_bytes: 수정본 크기.
        changes: 바뀐 곳 요약 (수정 순서대로). DOCX 면 ``line`` 이 문단 번호다.
        status: 수정본 상태. 텍스트 형식은 늘 ``ready``. DOCX 는 파싱을 기다린 결과
            (``ready`` | ``processing`` 시간 안에 못 끝남 | ``failed``).
        failures: 적용하지 못한 수정의 이유 (영어, 수정 순서대로). 나머지 수정만 수정본에 들어갔다.
        tracked: 변경 내용 추적으로 남겼는지 (DOCX). 사용자가 Word 에서 하나씩 수락·거절할 수 있다.
        author: 추적 기록·메모의 작성자 (DOCX).
    """

    id: uuid.UUID
    filename: str
    revision: int
    source_document_id: uuid.UUID
    kind: str
    size_bytes: int
    changes: list[Change]
    status: str = "ready"
    failures: list[str] = field(default_factory=list)
    tracked: bool = False
    author: str = ""


def revision_filename(filename: str, revision: int) -> str:
    """수정본 파일 이름을 만든다.

    이름 끝의 `` (수정 N)`` 은 떼고 새 번호를 붙인다 (``"보고서 (수정 2).md"`` → ``"보고서 (수정 3).md"``).
    확장자는 그대로 두고, 길면 이름 부분을 줄여 ``FILENAME_LIMIT`` 안에 맞춘다.

    Args:
        filename: 직전 문서의 파일 이름.
        revision: 새 버전 번호.

    Returns:
        수정본 파일 이름.
    """
    stem, dot, ext = filename.rpartition(".")
    if not dot or not stem:  # 확장자가 없거나 ".env" 처럼 점으로 시작하는 이름
        stem, ext = filename, ""
    suffix = f" (수정 {revision})" + (f".{ext}" if ext else "")
    stem = _REVISION_SUFFIX.sub("", stem)
    return stem[: max(FILENAME_LIMIT - len(suffix), 1)] + suffix


def _parse_id(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value.strip())
    except (ValueError, AttributeError):
        return None


def _not_found(document_id: str) -> EditError:
    # 다른 대화·프로젝트의 문서도 같은 말로 답한다 (있는지 없는지 숨긴다).
    return EditError(
        f"No editable document with id {document_id!r} in this conversation. Use a document_id "
        "from the attachment list or from a previous edit result."
    )


@dataclass(frozen=True)
class EditableText:
    r"""고칠 텍스트 문서(TXT·MD·CSV)와 그 원본에서 만든 글자.

    Attributes:
        source: 고칠 문서 행 (본문 ``content`` 는 읽지 않았다).
        text: 원본 바이트에서 만든 ``\n`` 기준 글자 (``documents.content`` 와 같은 글자).
        shape: 원본 형태 (BOM, 줄바꿈). 수정본을 저장할 때 되돌린다.
    """

    source: Document
    text: str
    shape: TextShape


@dataclass(frozen=True)
class EditableDocx:
    """고칠 DOCX 문서와 그 원본을 연 것.

    Attributes:
        source: 고칠 문서 행 (본문 ``content`` 는 읽지 않았다).
        document: 원본 바이트로 연 DOCX.
    """

    source: Document
    document: DocxDocument


async def load_editable(
    session: AsyncSession, storage: ObjectStorage, *, document_id: str, conversation_id: str
) -> EditableText | EditableDocx:
    """고칠 문서를 확인하고 원본 바이트를 형식에 맞게 연다.

    지금 대화에 붙은 문서(이전 수정본 포함)만 고칠 수 있다. 프로젝트 문서는 아직 고치지 않는다.
    보기 도구와 수정 도구가 같은 확인을 거치도록 둘 다 이 함수를 쓴다.

    업로드한 문서는 파싱이 끝나(``ready``) 있어야 한다 (파싱이 실패한 파일은 암호·손상 등 열 수 없는 경우가
    많다). 에이전트가 만든 수정본은 원본을 우리가 만들었으므로 파싱이 아직이거나 실패해도 고칠 수 있다.

    Args:
        session: DB 세션.
        storage: 첨부 저장소.
        document_id: 고칠 문서 ID (모델이 넘긴 값).
        conversation_id: 지금 대화 ID (서버가 정한 값).

    Returns:
        텍스트 형식이면 ``EditableText``, DOCX 면 ``EditableDocx``.

    Raises:
        EditError: 문서가 없거나 다른 대화·프로젝트의 문서일 때, 고칠 수 없는 형식이거나 준비되지
            않았을 때, 원본이 저장소에서 지워졌을 때, 원본을 열 수 없을 때 (텍스트: 줄바꿈이 섞였거나
            UTF-8 이 아님, DOCX: 손상·매크로).
    """
    doc_id, conv_id = _parse_id(document_id), _parse_id(conversation_id)
    if doc_id is None or conv_id is None:
        raise _not_found(document_id)
    source = await session.scalar(
        select(Document)
        .options(defer(Document.content))
        .where(
            Document.id == doc_id,
            Document.conversation_id == conv_id,
            Document.project_id.is_(None),
        )
    )
    if source is None:
        raise _not_found(document_id)
    if source.kind not in EDITABLE_KINDS:
        raise EditError(
            f"Editing {source.kind.upper()} documents is not supported yet. Only TXT, MD, CSV "
            "and Word (DOCX) documents can be edited."
        )
    if source.status != "ready" and source.created_by != "agent":
        raise EditError(
            "This document is not ready (it is still being processed or failed to process). "
            "Try again after it is ready."
        )
    try:
        data = await storage.get(source.storage_key)
    except ObjectNotFoundError as exc:
        raise EditError(
            "The original file of this document is no longer stored (it may have expired), so "
            "it cannot be edited."
        ) from exc
    if source.kind == DocumentKind.DOCX:
        return EditableDocx(source, DocxDocument.from_bytes(data))
    decoded, shape = text.decode(data)
    return EditableText(source, decoded, shape)


async def latest_revision(session: AsyncSession, source: Document) -> Document | None:
    """``source`` 를 원본으로 이어진 수정본 사슬(원본 → v2 → v3)의 마지막 수정본.

    같은 대화 안에서 ``source_document_id`` 를 따라간다. 한 문서에서 수정본이 여럿 갈라졌으면 가장 최근 것을
    따른다.

    Args:
        session: DB 세션.
        source: 기준 문서.

    Returns:
        더 새 수정본. 없으면 ``None``.
    """
    latest = None
    current = source.id
    for _ in range(1000):  # 사슬이 끝없이 이어지는 일은 없지만 안전하게 끊는다
        child = await session.scalar(
            select(Document)
            .options(defer(Document.content))
            .where(
                Document.source_document_id == current,
                Document.conversation_id == source.conversation_id,
            )
            .order_by(Document.created_at.desc())
            .limit(1)
        )
        if child is None:
            break
        latest, current = child, child.id
    return latest


async def _wait_for_parse(
    session: AsyncSession, doc_id: uuid.UUID, task: asyncio.Task, wait: float
) -> str:
    """수정본 파싱을 ``wait`` 초까지 기다리고 상태를 돌려준다. 넘기면 파싱은 백그라운드에서 이어진다."""
    try:
        # shield: 시간이 지나도 파싱 작업 자체는 취소하지 않는다.
        await asyncio.wait_for(asyncio.shield(task), timeout=max(wait, 0))
    except TimeoutError:
        return "processing"
    status = await session.scalar(select(Document.status).where(Document.id == doc_id))
    return status or "processing"


async def create_revision(
    session: AsyncSession,
    storage: ObjectStorage,
    *,
    document_id: str,
    conversation_id: str,
    edits: Sequence[Edit],
    max_bytes: int,
    parse_wait: float = 0.0,
    track_changes: bool | None = None,
    author: str = DEFAULT_AUTHOR,
) -> Revision:
    """문서에 수정을 적용해 수정본을 만든다. 원본은 바꾸지 않는다.

    고칠 수 있는 문서인지는 ``load_editable`` 과 같은 규칙으로 확인한다. 이 문서로 만든 더 새 수정본이 있으면
    고치지 않는다 (예전 버전을 고치면 그 뒤의 수정이 빠진 갈래가 생긴다). 수정본 원본을 저장소에 먼저 올리고
    DB 에 기록한다. 기록이 실패해서 남은 객체는 보관 기간 규칙이 지운다.

    적용할 수 없는 수정은 건너뛰고 이유를 ``Revision.failures`` 에 담는다. 하나도 적용하지 못하면 수정본을
    만들지 않고 오류를 올린다.

    텍스트 형식은 바로 ``ready`` 로 기록한다. DOCX 는 ``processing`` 으로 기록하고 업로드와 같은 파싱을
    시작해서 ``parse_wait`` 초까지 기다린다.

    Args:
        session: DB 세션. 이 함수가 commit 한다.
        storage: 첨부 저장소.
        document_id: 고칠 문서 ID (모델이 넘긴 값).
        conversation_id: 지금 대화 ID (서버가 정한 값).
        edits: 적용할 수정 (순서대로).
        max_bytes: 수정본의 최대 크기 (``Settings.document_max_bytes``).
        parse_wait: DOCX 수정본의 파싱을 기다릴 최대 시간(초).
        track_changes: DOCX 를 변경 내용 추적으로 고칠지. ``None`` 이면 원본에 Word 의 "변경 내용 추적" 이
            켜져 있을 때만 추적한다. 텍스트 형식에서 ``True`` 면 오류.
        author: DOCX 추적 기록·메모의 작성자.

    Returns:
        만든 수정본.

    Raises:
        EditError: 고칠 수 있는 문서가 아니거나(``load_editable`` 참고), 더 새 수정본이 있거나, 수정을
            하나도 적용할 수 없거나,
            CSV 가 깨지거나 문서가 비게 되거나, 수정본이 너무 클 때.
    """
    loaded = await load_editable(
        session, storage, document_id=document_id, conversation_id=conversation_id
    )
    source = loaded.source
    newer = await latest_revision(session, source)
    if newer is not None:
        raise EditError(
            f"A newer version of this document exists: {newer.filename} (document_id: "
            f"{newer.id}, revision {newer.revision}). Edit that version instead, so that earlier "
            "changes are not lost; look at it with view_document_for_edit first."
        )
    content: str | None
    tracked = False
    if isinstance(loaded, EditableDocx):
        tracked = loaded.document.tracks_revisions if track_changes is None else track_changes
        changes, failures = loaded.document.apply_some(edits, track=tracked, author=author)
        if not changes:
            raise text.combine_failures(failures)
        new_data = loaded.document.to_bytes()
        content, status = None, "processing"
    else:
        if track_changes:
            raise EditError(
                "Tracked changes only work for Word documents. Edit this file without "
                "track_changes."
            )
        before, shape = loaded.text, loaded.shape
        content, changes, failures = text.apply_some(before, edits)
        if not changes:
            raise text.combine_failures(failures)
        if source.kind == DocumentKind.CSV:
            text.check_csv(before, content)
        new_data = text.encode(content, shape)
        status = "ready"
    if len(new_data) > max_bytes:
        raise EditError(
            f"The edited document would be {len(new_data):,} bytes, larger than the limit of "
            f"{max_bytes:,} bytes."
        )

    revision = source.revision + 1
    doc = Document(
        id=uuid.uuid4(),
        user_id=source.user_id,
        conversation_id=source.conversation_id,
        source_document_id=source.id,
        revision=revision,
        created_by="agent",
        filename=revision_filename(source.filename, revision),
        kind=source.kind,
        mime_type=source.mime_type,
        size_bytes=len(new_data),
        sha256=hashlib.sha256(new_data).hexdigest(),
        status=status,
        content=content,
        parsed_at=datetime.now(UTC) if status == "ready" else None,
    )
    doc.storage_key = storage_key(doc.id)
    await storage.put(doc.storage_key, new_data, doc.mime_type)
    session.add(doc)
    await session.commit()
    if status == "processing":
        task = documents.schedule_parse(doc.id, new_data, doc.kind, doc.filename)
        status = await _wait_for_parse(session, doc.id, task, parse_wait)
    return Revision(
        id=doc.id,
        filename=doc.filename,
        revision=revision,
        source_document_id=source.id,
        kind=doc.kind,
        size_bytes=doc.size_bytes,
        changes=changes,
        status=status,
        failures=[str(f) for f in failures],
        tracked=tracked,
        author=author if tracked else "",
    )


@dataclass(frozen=True)
class NewDocument:
    """새로 만든 문서.

    Attributes:
        id: 문서 ID.
        filename: 파일 이름.
        kind: 문서 형식.
        size_bytes: 파일 크기.
        line_count: 줄 수 (에이전트에게 결과를 알려줄 때 쓴다).
    """

    id: uuid.UUID
    filename: str
    kind: str
    size_bytes: int
    line_count: int


def new_document_shape(kind: DocumentKind) -> TextShape:
    r"""새로 만드는 문서의 파일 형태.

    줄바꿈은 ``\n`` 이다. CSV 에만 BOM 을 붙인다: Excel 은 BOM 이 없는 UTF-8 CSV 의 한글을 깨뜨려 보여준다.
    (수정본은 원본 형태를 따르므로 이 규칙을 쓰지 않는다.)

    Args:
        kind: 문서 형식.

    Returns:
        저장할 때 쓸 형태.
    """
    return TextShape(bom=kind is DocumentKind.CSV, newline="\n")


async def create_new_document(
    session: AsyncSession,
    storage: ObjectStorage,
    *,
    conversation_id: str,
    filename: str,
    content: str,
    max_bytes: int,
) -> NewDocument:
    """새 문서(TXT·MD·CSV)를 만들어 지금 대화에 붙인다.

    수정본처럼 대화에 속하고 어느 메시지에도 붙지 않는 문서로 기록한다 (``revision`` 1, 직전 문서 없음).
    그래서 받기·읽기·고치기(``edit_document``)·보관 기간 정리가 업로드한 문서와 똑같이 동작한다.
    문서의 주인은 대화의 주인이다 (받을 때 주인인지 확인한다).

    Args:
        session: DB 세션. 이 함수가 commit 한다.
        storage: 첨부 저장소.
        conversation_id: 지금 대화 ID (서버가 정한 값).
        filename: 파일 이름 (모델이 넘긴 값). 확장자로 형식을 정한다.
        content: 파일 내용.
        max_bytes: 최대 크기 (``Settings.document_max_bytes``).

    Returns:
        만든 문서.

    Raises:
        EditError: 확장자가 없거나 지원하지 않을 때, 내용이 비었거나 NUL 글자가 있을 때, CSV 가 올바른
            표가 아닐 때, 너무 클 때, 대화를 찾을 수 없을 때.
    """
    name = safe_filename(filename)
    stem, dot, ext = name.rpartition(".")
    kind = NEW_DOCUMENT_KINDS.get(ext.lower()) if dot and stem else None
    if kind is None:
        raise EditError(
            f"Cannot create {name!r}: the filename must end with .txt, .md or .csv (other "
            "formats cannot be created yet)."
        )
    body = content.replace("\r\n", "\n")
    if not body.strip():
        raise EditError("The content is empty. Write the full text of the document.")
    if "\x00" in body:
        raise EditError("The content contains NUL characters, which text files cannot have.")
    if kind is DocumentKind.CSV:
        text.check_new_csv(body)
    data = text.encode(body, new_document_shape(kind))
    if len(data) > max_bytes:
        raise EditError(
            f"The document would be {len(data):,} bytes, larger than the limit of "
            f"{max_bytes:,} bytes."
        )
    conv_id = _parse_id(conversation_id)
    if conv_id is None:
        raise EditError("This conversation cannot store documents.")
    owner = await session.scalar(select(Conversation.user_id).where(Conversation.id == conv_id))
    if owner is None:
        raise EditError("This conversation cannot store documents.")

    doc = Document(
        id=uuid.uuid4(),
        user_id=owner,
        conversation_id=conv_id,
        revision=1,
        created_by="agent",
        filename=name,
        kind=kind.value,
        mime_type=MIME_TYPES[kind],
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        status="ready",
        content=body,
        parsed_at=datetime.now(UTC),
    )
    doc.storage_key = storage_key(doc.id)
    await storage.put(doc.storage_key, data, doc.mime_type)
    session.add(doc)
    await session.commit()
    lines = body.count("\n") + (0 if body.endswith("\n") else 1)
    return NewDocument(doc.id, doc.filename, doc.kind, doc.size_bytes, lines)
