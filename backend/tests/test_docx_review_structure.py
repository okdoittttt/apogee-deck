"""묶음 6 (검토 마무리) 테스트: 구조 수정 추적, 구조 기록 수락·거절, 작성자별 수락·거절.

추적으로 고친 결과는 "모두 수락 = 바로 고친 결과", "모두 거절 = 원본" 이어야 한다 (글자·위치·스타일).
"""

import pytest
from lxml import etree
from test_docx_editing import HEADER_ROW, W, cell, data_row, make_docx, p, r, table
from test_docx_review import DATE, assert_valid, edited
from test_docx_structure import STYLES_H, chapters, document_root, h

from app.documents.editing.docx import DocxDocument
from app.documents.editing.text import Edit, EditError

NS = {"w": W}


def shape(doc: DocxDocument) -> list[tuple[str, str | None, str | None, str | None]]:
    return [(x.text, x.location, x.style, x.list_kind) for x in doc.paragraphs if x.text]


def roundtrip(data: bytes, *edits: Edit) -> tuple[DocxDocument, bytes]:
    """추적으로 고친 결과를 돌려주고, 수락·거절 왕복을 검사한다."""
    direct = DocxDocument.from_bytes(data)
    direct.apply_edits(edits)
    tracked = DocxDocument.from_bytes(data)
    tracked.apply_edits(edits, track=True, date=DATE)
    out = tracked.to_bytes()
    assert_valid(out)
    accepted = edited(out, Edit(op="accept_changes"))
    assert shape(accepted) == shape(DocxDocument.from_bytes(direct.to_bytes()))
    assert not any(x.tracked for x in accepted.paragraphs)
    assert_valid(accepted.to_bytes())
    rejected = edited(out, Edit(op="reject_changes"))
    assert shape(rejected) == shape(DocxDocument.from_bytes(data))
    assert not any(x.tracked for x in rejected.paragraphs)
    assert_valid(rejected.to_bytes())
    return DocxDocument.from_bytes(out), out


def sales() -> bytes:
    return make_docx(
        p(r("앞 문단"))
        + table(HEADER_ROW, data_row("서울", "100"), data_row("부산", "80"))
        + p(r("끝")),
        styles=STYLES_H,
    )


# --- 구조 수정을 추적으로 ---------------------------------------------------------------------


def test_tracked_merge_round_trip():
    doc, out = roundtrip(
        make_docx(p(r("가")) + p(r("나")) + p(r("다"))), Edit(op="merge", paragraph="p1")
    )
    first = document_root(out).find(".//w:p", NS)
    assert first.find("w:pPr/w:rPr/w:del", NS) is not None
    assert first.find("w:ins/w:r/w:t", NS).text == " "
    assert "문단 합침 추적" in doc.view(1, 10).text


def test_tracked_set_style_records_old_paragraph_format():
    doc, out = roundtrip(
        make_docx(p(r("제목감")) + p(r("본문")), styles=STYLES_H),
        Edit(op="set_style", paragraph="p1", style="제목 1"),
    )
    props = document_root(out).find(".//w:p/w:pPr", NS)
    assert etree.QName(props[-1]).localname == "pPrChange"
    assert props.find("w:pPrChange/w:pPr/w:pStyle", NS) is None
    assert doc.paragraphs[0].style == "제목 1"


def test_tracked_new_list_round_trip():
    roundtrip(
        make_docx(p(r("첫째")) + p(r("둘째"))),
        Edit(op="set_style", paragraph="p1", style="번호 목록"),
    )


def test_tracked_insert_and_delete_rows_round_trip():
    doc, out = roundtrip(
        sales(),
        Edit(op="insert_row", paragraph="p4", cells=["대구", "70"]),
        Edit(op="delete_row", paragraph="p6"),
    )
    rows = document_root(out).findall(".//w:tr", NS)
    assert len(rows) == 4  # 지운 행은 기록만 달고 남는다
    assert rows[2].find("w:trPr/w:ins", NS) is not None
    assert rows[3].find("w:trPr/w:del", NS) is not None
    lines = doc.view(1, 20).text
    assert "행 넣음 추적" in lines and "행 지움 추적" in lines


def test_tracked_row_record_is_placed_after_row_properties():
    data = make_docx(
        table(
            "<w:tr><w:trPr><w:cantSplit/></w:trPr>" + cell("가") + "</w:tr>",
            "<w:tr>" + cell("나") + "</w:tr>",
        )
    )
    _, out = roundtrip(data, Edit(op="delete_row", paragraph="p1"))
    props = document_root(out).find(".//w:tr/w:trPr", NS)
    assert [etree.QName(c).localname for c in props] == ["cantSplit", "del"]


def test_tracked_markdown_round_trip():
    doc, out = roundtrip(
        make_docx(p(r("앞")) + p(r("뒤")), styles=STYLES_H),
        Edit(
            op="insert_markdown",
            paragraph="p1",
            new_text="## 일정\n\n- 설계\n- 구현\n\n| 단계 | 기간 |\n|---|---|\n| 설계 | 2주 |",
        ),
    )
    assert all(
        row.find("w:trPr/w:ins", NS) is not None for row in document_root(out).iter(f"{{{W}}}tr")
    )
    assert "(as a tracked change)" not in doc.view(1, 5).text


def test_tracked_delete_table_round_trip():
    _, out = roundtrip(sales(), Edit(op="delete_table", paragraph="p3"))
    assert all(
        row.find("w:trPr/w:del", NS) is not None for row in document_root(out).iter(f"{{{W}}}tr")
    )


def test_tracked_section_delete_and_move_round_trip():
    roundtrip(chapters(), Edit(op="delete_paragraph", paragraph="p3", whole_section=True))
    roundtrip(
        chapters(),
        Edit(
            op="move_paragraph", paragraph="p7", target="p1", position="before", whole_section=True
        ),
    )


def test_tracked_paragraph_move_round_trip():
    doc, _ = roundtrip(
        make_docx(p(r("가")) + p(r("나")) + p(r("다"))),
        Edit(op="move_paragraph", paragraph="p1", target="p3"),
    )
    assert [x.marked for x in doc.paragraphs] == ["{-가-}", "나", "다", "{+가+}"]


def test_tracked_move_of_commented_paragraph_is_refused():
    body = p('<w:commentRangeStart w:id="3"/>' + r("가") + '<w:commentRangeEnd w:id="3"/>') + p(
        r("나")
    )
    doc = DocxDocument.from_bytes(make_docx(body))
    with pytest.raises(EditError, match="copy it"):
        doc.apply_edits([Edit(op="move_paragraph", paragraph="p1", target="p2")], track=True)


def test_tracked_delete_keeps_comment_attached():
    body = (
        h(1, "1장")
        + p(
            '<w:commentRangeStart w:id="3"/>' + r("가") + '<w:commentRangeEnd w:id="3"/>',
            '<w:r><w:commentReference w:id="3"/></w:r>',
        )
        + h(1, "2장")
    )
    comments = '<w:comment w:id="3" w:author="김"><w:p><w:r><w:t>메모</w:t></w:r></w:p></w:comment>'
    doc = DocxDocument.from_bytes(make_docx(body, styles=STYLES_H, comments=comments))
    doc.apply_edits([Edit(op="delete_paragraph", paragraph="p1", whole_section=True)], track=True)
    out = doc.to_bytes()
    assert_valid(out)
    assert "메모 c3" in DocxDocument.from_bytes(out).view(1, 10).text


def test_own_inserted_paragraph_and_row_are_removed_without_records():
    doc = DocxDocument.from_bytes(sales())
    doc.apply_edits(
        [
            Edit(op="insert_paragraph", paragraph="p1", new_text="새 문단"),
            Edit(op="insert_row", paragraph="p4", cells=["대구", "70"]),
        ],
        track=True,
        date=DATE,
    )
    again = DocxDocument.from_bytes(doc.to_bytes())
    numbers = {x.text: x.number for x in again.paragraphs}
    again.apply_edits(
        [
            Edit(op="delete_paragraph", paragraph=f"p{numbers['새 문단']}"),
            Edit(op="delete_row", paragraph=f"p{numbers['대구']}"),
        ],
        track=True,
        date=DATE,
    )
    out = again.to_bytes()
    assert_valid(out)
    root = document_root(out)
    assert root.find(".//w:ins", NS) is None and root.find(".//w:del", NS) is None
    assert shape(DocxDocument.from_bytes(out)) == shape(DocxDocument.from_bytes(sales()))


def test_row_of_own_inserted_row_can_take_another_row():
    doc = DocxDocument.from_bytes(sales())
    doc.apply_edits([Edit(op="insert_row", paragraph="p6", cells=["대구", "70"])], track=True)
    again = DocxDocument.from_bytes(doc.to_bytes())
    number = next(x.number for x in again.paragraphs if x.text == "대구")
    again.apply_edits(
        [Edit(op="insert_row", paragraph=f"p{number}", cells=["광주", "60"])], track=True
    )
    assert "광주" in [x.text for x in again.paragraphs]


# --- 구조 기록 수락·거절 ------------------------------------------------------------------------


def row_doc(row_props: str, *, author: str = "가") -> bytes:
    return make_docx(
        "<w:tbl><w:tblGrid/>"
        + "<w:tr>"
        + cell("위")
        + "</w:tr>"
        + f"<w:tr><w:trPr>{row_props.format(a=author)}</w:trPr>"
        + cell("기록 행")
        + "</w:tr>"
        + "</w:tbl>"
        + p(r("뒤"))
    )


@pytest.mark.parametrize(
    ("record", "op", "kept"),
    [
        ('<w:ins w:id="1" w:author="{a}"/>', "accept_changes", True),
        ('<w:ins w:id="1" w:author="{a}"/>', "reject_changes", False),
        ('<w:del w:id="1" w:author="{a}"/>', "accept_changes", False),
        ('<w:del w:id="1" w:author="{a}"/>', "reject_changes", True),
    ],
)
def test_row_records(record, op, kept):
    doc = edited(row_doc(record), Edit(op=op))
    assert ("기록 행" in [x.text for x in doc.paragraphs]) == kept
    assert not any(x.tracked for x in doc.paragraphs)
    assert_valid(doc.to_bytes())


def test_rejecting_inserted_last_row_removes_table():
    data = make_docx(
        '<w:tbl><w:tblGrid/><w:tr><w:trPr><w:ins w:id="1" w:author="가"/></w:trPr>'
        + cell("혼자")
        + "</w:tr></w:tbl>"
        + p(r("뒤"))
    )
    doc = edited(data, Edit(op="reject_changes"))
    assert [x.text for x in doc.paragraphs if x.text] == ["뒤"]
    assert document_root(doc.to_bytes()).find(".//w:tbl", NS) is None


def test_reject_property_changes_restores_old_properties():
    data = make_docx(
        '<w:tbl><w:tblPr><w:jc w:val="center"/>'
        '<w:tblPrChange w:id="1" w:author="가"><w:tblPr><w:jc w:val="left"/></w:tblPr></w:tblPrChange>'
        '</w:tblPr><w:tblGrid><w:gridCol w:w="500"/>'
        '<w:tblGridChange w:id="2"><w:tblGrid><w:gridCol w:w="900"/></w:tblGrid></w:tblGridChange>'
        "</w:tblGrid><w:tr><w:trPr><w:cantSplit/>"
        '<w:trPrChange w:id="3" w:author="가"><w:trPr/></w:trPrChange></w:trPr>'
        '<w:tc><w:tcPr><w:shd w:fill="FF0000"/><w:tcPrChange w:id="4" w:author="가"><w:tcPr/>'
        "</w:tcPrChange></w:tcPr>"
        + p(r("칸"))
        + "</w:tc></w:tr></w:tbl>"
        + p(r("뒤"))
        + '<w:sectPr><w:pgSz w:w="16838"/><w:sectPrChange w:id="5" w:author="가"><w:sectPr>'
        '<w:pgSz w:w="11906"/></w:sectPr></w:sectPrChange></w:sectPr>'
    )
    view = DocxDocument.from_bytes(data).view(1, 10).text
    assert "표 서식 바뀜" in view and "칸 서식 바뀜" in view and "구역 설정 바뀜" in view
    doc = edited(data, Edit(op="reject_changes"))
    root = document_root(doc.to_bytes())
    assert root.find(".//w:tblPr/w:jc", NS).get(f"{{{W}}}val") == "left"
    assert root.find(".//w:gridCol", NS).get(f"{{{W}}}w") == "900"
    assert len(root.find(".//w:trPr", NS)) == 0
    assert root.find(".//w:tcPr/w:shd", NS) is None
    assert root.find(".//w:sectPr/w:pgSz", NS).get(f"{{{W}}}w") == "11906"
    assert not any(x.tracked for x in doc.paragraphs)


def test_accept_property_changes_keeps_new_properties():
    data = make_docx(
        p(r("본문"))
        + '<w:sectPr><w:headerReference w:type="default" r:id="rIdH"/><w:pgSz w:w="16838"/>'
        '<w:sectPrChange w:id="5" w:author="가"><w:sectPr><w:pgSz w:w="11906"/></w:sectPr>'
        "</w:sectPrChange></w:sectPr>",
        header=p(r("머리")),
    )
    accepted = edited(data, Edit(op="accept_changes"))
    section = document_root(accepted.to_bytes()).find(".//w:body/w:sectPr", NS)
    assert section.find("w:pgSz", NS).get(f"{{{W}}}w") == "16838"
    rejected = edited(data, Edit(op="reject_changes"))
    section = document_root(rejected.to_bytes()).find(".//w:body/w:sectPr", NS)
    names = [etree.QName(c).localname for c in section]
    assert names[0] == "headerReference" and section.find("w:pgSz", NS).get(f"{{{W}}}w") == "11906"


def test_numbering_change_can_be_accepted_but_not_rejected():
    body = p(
        '<w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/>'
        '<w:numberingChange w:id="1" w:author="가" w:original="%1."/></w:numPr></w:pPr>' + r("항목")
    )
    data = make_docx(body)
    accepted = edited(data, Edit(op="accept_changes"))
    assert document_root(accepted.to_bytes()).find(".//w:numberingChange", NS) is None
    doc = DocxDocument.from_bytes(data)
    _, (failure,) = doc.apply_some([Edit(op="reject_changes")])
    assert "cannot be rejected" in str(failure)


def test_paragraph_scope_includes_its_row_record():
    doc = edited(
        row_doc('<w:ins w:id="1" w:author="가"/>'), Edit(op="reject_changes", paragraph="p2")
    )
    assert "기록 행" not in [x.text for x in doc.paragraphs]


# --- 작성자별 ---------------------------------------------------------------------------------


def two_authors() -> bytes:
    return make_docx(
        p(
            '<w:ins w:id="1" w:author="김민지">' + r("김 넣음") + "</w:ins>",
            r(" 그대로 "),
            '<w:del w:id="2" w:author="박지훈"><w:r><w:delText>박 지움</w:delText></w:r></w:del>',
        )
        + p(r("둘째"))
    )


def test_accept_by_author_leaves_other_records():
    doc = edited(two_authors(), Edit(op="accept_changes", author="김민지"))
    (first, _) = doc.paragraphs
    assert first.track_authors == ("박지훈",)
    assert first.marked == "김 넣음 그대로 {-박 지움-}"
    again = edited(doc.to_bytes(), Edit(op="reject_changes", author=" 박지훈 "))
    assert again.paragraphs[0].text == "김 넣음 그대로 박 지움"
    assert not again.paragraphs[0].tracked


def test_author_without_records_lists_authors():
    doc = DocxDocument.from_bytes(two_authors())
    _, (failure,) = doc.apply_some([Edit(op="accept_changes", author="이준")])
    assert "by 이준 has no tracked changes" in str(failure)
    assert "김민지, 박지훈" in str(failure)


def test_author_filter_covers_structure_records():
    data = row_doc('<w:ins w:id="1" w:author="{a}"/>', author="박지훈")
    doc = DocxDocument.from_bytes(data)
    _, (failure,) = doc.apply_some([Edit(op="reject_changes", author="김민지")])
    assert "no tracked changes" in str(failure)
    rejected = edited(data, Edit(op="reject_changes", author="박지훈"))
    assert "기록 행" not in [x.text for x in rejected.paragraphs]


def test_accept_a_then_b_equals_accept_all():
    by_steps = edited(
        edited(two_authors(), Edit(op="accept_changes", author="김민지")).to_bytes(),
        Edit(op="accept_changes", author="박지훈"),
    )
    at_once = edited(two_authors(), Edit(op="accept_changes"))
    assert shape(by_steps) == shape(at_once)


# --- 메모 답글·해결·지우기 ----------------------------------------------------------------------

W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
W15 = "http://schemas.microsoft.com/office/word/2012/wordml"


def commented() -> bytes:
    body = p(
        '<w:commentRangeStart w:id="3"/>' + r("매출은 120억") + '<w:commentRangeEnd w:id="3"/>',
        '<w:r><w:commentReference w:id="3"/></w:r>',
    ) + p(r("둘째"))
    comments = '<w:comment w:id="3" w:author="김민지"><w:p><w:r><w:t>근거 필요</w:t></w:r></w:p></w:comment>'
    return make_docx(body, comments=comments)


def parts(data: bytes) -> dict[str, bytes]:
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


def test_reply_creates_thread_and_shows_under_parent():
    doc = edited(commented(), Edit(op="comment", comment="c3", new_text="출처를 달았습니다."))
    lines = doc.view(1, 10).text.splitlines()
    assert lines[1] == "    (메모 c3 · 김민지: '근거 필요' → '매출은 120억')"
    assert (
        lines[2].startswith("      (↳ 답글 c") and "AI 에이전트: '출처를 달았습니다.'" in lines[2]
    )
    files = parts(doc.to_bytes())
    extended = etree.fromstring(files["word/commentsExtended.xml"])
    entries = extended.findall(f"{{{W15}}}commentEx")
    assert len(entries) == 2 and entries[1].get(f"{{{W15}}}paraIdParent") == entries[0].get(
        f"{{{W15}}}paraId"
    )
    comments = etree.fromstring(files["word/comments.xml"])
    para_ids = [c.findall(".//w:p", NS)[-1].get(f"{{{W14}}}paraId") for c in comments]
    assert all(para_ids) and len(set(para_ids)) == 2
    assert b"commentsExtended+xml" in files["[Content_Types].xml"]
    assert b"2011/relationships/commentsExtended" in files["word/_rels/document.xml.rels"]
    root = document_root(doc.to_bytes())
    assert len(root.findall(".//w:commentReference", NS)) == 2
    assert_valid(doc.to_bytes())


def test_reply_to_reply_goes_to_parent():
    once = edited(commented(), Edit(op="comment", comment="c3", new_text="첫 답글"))
    reply = once.paragraphs[0].comments[0].replies[0].id
    twice = edited(once.to_bytes(), Edit(op="comment", comment=f"c{reply}", new_text="둘째 답글"))
    (comment,) = twice.paragraphs[0].comments
    assert [x.text for x in comment.replies] == ["첫 답글", "둘째 답글"]


def test_resolve_with_reply_marks_done():
    doc = edited(commented(), Edit(op="resolve_comment", comment="3", new_text="고쳤습니다."))
    (comment,) = doc.paragraphs[0].comments
    assert comment.resolved and comment.replies[0].text == "고쳤습니다."
    assert "· 해결됨)" in doc.view(1, 5).text
    again = DocxDocument.from_bytes(doc.to_bytes())
    (change,) = again.apply_edits([Edit(op="resolve_comment", comment="c3")])
    assert "already resolved" in change.note


def test_delete_comment_removes_thread_everywhere():
    doc = edited(commented(), Edit(op="comment", comment="c3", new_text="답글"))
    gone = edited(doc.to_bytes(), Edit(op="delete_comment", comment="c3"))
    assert not gone.paragraphs[0].comments
    files = parts(gone.to_bytes())
    root = document_root(gone.to_bytes())
    assert root.find(".//w:commentRangeStart", NS) is None
    assert root.find(".//w:commentReference", NS) is None
    assert etree.fromstring(files["word/comments.xml"]).find("w:comment", NS) is None
    assert etree.fromstring(files["word/commentsExtended.xml"]).find(f"{{{W15}}}commentEx") is None
    assert gone.paragraphs[0].text == "매출은 120억"
    assert_valid(gone.to_bytes())


def test_delete_only_a_reply():
    doc = edited(commented(), Edit(op="comment", comment="c3", new_text="답글"))
    reply = doc.paragraphs[0].comments[0].replies[0].id
    kept = edited(doc.to_bytes(), Edit(op="delete_comment", comment=f"c{reply}"))
    (comment,) = kept.paragraphs[0].comments
    assert comment.id == "3" and not comment.replies


def test_delete_comment_then_paragraph_in_one_call():
    doc = edited(
        commented(),
        Edit(op="delete_comment", comment="c3"),
        Edit(op="delete_paragraph", paragraph="p1"),
    )
    assert [x.text for x in doc.paragraphs] == ["둘째"]


def test_unknown_comment_lists_ids():
    doc = DocxDocument.from_bytes(commented())
    _, (failure,) = doc.apply_some([Edit(op="delete_comment", comment="c9")])
    assert "c9 does not exist (comments: c3)" in str(failure)


def test_existing_comment_ids_part_gets_new_entry():
    import io
    import zipfile

    source = commented()
    buffer = io.BytesIO()
    cid = "http://schemas.microsoft.com/office/word/2016/wordml/cid"
    with zipfile.ZipFile(io.BytesIO(source)) as zin, zipfile.ZipFile(buffer, "w") as zout:
        for info in zin.infolist():
            data = zin.read(info)
            if info.filename == "word/_rels/document.xml.rels":
                data = data.replace(
                    b"</Relationships>",
                    b'<Relationship Id="rIdI" Type="http://schemas.microsoft.com/office/2016/09/'
                    b'relationships/commentsIds" Target="commentsIds.xml"/></Relationships>',
                )
            zout.writestr(info, data)
        zout.writestr("word/commentsIds.xml", f'<w16cid:commentsIds xmlns:w16cid="{cid}"/>')
    doc = edited(buffer.getvalue(), Edit(op="comment", comment="c3", new_text="답글"))
    ids = etree.fromstring(parts(doc.to_bytes())["word/commentsIds.xml"])
    (entry,) = ids
    assert entry.get(f"{{{cid}}}durableId") and entry.get(f"{{{cid}}}paraId")


def test_accepting_deleted_commented_text_drops_the_comment():
    doc = DocxDocument.from_bytes(commented())
    doc.apply_edits([Edit(op="delete_paragraph", paragraph="p1")], track=True)
    out = doc.to_bytes()
    assert "메모 c3" in DocxDocument.from_bytes(out).view(1, 5).text
    accepted = DocxDocument.from_bytes(out)
    (change,) = accepted.apply_edits([Edit(op="accept_changes")])
    assert "comments on the removed text deleted: c3" in change.note
    data = accepted.to_bytes()
    assert_valid(data)
    assert [x.text for x in DocxDocument.from_bytes(data).paragraphs] == ["둘째"]


def test_comment_ops_work_in_tracking_mode():
    doc = DocxDocument.from_bytes(commented())
    doc.apply_edits(
        [
            Edit(op="comment", comment="c3", new_text="답글"),
            Edit(op="resolve_comment", comment="c3"),
        ],
        track=True,
    )
    (comment,) = DocxDocument.from_bytes(doc.to_bytes()).paragraphs[0].comments
    assert comment.resolved and len(comment.replies) == 1
