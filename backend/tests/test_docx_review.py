"""DOCX 검토 흐름: 변경 추적 기록 수락·거절, 추적 문단 고치기, 변경 추적으로 고치기, 메모 달기.

결과 XML 은 Word 가 "읽을 수 없는 내용" 오류를 내지 않도록 구조 규칙(``assert_valid``)도 검사한다.
LibreOffice 는 잘못된 모양도 그대로 열어서 기계 확인만으로는 이 규칙을 보장하지 못한다 (설계 기록 11.11).
"""

import io
import re
import zipfile

import pytest
from lxml import etree
from test_docx_editing import COMMENTS, REF, REVIEW_KO, make_docx, p, r

from app.documents.editing.docx import DocxDocument, W
from app.documents.editing.text import Edit

DATE = "2026-11-24T10:00:00Z"
NS = {"w": W}


def edited(data: bytes, *edits: Edit, **options) -> DocxDocument:
    doc = DocxDocument.from_bytes(data)
    doc.apply_edits(edits, **options)
    return DocxDocument.from_bytes(doc.to_bytes())


def view(doc: DocxDocument) -> list[str]:
    return doc.view(1, 100).text.splitlines()


def assert_valid(data: bytes, author: str = "AI 에이전트") -> None:
    """Word 가 받아들이는 변경 추적·메모 구조인지 확인한다.

    id 는 우리(``author``)가 단 기록끼리, 그리고 문서의 다른 id 와 겹치지 않는지 본다 (LibreOffice 가 저장한
    시험 문서는 원래부터 서식 변경 기록 id 가 겹친다).
    """
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        parts = {n: zf.read(n) for n in zf.namelist() if n.endswith((".xml", ".rels"))}
    document = etree.fromstring(parts["word/document.xml"])
    for deleted in document.iter(f"{{{W}}}del", f"{{{W}}}moveFrom"):
        if deleted.getparent().tag == f"{{{W}}}rPr":
            continue
        assert not deleted.findall(".//w:t", NS), "w:del 안에는 w:delText 만 온다"
    for text in document.iter(f"{{{W}}}delText"):
        assert any(a.tag in (f"{{{W}}}del", f"{{{W}}}moveFrom") for a in text.iterancestors())
    for record in document.iter(f"{{{W}}}ins", f"{{{W}}}del"):
        assert not any(a.tag in (f"{{{W}}}ins", f"{{{W}}}del") for a in record.iterancestors()), (
            "추적 기록은 겹치지 않는다"
        )
    records = list(
        document.iter(f"{{{W}}}ins", f"{{{W}}}del", f"{{{W}}}rPrChange", f"{{{W}}}pPrChange")
    )
    ours = [e.get(f"{{{W}}}id") for e in records if e.get(f"{{{W}}}author") == author]
    others = {e.get(f"{{{W}}}id") for e in records if e.get(f"{{{W}}}author") != author}
    assert len(ours) == len(set(ours)) and not set(ours) & others, "새 기록 id 는 겹치지 않는다"
    starts = {e.get(f"{{{W}}}id") for e in document.iter(f"{{{W}}}commentRangeStart")}
    ends = {e.get(f"{{{W}}}id") for e in document.iter(f"{{{W}}}commentRangeEnd")}
    refs = {e.get(f"{{{W}}}id") for e in document.iter(f"{{{W}}}commentReference")}
    assert starts == ends and starts <= refs
    if refs:
        comments = etree.fromstring(parts["word/comments.xml"])
        assert refs <= {c.get(f"{{{W}}}id") for c in comments.iter(f"{{{W}}}comment")}
    for element in document.iter():
        assert not str(element.tag).endswith("okdoit-paragraph")


# --- 수락·거절 --------------------------------------------------------------------------------


def test_accept_all_keeps_insertions_and_drops_deletions():
    doc = edited(REVIEW_KO.read_bytes(), Edit(op="accept_changes"))
    texts = [x.text for x in doc.paragraphs]
    assert texts[1] == "2024년 매출은 135억 원이다."
    assert texts[3] == "검토자가 넣은 문단이다."
    assert texts[5] == "팀 인원은 12명으로 늘었다."
    assert not any(x.tracked for x in doc.paragraphs)
    assert_valid(doc.to_bytes())


def test_reject_all_restores_deletions_and_joins_inserted_paragraph():
    doc = DocxDocument.from_bytes(REVIEW_KO.read_bytes())
    (change,) = doc.apply_edits([Edit(op="reject_changes")])
    assert "1 paragraph joined" in change.note
    texts = [x.text for x in doc.paragraphs]
    assert texts[1] == "2024년 매출은 120억 원이다."
    assert "검토자가 넣은 문단이다." not in texts
    assert texts[-1] == "지운 문장이다.팀 인원은 12명으로 늘었다."
    assert not any(x.tracked for x in doc.paragraphs)


def test_reject_format_change_restores_old_formatting():
    run = (
        '<w:r><w:rPr><w:b/><w:rPrChange w:id="1" w:author="가"><w:rPr><w:i/></w:rPr>'
        "</w:rPrChange></w:rPr><w:t>글</w:t></w:r>"
    )
    doc = edited(make_docx(p(run)), Edit(op="reject_changes", paragraph="p1"))
    props = doc.paragraphs[0].element.find(f".//{{{W}}}rPr")
    assert [etree.QName(c).localname for c in props] == ["i"]


def test_review_one_paragraph_only():
    doc = edited(REVIEW_KO.read_bytes(), Edit(op="accept_changes", paragraph="p2"))
    assert [x.number for x in doc.paragraphs if x.tracked] == [4, 5, 6]


def test_review_without_tracked_changes_fails():
    doc = DocxDocument.from_bytes(make_docx(p(r("글"))))
    _, (failure,) = doc.apply_some([Edit(op="accept_changes")])
    assert "no tracked changes" in str(failure)


def test_table_structure_changes_are_refused():
    row = (
        '<w:tbl><w:tr><w:trPr><w:ins w:id="1" w:author="가"/></w:trPr><w:tc><w:p>'
        + r("칸")
        + "</w:p></w:tc></w:tr></w:tbl>"
    )
    doc = DocxDocument.from_bytes(make_docx(row + p(r("뒤"))))
    _, (failure,) = doc.apply_some([Edit(op="accept_changes")])
    assert "tables, sections or numbering" in str(failure)


# --- 추적 문단 고치기 ----------------------------------------------------------------------


def test_tracked_paragraph_can_be_edited_away_from_the_records():
    doc = edited(
        REVIEW_KO.read_bytes(),
        Edit("2024년 매출은", "2024년 총매출은", paragraph="p2"),
        Edit("팀 인원은", "팀원 수는", paragraph="p5"),
    )
    assert view(doc)[1] == "[p2] (변경 추적: 김민지) 2024년 총매출은 {-120-}{+135+}억 원이다."
    assert doc.paragraphs[4].text == "팀원 수는 12명으로 늘었다."


@pytest.mark.parametrize(
    ("old", "new", "ref"),
    [
        ("135억", "140억", "p2"),  # 남이 넣은 글자
        ("은 135", "은 140", "p2"),  # 남이 지운 자리를 넘는다
        ("12명", "13명", "p5"),  # 서식 변경 기록이 있는 글자
    ],
)
def test_edits_touching_someone_elses_records_are_refused(old, new, ref):
    doc = DocxDocument.from_bytes(REVIEW_KO.read_bytes())
    _, (failure,) = doc.apply_some([Edit(old, new, paragraph=ref)])
    assert "accept_changes" in str(failure)


def test_tracked_paragraph_cannot_be_split():
    doc = DocxDocument.from_bytes(REVIEW_KO.read_bytes())
    _, (failure,) = doc.apply_some([Edit("매출은 ", "매출은\n", paragraph="p2")])
    assert "cannot be split" in str(failure)


# --- 변경 추적으로 고치기 ---------------------------------------------------------------------


def test_tracked_replace_records_only_the_difference():
    doc = edited(
        make_docx(p(r("매출은 120억 원이다.", bold=True))),
        Edit("120억 원이다.", "125억 원이었다."),
        track=True,
        date=DATE,
    )
    assert view(doc) == ["[p1] (변경 추적: AI 에이전트) 매출은 12{-0-}{+5+}억 원이{+었+}다."]
    assert doc.paragraphs[0].text == "매출은 125억 원이었다."
    element = doc.paragraphs[0].element
    record = element.find(f"{{{W}}}ins")
    assert record.get(f"{{{W}}}date") == DATE
    assert record.find(f".//{{{W}}}b") is not None  # 넣은 글자도 서식을 따른다
    assert_valid(doc.to_bytes())


def test_tracked_change_ids_do_not_collide_with_existing_ids():
    doc = DocxDocument.from_bytes(REVIEW_KO.read_bytes())
    doc.apply_edits([Edit("120억", "125억", paragraph="p3")], track=True)
    assert_valid(doc.to_bytes())
    with zipfile.ZipFile(io.BytesIO(doc.to_bytes())) as zf:
        ids = [int(i) for i in re.findall(rb'w:id="(\d+)"', zf.read("word/document.xml"))]
    with zipfile.ZipFile(io.BytesIO(REVIEW_KO.read_bytes())) as zf:
        before = [int(i) for i in re.findall(rb'w:id="(\d+)"', zf.read("word/document.xml"))]
    assert min(set(ids) - set(before)) > max(before)


def test_accepting_tracked_edits_gives_the_direct_result():
    data = make_docx(p(r("첫 문장. 둘째 문장.")) + p(r("지울 문단")) + p(r("끝")))
    edits = [
        Edit("첫 문장.", "처음 문장."),
        Edit(op="insert_paragraph", paragraph="p1", new_text="새 문단"),
        Edit(op="delete_paragraph", paragraph="p2"),
    ]
    direct = edited(data, *edits)
    tracked = edited(data, *edits, track=True)
    assert_valid(tracked.to_bytes())
    accepted = edited(tracked.to_bytes(), Edit(op="accept_changes"))
    assert [x.text for x in accepted.paragraphs] == [x.text for x in direct.paragraphs]
    rejected = edited(tracked.to_bytes(), Edit(op="reject_changes"))
    assert [x.text for x in rejected.paragraphs] == [
        x.text for x in DocxDocument.from_bytes(data).paragraphs
    ]


def test_tracked_split_marks_the_new_paragraph_break():
    doc = edited(make_docx(p(r("가. 나."))), Edit("가. 나.", "가.\n나."), track=True)
    assert [x.text for x in doc.paragraphs] == ["가.", "나."]
    assert "문단 넣음·나눔 추적" in doc.paragraphs[0].track_notes
    assert_valid(doc.to_bytes())
    rejected = edited(doc.to_bytes(), Edit(op="reject_changes"))
    assert [x.text for x in rejected.paragraphs] == ["가. 나."]


def test_own_tracked_text_is_edited_in_place():
    first = edited(make_docx(p(r("매출 120억"))), Edit("120억", "125억"), track=True)
    again = edited(first.to_bytes(), Edit("125억", "130억"), track=True)
    assert view(again) == ["[p1] (변경 추적: AI 에이전트) 매출 1{-20-}{+30+}억"]
    assert_valid(again.to_bytes())
    inserted = edited(
        make_docx(p(r("가"))),
        Edit(op="insert_paragraph", paragraph="p1", new_text="새 글"),
        track=True,
    )
    fixed = edited(inserted.to_bytes(), Edit("새 글", "고친 글"), track=True)
    assert view(fixed)[1] == "[p2] (변경 추적: AI 에이전트 · 문단 넣음·나눔 추적) {+고친 글+}"


def test_structure_ops_fail_in_tracking_mode():
    doc = DocxDocument.from_bytes(make_docx(p(r("가")) + p(r("나"))))
    _, failures = doc.apply_some(
        [Edit(op="merge", paragraph="p1"), Edit(op="move_paragraph", paragraph="p2", target="p1")],
        track=True,
    )
    assert all("cannot be made as a tracked change" in str(f) for f in failures)


def test_tracked_delete_of_last_paragraph_fails():
    doc = DocxDocument.from_bytes(make_docx(p(r("가")) + p(r("나"))))
    _, (failure,) = doc.apply_some([Edit(op="delete_paragraph", paragraph="p2")], track=True)
    assert "last paragraph" in str(failure)


def test_document_setting_says_whether_to_track():
    doc = DocxDocument.from_bytes(REVIEW_KO.read_bytes())
    assert doc.tracks_revisions is True  # LibreOffice 에서 "변경 내용 기록" 을 켠 채 저장했다
    assert DocxDocument.from_bytes(make_docx(p(r("가")))).tracks_revisions is False


# --- 메모 ---------------------------------------------------------------------------------


def test_comment_creates_comments_part():
    data = make_docx(p(r("2024년 매출은 120억 원이다.")))
    doc = edited(
        data, Edit("120억 원", "근거가\n필요합니다", op="comment"), author="검토 봇", date=DATE
    )
    assert view(doc) == [
        "[p1] 2024년 매출은 120억 원이다.",
        "    (메모 c1 · 검토 봇: '근거가 / 필요합니다' → '120억 원')",
    ]
    out = doc.to_bytes()
    assert_valid(out, author="검토 봇")
    with zipfile.ZipFile(io.BytesIO(out)) as zf:
        types = zf.read("[Content_Types].xml").decode()
        rels = zf.read("word/_rels/document.xml.rels").decode()
    assert "comments+xml" in types and rels.count("comments.xml") == 1


def test_comment_on_whole_paragraph_and_existing_part():
    data = make_docx(p(r("점 메모"), REF.format(4)) + p(r("둘째 문단")), comments=COMMENTS)
    doc = edited(data, Edit(op="comment", paragraph="p2", new_text="문단 전체 메모"))
    assert view(doc)[-1].startswith("    (메모 c5 · AI 에이전트: '문단 전체 메모' → '둘째 문단')")
    assert_valid(doc.to_bytes())


def test_comment_inside_one_run_keeps_text_and_formatting():
    doc = edited(make_docx(p(r("앞 강조 뒤", bold=True))), Edit("강조", "확인", op="comment"))
    assert doc.paragraphs[0].text == "앞 강조 뒤"
    assert len(doc.paragraphs[0].element.findall(f".//{{{W}}}b")) == 3


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (Edit("없는 글", "메모", op="comment"), "not found"),
        (Edit("글", "", op="comment"), "cannot be empty"),
        (Edit(op="comment", new_text="메모"), "set old_text"),
    ],
)
def test_comment_errors(edit, message):
    doc = DocxDocument.from_bytes(make_docx(p(r("글")) + p(r("글"))))
    _, (failure,) = doc.apply_some([edit])
    assert message in str(failure)


def test_failed_comment_leaves_no_new_part():
    doc = DocxDocument.from_bytes(make_docx(p(r("글"))))
    _, failures = doc.apply_some([Edit("없음", "메모", op="comment")])
    assert failures
    with zipfile.ZipFile(io.BytesIO(doc.to_bytes())) as zf:
        assert "word/comments.xml" not in zf.namelist()


def test_text_files_reject_review_ops_and_tracking():
    from app.documents.editing.text import apply_some

    _, changes, (failure,) = apply_some("글", [Edit("글", "메모", op="comment")])
    assert not changes and "only works for Word" in str(failure)
