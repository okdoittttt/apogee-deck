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
    '<Relationship Id="rIdS" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/styles" Target="styles.xml"/>'
    "</Relationships>"
)
_NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
)


STYLES = (
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/></w:style>'
)


def make_docx(
    body: str, *, header: str | None = None, macro: bool = False, styles: str | None = None
) -> bytes:
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
        if styles is not None:
            zf.writestr("word/styles.xml", f"<w:styles {_NS}>{styles}</w:styles>")
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


def test_line_break_splits_paragraph():
    body = (
        '<w:p><w:pPr><w:pStyle w:val="Body"/></w:pPr>'
        + r("첫 문장. ", bold=True)
        + r("둘째 문장.")
        + "</w:p>"
    )
    doc = edit(make_docx(body), Edit("첫 문장. 둘째", "첫 문장.\n둘째"))
    assert [x.text for x in doc.paragraphs] == ["첫 문장.", "둘째 문장."]
    out = doc.to_bytes()
    first, second = body_paragraphs(out)
    assert runs_of(first) == [("첫 문장.", True)]
    assert runs_of(second) == [("둘째 문장.", False)]
    for paragraph in (first, second):
        assert paragraph.find(f"{{{W}}}pPr/{{{W}}}pStyle").get(f"{{{W}}}val") == "Body"
    assert b"okdoit" not in out and b"ns0" not in out


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


# --- 수정마다 따로 성공·실패 (묶음 1) --------------------------------------------------------------


def test_apply_some_skips_failed_edits_only():
    doc = DocxDocument.from_bytes(make_docx(p(r("가나")) + p(r("다라"))))
    changes, failures = doc.apply_some(
        [Edit("가나", "마바"), Edit("없는 글", "X"), Edit("다라", "사아")]
    )
    assert [c.index for c in changes] == [1, 3]
    (failure,) = failures
    assert failure.index == 2 and "not found" in str(failure)
    assert [x.text for x in doc.paragraphs] == ["마바", "사아"]


def test_failed_replace_all_is_undone_as_a_whole():
    # 두 번째 문단(필드 결과 글자)에서 실패하면 첫 문단에서 바꾼 것도 되돌린다.
    field = f'<w:fldSimple w:instr="PAGE">{r("가나")}</w:fldSimple>'
    doc = DocxDocument.from_bytes(make_docx(p(r("가나")) + p(field)))
    changes, failures = doc.apply_some([Edit("가나", "X", replace_all=True)])
    assert changes == [] and len(failures) == 1
    assert [x.text for x in doc.paragraphs] == ["가나", "가나"]


def test_paragraph_marks_are_not_saved():
    doc = edit(make_docx(p(r("가")) + p(r("나"))), Edit("가", "다", paragraph="p1"))
    assert b"okdoit" not in doc.to_bytes()
    assert all("okdoit-paragraph" not in x.element.attrib for x in doc.paragraphs)


def test_unknown_op_fails():
    doc = DocxDocument.from_bytes(make_docx(p(r("가"))))
    _, (failure,) = doc.apply_some([Edit(op="shuffle", paragraph="p1")])
    assert "unknown op 'shuffle'" in str(failure)


# --- 문단 나누기·합치기, 빈 문단 채우기 (묶음 1, A1) ---------------------------------------------


def test_split_in_the_middle_of_a_run_keeps_formatting_on_both_sides():
    doc = edit(make_docx(p(r("가나다라", bold=True))), Edit("나다", "나\n다"))
    first, second = body_paragraphs(doc.to_bytes())
    assert runs_of(first) == [("가나", True)] and runs_of(second) == [("다라", True)]


def test_split_moves_section_break_to_last_piece():
    body = "<w:p><w:pPr><w:sectPr/></w:pPr>" + r("가 나 다") + "</w:p>"
    doc = edit(make_docx(body), Edit("가 나 다", "가\n나\n다"))
    pieces = body_paragraphs(doc.to_bytes())
    assert [x.find(f"{{{W}}}pPr/{{{W}}}sectPr") is not None for x in pieces] == [
        False,
        False,
        True,
    ]


def test_blank_lines_count_as_one_split():
    doc = edit(make_docx(p(r("가나"))), Edit("가나", "가\n\n\n나"))
    assert [x.text for x in doc.paragraphs] == ["가", "나"]


@pytest.mark.parametrize("new", ["가나\n", "\n가나", "가\n \n나"])
def test_split_that_leaves_an_empty_paragraph_fails(new):
    doc = DocxDocument.from_bytes(make_docx(p(r("가나"))))
    _, (failure,) = doc.apply_some([Edit("가나", new)])
    assert "empty paragraph" in str(failure)
    assert [x.text for x in doc.paragraphs] == ["가나"]


def test_split_inside_hyperlink_fails():
    link = f'<w:hyperlink r:id="rIdL">{r("링크 글자")}</w:hyperlink>'
    doc = DocxDocument.from_bytes(make_docx(p(r("앞 "), link)))
    _, (failure,) = doc.apply_some([Edit("링크 글자", "링크\n글자")])
    assert "hyperlink" in str(failure)


def test_paragraph_numbers_refer_to_the_view_before_the_call():
    doc = DocxDocument.from_bytes(make_docx(p(r("가나")) + p(r("다"))))
    changes, failures = doc.apply_some([Edit("가나", "가\n나"), Edit("다", "라", paragraph="p2")])
    assert failures == []
    assert [x.text for x in doc.paragraphs] == ["가", "나", "라"]
    assert changes[0].note == "split into 2 paragraphs: '가' | '나'" and changes[1].line == 2


def test_replace_all_skips_only_links_that_show_an_address():
    address = f'<w:hyperlink r:id="rIdL">{r("https://docs.openwebui.com/x")}</w:hyperlink>'
    named = f'<w:hyperlink r:id="rIdM">{r("openwebui 문서")}</w:hyperlink>'
    data = make_docx(
        p(r("소개, "), address)
        + p(r("주소 www.openwebui.com 참고, "), named)
        + p(r("openwebui 와 openwebui"))
    )
    doc = DocxDocument.from_bytes(data)
    (change,) = doc.apply_edits([Edit("openwebui", "오픈웹UI", replace_all=True)])
    assert [x.text for x in doc.paragraphs] == [
        "소개, https://docs.openwebui.com/x",
        "주소 www.오픈웹UI.com 참고, 오픈웹UI 문서",
        "오픈웹UI 와 오픈웹UI",
    ]
    assert (change.count, change.skipped, change.places) == (4, 1, (2, 2, 3, 3))
    assert change.in_urls == (2,)


def test_address_link_text_changes_when_old_text_is_the_address():
    link = f'<w:hyperlink r:id="rIdL">{r("https://docs.오픈웹UI.com/x")}</w:hyperlink>'
    doc = DocxDocument.from_bytes(make_docx(p(r("링크 "), link)))
    edits = [Edit("https://docs.오픈웹UI.com", "https://docs.openwebui.com", replace_all=True)]
    doc.apply_edits(edits)
    assert doc.paragraphs[0].text == "링크 https://docs.openwebui.com/x"


def test_replace_all_only_inside_address_links_fails():
    link = f'<w:hyperlink r:id="rIdL">{r("https://openwebui.com")}</w:hyperlink>'
    doc = DocxDocument.from_bytes(make_docx(p(r("링크 "), link)))
    _, (failure,) = doc.apply_some([Edit("openwebui", "X", replace_all=True)])
    assert "hyperlinks that show a web address" in str(failure)


def test_split_note_shows_each_piece():
    doc = DocxDocument.from_bytes(make_docx(p(r("첫 문장입니다. " + "둘째 문장" * 10))))
    (change,) = doc.apply_edits([Edit("입니다. 둘째", "입니다.\n둘째")])
    assert change.note.startswith("split into 2 paragraphs: '첫 문장입니다.' | '둘째 문장")


def test_merge_joins_with_a_space_and_keeps_formatting():
    doc = edit(make_docx(p(r("가.")) + p(r("나", bold=True))), Edit(op="merge", paragraph="p1"))
    assert [x.text for x in doc.paragraphs] == ["가. 나"]
    (paragraph,) = body_paragraphs(doc.to_bytes())
    assert runs_of(paragraph) == [("가. ", False), ("나", True)]


def test_merge_does_not_add_a_second_space():
    doc = edit(make_docx(p(r("가. ")) + p(r("나"))), Edit(op="merge", paragraph="p1"))
    assert doc.paragraphs[0].text == "가. 나"


def test_merge_skips_bookmarks_between_paragraphs():
    body = p(r("가")) + '<w:bookmarkStart w:id="1" w:name="m"/>' + p(r("나"))
    doc = edit(make_docx(body), Edit(op="merge", paragraph="p1"))
    assert [x.text for x in doc.paragraphs] == ["가 나"]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            p(r("가")) + "<w:tbl><w:tr><w:tc>" + p(r("칸")) + "</w:tc></w:tr></w:tbl>",
            "no paragraph",
        ),
        (
            "<w:tbl><w:tr><w:tc>" + p(r("칸")) + "</w:tc></w:tr></w:tbl>" + p(r("가")),
            "no paragraph",
        ),
        ("<w:p><w:pPr><w:sectPr/></w:pPr>" + r("가") + "</w:p>" + p(r("나")), "section"),
        (p(r("가")) + "<w:p><w:ins>" + r("나") + "</w:ins></w:p>", "tracked"),
    ],
)
def test_merge_failures(body, message):
    doc = DocxDocument.from_bytes(make_docx(body))
    _, (failure,) = doc.apply_some([Edit(op="merge", paragraph="p1")])
    assert message in str(failure)


def test_merged_paragraph_cannot_be_referenced_again():
    doc = DocxDocument.from_bytes(make_docx(p(r("가")) + p(r("나"))))
    _, (failure,) = doc.apply_some(
        [Edit(op="merge", paragraph="p1"), Edit("나", "다", paragraph="p2")]
    )
    assert "removed by an earlier edit" in str(failure)


def test_fill_empty_table_cell_uses_paragraph_mark_formatting():
    empty = "<w:p><w:pPr><w:rPr><w:b/></w:rPr></w:pPr></w:p>"
    table = f"<w:tbl><w:tr><w:tc>{p(r('지역'))}</w:tc><w:tc>{empty}</w:tc></w:tr></w:tbl>"
    doc = edit(make_docx(table), Edit("", "18억", paragraph="p2"))
    assert doc.view(1, 10).text.split("\n")[-1] == "[p2] (표1 1행 2열) 18억"
    cell = body_paragraphs(doc.to_bytes())[1]
    assert runs_of(cell) == [("18억", True)]


def test_fill_requires_an_empty_paragraph():
    doc = DocxDocument.from_bytes(make_docx(p(r("가"))))
    _, (failure,) = doc.apply_some([Edit("", "나", paragraph="p1")])
    assert "is not empty" in str(failure)
    _, (failure,) = doc.apply_some([Edit("", "나")])
    assert "also set paragraph" in str(failure)


def test_fixture_split_and_merge():
    original = DocxDocument.from_bytes(FIXTURE.read_bytes()).paragraphs[1].text
    doc = edit(FIXTURE.read_bytes(), Edit("원으로 전년보다", "원으로\n전년보다"))
    texts = [x.text for x in DocxDocument.from_bytes(doc.to_bytes()).paragraphs]
    assert texts[1:3] == ["매출은 120억 원으로", "전년보다 8% 늘었다."]
    merged = edit(doc.to_bytes(), Edit(op="merge", paragraph="p2"))
    assert merged.paragraphs[1].text == original


# --- 문단 넣기·지우기·옮기기, 목록 (묶음 1, A2·A4) -----------------------------------------------


def heading(text: str, level: int = 1) -> str:
    return f'<w:p><w:pPr><w:pStyle w:val="Heading{level}"/></w:pPr>{r(text)}</w:p>'


def listed(text: str, level: int = 0) -> str:
    numbering = f'<w:numPr><w:ilvl w:val="{level}"/><w:numId w:val="3"/></w:numPr>'
    return f"<w:p><w:pPr>{numbering}</w:pPr>{r(text)}</w:p>"


def test_insert_paragraph_after_copies_paragraph_and_run_formatting():
    body = '<w:p><w:pPr><w:jc w:val="both"/></w:pPr>' + r("본문", bold=True) + "</w:p>"
    doc = edit(make_docx(body), Edit(op="insert_paragraph", paragraph="p1", new_text="새 문단"))
    assert [x.text for x in doc.paragraphs] == ["본문", "새 문단"]
    first, second = body_paragraphs(doc.to_bytes())
    assert canonical(first.find(f"{{{W}}}pPr")) == canonical(second.find(f"{{{W}}}pPr"))
    assert runs_of(second) == [("새 문단", True)]


def test_insert_paragraph_before_and_several_lines():
    doc = edit(
        make_docx(p(r("셋"))),
        Edit(op="insert_paragraph", paragraph="p1", position="before", new_text="하나\n\n둘"),
    )
    assert [x.text for x in doc.paragraphs] == ["하나", "둘", "셋"]


def test_insert_after_heading_follows_next_body_paragraph():
    body = heading("제목") + '<w:p><w:pPr><w:ind w:left="200"/></w:pPr>' + r("본문") + "</w:p>"
    doc = edit(
        make_docx(body, styles=STYLES),
        Edit(op="insert_paragraph", paragraph="p1", new_text="새 본문"),
    )
    assert [(x.text, x.style) for x in doc.paragraphs] == [
        ("제목", "제목 1"),
        ("새 본문", None),
        ("본문", None),
    ]
    inserted = body_paragraphs(doc.to_bytes())[1]
    assert inserted.find(f"{{{W}}}pPr/{{{W}}}ind") is not None


def test_insert_after_last_heading_is_plain_paragraph():
    doc = edit(
        make_docx(heading("끝 제목"), styles=STYLES),
        Edit(op="insert_paragraph", paragraph="p1", new_text="본문"),
    )
    assert doc.paragraphs[1].style is None
    assert body_paragraphs(doc.to_bytes())[1].find(f"{{{W}}}pPr") is None


@pytest.mark.parametrize(("style", "label"), [("제목 2", "제목 2"), ("Heading 1", "제목 1")])
def test_insert_with_style(style, label):
    doc = edit(
        make_docx(p(r("본문", bold=True)), styles=STYLES),
        Edit(op="insert_paragraph", paragraph="p1", new_text="새 장", style=style),
    )
    assert doc.paragraphs[1].style == label
    assert runs_of(body_paragraphs(doc.to_bytes())[1]) == [("새 장", False)]


def test_insert_with_body_style_removes_heading():
    doc = edit(
        make_docx(heading("제목"), styles=STYLES),
        Edit(op="insert_paragraph", paragraph="p1", position="before", new_text="앞", style="본문"),
    )
    assert [x.style for x in doc.paragraphs] == [None, "제목 1"]


def test_insert_with_unknown_style_fails():
    doc = DocxDocument.from_bytes(make_docx(p(r("본문")), styles=STYLES))
    _, (failure,) = doc.apply_some(
        [Edit(op="insert_paragraph", paragraph="p1", new_text="x", style="인용")]
    )
    assert "style '인용' is not defined" in str(failure)


@pytest.mark.parametrize(
    ("new_text", "position", "message"),
    [("", "after", "cannot be empty"), ("x", "middle", "position must be")],
)
def test_insert_paragraph_failures(new_text, position, message):
    doc = DocxDocument.from_bytes(make_docx(p(r("본문"))))
    _, (failure,) = doc.apply_some(
        [Edit(op="insert_paragraph", paragraph="p1", new_text=new_text, position=position)]
    )
    assert message in str(failure)


def test_inserted_list_item_continues_numbering_and_view_shows_list():
    doc = edit(
        make_docx(listed("첫째") + listed("하위", 1)),
        Edit(op="insert_paragraph", paragraph="p1", new_text="둘째"),
    )
    assert doc.view(1, 10).text.split("\n") == [
        "[p1] (목록) 첫째",
        "[p2] (목록) 둘째",
        "[p3] (목록 2수준) 하위",
    ]
    inserted = body_paragraphs(doc.to_bytes())[1]
    assert inserted.find(f"{{{W}}}pPr/{{{W}}}numPr/{{{W}}}numId").get(f"{{{W}}}val") == "3"


def test_delete_paragraph():
    doc = edit(
        make_docx(p(r("가")) + p(r("나")) + p(r("다"))), Edit(op="delete_paragraph", paragraph="p2")
    )
    assert [x.text for x in doc.paragraphs] == ["가", "다"]


def test_delete_last_paragraph_of_cell_only_empties_it():
    table = "<w:tbl><w:tr><w:tc>" + p(r("칸")) + "</w:tc></w:tr></w:tbl>" + p(r("뒤"))
    doc = DocxDocument.from_bytes(make_docx(table))
    (change,), _ = doc.apply_some([Edit(op="delete_paragraph", paragraph="p1")])
    assert "empty paragraph stays" in change.note
    assert [x.text for x in doc.paragraphs] == ["", "뒤"]


def test_delete_paragraph_before_trailing_table_keeps_last_paragraph():
    body = p(r("가")) + "<w:tbl><w:tr><w:tc>" + p(r("칸")) + "</w:tc></w:tr></w:tbl>" + p(r("끝"))
    doc = DocxDocument.from_bytes(make_docx(body))
    (change,), _ = doc.apply_some([Edit(op="delete_paragraph", paragraph="p3")])
    assert "empty paragraph stays" in change.note


def test_delete_removes_bookmark_partner():
    body = p('<w:bookmarkStart w:id="7" w:name="m"/>', r("가")) + p(
        r("나"), '<w:bookmarkEnd w:id="7"/>'
    )
    doc = edit(make_docx(body), Edit(op="delete_paragraph", paragraph="p1"))
    assert b"bookmarkEnd" not in doc.to_bytes()


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("<w:p><w:pPr><w:sectPr/></w:pPr>" + r("가") + "</w:p>" + p(r("나")), "ends a section"),
        (p('<w:commentRangeStart w:id="1"/>', r("가")) + p(r("나")), "has a comment"),
        (p('<w:r><w:fldChar w:fldCharType="begin"/></w:r>', r("가")) + p(r("나")), "field"),
        ("<w:p><w:del>" + r("가") + "</w:del></w:p>" + p(r("나")), "tracked"),
    ],
)
def test_delete_paragraph_failures(body, message):
    doc = DocxDocument.from_bytes(make_docx(body))
    _, (failure,) = doc.apply_some([Edit(op="delete_paragraph", paragraph="p1")])
    assert message in str(failure)


def test_move_paragraph_keeps_formatting():
    body = p(r("가")) + p(r("나", bold=True)) + p(r("다"))
    doc = edit(
        make_docx(body), Edit(op="move_paragraph", paragraph="p2", target="p1", position="before")
    )
    assert [x.text for x in doc.paragraphs] == ["나", "가", "다"]
    assert runs_of(body_paragraphs(doc.to_bytes())[0]) == [("나", True)]


def test_moves_use_numbers_from_before_the_call():
    doc = edit(
        make_docx(p(r("1")) + p(r("2")) + p(r("3"))),
        Edit(op="move_paragraph", paragraph="p3", target="p1", position="before"),
        Edit(op="move_paragraph", paragraph="p1", target="p2"),
    )
    assert [x.text for x in doc.paragraphs] == ["3", "2", "1"]


def test_move_last_paragraph_out_of_cell_fails():
    table = "<w:tbl><w:tr><w:tc>" + p(r("칸")) + "</w:tc></w:tr></w:tbl>" + p(r("뒤"))
    doc = DocxDocument.from_bytes(make_docx(table))
    _, (failure,) = doc.apply_some([Edit(op="move_paragraph", paragraph="p1", target="p2")])
    assert "last paragraph of its table cell" in str(failure)
    assert [x.text for x in doc.paragraphs] == ["칸", "뒤"]


def test_move_between_parts_fails():
    doc = DocxDocument.from_bytes(make_docx(p(r("본문")) + p(r("둘")), header=p(r("머리"))))
    _, (failure,) = doc.apply_some([Edit(op="move_paragraph", paragraph="p3", target="p1")])
    assert "same part" in str(failure)


def test_fixture_insert_delete_move():
    doc = edit(
        FIXTURE.read_bytes(),
        Edit(
            op="insert_paragraph", paragraph="p3", new_text="해외 매출은 처음으로 10억 원을 넘었다."
        ),
        Edit(op="delete_paragraph", paragraph="p19"),
        Edit(op="move_paragraph", paragraph="p21", target="p18", position="before"),
    )
    texts = [x.text for x in DocxDocument.from_bytes(doc.to_bytes()).paragraphs]
    assert texts[3] == "해외 매출은 처음으로 10억 원을 넘었다."
    assert "자세한 내용은 영업팀 위키를 참고한다." not in texts
    assert texts.index("문의: 영업기획팀 김민수 (내선 1234)") < texts.index(
        "목표 매출은 140억 원이다.\t담당: 영업기획팀"
    )


# --- 표 행 넣기·지우기 (묶음 1, A3) ---------------------------------------------------------------


def cell(text: str, *, bold: bool = False, props: str = "") -> str:
    return f"<w:tc>{props}{p(r(text, bold=bold)) if text else '<w:p/>'}</w:tc>"


def table(*rows: str) -> str:
    return "<w:tbl><w:tblGrid/>" + "".join(rows) + "</w:tbl>" + p(r("표 뒤"))


HEADER_ROW = (
    "<w:tr><w:trPr><w:tblHeader/></w:trPr>"
    + cell("지역", bold=True)
    + cell("매출", bold=True)
    + "</w:tr>"
)
SHADED = '<w:tcPr><w:shd w:val="clear" w:fill="EEEEEE"/></w:tcPr>'


def data_row(region: str, amount: str) -> str:
    return f"<w:tr>{cell(region, props=SHADED)}{cell(amount)}</w:tr>"


def test_insert_row_after_copies_cell_formatting():
    doc = edit(
        make_docx(table(HEADER_ROW, data_row("서울", "52억"))),
        Edit(op="insert_row", paragraph="p3", cells=("부산", "31억")),
    )
    assert doc.view(1, 20).text.split("\n")[4:6] == [
        "[p5] (표1 3행 1열) 부산",
        "[p6] (표1 3행 2열) 31억",
    ]
    out = doc.to_bytes()
    assert b"ns0" not in out and b"okdoit" not in out
    new_row = list(
        etree.fromstring(zipfile.ZipFile(io.BytesIO(out)).read("word/document.xml")).iter(
            f"{{{W}}}tr"
        )
    )[2]
    assert new_row.find(f".//{{{W}}}shd") is not None


def test_insert_row_after_header_follows_data_row():
    doc = edit(
        make_docx(table(HEADER_ROW, data_row("서울", "52억"))),
        Edit(op="insert_row", paragraph="p1", cells=("부산", "31억")),
    )
    rows = list(
        etree.fromstring(
            zipfile.ZipFile(io.BytesIO(doc.to_bytes())).read("word/document.xml")
        ).iter(f"{{{W}}}tr")
    )
    new_row = rows[1]
    assert new_row.find(f"{{{W}}}trPr/{{{W}}}tblHeader") is None
    assert runs_of(new_row.find(f".//{{{W}}}p")) == [("부산", False)]
    assert [x.text for x in doc.paragraphs][:4] == ["지역", "매출", "부산", "31억"]


def test_insert_row_before_with_fewer_values_leaves_cells_empty():
    doc = edit(
        make_docx(table(HEADER_ROW, data_row("서울", "52억"))),
        Edit(op="insert_row", paragraph="p4", position="before", cells=("대구",)),
    )
    assert [x.text for x in doc.paragraphs][:6] == ["지역", "매출", "대구", "", "서울", "52억"]
    # 빈 칸은 나중에 채울 수 있다.
    doc.apply_edits([Edit("", "18억", paragraph="p4")])
    assert doc.paragraphs[3].text == "18억"


@pytest.mark.parametrize(
    ("edit_", "message"),
    [
        (Edit(op="insert_row", paragraph="p1", cells=("a", "b", "c")), "only 2 cells"),
        (Edit(op="insert_row", paragraph="p1", cells=()), "cells must list"),
        (Edit(op="insert_row", paragraph="p1", cells=("a\nb",)), "line breaks"),
        (Edit(op="insert_row", paragraph="p5", cells=("a",)), "not in a table"),
        (Edit(op="delete_row", paragraph="p5"), "not in a table"),
    ],
)
def test_row_failures(edit_, message):
    doc = DocxDocument.from_bytes(make_docx(table(HEADER_ROW, data_row("서울", "52억"))))
    _, (failure,) = doc.apply_some([edit_])
    assert message in str(failure)


def test_rows_with_vertical_merge_are_rejected():
    merged = (
        "<w:tr>"
        + cell("서울", props='<w:tcPr><w:vMerge w:val="restart"/></w:tcPr>')
        + cell("1")
        + "</w:tr>"
        "<w:tr>" + cell("", props="<w:tcPr><w:vMerge/></w:tcPr>") + cell("2") + "</w:tr>"
    )
    doc = DocxDocument.from_bytes(make_docx(table(HEADER_ROW, merged)))
    _, failures = doc.apply_some(
        [
            Edit(op="insert_row", paragraph="p3", cells=("x",)),
            Edit(op="delete_row", paragraph="p3"),
            Edit(op="insert_row", paragraph="p1", cells=("x",)),
        ]
    )
    assert len(failures) == 3 and all("vertically merged" in str(f) for f in failures)


def test_delete_row():
    doc = DocxDocument.from_bytes(
        make_docx(table(HEADER_ROW, data_row("서울", "52억"), data_row("부산", "31억")))
    )
    (change,), _ = doc.apply_some([Edit(op="delete_row", paragraph="p4")])
    assert change.note == "deleted table row 표1 2행: '서울 | 52억'"
    assert [x.text for x in doc.paragraphs][:4] == ["지역", "매출", "부산", "31억"]


def test_delete_only_row_fails():
    doc = DocxDocument.from_bytes(make_docx(table(data_row("서울", "52억"))))
    _, (failure,) = doc.apply_some([Edit(op="delete_row", paragraph="p1")])
    assert "only row" in str(failure)


def test_fixture_rows():
    doc = edit(
        FIXTURE.read_bytes(),
        Edit(op="insert_row", paragraph="p16", cells=("광주", "9억", "정하늘")),
        Edit(op="delete_row", paragraph="p11"),
    )
    text = DocxDocument.from_bytes(doc.to_bytes()).view(1, 100).text
    assert "(표1 4행 1열) 광주" in text and "부산" not in text


# --- 문단 스타일·목록 수준 바꾸기 (묶음 1) ---------------------------------------------------------


def test_set_style_heading_and_back_to_body():
    doc = edit(
        make_docx(p(r("장 제목")) + heading("원래 제목"), styles=STYLES),
        Edit(op="set_style", paragraph="p1", style="제목 2"),
        Edit(op="set_style", paragraph="p2", style="본문"),
    )
    assert [x.style for x in doc.paragraphs] == ["제목 2", None]
    first, second = body_paragraphs(doc.to_bytes())
    assert first.find(f"{{{W}}}pPr")[0].tag == f"{{{W}}}pStyle"
    assert second.find(f"{{{W}}}pPr") is None


def test_set_style_heading_removes_list_numbering():
    doc = edit(
        make_docx(listed("항목"), styles=STYLES),
        Edit(op="set_style", paragraph="p1", style="제목 1"),
    )
    assert doc.paragraphs[0].list_level is None and doc.paragraphs[0].style == "제목 1"


def test_set_list_level_of_list_item():
    doc = edit(
        make_docx(listed("첫째") + listed("둘째")),
        Edit(op="set_style", paragraph="p2", style="목록 2수준"),
    )
    assert [x.list_level for x in doc.paragraphs] == [1, 2]


def test_plain_paragraph_joins_neighbouring_list():
    body = (
        listed("첫째")
        + '<w:p><w:pPr><w:pStyle w:val="Normal"/><w:jc w:val="left"/></w:pPr>'
        + r("둘째")
        + "</w:p>"
    )
    doc = edit(make_docx(body, styles=STYLES), Edit(op="set_style", paragraph="p2", style="목록"))
    assert [x.list_level for x in doc.paragraphs] == [1, 1]
    props = body_paragraphs(doc.to_bytes())[1].find(f"{{{W}}}pPr")
    assert [c.tag.split("}")[1] for c in props] == ["pStyle", "numPr", "jc"]


@pytest.mark.parametrize(
    ("style", "message"),
    [("목록", "no list right before or after"), ("", "style is required"), ("인용", "not defined")],
)
def test_set_style_failures(style, message):
    doc = DocxDocument.from_bytes(make_docx(p(r("가")), styles=STYLES))
    _, (failure,) = doc.apply_some([Edit(op="set_style", paragraph="p1", style=style)])
    assert message in str(failure)
