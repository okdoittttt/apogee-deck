"""수정본 만들기: 원본 확인 규칙, 수정본 행·저장소 객체, 파일 이름, 원본 형태 보존."""

import asyncio
import hashlib
import uuid
from pathlib import Path

import pytest
from editing_fakes import CONVERSATION, USER, FakeSession, FakeStorage, source_doc

from app.documents.editing import service as editing_service
from app.documents.editing.docx import DocxDocument
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


@pytest.mark.parametrize("kind", ["pdf", "xlsx", "pptx", "hwp"])
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


async def test_all_edits_failing_saves_nothing():
    source = source_doc()
    storage = FakeStorage({source.storage_key: b"a\n"})
    session = FakeSession(source)
    with pytest.raises(EditError, match="None of the edits could be applied") as caught:
        await create_revision(
            session,
            storage,
            document_id=str(source.id),
            conversation_id=str(CONVERSATION),
            edits=[Edit("zzz", "b"), Edit("yyy", "y")],
            max_bytes=MAX_BYTES,
        )
    assert "Edit 1:" in str(caught.value) and "Edit 2:" in str(caught.value)
    assert session.added == [] and session.commits == 0
    assert list(storage.objects) == [source.storage_key]


async def test_some_edits_failing_saves_the_rest():
    source = source_doc()
    revision, _, storage = await revise(
        source, b"a b\n", [Edit("a", "A"), Edit("zzz", "y"), Edit("b", "B")]
    )
    assert storage.objects[f"documents/{revision.id}"] == b"A B\n"
    assert [c.index for c in revision.changes] == [1, 3]
    (failure,) = revision.failures
    assert failure.startswith("Edit 2: old_text was not found")


async def test_one_failed_edit_keeps_its_own_message():
    with pytest.raises(EditError, match=r"^Edit 1: old_text was not found"):
        await revise(source_doc(), b"a\n", [Edit("zzz", "b")])


async def test_csv_check_runs_after_all_edits():
    with pytest.raises(EditError, match="columns"):
        await revise(source_doc(kind="csv"), b"a,b\n1,2\n", [Edit("1,2", "1,2,3"), Edit("a", "A")])


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


# --- DOCX ----------------------------------------------------------------------------------------

DOCX = (Path(__file__).parent / "fixtures" / "sales-report.docx").read_bytes()
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def docx_source(**extra):
    return source_doc(kind="docx", filename="영업 보고.docx", mime_type=DOCX_MIME, **extra)


def fake_parse(monkeypatch, *, finish=True):
    """수정본 파싱(``schedule_parse``)을 가짜로 바꾸고 불린 인자를 기록한다."""
    calls = []

    def schedule(document_id, data, kind, filename):
        calls.append((document_id, data, kind, filename))
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        if finish:
            future.set_result(None)
        return future

    monkeypatch.setattr(editing_service.documents, "schedule_parse", schedule)
    return calls


async def revise_docx(source, edits, *, later=(), parse_wait=5.0):
    storage = FakeStorage({source.storage_key: DOCX})
    session = FakeSession(source, later=later)
    revision = await create_revision(
        session,
        storage,
        document_id=str(source.id),
        conversation_id=str(CONVERSATION),
        edits=edits,
        max_bytes=10_000_000,
        parse_wait=parse_wait,
    )
    return revision, session, storage


async def test_docx_revision_is_parsed_like_an_upload(monkeypatch):
    calls = fake_parse(monkeypatch)
    source = docx_source()
    revision, session, storage = await revise_docx(
        source, [Edit("120억", "135억")], later=["ready"]
    )
    (doc,) = session.added
    assert (doc.status, doc.content, doc.parsed_at) == ("processing", None, None)
    assert doc.filename == "영업 보고 (수정 2).docx"
    assert doc.mime_type == DOCX_MIME
    saved = storage.objects[doc.storage_key]
    assert "135억" in DocxDocument.from_bytes(saved).paragraphs[1].text
    assert calls == [(doc.id, saved, "docx", doc.filename)]
    assert revision.status == "ready"
    assert (revision.changes[0].line, revision.kind) == (2, "docx")


async def test_docx_paragraph_field_narrows_search(monkeypatch):
    fake_parse(monkeypatch)
    _, session, storage = await revise_docx(
        docx_source(), [Edit("김민수", "최유진", paragraph="p21")], later=["ready"]
    )
    text = DocxDocument.from_bytes(storage.objects[session.added[0].storage_key]).view(1, 99).text
    assert "[p10] (표1 2행 3열) 김민수" in text
    assert "[p21] 문의: 영업기획팀 최유진 (내선 1234)" in text


async def test_docx_parse_timeout_keeps_revision_processing(monkeypatch):
    fake_parse(monkeypatch, finish=False)
    revision, session, _ = await revise_docx(
        docx_source(), [Edit("120억", "135억")], parse_wait=0.01
    )
    assert revision.status == "processing"
    assert session.commits == 1


async def test_docx_failed_parse_is_reported(monkeypatch):
    fake_parse(monkeypatch)
    revision, _, _ = await revise_docx(docx_source(), [Edit("120억", "135억")], later=["failed"])
    assert revision.status == "failed"


async def test_agent_revision_can_be_edited_before_parse_finishes(monkeypatch):
    fake_parse(monkeypatch)
    source = docx_source(status="processing", created_by="agent", revision=2)
    revision, _, _ = await revise_docx(source, [Edit("120억", "135억")], later=["ready"])
    assert revision.revision == 3


async def test_docx_edit_error_saves_nothing(monkeypatch):
    calls = fake_parse(monkeypatch)
    with pytest.raises(EditError, match="image"):
        await revise_docx(docx_source(), [Edit("로고: ￼", "로고:")])
    assert calls == []


# --- 최신 수정본 확인 (묶음 1, C4) ----------------------------------------------------------------


async def test_editing_an_older_version_is_refused():
    source = source_doc()
    v2 = source_doc(revision=2, filename="notes (수정 2).txt")
    v3 = source_doc(revision=3, filename="notes (수정 3).txt")
    storage = FakeStorage({source.storage_key: b"a\n"})
    session = FakeSession(source, children=[v2, v3])
    with pytest.raises(EditError, match="newer version") as caught:
        await create_revision(
            session,
            storage,
            document_id=str(source.id),
            conversation_id=str(CONVERSATION),
            edits=[Edit("a", "b")],
            max_bytes=MAX_BYTES,
        )
    assert str(v3.id) in str(caught.value) and "revision 3" in str(caught.value)
    assert session.added == []
