"""DOCX 문서 요소(그림·필드·각주·문서 정보)의 XML 도우미 (묶음 7).

``docx.py`` 를 import 하지 않는 순수 함수만 둔다. 문서를 고치는 흐름(번호, 추적, 부분 만들기)은
``docx.DocxDocument`` 에 있다.

- 그림: Word 가 만드는 인라인 그림(``w:drawing/wp:inline``) 모양을 따른다. 크기 단위는 EMU
  (1cm = 360000, 1 twip = 635, 96dpi 의 1px = 9525).
- 요소 순서: ``w:pPr``·``w:sectPr``·``w:settings`` 의 자식은 순서가 정해져 있어 틀리면 Word 가 "읽을 수 없는
  내용" 을 낸다. ``insert_ordered`` 로 제자리에 넣는다.
"""

from dataclasses import dataclass
from xml.sax.saxutils import quoteattr

from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
PIC = "http://schemas.openxmlformats.org/drawingml/2006/picture"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
V = "urn:schemas-microsoft-com:vml"
PICTURE_URI = PIC
"""그림인 ``a:graphicData`` 의 ``uri`` (도형·차트·SmartArt 는 다르다)."""

CORE_PROPERTIES = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
DC = "http://purl.org/dc/elements/1.1/"
DCTERMS = "http://purl.org/dc/terms/"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
REL_CORE = "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties"
CORE_TYPE = "application/vnd.openxmlformats-package.core-properties+xml"

EMU_PER_CM = 360000
EMU_PER_TWIP = 635
EMU_PER_PIXEL = 9525
"""96dpi 기준 1px 의 EMU. 그림 원래 크기를 정할 때 쓴다 (Word 와 같다)."""

IMAGE_TYPES = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "gif": "image/gif",
    "bmp": "image/bmp",
}
"""문서에 넣을 수 있는 그림 확장자 → 내용 형식."""

IMAGE_MARKER = "okdoit-image"
"""Markdown 의 프로젝트 그림 자리 표시 (run 안, ``ref``·``alt`` 속성). 문서에 넣을 때 그림으로 바꾼다."""
NOTE_MARKER = "okdoit-note"
"""Markdown 의 각주 자리 표시 (run 안, ``text`` 속성). 문서에 넣을 때 각주로 바꾼다."""

PROPERTY_TAGS = {
    "title": f"{{{DC}}}title",
    "author": f"{{{DC}}}creator",
    "subject": f"{{{DC}}}subject",
    "keywords": f"{{{CORE_PROPERTIES}}}keywords",
}
"""``set_properties`` 로 바꾸는 문서 정보 → ``docProps/core.xml`` 의 요소."""

PROPERTY_LABELS = {"title": "제목", "author": "작성자", "subject": "주제", "keywords": "키워드"}
"""보기에 적는 문서 정보 이름."""


def _w(name: str) -> str:
    return f"{{{W}}}{name}"


@dataclass(frozen=True)
class ImageData:
    """문서에 넣을 그림 파일.

    Attributes:
        data: 그림 파일 내용 (PNG·JPEG·GIF).
        extension: 파일 확장자 (``"png"`` 등, ``IMAGE_TYPES`` 의 키).
        width: 가로 픽셀 수.
        height: 세로 픽셀 수.
    """

    data: bytes
    extension: str
    width: int
    height: int


# --- 요소 순서 -----------------------------------------------------------------------------------

PARAGRAPH_ORDER = [
    "pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr", "widowControl", "numPr",
    "suppressLineNumbers", "pBdr", "shd", "tabs", "suppressAutoHyphens", "kinsoku", "wordWrap",
    "overflowPunct", "topLinePunct", "autoSpaceDE", "autoSpaceDN", "bidi", "adjustRightInd",
    "snapToGrid", "spacing", "ind", "contextualSpacing", "mirrorIndents", "suppressOverlap", "jc",
    "textDirection", "textAlignment", "textboxTightWrap", "outlineLvl", "divId", "cnfStyle", "rPr",
    "sectPr", "pPrChange",
]  # fmt: skip
"""``w:pPr`` 자식 순서."""

SECTION_ORDER = [
    "headerReference", "footnotePr", "endnotePr", "type", "pgSz", "pgMar", "paperSrc",
    "pgBorders", "lnNumType", "pgNumType", "cols", "formProt", "vAlign", "noEndnote", "titlePg",
    "textDirection", "bidi", "rtlGutter", "docGrid", "printerSettings", "sectPrChange",
]  # fmt: skip
"""``w:sectPr`` 자식 순서. 머리글·바닥글 참조는 둘 다 맨 앞 묶음이라 ``headerReference`` 하나로 센다."""

SETTINGS_ORDER = [
    "writeProtection", "view", "zoom", "removePersonalInformation", "removeDateAndTime",
    "doNotDisplayPageBoundaries", "displayBackgroundShape", "printPostScriptOverText",
    "printFractionalCharacterWidth", "printFormsData", "embedTrueTypeFonts", "embedSystemFonts",
    "saveSubsetFonts", "saveFormsData", "mirrorMargins", "alignBordersAndEdges",
    "bordersDoNotSurroundHeader", "bordersDoNotSurroundFooter", "gutterAtTop",
    "hideSpellingErrors", "hideGrammaticalErrors", "activeWritingStyle", "proofState",
    "formsDesign", "attachedTemplate", "linkStyles", "stylePaneFormatFilter",
    "stylePaneSortMethod", "documentType", "mailMerge", "revisionView", "trackRevisions",
    "doNotTrackMoves", "doNotTrackFormatting", "documentProtection", "autoFormatOverride",
    "styleLockTheme", "styleLockQFSet", "defaultTabStop", "autoHyphenation",
    "consecutiveHyphenLimit", "hyphenationZone", "doNotHyphenateCaps", "showEnvelope",
    "summaryLength", "clickAndTypeStyle", "defaultTableStyle", "evenAndOddHeaders",
    "bookFoldRevPrinting", "bookFoldPrinting", "bookFoldPrintingSheets",
    "drawingGridHorizontalSpacing", "drawingGridVerticalSpacing",
    "displayHorizontalDrawingGridEvery", "displayVerticalDrawingGridEvery",
    "doNotUseMarginsForDrawingGridOrigin", "drawingGridHorizontalOrigin",
    "drawingGridVerticalOrigin", "doNotShadeFormData", "noPunctuationKerning",
    "characterSpacingControl", "printTwoOnOne", "strictFirstAndLastChars", "noLineBreaksAfter",
    "noLineBreaksBefore", "savePreviewPicture", "doNotValidateAgainstSchema", "saveInvalidXml",
    "ignoreMixedContent", "alwaysShowPlaceholderText", "doNotDemarcateInvalidXml",
    "saveXmlDataOnly", "useXSLTWhenSaving", "saveThroughXslt", "showXMLTags",
    "alwaysMergeEmptyNamespace", "updateFields", "hdrShapeDefaults", "footnotePr", "endnotePr",
    "compat", "docVars", "rsids", "mathPr", "attachedSchema", "themeFontLang",
    "clrSchemeMapping", "doNotIncludeSubdocsInStats", "doNotAutoCompressPictures",
    "forceUpgrade", "captions", "readModeInkLockDown", "smartTagType", "schemaLibrary",
    "shapeDefaults", "doNotEmbedSmartTags", "decimalSymbol", "listSeparator",
]  # fmt: skip
"""``w:settings`` 자식 순서."""


def _order_key(element: etree._Element, order: list[str]) -> int:
    """순서 목록에서의 위치. W 이름공간이 아닌 확장 요소·모르는 요소는 맨 뒤로 본다."""
    if not isinstance(element.tag, str):
        return -1
    name = etree.QName(element)
    if name.namespace != W:
        return len(order)
    local = "headerReference" if name.localname == "footerReference" else name.localname
    return order.index(local) if local in order else len(order)


def insert_ordered(
    parent: etree._Element, element: etree._Element, order: list[str], *, replace: bool = True
) -> etree._Element:
    """``element`` 를 ``parent`` 의 정해진 순서 자리에 넣는다.

    Args:
        parent: 넣을 부모 (``w:pPr``·``w:sectPr``·``w:settings``).
        element: 넣을 요소.
        order: 부모의 자식 순서 (``PARAGRAPH_ORDER`` 등).
        replace: 같은 태그가 이미 있으면 바꿀지 (아니면 같은 태그들 뒤에 더한다).

    Returns:
        넣은 요소.
    """
    if replace:
        old = parent.find(element.tag)
        if old is not None:
            old.addprevious(element)
            parent.remove(old)
            return element
    key = _order_key(element, order)
    for child in parent:
        if _order_key(child, order) > key:
            child.addprevious(element)
            return element
    parent.append(element)
    return element


def paragraph_prop(props: etree._Element, name: str, **attrs: str) -> etree._Element:
    """문단 서식(``w:pPr``)에 ``w:<name>`` 을 순서에 맞게 넣거나 바꾼다."""
    element = etree.Element(_w(name))
    for key, value in attrs.items():
        element.set(_w(key), value)
    return insert_ordered(props, element, PARAGRAPH_ORDER)


# --- 그림 ----------------------------------------------------------------------------------------


def inline_drawing(
    rel_id: str, shape_id: int, cx: int, cy: int, name: str, description: str
) -> etree._Element:
    """인라인 그림 ``w:drawing`` (Word 가 만드는 모양).

    Args:
        rel_id: 그림 파일 관계 id (``a:blip`` 의 ``r:embed``).
        shape_id: 문서 안에서 유일한 개체 id (``wp:docPr`` 의 ``id``).
        cx: 너비 (EMU).
        cy: 높이 (EMU).
        name: 개체 이름 (예: ``"그림 3"``).
        description: 대체 글자 (빈 글자면 넣지 않는다).

    Returns:
        ``w:drawing`` 요소.
    """
    descr = f" descr={quoteattr(description)}" if description else ""
    xml = (
        f'<w:drawing xmlns:w="{W}" xmlns:wp="{WP}" xmlns:a="{A}" xmlns:pic="{PIC}" '
        f'xmlns:r="{R}"><wp:inline distT="0" distB="0" distL="0" distR="0">'
        f'<wp:extent cx="{cx}" cy="{cy}"/><wp:effectExtent l="0" t="0" r="0" b="0"/>'
        f'<wp:docPr id="{shape_id}" name={quoteattr(name)}{descr}/>'
        '<wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr>'
        f'<a:graphic><a:graphicData uri="{PICTURE_URI}"><pic:pic><pic:nvPicPr>'
        f'<pic:cNvPr id="0" name={quoteattr(name)}/><pic:cNvPicPr/></pic:nvPicPr>'
        f'<pic:blipFill><a:blip r:embed="{rel_id}"/><a:stretch><a:fillRect/></a:stretch>'
        '</pic:blipFill><pic:spPr><a:xfrm><a:off x="0" y="0"/>'
        f'<a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        "</pic:spPr></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing>"
    )
    return etree.fromstring(xml)


def is_picture(drawing: etree._Element) -> bool:
    """``w:drawing`` 이 그림인지 (도형·차트·SmartArt·글상자가 아니라)."""
    data = drawing.find(f".//{{{A}}}graphicData")
    return data is not None and data.get("uri") == PICTURE_URI


def drawing_frame(drawing: etree._Element) -> etree._Element | None:
    """그림의 ``wp:inline`` 이나 ``wp:anchor``."""
    for child in drawing:
        if child.tag in (f"{{{WP}}}inline", f"{{{WP}}}anchor"):
            return child
    return None


def drawing_size(drawing: etree._Element) -> tuple[int, int] | None:
    """그림 크기 ``(너비, 높이)`` (EMU, ``wp:extent``). 모르면 ``None``."""
    frame = drawing_frame(drawing)
    extent = frame.find(f"{{{WP}}}extent") if frame is not None else None
    if extent is None:
        return None
    try:
        cx, cy = int(extent.get("cx", "0")), int(extent.get("cy", "0"))
    except ValueError:
        return None
    return (cx, cy) if cx > 0 and cy > 0 else None


def set_drawing_size(drawing: etree._Element, cx: int, cy: int) -> None:
    """그림 크기를 바꾼다 (``wp:extent`` 와 그림 모양의 ``a:ext``)."""
    frame = drawing_frame(drawing)
    if frame is None:
        return
    extent = frame.find(f"{{{WP}}}extent")
    if extent is not None:
        extent.set("cx", str(cx))
        extent.set("cy", str(cy))
    shape = drawing.find(f".//{{{PIC}}}spPr/{{{A}}}xfrm/{{{A}}}ext")
    if shape is not None:
        shape.set("cx", str(cx))
        shape.set("cy", str(cy))


def drawing_blip(drawing: etree._Element) -> etree._Element | None:
    """그림 파일을 가리키는 ``a:blip``."""
    return drawing.find(f".//{{{PIC}}}blipFill/{{{A}}}blip")


def drawing_properties(drawing: etree._Element) -> etree._Element | None:
    """그림의 ``wp:docPr`` (개체 id·이름·대체 글자)."""
    frame = drawing_frame(drawing)
    return frame.find(f"{{{WP}}}docPr") if frame is not None else None


def is_inline(drawing: etree._Element) -> bool:
    """글자 사이에 놓인 그림인지 (떠 있는 그림 ``wp:anchor`` 가 아니라)."""
    frame = drawing_frame(drawing)
    return frame is not None and frame.tag == f"{{{WP}}}inline"


def cm(emu: int) -> str:
    """EMU 를 보기용 cm 글자로 (``"12.0"``)."""
    return f"{emu / EMU_PER_CM:.1f}"


def image_extension(name: str) -> str:
    """ZIP 항목 이름의 확장자 (소문자, ``"png"``)."""
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


# --- 필드 ----------------------------------------------------------------------------------------


def field_runs(instruction: str, result: str, props: etree._Element | None) -> list[etree._Element]:
    """복합 필드 run 다섯 개 (시작·명령·구분·결과·끝). 결과 글자는 Word 가 다시 계산한다.

    ``w:fldSimple`` 은 변경 추적 기록(``w:ins``)에 넣을 수 없어서 늘 복합 필드로 쓴다.
    """
    runs = []
    for kind, value in (
        ("begin", None),
        ("instr", instruction),
        ("separate", None),
        ("text", result),
        ("end", None),
    ):
        run = etree.Element(_w("r"))
        if props is not None:
            run.append(etree.fromstring(etree.tostring(props)))
        if kind == "instr":
            node = etree.SubElement(run, _w("instrText"))
            node.text = value
            node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        elif kind == "text":
            node = etree.SubElement(run, _w("t"))
            node.text = value
        else:
            etree.SubElement(run, _w("fldChar")).set(_w("fldCharType"), kind)
        runs.append(run)
    return runs


PAGE_FIELDS = {
    "{쪽}": " PAGE ",
    "{PAGE}": " PAGE ",
    "{전체 쪽}": " NUMPAGES ",
    "{NUMPAGES}": " NUMPAGES ",
}
"""``set_header``·``set_footer`` 글자 안의 쪽 번호 자리 → 필드 명령."""


def split_page_fields(text: str) -> list[tuple[str, str]]:
    """글자를 ``(종류, 값)`` 조각으로 나눈다. 종류는 ``"text"`` 나 ``"field"`` (값은 필드 명령)."""
    pieces: list[tuple[str, str]] = []
    rest = text
    while rest:
        found = [(rest.find(k), k) for k in PAGE_FIELDS if k in rest]
        if not found:
            pieces.append(("text", rest))
            break
        at, key = min(found)
        if at:
            pieces.append(("text", rest[:at]))
        pieces.append(("field", PAGE_FIELDS[key]))
        rest = rest[at + len(key) :]
    return pieces


def toc_instruction(levels: int) -> str:
    r"""목차 필드 명령 (제목 1~``levels``, 링크, 웹 보기에서 쪽 번호 숨김, 개요 수준)."""
    return f' TOC \\o "1-{levels}" \\h \\z \\u '


# --- 문서 정보 -----------------------------------------------------------------------------------


def new_core_properties() -> etree._Element:
    """빈 문서 정보 부분(``docProps/core.xml``)."""
    return etree.Element(
        f"{{{CORE_PROPERTIES}}}coreProperties",
        nsmap={"cp": CORE_PROPERTIES, "dc": DC, "dcterms": DCTERMS, "xsi": XSI},
    )


def read_core_properties(root: etree._Element | None) -> dict[str, str]:
    """문서 정보 ``{"title": …}`` (값이 있는 것만, ``PROPERTY_TAGS`` 순서)."""
    if root is None:
        return {}
    found = {}
    for key, tag in PROPERTY_TAGS.items():
        element = root.find(tag)
        value = (element.text or "").strip() if element is not None else ""
        if value:
            found[key] = value
    return found


def set_core_property(root: etree._Element, tag: str, value: str) -> None:
    """문서 정보 하나를 바꾼다. 빈 글자면 지운다."""
    element = root.find(tag)
    if not value:
        if element is not None:
            root.remove(element)
        return
    if element is None:
        element = etree.SubElement(root, tag)
    element.text = value


def set_modified(root: etree._Element, author: str, stamp: str) -> None:
    """마지막으로 고친 사람·시각을 적는다."""
    set_core_property(root, f"{{{CORE_PROPERTIES}}}lastModifiedBy", author)
    modified = root.find(f"{{{DCTERMS}}}modified")
    if modified is None:
        modified = etree.SubElement(root, f"{{{DCTERMS}}}modified")
    modified.set(f"{{{XSI}}}type", "dcterms:W3CDTF")
    modified.text = stamp
