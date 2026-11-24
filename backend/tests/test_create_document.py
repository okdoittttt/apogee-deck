"""새 문서 만들기: 이름·내용 검사, CSV 의 BOM, 문서 행·저장소 객체, 도구 결과, E2E (fake 모델)."""

import hashlib
import json

import pytest
from editing_fakes import CONVERSATION, USER, FakeSession, FakeStorage, install_tools
from langgraph.checkpoint.memory import InMemorySaver

from app.agents import chat
from app.core.config import Settings
from app.core.errors import ToolError
from app.documents.editing.service import create_new_document
from app.documents.editing.text import BOM, EditError, check_new_csv
from app.documents.formats import DocumentKind
from app.documents.parser import parse
from app.models import Document
from app.orchestration import runtime
from app.orchestration.events import ToolFinished
from app.orchestration.tooling import handle_tool_error
from app.tools import build_tools, registry
from app.tools import documents as document_tools

CONFIG = {"configurable": {"thread_id": str(CONVERSATION)}}


async def create(filename, content, max_bytes=10_000, owner=USER):
    session, storage = FakeSession(owner), FakeStorage()
    created = await create_new_document(
        session,
        storage,
        conversation_id=str(CONVERSATION),
        filename=filename,
        content=content,
        max_bytes=max_bytes,
    )
    return created, session, storage


# --- 서비스 -------------------------------------------------------------------------------------


async def test_creates_markdown_without_bom():
    created, session, storage = await create("회의록.md", "# 회의록\r\n\r\n- 안건 1\n")
    (doc,) = session.added
    assert isinstance(doc, Document)
    saved = storage.objects[doc.storage_key]
    assert saved == "# 회의록\n\n- 안건 1\n".encode()  # BOM 없음, 줄바꿈은 \n
    assert (doc.kind, doc.status, doc.revision, doc.created_by) == ("md", "ready", 1, "agent")
    assert doc.source_document_id is None and doc.message_id is None and doc.project_id is None
    assert (doc.user_id, doc.conversation_id) == (USER, CONVERSATION)  # 대화 주인이 받을 수 있다
    assert doc.storage_key == f"documents/{doc.id}"
    assert doc.mime_type == "text/markdown; charset=utf-8"
    assert doc.sha256 == hashlib.sha256(saved).hexdigest() and doc.size_bytes == len(saved)
    assert session.commits == 1
    assert (created.filename, created.kind, created.line_count) == ("회의록.md", "md", 3)
    # DB 본문은 파서가 이 파일에서 만드는 글자와 같다 (read_document 가 읽는 글자)
    assert doc.content == parse(saved, DocumentKind.MD, "회의록.md", max_pages=10).content


async def test_csv_gets_bom_for_excel():
    content = '지점,주소,매출\n강남점,"서울, 강남구",1000\n'
    _, session, storage = await create("매출.csv", content)
    (doc,) = session.added
    assert storage.objects[doc.storage_key] == BOM + content.encode()
    assert doc.content == content  # 본문에는 BOM 이 없다
    assert doc.content == parse(BOM + content.encode(), DocumentKind.CSV, "a.csv", 10).content


async def test_txt_without_trailing_newline_counts_last_line():
    created, _, _ = await create("메모.txt", "첫 줄\n둘째 줄")
    assert created.line_count == 2


@pytest.mark.parametrize("filename", ["보고서.docx", "표.xlsx", "noext", ".md", "a.pdf"])
async def test_rejects_unsupported_names(filename):
    with pytest.raises(EditError, match=r"must end with \.txt, \.md or \.csv"):
        await create(filename, "내용")


async def test_markdown_extension_variant():
    created, _, _ = await create("README.markdown", "# hi\n")
    assert created.kind == "md"


async def test_filename_path_is_stripped():
    created, _, _ = await create("../../etc/메모.txt", "내용\n")
    assert created.filename == "메모.txt"


@pytest.mark.parametrize(
    ("content", "message"),
    [("", "empty"), ("  \n\n", "empty"), ("a\x00b", "NUL")],
)
async def test_rejects_bad_content(content, message):
    with pytest.raises(EditError, match=message):
        await create("a.txt", content)


async def test_rejects_too_large():
    with pytest.raises(EditError, match="larger than the limit of 5 bytes"):
        await create("a.txt", "123456", max_bytes=5)


async def test_csv_bom_counts_toward_size_limit():
    with pytest.raises(EditError, match="larger than the limit"):
        await create("a.csv", "a,b\n", max_bytes=5)  # 4바이트 + BOM 3바이트


async def test_rejects_conversation_without_owner():
    with pytest.raises(EditError, match="cannot store documents"):
        await create("a.txt", "내용", owner=None)


async def test_bad_csv_saves_nothing():
    session, storage = FakeSession(USER), FakeStorage()
    with pytest.raises(EditError, match="columns"):
        await create_new_document(
            session,
            storage,
            conversation_id=str(CONVERSATION),
            filename="a.csv",
            content="a,b\n1,2,3\n",
            max_bytes=1000,
        )
    assert session.added == [] and storage.objects == {}


# --- CSV 검사 -----------------------------------------------------------------------------------


def test_check_new_csv_accepts_quoted_values():
    check_new_csv('a,b\n"x, y","say ""hi"""\n"multi\nline",z\n')


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("a,b\n1,2,3\n", "line 2 has 3 columns, but the first row has 2"),
        ('a,b\n"x, y,1\n', "cannot be read"),
        ('a,b\n"x"y,1\n', "cannot be read"),
    ],
)
def test_check_new_csv_rejects_broken_tables(content, message):
    with pytest.raises(EditError, match=message):
        check_new_csv(content)


# --- 도구 ---------------------------------------------------------------------------------------


async def test_create_tool_result_and_artifact(monkeypatch):
    session, storage = install_tools(monkeypatch, document_tools)
    session.found = USER  # 대화 주인 조회 결과
    tool = document_tools.make_create_document(Settings())
    call = {
        "name": "create_document",
        "args": {"filename": "할일.md", "content": "# 할 일\n- 장보기\n"},
        "id": "c1",
        "type": "tool_call",
    }
    message = await tool.ainvoke(call, config=CONFIG)
    (doc,) = session.added
    head = message.content[:300]  # 지난 도구 결과는 다음 턴에 앞부분만 남는다
    assert f"document_id: {doc.id}" in head and "할일.md (MD, 2 lines" in head
    assert message.artifact == {
        "documents": [
            {
                "document_id": str(doc.id),
                "filename": "할일.md",
                "revision": 1,
                "source_document_id": None,
                "kind": "md",
                "size_bytes": doc.size_bytes,
            }
        ]
    }
    assert storage.objects[doc.storage_key] == "# 할 일\n- 장보기\n".encode()


async def test_create_tool_failure_is_error(monkeypatch):
    session, _ = install_tools(monkeypatch, document_tools)
    session.found = USER
    tool = document_tools.make_create_document(Settings())
    with pytest.raises(ToolError) as info:
        await tool.ainvoke({"filename": "보고서.docx", "content": "x"}, config=CONFIG)
    message = handle_tool_error(info.value)
    assert "must end with .txt, .md or .csv" in message
    assert message.endswith("No file was created.")


def test_create_tool_is_registered():
    assert [t.name for t in build_tools(Settings(), ["create_document"])] == ["create_document"]
    assert registry.tool_group("create_document") == "documents"
    assert registry.TOOLS["create_document"].label.done == "문서 만듦"
    assert "create a file" in registry.GROUPS["documents"].question
    assert "create_document" in chat.TOOL_NAMES


# --- E2E (fake 모델) ----------------------------------------------------------------------------


async def test_create_then_edit_in_the_same_conversation(monkeypatch):
    session, storage = install_tools(monkeypatch, document_tools)
    session.found = USER
    graph = chat.build_graph(Settings(llm_provider="fake"), InMemorySaver())
    monkeypatch.setitem(runtime._graphs, "chat", graph)
    args = {"filename": "매출.csv", "content": "지점,매출\n강남점,1000\n"}
    text = f"매출표 CSV 만들어줘 [[call:create_document]] {json.dumps(args, ensure_ascii=False)}"

    events = [e async for e in runtime.stream_reply(str(CONVERSATION), text)]

    finished = next(e for e in events if isinstance(e, ToolFinished))
    assert finished.status == "done"
    (document,) = finished.documents
    assert (document["filename"], document["revision"]) == ("매출.csv", 1)
    assert document["source_document_id"] is None  # 화면 칩: "새 문서"
    (doc,) = session.added
    assert storage.objects[doc.storage_key] == BOM + "지점,매출\n강남점,1000\n".encode()

    # 같은 대화에서 방금 만든 문서를 고친다: 수정본은 BOM 을 그대로 지킨다
    session.found = doc
    edit = {"document_id": str(doc.id), "edits": [{"old_text": "1000", "new_text": "1200"}]}
    text = f"강남점 매출 고쳐줘 [[call:edit_document]] {json.dumps(edit, ensure_ascii=False)}"
    events = [e async for e in runtime.stream_reply(str(CONVERSATION), text)]
    finished = next(e for e in events if isinstance(e, ToolFinished))
    assert finished.status == "done"
    revised = session.added[1]
    assert revised.filename == "매출 (수정 2).csv" and revised.source_document_id == doc.id
    assert storage.objects[revised.storage_key] == BOM + "지점,매출\n강남점,1200\n".encode()
