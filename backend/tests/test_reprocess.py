"""프로젝트 문서 다시 처리 검증: 서비스(이전 결과 지우기), API(파싱 다시 시작)."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.sql import Delete, Update

from app.api.routers import projects as projects_router
from app.core.storage import ObjectNotFoundError
from app.models import Document
from app.projects import service as project_service
from app.projects.service import ProjectError

USER = SimpleNamespace(id=uuid.uuid4(), status="approved")
PROJECT = SimpleNamespace(id=uuid.uuid4())


def _doc(status="ready") -> Document:
    return Document(
        id=uuid.uuid4(),
        project_id=PROJECT.id,
        filename="보고서.pdf",
        kind="pdf",
        mime_type="application/pdf",
        size_bytes=10,
        sha256="0" * 64,
        storage_key="projects/p/documents/d",
        status=status,
        created_at=datetime.now(UTC),
    )


class FakeSession:
    """``reprocess_document`` 가 쓰는 만큼만 흉내 낸다."""

    def __init__(self, doc):
        self.doc, self.statements, self.committed = doc, [], False

    async def get(self, model, key, options=None):
        return self.doc if self.doc is not None and key == self.doc.id else None

    async def execute(self, statement):
        self.statements.append(statement)
        if isinstance(statement, Update):
            self.doc.status = "processing"

    async def commit(self):
        self.committed = True

    async def refresh(self, obj):
        pass


class FakeStorage:
    def __init__(self, data=b"%PDF original"):
        self.data = data

    async def get(self, key):
        if self.data is None:
            raise ObjectNotFoundError(key)
        return self.data


@pytest.fixture
def deleted(monkeypatch):
    removed: list = []

    async def element_image_keys(session, condition):
        return ["projects/p/images/d/1"]

    async def delete_objects(keys):
        removed.extend(keys)

    monkeypatch.setattr(project_service.documents, "element_image_keys", element_image_keys)
    monkeypatch.setattr(project_service.documents, "delete_objects", delete_objects)
    return removed


async def test_reprocess_clears_previous_results_and_returns_original(deleted):
    doc = _doc()
    session = FakeSession(doc)
    out, data = await project_service.reprocess_document(session, FakeStorage(), PROJECT, doc.id)

    assert out.status == "processing" and data == b"%PDF original"
    deletes = [s.table.name for s in session.statements if isinstance(s, Delete)]
    assert sorted(deletes) == ["document_chunks", "document_elements"]
    assert session.committed
    assert deleted == ["projects/p/images/d/1"]  # 이전 그림 원본도 지운다


@pytest.mark.parametrize(
    ("doc", "storage", "code"),
    [
        (None, FakeStorage(), 404),  # 이 프로젝트의 문서가 아님
        (_doc("analyzing"), FakeStorage(), 409),  # 처리 중
        (_doc(), FakeStorage(None), 404),  # 원본이 없음
    ],
)
async def test_reprocess_refuses(doc, storage, code, deleted):
    session = FakeSession(doc)
    with pytest.raises(ProjectError) as exc:
        await project_service.reprocess_document(
            session, storage, PROJECT, doc.id if doc else uuid.uuid4()
        )
    assert exc.value.status_code == code
    assert session.statements == [] and deleted == []  # 아무것도 지우지 않는다


@pytest.fixture
def api(monkeypatch):
    scheduled: list = []

    async def own(session, project_id, user):
        return PROJECT

    monkeypatch.setattr(projects_router, "_own", own)
    monkeypatch.setattr(projects_router, "get_storage", lambda: FakeStorage())
    monkeypatch.setattr(
        projects_router.documents, "schedule_parse", lambda *args: scheduled.append(args)
    )
    return scheduled


async def test_reprocess_api_restarts_parsing(monkeypatch, api):
    doc = _doc("processing")

    async def reprocess(session, storage, project, document_id):
        return doc, b"%PDF original"

    monkeypatch.setattr(projects_router.service, "reprocess_document", reprocess)
    out = await projects_router.reprocess_project_document(PROJECT.id, doc.id, None, USER)
    assert out.status == "processing"
    assert api == [(doc.id, b"%PDF original", "pdf", "보고서.pdf")]


async def test_reprocess_api_maps_errors(monkeypatch, api):
    async def busy(session, storage, project, document_id):
        raise ProjectError(409, "처리 중")

    monkeypatch.setattr(projects_router.service, "reprocess_document", busy)
    with pytest.raises(HTTPException) as exc:
        await projects_router.reprocess_project_document(PROJECT.id, uuid.uuid4(), None, USER)
    assert exc.value.status_code == 409
    monkeypatch.setattr(projects_router, "get_storage", lambda: None)
    with pytest.raises(HTTPException) as exc:
        await projects_router.reprocess_project_document(PROJECT.id, uuid.uuid4(), None, USER)
    assert exc.value.status_code == 503
    assert api == []


async def test_reprocess_all_skips_documents_that_cannot_be_reprocessed(monkeypatch, api):
    ok, missing = _doc("processing"), _doc()

    async def reprocess(session, storage, project, document_id):
        if document_id == missing.id:
            raise ProjectError(404, "원본 없음")
        return ok, b"data"

    class Session:
        async def scalars(self, statement):
            return [ok.id, missing.id]

    monkeypatch.setattr(projects_router.service, "reprocess_document", reprocess)
    out = await projects_router.reprocess_project_documents(PROJECT.id, Session(), USER)
    assert [d.id for d in out] == [ok.id]
    assert [args[0] for args in api] == [ok.id]
