"""문서 보기 패널 검증: 블록 고르기(본문과 같은 규칙), 근거 위치 강조, API 응답."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.routers import projects as projects_router
from app.models import Document, DocumentChunk
from app.projects import service as project_service
from app.projects.service import ProjectError, ViewBlock

PROJECT = SimpleNamespace(id=uuid.uuid4())


def _doc(kind="pdf") -> Document:
    return Document(
        id=uuid.uuid4(),
        project_id=PROJECT.id,
        filename="보고서.pdf",
        kind=kind,
        mime_type="application/pdf",
        size_bytes=10,
        sha256="0" * 64,
        storage_key="k",
        status="ready",
        page_count=2,
        created_at=datetime.now(UTC),
    )


def _row(kind, content, page=1, image_key=None, image_status=None, heading=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        kind=kind,
        page=page,
        heading=heading,
        content=content,
        image_key=image_key,
        image_status=image_status,
    )


def _chunk(doc, content, kind="text", page=1, element_ids=None, image_element_id=None):
    return DocumentChunk(
        id=uuid.uuid4(),
        document_id=doc.id,
        project_id=PROJECT.id,
        position=0,
        kind=kind,
        page_start=page,
        page_end=page,
        content=content,
        element_ids=element_ids,
        image_element_id=image_element_id,
    )


class FakeSession:
    """``document_view`` 가 쓰는 만큼만 흉내 낸다: 문서 → 청크 순서로 ``scalar``, 요소는 ``execute``."""

    def __init__(self, doc, rows, chunk=None):
        self.scalars = [doc, chunk]
        self.rows = rows

    async def scalar(self, statement):
        return self.scalars.pop(0)

    async def execute(self, statement):
        return self.rows


def _block(kind, content, page=1):
    return ViewBlock(uuid.uuid4(), kind, page, None, content, False, False)


async def test_view_uses_same_rules_as_document_content():
    doc = _doc("pdf")
    rows = [
        _row("text", "  1쪽 본문  "),
        _row("table", "| a | b |\n| --- | --- |\n| 1 | 2 |"),
        _row("text", "   "),
        _row("image", "막대그래프 설명", image_key="img/1", image_status="described"),
        _row("image", "그림 속 글자", page=2, image_key="img/2", image_status="fallback"),
    ]
    view = await project_service.document_view(FakeSession(doc, rows), PROJECT, doc.id)
    # PDF 표와 빈 본문은 뺀다 (문서 본문과 같은 규칙)
    assert [(b.kind, b.content, b.described, b.has_image) for b in view.blocks] == [
        ("text", "1쪽 본문", False, False),
        ("image", "막대그래프 설명", True, True),
        ("image", "그림 속 글자", False, True),
    ]
    assert view.highlight is None


async def test_view_keeps_tables_for_office_documents():
    doc = _doc("docx")
    rows = [_row("table", "| a |\n| --- |\n| 1 |", page=None)]
    view = await project_service.document_view(FakeSession(doc, rows), PROJECT, doc.id)
    assert [b.kind for b in view.blocks] == ["table"]


async def test_view_of_other_projects_document_is_404():
    with pytest.raises(ProjectError) as exc:
        await project_service.document_view(FakeSession(None, []), PROJECT, uuid.uuid4())
    assert exc.value.status_code == 404


async def test_view_highlights_chunk_of_this_document():
    doc = _doc()
    rows = [_row("text", "첫 문단"), _row("text", "둘째 문단")]
    chunk = _chunk(doc, "첫 문단\n\n둘째 문단", element_ids=[rows[0].id, rows[1].id])
    view = await project_service.document_view(
        FakeSession(doc, rows, chunk), PROJECT, doc.id, chunk.id
    )
    assert view.highlight.block_ids == [rows[0].id, rows[1].id]
    assert view.highlight.text is None  # 블록 전체가 청크라 따로 표시할 부분이 없다
    assert view.highlight.page == 1


def test_highlight_marks_part_of_a_split_block():
    long = _block("text", "가나다 라마바. 사아자 차카타. 파하.")
    doc = _doc()
    chunk = _chunk(doc, "사아자 차카타.", element_ids=[long.id])
    highlight = project_service._highlight(chunk, [long])
    assert highlight.block_ids == [long.id]
    assert highlight.text == "사아자 차카타."


def test_highlight_without_element_ids_matches_content_on_same_page():
    first, second, other_page = (
        _block("text", "첫 문단입니다."),
        _block("text", "둘째\n문단입니다."),
        _block("text", "첫 문단입니다.", page=2),
    )
    doc = _doc()
    # 예전 청크: 요소 ID 가 없고, 묶을 때 줄바꿈이 바뀌었다
    chunk = _chunk(doc, "첫 문단입니다.\n\n둘째 문단입니다.")
    highlight = project_service._highlight(chunk, [first, second, other_page])
    assert highlight.block_ids == [first.id, second.id]


def test_highlight_old_image_chunk_uses_image_element_id():
    image = _block("image", "그림 설명")
    doc = _doc()
    chunk = _chunk(doc, "그림 설명", kind="image", image_element_id=image.id)
    assert project_service._highlight(chunk, [image]).block_ids == [image.id]


def test_highlight_of_hidden_pdf_table_falls_back_to_page():
    text = _block("text", "본문", page=2)
    doc = _doc()
    chunk = _chunk(doc, "| a |\n| --- |\n| 1 |", kind="table", page=2, element_ids=[uuid.uuid4()])
    highlight = project_service._highlight(chunk, [text])
    assert highlight.block_ids == []
    assert highlight.page == 2


async def test_view_api_builds_image_urls(monkeypatch):
    doc = _doc()
    image = ViewBlock(uuid.uuid4(), "image", 1, None, "설명", True, True)
    text = ViewBlock(uuid.uuid4(), "text", 1, "1장", "본문", False, False)
    highlight = project_service.ViewHighlight([text.id], None, 1)

    async def own(session, project_id, user):
        return PROJECT

    async def document_view(session, project, document_id, chunk_id):
        assert chunk_id == highlight_chunk
        return project_service.DocumentViewData(doc, [text, image], highlight)

    highlight_chunk = uuid.uuid4()
    monkeypatch.setattr(projects_router, "_own", own)
    monkeypatch.setattr(projects_router.service, "document_view", document_view)
    out = await projects_router.view_project_document(
        PROJECT.id, doc.id, None, None, chunk_id=highlight_chunk
    )
    assert out.filename == "보고서.pdf"
    assert [b.image_url for b in out.blocks] == [
        None,
        f"/api/projects/{PROJECT.id}/images/{image.id}",
    ]
    assert out.highlight.block_ids == [text.id]


async def test_view_api_maps_not_found(monkeypatch):
    async def own(session, project_id, user):
        return PROJECT

    async def missing(session, project, document_id, chunk_id):
        raise ProjectError(404, "Document not found")

    monkeypatch.setattr(projects_router, "_own", own)
    monkeypatch.setattr(projects_router.service, "document_view", missing)
    with pytest.raises(HTTPException) as exc:
        await projects_router.view_project_document(PROJECT.id, uuid.uuid4(), None, None)
    assert exc.value.status_code == 404
