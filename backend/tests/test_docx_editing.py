"""DOCX 수정 로직(``app.documents.editing.docx``) 테스트.

실제 오피스 프로그램(LibreOffice)이 저장한 시험 문서(``fixtures/sales-report.docx``)와,
잘게 쪼개진 run·링크·필드·변경 추적처럼 특정 상황을 만든 작은 XML 조각을 함께 쓴다.
"""

import io
import zipfile
from pathlib import Path

import pytest
from lxml import etree

from app.documents.editing.docx import WALL, DocxDocument, W
from app.documents.editing.text import Edit, EditError

FIXTURE = Path(__file__).parent / "fixtures" / "sales-report.docx"

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
    'officedocument.wordprocessingml.document.{kind}+xml"/>'
    "</Types>"
)
_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)
_DOC_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rIdH" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/header" Target="header1.xml"/>'
    '<Relationship Id="rIdL" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/hyperlink" Target="https://example.com" TargetMode="External"/>'
    "</Relationships>"
)
_NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
)


def make_docx(body: str, *, header: str | None = None, macro: bool = False) -> bytes:
    """본문 XML 조각(``w:body`` 안)으로 최소한의 DOCX 를 만든다."""
    sect = '<w:sectPr><w:headerReference w:type="default" r:id="rIdH"/></w:sectPr>'
    document = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:document {_NS}><w:body>'
        f"{body}{sect if header is not None else ''}</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        kind = "main" if not macro else "macroEnabled.main"
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES.format(kind=kind))
        zf.writestr("_rels/.rels", _ROOT_RELS)
        zf.writestr("word/_rels/document.xml.rels", _DOC_RELS)
        zf.writestr("word/document.xml", document)
        if header is not None:
            zf.writestr("word/header1.xml", f"<w:hdr {_NS}>{header}</w:hdr>")
    return buffer.getvalue()


def p(*runs: str) -> str:
    return f"<w:p>{''.join(runs)}</w:p>"


def r(text: str, *, bold: bool = False) -> str:
    props = "<w:rPr><w:b/></w:rPr>" if bold else "<w:rPr/>"
    space = ' xml:space="preserve"' if text != text.strip() else ""
    return f"<w:r>{props}<w:t{space}>{text}</w:t></w:r>"


def edit(data: bytes, *edits: Edit) -> DocxDocument:
    doc = DocxDocument.from_bytes(data)
    doc.apply_edits(edits)
    return doc


def body_paragraphs(data: bytes) -> list[etree._Element]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        root = etree.fromstring(zf.read("word/document.xml"))
    return list(root.iter(f"{{{W}}}p"))


def runs_of(paragraph: etree._Element) -> list[tuple[str, bool]]:
    """문단의 run 마다 ``(글자, 굵게인지)``."""
    result = []
    for run in paragraph.iter(f"{{{W}}}r"):
        text = "".join(t.text or "" for t in run.iter(f"{{{W}}}t"))
        bold = run.find(f"{{{W}}}rPr/{{{W}}}b") is not None
        result.append((text, bold))
    return result


def canonical(element: etree._Element) -> bytes:
    return etree.tostring(element, method="c14n")


# --- 보기 ----------------------------------------------------------------------------------------


def test_fixture_view_numbers_paragraphs_tables_header_and_footer():
    doc = DocxDocument.from_bytes(FIXTURE.read_bytes())
    lines = doc.view(1, 100).text.split("\n")

    assert lines[0] == "[p1] (제목 1) 2024년 하반기 영업 보고"
    assert lines[1] == "[p2] 매출은 120억 원으로 전년보다 8% 늘었다."
    assert "[p5] (표1 1행 1열) 지역" in lines
    assert "[p9] (표1 2행 2열) 52억" in lines
    assert "[p18] 목표 매출은 140억 원이다.\t담당: 영업기획팀" in lines
    assert f"[p20] 로고: {WALL} (2024년 판)" in lines
    assert lines[-2] == "[p22] (머리글 1) 주식회사 예시 · 대외비"
    # 쪽 번호 필드는 결과 글자 그대로 보인다 (고칠 수는 없다).
    assert lines[-1] == "[p23] (바닥글 1) 영업기획팀 · 1 쪽"


def test_view_pages_by_paragraph_and_skips_empty_paragraphs():
    data = make_docx(p(r("하나")) + p() + p(r("둘")) + p(r("셋")))
    doc = DocxDocument.from_bytes(data)

    first = doc.view(1, 2)
    assert first.text == "[p1] 하나\n[p3] 둘"
    assert (first.first, first.last, first.total) == (1, 3, 4)
    rest = doc.view(first.last + 1, 2)
    assert rest.text == "[p4] 셋"
    assert doc.view(9, 2).text == ""


def test_view_respects_max_chars_but_shows_first_paragraph():
    data = make_docx(p(r("가" * 50)) + p(r("나" * 50)))
    view = DocxDocument.from_bytes(data).view(1, 10, max_chars=20)
    assert view.text == "[p1] " + "가" * 50
    assert view.last == 1


def test_nested_tables_are_numbered_in_order():
    inner = "<w:tbl><w:tr><w:tc>" + p(r("안쪽")) + "</w:tc></w:tr></w:tbl>"
    outer = (
        "<w:tbl><w:tr><w:tc>"
        + p(r("바깥"))
        + inner
        + "</w:tc><w:tc>"
        + p(r("옆"))
        + "</w:tc></w:tr></w:tbl>"
    )
    text = DocxDocument.from_bytes(make_docx(outer)).view(1, 10).text
    assert text.split("\n") == [
        "[p1] (표1 1행 1열) 바깥",
        "[p2] (표2 1행 1열) 안쪽",
        "[p3] (표1 1행 2열) 옆",
    ]


def test_tracked_paragraph_is_labelled():
    tracked = "<w:p><w:ins><w:r><w:t>새 글</w:t></w:r></w:ins><w:del><w:r><w:delText>옛 글</w:delText></w:r></w:del></w:p>"
    text = DocxDocument.from_bytes(make_docx(tracked)).view(1, 10).text
    assert text == "[p1] (변경 추적 기록 있음 · 고칠 수 없음) 새 글"


def test_breaks_tabs_and_markers():
    body = (
        "<w:p><w:r><w:t>가</w:t><w:tab/><w:t>나</w:t><w:br/><w:t>다</w:t></w:r>"
        '<w:bookmarkStart w:id="0" w:name="b"/><w:proofErr w:type="spellStart"/>'
        '<w:r><w:t>라</w:t></w:r><w:bookmarkEnd w:id="0"/></w:p>'
    )
    doc = DocxDocument.from_bytes(make_docx(body))
    assert doc.paragraphs[0].text == "가\t나\n다라"


# --- 패키지 --------------------------------------------------------------------------------------


def test_round_trip_without_edits_keeps_every_entry():
    raw = FIXTURE.read_bytes()
    out = DocxDocument.from_bytes(raw).to_bytes()
    with zipfile.ZipFile(io.BytesIO(raw)) as a, zipfile.ZipFile(io.BytesIO(out)) as b:
        assert [i.filename for i in a.infolist()] == [i.filename for i in b.infolist()]
        for info in a.infolist():
            assert a.read(info) == b.read(info.filename)


def test_edit_rewrites_only_the_changed_part_and_paragraph():
    raw = FIXTURE.read_bytes()
    out = edit(raw, Edit("120억", "135억")).to_bytes()
    with zipfile.ZipFile(io.BytesIO(raw)) as a, zipfile.ZipFile(io.BytesIO(out)) as b:
        changed = [i.filename for i in a.infolist() if a.read(i) != b.read(i.filename)]
        assert changed == ["word/document.xml"]
        original = b.read("word/document.xml")
        assert original.startswith(b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>')
    before, after = body_paragraphs(raw), body_paragraphs(out)
    assert len(before) == len(after)
    differ = [i for i, (x, y) in enumerate(zip(before, after)) if canonical(x) != canonical(y)]
    assert differ == [1]


def test_macro_document_is_rejected():
    with pytest.raises(EditError, match="macros"):
        DocxDocument.from_bytes(make_docx(p(r("가")), macro=True))


def test_damaged_file_is_rejected():
    with pytest.raises(EditError, match="damaged"):
        DocxDocument.from_bytes(b"not a zip")


# --- 바꾸기: 서식 ---------------------------------------------------------------------------------


def test_replace_across_runs_keeps_bold_part():
    data = make_docx(p(r("매출 "), r("120", bold=True), r("억 원")))
    out = edit(data, Edit("120억", "135억")).to_bytes()
    assert runs_of(body_paragraphs(out)[0]) == [("매출 ", False), ("135", True), ("억 원", False)]


def test_replace_spanning_runs_puts_new_text_in_first_run():
    data = make_docx(p(r("가나", bold=True), r("다라")))
    out = edit(data, Edit("나다", "XY")).to_bytes()
    # 비슷한 글자가 없어 통째로 바꾼다: 첫 run(굵게)에 새 글자, 뒤 run 에서는 지운다.
    assert runs_of(body_paragraphs(out)[0]) == [("가XY", True), ("라", False)]


def test_insert_follows_previous_run_and_start_follows_next():
    data = make_docx(p(r("굵게", bold=True), r(" 보통")))
    out = edit(data, Edit("굵게 보통", "굵게임 보통")).to_bytes()
    assert runs_of(body_paragraphs(out)[0]) == [("굵게임", True), (" 보통", False)]
    out = edit(data, Edit("굵게", "아주 굵게")).to_bytes()
    assert runs_of(body_paragraphs(out)[0])[0] == ("아주 굵게", True)


def test_delete_removes_emptied_runs_and_keeps_spaces():
    data = make_docx(p(r("앞 "), r("지울것", bold=True), r(" 뒤")))
    out = edit(data, Edit("앞 지울것 뒤", "앞  뒤")).to_bytes()
    paragraph = body_paragraphs(out)[0]
    assert runs_of(paragraph) == [("앞 ", False), (" 뒤", False)]
    for t in paragraph.iter(f"{{{W}}}t"):
        assert t.get("{http://www.w3.org/XML/1998/namespace}space") == "preserve"


def test_new_trailing_space_gets_preserve():
    data = make_docx(p(r("가나")))
    out = edit(data, Edit("가나", "가나 ")).to_bytes()
    t = next(body_paragraphs(out)[0].iter(f"{{{W}}}t"))
    assert t.text == "가나 "
    assert t.get("{http://www.w3.org/XML/1998/namespace}space") == "preserve"


def test_tab_insert_and_break_delete():
    body = "<w:p><w:r><w:t>가</w:t><w:br/><w:t>나</w:t></w:r></w:p>"
    doc = edit(make_docx(body), Edit("가\n나", "가\t나"))
    out = doc.to_bytes()
    run = next(body_paragraphs(out)[0].iter(f"{{{W}}}r"))
    assert [etree.QName(c).localname for c in run] == ["t", "tab", "t"]
    assert DocxDocument.from_bytes(out).paragraphs[0].text == "가\t나"


def test_adding_line_break_is_rejected():
    with pytest.raises(EditError, match="line break"):
        edit(make_docx(p(r("가나"))), Edit("가나", "가\n나"))


def test_bookmark_inside_range_is_kept():
    body = "<w:p>" + r("가나") + '<w:bookmarkStart w:id="1" w:name="m"/>' + r("다라") + "</w:p>"
    out = edit(make_docx(body), Edit("나다", "XY")).to_bytes()
    paragraph = body_paragraphs(out)[0]
    assert paragraph.find(f"{{{W}}}bookmarkStart") is not None
    assert DocxDocument.from_bytes(out).paragraphs[0].text == "가XY라"


# --- 바꾸기: 벽과 경계 ---------------------------------------------------------------------------


def test_hyperlink_inside_is_allowed_and_target_kept():
    body = (
        "<w:p>"
        + r("자세한 건 ")
        + '<w:hyperlink r:id="rIdL">'
        + r("위키")
        + "</w:hyperlink>"
        + r("를 본다")
        + "</w:p>"
    )
    out = edit(make_docx(body), Edit("위키", "안내 위키")).to_bytes()
    link = body_paragraphs(out)[0].find(f"{{{W}}}hyperlink")
    assert (
        link.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        == "rIdL"
    )
    assert "".join(t.text for t in link.iter(f"{{{W}}}t")) == "안내 위키"


def test_crossing_hyperlink_edge_is_rejected():
    body = (
        "<w:p>" + r("자세한 건 ") + '<w:hyperlink r:id="rIdL">' + r("위키") + "</w:hyperlink></w:p>"
    )
    with pytest.raises(EditError, match="hyperlink"):
        edit(make_docx(body), Edit("건 위키", "XYZW"))


def test_field_result_cannot_be_changed_but_text_around_it_can():
    body = (
        "<w:p>"
        + r("쪽 ")
        + '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        + '<w:r><w:instrText xml:space="preserve"> PAGE </w:instrText></w:r>'
        + '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        + r("3")
        + '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
        + r(" 끝")
        + "</w:p>"
    )
    data = make_docx(body)
    assert DocxDocument.from_bytes(data).paragraphs[0].text == "쪽 3 끝"
    with pytest.raises(EditError, match="field"):
        edit(data, Edit("3", "4"))
    out = edit(data, Edit("3 끝", "3 마지막")).to_bytes()
    assert DocxDocument.from_bytes(out).paragraphs[0].text == "쪽 3 마지막"


def test_simple_field_is_a_wall():
    body = '<w:p><w:fldSimple w:instr=" DATE ">' + r("2024-01-01") + "</w:fldSimple></w:p>"
    with pytest.raises(EditError, match="field"):
        edit(make_docx(body), Edit("2024", "2025"))


def test_drawing_can_stay_but_not_be_removed():
    body = "<w:p>" + r("로고: ") + "<w:r><w:drawing/></w:r>" + r(" (2024년 판)") + "</w:p>"
    data = make_docx(body)
    out = edit(data, Edit(f"{WALL} (2024년 판)", f"{WALL} (2025년 판)")).to_bytes()
    assert body_paragraphs(out)[0].find(f".//{{{W}}}drawing") is not None
    with pytest.raises(EditError, match="image"):
        edit(data, Edit(f"로고: {WALL}", "로고:"))
    with pytest.raises(EditError, match="Objects cannot be added"):
        edit(data, Edit("로고", f"로고{WALL}"))


def test_content_control_inside_is_editable():
    body = "<w:p><w:sdt><w:sdtPr/><w:sdtContent>" + r("홍길동") + "</w:sdtContent></w:sdt></w:p>"
    out = edit(make_docx(body), Edit("홍길동", "김철수")).to_bytes()
    assert DocxDocument.from_bytes(out).paragraphs[0].text == "김철수"


def test_tracked_paragraph_is_rejected_but_others_are_editable():
    tracked = "<w:p><w:ins><w:r><w:t>새 글</w:t></w:r></w:ins></w:p>"
    data = make_docx(tracked + p(r("보통 글")))
    with pytest.raises(EditError, match="tracked changes"):
        edit(data, Edit("새 글", "다른 글"))
    out = edit(data, Edit("보통", "평범한")).to_bytes()
    assert DocxDocument.from_bytes(out).paragraphs[1].text == "평범한 글"


# --- 바꾸기: 찾기 규칙 ---------------------------------------------------------------------------


def test_spanning_paragraphs_is_rejected_with_hint():
    with pytest.raises(EditError, match="more than one paragraph"):
        edit(make_docx(p(r("가나")) + p(r("다라"))), Edit("나\n다", "X"))


def test_paragraph_prefix_hint():
    with pytest.raises(EditError, match=r"\[p12\]"):
        edit(make_docx(p(r("가나"))), Edit("[p1] 가나", "X"))


def test_duplicate_requires_paragraph_or_replace_all():
    data = make_docx(p(r("서울")) + p(r("부산")) + p(r("서울")))
    with pytest.raises(EditError, match="2 times"):
        edit(data, Edit("서울", "대전"))
    doc = edit(data, Edit("서울", "대전", paragraph="p3"))
    assert [x.text for x in doc.paragraphs] == ["서울", "부산", "대전"]
    doc = DocxDocument.from_bytes(data)
    (change,) = doc.apply_edits([Edit("서울", "대전", replace_all=True)])
    assert (change.line, change.count) == (1, 2)
    assert [x.text for x in doc.paragraphs] == ["대전", "부산", "대전"]


def test_replace_all_within_one_paragraph():
    doc = edit(make_docx(p(r("가가 나 가가"))), Edit("가가", "다", replace_all=True))
    assert doc.paragraphs[0].text == "다 나 다"


@pytest.mark.parametrize(
    ("ref", "message"),
    [("x12", "look like"), ("p9", "does not exist"), ("0", "does not exist")],
)
def test_bad_paragraph_reference(ref, message):
    with pytest.raises(EditError, match=message):
        edit(make_docx(p(r("가"))), Edit("가", "나", paragraph=ref))


@pytest.mark.parametrize("ref", ["p1", "P1", "[p1]", "1"])
def test_paragraph_reference_forms(ref):
    doc = edit(make_docx(p(r("가"))), Edit("가", "나", paragraph=ref))
    assert doc.paragraphs[0].text == "나"


def test_edits_are_atomic():
    data = make_docx(p(r("가나")) + p(r("다라")))
    doc = DocxDocument.from_bytes(data)
    with pytest.raises(EditError) as info:
        doc.apply_edits([Edit("가나", "마바"), Edit("없는 글", "X")])
    assert info.value.index == 2
    assert [x.text for x in doc.paragraphs] == ["가나", "다라"]
    with zipfile.ZipFile(io.BytesIO(data)) as a, zipfile.ZipFile(io.BytesIO(doc.to_bytes())) as b:
        assert a.read("word/document.xml") == b.read("word/document.xml")


def test_later_edit_sees_earlier_edit():
    doc = edit(make_docx(p(r("가나"))), Edit("가나", "다라"), Edit("다라", "마바"))
    assert doc.paragraphs[0].text == "마바"


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [("", "x", "empty"), ("가", "가", "the same"), ("가", "\x01", "control")],
)
def test_invalid_edit(old, new, message):
    with pytest.raises(EditError, match=message):
        edit(make_docx(p(r("가"))), Edit(old, new))


def test_header_is_editable():
    data = make_docx(p(r("본문")), header=p(r("대외비")))
    doc = edit(data, Edit("대외비", "사내 한정"))
    assert doc.paragraphs[1].location == "머리글 1"
    out = doc.to_bytes()
    with zipfile.ZipFile(io.BytesIO(out)) as zf:
        assert "사내 한정".encode() in zf.read("word/header1.xml")


def test_fixture_mixed_edits():
    doc = edit(
        FIXTURE.read_bytes(),
        Edit("120억 원", "135억 원"),
        Edit("김민수", "최유진", replace_all=True),
        Edit("영업기획팀 ·", "영업전략팀 ·"),
    )
    text = DocxDocument.from_bytes(doc.to_bytes()).view(1, 100).text
    assert "[p2] 매출은 135억 원으로 전년보다 8% 늘었다." in text
    assert "[p10] (표1 2행 3열) 최유진" in text
    assert "[p23] (바닥글 1) 영업전략팀 · 1 쪽" in text


def test_header_table_location_includes_part():
    table = "<w:tbl><w:tr><w:tc>" + p(r("칸")) + "</w:tc></w:tr></w:tbl>"
    doc = DocxDocument.from_bytes(make_docx(p(r("본문")), header=table))
    assert doc.view(1, 10).text.split("\n")[-1] == "[p2] (머리글 1 · 표1 1행 1열) 칸"
