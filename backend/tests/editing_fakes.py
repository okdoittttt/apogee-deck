"""문서 수정·생성 테스트가 함께 쓰는 가짜 DB 세션·저장소와 원본 문서."""

import uuid
from types import SimpleNamespace

from app.core.storage import ObjectNotFoundError

CONVERSATION = uuid.uuid4()
USER = uuid.uuid4()


class FakeStorage:
    """메모리에 객체를 두는 첨부 저장소."""

    def __init__(self, objects=None):
        self.objects: dict[str, bytes] = dict(objects or {})
        self.content_types: dict[str, str] = {}

    async def put(self, key, data, content_type):
        self.objects[key] = data
        self.content_types[key] = content_type

    async def get(self, key):
        if key not in self.objects:
            raise ObjectNotFoundError(key)
        return self.objects[key]


class FakeSession:
    """조회하면 ``found`` 를 돌려주고, 추가한 행과 commit 횟수를 기록한다.

    ``later`` 를 주면 두 번째 조회부터 그 값을 차례로 돌려준다 (예: 수정본 파싱 뒤 상태 조회).
    더 새 수정본 조회(``source_document_id`` 로 찾기)는 따로 ``children`` 에서 차례로 돌려준다 (없으면 ``None``).
    ``async with SessionLocal() as session`` 처럼 쓸 수 있게 자기 자신을 돌려준다.
    """

    def __init__(self, found=None, later=(), children=()):
        self.found = found
        self.later = list(later)
        self.children = list(children)
        self.queries = []
        self.added = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def scalar(self, query):
        if "WHERE documents.source_document_id =" in str(query):
            return self.children.pop(0) if self.children else None
        self.queries.append(query)
        if len(self.queries) > 1 and self.later:
            return self.later.pop(0)
        return self.found

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


def source_doc(kind="txt", status="ready", revision=1, filename=None, **extra):
    """대화에 붙은 원본 문서 행 (``Document`` 대신 쓰는 가벼운 객체)."""
    doc_id = uuid.uuid4()
    fields = {
        "id": doc_id,
        "user_id": USER,
        "conversation_id": CONVERSATION,
        "project_id": None,
        "kind": kind,
        "status": status,
        "revision": revision,
        "filename": filename or f"notes.{kind}",
        "mime_type": f"text/{kind}; charset=utf-8",
        "storage_key": f"documents/{doc_id}",
        "created_by": "user",
    }
    return SimpleNamespace(**(fields | extra))


def install_tools(monkeypatch, tools_module, source=None, data=None, *, storage_enabled=True):
    """문서 도구 모듈의 DB 세션과 저장소를 가짜로 바꾼다.

    Args:
        monkeypatch: pytest monkeypatch.
        tools_module: ``app.tools.documents``.
        source: 조회 결과로 돌려줄 문서. ``None`` 이면 찾지 못한다.
        data: ``source`` 의 원본 파일 내용.
        storage_enabled: ``False`` 면 저장소가 없는 설정처럼 ``get_storage`` 가 ``None`` 을 돌려준다.

    Returns:
        ``(session, storage)``.
    """
    session = FakeSession(source)
    storage = FakeStorage({source.storage_key: data} if source is not None else {})
    monkeypatch.setattr(tools_module, "SessionLocal", lambda: session)
    monkeypatch.setattr(tools_module, "get_storage", lambda: storage if storage_enabled else None)
    return session, storage
