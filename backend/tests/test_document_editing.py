"""수정본 만들기: 원본 확인 규칙, 수정본 행·저장소 객체, 파일 이름, 원본 형태 보존."""

import hashlib
import uuid

import pytest
from editing_fakes import CONVERSATION, USER, FakeSession, FakeStorage, source_doc

from app.documents.editing.service import (
    FILENAME_LIMIT,
    create_revision,
    revision_filename,
)
from app.documents.editing.text import BOM, Edit, EditError
from app.models import Document

MAX_BYTES = 1000


async def revise(source, data, edits, max_bytes=MAX_BYTES):
    storage = FakeStorage({source.storage_key: data} if data is not None else {})
    session = FakeSession(source)
    revision = await create_revision(
        session,
        storage,
        document_id=str(source.id),
        conversation_id=str(CONVERSATION),
        edits=edits,
        max_bytes=max_bytes,
    )
    return revision, session, storage


# --- 수정본 만들기 -------------------------------------------------------------------------------


async def test_creates_revision_row_and_object():
    source = source_doc()
    original = "참석자: 김민수\n장소: 본사 3층 회의실\n".encode()
    revision, session, storage = await revise(source, original, [Edit("3층", "13층")])

    assert session.commits == 1
    (doc,) = session.added
    assert isinstance(doc, Document)
    expected = "참석자: 김민수\n장소: 본사 13층 회의실\n"
    assert doc.id == revision.id
    assert (doc.status, doc.content, doc.kind) == ("ready", expected, "txt")
    assert (doc.revision, doc.created_by, doc.source_document_id) == (2, "agent", source.id)
    assert (doc.user_id, doc.conversation_id) == (USER, CONVERSATION)
    assert doc.message_id is None and doc.project_id is None
    assert doc.parsed_at is not None
    assert doc.storage_key == f"documents/{doc.id}"  # 대화 첨부와 같은 보관 기간 규칙을 받는다
    assert storage.objects[doc.storage_key] == expected.encode()
    assert storage.content_types[doc.storage_key] == source.mime_type
    assert doc.size_bytes == len(expected.encode())
    assert doc.sha256 == hashlib.sha256(expected.encode()).hexdigest()
    assert storage.objects[source.storage_key] == original  # 원본은 그대로

    assert (revision.filename, revision.revision, revision.kind) == ("notes (수정 2).txt", 2, "txt")
    assert revision.source_document_id == source.id
    assert [(c.line, c.before, c.after) for c in revision.changes] == [(2, "3층", "13층")]


async def test_keeps_bom_and_crlf_in_saved_file():
    source = source_doc(kind="md")
    original = BOM + "# 제목\r\n본문 오타\r\n".encode()
    _, session, storage = await revise(source, original, [Edit("오타", "수정")])
    (doc,) = session.added
    assert storage.objects[doc.storage_key] == BOM + "# 제목\r\n본문 수정\r\n".encode()
    # DB 본문은 파서와 같은 \n 기준 글자다 (read_document 가 읽는 글자)
    assert doc.content == "# 제목\n본문 수정\n"


async def test_revision_of_revision_continues_chain():
    source = source_doc(revision=2, filename="notes (수정 2).txt")
    revision, session, _ = await revise(source, b"a\n", [Edit("a", "b")])
    assert (revision.revision, revision.filename) == (3, "notes (수정 3).txt")
    assert session.added[0].source_document_id == source.id


async def test_query_limits_to_this_conversation_and_excludes_project_documents():
    source = source_doc()
    _, session, _ = await revise(source, b"a\n", [Edit("a", "b")])
    sql = str(session.queries[0])
    assert "documents.conversation_id =" in sql
    assert "documents.project_id IS NULL" in sql


async def test_csv_column_check_runs():
    source = source_doc(kind="csv")
    original = 'name,city\nKim,"서울, 강남"\n'.encode()
    with pytest.raises(EditError, match="columns"):
        await revise(source, original, [Edit('"서울, 강남"', "서울, 강남")])


async def test_csv_check_not_applied_to_txt():
    source = source_doc(kind="txt")
    revision, _, _ = await revise(source, b'a,b\n"x, y",1\n', [Edit('"x, y"', "x, y")])
    assert revision.revision == 2


# --- 거절 ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("document_id", ["not-a-uuid", "", str(uuid.uuid4())])
async def test_not_found(document_id):
    with pytest.raises(EditError, match="No editable document"):
        await create_revision(
            FakeSession(None),
            FakeStorage(),
            document_id=document_id,
            conversation_id=str(CONVERSATION),
            edits=[Edit("a", "b")],
            max_bytes=MAX_BYTES,
        )


@pytest.mark.parametrize("kind", ["docx", "pdf", "xlsx", "hwp"])
async def test_unsupported_kind(kind):
    with pytest.raises(EditError, match=f"Editing {kind.upper()} documents is not supported"):
        await revise(source_doc(kind=kind), b"x", [Edit("x", "y")])


@pytest.mark.parametrize("status", ["processing", "failed"])
async def test_not_ready(status):
    with pytest.raises(EditError, match="not ready"):
        await revise(source_doc(status=status), b"x", [Edit("x", "y")])


async def test_original_object_gone():
    with pytest.raises(EditError, match="no longer stored"):
        await revise(source_doc(), None, [Edit("x", "y")])


async def test_too_large():
    with pytest.raises(EditError, match="larger than the limit of 10 bytes"):
        await revise(source_doc(), b"short\n", [Edit("short", "much longer text")], max_bytes=10)


async def test_failed_edit_saves_nothing():
    source = source_doc()
    storage = FakeStorage({source.storage_key: b"a\n"})
    session = FakeSession(source)
    with pytest.raises(EditError, match="not found"):
        await create_revision(
            session,
            storage,
            document_id=str(source.id),
            conversation_id=str(CONVERSATION),
            edits=[Edit("a", "b"), Edit("zzz", "y")],
            max_bytes=MAX_BYTES,
        )
    assert session.added == [] and session.commits == 0
    assert list(storage.objects) == [source.storage_key]


async def test_mixed_newlines_rejected():
    with pytest.raises(EditError, match="mixes CRLF and LF"):
        await revise(source_doc(), b"a\r\nb\n", [Edit("a", "b")])


# --- 파일 이름 ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "revision", "expected"),
    [
        ("보고서.md", 2, "보고서 (수정 2).md"),
        ("보고서 (수정 2).md", 3, "보고서 (수정 3).md"),
        ("보고서(수정 2).md", 3, "보고서 (수정 3).md"),
        ("data.backup.csv", 2, "data.backup (수정 2).csv"),
        ("README", 2, "README (수정 2)"),
        (".env", 2, ".env (수정 2)"),
        ("보고서 (최종).txt", 2, "보고서 (최종) (수정 2).txt"),
    ],
)
def test_revision_filename(filename, revision, expected):
    assert revision_filename(filename, revision) == expected


def test_revision_filename_fits_limit():
    name = revision_filename("가" * 260 + ".txt", 12)
    assert len(name) == FILENAME_LIMIT
    assert name.endswith(" (수정 12).txt")
