"""새 DOCX 만들기: Markdown → Word 대응, 기본 양식, 사용자 양식, 서비스·도구 결과."""

import asyncio
import io
import zipfile
from pathlib import Path

import pytest
from editing_fakes import CONVERSATION, USER, FakeSession, FakeStorage, install_tools, source_doc
from lxml import etree

from app.core.config import Settings
from app.documents.editing import service as editing
from app.documents.editing.docx import DocxDocument, W
from app.documents.editing.docx_create import build_docx
from app.documents.editing.service import create_new_document
from app.documents.editing.text import Edit, EditError
from app.tools import documents as document_tools

FIXTURE = Path(__file__).parent / "fixtures" / "sales-report.docx"
NS = {"w": W}
CONFIG = {"configurable": {"thread_id": str(CONVERSATION)}}


def parts(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def body(data: bytes) -> etree._Element:
    return etree.fromstring(parts(data)["word/document.xml"]).find("w:body", NS)


def lines(data: bytes) -> list[str]:
    return DocxDocument.from_bytes(data).view(1, 200).text.splitlines()


# --- Markdown → Word ----------------------------------------------------------------------------


def test_headings_paragraphs_and_inline_formatting():
    created = build_docx(
        "# 보고서\n\n매출은 **120억** *원*, ~~취소~~ `코드` [위키](https://example.com/w).\n## 결론",
        author="AI 에이전트",
    )
    assert lines(created.data) == [
        "[p1] (제목 1) 보고서",
        "[p2] 매출은 120억 원, 취소 코드 위키.",
        "[p3] (제목 2) 결론",
    ]
    assert created.title == "보고서" and created.paragraphs == 3
    paragraph = body(created.data).findall("w:p", NS)[1]
    assert paragraph.find(".//w:b", NS) is not None and paragraph.find(".//w:i", NS) is not None
    assert paragraph.find(".//w:strike", NS) is not None
    link = paragraph.find("w:hyperlink", NS)
    rels = parts(created.data)["word/_rels/document.xml.rels"].decode()
    rel_id = link.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
    assert f'Id="{rel_id}"' in rels and 'Target="https://example.com/w"' in rels


def test_fonts_language_and_page_setup():
    files = parts(build_docx("글", author="가").data)
    styles = files["word/styles.xml"].decode()
    assert 'w:eastAsia="맑은 고딕"' in styles and 'w:eastAsia="ko-KR"' in styles
    assert b'w:w="11906"' in files["word/document.xml"]
    core = files["docProps/core.xml"].decode()
    assert "<dc:creator>가</dc:creator>" in core


def test_nested_lists_and_numbering_restarts():
    markdown = "1. 하나\n   - 가\n   - 나\n2. 둘\n\n문단\n\n3. 셋부터\n4. 넷\n"
    data = build_docx(markdown, author="a").data
    assert lines(data) == [
        "[p1] (번호 목록) 하나",
        "[p2] (글머리표 목록 2수준) 가",
        "[p3] (글머리표 목록 2수준) 나",
        "[p4] (번호 목록) 둘",
        "[p5] 문단",
        "[p6] (번호 목록) 셋부터",
        "[p7] (번호 목록) 넷",
    ]
    numbering = etree.fromstring(parts(data)["word/numbering.xml"])
    children = [etree.QName(c).localname for c in numbering]
    assert children.index("num") > max(i for i, c in enumerate(children) if c == "abstractNum")
    starts = [e.get(f"{{{W}}}val") for e in numbering.iter(f"{{{W}}}startOverride")]
    assert starts == ["1", "3"]


def test_table_with_header_and_alignment():
    markdown = "| 지역 | 매출 |\n|:--|--:|\n| 서울 | **50억** |\n| 부산 |"
    created = build_docx(markdown, author="a")
    assert created.tables == 1
    assert lines(created.data) == [
        "[p1] (표1 1행 1열) 지역",
        "[p2] (표1 1행 2열) 매출",
        "[p3] (표1 2행 1열) 서울",
        "[p4] (표1 2행 2열) 50억",
        "[p5] (표1 3행 1열) 부산",
        "[p6] (표1 3행 2열) (빈 칸)",
    ]
    tree = body(created.data)
    table = tree.find("w:tbl", NS)
    assert table.find("w:tr/w:trPr/w:tblHeader", NS) is not None
    assert table.find("w:tr/w:tc", NS).find(".//w:jc", NS) is None  # 왼쪽 정렬은 기본값
    assert (
        table.findall("w:tr", NS)[1].findall("w:tc", NS)[1].find(".//w:jc", NS).get(f"{{{W}}}val")
        == "right"
    )
    assert tree[-2].tag == f"{{{W}}}p"  # 본문은 표로 끝나지 않는다 (마지막은 구역 설정)


def test_quote_code_rule_image_and_html():
    markdown = "> 인용\n\n```\n줄 하나\n\t들여쓴 줄\n```\n\n---\n\n![차트](a.png) 끝 <b>굵게</b>"
    created = build_docx(markdown, author="a")
    texts = [x.text for x in DocxDocument.from_bytes(created.data).paragraphs]
    assert texts == ["인용", "줄 하나⏎\t들여쓴 줄", "", "[차트] 끝 <b>굵게</b>"]
    assert created.notes and "image" in created.notes[0]
    paragraphs = body(created.data).findall("w:p", NS)
    assert paragraphs[0].find("w:pPr/w:pStyle", NS).get(f"{{{W}}}val") == "Quote"
    assert paragraphs[1].find("w:pPr/w:pStyle", NS).get(f"{{{W}}}val") == "Code"
    assert paragraphs[2].find("w:pPr/w:pBdr/w:bottom", NS) is not None


def test_created_document_can_be_edited_and_tracked():
    data = build_docx("# 제목\n\n매출은 120억 원이다.\n\n- 항목", author="a").data
    doc = DocxDocument.from_bytes(data)
    doc.apply_edits(
        [Edit("120억", "135억"), Edit(op="insert_paragraph", paragraph="p3", new_text="새 항목")],
        track=True,
    )
    assert lines(doc.to_bytes())[1:] == [
        "[p2] (변경 추적: AI 에이전트) 매출은 1{-20-}{+35+}억 원이다.",
        "[p3] (글머리표 목록) 항목",
        "[p4] (글머리표 목록) (변경 추적: AI 에이전트 · 문단 넣음·나눔 추적) {+새 항목+}",
    ]


@pytest.mark.parametrize("markdown", ["", "   \n"])
def test_empty_content_fails(markdown):
    with pytest.raises(EditError):
        build_docx(markdown, author="a")


def test_invalid_xml_characters_are_dropped():
    created = build_docx("제어\x07글자", author="a")
    assert lines(created.data) == ["[p1] 제어글자"]


# --- 양식 -----------------------------------------------------------------------------------------


def test_template_keeps_header_footer_and_replaces_body():
    template = FIXTURE.read_bytes()
    created = build_docx("# 새 보고서\n\n본문만 바뀐다.\n\n- 목록", author="a", template=template)
    before, after = parts(template), parts(created.data)
    for name in ("word/header1.xml", "word/footer1.xml"):
        if name in before:
            assert after[name] == before[name]
    view = lines(created.data)
    assert view[:3] == [
        "[p1] (제목 1) 새 보고서",
        "[p2] 본문만 바뀐다.",
        "[p3] (글머리표 목록) 목록",
    ]
    assert not any("120억" in line for line in view[:3])
    section_before = etree.fromstring(before["word/document.xml"]).find("w:body/w:sectPr", NS)
    section_after = body(created.data).find("w:sectPr", NS)
    assert etree.tostring(section_after) == etree.tostring(section_before)


def test_template_without_needed_styles_gets_them_added():
    template = FIXTURE.read_bytes()
    created = build_docx("> 인용\n\n```\n코드\n```", author="a", template=template)
    styles = parts(created.data)["word/styles.xml"].decode()
    assert 'w:styleId="Quote"' in styles or "quote" in styles.lower()
    assert "Code" in styles


def test_template_that_is_not_a_docx_fails():
    with pytest.raises(EditError, match="not a readable Word document"):
        build_docx("글", author="a", template=b"not a zip")


# --- 서비스·도구 ----------------------------------------------------------------------------------


def immediate_parse(monkeypatch):
    def schedule(*args):
        future = asyncio.get_running_loop().create_future()
        future.set_result(None)
        return future

    monkeypatch.setattr(editing.documents, "schedule_parse", schedule)


async def test_service_creates_docx_and_waits_for_parse(monkeypatch):
    immediate_parse(monkeypatch)
    session, storage = FakeSession(USER, later=["ready"]), FakeStorage()
    created = await create_new_document(
        session,
        storage,
        conversation_id=str(CONVERSATION),
        filename="보고서.docx",
        content="# 보고서\n\n| a | b |\n|---|---|\n| 1 | 2 |",
        max_bytes=100_000,
        parse_wait=5,
        author="검토 봇",
    )
    (doc,) = session.added
    assert (created.kind, created.status, created.tables) == ("docx", "ready", 1)
    assert doc.status == "processing" and doc.content is None
    assert doc.mime_type.endswith("wordprocessingml.document")
    data = storage.objects[doc.storage_key]
    assert "<dc:creator>검토 봇</dc:creator>" in parts(data)["docProps/core.xml"].decode()


async def test_template_only_for_docx():
    with pytest.raises(EditError, match="only works when creating a .docx"):
        await create_new_document(
            FakeSession(USER),
            FakeStorage(),
            conversation_id=str(CONVERSATION),
            filename="a.md",
            content="글",
            max_bytes=1000,
            template_document_id="x",
        )


async def test_create_tool_with_template(monkeypatch):
    immediate_parse(monkeypatch)
    template = source_doc(kind="docx", filename="양식.docx", mime_type="application/docx")
    session, storage = install_tools(monkeypatch, document_tools, template, FIXTURE.read_bytes())
    session.later = [USER, "ready"]
    tool = document_tools.make_create_document(Settings())
    message = await tool.ainvoke(
        {
            "name": "create_document",
            "args": {
                "filename": "주간 보고.docx",
                "content": "# 주간 보고\n\n![그림](x.png) 이번 주 요약.",
                "template_document_id": str(template.id),
            },
            "id": "call-1",
            "type": "tool_call",
        },
        config=CONFIG,
    )
    content = message.content
    assert "DOCX, 2 paragraphs, 0 tables" in content
    assert f"uses the template {template.id}" in content and "alt text" in content
    assert message.artifact["documents"][0]["kind"] == "docx"
    (doc,) = session.added
    view = lines(storage.objects[doc.storage_key])
    assert view[0] == "[p1] (제목 1) 주간 보고"


# --- E2E (fake 모델) ----------------------------------------------------------------------------


async def test_create_word_then_edit_in_the_same_conversation(monkeypatch):
    import json

    from langgraph.checkpoint.memory import InMemorySaver

    from app.agents import chat
    from app.orchestration import runtime
    from app.orchestration.events import ToolFinished

    immediate_parse(monkeypatch)
    session, storage = install_tools(monkeypatch, document_tools)
    session.found = USER
    graph = chat.build_graph(Settings(llm_provider="fake"), InMemorySaver())
    monkeypatch.setitem(runtime._graphs, "chat", graph)
    args = {"filename": "보고서.docx", "content": "# 보고서\n\n매출은 120억 원이다."}
    text = f"보고서 워드로 만들어줘 [[call:create_document]] {json.dumps(args, ensure_ascii=False)}"
    events = [e async for e in runtime.stream_reply(str(CONVERSATION), text)]
    finished = next(e for e in events if isinstance(e, ToolFinished))
    assert finished.status == "done" and finished.documents[0]["kind"] == "docx"
    (doc,) = session.added

    # 만든 Word 문서를 같은 대화에서 고친다 (묶음 1·2 기능 그대로)
    session.found = doc
    doc.status = "ready"
    edit = {"document_id": str(doc.id), "edits": [{"old_text": "120억", "new_text": "135억"}]}
    text = f"매출 고쳐줘 [[call:edit_document]] {json.dumps(edit, ensure_ascii=False)}"
    events = [e async for e in runtime.stream_reply(str(CONVERSATION), text)]
    finished = next(e for e in events if isinstance(e, ToolFinished))
    assert finished.status == "done"
    revised = session.added[1]
    assert revised.filename == "보고서 (수정 2).docx"
    assert lines(storage.objects[revised.storage_key])[1] == "[p2] 매출은 135억 원이다."
