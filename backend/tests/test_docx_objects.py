"""묶음 7 (문서 요소) 테스트: 그림, 각주·미주, 글상자, 머리글·바닥글·쪽 번호, 목차, 문서 정보.

추적으로 고친 결과는 "모두 수락 = 바로 고친 결과", "모두 거절 = 원본" 이어야 한다 (``roundtrip``).
"""

import io
import zipfile

import pytest
from lxml import etree
from PIL import Image
from test_docx_editing import FIXTURE, W, make_docx, p, r
from test_docx_review import DATE, assert_valid, edited
from test_docx_review_structure import roundtrip
from test_docx_structure import STYLES_H, h

from app.documents.editing.docx import WALL, DocxDocument
from app.documents.editing.docx_objects import EMU_PER_CM, ImageData
from app.documents.editing.text import DocumentProperties, Edit, EditError

NS = {
    "w": W,
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
}
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"


def png(width: int = 400, height: int = 200) -> ImageData:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buffer, "PNG")
    return ImageData(buffer.getvalue(), "png", width, height)


IMAGES = {"img:abc": png()}


def entries(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


def xml(data: bytes, name: str) -> etree._Element:
    return etree.fromstring(entries(data)[name])


def apply(data: bytes, *edits: Edit, **options) -> bytes:
    doc = DocxDocument.from_bytes(data)
    doc.apply_edits(edits, images=IMAGES, date=DATE, **options)
    out = doc.to_bytes()
    assert_valid(out)
    return out


def failure(data: bytes, edit: Edit) -> str:
    doc = DocxDocument.from_bytes(data)
    _, (error,) = doc.apply_some([edit], images=IMAGES)
    return str(error)


def lines(data: bytes) -> list[str]:
    return DocxDocument.from_bytes(data).view(1, 200).text.splitlines()


def with_parts(data: bytes, parts: dict[str, str], rels: list[tuple[str, str, str]]) -> bytes:
    """``make_docx`` 결과에 부분과 본문 관계를 더한다 (각주·미주 시험 문서)."""
    files = entries(data)
    ns = (
        'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    )
    for name, body in parts.items():
        tag = name.rsplit("/", 1)[-1].removesuffix(".xml").rstrip("0123456789")
        files[name] = f"<w:{tag} {ns}>{body}</w:{tag}>".encode()
    rels_xml = files["word/_rels/document.xml.rels"].decode()
    extra = "".join(
        f'<Relationship Id="{i}" Type="{_REL}{t}" Target="{target}"/>' for i, t, target in rels
    )
    files["word/_rels/document.xml.rels"] = rels_xml.replace(
        "</Relationships>", extra + "</Relationships>"
    ).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buffer.getvalue()


SEPARATORS = (
    '<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>'
    '<w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:r><w:continuationSeparator/>'
    "</w:r></w:p></w:footnote>"
)


def footnoted() -> bytes:
    """p1 '매출은 120억 원이다.[각주]', p2 '끝', 각주 f1 '출처: 보고서'."""
    body = p(r("매출은 120억 원"), '<w:r><w:footnoteReference w:id="2"/></w:r>', r("이다.")) + p(
        r("끝")
    )
    notes = (
        SEPARATORS + '<w:footnote w:id="2"><w:p><w:r><w:footnoteRef/></w:r>'
        '<w:r><w:t xml:space="preserve"> 출처: 보고서</w:t></w:r></w:p></w:footnote>'
    )
    return with_parts(
        make_docx(body), {"word/footnotes.xml": notes}, [("rIdF", "footnotes", "footnotes.xml")]
    )


# --- 보기 ---------------------------------------------------------------------------------------


def test_view_shows_footnotes_after_footers_with_marks_in_text():
    assert lines(footnoted()) == [
        "[p1] 매출은 120억 원[각주 f1]이다.",
        "[p2] 끝",
        "[p3] (각주 f1) 출처: 보고서",
    ]
    doc = DocxDocument.from_bytes(footnoted())
    assert doc.paragraphs[0].text == f"매출은 120억 원{WALL}이다."


def test_object_labels_in_old_text_are_accepted():
    out = apply(footnoted(), Edit(old_text="원[각주 f1]이다", new_text="원[각주 f1]입니다"))
    assert lines(out)[0] == "[p1] 매출은 120억 원[각주 f1]입니다."


def test_footnote_text_is_editable():
    out = apply(footnoted(), Edit(old_text="보고서", new_text="사업보고서", paragraph="p3"))
    assert lines(out)[2] == "[p3] (각주 f1) 출처: 사업보고서"


TEXTBOX = (
    '<w:r><mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/'
    '2006"><mc:Choice Requires="wps"><w:drawing><wp:anchor xmlns:wp="http://schemas.openxmlformats.'
    'org/drawingml/2006/wordprocessingDrawing"><a:graphic xmlns:a="http://schemas.openxmlformats.org/'
    'drawingml/2006/main"><a:graphicData uri="http://schemas.microsoft.com/office/word/2010/'
    'wordprocessingShape"><wps:wsp xmlns:wps="http://schemas.microsoft.com/office/word/2010/'
    'wordprocessingShape"><wps:txbx><w:txbxContent><w:p><w:r><w:t>2024년 11월</w:t></w:r></w:p>'
    "</w:txbxContent></wps:txbx></wps:wsp></a:graphicData></a:graphic></wp:anchor></w:drawing>"
    '</mc:Choice><mc:Fallback><w:pict><v:shape xmlns:v="urn:schemas-microsoft-com:vml"><v:textbox>'
    "<w:txbxContent><w:p><w:r><w:t>2024년 11월</w:t></w:r></w:p></w:txbxContent></v:textbox>"
    "</v:shape></w:pict></mc:Fallback></mc:AlternateContent></w:r>"
)


def test_textbox_is_shown_and_editing_updates_the_fallback_copy():
    data = make_docx(p(r("표지"), TEXTBOX) + p(r("본문")))
    assert lines(data) == [
        "[p1] 표지[글상자 1]",
        "[p2] 본문",
        "[p3] (글상자 1 · p1 안) 2024년 11월",
    ]
    out = apply(data, Edit(old_text="11월", new_text="12월", paragraph="p3"))
    texts = [t.text for t in xml(out, "word/document.xml").iter(f"{{{W}}}t")]
    assert texts.count("2024년 12월") == 2 and "2024년 11월" not in texts


# --- 그림 ---------------------------------------------------------------------------------------


def test_insert_project_image_with_width_and_caption():
    data = make_docx(p(r("구성은 다음과 같다.")) + p(r("끝")), styles=STYLES_H)
    out = apply(
        data,
        Edit(
            op="insert_image",
            paragraph="p1",
            image="img:abc",
            width=8,
            new_text="구성도",
            caption="그림 1. 구성도",
        ),
    )
    assert lines(out) == [
        "[p1] 구성은 다음과 같다.",
        "[p2] [그림 g1]",
        "    (그림 g1 · 8.0×4.0cm · 대체 글자 '구성도')",
        "[p3] 그림 1. 구성도",
        "[p4] 끝",
    ]
    files = entries(out)
    assert files["word/media/image1.png"] == IMAGES["img:abc"].data
    types = etree.fromstring(files["[Content_Types].xml"])
    assert types.find("ct:Default[@Extension='png']", NS).get("ContentType") == "image/png"
    root = xml(out, "word/document.xml")
    extent = root.find(".//wp:extent", NS)
    assert (int(extent.get("cx")), int(extent.get("cy"))) == (8 * EMU_PER_CM, 4 * EMU_PER_CM)
    assert root.find(".//w:p[2]/w:pPr/w:jc", NS).get(f"{{{W}}}val") == "center"
    styles = xml(out, "word/styles.xml")
    assert styles.find("w:style[@w:styleId='Caption']", NS) is not None


def test_insert_image_is_narrowed_to_the_page_width():
    data = make_docx(p(r("가")))
    doc = DocxDocument.from_bytes(data)
    (change,) = doc.apply_edits(
        [Edit(op="insert_image", paragraph="p1", image="img:abc", width=40)], images=IMAGES
    )
    assert "narrower" in change.note
    cx = int(xml(doc.to_bytes(), "word/document.xml").find(".//wp:extent", NS).get("cx"))
    assert cx == (11906 - 2 * 1440) * 635


def test_copy_image_of_the_same_document_reuses_its_file():
    out = apply(FIXTURE.read_bytes(), Edit(op="insert_image", paragraph="p1", image="g1"))
    files = entries(out)
    assert [n for n in files if n.startswith("word/media/")] == ["word/media/image1.png"]
    embeds = {
        b.get(f"{{{NS['r']}}}embed")
        for b in xml(out, "word/document.xml").iter(f"{{{NS['a']}}}blip")
    }
    assert len(embeds) == 1
    ids = [e.get("id") for e in xml(out, "word/document.xml").iter(f"{{{NS['wp']}}}docPr")]
    assert len(ids) == len(set(ids)) == 2


def test_unknown_project_image_fails():
    message = failure(make_docx(p(r("가"))), Edit(op="insert_image", paragraph="p1", image="img:x"))
    assert "not found among the images" in message


def test_edit_image_width_and_alt_text():
    out = apply(
        FIXTURE.read_bytes(), Edit(op="edit_image", image="g1", width=4, new_text="회사 로고")
    )
    assert "    (그림 g1 · 4.0×2.0cm · 대체 글자 '회사 로고')" in lines(out)


def test_replacing_the_only_use_of_a_picture_removes_its_file():
    out = apply(FIXTURE.read_bytes(), Edit(op="edit_image", image="g1", target="img:abc"))
    files = entries(out)
    media = sorted(n for n in files if n.startswith("word/media/"))
    assert media == ["word/media/image2.png"]
    rels = xml(out, "word/_rels/document.xml.rels")
    targets = [x.get("Target") for x in rels if x.get("Type") == _REL + "image"]
    assert targets == ["media/image2.png"]
    # 너비는 그대로, 높이는 새 그림 비율 (2:1)
    assert "    (그림 g1 · 2.0×1.0cm)" in lines(out)


def test_delete_image_removes_run_and_file_but_keeps_text():
    out = apply(FIXTURE.read_bytes(), Edit(op="delete_object", image="g1"))
    assert "[p20] 로고:  (2024년 판)" in lines(out)
    assert not [n for n in entries(out) if n.startswith("word/media/")]


def test_delete_image_alone_in_its_paragraph_removes_the_paragraph():
    data = make_docx(p(r("가")) + p(r("나")))
    out = apply(data, Edit(op="insert_image", paragraph="p1", image="img:abc"))
    doc = DocxDocument.from_bytes(out)
    (change,) = doc.apply_edits([Edit(op="delete_object", image="g1")])
    assert "removed too" in change.note
    assert [x.text for x in doc.paragraphs] == ["가", "나"]


# --- 각주·미주 -----------------------------------------------------------------------------------


def test_add_footnote_creates_part_settings_and_styles():
    data = make_docx(p(r("매출은 120억 원이다.")), styles=STYLES_H)
    out = apply(
        data, Edit(op="footnote", paragraph="p1", old_text="120억 원", new_text="출처: 보고서")
    )
    assert lines(out) == ["[p1] 매출은 120억 원[각주 f1]이다.", "[p2] (각주 f1) 출처: 보고서"]
    notes = xml(out, "word/footnotes.xml")
    assert [n.get(f"{{{W}}}type") for n in notes] == ["separator", "continuationSeparator", None]
    settings = xml(out, "word/settings.xml")
    assert [e.get(f"{{{W}}}id") for e in settings.iterfind("w:footnotePr/w:footnote", NS)] == [
        "-1",
        "0",
    ]
    styles = xml(out, "word/styles.xml")
    assert styles.find("w:style[@w:styleId='FootnoteReference']", NS) is not None


def test_add_second_footnote_and_endnote():
    out = apply(
        footnoted(),
        Edit(op="footnote", paragraph="p2", old_text="끝", new_text="둘째"),
        Edit(op="footnote", paragraph="p1", old_text="매출은", new_text="미주", endnote=True),
    )
    assert lines(out) == [
        "[p1] 매출은[미주 e1] 120억 원[각주 f1]이다.",
        "[p2] 끝[각주 f2]",
        "[p3] (각주 f1) 출처: 보고서",
        "[p4] (각주 f2) 둘째",
        "[p5] (미주 e1) 미주",
    ]
    ids = [n.get(f"{{{W}}}id") for n in xml(out, "word/footnotes.xml")]
    assert ids == ["-1", "0", "2", "3"]


def test_footnote_ambiguous_text_fails():
    data = make_docx(p(r("가 가")))
    message = failure(data, Edit(op="footnote", paragraph="p1", old_text="가", new_text="주"))
    assert "appears 2 times" in message


def test_delete_footnote_removes_its_text():
    out = apply(footnoted(), Edit(op="delete_object", image="f1"))
    assert lines(out) == ["[p1] 매출은 120억 원이다.", "[p2] 끝"]
    assert [n.get(f"{{{W}}}id") for n in xml(out, "word/footnotes.xml")] == ["-1", "0"]


def test_deleting_a_paragraph_with_a_footnote_removes_the_footnote():
    doc = DocxDocument.from_bytes(footnoted())
    (change,) = doc.apply_edits([Edit(op="delete_paragraph", paragraph="p1")])
    assert "footnote/endnote text removed" in change.note
    assert [x.text for x in doc.paragraphs] == ["끝"]


# --- 머리글·바닥글 -------------------------------------------------------------------------------


def test_set_footer_with_page_numbers_creates_a_part():
    out = apply(make_docx(p(r("본문"))), Edit(op="set_footer", new_text="{쪽} / {전체 쪽}"))
    assert lines(out)[-1] == "[p2] (바닥글 1) 1 / 1"
    footer = xml(out, "word/footer1.xml")
    instructions = [t.text.strip() for t in footer.iter(f"{{{W}}}instrText")]
    assert instructions == ["PAGE", "NUMPAGES"]
    assert footer.find(".//w:jc", NS).get(f"{{{W}}}val") == "center"
    section = xml(out, "word/document.xml").find(".//w:sectPr", NS)
    ref = section.find("w:footerReference", NS)
    assert ref is not None and section[0] is ref


def test_set_header_replaces_text_header_of_every_section():
    data = make_docx(p(r("본문")), header=p(r("옛 머리글")))
    out = apply(data, Edit(op="set_header", new_text="주식회사 예시\t대외비"))
    assert lines(out)[-1] == "[p2] (머리글 1) 주식회사 예시\t대외비"


def test_set_header_refuses_a_header_with_a_logo():
    data = FIXTURE.read_bytes()
    doc = DocxDocument.from_bytes(data)
    doc.apply_edits([Edit(op="insert_image", paragraph="p22", image="g1")])
    message = failure(doc.to_bytes(), Edit(op="set_header", new_text="새 머리글"))
    assert "so that the logo is kept" in message


def test_footer_not_on_first_page():
    out = apply(make_docx(p(r("본문"))), Edit(op="set_footer", new_text="{쪽}", first_page=False))
    section = xml(out, "word/document.xml").find(".//w:sectPr", NS)
    names = [etree.QName(c).localname for c in section]
    assert names == ["footerReference", "footerReference", "titlePg"]
    assert {c.get(f"{{{W}}}type") for c in section[:2]} == {"default", "first"}


# --- 목차 ---------------------------------------------------------------------------------------


def toc_source() -> bytes:
    return make_docx(
        p(r("보고서")) + h(1, "1장") + p(r("가")) + h(2, "1.1 절") + h(1, "2장"), styles=STYLES_H
    )


def test_insert_toc_prefills_headings_and_asks_word_to_update():
    out = apply(toc_source(), Edit(op="insert_toc", paragraph="p1", levels=2))
    assert lines(out)[:6] == [
        "[p1] 보고서",
        "[p2] 목차",
        "[p3] (목차) 1장",
        "[p4] (목차) 1.1 절",
        "[p5] (목차) 2장",
        "[p6] (제목 1) 1장",
    ]
    instruction = next(xml(out, "word/document.xml").iter(f"{{{W}}}instrText")).text
    assert instruction.strip() == 'TOC \\o "1-2" \\h \\z \\u'
    assert xml(out, "word/settings.xml").find("w:updateFields", NS) is not None


def test_insert_toc_again_updates_the_existing_one():
    first = apply(toc_source(), Edit(op="insert_toc", paragraph="p1", levels=1))
    changed = apply(first, Edit(old_text="2장", new_text="둘째 장", paragraph="p8"))
    doc = DocxDocument.from_bytes(changed)
    (change,) = doc.apply_edits([Edit(op="insert_toc", levels=1)])
    assert "updated the table of contents" in change.note
    assert [x.text for x in doc.paragraphs][:4] == ["보고서", "목차", "1장", "둘째 장"]


def test_insert_toc_without_headings_fails():
    message = failure(make_docx(p(r("가"))), Edit(op="insert_toc", paragraph="p1"))
    assert "no headings" in message


# --- 문서 정보 -----------------------------------------------------------------------------------


def test_set_properties_creates_core_part():
    data = make_docx(p(r("가")))
    out = apply(
        data,
        Edit(op="set_properties", properties=DocumentProperties(title="계획", keywords="예산")),
    )
    doc = DocxDocument.from_bytes(out)
    assert doc.properties() == {"title": "계획", "keywords": "예산"}
    rels = xml(out, "_rels/.rels")
    assert any(x.get("Target") == "docProps/core.xml" for x in rels)


def test_set_properties_needs_a_value():
    message = failure(make_docx(p(r("가"))), Edit(op="set_properties"))
    assert "properties is required" in message


# --- 변경 추적 ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "edit",
    [
        Edit(op="insert_image", paragraph="p1", image="g1", caption="그림"),
        Edit(op="delete_object", image="g1"),
        Edit(op="footnote", paragraph="p2", old_text="120억", new_text="출처"),
        Edit(op="set_footer", new_text="{쪽} 쪽"),
        Edit(op="insert_toc", paragraph="p1"),
    ],
    ids=["insert_image", "delete_image", "footnote", "set_footer", "insert_toc"],
)
def test_tracked_round_trip(edit, monkeypatch):
    original = DocxDocument.apply_edits

    def with_images(self, edits, **options):
        return original(self, edits, images=IMAGES, **options)

    monkeypatch.setattr(DocxDocument, "apply_edits", with_images)
    doc, _ = roundtrip(FIXTURE.read_bytes(), edit)
    assert any(x.tracked for x in doc.paragraphs)


def test_tracked_image_size_change_is_noted():
    doc = DocxDocument.from_bytes(FIXTURE.read_bytes())
    (change,) = doc.apply_edits([Edit(op="edit_image", image="g1", width=3)], track=True)
    assert "not tracked" in change.note


def test_rejecting_a_tracked_footnote_removes_its_text():
    tracked = apply(
        footnoted(),
        Edit(op="footnote", paragraph="p2", old_text="끝", new_text="둘째"),
        track=True,
    )
    out = edited(tracked, Edit(op="reject_changes"))
    assert [n.get(f"{{{W}}}id") for n in xml(out.to_bytes(), "word/footnotes.xml")] == [
        "-1",
        "0",
        "2",
    ]


def test_object_names_refer_to_the_start_of_the_call():
    data = make_docx(p(r("가")) + p(r("나")))
    out = apply(data, Edit(op="insert_image", paragraph="p2", image="img:abc"))
    doc = DocxDocument.from_bytes(out)
    doc.apply_edits(
        [
            Edit(op="insert_image", paragraph="p1", position="before", image="img:abc"),
            Edit(op="edit_image", image="g1", width=2),
        ],
        images=IMAGES,
    )
    shown = doc.view(1, 10).text
    assert "(그림 g2 · 2.0×1.0cm)" in shown


def test_bad_object_name_fails():
    with pytest.raises(EditError, match="image must name an object"):
        DocxDocument.from_bytes(footnoted()).apply_edits([Edit(op="delete_object", image="p3")])


# --- Markdown 그림·각주 -------------------------------------------------------------------------


def test_insert_markdown_with_project_image_and_footnote():
    data = make_docx(p(r("앞")) + p(r("끝")), styles=STYLES_H)
    doc = DocxDocument.from_bytes(data)
    markdown = "## 구성\n\n구성은 아래와 같다[^1].\n\n![구성도](img:abc)\n\n[^1]: 설계 문서 3장"
    (change,) = doc.apply_edits(
        [Edit(op="insert_markdown", paragraph="p1", new_text=markdown)], images=IMAGES
    )
    assert "with 1 image and 1 footnote" in change.note
    out = doc.to_bytes()
    assert_valid(out)
    assert lines(out)[:6] == [
        "[p1] 앞",
        "[p2] (제목 2) 구성",
        "[p3] 구성은 아래와 같다[각주 f1].",
        "[p4] [그림 g1]",
        "    (그림 g1 · 10.6×5.3cm · 대체 글자 '구성도')",
        "[p5] 끝",
    ]
    assert lines(out)[-1] == "[p6] (각주 f1) 설계 문서 3장"


def test_insert_markdown_missing_project_image_keeps_alt_text():
    doc = DocxDocument.from_bytes(make_docx(p(r("앞"))))
    (change,) = doc.apply_edits(
        [Edit(op="insert_markdown", paragraph="p1", new_text="![구성도](img:none)")]
    )
    assert "not found" in change.note
    assert [x.text for x in doc.paragraphs] == ["앞", "[구성도]"]


def test_build_docx_with_project_image_and_footnote():
    from app.documents.editing.docx_create import build_docx

    created = build_docx(
        "# 보고서\n\n매출[^a]\n\n![차트](img:abc) ![웹](https://x/y.png)\n\n[^a]: 출처",
        author="AI",
        images=IMAGES,
    )
    assert_valid(created.data)
    assert lines(created.data) == [
        "[p1] (제목 1) 보고서",
        "[p2] 매출[각주 f1]",
        "[p3] [그림 g1] [웹]",
        "    (그림 g1 · 10.6×5.3cm · 대체 글자 '차트')",
        "[p4] (각주 f1) 출처",
    ]
    assert "web address" in created.notes[0]


# --- 서비스: 프로젝트 그림 ------------------------------------------------------------------------


def test_picture_file_converts_webp_to_png():
    from app.documents.editing.service import picture_file

    buffer = io.BytesIO()
    Image.new("RGBA", (30, 20), (0, 0, 255, 128)).save(buffer, "WEBP")
    picture = picture_file(buffer.getvalue())
    assert (picture.extension, picture.width, picture.height) == ("png", 30, 20)
    assert picture.data.startswith(b"\x89PNG")
    assert picture_file(b"not an image") is None
    assert picture_file(IMAGES["img:abc"].data).data == IMAGES["img:abc"].data


def test_image_refs_from_edits_and_markdown():
    import uuid

    from app.documents.editing.service import image_refs

    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    edits = [
        Edit(op="insert_image", paragraph="p1", image=f"img:{a}"),
        Edit(op="edit_image", image="g1", target=f"IMG:{str(b).upper()}"),
        Edit(op="insert_markdown", paragraph="p1", new_text=f"![x](img:{c})"),
        Edit(old_text=f"img:{uuid.uuid4()}", new_text="글자"),
    ]
    assert image_refs(edits) == {a, b, c}


async def test_project_images_reads_only_the_project_and_converts():
    import uuid

    from editing_fakes import FakeSession, FakeStorage

    from app.documents.editing.service import project_images

    element = uuid.uuid4()
    buffer = io.BytesIO()
    Image.new("RGB", (8, 4)).save(buffer, "WEBP")
    session = FakeSession(rows=[(element, "projects/p/images/d/0")])
    storage = FakeStorage({"projects/p/images/d/0": buffer.getvalue()})
    found = await project_images(session, storage, uuid.uuid4(), {element})
    assert list(found) == [f"img:{element}"] and found[f"img:{element}"].extension == "png"
    query = str(session.queries[0])
    assert "documents.project_id" in query and "document_elements.kind" in query
    assert await project_images(session, storage, None, {element}) == {}


def test_tracked_image_replacement_shows_old_and_new_picture():
    doc = DocxDocument.from_bytes(FIXTURE.read_bytes())
    doc.apply_edits(
        [Edit(op="edit_image", image="g1", target="img:abc")], track=True, images=IMAGES
    )
    out = doc.to_bytes()
    assert_valid(out)
    assert "[p20] (변경 추적: AI 에이전트) 로고: {-[그림]-}{+[그림 g1]+} (2024년 판)" in lines(out)
    rejected = edited(out, Edit(op="reject_changes")).to_bytes()
    assert_valid(rejected)
    assert sorted(n for n in entries(rejected) if n.startswith("word/media/")) == [
        "word/media/image1.png"
    ]


def test_footer_back_on_first_page():
    first = apply(make_docx(p(r("가"))), Edit(op="set_footer", new_text="{쪽}", first_page=False))
    out = apply(first, Edit(op="set_footer", new_text="{쪽} 쪽", first_page=True))
    assert xml(out, "word/document.xml").find(".//w:sectPr/w:titlePg", NS) is None
    assert lines(out)[-1] == "[p2] (바닥글 1) 1 쪽"
