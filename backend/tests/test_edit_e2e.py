"""문서 수정 E2E (fake 모델): 요청 → 에이전트 그래프 → 도구 호출 → 수정본 저장 → 화면용 실행 단계."""

import json

from editing_fakes import CONVERSATION, install_tools, source_doc
from langgraph.checkpoint.memory import InMemorySaver

from app.agents import chat
from app.api.schemas import ToolStep
from app.core.config import Settings
from app.orchestration import runtime
from app.orchestration.events import Finished, StepRecorder, ToolFinished, ToolStarted
from app.tools import documents as document_tools

NOTES = "주간 회의록\r\n\r\n참석자: 김민수\r\n장소: 본사 3층 회의실\r\n".encode()


def request(tool: str, args: dict) -> str:
    # fake 모델은 [[call:<도구>]] {JSON} 표시를 보고 그 도구를 부른다 (app.llm.fake).
    return f"3번째 줄을 고쳐줘 [[call:{tool}]] {json.dumps(args, ensure_ascii=False)}"


async def run(monkeypatch, text: str):
    graph = chat.build_graph(Settings(llm_provider="fake"), InMemorySaver())
    monkeypatch.setitem(runtime._graphs, "chat", graph)
    events = [e async for e in runtime.stream_reply(str(CONVERSATION), text)]
    recorder = StepRecorder()
    for event in events:
        recorder.apply(event)
    return events, recorder.to_json()


async def test_edit_request_creates_downloadable_revision(monkeypatch):
    source = source_doc(kind="txt", filename="meeting-notes.txt")
    session, storage = install_tools(monkeypatch, document_tools, source, NOTES)
    args = {
        "document_id": str(source.id),
        "edits": [{"old_text": "김민수", "new_text": "김민수, 홍길동"}],
    }

    events, steps = await run(monkeypatch, request("edit_document", args))

    started = next(e for e in events if isinstance(e, ToolStarted))
    finished = next(e for e in events if isinstance(e, ToolFinished))
    assert started.name == "edit_document" and started.label.done == "문서 수정함"
    assert finished.status == "done"

    # 수정본: 원본의 CRLF 를 지킨 채 고친 곳만 바뀐다
    (doc,) = session.added
    assert storage.objects[doc.storage_key] == (
        "주간 회의록\r\n\r\n참석자: 김민수, 홍길동\r\n장소: 본사 3층 회의실\r\n".encode()
    )
    assert storage.objects[source.storage_key] == NOTES

    # 화면: 도구 단계에 수정본 정보가 남고(새로고침해도 API 로 그대로 나온다), 다운로드 ID 가 맞다
    (document,) = finished.documents
    assert document["document_id"] == str(doc.id)
    assert document["filename"] == "meeting-notes (수정 2).txt"
    (step,) = [s for s in steps if s["type"] == "tool"]
    assert ToolStep.model_validate(step).documents[0].document_id == str(doc.id)

    # 모델은 도구 결과(새 문서 ID)를 받고 답을 마친다
    done = events[-1]
    assert isinstance(done, Finished) and done.stop_reason == "end_turn"
    assert f"document_id: {doc.id}" in done.text


async def test_failed_edit_shows_error_step_without_file(monkeypatch):
    source = source_doc(kind="txt")
    session, storage = install_tools(monkeypatch, document_tools, source, NOTES)
    args = {"document_id": str(source.id), "edits": [{"old_text": "없는 글자", "new_text": "x"}]}

    events, steps = await run(monkeypatch, request("edit_document", args))

    finished = next(e for e in events if isinstance(e, ToolFinished))
    assert finished.status == "error" and finished.documents == []
    assert "old_text was not found" in finished.output
    (step,) = [s for s in steps if s["type"] == "tool"]
    assert step["status"] == "error" and "documents" not in step
    assert session.added == [] and list(storage.objects) == [source.storage_key]


async def test_view_then_edit_tools_are_offered_to_the_model(monkeypatch):
    source = source_doc(kind="md")
    install_tools(monkeypatch, document_tools, source, "# 제목\n본문\n".encode())

    events, _ = await run(
        monkeypatch, request("view_document_for_edit", {"document_id": str(source.id)})
    )

    finished = next(e for e in events if isinstance(e, ToolFinished))
    assert finished.status == "done" and "[L2] 본문" in finished.output
