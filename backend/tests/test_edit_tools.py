"""문서 수정 도구: 줄 번호 보기, 수정본 만들기, 실패 표시, 화면으로 넘기는 artifact·실행 단계."""

import uuid

import pytest
from editing_fakes import CONVERSATION, install_tools, source_doc
from langchain_core.messages import ToolMessage

from app.api.schemas import ToolStep
from app.core.config import Settings
from app.core.errors import ToolError
from app.orchestration.events import StepRecorder, ToolFinished, ToolStarted, tool_documents
from app.orchestration.tooling import handle_tool_error
from app.tools import build_tools, registry
from app.tools import documents as document_tools
from app.tools.registry import ToolLabel

CONFIG = {"configurable": {"thread_id": str(CONVERSATION)}}
NOTES = "주간 회의록\n\n참석자: 김민수, 이서연\n장소: 본사 3층 회의실\n".encode()


def install(monkeypatch, *, kind="txt", data=NOTES, storage_enabled=True):
    source = source_doc(kind=kind, mime_type="text/plain; charset=utf-8")
    session, storage = install_tools(
        monkeypatch, document_tools, source, data, storage_enabled=storage_enabled
    )
    return source, session, storage


def call(name, args):
    return {"name": name, "args": args, "id": "call-1", "type": "tool_call"}


async def failure(tool, args):
    """도구가 ``ToolError`` 로 실패하는지 확인하고, 모델에게 갈 문구를 돌려준다.

    그래프의 ``ToolNode`` 는 ``ToolError`` 를 ``handle_tool_error`` 문구로 바꾸고 ``status="error"`` 로
    표시한다 (화면에 실패로 보인다).
    """
    with pytest.raises(ToolError) as info:
        await tool.ainvoke(args, config=CONFIG)
    return handle_tool_error(info.value)


# --- view_document_for_edit ---------------------------------------------------------------------


async def test_view_shows_numbered_lines(monkeypatch):
    source, _, _ = install(monkeypatch)
    tool = document_tools.make_view_document_for_edit(Settings())
    out = await tool.ainvoke({"document_id": str(source.id)}, config=CONFIG)
    assert f"Document: notes.txt (document_id: {source.id}, TXT, revision 1)" in out
    assert "Lines 1-4 of 4." in out
    assert "[L4] 장소: 본사 3층 회의실" in out
    assert "config" not in tool.tool_call_schema.model_json_schema()["properties"]


async def test_view_pages_long_documents(monkeypatch):
    text = "".join(f"line {i}\n" for i in range(1, 11))
    source, _, _ = install(monkeypatch, data=text.encode())
    tool = document_tools.make_view_document_for_edit(Settings(document_edit_view_max_lines=3))
    out = await tool.ainvoke({"document_id": str(source.id), "start_line": 4}, config=CONFIG)
    assert "Lines 4-6 of 10. Call again with start_line=7 to see more." in out
    assert "[L4] line 4" in out and "[L7]" not in out

    past = await tool.ainvoke({"document_id": str(source.id), "start_line": 50}, config=CONFIG)
    assert "There is no line 50; the document has 10 lines." in past


async def test_view_refuses_what_edit_refuses(monkeypatch):
    source, _, _ = install(monkeypatch, kind="pdf")
    tool = document_tools.make_view_document_for_edit(Settings())
    message = await failure(tool, {"document_id": str(source.id)})
    assert "Editing PDF documents is not supported yet" in message


# --- edit_document ------------------------------------------------------------------------------


async def test_edit_creates_revision_and_artifact(monkeypatch):
    source, session, storage = install(monkeypatch)
    tool = document_tools.make_edit_document(Settings())
    message = await tool.ainvoke(
        call(
            "edit_document",
            {
                "document_id": str(source.id),
                "edits": [
                    {"old_text": "본사 3층", "new_text": "본사 13층"},
                    {"old_text": "이서연\n", "new_text": "이서연, 홍길동\n"},
                ],
            },
        ),
        config=CONFIG,
    )
    assert isinstance(message, ToolMessage)
    (doc,) = session.added
    assert storage.objects[doc.storage_key] == (
        "주간 회의록\n\n참석자: 김민수, 이서연, 홍길동\n장소: 본사 13층 회의실\n".encode()
    )

    # 지난 도구 결과는 다음 턴에 앞 300자만 남는다: 새 문서 ID 와 이름이 맨 앞에 있어야 한다.
    head = message.content[:300]
    assert f"document_id: {doc.id}" in head and "notes (수정 2).txt" in head
    assert "1. line 4: '본사 3층' -> '본사 13층'" in message.content
    assert message.artifact == {
        "documents": [
            {
                "document_id": str(doc.id),
                "filename": "notes (수정 2).txt",
                "revision": 2,
                "source_document_id": str(source.id),
                "kind": "txt",
                "size_bytes": doc.size_bytes,
            }
        ]
    }


async def test_edit_replace_all_summary(monkeypatch):
    source, _, _ = install(monkeypatch, data=b"2024\n2024\n")
    tool = document_tools.make_edit_document(Settings())
    message = await tool.ainvoke(
        call(
            "edit_document",
            {
                "document_id": str(source.id),
                "edits": [{"old_text": "2024", "new_text": "2025", "replace_all": True}],
            },
        ),
        config=CONFIG,
    )
    assert "1. line 1, 2 places: '2024' -> '2025'" in message.content


async def test_failed_edit_is_an_error_and_saves_nothing(monkeypatch):
    source, session, storage = install(monkeypatch)
    tool = document_tools.make_edit_document(Settings())
    args = {"document_id": str(source.id), "edits": [{"old_text": "[L4] 장소", "new_text": "위치"}]}
    message = await failure(tool, args)
    assert "line number prefixes" in message
    assert message.endswith("No changes were saved.")
    assert session.added == [] and list(storage.objects) == [source.storage_key]


async def test_edit_without_storage(monkeypatch):
    source, _, _ = install(monkeypatch, storage_enabled=False)
    tool = document_tools.make_edit_document(Settings())
    args = {"document_id": str(source.id), "edits": [{"old_text": "a", "new_text": "b"}]}
    assert await failure(tool, args) == document_tools.NO_STORAGE


def test_edit_schema_describes_edits():
    schema = document_tools.make_edit_document(Settings()).tool_call_schema.model_json_schema()
    assert set(schema["properties"]) == {"document_id", "edits"}
    edit = schema["$defs"]["DocumentEdit"]
    assert set(edit["properties"]) == {"old_text", "new_text", "replace_all"}
    assert "[L12]" in edit["properties"]["old_text"]["description"]


def test_edit_tools_are_registered_in_documents_group():
    names = [t.name for t in build_tools(Settings(), ["view_document_for_edit", "edit_document"])]
    assert names == ["view_document_for_edit", "edit_document"]
    assert registry.tool_group("edit_document") == "documents"
    assert registry.TOOLS["edit_document"].label.done == "문서 수정함"
    # 도구 결정 모델이 "고쳐줘" 요청에도 문서 도구를 켜도록 묻는다 (결정 A)
    assert "change, fix or add to an attached file" in registry.GROUPS["documents"].question


# --- 화면으로 넘기기 (artifact → 실행 단계) -------------------------------------------------------

DOC_ID = str(uuid.uuid4())


def test_tool_documents_keeps_valid_entries_only():
    artifact = {
        "documents": [
            {"document_id": DOC_ID, "filename": "a (수정 2).md", "revision": 2, "kind": "md"},
            {"document_id": "../etc/passwd", "filename": "bad"},
            "not a dict",
        ]
    }
    assert tool_documents(artifact) == [
        {
            "document_id": DOC_ID,
            "filename": "a (수정 2).md",
            "revision": 2,
            "source_document_id": None,
            "kind": "md",
            "size_bytes": None,
        }
    ]
    assert tool_documents(None) == [] and tool_documents({"sources": []}) == []


def test_step_recorder_keeps_documents_and_schema_accepts_them():
    rec = StepRecorder()
    rec.apply(ToolStarted("t1", "edit_document", {}, ToolLabel("문서 수정하는 중", "문서 수정함")))
    documents = tool_documents({"documents": [{"document_id": DOC_ID, "filename": "a.txt"}]})
    rec.apply(ToolFinished("t1", "done", "done", documents=documents))
    (step,) = rec.to_json()
    assert step["documents"][0]["document_id"] == DOC_ID
    assert ToolStep.model_validate(step).documents[0].filename == "a.txt"

    # 문서가 없는 도구 단계에는 키를 넣지 않는다 (예전 메시지와 같은 모양)
    rec.apply(ToolStarted("t2", "get_current_time", {}, ToolLabel("a", "b")))
    rec.apply(ToolFinished("t2", "ok", "done"))
    assert "documents" not in rec.to_json()[1]
    assert ToolStep.model_validate(rec.to_json()[1]).documents == []
