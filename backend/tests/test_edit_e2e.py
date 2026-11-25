"""문서 수정 E2E (fake 모델): 요청 → 에이전트 그래프 → 도구 호출 → 수정본 저장 → 화면용 실행 단계."""

import asyncio
import json
from pathlib import Path

from editing_fakes import CONVERSATION, install_tools, source_doc
from langgraph.checkpoint.memory import InMemorySaver
from lxml import etree

from app.agents import chat
from app.api.schemas import ToolStep
from app.core.config import Settings
from app.documents.editing import service as editing
from app.documents.editing.docx import DocxDocument, W
from app.documents.editing.text import Edit
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


# --- DOCX ----------------------------------------------------------------------------------------

DOCX = (Path(__file__).parent / "fixtures" / "sales-report.docx").read_bytes()


def install_docx(monkeypatch):
    source = source_doc(
        kind="docx",
        filename="sales-report.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    session, storage = install_tools(monkeypatch, document_tools, source, DOCX)

    def schedule(*args):
        # 수정본 파싱은 바로 끝난 것으로 하고, 뒤이은 상태 조회에 ready 를 돌려준다.
        future = asyncio.get_running_loop().create_future()
        future.set_result(None)
        session.later.append("ready")
        return future

    monkeypatch.setattr(editing.documents, "schedule_parse", schedule)
    return source, session, storage


async def test_docx_view_then_edit_keeps_formatting(monkeypatch):
    source, session, storage = install_docx(monkeypatch)

    events, _ = await run(
        monkeypatch, request("view_document_for_edit", {"document_id": str(source.id)})
    )
    viewed = next(e for e in events if isinstance(e, ToolFinished))
    assert viewed.status == "done" and "[p2] 매출은 120억 원으로" in viewed.output

    args = {
        "document_id": str(source.id),
        "edits": [
            {"old_text": "120억", "new_text": "135억"},
            {"old_text": "영업기획팀 ·", "new_text": "영업전략팀 ·", "paragraph": "p23"},
        ],
    }
    events, steps = await run(monkeypatch, request("edit_document", args))

    finished = next(e for e in events if isinstance(e, ToolFinished))
    assert finished.status == "done"
    (doc,) = session.added
    assert finished.documents[0]["filename"] == "sales-report (수정 2).docx"
    assert finished.documents[0]["kind"] == "docx"
    revised = DocxDocument.from_bytes(storage.objects[doc.storage_key])
    assert revised.paragraphs[1].text == "매출은 135억 원으로 전년보다 8% 늘었다."
    assert revised.paragraphs[22].text == "영업전략팀 · 1 쪽"
    # "135" 는 원본의 "120" 처럼 굵게 남는다.
    run_xml = [
        etree.tostring(r, encoding="unicode")
        for r in revised.paragraphs[1].element.iter(f"{{{W}}}r")
    ]
    assert any("<w:b/>" in x and ">135<" in x for x in run_xml)
    assert storage.objects[source.storage_key] == DOCX
    (step,) = [s for s in steps if s["type"] == "tool"]
    assert ToolStep.model_validate(step).documents[0].document_id == str(doc.id)


async def test_docx_edit_touching_image_is_refused(monkeypatch):
    source, session, _ = install_docx(monkeypatch)
    args = {
        "document_id": str(source.id),
        "edits": [{"old_text": "로고: ￼", "new_text": "로고:"}],
    }
    events, _ = await run(monkeypatch, request("edit_document", args))
    finished = next(e for e in events if isinstance(e, ToolFinished))
    assert finished.status == "error" and "image" in finished.output
    assert session.added == []


async def test_docx_structure_edit_request(monkeypatch):
    # 실제 사용에서 막혔던 요청: "덩어리진 문단을 나눠 줘" + 표에 행 추가 + 일부 실패.
    source, session, storage = install_docx(monkeypatch)
    args = {
        "document_id": str(source.id),
        "edits": [
            {"old_text": "원으로 전년보다", "new_text": "원으로\n전년보다"},
            {"op": "insert_row", "paragraph": "p16", "cells": ["광주", "9억", "정하늘"]},
            {"op": "delete_row", "paragraph": "p99"},
        ],
    }
    events, steps = await run(monkeypatch, request("edit_document", args))

    finished = next(e for e in events if isinstance(e, ToolFinished))
    assert finished.status == "done"
    assert "Not applied" in finished.output and "Edit 3:" in finished.output
    (doc,) = session.added
    revised = DocxDocument.from_bytes(storage.objects[doc.storage_key])
    texts = [x.text for x in revised.paragraphs]
    assert texts[1:3] == ["매출은 120억 원으로", "전년보다 8% 늘었다."]
    assert "(표1 5행 1열) 광주" in revised.view(1, 100).text
    (step,) = [s for s in steps if s["type"] == "tool"]
    assert ToolStep.model_validate(step).documents[0].document_id == str(doc.id)


async def test_docx_review_request_with_tracked_changes(monkeypatch):
    # "고친 곳을 Word 에서 확인할 수 있게 고쳐 주고, 의견은 메모로 남겨 줘."
    source, session, storage = install_docx(monkeypatch)
    args = {
        "document_id": str(source.id),
        "edits": [
            {"old_text": "120억", "new_text": "125억"},
            {"op": "comment", "old_text": "전년보다", "new_text": "비교 기준 연도를 적어 주세요."},
        ],
        "track_changes": True,
    }
    events, _ = await run(monkeypatch, request("edit_document", args))

    finished = next(e for e in events if isinstance(e, ToolFinished))
    assert finished.status == "done" and "tracked changes" in finished.output
    (doc,) = session.added
    revised = DocxDocument.from_bytes(storage.objects[doc.storage_key])
    text = revised.view(1, 100).text
    assert "12{-0-}{+5+}억" in text and "비교 기준 연도를 적어 주세요." in text


async def test_docx_find_then_format_request(monkeypatch):
    # "영업기획팀이 나온 곳을 찾아서 빨간 굵은 글씨로, 매출 숫자는 노란 형광펜으로."
    source, session, storage = install_docx(monkeypatch)
    view = {"document_id": str(source.id), "query": "영업기획팀"}
    events, _ = await run(monkeypatch, request("view_document_for_edit", view))
    viewed = next(e for e in events if isinstance(e, ToolFinished))
    assert viewed.status == "done" and "3 paragraphs contain '영업기획팀'" in viewed.output

    args = {
        "document_id": str(source.id),
        "edits": [
            {
                "op": "format",
                "old_text": "영업기획팀",
                "replace_all": True,
                "format": {"bold": True, "color": "red"},
            },
            {"op": "format", "paragraph": "p9", "format": {"highlight": "노랑"}},
        ],
    }
    events, _ = await run(monkeypatch, request("edit_document", args))
    finished = next(e for e in events if isinstance(e, ToolFinished))
    assert finished.status == "done"
    assert "1. formatted 3 places in 3 paragraphs (p18, p21, p23) as bold, color red" in (
        finished.output
    )
    (doc,) = session.added
    revised = DocxDocument.from_bytes(storage.objects[doc.storage_key])
    lines = revised.view(1, 100, formatting=True).text
    assert "(서식: 굵게, 글자색 빨강 '영업기획팀')" in lines
    assert "(서식: 굵게, 형광펜 노랑 '52억')" in lines  # 원래 굵은 칸


async def test_docx_bundle5_structure_request(monkeypatch):
    # "다음 분기 계획을 지역별 매출 앞으로 옮기고, 표에 목표 열을 넣고, 일정 표를 새로 추가하고,
    #  문의 문단은 줄을 바꿔 줘."
    source, session, storage = install_docx(monkeypatch)
    args = {
        "document_id": str(source.id),
        "edits": [
            {
                "op": "move_paragraph",
                "paragraph": "p17",
                "target": "p4",
                "position": "before",
                "whole_section": True,
            },
            {"op": "insert_column", "paragraph": "p6", "cells": ["목표", "55억", "33억", "20억"]},
            {
                "op": "insert_markdown",
                "paragraph": "p19",
                "new_text": "### 일정\n\n| 단계 | 기간 |\n|---|---|\n| 준비 | 1월 |",
            },
            {"old_text": "김민수 (내선", "new_text": "김민수⏎(내선"},
        ],
    }
    events, _ = await run(monkeypatch, request("edit_document", args))

    finished = next(e for e in events if isinstance(e, ToolFinished))
    assert finished.status == "done", finished.output
    assert "Not applied" not in finished.output
    assert "moved section '다음 분기 계획'" in finished.output
    assert "inserted a column after '매출' in 표1" in finished.output
    (doc,) = session.added
    revised = DocxDocument.from_bytes(storage.objects[doc.storage_key])
    view = revised.view(1, 100).text
    order = [line.split("] ", 1)[1] for line in view.splitlines() if line.startswith("[p")]
    assert order.index("(제목 2) 다음 분기 계획") < order.index("(제목 2) 지역별 매출")
    # 옮긴 섹션에 넣은 일정 표가 앞에 와서 표1, 매출 표는 표2 가 된다
    assert "(표1 2행 1열) 준비" in view
    assert "(표2 1행 3열) 목표" in view and "(표2 2행 3열) 55억" in view
    assert "문의: 영업기획팀 김민수⏎(내선 1234)" in view


async def test_docx_bundle6_tracked_structure_request(monkeypatch):
    # "변경 추적으로: 다음 분기 계획 섹션은 빼고, 표에 광주 행을 넣고, 신규 고객 문단은 제목으로 바꾸고,
    #  매출 문장에는 근거를 묻는 메모를 달아 줘."
    source, session, storage = install_docx(monkeypatch)
    args = {
        "document_id": str(source.id),
        "track_changes": True,
        "edits": [
            {"op": "delete_paragraph", "paragraph": "p17", "whole_section": True},
            {"op": "insert_row", "paragraph": "p14", "cells": ["광주", "12억", "최유진"]},
            {"op": "set_style", "paragraph": "p3", "style": "제목 2"},
            {"op": "comment", "old_text": "120억 원", "new_text": "근거 자료가 필요합니다"},
        ],
    }
    events, _ = await run(monkeypatch, request("edit_document", args))

    finished = next(e for e in events if isinstance(e, ToolFinished))
    assert finished.status == "done", finished.output
    assert "Not applied" not in finished.output
    assert "deleted section '다음 분기 계획'" in finished.output
    assert "as a tracked change" in finished.output
    (doc,) = session.added
    data = storage.objects[doc.storage_key]
    revised = DocxDocument.from_bytes(data)
    view = revised.view(1, 100).text
    assert "행 넣음 추적" in view and "문단 서식 바뀜" in view and "{-다음 분기 계획-}" in view
    comment = next(c for x in revised.paragraphs for c in x.comments)

    revised.apply_edits(
        [
            Edit(op="reject_changes", author="AI 에이전트", paragraph="p3"),
            Edit(op="accept_changes", author="AI 에이전트"),
            Edit(op="resolve_comment", comment=f"c{comment.id}", new_text="출처를 확인했습니다."),
        ]
    )
    final = DocxDocument.from_bytes(revised.to_bytes())
    texts = [x.text for x in final.paragraphs if x.text]
    assert "다음 분기 계획" not in texts and "광주" in texts
    assert next(x for x in final.paragraphs if x.text.startswith("신규 고객")).style is None
    (kept,) = [c for x in final.paragraphs for c in x.comments]
    assert kept.resolved and kept.replies[0].text == "출처를 확인했습니다."
    assert not any(x.tracked for x in final.paragraphs)


async def test_docx_bundle7_document_elements_request(monkeypatch):
    # "로고를 문서 끝에도 한 번 더 넣고, 매출 수치에 출처 각주를 달고, 바닥글에 쪽 번호,
    #  제목 아래에 목차를 넣고, 문서 제목 속성도 바꿔 줘."
    source, session, storage = install_docx(monkeypatch)
    args = {
        "document_id": str(source.id),
        "edits": [
            {"op": "insert_image", "paragraph": "p21", "image": "g1", "width": 3,
             "new_text": "회사 로고", "caption": "회사 로고"},
            {"op": "footnote", "paragraph": "p2", "old_text": "120억 원",
             "new_text": "출처: 2024년 사업보고서"},
            {"op": "set_footer", "new_text": "{쪽} / {전체 쪽}"},
            {"op": "insert_toc", "paragraph": "p1", "levels": 2},
            {"op": "set_properties", "properties": {"title": "2024 하반기 영업 보고"}},
        ],
    }  # fmt: skip
    events, _ = await run(monkeypatch, request("edit_document", args))

    finished = next(e for e in events if isinstance(e, ToolFinished))
    assert finished.status == "done", finished.output
    assert "Not applied" not in finished.output
    assert "inserted an image (3.0×1.5cm) after p21 with the caption '회사 로고'" in finished.output
    assert "Word asks to update fields" in finished.output
    (doc,) = session.added
    revised = DocxDocument.from_bytes(storage.objects[doc.storage_key])
    view = revised.view(1, 100).text
    assert "[각주 f1]" in view and "(각주 f1) 출처: 2024년 사업보고서" in view
    assert "(그림 g2 · 3.0×1.5cm · 대체 글자 '회사 로고')" in view
    assert "(바닥글 1) 1 / 1" in view and "(목차) 지역별 매출" in view
    assert revised.properties()["title"] == "2024 하반기 영업 보고"
