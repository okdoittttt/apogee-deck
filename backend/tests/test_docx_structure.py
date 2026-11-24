"""묶음 5 (구조 마무리) 테스트: 줄바꿈, 병합 표의 행, 표 열, Markdown 넣기, 표 지우기, 새 목록, 섹션."""

import pytest
from lxml import etree
from test_docx_editing import HEADER_ROW, W, body_paragraphs, cell, edit, make_docx, p, r, table
from test_docx_review import assert_valid

from app.documents.editing.docx import LINE_BREAK, DocxDocument
from app.documents.editing.text import Edit, EditError

NS = {"w": W}


def tag(element: etree._Element) -> str:
    return etree.QName(element).localname


def document_root(data: bytes) -> etree._Element:
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return etree.fromstring(zf.read("word/document.xml"))


# --- A6 문단 안 줄바꿈 ----------------------------------------------------------------------------


def test_view_shows_soft_break_as_symbol():
    body = "<w:p><w:r><w:t>서울시 중구</w:t><w:br/><w:t>세종대로 110</w:t></w:r></w:p>"
    doc = DocxDocument.from_bytes(make_docx(body))
    assert doc.view(1, 10).text == f"[p1] 서울시 중구{LINE_BREAK}세종대로 110"


def test_symbol_in_new_text_inserts_line_break_not_paragraph():
    body = p(r("서울시 중구 세종대로 110"))
    doc = edit(make_docx(body), Edit("중구 세종대로", f"중구{LINE_BREAK}세종대로"))
    out = doc.to_bytes()
    (paragraph,) = body_paragraphs(out)
    assert [tag(c) for c in paragraph.find("w:r", NS)] == ["rPr", "t", "br", "t"]
    assert DocxDocument.from_bytes(out).paragraphs[0].text == f"서울시 중구{LINE_BREAK}세종대로 110"


def test_symbol_finds_and_removes_existing_break():
    body = "<w:p><w:r><w:t>가</w:t><w:br/><w:t>나</w:t></w:r></w:p>"
    doc = edit(make_docx(body), Edit(f"가{LINE_BREAK}나", "가 나"))
    assert doc.paragraphs[0].text == "가 나"


def test_raw_newline_still_finds_soft_break_for_compatibility():
    body = "<w:p><w:r><w:t>가</w:t><w:br/><w:t>나</w:t></w:r></w:p>"
    doc = edit(make_docx(body), Edit("가\n나", "가\n다"))
    assert len(doc.paragraphs) == 1 and doc.paragraphs[0].text == f"가{LINE_BREAK}다"


def test_line_breaks_in_new_paragraph_and_cells():
    body = p(r("주소")) + p(r("끝"))
    doc = edit(
        make_docx(body),
        Edit(op="insert_paragraph", paragraph="p1", new_text=f"서울{LINE_BREAK}부산"),
    )
    assert [x.text for x in doc.paragraphs] == ["주소", f"서울{LINE_BREAK}부산", "끝"]


def test_tracked_line_break():
    body = p(r("서울시 중구 세종대로"))
    doc = DocxDocument.from_bytes(make_docx(body))
    doc.apply_edits([Edit("중구 세종", f"중구{LINE_BREAK}세종")], track=True)
    out = doc.to_bytes()
    assert_valid(out)
    inserted = document_root(out).find(".//w:ins", NS)
    assert inserted is not None and inserted.find(".//w:br", NS) is not None


def test_literal_symbol_in_paragraph_refuses_break_symbol():
    body = p(r(f"화살표 {LINE_BREAK} 기호"))
    doc = DocxDocument.from_bytes(make_docx(body))
    with pytest.raises(EditError, match="as text"):
        doc.apply_edits([Edit("화살표", f"화{LINE_BREAK}살표")])


# --- A3 세로 병합이 걸린 표의 행 -------------------------------------------------------------------

RESTART = '<w:tcPr><w:vMerge w:val="restart"/></w:tcPr>'
CONTINUE = "<w:tcPr><w:vMerge/></w:tcPr>"


def merged_table() -> bytes:
    """``1팀`` 칸이 세 행에 걸쳐 세로 병합된 표 (p1 머리글, p3 1팀, p4 김, p5 빈, p6 이, p7 빈, p8 박)."""
    rows = (
        "<w:tr>" + cell("1팀", props=RESTART) + cell("김") + "</w:tr>"
        "<w:tr>" + cell("", props=CONTINUE) + cell("이") + "</w:tr>"
        "<w:tr>" + cell("", props=CONTINUE) + cell("박") + "</w:tr>"
    )
    return make_docx(table(HEADER_ROW, rows))


def column_states(data: bytes) -> list[list[str | None]]:
    """행마다 칸의 세로 병합 상태와 글자."""
    result = []
    for row in document_root(data).iter(f"{{{W}}}tr"):
        states = []
        for tc in row.iterchildren(f"{{{W}}}tc"):
            merge = tc.find("w:tcPr/w:vMerge", NS)
            text = "".join(t.text or "" for t in tc.iter(f"{{{W}}}t"))
            state = None if merge is None else merge.get(f"{{{W}}}val", "continue")
            states.append(f"{state}:{text}" if state else text)
        result.append(states)
    return result


def test_view_marks_continued_merge_cells():
    view = DocxDocument.from_bytes(merged_table()).view(1, 20).text
    assert "[p5] (표1 3행 1열) (위 칸과 병합)" in view
    assert "[p4] (표1 2행 2열) 김" in view


def test_fill_refuses_continued_merge_cell_and_points_to_start():
    doc = DocxDocument.from_bytes(merged_table())
    with pytest.raises(EditError, match="merged with the cell above.*p3"):
        doc.apply_edits([Edit("", "x", paragraph="p5")])


def test_insert_row_inside_merge_extends_it():
    doc = DocxDocument.from_bytes(merged_table())
    (change,) = doc.apply_edits([Edit(op="insert_row", paragraph="p4", cells=("무시", "최"))])
    out = doc.to_bytes()
    assert column_states(out)[1:] == [
        ["restart:1팀", "김"],
        ["continue:", "최"],
        ["continue:", "이"],
        ["continue:", "박"],
    ]
    assert "left out" in change.note
    assert_valid(out)


def test_insert_row_at_merge_edges_is_not_merged():
    doc = DocxDocument.from_bytes(merged_table())
    doc.apply_edits(
        [
            Edit(op="insert_row", paragraph="p3", position="before", cells=("0팀", "가")),
            Edit(op="insert_row", paragraph="p8", cells=("2팀", "하")),
        ]
    )
    assert column_states(doc.to_bytes())[1:] == [
        ["0팀", "가"],
        ["restart:1팀", "김"],
        ["continue:", "이"],
        ["continue:", "박"],
        ["2팀", "하"],
    ]


def test_delete_merge_start_row_moves_text_down():
    doc = edit(merged_table(), Edit(op="delete_row", paragraph="p4"))
    assert column_states(doc.to_bytes())[1:] == [["restart:1팀", "이"], ["continue:", "박"]]


def test_delete_rows_until_merge_is_single_cell_drops_merge():
    doc = edit(
        merged_table(),
        Edit(op="delete_row", paragraph="p8"),
        Edit(op="delete_row", paragraph="p6"),
    )
    assert column_states(doc.to_bytes())[1:] == [["1팀", "김"]]
    doc = edit(
        merged_table(), Edit(op="delete_row", paragraph="p3"), Edit(op="delete_row", paragraph="p6")
    )
    assert column_states(doc.to_bytes())[1:] == [["1팀", "박"]]


# --- A3 표 열 넣기·지우기 -------------------------------------------------------------------------


def wcell(text: str, width: int, extra: str = "") -> str:
    props = f'<w:tcPr><w:tcW w:w="{width}" w:type="dxa"/>{extra}</w:tcPr>'
    return cell(text, props=props)


def grid_table() -> bytes:
    """3열 표 (너비 2000·3000·4000). 첫 행은 가로 병합된 제목 칸 하나 (p1 제목, p2 지역 …)."""
    rows = (
        "<w:tr>" + wcell("제목", 9000, '<w:gridSpan w:val="3"/>') + "</w:tr>"
        "<w:tr>" + wcell("지역", 2000) + wcell("매출", 3000) + wcell("비고", 4000) + "</w:tr>"
        "<w:tr>" + wcell("서울", 2000) + wcell("52억", 3000) + wcell("", 4000) + "</w:tr>"
    )
    grid = '<w:tblGrid><w:gridCol w:w="2000"/><w:gridCol w:w="3000"/><w:gridCol w:w="4000"/></w:tblGrid>'
    return make_docx(f"<w:tbl>{grid}{rows}</w:tbl>" + p(r("표 뒤")))


def table_shape(data: bytes) -> tuple[list[int], list[list[tuple[str, int, int]]]]:
    """``(격자 너비, 행마다 [(글자, gridSpan, tcW)])``."""
    root = document_root(data)
    grid = [int(c.get(f"{{{W}}}w")) for c in root.iter(f"{{{W}}}gridCol")]
    rows = []
    for row in root.iter(f"{{{W}}}tr"):
        cells = []
        for tc in row.iterchildren(f"{{{W}}}tc"):
            span = tc.find("w:tcPr/w:gridSpan", NS)
            width = tc.find("w:tcPr/w:tcW", NS)
            text = "".join(t.text or "" for t in tc.iter(f"{{{W}}}t"))
            cells.append(
                (
                    text,
                    int(span.get(f"{{{W}}}val")) if span is not None else 1,
                    int(width.get(f"{{{W}}}w")) if width is not None else 0,
                )
            )
        rows.append(cells)
    return grid, rows


def test_insert_column_keeps_table_width_and_widens_spanning_title():
    doc = DocxDocument.from_bytes(grid_table())
    (change,) = doc.apply_edits(
        [Edit(op="insert_column", paragraph="p3", cells=("무시", "목표", "60억"))]
    )
    grid, rows = table_shape(doc.to_bytes())
    assert sum(grid) == 9000 and len(grid) == 4
    assert grid == [1500, 2250, 2250, 3000]
    assert rows == [
        [("제목", 4, 9000)],
        [("지역", 1, 1500), ("매출", 1, 2250), ("목표", 1, 2250), ("비고", 1, 3000)],
        [("서울", 1, 1500), ("52억", 1, 2250), ("60억", 1, 2250), ("", 1, 3000)],
    ]
    assert change.note.startswith("inserted a column after '매출' in 표1: ")
    assert "left out" in change.note
    assert_valid(doc.to_bytes())


def test_insert_column_before_first_column():
    doc = edit(
        grid_table(),
        Edit(op="insert_column", paragraph="p2", position="before", cells=("", "번호", "1")),
    )
    _, rows = table_shape(doc.to_bytes())
    assert [c[0] for c in rows[1]] == ["번호", "지역", "매출", "비고"]
    assert [c[0] for c in rows[2]] == ["1", "서울", "52억", ""]


def test_insert_column_follows_reference_cell_format():
    rows = "<w:tr>" + cell("지역", bold=True) + cell("매출", bold=True) + "</w:tr>"
    doc = edit(make_docx(table(rows)), Edit(op="insert_column", paragraph="p2", cells=("비고",)))
    first_row = next(document_root(doc.to_bytes()).iter(f"{{{W}}}tr"))
    last = list(first_row.iterchildren(f"{{{W}}}tc"))[-1]
    assert last.find(".//w:b", NS) is not None
    assert [x.text for x in doc.paragraphs][:3] == ["지역", "매출", "비고"]


def test_insert_column_refuses_too_many_values():
    doc = DocxDocument.from_bytes(grid_table())
    with pytest.raises(EditError, match="only 3 rows"):
        doc.apply_edits([Edit(op="insert_column", paragraph="p2", cells=("a", "b", "c", "d"))])


def test_delete_column_returns_width_and_narrows_span():
    doc = DocxDocument.from_bytes(grid_table())
    (change,) = doc.apply_edits([Edit(op="delete_column", paragraph="p3")])
    grid, rows = table_shape(doc.to_bytes())
    assert grid == [3000, 6000]
    assert rows == [
        [("제목", 2, 9000)],
        [("지역", 1, 3000), ("비고", 1, 6000)],
        [("서울", 1, 3000), ("", 1, 6000)],
    ]
    assert change.note == "deleted column '매출' of 표1: '매출 | 52억'"


def test_delete_last_column_is_refused():
    rows = "<w:tr>" + cell("하나") + "</w:tr>"
    doc = DocxDocument.from_bytes(make_docx(table(rows)))
    with pytest.raises(EditError, match="delete_table"):
        doc.apply_edits([Edit(op="delete_column", paragraph="p1")])


def test_column_ops_refused_in_tracking_mode():
    doc = DocxDocument.from_bytes(grid_table())
    with pytest.raises(EditError, match="tracked change"):
        doc.apply_edits([Edit(op="delete_column", paragraph="p3")], track=True)


# --- insert_markdown (표 만들기, 새 목록, 섹션 넣기), delete_table ------------------------------------

PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
STYLES_XML = (
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/></w:style>'
)
SECTION = (
    '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/>'
    '<w:pgMar w:top="1440" w:right="1800" w:bottom="1440" w:left="1800"/></w:sectPr>'
)


def parts_of(data: bytes) -> dict[str, bytes]:
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


def with_part(data: bytes, name: str, xml: str) -> bytes:
    """``data`` 에 부분 하나를 더한다 (관계·내용 형식은 이미 있다고 본다)."""
    import io
    import zipfile

    entries = parts_of(data)
    entries[name] = xml.encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for key, value in entries.items():
            zf.writestr(key, value)
    return buffer.getvalue()


SIZED = '<w:r><w:rPr><w:sz w:val="28"/></w:rPr><w:t>본문 첫 문단</w:t></w:r>'


def test_insert_markdown_heading_paragraph_and_table():
    body = (
        '<w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr><w:r><w:t>1. 개요</w:t></w:r></w:p>'
        f"<w:p>{SIZED}</w:p>" + SECTION
    )
    data = make_docx(body, styles=STYLES_XML)
    markdown = (
        "## 2. 일정\n\n아래와 **같이** 진행한다.\n\n| 단계 | 기간 |\n|---|---|\n| 설계 | 2주 |"
    )
    doc = DocxDocument.from_bytes(data)
    (change,) = doc.apply_edits([Edit(op="insert_markdown", paragraph="p2", new_text=markdown)])
    assert change.note == "inserted 1 heading, 1 paragraph, 1 table (2 rows) after p2"
    out = doc.to_bytes()
    assert_valid(out)
    reopened = DocxDocument.from_bytes(out)
    texts = [(x.style, x.text) for x in reopened.paragraphs]
    assert texts[:7] == [
        ("제목 2", "1. 개요"),
        (None, "본문 첫 문단"),
        ("제목 2", "2. 일정"),
        (None, "아래와 같이 진행한다."),
        (None, "단계"),
        (None, "기간"),
        (None, "설계"),
    ]
    root = document_root(out)
    # 스타일 없는 본문 문단은 이웃 본문 문단의 글자 크기를 따르고, 굵게는 그 위에 둔다
    plain = root.findall(".//w:body/w:p", NS)[3]
    sizes = [r.find("w:rPr/w:sz", NS).get(f"{{{W}}}val") for r in plain.findall("w:r", NS)]
    assert sizes == ["28", "28", "28"]
    assert plain.findall("w:r", NS)[1].find("w:rPr/w:b", NS) is not None
    # 표 너비는 구역의 본문 너비 (12240 − 1800 × 2)
    grid = [int(c.get(f"{{{W}}}w")) for c in root.iter(f"{{{W}}}gridCol")]
    assert grid == [4320, 4320]
    # 본문 끝(구역 설정 앞)에 표가 오면 빈 문단을 붙인다
    body_children = [tag(c) for c in root.find("w:body", NS)]
    assert body_children[-3:] == ["tbl", "p", "sectPr"]


def test_insert_markdown_adds_missing_styles_and_numbering_part():
    data = make_docx(p(r("첫 문단")))
    doc = DocxDocument.from_bytes(data)
    doc.apply_edits(
        [
            Edit(
                op="insert_markdown",
                paragraph="p1",
                new_text="# 새 장\n\n1. 하나\n2. 둘\n\n- 점\n\n[링크](https://example.org)",
            )
        ]
    )
    out = doc.to_bytes()
    assert_valid(out)
    entries = parts_of(out)
    assert "word/numbering.xml" in entries and "word/styles.xml" in entries
    assert b"numbering+xml" in entries["[Content_Types].xml"]
    assert b"https://example.org" in entries["word/_rels/document.xml.rels"]
    reopened = DocxDocument.from_bytes(out)
    assert [(x.style, x.list_level, x.text) for x in reopened.paragraphs] == [
        (None, None, "첫 문단"),
        ("제목 1", None, "새 장"),
        (None, 1, "하나"),
        (None, 1, "둘"),
        (None, 1, "점"),
        (None, None, "링크"),
    ]


NUMBERING = (
    '<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    '<w:abstractNum w:abstractNumId="7"><w:lvl w:ilvl="0"><w:start w:val="1"/>'
    '<w:numFmt w:val="decimal"/><w:lvlText w:val="%1)"/></w:lvl></w:abstractNum>'
    '<w:num w:numId="3"><w:abstractNumId w:val="7"/></w:num></w:numbering>'
)
NUM_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<Relationships xmlns="{PKG_REL}">'
    f'<Relationship Id="rIdN" Type="{REL}numbering" Target="numbering.xml"/>'
    f'<Relationship Id="rIdS" Type="{REL}styles" Target="styles.xml"/>'
    "</Relationships>"
)


def numbered_docx() -> bytes:
    item = (
        '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="3"/></w:numPr></w:pPr>'
        "<w:r><w:t>기존 항목</w:t></w:r></w:p>"
    )
    data = make_docx(item + p(r("본문")), styles=STYLES_XML)
    data = with_part(data, "word/numbering.xml", NUMBERING)
    return with_part(data, "word/_rels/document.xml.rels", NUM_RELS)


def test_insert_markdown_list_follows_document_list_definition_and_restarts():
    doc = DocxDocument.from_bytes(numbered_docx())
    doc.apply_edits([Edit(op="insert_markdown", paragraph="p2", new_text="1. 새 하나\n2. 새 둘")])
    out = doc.to_bytes()
    numbering = etree.fromstring(parts_of(out)["word/numbering.xml"])
    nums = numbering.findall("w:num", NS)
    assert len(nums) == 2 and len(numbering.findall("w:abstractNum", NS)) == 1
    new = nums[-1]
    assert new.find("w:abstractNumId", NS).get(f"{{{W}}}val") == "7"
    assert new.find("w:lvlOverride/w:startOverride", NS).get(f"{{{W}}}val") == "1"
    used = {n.get(f"{{{W}}}val") for n in document_root(out).iter(f"{{{W}}}numId")}
    assert used == {"3", new.get(f"{{{W}}}numId")}


def test_insert_markdown_refuses_table_cell_and_empty():
    doc = DocxDocument.from_bytes(make_docx(table("<w:tr>" + cell("칸") + "</w:tr>")))
    _, failures = doc.apply_some(
        [
            Edit(op="insert_markdown", paragraph="p1", new_text="글"),
            Edit(op="insert_markdown", paragraph="p2", new_text="  "),
        ]
    )
    assert "directly in the document body" in str(failures[0])
    assert "empty" in str(failures[1])


def test_text_to_table_in_one_call():
    body = p(r("목록")) + p(r("서울 52억")) + p(r("부산 31억")) + p(r("끝"))
    doc = edit(
        make_docx(body),
        Edit(
            op="insert_markdown",
            paragraph="p3",
            new_text="| 지역 | 매출 |\n|---|---|\n| 서울 | 52억 |\n| 부산 | 31억 |",
        ),
        Edit(op="delete_paragraph", paragraph="p2"),
        Edit(op="delete_paragraph", paragraph="p3"),
    )
    assert [x.text for x in doc.paragraphs] == [
        "목록",
        "지역",
        "매출",
        "서울",
        "52억",
        "부산",
        "31억",
        "끝",
    ]


def test_delete_table():
    rows = (
        "<w:tr>"
        + cell("지역")
        + cell("매출")
        + "</w:tr><w:tr>"
        + cell("서울")
        + cell("52억")
        + "</w:tr>"
    )
    doc = DocxDocument.from_bytes(make_docx(p(r("앞")) + table(rows)))
    (change,) = doc.apply_edits([Edit(op="delete_table", paragraph="p4")])
    assert change.note == "deleted 표1 (2 rows x 2 columns): '지역 | 매출'"
    assert [x.text for x in doc.paragraphs] == ["앞", "표 뒤"]


def test_delete_table_keeps_paragraph_where_needed():
    inner = "<w:tbl><w:tblGrid/><w:tr>" + cell("안") + "</w:tr></w:tbl>"
    outer = f"<w:tbl><w:tblGrid/><w:tr><w:tc>{inner}<w:p/></w:tc></w:tr></w:tbl>"
    doc = DocxDocument.from_bytes(make_docx(outer + p(r("끝"))))
    doc.apply_edits([Edit(op="delete_table", paragraph="p1")])
    root = document_root(doc.to_bytes())
    assert len(root.findall(".//w:tbl", NS)) == 1
    two = (
        "<w:tbl><w:tblGrid/><w:tr>" + cell("가") + "</w:tr></w:tbl>"
        "<w:tbl><w:tblGrid/><w:tr>" + cell("나") + "</w:tr></w:tbl>"
        "<w:tbl><w:tblGrid/><w:tr>" + cell("다") + "</w:tr></w:tbl>" + p(r("끝"))
    )
    doc = edit(make_docx(two), Edit(op="delete_table", paragraph="p2"))
    children = [tag(c) for c in document_root(doc.to_bytes()).find("w:body", NS)]
    assert children == ["tbl", "p", "tbl", "p"]


def test_delete_table_refuses_comment():
    rows = (
        '<w:tr><w:tc><w:p><w:commentRangeStart w:id="1"/><w:r><w:t>칸</w:t></w:r>'
        '<w:commentRangeEnd w:id="1"/><w:r><w:commentReference w:id="1"/></w:r></w:p></w:tc></w:tr>'
    )
    doc = DocxDocument.from_bytes(make_docx(table(rows)))
    with pytest.raises(EditError, match="has a comment"):
        doc.apply_edits([Edit(op="delete_table", paragraph="p1")])


# --- A4 이미 있는 문단을 새 목록으로 ----------------------------------------------------------------


def test_set_style_starts_numbered_list_and_next_items_join():
    body = p(r("소개")) + p(r("첫째")) + p(r("둘째")) + p(r("끝"))
    doc = edit(
        make_docx(body),
        Edit(op="set_style", paragraph="p2", style="번호 목록"),
        Edit(op="set_style", paragraph="p3", style="번호 목록"),
    )
    out = doc.to_bytes()
    assert_valid(out)
    view = DocxDocument.from_bytes(out).view(1, 10).text.splitlines()
    assert view == ["[p1] 소개", "[p2] (번호 목록) 첫째", "[p3] (번호 목록) 둘째", "[p4] 끝"]
    ids = [n.get(f"{{{W}}}val") for n in document_root(out).iter(f"{{{W}}}numId")]
    assert len(ids) == 2 and ids[0] == ids[1]


def test_set_style_bullet_next_to_numbered_list_starts_its_own_list():
    doc = DocxDocument.from_bytes(numbered_docx())
    doc.apply_edits([Edit(op="set_style", paragraph="p2", style="글머리표 목록 2수준")])
    out = doc.to_bytes()
    view = DocxDocument.from_bytes(out).view(1, 10).text.splitlines()
    assert view == ["[p1] (번호 목록) 기존 항목", "[p2] (글머리표 목록 2수준) 본문"]
    numbering = etree.fromstring(parts_of(out)["word/numbering.xml"])
    assert len(numbering.findall("w:abstractNum", NS)) == 2  # 문서에 글머리표 정의가 없어 더함


def test_set_style_numbered_list_follows_document_definition():
    doc = edit(numbered_docx(), Edit(op="set_style", paragraph="p2", style="번호 목록"))
    out = doc.to_bytes()
    ids = [n.get(f"{{{W}}}val") for n in document_root(out).iter(f"{{{W}}}numId")]
    assert ids == ["3", "3"]  # 바로 앞 같은 종류 목록에 이어 붙는다


def test_generic_list_style_without_neighbour_suggests_kinds():
    doc = DocxDocument.from_bytes(make_docx(p(r("가"))))
    with pytest.raises(EditError, match="번호 목록"):
        doc.apply_edits([Edit(op="set_style", paragraph="p1", style="목록")])


# --- A5 섹션 옮기기·지우기 ------------------------------------------------------------------------

STYLES_H = (
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/></w:style>'
)


def h(level: int, text: str) -> str:
    return (
        f'<w:p><w:pPr><w:pStyle w:val="Heading{level}"/></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>'
    )


def chapters() -> bytes:
    """p1 1장, p2 가, p3 2장, p4 2.1, p5 나, (표: p6 칸), p7 3장, p8 다."""
    small = "<w:tbl><w:tblGrid/><w:tr>" + cell("칸") + "</w:tr></w:tbl>"
    body = (
        h(1, "1장")
        + p(r("가"))
        + h(1, "2장")
        + h(2, "2.1")
        + p(r("나"))
        + small
        + h(1, "3장")
        + p(r("다"))
        + "<w:sectPr/>"
    )
    return make_docx(body, styles=STYLES_H)


def texts(doc: DocxDocument) -> list[str]:
    return [x.text for x in doc.paragraphs]


def test_move_section_before_another_chapter():
    doc = DocxDocument.from_bytes(chapters())
    (change,) = doc.apply_edits(
        [
            Edit(
                op="move_paragraph",
                paragraph="p3",
                target="p1",
                position="before",
                whole_section=True,
            )
        ]
    )
    assert texts(doc) == ["2장", "2.1", "나", "칸", "1장", "가", "3장", "다"]
    assert change.note == "moved section '2장' (p3-p6: 3 paragraphs, 1 table) before p1"
    assert_valid(doc.to_bytes())


def test_move_section_after_heading_target_goes_after_whole_section():
    doc = edit(
        chapters(), Edit(op="move_paragraph", paragraph="p1", target="p3", whole_section=True)
    )
    assert texts(doc) == ["2장", "2.1", "나", "칸", "1장", "가", "3장", "다"]


def test_move_last_section_keeps_body_ending_with_paragraph():
    doc = edit(
        chapters(),
        Edit(op="move_paragraph", paragraph="p3", target="p7", whole_section=True),
    )
    assert texts(doc) == ["1장", "가", "3장", "다", "2장", "2.1", "나", "칸", ""]
    children = [tag(c) for c in document_root(doc.to_bytes()).find("w:body", NS)]
    assert children[-3:] == ["tbl", "p", "sectPr"]


def test_subsection_stops_at_same_level_heading():
    doc = edit(chapters(), Edit(op="delete_paragraph", paragraph="p4", whole_section=True))
    assert texts(doc) == ["1장", "가", "2장", "3장", "다"]


def test_delete_section():
    doc = DocxDocument.from_bytes(chapters())
    (change,) = doc.apply_edits([Edit(op="delete_paragraph", paragraph="p7", whole_section=True)])
    assert change.note == "deleted section '3장' (p7-p8: 2 paragraphs)"
    assert texts(doc) == ["1장", "가", "2장", "2.1", "나", "칸", ""]


def test_whole_section_refusals():
    doc = DocxDocument.from_bytes(chapters())
    _, failures = doc.apply_some(
        [
            Edit(op="delete_paragraph", paragraph="p2", whole_section=True),
            Edit(op="move_paragraph", paragraph="p3", target="p5", whole_section=True),
            Edit(op="move_paragraph", paragraph="p3", target="p6", whole_section=True),
        ]
    )
    assert "is not" in str(failures[0]) and "set_style" in str(failures[0])
    assert "inside the section being moved" in str(failures[1])
    assert "directly in the document body" in str(failures[2])


def test_section_with_comment_reaching_outside_is_refused():
    body = (
        h(1, "1장")
        + '<w:p><w:commentRangeStart w:id="1"/><w:r><w:t>가</w:t></w:r></w:p>'
        + h(1, "2장")
        + '<w:p><w:r><w:t>나</w:t></w:r><w:commentRangeEnd w:id="1"/>'
        '<w:r><w:commentReference w:id="1"/></w:r></w:p>'
    )
    doc = DocxDocument.from_bytes(make_docx(body, styles=STYLES_H))
    with pytest.raises(EditError, match="reaches outside"):
        doc.apply_edits(
            [
                Edit(
                    op="move_paragraph",
                    paragraph="p3",
                    target="p1",
                    position="before",
                    whole_section=True,
                )
            ]
        )
