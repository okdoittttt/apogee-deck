"""버전 목록·비교·되돌리기 (묶음 4 화면용 API) 테스트."""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from editing_fakes import CONVERSATION, USER, FakeStorage
from fastapi import HTTPException

from app.api.routers import documents as documents_router
from app.documents.editing import versions
from app.documents.editing.compare import Block, Part, Unit, compare, word_parts
from app.documents.editing.docx import DocxDocument
from app.documents.editing.text import Edit

FIXTURE = (Path(__file__).parent / "fixtures" / "sales-report.docx").read_bytes()
START = datetime(2026, 11, 24, 9, 0, tzinfo=UTC)

# --- 비교 (순수 함수) ----------------------------------------------------------------------------


def units(*texts: str) -> list[Unit]:
    return [Unit(f"p{n}", t) for n, t in enumerate(texts, 1)]


def test_word_parts_marks_changed_words():
    assert word_parts("매출은 120억 원이다.", "매출은 135억 원이다.") == (
        Part("equal", "매출은 "),
        Part("delete", "120억"),
        Part("insert", "135억"),
        Part("equal", " 원이다."),
    )


def test_compare_pairs_changed_and_reports_added_and_removed():
    old = units("제목", "첫 문단", "둘째 문단", "지울 문단", "끝")
    new = units("제목", "첫 문단 고침", "둘째 문단", "새 문단 하나", "새 문단 둘", "끝")
    blocks = compare(old, new, context=5)
    assert [(b.kind, b.label) for b in blocks] == [
        ("same", "p1"),
        ("changed", "p2"),
        ("same", "p3"),
        ("changed", "p4"),
        ("added", "p5"),
        ("same", "p6"),
    ]
    assert blocks[1].parts == (Part("equal", "첫 문단"), Part("insert", " 고침"))


def test_compare_removed_uses_old_label():
    blocks = compare(units("가", "나", "다"), units("가", "다"), context=5)
    assert [(b.kind, b.label) for b in blocks] == [
        ("same", "p1"),
        ("removed", "p2"),
        ("same", "p2"),
    ]


def test_compare_folds_far_unchanged_blocks():
    old = units(*[f"문단 {n}" for n in range(1, 11)])
    new = units(*[f"문단 {n}" if n != 5 else "문단 다섯" for n in range(1, 11)])
    blocks = compare(old, new)
    assert [(b.kind, b.label, b.skipped) for b in blocks] == [
        ("skipped", "", 3),
        ("same", "p4", 0),
        ("changed", "p5", 0),
        ("same", "p6", 0),
        ("skipped", "", 4),
    ]
    assert compare(old, old) == [Block("skipped", skipped=10)]


# --- 버전 사슬, 비교, 되돌리기 (서비스) ------------------------------------------------------------


class ChainSession:
    """문서 행 목록에서 ``id``·``source_document_id`` 조건으로 찾는 가짜 세션."""

    def __init__(self, docs):
        self.docs = list(docs)
        self.added = []
        self.commits = 0

    async def scalar(self, query):
        params = query.compile().params
        conversation = params.get("conversation_id_1")
        rows = [d for d in self.docs if conversation is None or d.conversation_id == conversation]
        if "source_document_id_1" in params:
            rows = [d for d in rows if d.source_document_id == params["source_document_id_1"]]
            rows.sort(key=lambda d: d.created_at, reverse=True)
        else:
            rows = [d for d in rows if d.id == params["id_1"]]
        return rows[0] if rows else None

    def add(self, obj):
        self.added.append(obj)
        self.docs.append(obj)

    async def commit(self):
        self.commits += 1


def version(revision, source=None, *, data=b"", kind="docx", status="ready", **extra):
    doc_id = uuid.uuid4()
    name = "보고.docx" if revision == 1 else f"보고 (수정 {revision}).docx"
    fields = {
        "id": doc_id,
        "user_id": USER,
        "conversation_id": CONVERSATION,
        "project_id": None,
        "message_id": None,
        "source_document_id": source.id if source is not None else None,
        "revision": revision,
        "created_by": "user" if revision == 1 else "agent",
        "filename": name,
        "kind": kind,
        "mime_type": "application/docx",
        "size_bytes": len(data),
        "sha256": "x",
        "status": status,
        "content": f"본문 {revision}",
        "page_count": None,
        "token_count": None,
        "storage_key": f"documents/{doc_id}",
        "created_at": START + timedelta(minutes=revision),
    }
    return SimpleNamespace(**(fields | extra))


def edited(data: bytes, *edits: Edit) -> bytes:
    doc = DocxDocument.from_bytes(data)
    doc.apply_edits(edits)
    return doc.to_bytes()


@pytest.fixture
def chain():
    v2_data = edited(FIXTURE, Edit(old_text="120억", new_text="135억"))
    v3_data = edited(v2_data, Edit(op="delete_paragraph", paragraph="p19"))
    v1 = version(1, data=FIXTURE)
    v2 = version(2, v1, data=v2_data)
    v3 = version(3, v2, data=v3_data)
    storage = FakeStorage(
        {v1.storage_key: FIXTURE, v2.storage_key: v2_data, v3.storage_key: v3_data}
    )
    return ChainSession([v1, v2, v3]), storage, (v1, v2, v3)


async def test_version_chain_from_any_version(chain):
    session, _, docs = chain
    for doc in docs:
        assert await versions.version_chain(session, doc) == list(docs)


async def test_version_chain_follows_newest_branch_and_stays_in_conversation(chain):
    session, _, (v1, v2, v3) = chain
    branch = version(3, v2, created_at=START + timedelta(hours=1))
    other = version(4, v3, conversation_id=uuid.uuid4())
    session.docs += [branch, other]
    assert await versions.version_chain(session, v1) == [v1, v2, branch]


async def test_compare_with_previous_docx(chain):
    session, storage, (v1, v2, v3) = chain
    result = await versions.compare_with_previous(session, storage, v2)
    assert result.previous is v1 and result.unit == "paragraph"
    changed = [b for b in result.blocks if b.kind != "same" and b.kind != "skipped"]
    assert [(b.kind, b.label) for b in changed] == [("changed", "p2")]
    assert Part("insert", "135억") in changed[0].parts
    removed = await versions.compare_with_previous(session, storage, v3)
    assert [(b.kind, b.label) for b in removed.blocks if b.kind == "removed"] == [
        ("removed", "p19")
    ]
    first = await versions.compare_with_previous(session, storage, v1)
    assert first.previous is None and first.blocks == []


async def test_compare_text_lines():
    v1 = version(1, kind="md", data=b"# a\n\nb\n")
    v2 = version(2, v1, kind="md", data=b"# a\n\nc\n")
    storage = FakeStorage({v1.storage_key: b"# a\n\nb\n", v2.storage_key: b"# a\n\nc\n"})
    result = await versions.compare_with_previous(ChainSession([v1, v2]), storage, v2)
    assert result.unit == "line"
    assert [(b.kind, b.label) for b in result.blocks] == [
        ("skipped", ""),
        ("same", "L2"),
        ("changed", "L3"),
    ]


async def test_compare_refuses_other_kinds():
    doc = version(2, kind="pdf")
    with pytest.raises(versions.VersionError) as info:
        await versions.compare_with_previous(ChainSession([doc]), FakeStorage(), doc)
    assert info.value.status == 415


async def test_restore_copies_old_version_after_latest(chain):
    session, storage, (v1, _, v3) = chain
    restored = await versions.restore_version(session, storage, v1)
    assert (restored.revision, restored.source_document_id) == (4, v3.id)
    assert restored.filename == "보고 (수정 4).docx" and restored.created_by == "user"
    assert (restored.status, restored.content) == ("ready", "본문 1")
    assert storage.objects[restored.storage_key] == FIXTURE
    assert restored.conversation_id == CONVERSATION and session.commits == 1
    with pytest.raises(versions.VersionError, match="이미 가장 새 버전"):
        await versions.restore_version(session, storage, restored)


async def test_restore_reparses_version_that_was_not_ready(chain, monkeypatch):
    session, storage, (_, v2, _) = chain
    v2.status = "processing"
    scheduled = []
    monkeypatch.setattr(versions.documents, "schedule_parse", lambda *a: scheduled.append(a))
    restored = await versions.restore_version(session, storage, v2)
    assert (restored.status, restored.content) == ("processing", None)
    assert scheduled[0][0] == restored.id


async def test_restore_refuses_project_documents(chain):
    session, storage, (v1, _, _) = chain
    v1.project_id = uuid.uuid4()
    with pytest.raises(versions.VersionError, match="대화에서 만든 문서만"):
        await versions.restore_version(session, storage, v1)


# --- API ----------------------------------------------------------------------------------------


@pytest.fixture
def api(chain, monkeypatch):
    session, storage, docs = chain

    async def load(_session, document_id, _user):
        for doc in session.docs:
            if doc.id == document_id:
                return doc, None
        raise HTTPException(status_code=404, detail="Document not found")

    monkeypatch.setattr(documents_router, "_load", load)
    monkeypatch.setattr(documents_router, "get_storage", lambda: storage)
    return session, storage, docs


async def test_api_versions_changes_and_restore(api):
    session, _, (v1, v2, v3) = api
    user = SimpleNamespace(id=USER)
    listed = await documents_router.list_versions(v2.id, session, user)
    assert [(v.revision, v.created_by) for v in listed] == [(1, "user"), (2, "agent"), (3, "agent")]

    changes = await documents_router.document_changes(v2.id, session, user)
    assert changes.previous.id == v1.id and changes.unit == "paragraph" and changes.changed == 1

    restored = await documents_router.restore_document(v1.id, session, user)
    assert restored.revision == 4 and restored.source_document_id == v3.id
    with pytest.raises(HTTPException) as info:
        await documents_router.restore_document(restored.id, session, user)
    assert info.value.status_code == 409


async def test_api_changes_reports_deleted_file(api):
    session, storage, (_, v2, _) = api
    del storage.objects[v2.storage_key]
    with pytest.raises(HTTPException) as info:
        await documents_router.document_changes(v2.id, session, SimpleNamespace(id=USER))
    assert info.value.status_code == 410
