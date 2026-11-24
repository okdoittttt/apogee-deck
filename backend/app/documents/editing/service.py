"""수정본·새 문서 만들기.

- 수정본: 원본 확인 → 원본 바이트 읽기 → 수정 적용·검사 → 수정본 저장 → 문서 행 추가.
- 새 문서: 이름·내용 검사 → 저장 → 문서 행 추가 (``create_new_document``).

수정본과 새 문서는 ``documents`` 의 새 행이다 (``created_by == "agent"``). 아래 설명은 둘 다 같다.
수정본은 원본과 같은 대화에 속하고 어느 메시지에도 붙지 않으며
(``message_id`` 가 ``None``), 원본은 바꾸지 않는다. 그래서 읽기(``read_document``)·다운로드·대화 삭제·
보관 기간 정리가 원본과 똑같이 동작한다.

텍스트 형식은 파싱이 글자 그대로라서 따로 파싱하지 않고 바로 ``ready`` 로 기록한다. 그래서 수정이 끝나자마자
다음 ``read_document`` 가 새 내용을 읽는다.

오류(``EditError``)의 메시지는 에이전트에게 도구 결과로 그대로 전달되므로 영어로 쓴다.
"""

import hashlib
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.core.storage import ObjectNotFoundError, ObjectStorage
from app.documents.editing import text
from app.documents.editing.text import Change, Edit, EditError, TextShape
from app.documents.formats import MIME_TYPES, DocumentKind, safe_filename
from app.documents.service import storage_key
from app.models import Conversation, Document

EDITABLE_KINDS = frozenset({DocumentKind.TXT, DocumentKind.MD, DocumentKind.CSV})
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
        changes: 바뀐 곳 요약 (수정 순서대로).
    """

    id: uuid.UUID
    filename: str
    revision: int
    source_document_id: uuid.UUID
    kind: str
    size_bytes: int
    changes: list[Change]


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
    r"""고칠 문서와 그 원본에서 만든 글자.

    Attributes:
        source: 고칠 문서 행 (본문 ``content`` 는 읽지 않았다).
        text: 원본 바이트에서 만든 ``\n`` 기준 글자 (``documents.content`` 와 같은 글자).
        shape: 원본 형태 (BOM, 줄바꿈). 수정본을 저장할 때 되돌린다.
    """

    source: Document
    text: str
    shape: TextShape


async def load_text(
    session: AsyncSession, storage: ObjectStorage, *, document_id: str, conversation_id: str
) -> EditableText:
    """고칠 문서를 확인하고 원본 바이트에서 수정할 글자를 만든다.

    지금 대화에 붙은 문서(이전 수정본 포함)만 고칠 수 있다. 프로젝트 문서는 아직 고치지 않는다.
    보기 도구와 수정 도구가 같은 확인을 거치도록 둘 다 이 함수를 쓴다.

    Args:
        session: DB 세션.
        storage: 첨부 저장소.
        document_id: 고칠 문서 ID (모델이 넘긴 값).
        conversation_id: 지금 대화 ID (서버가 정한 값).

    Returns:
        고칠 문서와 수정할 글자, 원본 형태.

    Raises:
        EditError: 문서가 없거나 다른 대화·프로젝트의 문서일 때, 고칠 수 없는 형식이거나 준비되지
            않았을 때, 원본이 저장소에서 지워졌을 때, 줄바꿈이 섞였거나 UTF-8 이 아닐 때.
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
            f"Editing {source.kind.upper()} documents is not supported yet. Only TXT, MD and "
            "CSV documents can be edited."
        )
    if source.status != "ready":
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
    decoded, shape = text.decode(data)
    return EditableText(source, decoded, shape)


async def create_revision(
    session: AsyncSession,
    storage: ObjectStorage,
    *,
    document_id: str,
    conversation_id: str,
    edits: Sequence[Edit],
    max_bytes: int,
) -> Revision:
    """문서에 수정을 적용해 수정본을 만든다. 원본은 바꾸지 않는다.

    고칠 수 있는 문서인지는 ``load_text`` 와 같은 규칙으로 확인한다. 수정본 원본을 저장소에 먼저 올리고
    DB 에 기록한다. 기록이 실패해서 남은 객체는 보관 기간 규칙이 지운다.

    Args:
        session: DB 세션. 이 함수가 commit 한다.
        storage: 첨부 저장소.
        document_id: 고칠 문서 ID (모델이 넘긴 값).
        conversation_id: 지금 대화 ID (서버가 정한 값).
        edits: 적용할 수정 (순서대로).
        max_bytes: 수정본의 최대 크기 (``Settings.document_max_bytes``).

    Returns:
        만든 수정본.

    Raises:
        EditError: 고칠 수 있는 문서가 아니거나(``load_text`` 참고), 수정을 적용할 수 없거나,
            수정본이 너무 클 때.
    """
    loaded = await load_text(
        session, storage, document_id=document_id, conversation_id=conversation_id
    )
    source, before, shape = loaded.source, loaded.text, loaded.shape
    after, changes = text.apply_edits(before, edits)
    if source.kind == DocumentKind.CSV:
        text.check_csv(before, after)
    new_data = text.encode(after, shape)
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
        status="ready",
        content=after,
        parsed_at=datetime.now(UTC),
    )
    doc.storage_key = storage_key(doc.id)
    await storage.put(doc.storage_key, new_data, doc.mime_type)
    session.add(doc)
    await session.commit()
    return Revision(
        id=doc.id,
        filename=doc.filename,
        revision=revision,
        source_document_id=source.id,
        kind=doc.kind,
        size_bytes=doc.size_bytes,
        changes=changes,
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
