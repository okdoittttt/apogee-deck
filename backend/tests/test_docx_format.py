"""묶음 4 (서식) 테스트: 보기에서 찾기, 글자 서식, 고쳐 쓰기에서 서식 지키기."""

from pathlib import Path

import pytest
from lxml import etree
from test_docx_editing import FIXTURE, STYLES, W, body_paragraphs, make_docx, p, r, runs_of
from test_docx_review import assert_valid
from test_edit_tools import CONFIG, install, install_docx

from app.core.config import Settings
from app.documents.editing.docx import DocxDocument
from app.documents.editing.text import Edit, EditError, TextFormat, numbered_lines
from app.documents.editing.text import apply_edits as text_apply
from app.tools import documents as document_tools

# --- 보기에서 찾기 (C2) ---------------------------------------------------------------------------


def fixture() -> DocxDocument:
    return DocxDocument.from_bytes(Path(FIXTURE).read_bytes())


def test_view_query_shows_only_matching_paragraphs_with_numbers():
    view = fixture().view(1, 100, query="영업기획팀")
    assert view.text.splitlines() == [
        "[p18] 목표 매출은 140억 원이다.\t담당: 영업기획팀",
        "[p21] 문의: 영업기획팀 김민수 (내선 1234)",
        "[p23] (바닥글 1) 영업기획팀 · 1 쪽",
    ]
    assert (view.matches, view.shown, view.last, view.total) == (3, 3, 23, 23)


def test_view_query_ignores_case_and_finds_table_cells():
    doc = fixture()
    assert doc.view(1, 100, query="  김민수 ").shown == 2  # 표 칸과 본문
    assert doc.view(1, 100, query="억").matches == 5
    view = doc.view(1, 100, query="없는 말")
    assert (view.text, view.matches, view.shown) == ("", 0, 0)


def test_view_query_pages_from_start():
    doc = fixture()
    first = doc.view(1, 2, query="억")
    assert first.text.splitlines()[0].startswith("[p2]") and first.shown == 2
    assert first.matches == 5 and first.last == 9  # p2, p9 를 보여주고 다음 문단에서 멈춤
    rest = doc.view(first.last + 1, 100, query="억")
    assert [line.split("]")[0] for line in rest.text.splitlines()] == [
        "[p12",
        "[p15",
        "[p18",
    ]
    assert rest.matches == 3 and rest.last == 23


def test_view_query_respects_max_chars():
    view = fixture().view(1, 100, max_chars=40, query="억")
    assert view.shown == 1 and view.matches == 5 and view.last == 8


def test_view_without_query_is_unchanged():
    view = fixture().view(1, 5)
    assert (view.first, view.last, view.matches, view.shown) == (1, 5, None, 5)


def test_numbered_lines_query():
    text = "Apple pie\nbanana\napple jam\ncherry\nAPPLE\n"
    view = numbered_lines(text, 1, 10, query="apple")
    assert view.text == "[L1] Apple pie\n[L3] apple jam\n[L5] APPLE"
    assert (view.matches, view.shown, view.last) == (3, 3, 5)
    paged = numbered_lines(text, 1, 1, query="apple")
    assert (paged.text, paged.matches, paged.last) == ("[L1] Apple pie", 3, 1)
    assert numbered_lines(text, 3, 10, query="apple").matches == 2
    assert numbered_lines(text, 1, 10, query="kiwi").matches == 0
    assert numbered_lines(text, 1, 2).matches is None


async def test_view_tool_query_on_docx(monkeypatch):
    source, _, _ = install_docx(monkeypatch, parse_status=None)
    tool = document_tools.make_view_document_for_edit(Settings(document_edit_view_max_lines=2))
    out = await tool.ainvoke({"document_id": str(source.id), "query": "억"}, config=CONFIG)
    assert (
        "5 paragraphs contain '억'. Showing the first 2; call again with the same query and "
        "start=10 to see more." in out
    )
    assert "[p2] 매출은" in out and "[p9] (표1 2행 2열) 52억" in out and "[p12] (표1" not in out
    assert out.endswith(document_tools.DOCX_VIEW_LEGEND)
    more = await tool.ainvoke(
        {"document_id": str(source.id), "query": "억", "start": 10}, config=CONFIG
    )
    assert "3 paragraphs contain '억' from paragraph 10." in more and "start=16" in more


async def test_view_tool_query_without_match(monkeypatch):
    source, _, _ = install(monkeypatch)
    tool = document_tools.make_view_document_for_edit(Settings())
    out = await tool.ainvoke({"document_id": str(source.id), "query": "예산"}, config=CONFIG)
    assert "No line contains '예산' (the document has 4 lines)." in out
    found = await tool.ainvoke({"document_id": str(source.id), "query": "김민수"}, config=CONFIG)
    assert "1 line contains '김민수'." in found and "[L3] 참석자: 김민수, 이서연" in found


# --- 글자 서식 (E1) -------------------------------------------------------------------------------

FORMAT_STYLES = (
    STYLES + '<w:style w:type="paragraph" w:styleId="Strong"><w:name w:val="Strong Para"/>'
    "<w:rPr><w:b/></w:rPr></w:style>"
    '<w:style w:type="paragraph" w:styleId="Child"><w:name w:val="Child Para"/>'
    '<w:basedOn w:val="Strong"/></w:style>'
    '<w:style w:type="character" w:styleId="Hyperlink"><w:name w:val="Hyperlink"/>'
    '<w:rPr><w:color w:val="0563C1"/><w:u w:val="single"/></w:rPr></w:style>'
)


def fmt(data: bytes, *edits: Edit, **options) -> tuple[DocxDocument, bytes]:
    doc = DocxDocument.from_bytes(data)
    doc.apply_edits(edits, **options)
    out = doc.to_bytes()
    assert_valid(out)
    return DocxDocument.from_bytes(out), out


def props_of(paragraph: etree._Element) -> list[tuple[str, list[str]]]:
    """문단의 run 마다 ``(글자, rPr 자식 이름(val 포함))``."""
    result = []
    for run in paragraph.iter(f"{{{W}}}r"):
        text = "".join(t.text or "" for t in run.iter(f"{{{W}}}t"))
        props = run.find(f"{{{W}}}rPr")
        names = []
        for child in props if props is not None else []:
            name = etree.QName(child).localname
            value = child.get(f"{{{W}}}val")
            names.append(f"{name}={value}" if value is not None else name)
        result.append((text, names))
    return result


def test_format_splits_runs_at_range_edges():
    data = make_docx(p(r("매출은 135억 원이다.")))
    _, out = fmt(data, Edit(op="format", old_text="135억", format=TextFormat(bold=True)))
    assert props_of(body_paragraphs(out)[0]) == [
        ("매출은 ", []),
        ("135억", ["b", "bCs"]),
        (" 원이다.", []),
    ]


def test_format_keeps_schema_order_and_existing_props():
    run = (
        '<w:r><w:rPr><w:rFonts w:ascii="Arial"/><w:sz w:val="28"/><w:lang w:val="ko-KR"/></w:rPr>'
        "<w:t>중요</w:t></w:r>"
    )
    data = make_docx(p(run))
    change = TextFormat(bold=True, italic=True, underline=True, color="빨강", highlight="노랑")
    _, out = fmt(data, Edit(op="format", paragraph="p1", format=change))
    assert props_of(body_paragraphs(out)[0]) == [
        (
            "중요",
            [
                "rFonts",
                "b",
                "bCs",
                "i",
                "iCs",
                "color=FF0000",
                "sz=28",
                "highlight=yellow",
                "u=single",
                "lang=ko-KR",
            ],
        )
    ]


def test_format_remove_direct_and_styled_bold():
    body = (
        p(r("굵은 글", bold=True))
        + '<w:p><w:pPr><w:pStyle w:val="Child"/></w:pPr>'
        + r("스타일로 굵은 글")
        + "</w:p>"
    )
    data = make_docx(body, styles=FORMAT_STYLES)
    _, out = fmt(
        data,
        Edit(op="format", paragraph="p1", format=TextFormat(bold=False)),
        Edit(op="format", paragraph="p2", format=TextFormat(bold=False)),
    )
    first, second = body_paragraphs(out)
    assert props_of(first) == [("굵은 글", [])]  # 스타일이 굵지 않으면 끄는 표시도 없다
    assert props_of(second) == [
        ("스타일로 굵은 글", ["b=0"])
    ]  # basedOn 사슬로 굵음 (bCs 는 스타일에 없음)


def test_format_remove_underline_and_color_from_link_style():
    link = (
        '<w:hyperlink r:id="rIdL"><w:r><w:rPr><w:rStyle w:val="Hyperlink"/></w:rPr>'
        "<w:t>링크</w:t></w:r></w:hyperlink>"
    )
    data = make_docx(p(r("앞 "), link), styles=FORMAT_STYLES)
    _, out = fmt(
        data, Edit(op="format", old_text="링크", format=TextFormat(underline=False, color="auto"))
    )
    assert props_of(body_paragraphs(out)[0])[1] == (
        "링크",
        ["rStyle=Hyperlink", "color=auto", "u=none"],
    )


def test_format_replace_all_and_result_note():
    data = make_docx(p(r("마감 오늘")) + p(r("내일 마감, 모레 마감")))
    doc = DocxDocument.from_bytes(data)
    (change,) = doc.apply_edits(
        [
            Edit(
                op="format",
                old_text="마감",
                replace_all=True,
                format=TextFormat(highlight="yellow"),
            )
        ]
    )
    assert change.count == 3 and change.places == (1, 2, 2)
    assert change.note == (
        "formatted 3 places in 2 paragraphs (p1, p2) as highlight yellow: '마감'"
    )
    marked = [t for t, names in props_of(body_paragraphs(doc.to_bytes())[1]) if names]
    assert marked == ["마감", "마감"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (None, "format is required"),
        (TextFormat(), "format is required"),
        (TextFormat(color="연분홍"), "unknown color"),
        (TextFormat(highlight="orange"), "Word highlights only come in these colors"),
    ],
)
def test_format_rejects_bad_requests(change, message):
    data = make_docx(p(r("글자")))
    with pytest.raises(EditError, match=message):
        DocxDocument.from_bytes(data).apply_edits(
            [Edit(op="format", old_text="글자", format=change)]
        )


def test_format_errors_for_no_change_ambiguity_and_empty_target():
    data = make_docx(p(r("굵게", bold=True)) + p(r("같은 말 같은 말")) + "<w:p/>")
    doc = DocxDocument.from_bytes(data)
    with pytest.raises(EditError, match="already has that formatting"):
        doc.apply_edits([Edit(op="format", old_text="굵게", format=TextFormat(bold=True))])
    with pytest.raises(EditError, match="appears 2 times"):
        doc.apply_edits([Edit(op="format", old_text="같은 말", format=TextFormat(bold=True))])
    with pytest.raises(EditError, match="has no text to format"):
        doc.apply_edits([Edit(op="format", paragraph="p3", format=TextFormat(bold=True))])
    with pytest.raises(EditError, match="set old_text"):
        doc.apply_edits([Edit(op="format", format=TextFormat(bold=True))])


def test_format_hex_color_and_strike_removes_double_strike():
    run = "<w:r><w:rPr><w:dstrike/></w:rPr><w:t>취소</w:t></w:r>"
    data = make_docx(p(run))
    _, out = fmt(
        data, Edit(op="format", paragraph="p1", format=TextFormat(strike=True, color="#c00000"))
    )
    assert props_of(body_paragraphs(out)[0]) == [("취소", ["strike", "color=C00000"])]


def test_format_skips_objects_outside_runs():
    math = '<m:oMath xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"/>'
    data = make_docx(p(r("값 "), math, r(" 끝")))
    _, out = fmt(data, Edit(op="format", paragraph="p1", format=TextFormat(italic=True)))
    assert [names for _, names in props_of(body_paragraphs(out)[0])] == [
        ["i", "iCs"],
        ["i", "iCs"],
    ]


def test_text_files_refuse_format():
    with pytest.raises(EditError, match="only works for Word documents"):
        text_apply("a b", [Edit(op="format", old_text="a", format=TextFormat(bold=True))])


# --- 서식 변경 추적 (E1) ----------------------------------------------------------------------------


def change_records(paragraph: etree._Element) -> list[etree._Element]:
    return list(paragraph.iter(f"{{{W}}}rPrChange"))


def test_tracked_format_keeps_old_props_in_rpr_change():
    data = make_docx(p(r("매출은 135억 원")))
    doc, out = fmt(
        data,
        Edit(op="format", old_text="135억", format=TextFormat(bold=True, color="red")),
        track=True,
        date="2026-11-24T10:00:00Z",
    )
    paragraph = body_paragraphs(out)[0]
    (record,) = change_records(paragraph)
    assert record.get(f"{{{W}}}author") == "AI 에이전트"
    assert record.get(f"{{{W}}}date") == "2026-11-24T10:00:00Z"
    assert [etree.QName(c).localname for c in record] == ["rPr"] and len(record[0]) == 0
    assert props_of(paragraph)[1] == ("135억", ["b", "bCs", "color=FF0000", "rPrChange"])
    view = doc.view(1, 10).text
    assert "(변경 추적: AI 에이전트 · 서식 바뀜)" in view


def test_tracked_format_reject_restores_and_accept_keeps():
    data = make_docx(p(r("앞 ", bold=False), r("굵은 말", bold=True)))
    _, out = fmt(
        data, Edit(op="format", old_text="굵은 말", format=TextFormat(bold=False)), track=True
    )
    _, rejected = fmt(out, Edit(op="reject_changes"))
    assert props_of(body_paragraphs(rejected)[0])[1] == ("굵은 말", ["b"])
    _, accepted = fmt(out, Edit(op="accept_changes"))
    assert props_of(body_paragraphs(accepted)[0])[1] == ("굵은 말", [])
    assert not change_records(body_paragraphs(accepted)[0])


def test_tracked_format_twice_keeps_the_first_old_props():
    data = make_docx(p(r("글자")))
    _, once = fmt(data, Edit(op="format", paragraph="p1", format=TextFormat(bold=True)), track=True)
    _, twice = fmt(
        once, Edit(op="format", paragraph="p1", format=TextFormat(italic=True)), track=True
    )
    paragraph = body_paragraphs(twice)[0]
    (record,) = change_records(paragraph)
    assert len(record[0]) == 0  # 처음 서식(아무것도 없음)을 그대로 기억한다
    _, rejected = fmt(twice, Edit(op="reject_changes"))
    assert props_of(body_paragraphs(rejected)[0]) == [("글자", [])]


def test_tracked_format_of_own_insertion_adds_no_record():
    data = make_docx(p(r("가 나")))
    _, inserted = fmt(data, Edit(old_text="가 나", new_text="가 새 나"), track=True)
    _, out = fmt(
        inserted, Edit(op="format", old_text="새", format=TextFormat(bold=True)), track=True
    )
    paragraph = body_paragraphs(out)[0]
    assert not change_records(paragraph)
    bold = [t for t, names in props_of(paragraph) if "b" in names]
    assert bold == ["새"]
    ins = paragraph.find(f".//{{{W}}}ins")
    assert "".join(t.text for t in ins.iter(f"{{{W}}}t")).strip() == "새"


def test_format_refuses_text_with_someone_elses_tracked_change():
    other = (
        '<w:r><w:rPr><w:b/><w:rPrChange w:id="1" w:author="김민수" w:date="2026-01-01T00:00:00Z">'
        "<w:rPr/></w:rPrChange></w:rPr><w:t>남의 서식</w:t></w:r>"
    )
    data = make_docx(p(other))
    with pytest.raises(EditError, match="not made by you"):
        DocxDocument.from_bytes(data).apply_edits(
            [Edit(op="format", paragraph="p1", format=TextFormat(italic=True))]
        )


# --- 서식 보기 -----------------------------------------------------------------------------------


def test_view_formatting_lists_direct_formatting():
    runs = (
        r("매출 ")
        + r("135억", bold=True)
        + '<w:r><w:rPr><w:color w:val="FF0000"/><w:highlight w:val="yellow"/></w:rPr>'
        "<w:t>주의</w:t></w:r>"
        + '<w:r><w:rPr><w:color w:val="123456"/></w:rPr><w:t>기타</w:t></w:r>'
        + '<w:r><w:rPr><w:b w:val="0"/><w:u w:val="none"/></w:rPr><w:t> 끝</w:t></w:r>'
    )
    doc = DocxDocument.from_bytes(make_docx(p(runs) + p(r("평범"))))
    assert doc.view(1, 10, formatting=True).text.splitlines() == [
        "[p1] 매출 135억주의기타 끝",
        "    (서식: 굵게 '135억' · 글자색 빨강, 형광펜 노랑 '주의' · 글자색 #123456 '기타')",
        "[p2] 평범",
    ]
    assert "서식" not in doc.view(1, 10).text


async def test_view_tool_formatting_adds_legend(monkeypatch):
    source, _, _ = install_docx(monkeypatch, parse_status=None)
    tool = document_tools.make_view_document_for_edit(Settings())
    out = await tool.ainvoke({"document_id": str(source.id), "formatting": True}, config=CONFIG)
    assert out.endswith(document_tools.FORMATTING_LEGEND)
    plain = await tool.ainvoke({"document_id": str(source.id)}, config=CONFIG)
    assert document_tools.FORMATTING_LEGEND not in plain


# --- 고쳐 쓰기에서 서식 지키기 (C3) ----------------------------------------------------------------

OLD = "지난 분기 매출은 135억 원으로 크게 늘었다."
NEW = "역대 최고 실적: 135억 원 달성"


def rewrite_body() -> str:
    return p(r("지난 분기 매출은 "), r("135억", bold=True), r(" 원으로 크게 늘었다."))


def test_rewrite_is_a_whole_replacement():
    from difflib import SequenceMatcher

    assert SequenceMatcher(None, OLD, NEW, autojunk=False).ratio() < 0.5


def test_rewrite_keeps_bold_piece_that_appears_in_new_text():
    doc = DocxDocument.from_bytes(make_docx(rewrite_body()))
    (change,) = doc.apply_edits([Edit(old_text=OLD, new_text=NEW)])
    assert "formatted piece" not in change.note
    runs = runs_of(body_paragraphs(doc.to_bytes())[0])
    assert "".join(t for t, _ in runs) == NEW
    assert [t for t, bold in runs if bold] == ["135억"]


def test_rewrite_keeps_link_and_no_longer_fails_at_its_edge():
    link = '<w:hyperlink r:id="rIdL"><w:r><w:t>영업팀 위키</w:t></w:r></w:hyperlink>'
    old = "자세한 내용은 영업팀 위키를 참고한다."
    new = "참고 자료: 영업팀 위키 (매주 갱신)"
    data = make_docx(p(r("자세한 내용은 "), link, r("를 참고한다.")))
    doc, out = fmt(data, Edit(old_text=old, new_text=new))
    assert doc.paragraphs[0].text == new
    (hyperlink,) = body_paragraphs(out)[0].iter(f"{{{W}}}hyperlink")
    assert "".join(t.text for t in hyperlink.iter(f"{{{W}}}t")) == "영업팀 위키"


def test_rewrite_reports_pieces_it_could_not_keep():
    body = p(r("먼저 "), r("가나다", bold=True), r(" 그리고 "), r("라마바", bold=True), r(" 끝"))
    doc = DocxDocument.from_bytes(make_docx(body))
    old = "먼저 가나다 그리고 라마바 끝"
    (change,) = doc.apply_edits([Edit(old_text=old, new_text="라마바와 가나다 순서 바꿈")])
    # 새 글에서 순서가 뒤집혔다. 앞에서부터 고르므로 가나다를 지키고, 그보다 앞에 나오는 라마바는 버린다.
    bold = [t for t, b in runs_of(body_paragraphs(doc.to_bytes())[0]) if b]
    assert bold == ["가나다"]
    assert "1 formatted piece (such as bold words or links) did not appear" in change.note


def test_rewrite_ignores_one_letter_pieces():
    body = p(r("값 "), r("1", bold=True), r(" 을 적은 아주 긴 옛 문장입니다"))
    doc = DocxDocument.from_bytes(make_docx(body))
    (change,) = doc.apply_edits(
        [Edit(old_text="값 1 을 적은 아주 긴 옛 문장입니다", new_text="새로 1 쓴 글")]
    )
    assert "formatted piece" not in change.note
    assert [t for t, b in runs_of(body_paragraphs(doc.to_bytes())[0]) if b] == []


def test_tracked_rewrite_keeps_bold_piece_outside_records():
    _, out = fmt(make_docx(rewrite_body()), Edit(old_text=OLD, new_text=NEW), track=True)
    paragraph = body_paragraphs(out)[0]
    plain = [run for run in paragraph.iter(f"{{{W}}}r") if run.getparent().tag == f"{{{W}}}p"]
    assert [(t, b) for t, b in runs_of(paragraph) if (t, b) == ("135억", True)]
    assert any(run.find(f"{{{W}}}rPr/{{{W}}}b") is not None for run in plain)
    _, accepted = fmt(out, Edit(op="accept_changes"))
    assert [t for t, b in runs_of(body_paragraphs(accepted)[0]) if b] == ["135억"]
    assert DocxDocument.from_bytes(accepted).paragraphs[0].text == NEW
