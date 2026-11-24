r"""DOCX 의 수정 로직. ZIP 안의 XML 을 lxml 로 직접 고친다.

목표는 **바꾼 글자 말고는 아무것도 달라지지 않는 것**이다. 그래서

- 고친 XML 부분(본문·머리글·바닥글)만 다시 쓰고, 나머지 ZIP 항목은 내용 그대로 복사한다.
- 문단을 "글자 목록"으로 펼쳐 글자마다 출처(run 과 그 안의 위치)를 기억한다. 화면에서는 한 문장이어도
  XML 에서는 서식 조각(run) 여러 개로 나뉘어 있기 때문이다.
- 바꿀 때는 원래 글자와 새 글자를 글자 단위로 비교해 **달라진 부분만** 고친다. 통째로 바꾸면
  바꾸지 않은 글자의 서식까지 하나로 뭉개진다.

고칠 수 없는 요소(그림, 필드, 각주 번호, 수식 등)는 "벽" 글자로 펼친다. 보기에서는 ``￼`` 로 보이고
(필드는 결과 글자 그대로), 바꾸는 범위에 걸리면 거절한다. 하이퍼링크·콘텐츠 컨트롤은 경계를 넘는
바꾸기를 거절한다. 변경 내용 추적 기록(``w:ins``·``w:del`` 등)이 있는 문단은 고치지 않는다.

오류 메시지는 에이전트(LLM)에게 도구 결과로 그대로 전달되므로 영어로 쓴다 (보기의 위치 표시는 한국어).
DB 와 저장소를 모르는 순수 함수라 테스트하기 쉽다.
"""

import io
import re
import zipfile
from collections.abc import Iterator, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import Any, Literal

from lxml import etree

from app.documents.editing.text import (
    EDIT_OPS,
    SNIPPET_CHARS,
    Change,
    Edit,
    EditError,
    TextFormat,
    in_url,
    matches_query,
    snippets,
    targets_url,
    url_spans,
)

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
"""WordprocessingML 네임스페이스."""

_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_MATH = "http://schemas.openxmlformats.org/officeDocument/2006/math"
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_REL_OFFICE_DOCUMENT = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
)
_REL_BASE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
W15 = "http://schemas.microsoft.com/office/word/2012/wordml"
W16CID = "http://schemas.microsoft.com/office/word/2016/wordml/cid"
W16CEX = "http://schemas.microsoft.com/office/word/2018/wordml/cex"
_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_REL_COMMENTS_EXTENDED = "http://schemas.microsoft.com/office/2011/relationships/commentsExtended"
_REL_COMMENTS_IDS = "http://schemas.microsoft.com/office/2016/09/relationships/commentsIds"
_REL_COMMENTS_EXTENSIBLE = (
    "http://schemas.microsoft.com/office/2018/08/relationships/commentsExtensible"
)
_COMMENTS_EXTENDED_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtended+xml"
)
_PARA_ID = f"{{{W14}}}paraId"
_CONTENT_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
_COMMENTS_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"
_STYLES_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"
_NUMBERING_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"

WALL = "￼"
"""고칠 수 없는 자리(그림, 각주 번호 등)를 보기에서 나타내는 글자 (OBJECT REPLACEMENT CHARACTER)."""

LINE_BREAK = "⏎"
"""보기와 수정 글자에서 문단 안 줄바꿈(``w:br``·``w:cr``)을 나타내는 글자. ``\\n`` 은 문단 나누기다."""

_TRACK_MARKS = re.compile(r"\{[+-]|[+-]\}")
"""보기의 추적 표기. 에이전트가 ``old_text`` 에 표기째 복사하는 실수를 찾는다."""

_PARAGRAPH_PREFIX = re.compile(r"\s*\[p\d+\]", re.IGNORECASE)
"""보기(``view``)가 문단 앞에 붙이는 번호. 에이전트가 ``old_text`` 에 잘못 넣는 경우를 찾는다."""

_PARAGRAPH_REF = re.compile(r"\[?\s*p?(\d+)\s*\]?", re.IGNORECASE)

_FORBIDDEN_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
"""XML 에 넣을 수 없거나 Word 문단에 맞지 않는 제어 문자 (``\t`` 와 ``\n`` 은 따로 다룬다)."""

_MARK = "okdoit-paragraph"
"""수정 호출 동안 문단(``w:p``)에 다는 임시 속성 (호출을 시작할 때의 번호). 호출이 끝나면 지운다."""

_SPLIT = "okdoit-split"
"""문단을 나눌 자리에 잠깐 넣는 ``w:br`` 의 표시 속성. 바꾸기를 마친 뒤 그 자리에서 문단을 나눈다."""

WHOLE_REPLACE_RATIO = 0.5
"""원래 글자와 새 글자의 비슷한 정도(``SequenceMatcher.ratio``)가 이보다 낮으면 범위를 통째로 바꾼다.

거의 다 바뀐 문장을 글자 단위로 맞추면 우연히 같은 글자만 남아 run 조각이 이상하게 남기 때문이다.
"""


def _w(name: str) -> str:
    return f"{{{W}}}{name}"


# run 안에서 글자가 아니지만 길이도 없는 요소. 그대로 둔다 (보기에 보이지 않는다).
_RUN_SILENT = {
    _w(n)
    for n in (
        "fldChar",
        "instrText",
        "delText",
        "delInstrText",
        "lastRenderedPageBreak",
        "softHyphen",
        "annotationRef",
        "commentReference",
    )
}

# 문단 안에서 길이 없는 표시. 그대로 둔다.
_PARAGRAPH_SILENT = {
    _w(n)
    for n in (
        "pPr",
        "proofErr",
        "bookmarkStart",
        "bookmarkEnd",
        "commentRangeStart",
        "commentRangeEnd",
        "permStart",
        "permEnd",
        "moveFromRangeStart",
        "moveFromRangeEnd",
        "moveToRangeStart",
        "moveToRangeEnd",
        "customXmlInsRangeStart",
        "customXmlInsRangeEnd",
        "customXmlDelRangeStart",
        "customXmlDelRangeEnd",
        "customXmlMoveFromRangeStart",
        "customXmlMoveFromRangeEnd",
        "customXmlMoveToRangeStart",
        "customXmlMoveToRangeEnd",
    )
}

# 안의 글자를 일반 글자처럼 다루되, 바꾸기가 경계를 넘으면 안 되는 묶음.
_SCOPES = {_w("hyperlink"), _w("sdtContent")}
# 경계 없이 안으로 들어가는 묶음.
_TRANSPARENT = {_w("smartTag"), _w("customXml"), _w("dir"), _w("bdo")}
# 변경 내용 추적 기록. 보이는 글자(ins, moveTo)와 지워진 글자(del, moveFrom).
_TRACKED_SHOWN = {_w("ins"), _w("moveTo")}
_TRACKED_HIDDEN = {_w("del"), _w("moveFrom")}
# 메모 범위 표시. 보기에서 메모가 가리키는 글자를 찾는 데 쓴다.
_COMMENT_RANGE = {_w("commentRangeStart"), _w("commentRangeEnd")}
DEFAULT_AUTHOR = "AI 에이전트"
"""에이전트가 남기는 변경 추적 기록·메모의 기본 작성자."""
FORMAT_CHANGE = "\0format:"
"""``_Glyph.tracked`` 에서 서식 변경 기록이 있는 run 의 글자를 뜻하는 접두어. 뒤에 기록의 작성자를 붙인다
(작성자 이름과 겹치지 않게 NUL 로 시작한다)."""
_COMMENT_CHARS = 120
"""보기에 보여줄 메모 내용 글자 수의 상한."""
_ANCHOR_CHARS = 40
"""보기에 보여줄 메모 범위 글자 수의 상한."""

_SKIPPABLE_BLOCK = {
    _w(n) for n in ("bookmarkStart", "bookmarkEnd", "proofErr", "permStart", "permEnd")
}
"""문단 사이(블록 수준)에 올 수 있는 길이 없는 표시. 이웃 문단을 찾을 때 건너뛴다."""

_HEADING = re.compile(r"heading (\d)$", re.IGNORECASE)


def _shorten(value: str) -> str:
    return value if len(value) <= SNIPPET_CHARS else value[: SNIPPET_CHARS - 1] + "…"


def _head(value: str, size: int = 30) -> str:
    """나뉜 문단을 알아볼 수 있을 만큼의 앞부분."""
    return value if len(value) <= size else value[: size - 1] + "…"


def _in_address_link(paragraph: "Paragraph", start: int, end: int) -> bool:
    """``[start, end)`` 가 웹 주소를 글자로 보여주는 하이퍼링크(``w:hyperlink``)에 걸치는지."""
    for glyph in paragraph.glyphs[start:end]:
        scope = glyph.scope
        if scope is None or scope.tag != _w("hyperlink"):
            continue
        shown = "".join(g.char for g in paragraph.glyphs if g.scope is scope).strip()
        if url_spans(shown)[:1] == [(0, len(shown))]:
            return True
    return False


def _paragraph_text(paragraph: etree._Element) -> str:
    """문단 요소의 ``w:t`` 글자를 이어 붙인 것 (결과 요약용)."""
    return "".join(t.text or "" for t in paragraph.iter(_w("t")))


# --- run 을 토큰으로 ------------------------------------------------------------------------------


@dataclass
class _Token:
    """run 내용의 한 조각. 글자 하나, 탭, 줄바꿈, 또는 건드리지 않는 요소.

    Attributes:
        kind: ``"c"`` 글자, ``"tab"`` 탭, ``"br"`` 줄바꿈, ``"x"`` 그대로 두는 요소.
        char: ``"c"`` 의 글자.
        element: 원래 XML 요소 (새로 만든 글자·탭은 ``None``).
    """

    kind: Literal["c", "tab", "br", "x"]
    char: str = ""
    element: etree._Element | None = None


def _is_text_break(element: etree._Element) -> bool:
    if element.tag == _w("cr"):
        return True
    return element.tag == _w("br") and element.get(_w("type"), "textWrapping") == "textWrapping"


def _tokens(run: etree._Element) -> list[_Token]:
    """Run 내용(``w:rPr`` 제외)을 순서대로 토큰으로 나눈다. 같은 run 이면 늘 같은 결과다."""
    tokens: list[_Token] = []
    for child in run:
        if child.tag == _w("rPr") or not isinstance(child.tag, str):
            continue
        if child.tag == _w("t"):
            tokens.extend(_Token("c", ch) for ch in child.text or "")
        elif child.tag == _w("tab"):
            tokens.append(_Token("tab", "\t", child))
        elif _is_text_break(child):
            tokens.append(_Token("br", LINE_BREAK, child))
        else:
            tokens.append(_Token("x", "", child))
    return tokens


def _write_run(run: etree._Element, tokens: list[_Token]) -> None:
    """토큰을 run 의 내용으로 다시 쓴다. 남은 내용이 없으면 run 을 지운다."""
    if not tokens:
        parent = run.getparent()
        if parent is not None:
            parent.remove(run)
        return
    for child in list(run):
        if child.tag != _w("rPr"):
            run.remove(child)
    text: list[str] = []

    def flush() -> None:
        if not text:
            return
        value = "".join(text)
        t = etree.SubElement(run, _w("t"))
        t.text = value
        if value[0].isspace() or value[-1].isspace() or "  " in value:
            t.set(_XML_SPACE, "preserve")
        text.clear()

    for token in tokens:
        if token.kind == "c":
            text.append(token.char)
            continue
        flush()
        if token.element is not None:
            run.append(token.element)
        elif token.kind == "br":  # 새로 넣은 줄바꿈
            etree.SubElement(run, _w("br"))
        else:  # 새로 넣은 탭
            etree.SubElement(run, _w("tab"))
    flush()


def _split_marker() -> etree._Element:
    marker = etree.Element(_w("br"))
    marker.set(_SPLIT, "1")
    return marker


def _text_tokens(text: str) -> list[_Token]:
    r"""넣을 글자를 run 토큰으로. ``\t`` 은 탭, ``⏎`` 는 줄바꿈, ``\n`` 은 문단을 나눌 자리 표시."""
    tokens = []
    for ch in text:
        if ch == "\t":
            tokens.append(_Token("tab", "\t"))
        elif ch == LINE_BREAK:
            tokens.append(_Token("br", LINE_BREAK))
        elif ch == "\n":
            tokens.append(_Token("x", "", _split_marker()))
        else:
            tokens.append(_Token("c", ch))
    return tokens


_TRACKING_PROPS = {_w(n) for n in ("ins", "del", "moveFrom", "moveTo", "rPrChange", "pPrChange")}
"""서식 안에 든 변경 추적 기록. 새 글자·문단의 서식으로 복사하지 않는다."""


def _run_props_for(paragraph: etree._Element) -> etree._Element | None:
    """문단에 새로 넣을 run 의 서식. 글자가 든 첫 run, 없으면 문단 기호 서식(``w:pPr/w:rPr``)을 복사한다."""
    runs = list(paragraph.iter(_w("r")))
    for run in sorted(runs, key=lambda x: x.find(_w("t")) is None):
        props = run.find(_w("rPr"))
        if props is not None:
            return deepcopy(props)
    mark = paragraph.find(f"{_w('pPr')}/{_w('rPr')}")
    if mark is None:
        return None
    props = etree.Element(_w("rPr"))
    for child in mark:
        if child.tag not in _TRACKING_PROPS:
            props.append(deepcopy(child))
    return props


_BEFORE_NUMBERING = {
    _w(n) for n in ("pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr", "widowControl")
}
"""문단 서식(``w:pPr``) 안에서 ``w:numPr`` 보다 앞에 와야 하는 요소 (스키마 순서)."""


def _insert_ordered(props: etree._Element, numbering: etree._Element) -> None:
    """``w:numPr`` 를 스키마 순서에 맞는 자리에 넣는다."""
    position = 0
    for i, child in enumerate(props):
        if child.tag in _BEFORE_NUMBERING:
            position = i + 1
    props.insert(position, numbering)


def _without_tracking(props: etree._Element) -> etree._Element:
    """서식 요소 안의 변경 추적 기록(``w:pPrChange``, ``w:rPr/w:ins`` 등)을 지운다 (제자리에서)."""
    for element in list(props.iter()):
        if element.tag in _TRACKING_PROPS and element is not props:
            parent = element.getparent()
            if parent is not None:
                parent.remove(element)
    return props


_BLOCKS = {_w(n) for n in ("p", "tbl", "sdt", "customXml")}


def _keeps_structure(container: etree._Element, without: etree._Element | None = None) -> bool:
    """``container``(표 칸, 본문, 머리글 등)의 블록이 ``without`` 없이도 올바른지.

    Word 는 표 칸·머리글·바닥글·본문이 문단으로 끝나기를 바란다. 블록이 하나도 없거나 표로 끝나면 안 된다.
    """
    blocks = [c for c in container if c.tag in _BLOCKS and c is not without]
    return bool(blocks) and blocks[-1].tag != _w("tbl")


def _drop_bookmark_partners(root: etree._Element, paragraph: etree._Element) -> None:
    """지울 문단 안의 책갈피 시작·끝과 짝이 되는, 문단 밖의 끝·시작을 지운다."""
    inside = {
        (e.tag, e.get(_w("id"))) for e in paragraph.iter(_w("bookmarkStart"), _w("bookmarkEnd"))
    }
    partner = {_w("bookmarkStart"): _w("bookmarkEnd"), _w("bookmarkEnd"): _w("bookmarkStart")}
    wanted = {(partner[tag], bookmark_id) for tag, bookmark_id in inside}
    for element in list(root.iter(_w("bookmarkStart"), _w("bookmarkEnd"))):
        if (element.tag, element.get(_w("id"))) in wanted and not _within(element, paragraph):
            parent = element.getparent()
            if parent is not None:
                parent.remove(element)


def _blank_cell(column: etree._Element, value: str) -> etree._Element:
    """기준 칸(``w:tc``)의 칸 서식·첫 문단 서식·첫 글자 서식만 따른 새 칸. ``value`` 를 글자로 넣는다."""
    cell = column.makeelement(_w("tc"))
    cell_props = column.find(_w("tcPr"))
    if cell_props is not None:
        copied = _without_tracking(deepcopy(cell_props))
        for drop in list(copied.iter(_w("cnfStyle"), _w("vMerge"))):
            drop.getparent().remove(drop)
        cell.append(copied)
    first = column.find(_w("p"))
    paragraph = etree.SubElement(cell, _w("p"))
    run_props = None
    if first is not None:
        props = first.find(_w("pPr"))
        if props is not None:
            copied = _without_tracking(deepcopy(props))
            for drop in list(copied.iter(_w("cnfStyle"))):
                drop.getparent().remove(drop)
            paragraph.append(copied)
        run_props = _run_props_for(first)
        if run_props is not None:
            run_props = _without_tracking(run_props)
    if value:
        run = etree.SubElement(paragraph, _w("r"))
        if run_props is not None:
            run.append(run_props)
        _write_run(run, _text_tokens(value))
    elif run_props is not None:
        # 빈 칸: 나중에 채울 글자가 같은 서식을 따르도록 문단 기호 서식으로 남긴다.
        props = paragraph.find(_w("pPr"))
        if props is None:
            props = etree.SubElement(paragraph, _w("pPr"))
        if props.find(_w("rPr")) is None:
            props.append(run_props)
    return cell


def _within(element: etree._Element, ancestor: etree._Element) -> bool:
    return any(x is ancestor for x in element.iterancestors()) or element is ancestor


# --- 표 격자 -------------------------------------------------------------------------------------


def _fix_container_end(container: etree._Element) -> None:
    """본문이 블록 없이 끝나거나 표로 끝나면 빈 문단을 붙인다 (Word 는 문단으로 끝나기를 바란다)."""
    if _keeps_structure(container):
        return
    paragraph = etree.Element(_w("p"))
    section = container.find(_w("sectPr"))
    if section is not None:
        section.addprevious(paragraph)
    else:
        container.append(paragraph)


_DEFAULT_TEXT_WIDTH = 11906 - 2 * 1440
"""구역 설정을 모를 때의 본문 너비 (A4 − 좌우 2.54cm, twip)."""


def _apply_body_format(
    paragraph: etree._Element,
    paragraph_props: etree._Element | None,
    run_props: etree._Element | None,
) -> None:
    """Markdown 으로 만든 본문 문단에 이웃 문단의 서식을 입힌다. 굵게 등 Markdown 서식은 그 위에 둔다."""
    if paragraph_props is not None and paragraph.find(_w("pPr")) is None:
        paragraph.insert(0, deepcopy(paragraph_props))
    if run_props is None:
        return
    for run in paragraph.iter(_w("r")):
        own = run.find(_w("rPr"))
        merged = deepcopy(run_props)
        if own is not None:
            for child in own:
                name = etree.QName(child).localname
                if name in _RUN_PROP_ORDER:
                    _set_prop(
                        merged, name, {etree.QName(k).localname: v for k, v in child.attrib.items()}
                    )
            run.remove(own)
        run.insert(0, merged)


def _int_val(element: etree._Element | None, default: int) -> int:
    if element is None:
        return default
    try:
        return int(element.get(_w("val"), default))
    except ValueError:
        return default


def _span(cell: etree._Element) -> int:
    """칸이 차지하는 격자 열 수 (``w:gridSpan``)."""
    return max(_int_val(cell.find(f"{_w('tcPr')}/{_w('gridSpan')}"), 1), 1)


def _vmerge(cell: etree._Element) -> str | None:
    """칸의 세로 병합: ``"restart"`` (병합 시작), ``"continue"`` (위 칸에 이어짐), 없으면 ``None``."""
    merge = cell.find(f"{_w('tcPr')}/{_w('vMerge')}")
    if merge is None:
        return None
    return "restart" if merge.get(_w("val")) == "restart" else "continue"


def _set_vmerge(cell: etree._Element, value: str | None) -> None:
    """칸의 세로 병합을 바꾼다 (``None`` 이면 뗀다). ``w:tcPr`` 자식 순서를 지킨다."""
    props = cell.find(_w("tcPr"))
    merge = props.find(_w("vMerge")) if props is not None else None
    if value is None:
        if merge is not None:
            props.remove(merge)
        return
    if props is None:
        props = etree.Element(_w("tcPr"))
        cell.insert(0, props)
    if merge is None:
        merge = etree.Element(_w("vMerge"))
        position = 0
        for i, child in enumerate(props):
            if child.tag in _BEFORE_VMERGE:
                position = i + 1
        props.insert(position, merge)
    if value == "restart":
        merge.set(_w("val"), "restart")
    else:
        merge.attrib.pop(_w("val"), None)


_BEFORE_VMERGE = {_w(n) for n in ("cnfStyle", "tcW", "gridSpan", "hMerge")}
"""칸 서식(``w:tcPr``) 안에서 ``w:vMerge`` 보다 앞에 오는 요소 (스키마 순서)."""


def _set_span(cell: etree._Element, span: int) -> None:
    """칸의 가로 병합 수(``w:gridSpan``)를 바꾼다. 1이면 뗀다."""
    props = cell.find(_w("tcPr"))
    element = props.find(_w("gridSpan")) if props is not None else None
    if span <= 1:
        if element is not None:
            props.remove(element)
        return
    if props is None:
        props = etree.Element(_w("tcPr"))
        cell.insert(0, props)
    if element is None:
        element = etree.Element(_w("gridSpan"))
        position = 0
        for i, child in enumerate(props):
            if child.tag in (_w("cnfStyle"), _w("tcW")):
                position = i + 1
        props.insert(position, element)
    element.set(_w("val"), str(span))


def _set_cell_width(cell: etree._Element, width: int) -> None:
    """``dxa`` 로 적힌 칸 너비(``w:tcW``)만 바꾼다 (``pct``·``auto`` 는 그대로)."""
    element = cell.find(f"{_w('tcPr')}/{_w('tcW')}")
    if element is not None and element.get(_w("type"), "dxa") == "dxa":
        element.set(_w("w"), str(max(width, 1)))


def _scaled(widths: list[int], total: int) -> list[int]:
    """너비들을 합이 ``total`` 이 되도록 비율대로 늘리거나 줄인다 (반올림 나머지는 마지막 열에)."""
    current = sum(widths) or 1
    result = [max(round(w * total / current), 1) for w in widths]
    result[-1] = max(result[-1] + total - sum(result), 1)
    return result


def _int_val_attr(element: etree._Element, name: str) -> int:
    try:
        return int(element.get(_w(name), "0"))
    except ValueError:
        return 0


def _row_cells(row: etree._Element) -> list[tuple[etree._Element, int, int]]:
    """행의 칸마다 ``(w:tc, 시작 격자 열, 끝 격자 열)`` (끝은 포함하지 않음). ``w:gridBefore`` 를 센다."""
    column = _int_val(row.find(f"{_w('trPr')}/{_w('gridBefore')}"), 0)
    cells = []
    for cell in row.iterchildren(_w("tc")):
        span = _span(cell)
        cells.append((cell, column, column + span))
        column += span
    return cells


def _cell_at(row: etree._Element | None, start: int) -> etree._Element | None:
    """``row`` 에서 격자 열 ``start`` 에서 시작하는 칸."""
    if row is None:
        return None
    return next((c for c, a, _ in _row_cells(row) if a == start), None)


def _sibling_row(row: etree._Element, step: str) -> etree._Element | None:
    """같은 표의 바로 앞(``"before"``)·뒤(``"after"``) 행."""
    sibling = row.getnext() if step == "after" else row.getprevious()
    while sibling is not None and sibling.tag != _w("tr"):
        if sibling.tag in (_w("tr"), _w("tblGrid"), _w("tblPr")):
            return None
        sibling = sibling.getnext() if step == "after" else sibling.getprevious()
    return sibling


def _merge_start(cell: etree._Element) -> etree._Element | None:
    """세로 병합이 이어지는 칸(``continue``)의 시작 칸 (위로 올라가며 찾는다)."""
    row = cell.getparent()
    start = next((a for c, a, _ in _row_cells(row) if c is cell), None) if row is not None else None
    while row is not None and start is not None:
        row = _sibling_row(row, "before")
        above = _cell_at(row, start)
        if above is None or _vmerge(above) is None:
            return None
        if _vmerge(above) == "restart":
            return above
    return None


def _unmerge_row(row: etree._Element) -> None:
    """지울 행의 세로 병합을 정리한다. 병합 시작 칸의 글자는 아래 칸으로 옮기고, 한 칸만 남은 병합은 뗀다."""
    above_row, below_row = _sibling_row(row, "before"), _sibling_row(row, "after")
    for cell, start, _ in _row_cells(row):
        state = _vmerge(cell)
        if state is None:
            continue
        below = _cell_at(below_row, start)
        continues = below is not None and _vmerge(below) == "continue"
        if state == "restart" and continues:
            assert below is not None
            for child in list(below):
                if child.tag in _BLOCKS:
                    below.remove(child)
            for child in list(cell):
                if child.tag != _w("tcPr"):
                    below.append(child)
            _set_vmerge(below, "restart")
            _drop_lonely_merge(below)
        elif state == "continue" and not continues:
            above = _cell_at(above_row, start)
            if above is not None and _vmerge(above) == "restart":
                _set_vmerge(above, None)  # 구역이 시작 칸 하나만 남는다


def _drop_lonely_merge(cell: etree._Element) -> None:
    """병합 시작 칸 아래에 이어지는 칸이 없으면 병합 표시를 뗀다 (한 칸짜리 병합)."""
    if _vmerge(cell) != "restart":
        return
    row = cell.getparent()
    start = next((a for c, a, _ in _row_cells(row) if c is cell), None) if row is not None else None
    if row is None or start is None:
        return
    below = _cell_at(_sibling_row(row, "after"), start)
    if below is None or _vmerge(below) != "continue":
        _set_vmerge(cell, None)


def _continued_cell(paragraph: etree._Element) -> etree._Element | None:
    """문단이 세로 병합으로 위 칸에 이어진 칸(``continue``) 안에 있으면 그 칸."""
    cell = next(paragraph.iterancestors(_w("tc")), None)
    return cell if cell is not None and _vmerge(cell) == "continue" else None


def _split_at(
    paragraph: etree._Element, run: etree._Element, marker: etree._Element
) -> etree._Element:
    """``marker``(``run`` 안)에서 문단을 둘로 나누고 뒤 조각(새 문단)을 돌려준다.

    뒤 조각은 문단 서식을 복사한다. 구역 나누기(``w:sectPr``)는 뒤 조각으로 옮긴다 (원래 문단의 끝에
    있던 것이므로). ``run`` 은 둘로 쪼개고 서식을 양쪽에 둔다. ``run`` 뒤의 형제 요소는 뒤 조각으로 옮긴다.
    """
    piece = paragraph.makeelement(_w("p"))
    props = paragraph.find(_w("pPr"))
    if props is not None:
        piece.append(deepcopy(props))
        section = props.find(_w("sectPr"))
        if section is not None:
            props.remove(section)
    following = list(run.itersiblings())
    tail = list(marker.itersiblings())
    run.remove(marker)
    paragraph.addnext(piece)
    if tail:
        rest = etree.SubElement(piece, _w("r"))
        run_props = run.find(_w("rPr"))
        if run_props is not None:
            rest.append(deepcopy(run_props))
        for child in tail:
            rest.append(child)
    if all(child.tag == _w("rPr") for child in run):
        paragraph.remove(run)
    for sibling in following:
        piece.append(sibling)
    return piece


# --- 문단을 글자 목록으로 -------------------------------------------------------------------------


@dataclass
class _Glyph:
    """보기 글자 하나와 그 출처.

    Attributes:
        char: 보기에 보이는 글자.
        run: 글자가 든 run (run 밖의 벽이면 ``None``).
        index: run 토큰 목록에서의 위치.
        wall: 고칠 수 없는 글자인지 (그림, 필드 결과, 각주 번호 등).
        scope: 글자를 둘러싼 가장 가까운 하이퍼링크·콘텐츠 컨트롤 (없으면 ``None``).
        tracked: 추적 기록 안의 글자면 그 기록. 넣은 글자(``w:ins``·``w:moveTo``)는 작성자 이름,
            서식 변경 기록(``w:rPrChange``)이 있는 run 의 글자는 ``FORMAT_CHANGE + 작성자``. 아니면 ``None``.
    """

    char: str
    run: etree._Element | None
    index: int
    wall: bool
    scope: etree._Element | None
    tracked: str | None = None


@dataclass
class Paragraph:
    r"""고칠 수 있는 단위인 문단 하나.

    Attributes:
        number: 문단 번호 (1부터, 본문 → 머리글 → 바닥글 순서). 보기의 ``[p12]`` 와 같다.
        location: 위치 표시 (예: ``"표1 2행 1열"``, ``"머리글 1"``). 본문의 일반 문단이면 ``None``.
        style: 제목 스타일 표시 (예: ``"제목 1"``). 제목이 아니면 ``None``.
        text: 보기 글자 (탭 ``\t``, 문단 안 줄바꿈 ``⏎``, 고칠 수 없는 자리 ``￼``).
        tracked: 변경 내용 추적 기록이 있는지 (있으면 고칠 수 없다).
        list_level: 목록(글머리표·번호) 문단이면 수준 (1부터). 목록이 아니면 ``None``.
        list_kind: 목록 종류 (``"번호"`` | ``"글머리표"``). 목록이 아니거나 알 수 없으면 ``None``.
        element: 문단 XML 요소 (``w:p``).
        part: 문단이 든 XML 부분 이름 (예: ``"word/document.xml"``).
        marked: 추적 표기를 넣은 보기 글자 (넣은 글자 ``{+…+}``, 지운 글자 ``{-…-}``).
            추적 기록이 없으면 ``text`` 와 같다. 보기에만 쓰고, ``old_text`` 는 ``text`` 에서 찾는다.
        track_authors: 추적 기록을 남긴 사람.
        track_notes: 글자로 보여줄 수 없는 추적 기록 설명 (예: ``"서식 바뀜"``).
        comments: 이 문단에 표시(``w:commentReference``)가 있는 메모.
        deleted_at: 숨긴 지운 기록의 ``(text 의 글자 위치, 작성자)``.
    """

    number: int
    location: str | None
    style: str | None
    text: str
    tracked: bool
    element: etree._Element
    part: str
    glyphs: list[_Glyph] = field(repr=False, default_factory=list)
    list_level: int | None = None
    list_kind: str | None = None
    marked: str = ""
    track_authors: tuple[str, ...] = ()
    track_notes: tuple[str, ...] = ()
    comments: tuple["Comment", ...] = ()
    deleted_at: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True)
class Comment:
    """문서에 달린 메모 하나.

    Attributes:
        id: 메모 id (``w:comment`` 의 ``w:id``). 보기에서는 ``c3`` 처럼 보인다.
        author: 작성자.
        text: 메모 내용 (문단은 `` / `` 로 잇는다).
        anchor: 메모가 가리키는 본문 글자. 범위가 없거나 찾지 못하면 빈 글자.
        parent: 답글이면 부모 메모 id. 아니면 빈 글자.
        resolved: 해결됨으로 표시된 메모인지 (``commentsExtended.xml`` 의 ``w15:done``).
        replies: 이 메모의 답글 (보기에서 메모 아래에 보인다).
    """

    id: str
    author: str
    text: str
    anchor: str = ""
    parent: str = ""
    resolved: bool = False
    replies: tuple["Comment", ...] = ()


class _FieldState:
    """복합 필드(``w:fldChar``) 깊이. 필드는 문단을 넘을 수 있어 부분(part) 하나를 훑는 동안 이어진다."""

    def __init__(self) -> None:
        self.depth = 0

    def update(self, element: etree._Element) -> None:
        kind = element.get(_w("fldCharType"))
        if kind == "begin":
            self.depth += 1
        elif kind == "end" and self.depth > 0:
            self.depth -= 1


@dataclass
class _Flat:
    """문단을 펼친 결과.

    Attributes:
        glyphs: 보기 글자 (변경 추적은 수락한 모습: 넣은 글자는 있고 지운 글자는 없다).
        tracked: 변경 추적 기록(글자·서식·문단 기호)이 있는지.
        marked: 추적 표기를 넣은 보기 글자 (넣은 글자 ``{+…+}``, 지운 글자 ``{-…-}``).
        authors: 추적 기록을 남긴 사람 (나온 순서, 중복 없음).
        notes: 글자로 보여줄 수 없는 추적 기록의 설명 (예: ``"서식 바뀜"``).
        comments: 메모 표시 ``(종류, 메모 id, 글자 위치)``. 종류는 ``start``·``end``·``ref``.
        deleted_at: 숨긴 지운 기록(``w:del``·``w:moveFrom``)의 ``(글자 위치, 작성자)``. 위치는 그 앞 글자 수.
    """

    glyphs: list[_Glyph]
    tracked: bool = False
    marked: str = ""
    authors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    comments: list[tuple[str, str, int]] = field(default_factory=list)
    deleted_at: list[tuple[int, str]] = field(default_factory=list)


def _deleted_text(element: etree._Element) -> str:
    """지운 기록(``w:del``·``w:moveFrom``) 안의 글자."""
    parts = []
    for node in element.iter(_w("delText"), _w("t"), _w("tab")):
        parts.append("\t" if node.tag == _w("tab") else node.text or "")
    return "".join(parts)


class _CommentState:
    """메모 범위 글자 모으기. 범위는 문단을 넘을 수 있어 문서 전체를 훑는 동안 이어진다.

    Attributes:
        parts: 메모 id → 지금까지 모은 범위 글자.
        open: 아직 끝나지 않은 범위의 메모 id.
    """

    def __init__(self) -> None:
        self.parts: dict[str, list[str]] = {}
        self.open: set[str] = set()


def _attach_comments(
    events: list[tuple[str, str, int]],
    text: str,
    state: _CommentState,
    comments: dict[str, "Comment"],
) -> tuple["Comment", ...]:
    """문단의 메모 표시로 이 문단에 보여줄 메모를 만든다. 메모는 표시(``w:commentReference``)가 있는 문단에 붙인다."""
    found: list[Comment] = []
    begins = {comment_id: 0 for comment_id in state.open}  # 앞 문단에서 이어진 범위는 문단 처음부터
    for kind, comment_id, position in events:
        if kind == "start":
            state.parts[comment_id] = []
            state.open.add(comment_id)
            begins[comment_id] = position
        elif kind == "end" and comment_id in state.open:
            state.parts[comment_id].append(text[begins.pop(comment_id, 0) : position])
            state.open.discard(comment_id)
        elif kind == "ref" and comment_id in comments:
            anchor = "".join(state.parts.pop(comment_id, [])).strip()
            found.append(replace(comments[comment_id], anchor=anchor))
    for comment_id, position in begins.items():  # 다음 문단으로 이어지는 범위
        state.parts[comment_id].append(text[position:] + " ")
    return tuple(found)


def _group_replies(paragraphs: list["Paragraph"], comments: dict[str, "Comment"]) -> None:
    """답글을 부모 메모 아래로 모은다. 부모가 보이지 않는 답글은 제자리에 그대로 둔다."""
    shown = {c.id for paragraph in paragraphs for c in paragraph.comments}
    replies: dict[str, list[Comment]] = {}
    for comment in comments.values():
        if comment.parent:
            replies.setdefault(comment.parent, []).append(comment)
    for paragraph in paragraphs:
        if not paragraph.comments:
            continue
        grouped = []
        for comment in paragraph.comments:
            if comment.parent and comment.parent in shown:
                continue
            grouped.append(replace(comment, replies=tuple(replies.get(comment.id, ()))))
        paragraph.comments = tuple(grouped)


def _structure_notes(paragraph: etree._Element) -> list[tuple[str, str]]:
    """문단이 든 행·칸·표·구역의 구조 추적 기록 ``(라벨, 작성자)``. 표 기록은 표 첫 문단에만 붙인다."""
    found: list[tuple[str, str]] = []

    def add(element: etree._Element | None, label: str) -> None:
        if element is not None:
            found.append((label, (element.get(_w("author")) or "").strip()))

    cell = next(paragraph.iterancestors(_w("tc")), None)
    row = cell.getparent() if cell is not None else None
    table = row.getparent() if row is not None else None
    if row is not None:
        add(row.find(f"{_w('trPr')}/{_w('ins')}"), "행 넣음 추적")
        add(row.find(f"{_w('trPr')}/{_w('del')}"), "행 지움 추적")
        add(row.find(f"{_w('trPr')}/{_w('trPrChange')}"), "행 서식 바뀜")
    if cell is not None:
        add(cell.find(f"{_w('tcPr')}/{_w('tcPrChange')}"), "칸 서식 바뀜")
        for tag in ("cellIns", "cellDel", "cellMerge"):
            add(cell.find(f"{_w('tcPr')}/{_w(tag)}"), "칸 넣음·지움·병합 추적")
    if table is not None and next(table.iter(_w("p")), None) is paragraph:
        for path in ("tblPr/w:tblPrChange", "tblGrid/w:tblGridChange"):
            add(table.find(f"w:{path}", {"w": W}), "표 서식 바뀜")
    section = paragraph.find(f"{_w('pPr')}/{_w('sectPr')}")
    following = paragraph.getnext()
    if section is None and following is not None and following.tag == _w("sectPr"):
        section = following
    if section is not None:
        add(section.find(_w("sectPrChange")), "구역 설정 바뀜")
    return found


def _flatten(paragraph: etree._Element, fields: _FieldState) -> _Flat:
    """문단을 글자 목록으로 펼친다. 추적 표기를 넣은 글자와 메모 위치도 함께 모은다."""
    flat = _Flat([])
    glyphs = flat.glyphs
    marked: list[str] = []

    def add(glyph: _Glyph) -> None:
        glyphs.append(glyph)
        marked.append(glyph.char)

    def note(element: etree._Element, label: str | None = None) -> None:
        flat.tracked = True
        author = element.get(_w("author"))
        if author and author not in flat.authors:
            flat.authors.append(author)
        if label and label not in flat.notes:
            flat.notes.append(label)

    def wall(scope: etree._Element | None, char: str = WALL) -> None:
        add(_Glyph(char, None, -1, True, scope, track))

    track: str | None = None  # 지금 펼치는 글자를 감싼 넣기 기록의 작성자

    def run(element: etree._Element, scope: etree._Element | None, forced_wall: bool) -> None:
        props = element.find(_w("rPr"))
        change = props.find(_w("rPrChange")) if props is not None else None
        tracked = track
        if change is not None:
            note(change, "서식 바뀜")
            tracked = FORMAT_CHANGE + (change.get(_w("author")) or "?")
        for index, token in enumerate(_tokens(element)):
            if token.kind == "x":
                assert token.element is not None
                tag = token.element.tag
                if tag == _w("fldChar"):
                    fields.update(token.element)
                elif tag == _w("commentReference"):
                    flat.comments.append(("ref", token.element.get(_w("id"), ""), len(glyphs)))
                elif tag == _w("noBreakHyphen"):
                    add(_Glyph("-", element, index, True, scope, tracked))
                elif tag not in _RUN_SILENT:
                    add(_Glyph(WALL, element, index, True, scope, tracked))
                continue
            locked = forced_wall or fields.depth > 0
            add(_Glyph(token.char, element, index, locked, scope, tracked))

    def walk(parent: etree._Element, scope: etree._Element | None, forced_wall: bool) -> None:
        nonlocal track
        for child in parent:
            tag = child.tag
            if not isinstance(tag, str):
                continue
            if tag in _COMMENT_RANGE:
                kind = "start" if tag == _w("commentRangeStart") else "end"
                flat.comments.append((kind, child.get(_w("id"), ""), len(glyphs)))
                continue
            if tag == _w("pPr"):
                paragraph_props(child)
                continue
            if tag in _PARAGRAPH_SILENT:
                continue
            if tag == _w("r"):
                run(child, scope, forced_wall)
            elif tag in _SCOPES:
                walk(child, child, forced_wall)
            elif tag == _w("sdt"):
                content = child.find(_w("sdtContent"))
                if content is not None:
                    walk(content, content, forced_wall)
            elif tag in _TRANSPARENT:
                walk(child, scope, forced_wall)
            elif tag == _w("fldSimple"):
                # 필드 결과 글자는 보여주되 고치지 않는다 (Word 가 다시 계산하는 값).
                walk(child, scope, True)
            elif tag in _TRACKED_SHOWN:
                note(child)
                marked.append("{+")
                outer, track = track, child.get(_w("author")) or "?"
                walk(child, scope, forced_wall)
                track = outer
                marked.append("+}")
            elif tag in _TRACKED_HIDDEN:
                note(child)
                flat.deleted_at.append((len(glyphs), child.get(_w("author")) or "?"))
                marked.append("{-" + _deleted_text(child) + "-}")
                for reference in child.iter(_w("commentReference")):  # 지운 글자에 붙은 메모
                    flat.comments.append(("ref", reference.get(_w("id"), ""), len(glyphs)))
            else:
                # 수식, mc:AlternateContent, 알 수 없는 요소: 통째로 벽 하나.
                wall(scope)

    def paragraph_props(props: etree._Element) -> None:
        for element in props.iter(_w("pPrChange"), _w("numberingChange")):
            note(element, "문단 서식 바뀜")
        mark = props.find(_w("rPr"))
        if mark is None:
            return
        for element in mark:
            if element.tag == _w("ins"):
                note(element, "문단 넣음·나눔 추적")
            elif element.tag == _w("del"):
                note(element, "문단 합침 추적")
            elif element.tag == _w("rPrChange"):
                note(element, "서식 바뀜")

    walk(paragraph, None, False)
    # 이어진 같은 종류 표기는 하나로 (Word·LibreOffice 는 run 마다 기록을 나눠 저장한다).
    flat.marked = "".join(marked).replace("+}{+", "").replace("-}{-", "")
    return flat


# --- 패키지 ---------------------------------------------------------------------------------------


def _parse_xml(data: bytes) -> etree._Element:
    parser = etree.XMLParser(
        resolve_entities=False, no_network=True, huge_tree=False, remove_blank_text=False
    )
    try:
        return etree.fromstring(data, parser)
    except etree.XMLSyntaxError as exc:
        raise EditError("This Word document is damaged and cannot be edited.") from exc


_DECLARATION = re.compile(rb"^\s*<\?xml[^>]*\?>\s*")


def _serialize(root: etree._Element, original: bytes) -> bytes:
    """XML 을 원본의 선언(``<?xml … standalone="yes"?>``)을 그대로 붙여 쓴다."""
    match = _DECLARATION.match(original)
    head = match.group(0) if match else b""
    return head + etree.tostring(root, encoding="UTF-8", xml_declaration=False)


def _resolve(base: str, target: str) -> str:
    """관계(rels)의 대상 경로를 ZIP 항목 이름으로 바꾼다."""
    if target.startswith("/"):
        return target.lstrip("/")
    folder = base.rsplit("/", 1)[0] if "/" in base else ""
    parts = [p for p in folder.split("/") if p]
    for piece in target.split("/"):
        if piece == "..":
            if parts:
                parts.pop()
        elif piece and piece != ".":
            parts.append(piece)
    return "/".join(parts)


def _rels_name(part: str) -> str:
    folder, _, name = part.rpartition("/")
    return f"{folder}/_rels/{name}.rels" if folder else f"_rels/{name}.rels"


@dataclass(frozen=True)
class ParagraphView:
    r"""문단 번호를 붙인 본문 일부.

    Attributes:
        text: ``[p12] 문단 글자`` 형태의 줄들 (``\n`` 으로 잇는다). 범위 밖이면 빈 글자.
        first: 보기를 시작한 문단 번호 (1부터).
        last: 보여준 마지막 문단 번호. 보여준 문단이 없으면 ``first - 1``.
        total: 전체 문단 수 (빈 문단 포함).
        matches: 찾는 글자(``query``)가 든 문단 수 (``first`` 부터 끝까지). 찾지 않았으면 ``None``.
        shown: 보여준 문단 수.
    """

    text: str
    first: int
    last: int
    total: int
    matches: int | None = None
    shown: int = 0


class DocxDocument:
    """수정할 DOCX 파일 하나. ``from_bytes`` 로 열고, 고친 뒤 ``to_bytes`` 로 저장한다."""

    def __init__(
        self,
        entries: list[tuple[zipfile.ZipInfo, bytes]],
        parts: list[tuple[str, str | None]],
        styles: dict[str, str],
    ) -> None:
        """열린 패키지로 문서를 만든다. 직접 부르지 말고 ``from_bytes`` 를 쓴다.

        Args:
            entries: ZIP 항목과 (압축을 푼) 내용, 원래 순서대로.
            parts: 문단을 찾을 XML 부분 ``(이름, 위치 표시)``. 본문이 먼저다.
            styles: 스타일 ID → 스타일 이름.
        """
        self._entries = entries
        self._data = {info.filename: data for info, data in entries}
        self._parts = parts
        self._styles = styles
        self._roots = {name: _parse_xml(self._data[name]) for name, _ in parts}
        self._dirty: set[str] = set()
        self._paragraphs: list[Paragraph] | None = None
        self._marked_total = 0
        self._track = False
        self._author = DEFAULT_AUTHOR
        self._date = ""
        self._next_id: int | None = None
        self._next_hex: int | None = None

    @classmethod
    def from_bytes(cls, data: bytes) -> "DocxDocument":
        """DOCX 파일 내용을 연다.

        Args:
            data: DOCX 파일 내용.

        Returns:
            열린 문서.

        Raises:
            EditError: ZIP·XML 이 깨졌거나, 본문을 찾을 수 없거나, 매크로가 든 문서(``.docm``)일 때.
        """
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                entries = [(info, zf.read(info)) for info in zf.infolist()]
        except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError) as exc:
            raise EditError("This Word document is damaged and cannot be edited.") from exc
        contents = {info.filename: body for info, body in entries}
        content_types = contents.get("[Content_Types].xml", b"")
        if b"macroEnabled" in content_types or "word/vbaProject.bin" in contents:
            raise EditError(
                "This Word document contains macros (.docm), which cannot be edited for safety."
            )
        main = cls._main_part(contents)
        parts: list[tuple[str, str | None]] = [(main, None)]
        parts += cls._header_footer_parts(main, contents)
        styles = cls._style_names(main, contents)
        return cls(entries, parts, styles)

    @staticmethod
    def _main_part(contents: dict[str, bytes]) -> str:
        rels = contents.get("_rels/.rels")
        if rels is not None:
            for rel in _parse_xml(rels).iter(f"{{{_PKG_REL}}}Relationship"):
                if rel.get("Type") == _REL_OFFICE_DOCUMENT:
                    name = _resolve("", rel.get("Target", ""))
                    if name in contents:
                        return name
        if "word/document.xml" in contents:
            return "word/document.xml"
        raise EditError("This file does not look like a Word document, so it cannot be edited.")

    @staticmethod
    def _relationships(part: str, contents: dict[str, bytes]) -> dict[str, tuple[str, str]]:
        """부분의 관계 ``{rId: (종류, ZIP 항목 이름)}``. 외부 링크는 뺀다."""
        rels = contents.get(_rels_name(part))
        if rels is None:
            return {}
        result = {}
        for rel in _parse_xml(rels).iter(f"{{{_PKG_REL}}}Relationship"):
            if rel.get("TargetMode") == "External":
                continue
            kind = rel.get("Type", "").removeprefix(_REL_BASE)
            result[rel.get("Id", "")] = (kind, _resolve(part, rel.get("Target", "")))
        return result

    @classmethod
    def _header_footer_parts(
        cls, main: str, contents: dict[str, bytes]
    ) -> list[tuple[str, str | None]]:
        """본문의 구역 설정이 가리키는 순서대로 머리글들, 그다음 바닥글들."""
        rels = cls._relationships(main, contents)
        root = _parse_xml(contents[main])
        found: dict[str, list[str]] = {"header": [], "footer": []}
        for kind, names in found.items():
            for ref in root.iter(_w(f"{kind}Reference")):
                target = rels.get(ref.get(f"{{{_R}}}id", ""))
                if (
                    target
                    and target[0] == kind
                    and target[1] in contents
                    and target[1] not in names
                ):
                    names.append(target[1])
        labels = {"header": "머리글", "footer": "바닥글"}
        return [
            (name, f"{labels[kind]} {n}")
            for kind in ("header", "footer")
            for n, name in enumerate(found[kind], 1)
        ]

    @classmethod
    def _style_names(cls, main: str, contents: dict[str, bytes]) -> dict[str, str]:
        rels = cls._relationships(main, contents)
        name = next((t for k, t in rels.values() if k == "styles"), None)
        if name is None or name not in contents:
            return {}
        names = {}
        for style in _parse_xml(contents[name]).iter(_w("style")):
            label = style.find(_w("name"))
            if label is not None:
                names[style.get(_w("styleId"), "")] = label.get(_w("val"), "")
        return names

    def _part_root(self, name: str) -> etree._Element | None:
        """ZIP 항목의 XML (고치면 저장되도록 ``_roots`` 에 둔다). 없으면 ``None``."""
        if name not in self._roots:
            if name not in self._data:
                return None
            self._roots[name] = _parse_xml(self._data[name])
        return self._roots[name]

    def _related(self, kind: str) -> str | None:
        """본문 부분과 ``kind`` 관계(예: ``"comments"``)로 이어진 ZIP 항목 이름."""
        return self._related_type(_REL_BASE + kind)

    def _related_type(self, rel_type: str) -> str | None:
        """본문 부분과 관계 형식 ``rel_type``(전체 주소)로 이어진 ZIP 항목 이름."""
        main = self._parts[0][0]
        rels = self._part_root(_rels_name(main))
        if rels is None:
            return None
        for rel in rels.iter(f"{{{_PKG_REL}}}Relationship"):
            if rel.get("TargetMode") != "External" and rel.get("Type") == rel_type:
                return _resolve(main, rel.get("Target", ""))
        return None

    @property
    def tracks_revisions(self) -> bool:
        """Word 에서 "변경 내용 추적" 이 켜진 문서인지 (``word/settings.xml`` 의 ``w:trackRevisions``)."""
        name = self._related("settings")
        root = self._part_root(name) if name else None
        flag = root.find(_w("trackRevisions")) if root is not None else None
        return flag is not None and flag.get(_w("val"), "true") not in ("false", "0", "off")

    def _comments(self) -> dict[str, Comment]:
        """메모 부분(``comments.xml``)의 메모 ``{id: 메모}`` (범위 글자 없이). 없으면 빈 사전.

        답글·해결 표시는 ``commentsExtended.xml`` 에서 읽는다 (메모 마지막 문단의 ``w14:paraId`` 로 짝짓는다).
        """
        name = self._related("comments")
        root = self._part_root(name) if name else None
        if root is None:
            return {}
        extended = self._comments_extended()
        by_para: dict[str, str] = {}
        result: dict[str, Comment] = {}
        for comment in root.iter(_w("comment")):
            paragraphs = list(comment.iter(_w("p")))
            lines = ["".join(t.text or "" for t in p.iter(_w("t"))) for p in paragraphs]
            text = " / ".join(line.strip() for line in lines if line.strip())
            comment_id = comment.get(_w("id"), "")
            result[comment_id] = Comment(comment_id, comment.get(_w("author"), ""), text)
            para_id = paragraphs[-1].get(_PARA_ID) if paragraphs else None
            if para_id:
                by_para[para_id.upper()] = comment_id
        if extended is not None:
            for entry in extended.iter(f"{{{W15}}}commentEx"):
                comment_id = by_para.get((entry.get(f"{{{W15}}}paraId") or "").upper())
                if comment_id is None:
                    continue
                parent = by_para.get((entry.get(f"{{{W15}}}paraIdParent") or "").upper(), "")
                done = entry.get(f"{{{W15}}}done") in ("1", "true")
                result[comment_id] = replace(result[comment_id], parent=parent, resolved=done)
        return result

    def _comments_extended(self) -> etree._Element | None:
        """답글·해결 표시 부분(``commentsExtended.xml``). 없으면 ``None``."""
        name = self._related_type(_REL_COMMENTS_EXTENDED)
        return self._part_root(name) if name else None

    # --- 문단 ------------------------------------------------------------------------------------

    def _list_formats(self) -> dict[str, dict[int, str]]:
        """목록 ``numId`` → 수준(0부터)별 번호 형식 (``decimal``·``bullet`` 등). 목록 번호 부분이 없으면 빈 표."""
        name = self._related("numbering")
        root = self._part_root(name) if name else None
        if root is None:
            return {}
        abstracts: dict[str, dict[int, str]] = {}
        for definition in root.iter(_w("abstractNum")):
            abstracts[definition.get(_w("abstractNumId"), "")] = _level_formats(definition)
        formats: dict[str, dict[int, str]] = {}
        for num in root.iter(_w("num")):
            link = num.find(_w("abstractNumId"))
            levels = dict(abstracts.get(link.get(_w("val"), "") if link is not None else "", {}))
            for override in num.iterchildren(_w("lvlOverride")):
                levels.update(_level_formats(override))
            formats[num.get(_w("numId"), "")] = levels
        return formats

    def _style_label(self, paragraph: etree._Element) -> str | None:
        ref = paragraph.find(f"{_w('pPr')}/{_w('pStyle')}")
        if ref is None:
            return None
        name = self._styles.get(ref.get(_w("val"), ""), "").strip()
        if match := _HEADING.match(name):
            return f"제목 {match.group(1)}"
        lowered = name.lower()
        if lowered == "title":
            return "문서 제목"
        if lowered == "subtitle":
            return "부제목"
        return None

    def _walk_blocks(
        self,
        parent: etree._Element,
        tables: list[int],
        part_label: str | None,
        where: str | None,
    ) -> Iterator[tuple[etree._Element, str | None]]:
        """블록(문단·표)을 본문 순서대로 훑어 ``(문단, 위치 표시)`` 를 낸다. 표 칸 안과 중첩 표 포함.

        표는 부분(본문·머리글·바닥글)을 통틀어 나오는 순서대로 번호를 매기고, 중첩 표 안의 문단은 가장 안쪽
        표의 칸만 적는다. 머리글·바닥글이면 앞에 ``머리글 1 · `` 처럼 붙인다.
        """
        for child in parent:
            tag = child.tag
            if tag == _w("p"):
                yield child, where
            elif tag == _w("tbl"):
                tables[0] += 1
                number = tables[0]
                for row_no, row in enumerate(child.iterchildren(_w("tr")), 1):
                    for col_no, cell in enumerate(row.iterchildren(_w("tc")), 1):
                        cell_label = f"표{number} {row_no}행 {col_no}열"
                        label = f"{part_label} · {cell_label}" if part_label else cell_label
                        yield from self._walk_blocks(cell, tables, part_label, label)
            elif tag == _w("sdt"):
                content = child.find(_w("sdtContent"))
                if content is not None:
                    yield from self._walk_blocks(content, tables, part_label, where)
            elif tag in (_w("customXml"), _w("body")):
                yield from self._walk_blocks(child, tables, part_label, where)

    @property
    def paragraphs(self) -> list[Paragraph]:
        """모든 문단 (본문 → 머리글 → 바닥글 순서, 빈 문단 포함)."""
        if self._paragraphs is None:
            result: list[Paragraph] = []
            tables = [0]
            comments = self._comments()
            formats = self._list_formats()
            state = _CommentState()
            for name, label in self._parts:
                fields = _FieldState()
                blocks = self._walk_blocks(self._roots[name], tables, label, label)
                for element, where in blocks:
                    flat = _flatten(element, fields)
                    for label, author in _structure_notes(element):
                        flat.tracked = True
                        if author and author not in flat.authors:
                            flat.authors.append(author)
                        if label not in flat.notes:
                            flat.notes.append(label)
                    text = "".join(g.char for g in flat.glyphs)
                    found = _attach_comments(flat.comments, text, state, comments)
                    result.append(
                        Paragraph(
                            number=len(result) + 1,
                            location=where,
                            style=self._style_label(element),
                            list_level=_list_level(element),
                            list_kind=_list_kind(element, formats),
                            text=text,
                            tracked=flat.tracked,
                            element=element,
                            part=name,
                            glyphs=flat.glyphs,
                            marked=flat.marked,
                            track_authors=tuple(flat.authors),
                            track_notes=tuple(flat.notes),
                            comments=found,
                            deleted_at=tuple(flat.deleted_at),
                        )
                    )
            _group_replies(result, comments)
            self._paragraphs = result
        return self._paragraphs

    def view(
        self,
        first: int,
        limit: int,
        max_chars: int | None = None,
        query: str | None = None,
        formatting: bool = False,
    ) -> ParagraphView:
        """문단에 번호를 붙여 일부를 보여준다 (``[p12] (표1 1행 1열) 글자``).

        빈 문단은 번호만 차지하고 보여주지 않는다. 다만 표 칸의 빈 문단은 ``(빈 칸)`` 으로 보여준다 (채울 수
        있게). ``max_chars`` 를 주면 결과가 그 글자 수를 넘기 전에 멈춘다. 그래도 첫 문단은 길어도 보여준다.
        ``query`` 를 주면 그 글자가 든 문단만 보여준다. 추적 기록이 있는 문단은 수락한 모습에서 찾는다
        (``old_text`` 를 찾는 글자와 같다).

        Args:
            first: 보여줄 첫 문단 번호 (1부터). 1보다 작으면 1로 본다.
            limit: 보여줄 최대 문단 수 (1 이상, 빈 문단은 세지 않는다).
            max_chars: 결과 글자 수의 상한. ``None`` 이면 문단 수로만 자른다.
            query: 찾을 글자 (대소문자 무시). ``None`` 이나 빈 글자면 모든 문단.
            formatting: 문단 아래에 직접 서식(굵게·색·형광펜 등)이 있는 글자를 한 줄로 보여줄지.

        Returns:
            번호를 붙인 문단들과 범위, 전체 문단 수 (찾았으면 맞는 문단 수).
        """
        paragraphs = self.paragraphs
        first = max(first, 1)
        query = query.strip() if query and query.strip() else None
        shown: list[str] = []
        last = first - 1
        size = 0
        matches = 0
        full = False
        for paragraph in paragraphs[first - 1 :]:
            full = full or len(shown) >= max(limit, 1)
            if full and query is None:
                break
            wanted = _shown(paragraph) if query is None else matches_query(paragraph.text, query)
            if wanted and query is not None:
                matches += 1
            if wanted and not full:
                line = _format_paragraph(paragraph, formatting)
                size += len(line) + (1 if shown else 0)
                if shown and max_chars is not None and size > max_chars:
                    full = True
                    if query is None:
                        break
                else:
                    shown.append(line)
            if not full:
                last = paragraph.number
        return ParagraphView(
            "\n".join(shown),
            first,
            last,
            len(paragraphs),
            matches if query is not None else None,
            len(shown),
        )

    # --- 수정 ------------------------------------------------------------------------------------

    def apply_some(
        self,
        edits: Sequence[Edit],
        *,
        track: bool = False,
        author: str = DEFAULT_AUTHOR,
        date: str | None = None,
    ) -> tuple[list[Change], list[EditError]]:
        r"""수정을 순서대로 적용하고, 적용할 수 없는 수정은 그 수정만 되돌리고 건너뛴다.

        수정 안의 문단 번호(``paragraph``, ``target``)는 모두 **이 호출을 시작할 때의 번호**다. 앞 수정이
        문단을 나누거나 지워도 뒤 수정의 ``p7`` 은 원래 p7 문단을 가리킨다. ``old_text`` 는 앞 수정을
        적용한 글자에서 찾는다. 바뀐 곳 요약의 ``line`` 은 (호출을 시작할 때의) 문단 번호다.

        Args:
            edits: 적용할 수정 (1개 이상).

            track: 변경 내용 추적으로 남길지 (Word 에서 하나씩 수락·거절할 수 있게). 추적할 수 없는 수정은
                실패한다.
            author: 추적 기록·메모의 작성자. 이 이름으로 넣은 추적 글자는 다시 고칠 수 있다.
            date: 추적 기록·메모의 시각 (``2026-11-24T10:00:00Z``). 없으면 지금 시각.

        Returns:
            ``(성공한 수정의 바뀐 곳 요약, 실패한 수정의 오류)``.

        Raises:
            EditError: 수정이 없을 때.
        """
        if not edits:
            raise EditError("No edits were given.")
        self._track, self._author = track, author
        self._date = date or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._mark()
        changes: list[Change] = []
        failures: list[EditError] = []
        try:
            for index, edit in enumerate(edits, 1):
                snapshot = self._snapshot()
                try:
                    changes.append(self._apply(index, edit))
                except EditError as exc:
                    self._restore(snapshot)
                    failures.append(exc)
        finally:
            self._unmark()
        return changes, failures

    def apply_edits(self, edits: Sequence[Edit], **options: Any) -> list[Change]:
        r"""수정을 순서대로 적용한다. 하나라도 실패하면 아무것도 적용하지 않는다.

        문단 번호와 ``old_text`` 를 찾는 규칙은 ``apply_some`` 과 같다.

        Args:
            edits: 적용할 수정 (1개 이상).
            **options: ``apply_some`` 의 ``track``·``author``·``date``.

        Returns:
            바뀐 곳 요약.

        Raises:
            EditError: 수정이 없거나, 어느 수정이든 적용할 수 없을 때 (첫 실패의 오류).
        """
        snapshot = self._snapshot()
        changes, failures = self.apply_some(edits, **options)
        if failures:
            self._restore(snapshot)
            raise failures[0]
        return changes

    def _snapshot(self) -> tuple[dict[str, bytes], set[str]]:
        return {n: etree.tostring(root) for n, root in self._roots.items()}, set(self._dirty)

    def _restore(self, snapshot: tuple[dict[str, bytes], set[str]]) -> None:
        roots, dirty = snapshot
        parser = etree.XMLParser(resolve_entities=False, no_network=True)
        self._roots = {n: etree.fromstring(x, parser) for n, x in roots.items()}
        self._dirty = set(dirty)
        self._paragraphs = None
        self._refresh_style_names()

    def _refresh_style_names(self) -> None:
        """스타일 id → 이름 표를 스타일 부분에서 다시 읽는다 (스타일을 더하거나 되돌린 뒤)."""
        root = self._styles_root()
        if root is None:
            return
        names = {}
        for style in root.iter(_w("style")):
            label = style.find(_w("name"))
            if label is not None:
                names[style.get(_w("styleId"), "")] = label.get(_w("val"), "")
        self._styles = names

    def _mark(self) -> None:
        """문단마다 지금 번호를 임시 속성으로 단다. 문단을 나누거나 지워도 번호로 원래 문단을 찾는다."""
        self._unmark()
        for paragraph in self.paragraphs:
            paragraph.element.set(_MARK, str(paragraph.number))
        self._marked_total = len(self.paragraphs)

    def _unmark(self) -> None:
        for root in self._roots.values():
            for element in root.iter(_w("p")):
                element.attrib.pop(_MARK, None)

    def _apply(self, index: int, edit: Edit) -> Change:
        self._refuse_tracking(index, edit)
        if edit.op == "replace":
            return self._replace(index, edit)
        if edit.op == "merge":
            return self._merge(index, edit)
        if edit.op == "insert_paragraph":
            return self._insert_paragraph(index, edit)
        if edit.op == "delete_paragraph":
            return self._delete_paragraph(index, edit)
        if edit.op == "move_paragraph":
            return self._move_paragraph(index, edit)
        if edit.op == "insert_row":
            return self._insert_row(index, edit)
        if edit.op == "delete_row":
            return self._delete_row(index, edit)
        if edit.op == "insert_column":
            return self._insert_column(index, edit)
        if edit.op == "delete_column":
            return self._delete_column(index, edit)
        if edit.op == "delete_table":
            return self._delete_table(index, edit)
        if edit.op == "insert_markdown":
            return self._insert_markdown(index, edit)
        if edit.op == "set_style":
            return self._set_style(index, edit)
        if edit.op in ("accept_changes", "reject_changes"):
            return self._review(index, edit, accept=edit.op == "accept_changes")
        if edit.op == "comment":
            return self._comment(index, edit)
        if edit.op == "resolve_comment":
            return self._resolve_comment(index, edit)
        if edit.op == "delete_comment":
            return self._delete_comment(index, edit)
        if edit.op == "format":
            return self._format(index, edit)
        raise EditError(
            f"Edit {index}: unknown op {edit.op!r}. Use one of: {', '.join(sorted(EDIT_OPS))}.",
            index,
        )

    def _paragraph(self, index: int, ref: str | None, name: str = "paragraph") -> Paragraph:
        """수정의 문단 번호(``"p12"``)가 가리키는 문단. 번호는 호출을 시작할 때의 번호다."""
        if not ref or not ref.strip():
            raise EditError(f'Edit {index}: {name} is required (for example "p12").', index)
        match = _PARAGRAPH_REF.fullmatch(ref.strip())
        if not match:
            raise EditError(f'Edit {index}: {name} must look like "p12".', index)
        number = int(match.group(1))
        if not 1 <= number <= self._marked_total:
            raise EditError(
                f"Edit {index}: paragraph p{number} does not exist "
                f"(the document has p1 to p{self._marked_total}).",
                index,
            )
        for paragraph in self.paragraphs:
            if paragraph.element.get(_MARK) == str(number):
                return paragraph
        raise EditError(
            f"Edit {index}: paragraph p{number} was removed by an earlier edit in this call.", index
        )

    def _replace(self, index: int, edit: Edit) -> Change:
        old = edit.old_text.replace("\r\n", "\n")
        new = edit.new_text.replace("\r\n", "\n")
        if "\n" not in old:
            new = re.sub(r"\n{2,}", "\n", new)  # 빈 줄은 문단 나누기 하나로 본다
        if not old:
            if edit.paragraph and new:
                return self._fill(index, self._paragraph(index, edit.paragraph), new)
            raise EditError(
                f"Edit {index}: old_text is empty. To put text into an empty paragraph (such as "
                'an empty table cell), also set paragraph (for example "p12").',
                index,
            )
        if old == new:
            raise EditError(f"Edit {index}: old_text and new_text are the same.", index)
        if _FORBIDDEN_CHARS.search(new):
            raise EditError(f"Edit {index}: new_text contains control characters.", index)
        target = self._paragraph(index, edit.paragraph) if edit.paragraph else None
        targets = [target] if target is not None else self.paragraphs
        hits = [(p, start) for p in targets for start in _find_all(p.text, old)]
        if not hits and "\n" in old:
            # 예전 보기는 문단 안 줄바꿈을 날것의 줄바꿈으로 보였다. 그렇게 쓴 old_text 도 받는다.
            soft_old, soft_new = old.replace("\n", LINE_BREAK), new.replace("\n", LINE_BREAK)
            soft = [(p, start) for p in targets for start in _find_all(p.text, soft_old)]
            if soft:
                old, new, hits = soft_old, soft_new, soft
        if not hits:
            raise self._not_found(index, old, targets, edit.paragraph)
        if LINE_BREAK in old + new and any(
            LINE_BREAK in _paragraph_text(p.element) for p, _ in hits
        ):
            raise EditError(
                f"Edit {index}: this paragraph contains the character {LINE_BREAK} as text, so "
                f"{LINE_BREAK} cannot be used there to mean a line break.",
                index,
            )
        skipped = 0
        in_urls: list[int] = []
        if edit.replace_all and not targets_url(old):
            # 주소가 보이는 하이퍼링크의 글자를 바꾸면 보이는 주소와 실제 링크(관계 파일)가 어긋난다.
            # 링크 대상은 고칠 수 없으니 건너뛴다. 일반 글자 속 주소는 바꾸고 결과에 알린다.
            kept = [(p, s) for p, s in hits if not _in_address_link(p, s, s + len(old))]
            skipped = len(hits) - len(kept)
            if not kept:
                raise EditError(
                    f"Edit {index}: all {len(hits)} matches are inside hyperlinks that show a web "
                    "address. Changing that text would no longer match where the link goes, and "
                    "link targets cannot be changed yet.",
                    index,
                )
            hits = kept
            in_urls = [
                self._number(p) for p, s in hits if in_url(url_spans(p.text), s, s + len(old))
            ]
        if len(hits) > 1 and not edit.replace_all:
            raise EditError(
                f"Edit {index}: old_text appears {len(hits)} times. Include more surrounding text "
                'so that it matches exactly one place, set paragraph (for example "p12") to '
                "search only one paragraph, or set replace_all to change every occurrence.",
                index,
            )
        own = {(id(p.element), s): self._check_range(index, p, s, len(old), new) for p, s in hits}
        lost = sum(lost_formatting(old, new, p.glyphs[s : s + len(old)]) for p, s in hits)
        first = self._number(hits[0][0])
        count = 0
        places: list[int] = []
        # 같은 문단 안에서는 뒤에서부터 고쳐야 앞쪽 글자 위치가 그대로다. replace_all 은 겹치지 않게.
        by_paragraph: dict[int, tuple[Paragraph, list[int]]] = {}
        for paragraph, start in hits:
            _, starts = by_paragraph.setdefault(id(paragraph.element), (paragraph, []))
            if not starts or start >= starts[-1] + len(old):
                starts.append(start)
        added = 0
        for paragraph, starts in by_paragraph.values():
            if not self._track:
                editor = _ParagraphEditor(paragraph, index, self._author)
                for start in reversed(starts):
                    editor.replace(start, old, new)
                editor.commit()
            else:
                # 뒤에서부터 하나씩 바로 고친다: 앞쪽 글자의 run·토큰 위치는 그대로 남는다.
                for start in reversed(starts):
                    if own[(id(paragraph.element), start)]:
                        # 우리가 추적으로 넣은 글자는 Word 처럼 그 넣기 기록 안을 바로 고친다.
                        editor = _ParagraphEditor(paragraph, index, self._author)
                        editor.replace(start, old, new)
                        editor.commit()
                    else:
                        self._tracked_replace(index, paragraph, start, old, new)
            count += len(starts)
            places.extend([self._number(paragraph)] * len(starts))
            _drop_empty_records(paragraph.element)
            pieces = self._split(index, paragraph)
            if self._track:
                for piece in pieces[:-1]:
                    self._mark_paragraph(piece, "ins")
            added += len(pieces) - 1
            self._dirty.add(paragraph.part)
        self._paragraphs = None
        note = ""
        if added and len(by_paragraph) == 1:
            heads = " | ".join(repr(_head(_paragraph_text(piece))) for piece in pieces)
            note = f"split into {added + 1} paragraphs: {heads}"
        elif added:
            note = f"{added} new paragraphs from line breaks"
        if lost:
            lost_note = (
                f"{lost} formatted piece{'s' if lost > 1 else ''} (such as bold words or links) "
                "did not appear unchanged in new_text, so that text now has the paragraph's "
                "first formatting; use op format to restore formatting if needed"
            )
            note = f"{note}; {lost_note}" if note else lost_note
        before, after = snippets(old, new)
        return Change(
            index,
            first,
            count,
            before,
            after,
            note=note,
            places=tuple(places),
            in_urls=tuple(in_urls),
            skipped=skipped,
        )

    def _fill(self, index: int, paragraph: Paragraph, new: str) -> Change:
        """빈 문단(빈 표 칸 등)에 글자를 넣는다. 서식은 문단의 run 이나 문단 기호 서식을 따른다."""
        number = self._number(paragraph)
        if paragraph.text:
            raise EditError(
                f"Edit {index}: old_text is empty, but paragraph p{number} is not empty. Put the "
                "text to change in old_text.",
                index,
            )
        continued = _continued_cell(paragraph.element)
        if continued is not None:
            start = _merge_start(continued)
            owner = next(
                (x for x in self.paragraphs if start is not None and _within(x.element, start)),
                None,
            )
            where = f" Edit the merged cell's text in p{self._number(owner)}." if owner else ""
            raise EditError(
                f"Edit {index}: paragraph p{number} is in a cell merged with the cell above, so "
                f"it cannot hold text.{where}",
                index,
            )
        self._check_untracked(index, paragraph)
        if _FORBIDDEN_CHARS.search(new):
            raise EditError(f"Edit {index}: new_text contains control characters.", index)
        if WALL in new:
            raise EditError(
                f"Edit {index}: new_text contains {WALL}, which stands for an image or other "
                "object. Objects cannot be added.",
                index,
            )
        element = paragraph.element
        run = etree.SubElement(element, _w("r"))
        props = _run_props_for(element)
        if props is not None:
            run.append(props)
        _write_run(run, _text_tokens(new))
        if self._track:
            # 나눌 자리 표시는 넣기 기록 밖(문단 바로 아래)에 있어야 하므로 줄마다 run 을 나눈다.
            run.getparent().remove(run)
            for k, line in enumerate(new.split("\n")):
                if k:
                    element.append(_new_run(props, [_Token("x", "", _split_marker())]))
                if line:
                    record = self._record("ins")
                    record.append(_new_run(props, _text_tokens(line)))
                    element.append(record)
        pieces = self._split(index, paragraph)
        if self._track:
            for piece in pieces[:-1]:
                self._mark_paragraph(piece, "ins")
        added = len(pieces) - 1
        self._dirty.add(paragraph.part)
        self._paragraphs = None
        note = "filled an empty paragraph" + (f", split into {added + 1}" if added else "")
        return Change(index, number, 1, "", _shorten(new), note=note)

    def _split(self, index: int, paragraph: Paragraph) -> list[etree._Element]:
        """바꾸기가 남긴 나눌 자리(``_SPLIT``)에서 문단을 나눈다. 나눈 조각들(첫 조각은 원래 요소)."""
        number = self._number(paragraph)
        pieces = [paragraph.element]
        while True:
            current = pieces[-1]
            marker = next((e for e in current.iter(_w("br")) if e.get(_SPLIT)), None)
            if marker is None:
                break
            run = marker.getparent()
            assert run is not None
            if run.getparent() is not current:
                raise EditError(
                    f"Edit {index}: a line break in new_text would split paragraph p{number} inside "
                    "a hyperlink or content control. Split it outside that part.",
                    index,
                )
            depth = 0
            for element in current.iter():
                if element is marker:
                    break
                if element.tag == _w("fldChar"):
                    kind = element.get(_w("fldCharType"))
                    depth += 1 if kind == "begin" else -1 if kind == "end" else 0
            if depth != 0:
                raise EditError(
                    f"Edit {index}: a line break in new_text would split paragraph p{number} "
                    "inside a field (such as a page number or date). Split it elsewhere.",
                    index,
                )
            pieces.append(_split_at(current, run, marker))
        if len(pieces) > 1:
            for piece in pieces:
                glyphs = _flatten(piece, _FieldState()).glyphs
                if not "".join(g.char for g in glyphs).strip():
                    raise EditError(
                        f"Edit {index}: the line breaks in new_text would leave an empty paragraph "
                        f"in p{number}. Use a single line break between paragraphs, and do not "
                        "start or end new_text with one where the paragraph starts or ends.",
                        index,
                    )
        return pieces

    # --- 문단 넣기·지우기·옮기기 -------------------------------------------------------------------

    def _position(self, index: int, edit: Edit) -> str:
        position = (edit.position or "after").strip().lower()
        if position not in ("after", "before"):
            raise EditError(f'Edit {index}: position must be "after" or "before".', index)
        return position

    def _neighbour(self, element: etree._Element, step: str) -> Paragraph | None:
        """같은 자리(표 칸, 본문)에서 바로 앞(``"before"``)이나 뒤(``"after"``)의 문단."""
        sibling = element.getnext() if step == "after" else element.getprevious()
        while sibling is not None and (
            not isinstance(sibling.tag, str) or sibling.tag in _SKIPPABLE_BLOCK
        ):
            sibling = sibling.getnext() if step == "after" else sibling.getprevious()
        return next((x for x in self.paragraphs if x.element is sibling), None)

    def _style_props(self, index: int, style: str) -> etree._Element | None:
        """``style`` 이름으로 새 문단의 문단 서식. ``본문`` 이면 ``None`` (스타일 없음 = 기본 문단)."""
        value = style.strip()
        if value.lower() in ("본문", "normal", "body"):
            return None
        heading = re.fullmatch(r"제목\s*(\d)", value)
        aliases = {"문서 제목": "title", "부제목": "subtitle"}
        name = f"heading {heading.group(1)}" if heading else aliases.get(value, value)
        for style_id, style_name in self._styles.items():
            if name.lower() in (style_name.lower(), style_id.lower()):
                props = etree.Element(_w("pPr"))
                etree.SubElement(props, _w("pStyle")).set(_w("val"), style_id)
                return props
        raise EditError(
            f"Edit {index}: style {style!r} is not defined in this document. Use 제목 1 to "
            "제목 9, 본문, 문서 제목, 부제목, or leave style empty to follow the nearby paragraph.",
            index,
        )

    def _nearby_format(
        self, reference: Paragraph, position: str
    ) -> tuple[etree._Element | None, etree._Element | None]:
        """기준 문단 근처에 넣을 본문 글의 ``(문단 서식, 글자 서식)`` 사본 (구역 나누기·추적 기록 뺌).

        기준 문단이 제목이면 넣는 쪽 이웃 본문 문단을 따르고, 없으면 기본 문단 (``(None, None)``).
        """
        template: Paragraph | None = reference
        if reference.style is not None:
            # 제목 앞뒤에 넣는 글은 보통 본문이다. 이웃 본문 문단을 따르고, 없으면 기본 문단으로.
            near = self._neighbour(reference.element, position)
            template = near if near is not None and near.style is None else None
        paragraph_props, run_props = None, None
        if template is not None:
            original = template.element.find(_w("pPr"))
            if original is not None:
                paragraph_props = _without_tracking(deepcopy(original))
                section = paragraph_props.find(_w("sectPr"))
                if section is not None:
                    paragraph_props.remove(section)
            run_props = _run_props_for(template.element)
            if run_props is not None:
                run_props = _without_tracking(run_props)
        return paragraph_props, run_props

    def _insert_paragraph(self, index: int, edit: Edit) -> Change:
        """기준 문단 앞이나 뒤에 새 문단을 넣는다. ``new_text`` 의 줄마다 문단 하나."""
        reference = self._paragraph(index, edit.paragraph)
        number = self._number(reference)
        position = self._position(index, edit)
        text = re.sub(r"\n{2,}", "\n", edit.new_text.replace("\r\n", "\n")).strip("\n")
        lines = text.split("\n")
        if not text.strip() or any(not line.strip() for line in lines):
            raise EditError(
                f"Edit {index}: new_text is the text of the new paragraph and cannot be empty "
                "(empty paragraphs cannot be added).",
                index,
            )
        if _FORBIDDEN_CHARS.search(text):
            raise EditError(f"Edit {index}: new_text contains control characters.", index)
        if WALL in text:
            raise EditError(
                f"Edit {index}: new_text contains {WALL}, which stands for an image or other "
                "object. Objects cannot be added.",
                index,
            )
        if edit.style and edit.style.strip():
            paragraph_props = self._style_props(index, edit.style)
            run_props = None
        else:
            paragraph_props, run_props = self._nearby_format(reference, position)
        anchor = reference.element
        for line in lines:
            new = anchor.makeelement(_w("p"))
            if paragraph_props is not None:
                new.append(deepcopy(paragraph_props))
            run = etree.SubElement(new, _w("r"))
            if run_props is not None:
                run.append(deepcopy(run_props))
            _write_run(run, _text_tokens(line))
            if self._track:
                record = self._record("ins")
                run.addprevious(record)
                record.append(run)
                self._mark_paragraph(new, "ins")
            if position == "after":
                anchor.addnext(new)
                anchor = new
            else:
                reference.element.addprevious(new)
        self._dirty.add(reference.part)
        self._paragraphs = None
        count = f"{len(lines)} paragraphs" if len(lines) > 1 else "a paragraph"
        note = f"inserted {count} {position} p{number}: {_shorten(text)!r}"
        if edit.style and edit.style.strip():
            note += f" (style {edit.style.strip()})"
        return Change(index, number, len(lines), "", _shorten(text), kind=edit.op, note=note)

    def _check_movable(self, index: int, paragraph: Paragraph, action: str) -> None:
        """문단을 지우거나 옮겨도 되는지. 구역 나누기·변경 추적·문단 밖으로 이어지는 필드는 거절."""
        number = self._number(paragraph)
        self._check_untracked(index, paragraph)
        if paragraph.element.find(f"{_w('pPr')}/{_w('sectPr')}") is not None:
            raise EditError(
                f"Edit {index}: paragraph p{number} ends a section (page layout break), so it "
                f"cannot be {action}.",
                index,
            )
        depth = 0
        for element in paragraph.element.iter(_w("fldChar")):
            kind = element.get(_w("fldCharType"))
            depth += 1 if kind == "begin" else -1 if kind == "end" else 0
            if depth < 0:
                break
        if depth != 0:
            raise EditError(
                f"Edit {index}: paragraph p{number} is part of a field that continues into "
                f"another paragraph (such as a table of contents), so it cannot be {action}.",
                index,
            )

    def _delete_paragraph(self, index: int, edit: Edit) -> Change:
        """문단을 지운다. 지우면 구조가 깨지는 자리(칸의 마지막 문단 등)면 글자만 비운다."""
        paragraph = self._paragraph(index, edit.paragraph)
        if edit.whole_section:
            return self._delete_section(index, edit, paragraph)
        if self._track:
            return self._tracked_delete(index, paragraph)
        number = self._number(paragraph)
        self._check_movable(index, paragraph, "deleted")
        element = paragraph.element
        comments = {_w(n) for n in ("commentRangeStart", "commentRangeEnd", "commentReference")}
        if any(e.tag in comments for e in element.iter()):
            raise EditError(
                f"Edit {index}: paragraph p{number} has a comment. Delete the comment first "
                "(op delete_comment, earlier in the same call), or ask the user.",
                index,
            )
        root = self._roots[paragraph.part]
        _drop_bookmark_partners(root, element)
        parent = element.getparent()
        assert parent is not None
        note = f"deleted paragraph p{number}: {_shorten(paragraph.text)!r}"
        if _keeps_structure(parent, without=element):
            parent.remove(element)
        else:
            for child in list(element):
                if child.tag != _w("pPr"):
                    element.remove(child)
            note += (
                " (its text was removed but the empty paragraph stays, because a table cell "
                "or page part must keep a paragraph there)"
            )
        self._dirty.add(paragraph.part)
        self._paragraphs = None
        return Change(index, number, 1, _shorten(paragraph.text), "", kind=edit.op, note=note)

    def _move_paragraph(self, index: int, edit: Edit) -> Change:
        """문단을 서식 그대로 ``target`` 문단 앞이나 뒤로 옮긴다 (같은 부분 안에서만)."""
        paragraph = self._paragraph(index, edit.paragraph)
        target = self._paragraph(index, edit.target, "target")
        if edit.whole_section:
            return self._move_section(index, edit, paragraph, target)
        number, destination = self._number(paragraph), self._number(target)
        position = self._position(index, edit)
        if paragraph.element is target.element:
            raise EditError(
                f"Edit {index}: target must be a different paragraph from p{number}.", index
            )
        if paragraph.part != target.part:
            raise EditError(
                f"Edit {index}: paragraphs can only be moved within the same part (the body, "
                "one header or one footer).",
                index,
            )
        self._check_movable(index, paragraph, "moved")
        element = paragraph.element
        if self._track:
            return self._tracked_move(index, paragraph, target, position)
        source = element.getparent()
        assert source is not None
        if position == "after":
            target.element.addnext(element)
        else:
            target.element.addprevious(element)
        if not _keeps_structure(source):
            raise EditError(
                f"Edit {index}: paragraph p{number} is the last paragraph of its table cell or "
                "page part, so it cannot be moved away. Insert a copy with insert_paragraph "
                "instead.",
                index,
            )
        self._dirty.add(paragraph.part)
        self._paragraphs = None
        note = f"moved paragraph p{number} {position} p{destination}: {_shorten(paragraph.text)!r}"
        return Change(index, number, 1, _shorten(paragraph.text), "", kind=edit.op, note=note)

    def _tracked_move(
        self, index: int, paragraph: Paragraph, target: Paragraph, position: str
    ) -> Change:
        """문단 옮기기를 추적으로 남긴다: 새 자리에 사본을 넣은 기록, 옛 자리는 지운 기록."""
        number, destination = self._number(paragraph), self._number(target)
        element = paragraph.element
        label = f"paragraph p{number}"
        comments = {_w(n) for n in ("commentRangeStart", "commentRangeEnd", "commentReference")}
        if any(e.tag in comments for e in element.iter()):
            raise EditError(
                f"Edit {index}: {label} has a comment, and a tracked move would copy it. Move it "
                "without tracking (ask the user), or delete the comment first (delete_comment).",
                index,
            )
        following = element.getnext()
        while following is not None and following.tag in _SKIPPABLE_BLOCK:
            following = following.getnext()
        if following is None or following.tag != _w("p"):
            raise EditError(
                f"Edit {index}: {label} is the last paragraph of a table cell, section or "
                "document, so moving it cannot be tracked. Ask the user whether to move it "
                "without tracking (track_changes false).",
                index,
            )
        self._check_trackable(index, [element], label)
        copy = deepcopy(element)
        _strip_copy(copy)
        if position == "after":
            target.element.addnext(copy)
        else:
            target.element.addprevious(copy)
        self._track_block(copy, "ins")
        self._track_block(element, "del")
        self._dirty.add(paragraph.part)
        self._paragraphs = None
        note = (
            f"moved paragraph p{number} {position} p{destination} as a tracked change (deleted "
            f"at the old place, inserted at the new one): {_shorten(paragraph.text)!r}"
        )
        return Change(
            index, number, 1, _shorten(paragraph.text), "", kind="move_paragraph", note=note
        )

    # --- 섹션 (제목 단위) ---------------------------------------------------------------------------

    def _heading_level(self, element: etree._Element) -> int | None:
        """본문 문단의 제목 수준 (보기의 ``(제목 N)``, 없으면 문단의 ``w:outlineLvl``). 제목이 아니면 ``None``."""
        if element.tag != _w("p"):
            return None
        label = self._style_label(element)
        if label and (match := re.fullmatch(r"제목 (\d)", label)):
            return int(match.group(1))
        outline = element.find(f"{_w('pPr')}/{_w('outlineLvl')}")
        if outline is not None:
            level = _int_val(outline, 9) + 1
            return level if 1 <= level <= 9 else None
        return None

    def _section(
        self, index: int, paragraph: Paragraph, name: str = "paragraph"
    ) -> list[etree._Element]:
        """제목 문단부터 같거나 높은 수준의 다음 제목 전까지의 본문 블록."""
        number = self._number(paragraph)
        element = paragraph.element
        level = self._heading_level(element)
        parent = element.getparent()
        if level is None or parent is None or parent.tag != _w("body"):
            raise EditError(
                f"Edit {index}: whole_section needs {name} to be a heading in the document body "
                f"(shown as (제목 N) in the view), but p{number} is not. Change it to a heading "
                "with set_style first, or handle the paragraphs one by one.",
                index,
            )
        blocks = [element]
        for sibling in element.itersiblings():
            if sibling.tag == _w("sectPr"):
                break
            other = self._heading_level(sibling)
            if other is not None and other <= level:
                break
            blocks.append(sibling)
        while len(blocks) > 1 and blocks[-1].tag not in _BLOCKS:
            blocks.pop()  # 다음 제목 앞의 책갈피 등은 다음 섹션 쪽에 둔다
        return blocks

    def _check_section(
        self, index: int, blocks: list[etree._Element], label: str, action: str
    ) -> None:
        """섹션을 지우거나 옮겨도 되는지: 구역 나누기·추적 기록·밖으로 이어지는 필드·걸친 메모는 거절."""
        tracked = _TRACKED_ALL | {_w(n) for n in ("pPrChange", "rPrChange", "cellIns", "cellDel")}
        depth = 0
        events: dict[str, set[str]] = {}
        comment_tags = {
            _w("commentRangeStart"): "start",
            _w("commentRangeEnd"): "end",
            _w("commentReference"): "ref",
        }
        for block in blocks:
            for element in block.iter():
                tag = element.tag
                if tag == _w("sectPr"):
                    raise EditError(
                        f"Edit {index}: {label} contains a section break (page layout change), "
                        f"so it cannot be {action} as a whole.",
                        index,
                    )
                if tag in tracked:
                    raise EditError(
                        f"Edit {index}: {label} has tracked changes. Ask the user to accept or "
                        "reject them in Word first.",
                        index,
                    )
                if tag == _w("fldChar"):
                    kind = element.get(_w("fldCharType"))
                    depth += 1 if kind == "begin" else -1 if kind == "end" else 0
                elif tag in comment_tags:
                    events.setdefault(element.get(_w("id"), ""), set()).add(comment_tags[tag])
        if depth != 0:
            raise EditError(
                f"Edit {index}: {label} has a field that continues outside it (such as a table "
                f"of contents), so it cannot be {action}.",
                index,
            )
        if self._track and action == "moved" and events:
            raise EditError(
                f"Edit {index}: {label} has a comment, and a tracked move would copy it. Move it "
                "without tracking (ask the user), or delete the comment first (delete_comment).",
                index,
            )
        if self._track and action == "deleted":
            return  # 메모는 지운 글자에 붙은 채 남는다 (Word 와 같다)
        if events and (action == "deleted" or any(len(e) != 3 for e in events.values())):
            raise EditError(
                f"Edit {index}: {label} has a comment"
                + (" that reaches outside it" if action != "deleted" else "")
                + ". Delete the comment first (delete_comment in the same call, before this "
                "edit), or ask the user.",
                index,
            )

    def _section_label(self, blocks: list[etree._Element], heading: Paragraph) -> str:
        numbers = [
            self._number(x) for x in self.paragraphs if any(_within(x.element, b) for b in blocks)
        ]
        tables = sum(1 for b in blocks if b.tag == _w("tbl"))
        paragraphs = sum(1 for b in blocks if b.tag == _w("p"))
        extent = f"p{numbers[0]}-p{numbers[-1]}" if len(numbers) > 1 else f"p{numbers[0]}"
        counts = f"{paragraphs} paragraph{'s' if paragraphs > 1 else ''}"
        if tables:
            counts += f", {tables} table{'s' if tables > 1 else ''}"
        return f"section {_head(heading.text)!r} ({extent}: {counts})"

    def _delete_section(self, index: int, edit: Edit, heading: Paragraph) -> Change:
        """제목 문단부터 그 섹션 끝까지 지운다."""
        blocks = self._section(index, heading)
        label = self._section_label(blocks, heading)
        self._check_section(index, blocks, label, "deleted")
        if self._track:
            self._check_trackable(index, blocks, label)
            for block in blocks:
                self._track_block(block, "del")
            self._dirty.add(heading.part)
            self._paragraphs = None
            return Change(
                index,
                self._number(heading),
                len(blocks),
                _shorten(heading.text),
                "",
                kind=edit.op,
                note=f"deleted {label} as a tracked change",
            )
        body = heading.element.getparent()
        assert body is not None
        root = self._roots[heading.part]
        for block in blocks:
            _drop_bookmark_partners(root, block)
            body.remove(block)
        _fix_container_end(body)
        self._dirty.add(heading.part)
        self._paragraphs = None
        return Change(
            index,
            self._number(heading),
            len(blocks),
            _shorten(heading.text),
            "",
            kind=edit.op,
            note=f"deleted {label}",
        )

    def _move_section(
        self, index: int, edit: Edit, heading: Paragraph, target: Paragraph
    ) -> Change:
        """제목 문단부터 그 섹션 끝까지 ``target`` 앞이나 뒤로 옮긴다.

        ``after`` 이고 ``target`` 이 제목이면 그 섹션 끝 뒤로 옮긴다 ("2장 뒤로" 는 2장 전체 뒤).
        """
        position = self._position(index, edit)
        blocks = self._section(index, heading)
        label = self._section_label(blocks, heading)
        destination = self._number(target)
        parent = target.element.getparent()
        if parent is None or parent.tag != _w("body") or target.part != heading.part:
            raise EditError(
                f"Edit {index}: target must be a paragraph directly in the document body (not "
                "in a table cell, header or footer).",
                index,
            )
        if any(target.element is b for b in blocks):
            raise EditError(
                f"Edit {index}: target p{destination} is inside the section being moved. Choose "
                "a paragraph outside it.",
                index,
            )
        self._check_section(index, blocks, label, "moved")
        anchor = target.element
        if position == "after" and self._heading_level(anchor) is not None:
            anchor = self._section(index, target, "target")[-1]
            where = f"after the section of p{destination}"
        else:
            where = f"{position} p{destination}"
        if any(anchor is b for b in blocks):
            raise EditError(
                f"Edit {index}: the section is already there.",
                index,
            )
        body = heading.element.getparent()
        assert body is not None
        moving = blocks
        if self._track:
            # 옛 자리는 지운 기록, 새 자리에는 사본을 넣은 기록 (수락·거절 결과가 옮기기와 같다).
            self._check_trackable(index, blocks, label)
            moving = [deepcopy(b) for b in blocks]
            for copy in moving:
                _strip_copy(copy)
        else:
            for block in blocks:
                body.remove(block)
        if position == "after":
            for block in moving:
                anchor.addnext(block)
                anchor = block
        else:
            for block in moving:
                anchor.addprevious(block)
        if self._track:
            for block in moving:
                self._track_block(block, "ins")
            for block in blocks:
                self._track_block(block, "del")
            where += " as a tracked change (deleted at the old place, inserted at the new one)"
        _fix_container_end(body)
        self._dirty.add(heading.part)
        self._paragraphs = None
        return Change(
            index,
            self._number(heading),
            len(blocks),
            _shorten(heading.text),
            "",
            kind=edit.op,
            note=f"moved {label} {where}",
        )

    # --- 문단 스타일·목록 수준 ---------------------------------------------------------------------

    def _set_style(self, index: int, edit: Edit) -> Change:
        """이미 있는 문단의 스타일이나 목록 수준을 바꾼다.

        ``style`` 은 보기의 표시와 같은 말을 받는다: ``제목 1``~``제목 9``, ``본문``, ``문서 제목``, ``부제목``,
        문서의 스타일 이름, ``목록``·``목록 N수준``. 제목·본문으로 바꾸면 목록 번호는 뗀다. 목록 수준은 목록
        문단이면 수준만 바꾸고, 목록이 아니면 바로 앞이나 뒤의 목록 문단에 이어 붙인다.
        """
        paragraph = self._paragraph(index, edit.paragraph)
        number = self._number(paragraph)
        self._check_untracked(index, paragraph)
        value = (edit.style or "").strip()
        if not value:
            raise EditError(
                f'Edit {index}: style is required (for example "제목 2", "본문" or "목록 2수준").',
                index,
            )
        element = paragraph.element
        props = element.find(_w("pPr"))
        if props is None:
            props = etree.Element(_w("pPr"))
            element.insert(0, props)
        before = [
            deepcopy(c) for c in props if c.tag not in (_w("rPr"), _w("sectPr"), _w("pPrChange"))
        ]
        listing = re.fullmatch(r"(번호 |글머리표 )?목록(?:\s*(\d)\s*수준)?", value)
        if listing:
            level = int(listing.group(2) or 1)
            if not 1 <= level <= 9:
                raise EditError(f"Edit {index}: list levels go from 1 to 9.", index)
            wanted = listing.group(1).strip() if listing.group(1) else None
            numbering = props.find(_w("numPr"))
            same = paragraph.list_level is not None and (
                wanted is None or paragraph.list_kind == wanted
            )
            if numbering is None or not same:
                source = next(
                    (
                        x
                        for x in (
                            self._neighbour(element, "before"),
                            self._neighbour(element, "after"),
                        )
                        if x is not None
                        and x.list_level is not None
                        and (wanted is None or x.list_kind == wanted)
                    ),
                    None,
                )
                if source is None and wanted is None:
                    raise EditError(
                        f"Edit {index}: paragraph p{number} is not a list item and there is no "
                        'list right before or after it to join. To start a new list, use "번호 '
                        '목록" (numbered) or "글머리표 목록" (bulleted).',
                        index,
                    )
                if numbering is not None:
                    props.remove(numbering)
                if source is not None:
                    numbering = deepcopy(source.element.find(f"{_w('pPr')}/{_w('numPr')}"))
                else:
                    numbering = self._new_list(wanted == "글머리표", level)
                _insert_ordered(props, numbering)
            level_element = numbering.find(_w("ilvl"))
            if level_element is None:
                level_element = etree.Element(_w("ilvl"))
                numbering.insert(0, level_element)
            level_element.set(_w("val"), str(level - 1))
        else:
            styled = self._style_props(index, value)
            for old in props.findall(_w("pStyle")) + props.findall(_w("numPr")):
                props.remove(old)
            if styled is not None:
                props.insert(0, styled[0])
        note = f"changed paragraph p{number} to {value}: {_shorten(paragraph.text)!r}"
        if self._track:
            record = self._record("pPrChange")
            old = etree.SubElement(record, _w("pPr"))
            for child in before:
                old.append(_without_tracking(child))
            props.append(record)  # w:pPrChange 는 w:pPr 의 마지막
            note += " (as a tracked change)"
        elif len(props) == 0:
            element.remove(props)
        self._dirty.add(paragraph.part)
        self._paragraphs = None
        return Change(index, number, 1, "", "", kind=edit.op, note=note)

    def _new_list(self, bullet: bool, level: int) -> etree._Element:
        """새 목록의 ``w:numPr``. 문서가 이미 쓰는 같은 종류 목록 정의를 따르고, 번호 목록은 1부터."""
        # docx_create 가 이 모듈을 import 하므로 여기서 늦게 부른다 (순환 import).
        from app.documents.editing.docx_create import new_list_number

        num_id = new_list_number(self._numbering_root(), self._used_lists(), bullet, level - 1)
        numbering = etree.Element(_w("numPr"))
        etree.SubElement(numbering, _w("ilvl")).set(_w("val"), str(level - 1))
        etree.SubElement(numbering, _w("numId")).set(_w("val"), str(num_id))
        return numbering

    # --- 표 행 ----------------------------------------------------------------------------------

    def _row_of(self, index: int, edit: Edit) -> tuple[Paragraph, etree._Element, str]:
        """수정의 ``paragraph`` 가 든 표 행. ``(문단, w:tr, "표1 2행" 같은 위치)``."""
        paragraph = self._paragraph(index, edit.paragraph)
        number = self._number(paragraph)
        row = next(paragraph.element.iterancestors(_w("tr")), None)
        if row is None or row.getparent() is None or row.getparent().tag != _w("tbl"):
            raise EditError(
                f"Edit {index}: paragraph p{number} is not in a table cell. Set paragraph to a "
                "paragraph inside the row, such as one labeled (표1 2행 1열).",
                index,
            )
        found = re.search(r"표\d+ \d+행", paragraph.location or "")
        label = found.group(0) if found else f"the row of p{number}"
        tracked = {_w(n) for n in ("ins", "del", "moveFrom", "moveTo")}
        if any(e.tag in tracked for e in row.iter()) and not self._own_inserted(row):
            raise EditError(
                f"Edit {index}: {label} has tracked changes. Accept or reject them first "
                f'(op "accept_changes" or "reject_changes" with paragraph "p{number}").',
                index,
            )
        return paragraph, row, label

    def _insert_row(self, index: int, edit: Edit) -> Change:
        """표 행 앞이나 뒤에 행을 넣는다. 칸 서식은 기준 행을 따르고 ``cells`` 값을 칸마다 넣는다."""
        paragraph, row, label = self._row_of(index, edit)
        position = self._position(index, edit)
        # 새 행 바로 아래에 올 행. 그 행의 칸이 위에서 이어진 병합(continue)이면 새 행도 병합 구역 안이다.
        lower = _sibling_row(row, "after") if position == "after" else row
        template = row
        after = _sibling_row(row, "after")
        if row.find(f"{_w('trPr')}/{_w('tblHeader')}") is not None and after is not None:
            template = after  # 머리글 행(굵게·배경) 대신 아래 데이터 행의 모양을 따른다
        cells = [c.replace("\r\n", "\n") for c in (edit.cells or ())]
        columns = list(template.iterchildren(_w("tc")))
        if not any(c.strip() for c in cells):
            raise EditError(
                f"Edit {index}: cells must list the values of the new row, from left to right.",
                index,
            )
        if len(cells) > len(columns):
            raise EditError(
                f"Edit {index}: cells has {len(cells)} values, but {label} has only "
                f"{len(columns)} cells.",
                index,
            )
        for value in cells:
            if "\n" in value or _FORBIDDEN_CHARS.search(value) or WALL in value:
                raise EditError(
                    f"Edit {index}: cell values cannot contain line breaks, control characters "
                    f"or {WALL}.",
                    index,
                )
        new_row = row.makeelement(_w("tr"))
        for child in template:
            if child.tag in (_w("tblPrEx"), _w("trPr")):
                copied = _without_tracking(deepcopy(child))
                for drop in list(copied.iter(_w("tblHeader"), _w("cnfStyle"))):
                    drop.getparent().remove(drop)
                new_row.append(copied)
        merged = 0
        values = cells + [""] * (len(columns) - len(cells))
        for (column, start, _), value in zip(_row_cells(template), values, strict=True):
            below = _cell_at(lower, start)
            if below is not None and _vmerge(below) == "continue":
                # 병합 구역 가운데에 넣는다: Word 처럼 구역을 늘리고, 값은 넣지 않는다 (시작 칸에 있다).
                cell = _blank_cell(column, "")
                _set_vmerge(cell, "continue")
                merged += 1 if value.strip() else 0
            else:
                cell = _blank_cell(column, value)
            new_row.append(cell)
        if position == "after":
            row.addnext(new_row)
        else:
            row.addprevious(new_row)
        if self._track:
            self._mark_row(new_row, "ins")
            for cell_paragraph in new_row.iter(_w("p")):
                self._track_runs(cell_paragraph, "ins")
        self._dirty.add(paragraph.part)
        self._paragraphs = None
        note = f"inserted a table row {position} {label}: {_shorten(' | '.join(cells))!r}"
        if self._track:
            note += " (as a tracked change)"
        if merged:
            note += (
                f" ({merged} value(s) were left out because those cells are part of a vertically "
                "merged cell above, which keeps its text)"
            )
        return Change(
            index,
            self._number(paragraph),
            1,
            "",
            _shorten(" | ".join(cells)),
            kind=edit.op,
            note=note,
        )

    def _delete_row(self, index: int, edit: Edit) -> Change:
        """``paragraph`` 가 든 표 행을 지운다."""
        paragraph, row, label = self._row_of(index, edit)
        table = row.getparent()
        assert table is not None
        if len(list(table.iterchildren(_w("tr")))) == 1:
            raise EditError(
                f"Edit {index}: {label} is the only row of its table. Use delete_table to delete "
                "the whole table.",
                index,
            )
        own = self._track and self._own_inserted(row)
        comments = {_w(n) for n in ("commentRangeStart", "commentRangeEnd", "commentReference")}
        if not self._track and any(e.tag in comments for e in row.iter()):
            raise EditError(
                f"Edit {index}: {label} has a comment. Delete the comment first (op "
                "delete_comment, earlier in the same call), or ask the user.",
                index,
            )
        depth = 0
        for element in row.iter(_w("fldChar")):
            kind = element.get(_w("fldCharType"))
            depth += 1 if kind == "begin" else -1 if kind == "end" else 0
        if depth != 0:
            raise EditError(
                f"Edit {index}: {label} has a field that continues outside the row, so it "
                "cannot be deleted.",
                index,
            )
        values = []
        for cell in row.iterchildren(_w("tc")):
            inside = [x.text for x in self.paragraphs if _within(x.element, cell) and x.text]
            values.append(" ".join(inside))
        summary = _shorten(" | ".join(values))
        note = f"deleted table row {label}: {summary!r}"
        if self._track and not own:
            # 행은 남기고 기록만 단다. 세로 병합 정리는 수락할 때 한다.
            self._check_trackable(index, [row], label)
            self._mark_row(row, "del")
            for cell_paragraph in row.iter(_w("p")):
                self._track_runs(cell_paragraph, "del")
            note += " (as a tracked change)"
        else:
            _drop_bookmark_partners(self._roots[paragraph.part], row)
            _unmerge_row(row)
            table.remove(row)
            if own:
                note += " (you had inserted it as a tracked change, so it was removed)"
        self._dirty.add(paragraph.part)
        self._paragraphs = None
        return Change(index, self._number(paragraph), 1, summary, "", kind=edit.op, note=note)

    # --- Markdown 넣기, 표 지우기 -----------------------------------------------------------------

    def _text_width(self, element: etree._Element) -> int:
        """``element`` 가 든 구역의 본문 너비 (쪽 너비 − 좌우 여백, twip). 모르면 A4 기본값."""
        section = None
        for sibling in element.itersiblings():
            if sibling.tag == _w("sectPr"):
                section = sibling
                break
            found = sibling.find(f"{_w('pPr')}/{_w('sectPr')}") if sibling.tag == _w("p") else None
            if found is not None:
                section = found
                break
        if section is None:
            own = element.find(f"{_w('pPr')}/{_w('sectPr')}")
            section = own
        if section is None:
            return _DEFAULT_TEXT_WIDTH
        size = section.find(_w("pgSz"))
        margin = section.find(_w("pgMar"))
        try:
            width = int(size.get(_w("w"))) if size is not None else 11906
            if size is not None and size.get(_w("orient")) == "landscape" and size.get(_w("h")):
                width = max(width, int(size.get(_w("h"))))
            left = int(margin.get(_w("left"), "1440")) if margin is not None else 1440
            right = int(margin.get(_w("right"), "1440")) if margin is not None else 1440
            gutter = int(margin.get(_w("gutter"), "0")) if margin is not None else 0
        except (TypeError, ValueError):
            return _DEFAULT_TEXT_WIDTH
        text = width - left - right - gutter
        return text if text > 1000 else _DEFAULT_TEXT_WIDTH

    def _used_lists(self) -> list[str]:
        """본문 문단이 쓰는 목록 ``numId`` (나온 순서, 중복 없음)."""
        used: list[str] = []
        main = self._parts[0][0]
        for paragraph in self.paragraphs:
            if paragraph.part != main or paragraph.list_level is None:
                continue
            num = paragraph.element.find(f"{_w('pPr')}/{_w('numPr')}/{_w('numId')}")
            value = num.get(_w("val")) if num is not None else None
            if value and value not in used:
                used.append(value)
        return used

    def _numbering_root(self) -> etree._Element:
        """목록 번호 부분 (없으면 만든다)."""
        name = self._related("numbering")
        root = self._part_root(name) if name else None
        if root is None:
            root = etree.Element(_w("numbering"), nsmap={"w": W})
            name = self._create_part("numbering", name, root, _NUMBERING_TYPE)
        assert name is not None
        self._dirty.add(name)
        return root

    def _styles_or_new(self) -> tuple[str, etree._Element]:
        """스타일 부분 (없으면 기본 문단 스타일만 든 부분을 만든다)."""
        name = self._related("styles")
        root = self._part_root(name) if name else None
        if root is None:
            root = etree.fromstring(
                f'<w:styles xmlns:w="{W}"><w:style w:type="paragraph" w:default="1" '
                'w:styleId="Normal"><w:name w:val="Normal"/></w:style></w:styles>'
            )
            name = self._create_part("styles", name, root, _STYLES_TYPE)
            self._dirty.add(name)
        assert name is not None
        return name, root

    def _insert_markdown(self, index: int, edit: Edit) -> Change:
        """기준 문단 앞이나 뒤에 Markdown 내용(제목·문단·목록·표 등)을 넣는다.

        스타일 없는 본문 문단은 ``insert_paragraph`` 처럼 이웃 본문 문단의 서식을 따른다. 본문 바로 아래의
        문단만 기준으로 쓴다 (표 칸·머리글·바닥글·콘텐츠 컨트롤 안은 거절).
        """
        # docx_create 가 이 모듈을 import 하므로 여기서 늦게 부른다 (순환 import).
        from app.documents.editing.docx_create import markdown_blocks

        reference = self._paragraph(index, edit.paragraph)
        number = self._number(reference)
        position = self._position(index, edit)
        element = reference.element
        parent = element.getparent()
        if parent is None or parent.tag != _w("body"):
            raise EditError(
                f"Edit {index}: insert_markdown needs a paragraph directly in the document body, "
                f"but p{number} is inside a table cell, header, footer or content control. Use "
                "insert_paragraph there instead.",
                index,
            )
        if _FORBIDDEN_CHARS.search(edit.new_text.replace("\t", " ")):
            raise EditError(f"Edit {index}: new_text contains control characters.", index)
        styles_name, styles_root = self._styles_or_new()
        try:
            made = markdown_blocks(
                edit.new_text,
                styles=styles_root,
                numbering=self._numbering_root,
                rels=self._main_rels(),
                text_width=self._text_width(element),
                used_lists=self._used_lists(),
            )
        except EditError as exc:
            raise EditError(f"Edit {index}: {exc}", index) from exc
        if "styles" in made.changed:
            self._dirty.add(styles_name)
            self._refresh_style_names()
        if "rels" in made.changed:
            self._dirty.add(_rels_name(self._parts[0][0]))
        paragraph_props, run_props = self._nearby_format(reference, position)
        if paragraph_props is not None:
            for drop in paragraph_props.findall(_w("numPr")):
                paragraph_props.remove(drop)  # 목록 문단 옆에 넣어도 본문 글은 목록이 아니다
        for block in made.plain:
            _apply_body_format(block, paragraph_props, run_props)
        blocks = list(made.blocks)
        before = element if position == "after" else element.getprevious()
        after = element.getnext() if position == "after" else element
        if blocks[0].tag == _w("tbl") and before is not None and before.tag == _w("tbl"):
            blocks.insert(0, etree.Element(_w("p")))
        if blocks[-1].tag == _w("tbl") and (
            after is None or after.tag in (_w("tbl"), _w("sectPr"))
        ):
            blocks.append(etree.Element(_w("p")))  # 표 뒤에는 문단이 와야 한다
        anchor = element
        for block in blocks:
            if position == "after":
                anchor.addnext(block)
                anchor = block
            else:
                element.addprevious(block)
        if self._track:
            # 표 앞뒤에 붙인 빈 문단은 기록하지 않는다 (거절해도 표가 본문 끝에 오지 않게).
            for block in made.blocks:
                self._track_block(block, "ins")
        self._dirty.add(reference.part)
        self._paragraphs = None
        parts = []
        for count, word in (
            (made.headings, "heading"),
            (made.paragraphs, "paragraph"),
            (made.list_items, "list item"),
        ):
            if count:
                parts.append(f"{count} {word}{'s' if count > 1 else ''}")
        if made.tables:
            parts.append(f"{made.tables} table{'s' if made.tables > 1 else ''} ({made.rows} rows)")
        note = f"inserted {', '.join(parts) or 'content'} {position} p{number}"
        if self._track:
            note += " (as a tracked change)"
        if made.images:
            note += (
                f"; {made.images} image(s) were written as their alt text in brackets, because "
                "images cannot be added"
            )
        text = _shorten(" ".join(_paragraph_text(b) for b in made.blocks if b.tag == _w("p")))
        return Change(index, number, len(blocks), "", text, kind=edit.op, note=note)

    def _delete_table(self, index: int, edit: Edit) -> Change:
        """``paragraph`` 가 든 (가장 안쪽) 표를 통째로 지운다."""
        paragraph = self._paragraph(index, edit.paragraph)
        number = self._number(paragraph)
        table = next(paragraph.element.iterancestors(_w("tbl")), None)
        if table is None:
            raise EditError(
                f"Edit {index}: paragraph p{number} is not in a table. Set paragraph to a "
                "paragraph inside the table, such as one labeled (표1 1행 1열).",
                index,
            )
        found = re.findall(r"표\d+", paragraph.location or "")
        label = found[-1] if found else "the table"
        own = self._track and self._own_inserted(table)
        tracked = {_w(n) for n in ("ins", "del", "moveFrom", "moveTo", "cellIns", "cellDel")}
        if not own and any(e.tag in tracked for e in table.iter()):
            raise EditError(
                f"Edit {index}: {label} has tracked changes. Accept or reject them first "
                '(op "accept_changes" or "reject_changes").',
                index,
            )
        comments = {_w(n) for n in ("commentRangeStart", "commentRangeEnd", "commentReference")}
        if not self._track and any(e.tag in comments for e in table.iter()):
            raise EditError(
                f"Edit {index}: {label} has a comment. Delete the comment first (op "
                "delete_comment, earlier in the same call), or ask the user.",
                index,
            )
        depth = 0
        for element in table.iter(_w("fldChar")):
            kind = element.get(_w("fldCharType"))
            depth += 1 if kind == "begin" else -1 if kind == "end" else 0
        if depth != 0:
            raise EditError(
                f"Edit {index}: {label} has a field that continues outside it, so it cannot be "
                "deleted.",
                index,
            )
        rows = list(table.iterchildren(_w("tr")))
        first_row = (
            " | ".join(self._cell_text(c) for c, _, _ in _row_cells(rows[0])) if rows else ""
        )
        columns = max((len(_row_cells(row)) for row in rows), default=0)
        summary = _shorten(first_row)
        note = f"deleted {label} ({len(rows)} rows x {columns} columns): {summary!r}"
        if self._track and not own:
            self._check_trackable(index, [table], label)
            self._track_block(table, "del")
            note += " (as a tracked change)"
        else:
            _drop_bookmark_partners(self._roots[paragraph.part], table)
            _remove_table(table)
            if own:
                note += " (you had inserted it as a tracked change, so it was removed)"
        self._dirty.add(paragraph.part)
        self._paragraphs = None
        return Change(index, number, 1, summary, "", kind=edit.op, note=note)

    # --- 표 열 ----------------------------------------------------------------------------------

    def _column_of(self, index: int, edit: Edit) -> tuple[Paragraph, etree._Element, int, int, str]:
        """수정의 ``paragraph`` 가 든 칸의 표와 격자 열 범위. ``(문단, w:tbl, 시작, 끝, "표1")``."""
        paragraph, row, _ = self._row_of(index, edit)
        table = row.getparent()
        assert table is not None
        cell = next(paragraph.element.iterancestors(_w("tc")))
        start, end = next((a, b) for c, a, b in _row_cells(row) if c is cell)
        found = re.search(r"표\d+", paragraph.location or "")
        label = found.group(0) if found else "the table"
        tracked = {_w(n) for n in ("ins", "del", "moveFrom", "moveTo", "cellIns", "cellDel")}
        if any(e.tag in tracked for e in table.iter()):
            raise EditError(
                f"Edit {index}: {label} has tracked changes, so its columns cannot be changed "
                "yet. Ask the user to accept or reject them in Word first.",
                index,
            )
        if any(c.tag != _w("tr") and c.tag in (_w("sdt"), _w("customXml")) for c in table):
            raise EditError(
                f"Edit {index}: {label} has rows inside content controls, so its columns cannot "
                "be changed.",
                index,
            )
        return paragraph, table, start, end, label

    @staticmethod
    def _grid_widths(table: etree._Element) -> tuple[etree._Element | None, list[int]]:
        """표 격자(``w:tblGrid``)와 열 너비. 격자가 행과 맞지 않으면 너비는 빈 목록 (너비는 고치지 않는다)."""
        grid = table.find(_w("tblGrid"))
        columns = 0
        for row in table.iterchildren(_w("tr")):
            cells = _row_cells(row)
            after = _int_val(row.find(f"{_w('trPr')}/{_w('gridAfter')}"), 0)
            columns = max(columns, (cells[-1][2] if cells else 0) + after)
        if grid is None:
            return None, []
        widths = [_int_val_attr(c, "w") for c in grid.iterchildren(_w("gridCol"))]
        if len(widths) != columns or not all(widths):
            return grid, []
        return grid, widths

    @staticmethod
    def _sync_cell_widths(table: etree._Element, widths: list[int]) -> None:
        for row in table.iterchildren(_w("tr")):
            for cell, a, b in _row_cells(row):
                _set_cell_width(cell, sum(widths[a:b]))

    def _cell_text(self, cell: etree._Element | None) -> str:
        if cell is None:
            return ""
        return " ".join(x.text for x in self.paragraphs if _within(x.element, cell) and x.text)

    def _column_header(self, rows: list[etree._Element], column: int) -> str:
        """열 이름: 위에서부터 그 열만 덮는(가로 병합이 아닌) 첫 칸의 글자."""
        for row in rows:
            cell = next((c for c, a, b in _row_cells(row) if a == column and b == column + 1), None)
            if cell is not None:
                return _shorten(self._cell_text(cell))
        return ""

    def _insert_column(self, index: int, edit: Edit) -> Change:
        """``paragraph`` 가 든 칸의 열 앞이나 뒤에 열을 넣는다. ``cells`` 는 위에서 아래로 행마다 값."""
        paragraph, table, start, end, label = self._column_of(index, edit)
        position = self._position(index, edit)
        boundary = end if position == "after" else start
        rows = list(table.iterchildren(_w("tr")))
        values = [c.replace("\r\n", "\n") for c in (edit.cells or ())]
        if len(values) > len(rows):
            raise EditError(
                f"Edit {index}: cells has {len(values)} values, but {label} has only {len(rows)} "
                "rows. Give one value per row, from top to bottom (including the header row).",
                index,
            )
        for value in values:
            if "\n" in value or _FORBIDDEN_CHARS.search(value) or WALL in value:
                raise EditError(
                    f"Edit {index}: cell values cannot contain paragraph breaks, control "
                    f"characters or {WALL} (use {LINE_BREAK} for a line break inside a cell).",
                    index,
                )
        values += [""] * (len(rows) - len(values))
        # ("widen", 걸친 칸) 또는 ("after"·"before", 기준 칸): 기준 칸의 뒤나 앞에 새 칸을 넣는다.
        plan: list[tuple[str, etree._Element]] = []
        for number, row in enumerate(rows, 1):
            cells = _row_cells(row)
            before = _int_val(row.find(f"{_w('trPr')}/{_w('gridBefore')}"), 0)
            last = cells[-1][2] if cells else before
            if not cells or boundary < before or boundary > last:
                raise EditError(
                    f"Edit {index}: row {number} of {label} has an irregular shape (cells that "
                    "start late or end early) where the column would go, so the column cannot "
                    "be added.",
                    index,
                )
            spanning = next((c for c, a, b in cells if a < boundary < b), None)
            if spanning is not None:
                plan.append(("widen", spanning))
                continue
            ends = {b: c for c, _, b in cells}
            starts = {a: c for c, a, _ in cells}
            if position == "after" and boundary in ends or boundary not in starts:
                plan.append(("after", ends[boundary]))
            else:
                plan.append(("before", starts[boundary]))
        grid, widths = self._grid_widths(table)
        reference_column = end - 1 if position == "after" else start
        ignored = 0
        for (action, element), value in zip(plan, values, strict=True):
            if action == "widen":
                _set_span(element, _span(element) + 1)
                ignored += 1 if value.strip() else 0
                continue
            new = _blank_cell(element, value)
            _set_span(new, 1)
            if action == "after":
                element.addnext(new)
            else:
                element.addprevious(new)
        if (
            grid is not None and widths
        ):  # 격자가 행과 맞을 때만 너비를 고친다 (표 전체 너비는 그대로)
            new_col = etree.Element(_w("gridCol"))
            columns = list(grid.iterchildren(_w("gridCol")))
            if boundary < len(columns):
                columns[boundary].addprevious(new_col)
            else:
                columns[-1].addnext(new_col)
            total = sum(widths)
            widths.insert(boundary, widths[reference_column])
            widths = _scaled(widths, total)
            for column, width in zip(grid.iterchildren(_w("gridCol")), widths, strict=True):
                column.set(_w("w"), str(width))
            self._sync_cell_widths(table, widths)
        self._dirty.add(paragraph.part)
        self._paragraphs = None
        header = self._column_header(rows, reference_column)
        where = (
            f" {position} {header!r}" if header else f" {position} column {reference_column + 1}"
        )
        note = f"inserted a column{where} in {label}"
        if any(v.strip() for v in values):
            note += f": {_shorten(' | '.join(values))!r}"
        if ignored:
            note += (
                f" ({ignored} value(s) were left out because those rows have a cell merged "
                "across the new column, which was widened instead)"
            )
        return Change(index, self._number(paragraph), 1, "", "", kind=edit.op, note=note)

    def _delete_column(self, index: int, edit: Edit) -> Change:
        """``paragraph`` 가 든 칸의 열을 지운다. 걸친 가로 병합 칸은 좁힌다."""
        paragraph, table, start, end, label = self._column_of(index, edit)
        rows = list(table.iterchildren(_w("tr")))
        grid, widths = self._grid_widths(table)
        header = self._column_header(rows, start)
        removed: list[etree._Element] = []
        narrowed: list[tuple[etree._Element, int]] = []
        for number, row in enumerate(rows, 1):
            cells = _row_cells(row)
            before = _int_val(row.find(f"{_w('trPr')}/{_w('gridBefore')}"), 0)
            last = cells[-1][2] if cells else before
            if start < before or end > last:
                raise EditError(
                    f"Edit {index}: row {number} of {label} has an irregular shape (cells that "
                    "start late or end early) at this column, so it cannot be deleted.",
                    index,
                )
            kept = 0
            for cell, a, b in cells:
                overlap = max(0, min(b, end) - max(a, start))
                if overlap == 0:
                    kept += 1
                elif overlap == b - a:
                    removed.append(cell)
                else:
                    narrowed.append((cell, b - a - overlap))
                    kept += 1
            if kept == 0:
                raise EditError(
                    f"Edit {index}: this is the only column of {label}. Use delete_table to "
                    "remove the whole table.",
                    index,
                )
        comments = {_w(n) for n in ("commentRangeStart", "commentRangeEnd", "commentReference")}
        for cell in removed:
            if any(e.tag in comments for e in cell.iter()):
                raise EditError(
                    f"Edit {index}: a cell in this column has a comment. Delete the comment "
                    "first (op delete_comment, earlier in the same call), or ask the user.",
                    index,
                )
            depth = 0
            for element in cell.iter(_w("fldChar")):
                kind = element.get(_w("fldCharType"))
                depth += 1 if kind == "begin" else -1 if kind == "end" else 0
            if depth != 0:
                raise EditError(
                    f"Edit {index}: a cell in this column has a field that continues outside "
                    "it, so the column cannot be deleted.",
                    index,
                )
        values = [self._cell_text(cell) for cell in removed]
        root = self._roots[paragraph.part]
        for cell in removed:
            _drop_bookmark_partners(root, cell)
            parent = cell.getparent()
            assert parent is not None
            parent.remove(cell)
        for cell, span in narrowed:
            _set_span(cell, span)
        if grid is not None and widths:
            for column in list(grid.iterchildren(_w("gridCol")))[start:end]:
                grid.remove(column)
            total = sum(widths)
            widths = _scaled(widths[:start] + widths[end:], total)
            for column, width in zip(grid.iterchildren(_w("gridCol")), widths, strict=True):
                column.set(_w("w"), str(width))
            self._sync_cell_widths(table, widths)
        self._dirty.add(paragraph.part)
        self._paragraphs = None
        name = f"{header!r}" if header else f"column {start + 1}"
        summary = _shorten(" | ".join(values))
        note = f"deleted column {name} of {label}: {summary!r}"
        return Change(index, self._number(paragraph), 1, summary, "", kind=edit.op, note=note)

    def _merge(self, index: int, edit: Edit) -> Change:
        """문단과 바로 다음 문단을 합친다. 앞 문단의 문단 서식을 쓴다."""
        first = self._paragraph(index, edit.paragraph)
        number = self._number(first)
        sibling = first.element.getnext()
        while sibling is not None and (
            not isinstance(sibling.tag, str) or sibling.tag in _SKIPPABLE_BLOCK
        ):
            sibling = sibling.getnext()
        second = next((x for x in self.paragraphs if x.element is sibling), None)
        if second is None:
            raise EditError(
                f"Edit {index}: paragraph p{number} has no paragraph right after it in the same "
                "place (for example it is the last paragraph of a table cell, or a table follows). "
                "Only a paragraph and the one right after it can be merged.",
                index,
            )
        self._check_untracked(index, first)
        self._check_untracked(index, second)
        for paragraph in (first, second):
            if paragraph.element.find(f"{_w('pPr')}/{_w('sectPr')}") is not None:
                raise EditError(
                    f"Edit {index}: paragraph p{self._number(paragraph)} ends a section (page "
                    "layout break), so these paragraphs cannot be merged.",
                    index,
                )
        left, right = first.text, second.text
        if self._track:
            return self._tracked_merge(index, first, second)
        if left and right and not left[-1].isspace() and not right[0].isspace():
            editor = _ParagraphEditor(first, index)
            try:
                editor.insert_end(" ")
                editor.commit()
            except EditError:
                space = etree.SubElement(first.element, _w("r"))
                _write_run(space, _text_tokens(" "))
        for child in list(second.element):
            if child.tag != _w("pPr"):
                first.element.append(child)
        parent = second.element.getparent()
        assert parent is not None
        parent.remove(second.element)
        self._dirty.add(first.part)
        self._paragraphs = None
        merged = next(x for x in self.paragraphs if x.element is first.element)
        return Change(
            index,
            number,
            1,
            "",
            "",
            kind="merge",
            note=f"merged paragraph p{number} with the next paragraph "
            f"(p{self._number(second)}): {_shorten(merged.text)!r}",
        )

    def _tracked_merge(self, index: int, first: Paragraph, second: Paragraph) -> Change:
        """문단 합치기를 추적으로 남긴다: 앞 문단 기호에 ``w:del``, 사이에 넣는 공백은 ``w:ins``.

        수락하면 Word 처럼 두 문단이 합쳐진다 (합친 문단은 뒤 문단의 문단 서식을 따른다).
        """
        number = self._number(first)
        left, right = first.text, second.text
        if left and right and not left[-1].isspace() and not right[0].isspace():
            record = self._record("ins")
            record.append(_new_run(_run_props_for(first.element), _text_tokens(" ")))
            first.element.append(record)
        self._mark_paragraph(first.element, "del")
        self._dirty.add(first.part)
        self._paragraphs = None
        note = (
            f"merged paragraph p{number} with the next paragraph (p{self._number(second)}) as a "
            f"tracked change: {_shorten(left + ' ' + right)!r}"
        )
        return Change(index, number, 1, "", "", kind="merge", note=note)

    # --- 변경 추적으로 남기기 -----------------------------------------------------------------

    def _record(self, kind: str) -> etree._Element:
        """작성자·시각·새 id 를 단 추적 기록 요소 (``ins``·``del``)."""
        element = etree.Element(_w(kind))
        element.set(_w("id"), str(self._new_id()))
        element.set(_w("author"), self._author)
        element.set(_w("date"), self._date)
        return element

    def _new_id(self) -> int:
        """문서 안에서 겹치지 않는 새 ``w:id``. 모든 XML 부분의 ``w:id`` 최댓값 다음부터 쓴다."""
        if self._next_id is None:
            found = [0]
            for name, data in self._data.items():
                if name.endswith(".xml") and name not in self._roots:
                    found += [int(x) for x in _ID.findall(data)]
            for root in self._roots.values():
                found += [int(x) for x in _ID.findall(etree.tostring(root))]
            self._next_id = max(found) + 1
        value = self._next_id
        self._next_id += 1
        return value

    def _mark_paragraph(self, paragraph: etree._Element, kind: str) -> None:
        """문단 기호에 추적 기록(``w:pPr/w:rPr/w:ins|del``)을 단다."""
        props = paragraph.find(_w("pPr"))
        if props is None:
            props = etree.Element(_w("pPr"))
            paragraph.insert(0, props)
        mark = props.find(_w("rPr"))
        if mark is None:
            mark = etree.Element(_w("rPr"))
            after = [c for c in props if c.tag in (_w("sectPr"), _w("pPrChange"))]
            if after:
                after[0].addprevious(mark)
            else:
                props.append(mark)
        mark.insert(0, self._record(kind))

    def _check_range(
        self, index: int, paragraph: Paragraph, start: int, length: int, new: str
    ) -> bool:
        """바꿀 범위가 추적 기록에 닿는지 확인한다. 범위가 모두 우리가 추적으로 넣은 글자면 ``True``.

        다른 사람의 추적 기록(넣은 글자, 서식 변경, 숨은 지운 글자 자리)에 닿으면 거절한다. 추적 기록이 있는
        문단은 나누지 않는다.
        """
        number = self._number(paragraph)
        span = paragraph.glyphs[start : start + length]
        marks = {g.tracked for g in span}
        own = (self._author, FORMAT_CHANGE + self._author)
        foreign = {m for m in marks if m is not None and m not in own}
        touches_deleted = any(
            start < at < start + length and who != self._author for at, who in paragraph.deleted_at
        )
        if foreign or touches_deleted:
            raise EditError(
                f"Edit {index}: the text to change in paragraph p{number} overlaps tracked "
                "changes (shown as {+ +} / {- -} or a 서식 바뀜 label) that were not made by you. "
                f'Accept or reject them first (op "accept_changes" or "reject_changes" with '
                f'paragraph "p{number}"), or change only the text around them.',
                index,
            )
        if "\n" in new and paragraph.tracked:
            raise EditError(
                f"Edit {index}: paragraph p{number} has tracked changes, so it cannot be split. "
                "Accept or reject them first.",
                index,
            )
        return marks == {self._author}

    def _tracked_replace(
        self, index: int, paragraph: Paragraph, start: int, old: str, new: str
    ) -> None:
        r"""``start`` 의 ``old`` 를 ``new`` 로 바꾸되, 지운 글자는 ``w:del``, 넣은 글자는 ``w:ins`` 로 남긴다.

        달라진 부분만 기록한다 (바꾸기 알고리즘과 같은 비교). ``\n`` 은 나눌 자리 표시로 넣고, 문단 기호의
        추적은 호출한 쪽이 나눈 뒤에 단다.
        """
        number = self._number(paragraph)
        glyphs = paragraph.glyphs
        span = glyphs[start : start + len(old)]
        for _, i1, i2, j1, j2 in reversed(_diff_ops(old, new, span)):
            begin, end, text = start + i1, start + i2, new[j1:j2]
            removed = glyphs[begin:end]
            _check_change(index, number, removed, text)
            if removed:
                source = removed[0].run
                groups: list[list[Any]] = []  # [run, 첫 토큰, 마지막 토큰]
                for glyph in removed:
                    if groups and groups[-1][0] is glyph.run:
                        groups[-1][2] = glyph.index
                    else:
                        groups.append([glyph.run, glyph.index, glyph.index])
                spot: tuple[etree._Element, int] | None = None  # 넣을 자리: 맨 오른쪽 조각 뒤
                for run, low, high in reversed(groups):
                    piece = _cut_run(run, low, high + 1)
                    holder = piece.getparent()
                    assert holder is not None
                    if _record_author(piece) == self._author:
                        # 우리가 추적으로 넣은 글자를 지우면 기록 없이 없앤다 (Word 와 같다).
                        at = holder.index(piece)
                        holder.remove(piece)
                        spot = spot or (holder, at)
                        continue
                    record = self._record("del")
                    piece.addprevious(record)
                    record.append(piece)
                    for node in piece.iter(_w("t"), _w("instrText")):
                        node.tag = _w("delText") if node.tag == _w("t") else _w("delInstrText")
                    spot = spot or (holder, holder.index(record) + 1)
                assert spot is not None
                parent, position = spot
            else:
                glyph, after = _insert_anchor(
                    glyphs, begin, (start, start + len(old)), self._author
                )
                if glyph is None:
                    raise EditError(
                        f"Edit {index}: there is no plain text next to this position in paragraph "
                        f"p{number} to take formatting from.",
                        index,
                    )
                source = glyph.run
                if after:
                    _split_run(source, glyph.index + 1)
                    parent, position = source.getparent(), source.getparent().index(source) + 1
                else:
                    right = _split_run(source, glyph.index)
                    target = right if right is not None else source
                    parent, position = target.getparent(), target.getparent().index(target)
            if not text:
                continue
            assert source is not None and parent is not None
            props = source.find(_w("rPr"))
            # 우리 넣기 기록 안이면 새 글자도 그 기록에 그대로 들어간다 (기록을 겹치지 않는다).
            inside_own = parent.tag in _TRACKED_SHOWN and parent.get(_w("author")) == self._author
            for k, segment in enumerate(text.split("\n")):
                if k:
                    # 문단을 나눌 자리 표시는 넣기 기록 밖에 둔다 (나눌 때 문단 바로 아래여야 한다).
                    marker = _new_run(props, [_Token("x", "", _split_marker())])
                    parent.insert(position, marker)
                    position += 1
                if segment:
                    run = _new_run(props, _text_tokens(segment))
                    if not inside_own:
                        record = self._record("ins")
                        record.append(run)
                        run = record
                    parent.insert(position, run)
                    position += 1

    def _tracked_delete(self, index: int, paragraph: Paragraph) -> Change:
        """문단 지우기를 추적으로 남긴다: 글자를 모두 ``w:del`` 로 감싸고 문단 기호에 ``w:del`` 을 단다."""
        number = self._number(paragraph)
        element = paragraph.element
        if self._own_inserted(element):
            parent = element.getparent()
            assert parent is not None
            if _keeps_structure(parent, without=element):
                _drop_bookmark_partners(self._roots[paragraph.part], element)
                parent.remove(element)
                self._dirty.add(paragraph.part)
                self._paragraphs = None
                note = (
                    f"removed paragraph p{number}, which you had inserted as a tracked change: "
                    f"{_shorten(paragraph.text)!r}"
                )
                return Change(
                    index,
                    number,
                    1,
                    _shorten(paragraph.text),
                    "",
                    kind="delete_paragraph",
                    note=note,
                )
        self._check_untracked(index, paragraph)
        following = element.getnext()
        while following is not None and following.tag in _SKIPPABLE_BLOCK:
            following = following.getnext()
        if (
            following is None
            or following.tag != _w("p")
            or element.find(f"{_w('pPr')}/{_w('sectPr')}") is not None
        ):
            raise EditError(
                f"Edit {index}: paragraph p{number} is the last paragraph of a table cell, "
                "section or document, so deleting it cannot be tracked. Delete its text instead "
                "(replace with an empty new_text).",
                index,
            )
        if any(g.wall and g.run is None for g in paragraph.glyphs):
            raise EditError(
                f"Edit {index}: paragraph p{number} contains an object (such as an equation) "
                "whose deletion cannot be tracked.",
                index,
            )
        self._track_runs(element, "del")
        self._mark_paragraph(element, "del")
        self._dirty.add(paragraph.part)
        self._paragraphs = None
        note = f"deleted paragraph p{number} as a tracked change: {_shorten(paragraph.text)!r}"
        return Change(
            index, number, 1, _shorten(paragraph.text), "", kind="delete_paragraph", note=note
        )

    def _track_runs(self, paragraph: etree._Element, kind: str) -> None:
        """문단 바로 아래 run 을 하나씩 추적 기록(``ins``·``del``)으로 감싼다. 이미 기록 안인 run 은 그대로."""
        runs = [
            r
            for r in paragraph.iter(_w("r"))
            if not any(a.tag == _w("r") for a in r.iterancestors())
            and _closest_paragraph(r) is paragraph
            and not _inside_record(r, paragraph)
        ]
        for run in runs:
            record = self._record(kind)
            run.addprevious(record)
            record.append(run)
            if kind == "del":
                for node in run.iter(_w("t"), _w("instrText")):
                    node.tag = _w("delText") if node.tag == _w("t") else _w("delInstrText")

    def _mark_row(self, row: etree._Element, kind: str) -> None:
        """표 행에 행 넣음·지움 기록(``w:trPr/w:ins|del``)을 단다 (``w:trPrChange`` 앞)."""
        props = row.find(_w("trPr"))
        if props is None:
            props = etree.Element(_w("trPr"))
            first = row.find(_w("tblPrEx"))
            row.insert(0 if first is None else row.index(first) + 1, props)
        record = self._record(kind)
        change = props.find(_w("trPrChange"))
        if change is not None:
            change.addprevious(record)
        else:
            props.append(record)

    def _track_block(self, block: etree._Element, kind: str) -> None:
        """블록(문단·표 등)을 통째로 넣거나 지운 기록으로 남긴다.

        글자는 run 마다, 표는 행마다 기록을 단다. 표 칸 밖 문단은 문단 기호에도 기록을 단다 (칸 안 문단
        기호는 행 기록이 대신한다).
        """
        for row in block.iter(_w("tr")):
            self._mark_row(row, kind)
        for paragraph in block.iter(_w("p")):
            self._track_runs(paragraph, kind)
            cell = next(paragraph.iterancestors(_w("tc")), None)
            if cell is None or not _within(cell, block):
                self._mark_paragraph(paragraph, kind)

    def _check_trackable(self, index: int, blocks: list[etree._Element], label: str) -> None:
        """블록을 추적으로 지우거나 옮길 수 있는지: run 밖의 개체(수식 등)는 기록으로 감쌀 수 없다."""
        for block in blocks:
            for paragraph in block.iter(_w("p")):
                if any(g.wall and g.run is None for g in _flatten(paragraph, _FieldState()).glyphs):
                    raise EditError(
                        f"Edit {index}: {label} contains an object (such as an equation) whose "
                        "deletion cannot be tracked. Ask the user whether to do it without "
                        "tracking (track_changes false).",
                        index,
                    )

    def _own_inserted(self, block: etree._Element) -> bool:
        """블록이 통째로 우리가 추적으로 넣은 것인지 (지우면 기록 없이 없앤다, Word 와 같다)."""
        rows = list(block.iter(_w("tr")))
        if block.tag == _w("p"):
            mark = block.find(f"{_w('pPr')}/{_w('rPr')}/{_w('ins')}")
            if mark is None or mark.get(_w("author")) != self._author:
                return False
        elif not rows:
            return False
        for row in rows:
            record = row.find(f"{_w('trPr')}/{_w('ins')}")
            if record is None or record.get(_w("author")) != self._author:
                return False
        tags = (*_TRACKED_ALL, _w("rPrChange"), _w("pPrChange"), *_PROP_CHANGE_KEEP)
        for record in block.iter(*tags):
            if record.tag in _TRACKED_HIDDEN or record.get(_w("author")) != self._author:
                return False
        for paragraph in block.iter(_w("p")):
            for run in paragraph.iter(_w("r")):
                outside = _closest_paragraph(run) is paragraph and not _inside_record(
                    run, paragraph
                )
                if outside and any(c.tag != _w("rPr") for c in run):
                    return False
        return True

    def _refuse_tracking(self, index: int, edit: Edit) -> None:
        """추적 모드에서 아직 추적할 수 없는 수정을 거절한다 (추적 없이 몰래 고치지 않는다)."""
        if self._track and edit.op in _UNTRACKABLE_OPS:
            raise EditError(
                f'Edit {index}: op "{edit.op}" cannot be made as a tracked change (Word cannot '
                "track inserted or deleted table columns). Ask the user whether to make it "
                "without tracking (track_changes false).",
                index,
            )

    # --- 글자 서식 ------------------------------------------------------------------------------

    def _format(self, index: int, edit: Edit) -> Change:
        """``old_text``(없으면 ``paragraph`` 문단 전체)의 글자 서식을 바꾼다.

        범위 경계에서 run 을 쪼개고 조각마다 ``w:rPr`` 를 고친다. 빼는 서식이 스타일에서 오면 직접 서식으로
        끈다 (``w:b w:val="0"``). 추적 모드면 바꾸기 전 서식을 ``w:rPrChange`` 로 남긴다.
        """
        wanted = edit.format
        if wanted is None or wanted.is_empty():
            raise EditError(
                f'Edit {index}: format is required for op "format" (for example '
                '{"bold": true} or {"color": "red"}).',
                index,
            )
        changes = _format_changes(index, wanted)
        old = edit.old_text.replace("\r\n", "\n")
        if not old:
            if not edit.paragraph:
                raise EditError(
                    f"Edit {index}: set old_text (the text to format), or leave it empty and set "
                    'paragraph (for example "p12") to format the whole paragraph.',
                    index,
                )
            target = self._paragraph(index, edit.paragraph)
            if not target.text.strip():
                raise EditError(
                    f"Edit {index}: paragraph p{self._number(target)} has no text to format.", index
                )
            hits = [(target, 0, len(target.text))]
        else:
            target = self._paragraph(index, edit.paragraph) if edit.paragraph else None
            targets = [target] if target is not None else self.paragraphs
            hits = [(p, s, len(old)) for p in targets for s in _find_all(p.text, old)]
            if not hits:
                raise self._not_found(index, old, targets, edit.paragraph)
            if len(hits) > 1 and not edit.replace_all:
                raise EditError(
                    f"Edit {index}: old_text appears {len(hits)} times. Include more surrounding "
                    'text so that it matches exactly one place, set paragraph (for example "p12") '
                    "to search only one paragraph, or set replace_all to format every occurrence.",
                    index,
                )
        by_paragraph: dict[int, tuple[Paragraph, list[tuple[int, int]]]] = {}
        for paragraph, start, length in hits:
            self._check_range(index, paragraph, start, length, "")
            _, spans = by_paragraph.setdefault(id(paragraph.element), (paragraph, []))
            if not spans or start >= spans[-1][0] + spans[-1][1]:
                spans.append((start, length))
        changed = 0
        places: list[int] = []
        for paragraph, spans in by_paragraph.values():
            # 뒤에서부터 고친다: 앞쪽 글자의 run·토큰 위치는 그대로 남는다.
            for start, length in reversed(spans):
                changed += self._format_span(paragraph, start, length, changes)
            places.extend([self._number(paragraph)] * len(spans))
            self._dirty.add(paragraph.part)
        if not changed:
            raise EditError(
                f"Edit {index}: the text already has that formatting, so nothing changed.", index
            )
        self._paragraphs = None
        first = hits[0][0]
        sample = first.text[hits[0][1] : hits[0][1] + hits[0][2]]
        where = f"p{places[0]}"
        if len(places) > 1:
            numbers = list(dict.fromkeys(places))
            shown = ", ".join(f"p{n}" for n in numbers[:30])
            more = f" and {len(numbers) - 30} more" if len(numbers) > 30 else ""
            where = f"{len(places)} places in {len(numbers)} paragraphs ({shown}{more})"
        note = f"formatted {where} as {_describe_format(wanted)}: {_shorten(sample)!r}"
        return Change(
            index,
            places[0],
            len(places),
            "",
            "",
            kind=edit.op,
            note=note,
            places=tuple(places),
        )

    def _format_span(
        self, paragraph: Paragraph, start: int, length: int, changes: list["_PropChange"]
    ) -> int:
        """문단 글자 ``[start, start + length)`` 의 서식을 바꾼다. 서식이 실제로 바뀐 run 조각 수."""
        groups: list[list[Any]] = []  # [run, 첫 토큰, 마지막 토큰]
        for glyph in paragraph.glyphs[start : start + length]:
            if glyph.run is None:  # run 밖의 벽(수식 등)은 서식이 없다
                continue
            if groups and groups[-1][0] is glyph.run:
                groups[-1][2] = glyph.index
            else:
                groups.append([glyph.run, glyph.index, glyph.index])
        changed = 0
        for run, low, high in reversed(groups):
            piece = _cut_run(run, low, high + 1)
            props = piece.find(_w("rPr"))
            if props is None:
                props = etree.Element(_w("rPr"))
                piece.insert(0, props)
            before = deepcopy(props)
            for change in changes:
                self._apply_prop(change, props, piece, paragraph.element)
            # 이미 그 서식이면(짝 태그 bCs 등만 다르면) 그대로 둔다: 헛 수정과 헛 추적 기록을 막는다.
            if all(
                _main_prop(props, tags[0]) == _main_prop(before, tags[0]) for tags, _, _ in changes
            ):
                for child in list(props):
                    props.remove(child)
                for child in before:
                    props.append(child)
                if len(props) == 0:
                    piece.remove(props)
                continue
            changed += 1
            if self._track and _record_author(piece) != self._author:
                self._track_format(props, before)
            if len(props) == 0:
                piece.remove(props)
        return changed

    def _apply_prop(
        self,
        change: "_PropChange",
        props: etree._Element,
        run: etree._Element,
        paragraph: etree._Element,
    ) -> None:
        """서식 항목 하나를 ``props``(run 의 ``w:rPr``)에 넣거나 뺀다."""
        tags, value, off = change
        if value is not None:
            for tag in tags:
                _set_prop(props, tag, value)
            return
        styled = self._styled(tags[0], props, paragraph)
        for tag in tags:
            for old in props.findall(_w(tag)):
                props.remove(old)
        if styled:
            _set_prop(props, tags[0], off)

    def _track_format(self, props: etree._Element, before: etree._Element) -> None:
        """서식 바꾸기를 ``w:rPrChange`` 로 남긴다. 이미 기록(우리 것)이 있으면 처음 서식을 그대로 둔다."""
        old_change = before.find(_w("rPrChange"))
        for existing in props.findall(_w("rPrChange")):
            props.remove(existing)
        if old_change is not None:
            props.append(old_change)
            return
        record = self._record("rPrChange")
        old = etree.SubElement(record, _w("rPr"))
        for child in _without_tracking(before):
            old.append(child)
        props.append(record)

    def _styled(self, tag: str, props: etree._Element, paragraph: etree._Element) -> bool:
        """``tag`` 서식(예: ``"b"``)이 직접 서식이 아니라 스타일(문자·문단 스타일, 문서 기본값)에서 오는지."""
        root = self._styles_root()
        if root is None:
            return False
        styles = {st.get(_w("styleId"), ""): st for st in root.iter(_w("style"))}
        chains: list[str | None] = []
        ref = props.find(_w("rStyle"))
        chains.append(ref.get(_w("val")) if ref is not None else None)
        pref = paragraph.find(f"{_w('pPr')}/{_w('pStyle')}")
        if pref is not None:
            chains.append(pref.get(_w("val")))
        else:
            default = next(
                (
                    st.get(_w("styleId"))
                    for st in styles.values()
                    if st.get(_w("type")) == "paragraph" and st.get(_w("default")) in ("1", "true")
                ),
                None,
            )
            chains.append(default)
        for style_id in chains:
            seen: set[str] = set()
            while style_id and style_id not in seen and style_id in styles:
                seen.add(style_id)
                style = styles[style_id]
                found = style.find(f"{_w('rPr')}/{_w(tag)}")
                if found is not None:
                    return _prop_on(tag, found)
                based = style.find(_w("basedOn"))
                style_id = based.get(_w("val")) if based is not None else None
        default_props = root.find(f"{_w('docDefaults')}/{_w('rPrDefault')}/{_w('rPr')}")
        found = default_props.find(_w(tag)) if default_props is not None else None
        return found is not None and _prop_on(tag, found)

    def _styles_root(self) -> etree._Element | None:
        name = self._related("styles")
        return self._part_root(name) if name else None

    # --- 메모 ----------------------------------------------------------------------------------

    def _comment(self, index: int, edit: Edit) -> Change:
        """``old_text``(없으면 문단 전체)에 메모를 단다. 메모 내용은 ``new_text``.

        ``comment`` 를 주면 그 메모에 답글을 단다.
        """
        if edit.comment and edit.comment.strip():
            return self._reply(index, edit)
        body = self._comment_body(index, edit)
        old = edit.old_text.replace("\r\n", "\n")
        if not old:
            if not edit.paragraph:
                raise EditError(
                    f"Edit {index}: set old_text to the text to comment on, or paragraph (for "
                    'example "p12") to comment on a whole paragraph.',
                    index,
                )
            paragraph = self._paragraph(index, edit.paragraph)
            start, end = 0, len(paragraph.glyphs)
            if not paragraph.text.strip():
                raise EditError(
                    f"Edit {index}: paragraph {edit.paragraph} is empty, so there is nothing to "
                    "comment on.",
                    index,
                )
        else:
            target = self._paragraph(index, edit.paragraph) if edit.paragraph else None
            targets = [target] if target is not None else self.paragraphs
            hits = [(p, s) for p in targets for s in _find_all(p.text, old)]
            if not hits:
                raise self._not_found(index, old, targets, edit.paragraph)
            if len(hits) > 1:
                raise EditError(
                    f"Edit {index}: old_text appears {len(hits)} times. Include more surrounding "
                    'text or set paragraph (for example "p12") so that it matches one place.',
                    index,
                )
            (paragraph, start), end = hits[0], hits[0][1] + len(old)
        number = self._number(paragraph)
        comment_id = str(self._new_id())
        reference = self._reference_run(comment_id)
        range_start = etree.Element(_w("commentRangeStart"))
        range_end = etree.Element(_w("commentRangeEnd"))
        for marker in (range_start, range_end):
            marker.set(_w("id"), comment_id)
        element = paragraph.element
        if old:
            first, last = paragraph.glyphs[start], paragraph.glyphs[end - 1]
            if first.run is None or last.run is None or first.scope is not last.scope:
                raise EditError(
                    f"Edit {index}: the text to comment on in paragraph p{number} starts or ends "
                    "at an object or crosses the edge of a hyperlink. Choose text inside or "
                    "outside the link.",
                    index,
                )
            _split_run(last.run, last.index + 1)
            right = _split_run(first.run, first.index)
            begin = right if right is not None else first.run
            finish = right if right is not None and first.run is last.run else last.run
            begin.addprevious(range_start)
            finish.addnext(range_end)
            range_end.addnext(reference)
        else:
            props = element.find(_w("pPr"))
            element.insert(0 if props is None else element.index(props) + 1, range_start)
            element.append(range_end)
            element.append(reference)
        self._add_comment(comment_id, body)
        self._dirty.add(paragraph.part)
        self._paragraphs = None
        anchor = paragraph.text[start:end]
        note = f"commented on p{number} {_shorten(anchor)!r}: {_shorten(body)!r}"
        return Change(index, number, 1, _shorten(anchor), _shorten(body), kind="comment", note=note)

    @staticmethod
    def _comment_body(index: int, edit: Edit) -> str:
        body = edit.new_text.replace("\r\n", "\n").strip()
        if not body:
            raise EditError(
                f"Edit {index}: new_text is the comment text and cannot be empty.", index
            )
        if _FORBIDDEN_CHARS.search(body):
            raise EditError(f"Edit {index}: new_text contains control characters.", index)
        return body

    def _target_comment(self, index: int, edit: Edit) -> tuple[str, dict[str, Comment]]:
        """수정의 ``comment``(``"c3"``·``"3"``)가 가리키는 메모 id 와 모든 메모."""
        raw = (edit.comment or "").strip()
        match = re.fullmatch(r"[cC]?(\d+)", raw)
        if not match:
            raise EditError(
                f'Edit {index}: comment must be a comment id from the view, such as "c3".', index
            )
        comments = self._comments()
        comment_id = match.group(1)
        if comment_id not in comments:
            known = ", ".join(f"c{c}" for c in comments) or "none"
            raise EditError(
                f"Edit {index}: comment c{comment_id} does not exist (comments: {known}).", index
            )
        return comment_id, comments

    def _comment_element(self, comment_id: str) -> etree._Element:
        name = self._related("comments")
        root = self._part_root(name) if name else None
        assert root is not None
        return next(c for c in root.iter(_w("comment")) if c.get(_w("id")) == comment_id)

    def _ensure_namespace(self, name: str, prefix: str, uri: str) -> etree._Element:
        """부분의 뿌리 요소에 이름공간을 선언하고 ``mc:Ignorable`` 에 넣는다 (옛 Word 가 모르는 속성용).

        lxml 은 있는 요소에 이름공간을 더할 수 없어 뿌리 요소를 새로 만들고 자식을 옮긴다.
        """
        root = self._roots[name]
        ignorable = f"{{{_MC}}}Ignorable"
        if root.nsmap.get(prefix) == uri and prefix in (root.get(ignorable) or "").split():
            return root
        nsmap = {**root.nsmap, prefix: uri, "mc": _MC}
        new = etree.Element(root.tag, nsmap=nsmap)
        for key, value in root.attrib.items():
            new.set(key, value)
        words = (root.get(ignorable) or "").split()
        new.set(ignorable, " ".join([*words, prefix] if prefix not in words else words))
        new.text = root.text
        for child in list(root):
            new.append(child)
        self._roots[name] = new
        self._dirty.add(name)
        return new

    def _new_hex_id(self) -> str:
        """문서 안에서 겹치지 않는 8자리 16진수 id (``w14:paraId``·``durableId``, ``0x7FFFFFFF`` 미만)."""
        if self._next_hex is None:
            pattern = re.compile(rb'(?:paraId|textId|durableId)="([0-9A-Fa-f]{8})"')
            found = [0x10000000]
            for name, data in self._data.items():
                if name.endswith(".xml") and name not in self._roots:
                    found += [int(x, 16) for x in pattern.findall(data)]
            for root in self._roots.values():
                found += [int(x, 16) for x in pattern.findall(etree.tostring(root))]
            self._next_hex = max(v for v in found if v < 0x7FFFFFF0) + 1
        value = self._next_hex
        self._next_hex += 1
        return f"{value:08X}"

    def _para_id(self, comment: etree._Element) -> str:
        """메모 마지막 문단의 ``w14:paraId`` (없으면 붙인다). 답글·해결 표시가 메모를 이것으로 가리킨다."""
        paragraphs = list(comment.iter(_w("p")))
        if not paragraphs:
            paragraphs = [etree.SubElement(comment, _w("p"))]
        last = paragraphs[-1]
        value = last.get(_PARA_ID)
        if not value:
            value = self._new_hex_id()
            last.set(_PARA_ID, value)
            name = self._related("comments")
            assert name is not None
            self._dirty.add(name)
        return value

    def _comments_extended_or_new(self) -> etree._Element:
        """답글·해결 표시 부분 (없으면 만든다)."""
        name = self._related_type(_REL_COMMENTS_EXTENDED)
        root = self._part_root(name) if name else None
        if root is None:
            root = etree.Element(f"{{{W15}}}commentsEx", nsmap={"w15": W15, "mc": _MC})
            root.set(f"{{{_MC}}}Ignorable", "w15")
            name = self._create_part(
                "commentsExtended", name, root, _COMMENTS_EXTENDED_TYPE, _REL_COMMENTS_EXTENDED
            )
        assert name is not None
        self._dirty.add(name)
        return root

    @staticmethod
    def _extended_entry(root: etree._Element, para_id: str) -> etree._Element:
        """``para_id`` 메모의 ``w15:commentEx`` (없으면 해결 안 됨으로 만든다)."""
        for entry in root.iter(f"{{{W15}}}commentEx"):
            if (entry.get(f"{{{W15}}}paraId") or "").upper() == para_id.upper():
                return entry
        entry = etree.SubElement(root, f"{{{W15}}}commentEx")
        entry.set(f"{{{W15}}}paraId", para_id)
        entry.set(f"{{{W15}}}done", "0")
        return entry

    def _add_durable_id(self, para_id: str) -> None:
        """새 Word 의 메모 id 부분(``commentsIds``·``commentsExtensible``)이 있으면 새 메모 항목을 더한다."""
        ids_name = self._related_type(_REL_COMMENTS_IDS)
        ids_root = self._part_root(ids_name) if ids_name else None
        if ids_root is None or ids_name is None:
            return
        durable = self._new_hex_id()
        entry = etree.SubElement(ids_root, f"{{{W16CID}}}commentId")
        entry.set(f"{{{W16CID}}}paraId", para_id)
        entry.set(f"{{{W16CID}}}durableId", durable)
        self._dirty.add(ids_name)
        ext_name = self._related_type(_REL_COMMENTS_EXTENSIBLE)
        ext_root = self._part_root(ext_name) if ext_name else None
        if ext_root is not None and ext_name is not None:
            item = etree.SubElement(ext_root, f"{{{W16CEX}}}commentExtensible")
            item.set(f"{{{W16CEX}}}durableId", durable)
            item.set(f"{{{W16CEX}}}dateUtc", self._date)
            self._dirty.add(ext_name)

    def _reply(self, index: int, edit: Edit) -> Change:
        """메모에 답글을 단다. 답글의 답글은 부모 메모의 답글로 단다 (Word 의 스레드는 한 단계)."""
        comment_id, comments = self._target_comment(index, edit)
        body = self._comment_body(index, edit)
        parent_id = comments[comment_id].parent or comment_id
        name = self._related("comments")
        assert name is not None
        self._ensure_namespace(name, "w14", W14)
        parent_para = self._para_id(self._comment_element(parent_id))
        reply_id = str(self._new_id())
        reply_para = self._para_id(self._add_comment(reply_id, body))
        extended = self._comments_extended_or_new()
        self._extended_entry(extended, parent_para)
        self._extended_entry(extended, reply_para).set(f"{{{W15}}}paraIdParent", parent_para)
        self._add_durable_id(reply_para)
        placed = self._place_reply(parent_id, reply_id)
        self._paragraphs = None
        number = next(
            (
                self._number(x)
                for x in self.paragraphs
                if any(c.id == parent_id for c in x.comments)
            ),
            1,
        )
        note = f"replied to comment c{parent_id}: {_shorten(body)!r}"
        if not placed:
            note += " (the comment has no mark in the text, so the reply may show on its own)"
        return Change(index, number, 1, "", _shorten(body), kind="comment", note=note)

    def _place_reply(self, parent_id: str, reply_id: str) -> bool:
        """답글의 범위 표시·표시 run 을 부모 메모의 것 바로 뒤에 둔다. 부모 표시가 없으면 ``False``."""
        placed = False
        for name, _ in self._parts:
            root = self._roots[name]
            for element in list(root.iter(_w("commentRangeStart"), _w("commentRangeEnd"))):
                if element.get(_w("id")) == parent_id:
                    marker = etree.Element(element.tag)
                    marker.set(_w("id"), reply_id)
                    element.addnext(marker)
                    self._dirty.add(name)
            for reference in list(root.iter(_w("commentReference"))):
                if reference.get(_w("id")) == parent_id:
                    run = reference.getparent()
                    assert run is not None
                    run.addnext(self._reference_run(reply_id))
                    self._dirty.add(name)
                    placed = True
        return placed

    def _resolve_comment(self, index: int, edit: Edit) -> Change:
        """메모를 해결됨으로 표시한다. ``new_text`` 가 있으면 먼저 답글을 단다. 답글을 주면 그 스레드."""
        comment_id, comments = self._target_comment(index, edit)
        target = comments[comment_id].parent or comment_id
        replied = ""
        if edit.new_text.strip():
            self._reply(index, edit)
            replied = f" after replying {_shorten(edit.new_text.strip())!r}"
        name = self._related("comments")
        assert name is not None
        self._ensure_namespace(name, "w14", W14)
        para = self._para_id(self._comment_element(target))
        entry = self._extended_entry(self._comments_extended_or_new(), para)
        already = entry.get(f"{{{W15}}}done") in ("1", "true")
        entry.set(f"{{{W15}}}done", "1")
        self._paragraphs = None
        number = next(
            (self._number(x) for x in self.paragraphs if any(c.id == target for c in x.comments)),
            1,
        )
        replies = sum(1 for c in comments.values() if c.parent == target)
        extra = f" (and {replies} repl{'ies' if replies > 1 else 'y'})" if replies else ""
        note = f"resolved comment c{target}{extra}{replied}"
        if already and not replied:
            note = f"comment c{target} was already resolved"
        return Change(index, number, 1, "", "", kind=edit.op, note=note)

    def _delete_comment(self, index: int, edit: Edit) -> Change:
        """메모와 그 답글을 지운다 (답글을 주면 그 답글만). 추적 모드여도 기록 없이 지운다 (Word 와 같다)."""
        comment_id, comments = self._target_comment(index, edit)
        target = comments[comment_id]
        ids = {comment_id}
        if not target.parent:
            ids |= {c.id for c in comments.values() if c.parent == comment_id}
        number = next(
            (
                self._number(x)
                for x in self.paragraphs
                if any(
                    c.id == comment_id or any(rp.id == comment_id for rp in c.replies)
                    for c in x.comments
                )
            ),
            1,
        )
        self._remove_comments(ids)
        self._paragraphs = None
        replies = len(ids) - 1
        extra = f" and its {replies} repl{'ies' if replies > 1 else 'y'}" if replies else ""
        kind = "reply" if target.parent else "comment"
        note = f"deleted {kind} c{comment_id}{extra}: {_shorten(target.text)!r}"
        return Change(index, number, len(ids), _shorten(target.text), "", kind=edit.op, note=note)

    def _remove_comments(self, ids: set[str]) -> None:
        """메모들을 메모 부분·본문 표시·답글·해결·id 부분에서 모두 지운다."""
        name = self._related("comments")
        root = self._part_root(name) if name else None
        paras: set[str] = set()
        if root is not None and name is not None:
            for comment in list(root.iter(_w("comment"))):
                if comment.get(_w("id")) in ids:
                    for paragraph in comment.iter(_w("p")):
                        if paragraph.get(_PARA_ID):
                            paras.add(paragraph.get(_PARA_ID, "").upper())
                    root.remove(comment)
            self._dirty.add(name)
        for part, _ in self._parts:
            part_root = self._roots[part]
            for marker in list(
                part_root.iter(
                    _w("commentRangeStart"), _w("commentRangeEnd"), _w("commentReference")
                )
            ):
                if marker.get(_w("id")) not in ids:
                    continue
                parent = marker.getparent()
                assert parent is not None
                parent.remove(marker)
                self._dirty.add(part)
                if parent.tag == _w("r") and all(c.tag == _w("rPr") for c in parent):
                    holder = parent.getparent()
                    assert holder is not None
                    holder.remove(parent)
                    if (
                        holder.tag in _TRACKED_ALL
                        and len(holder) == 0
                        and holder.getparent() is not None
                    ):
                        holder.getparent().remove(holder)
        extended = self._comments_extended()
        if extended is not None and paras:
            for entry in list(extended.iter(f"{{{W15}}}commentEx")):
                if (entry.get(f"{{{W15}}}paraId") or "").upper() in paras:
                    extended.remove(entry)
            self._dirty.add(self._related_type(_REL_COMMENTS_EXTENDED) or "")
        ids_name = self._related_type(_REL_COMMENTS_IDS)
        ids_root = self._part_root(ids_name) if ids_name else None
        durable: set[str] = set()
        if ids_root is not None and ids_name is not None and paras:
            for entry in list(ids_root.iter(f"{{{W16CID}}}commentId")):
                if (entry.get(f"{{{W16CID}}}paraId") or "").upper() in paras:
                    durable.add((entry.get(f"{{{W16CID}}}durableId") or "").upper())
                    ids_root.remove(entry)
            self._dirty.add(ids_name)
        ext_name = self._related_type(_REL_COMMENTS_EXTENSIBLE)
        ext_root = self._part_root(ext_name) if ext_name else None
        if ext_root is not None and ext_name is not None and durable:
            for item in list(ext_root.iter(f"{{{W16CEX}}}commentExtensible")):
                if (item.get(f"{{{W16CEX}}}durableId") or "").upper() in durable:
                    ext_root.remove(item)
            self._dirty.add(ext_name)

    def _comment_references(self) -> set[str]:
        """본문·머리글·바닥글에 표시(``w:commentReference``)가 있는 메모 id."""
        return {
            e.get(_w("id"), "")
            for name, _ in self._parts
            for e in self._roots[name].iter(_w("commentReference"))
        }

    def _reference_run(self, comment_id: str) -> etree._Element:
        """본문에 메모 표시를 하는 run (``w:commentReference``)."""
        run = etree.Element(_w("r"))
        style = self._style_id("annotation reference")
        if style:
            props = etree.SubElement(run, _w("rPr"))
            etree.SubElement(props, _w("rStyle")).set(_w("val"), style)
        etree.SubElement(run, _w("commentReference")).set(_w("id"), comment_id)
        return run

    def _style_id(self, name: str) -> str | None:
        """스타일 이름(소문자 비교)의 id. 문서에 없으면 ``None``."""
        return next((i for i, n in self._styles.items() if n.lower() == name), None)

    def _add_comment(self, comment_id: str, body: str) -> etree._Element:
        """메모 부분에 메모를 더한다. 메모 부분이 없으면 만들고 관계·내용 형식을 등록한다."""
        name = self._related("comments")
        root = self._part_root(name) if name else None
        if root is None:
            name = self._create_comments_part(name)
            root = self._roots[name]
        comment = etree.SubElement(root, _w("comment"))
        comment.set(_w("id"), comment_id)
        comment.set(_w("author"), self._author)
        comment.set(_w("date"), self._date)
        initials = "".join(word[0] for word in self._author.split() if word)[:4]
        if initials:
            comment.set(_w("initials"), initials)
        text_style = self._style_id("annotation text")
        reference_style = self._style_id("annotation reference")
        for k, line in enumerate(body.split("\n")):
            paragraph = etree.SubElement(comment, _w("p"))
            if text_style:
                props = etree.SubElement(paragraph, _w("pPr"))
                etree.SubElement(props, _w("pStyle")).set(_w("val"), text_style)
            if k == 0:
                mark = etree.SubElement(paragraph, _w("r"))
                if reference_style:
                    style = etree.SubElement(etree.SubElement(mark, _w("rPr")), _w("rStyle"))
                    style.set(_w("val"), reference_style)
                etree.SubElement(mark, _w("annotationRef"))
            if line:
                paragraph.append(_new_run(None, _text_tokens(line.replace("\t", " "))))
        self._dirty.add(name)
        return comment

    def _create_comments_part(self, name: str | None) -> str:
        """빈 메모 부분을 만들고 내용 형식에 등록한다 (``_create_part`` 참고)."""
        root = etree.Element(_w("comments"), nsmap={"w": W})
        return self._create_part("comments", name, root, _COMMENTS_TYPE)

    def _create_part(
        self,
        kind: str,
        name: str | None,
        root: etree._Element,
        content_type: str,
        rel_type: str | None = None,
    ) -> str:
        """본문과 ``kind`` 관계로 이어질 새 부분을 만들고 내용 형식에 등록한다.

        ``name`` 은 관계는 있는데 파일이 없을 때의 그 이름이다. 없으면 본문 옆에 ``{kind}.xml`` 로 만들고
        본문 관계도 더한다. 관계 형식은 ``rel_type``(전체 주소), 없으면 표준 관계 형식 + ``kind``.
        """
        main = self._parts[0][0]
        linked = name is not None
        if name is None:
            folder = main.rsplit("/", 1)[0] + "/" if "/" in main else ""
            name = f"{folder}{kind}.xml"
        self._roots[name] = root
        self._register_type(name, content_type)
        if linked:
            return name
        rels = self._main_rels()
        used = {rel.get("Id") for rel in rels}
        number = 1
        while f"rId{number}" in used:
            number += 1
        rel = etree.SubElement(rels, f"{{{_PKG_REL}}}Relationship")
        rel.set("Id", f"rId{number}")
        rel.set("Type", rel_type or _REL_BASE + kind)
        rel.set("Target", f"{kind}.xml")
        self._dirty.add(_rels_name(main))
        return name

    def _main_rels(self) -> etree._Element:
        """본문 부분의 관계 (없으면 만든다)."""
        name = _rels_name(self._parts[0][0])
        rels = self._part_root(name)
        if rels is None:
            rels = etree.Element(f"{{{_PKG_REL}}}Relationships", nsmap={None: _PKG_REL})
            self._roots[name] = rels
        return rels

    def _register_type(self, name: str, content_type: str) -> None:
        """``[Content_Types].xml`` 에 새 부분의 내용 형식을 등록한다 (이미 있으면 그대로)."""
        types = self._part_root("[Content_Types].xml")
        if types is None:
            return
        if any(o.get("PartName") == f"/{name}" for o in types):
            return
        override = etree.SubElement(types, f"{{{_CONTENT_TYPES}}}Override")
        override.set("PartName", f"/{name}")
        override.set("ContentType", content_type)
        self._dirty.add("[Content_Types].xml")

    def _review(self, index: int, edit: Edit, *, accept: bool) -> Change:
        """변경 추적 기록을 수락하거나 거절한다. ``paragraph`` 가 없으면 문서 전체(본문·머리글·바닥글).

        ``paragraph`` 를 주면 그 문단과, 그 문단이 든 행·칸·표의 구조 기록도 다룬다. ``author`` 를 주면 그
        사람이 남긴 기록만 다룬다. 다룰 수 없는 기록(칸 단위 기록 등)이 범위에 있으면 실패한다.
        """
        verb = "accept" if accept else "reject"
        who = edit.author.strip() if edit.author and edit.author.strip() else None
        if edit.paragraph:
            paragraph = self._paragraph(index, edit.paragraph)
            number = self._number(paragraph)
            scope = [paragraph.element]
            where = f"paragraph p{number}"
            regions = [(paragraph.part, paragraph.element)]
            containers = [paragraph.element, *_table_props_around(paragraph.element)]
            structure = [e for c in containers for e in c.iter(*_STRUCTURE_RECORDS)]
        else:
            number = 1
            regions = [(name, self._roots[name]) for name, _ in self._parts]
            scope = [p for _, root in regions for p in root.iter(_w("p"))]
            where = "the document"
            containers = [root for _, root in regions]
            structure = [e for _, root in regions for e in root.iter(*_STRUCTURE_RECORDS)]
        if who:
            where += f" by {who}"
        structure = [e for e in structure if _by(e, who) and _structure_record(e)]
        for container in containers:
            for element in container.iter(*_UNSUPPORTED_TRACKING):
                if not _by(element, who):
                    continue
                if element.tag == _w("numberingChange") and accept:
                    continue
                raise EditError(
                    f"Edit {index}: {where} has tracked changes that cannot be {verb}ed here "
                    "(such as inserted or merged single table cells, or old-style numbering "
                    f"changes). Ask the user to {verb} them in Word, or {verb} the changes of "
                    "one paragraph at a time with paragraph.",
                    index,
                )
        count = 0
        references = self._comment_references()
        for element in scope:
            count += _review_content(element, accept, who)
        lost = references - self._comment_references()
        if lost:
            self._remove_comments(lost)  # 표시째 없어진 글자에 달린 메모는 Word 처럼 지운다
        for container in containers:
            for element in list(container.iter(_w("numberingChange"))):
                if _by(element, who) and element.getparent() is not None:
                    element.getparent().remove(element)
                    count += 1
        doomed: list[etree._Element] = []  # 지울 행
        for record in structure:
            parent = record.getparent()
            if parent is None:
                continue
            count += 1
            if record.tag in _TRACKED_ALL:  # 행 넣음·지움 (w:trPr 안)
                parent.remove(record)
                if (record.tag in _TRACKED_SHOWN) != accept:
                    row = parent.getparent()
                    if row is not None and row not in doomed:
                        doomed.append(row)
            elif accept:
                parent.remove(record)
            else:
                keep, first = _PROP_CHANGE_KEEP[record.tag]
                _restore_props(record, keep, first)
        joins: list[etree._Element] = []
        for element in scope:
            if any(_within(element, row) for row in doomed):
                continue
            mark = element.find(f"{_w('pPr')}/{_w('rPr')}")
            if mark is None:
                continue
            for record in list(mark):
                if record.tag not in _TRACKED_ALL or not _by(record, who):
                    continue
                count += 1
                inserted = record.tag in _TRACKED_SHOWN
                mark.remove(record)
                if inserted != accept:  # 넣은 문단 기호를 거절하거나, 지운 문단 기호를 수락
                    joins.append(element)
        if not count:
            others = sorted(self._record_authors(containers))
            hint = (
                f" Authors of tracked changes there: {', '.join(others)}." if who and others else ""
            )
            raise EditError(f"Edit {index}: {where} has no tracked changes to {verb}.{hint}", index)
        removed = 0
        for element in reversed(joins):
            removed += self._join_next(index, element)
        for row in doomed:
            _remove_row(row)
        for name, root in regions:
            self._dirty.add(name)
            body = root.find(_w("body")) if root.tag != _w("body") else root
            if body is not None and root.tag == _w("document"):
                _fix_container_end(body)
        self._paragraphs = None
        note = f"{verb}ed {count} tracked change{'s' if count > 1 else ''} in {where}"
        extra = []
        if len(joins) - removed:
            joined = len(joins) - removed
            extra.append(f"{joined} paragraph{'s' if joined > 1 else ''} joined")
        if removed:
            extra.append(f"{removed} empty paragraph{'s' if removed > 1 else ''} removed")
        if doomed:
            extra.append(f"{len(doomed)} table row{'s' if len(doomed) > 1 else ''} removed")
        if lost:
            names = ", ".join(f"c{c}" for c in sorted(lost, key=int))
            extra.append(f"comments on the removed text deleted: {names}")
        if extra:
            note += f" ({', '.join(extra)})"
        return Change(index, number, count, "", "", kind=edit.op, note=note)

    @staticmethod
    def _record_authors(containers: list[etree._Element]) -> set[str]:
        """추적 기록을 남긴 사람들 (작성자별 수락·거절이 실패할 때 알려 준다)."""
        tags = (*_TRACKED_ALL, _w("rPrChange"), _w("pPrChange"), *_STRUCTURE_RECORDS)
        return {
            (e.get(_w("author")) or "").strip()
            for c in containers
            for e in c.iter(*tags)
            if (e.get(_w("author")) or "").strip()
        }

    def _join_next(self, index: int, element: etree._Element) -> int:
        """문단 기호가 없어진 문단을 다음 문단 앞에 붙인다 (Word 와 같이 다음 문단의 서식을 따른다).

        글자가 남지 않은 문단은 붙이지 않고 지운다 (표 앞이어도 된다). 칸·본문의 마지막 문단이면 빈 문단으로
        남긴다. 지웠으면 1, 아니면 0.
        """
        parent = element.getparent()
        assert parent is not None
        empty = all(c.tag == _w("pPr") or c.tag in _SKIPPABLE_BLOCK for c in element)
        empty = empty or (
            next(element.iter(_w("r")), None) is None
            and all(c.tag in _EMPTY_PARAGRAPH_PARTS for c in element)
        )
        if empty and element.find(f"{_w('pPr')}/{_w('sectPr')}") is None:
            if not _keeps_structure(parent, without=element):
                return 0  # 칸·본문의 마지막 문단: 빈 문단으로 남긴다
            root = element.getroottree().getroot()
            _drop_bookmark_partners(root, element)
            parent.remove(element)
            return 1
        following = element.getnext()
        while following is not None and following.tag in _SKIPPABLE_BLOCK:
            following = following.getnext()
        props = element.find(_w("pPr"))
        if (
            following is None
            or following.tag != _w("p")
            or (props is not None and props.find(_w("sectPr")) is not None)
        ):
            raise EditError(
                f"Edit {index}: a tracked paragraph break at the end of a table cell, section "
                "or document cannot be handled here. Ask the user to handle it in Word.",
                index,
            )
        target_props = following.find(_w("pPr"))
        position = 0 if target_props is None else following.index(target_props) + 1
        for child in list(element):
            if child.tag == _w("pPr"):
                continue
            following.insert(position, child)
            position += 1
        parent.remove(element)
        return 0

    def _check_untracked(self, index: int, paragraph: Paragraph) -> None:
        if paragraph.tracked:
            raise EditError(
                f"Edit {index}: paragraph p{self._number(paragraph)} has tracked changes, so this "
                'edit cannot be made there. Accept or reject them first (op "accept_changes" or '
                f'"reject_changes" with paragraph "p{self._number(paragraph)}"), if the user agrees.',
                index,
            )

    def _number(self, paragraph: Paragraph) -> int:
        """문단의 (호출을 시작할 때의) 번호. 이번 호출에서 새로 생긴 문단이면 지금 번호."""
        mark = paragraph.element.get(_MARK)
        return int(mark) if mark is not None else paragraph.number

    def _not_found(
        self, index: int, old: str, targets: list[Paragraph], ref: str | None
    ) -> EditError:
        where = f"paragraph {ref}" if ref is not None else "the document"
        message = f"Edit {index}: old_text was not found in {where}."
        joined = "\n".join(p.text for p in targets)
        if ref is None and "\n" in old and old in joined:
            message += (
                " It spans more than one paragraph; edit each paragraph separately, or "
                'merge them first with op "merge".'
            )
        elif _PARAGRAPH_PREFIX.match(old):
            message += " Do not include paragraph number prefixes such as [p12] in old_text."
        elif _TRACK_MARKS.search(old):
            message += (
                " Do not copy the tracked change marks {+ +} and {- -}: old_text is searched in "
                "the text with tracked changes accepted (inserted text kept without marks, "
                "deleted text left out)."
            )
        else:
            message += (
                " Copy it exactly from the view, including spaces and punctuation, "
                "and keep it inside one paragraph."
            )
        return EditError(message, index)

    # --- 저장 ------------------------------------------------------------------------------------

    def to_bytes(self) -> bytes:
        """고친 XML 부분만 다시 쓰고 나머지 항목은 내용 그대로 복사한 DOCX 파일 내용.

        ZIP 항목의 순서·이름·날짜·압축 방식은 원본을 따른다.

        Returns:
            저장할 DOCX 파일 내용.
        """
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            for info, data in self._entries:
                if info.filename in self._dirty:
                    data = _serialize(self._roots[info.filename], data)
                zf.writestr(info, data, compress_type=info.compress_type)
            for name, root in self._roots.items():  # 이번에 새로 만든 부분 (예: 메모)
                if name not in self._data:
                    data = _serialize(
                        root, b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                    )
                    zf.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
        return buffer.getvalue()


_RUN_PROP_ORDER = [
    "rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps", "strike", "dstrike",
    "outline", "shadow", "emboss", "imprint", "noProof", "snapToGrid", "vanish", "webHidden",
    "color", "spacing", "w", "kern", "position", "sz", "szCs", "highlight", "u", "effect",
    "bdr", "shd", "fitText", "vertAlign", "rtl", "cs", "em", "lang", "eastAsianLayout",
    "specVanish", "oMath",
]  # fmt: skip
"""``w:rPr`` 자식의 스키마 순서. 이 순서를 어기면 Word 가 파일을 고쳐야 한다고 한다."""

_COLORS = {
    "black": "000000", "검정": "000000", "검은색": "000000",
    "white": "FFFFFF", "흰색": "FFFFFF", "하양": "FFFFFF",
    "red": "FF0000", "빨강": "FF0000", "빨간색": "FF0000",
    "dark red": "C00000", "진한 빨강": "C00000",
    "orange": "FFC000", "주황": "FFC000", "주황색": "FFC000",
    "yellow": "FFFF00", "노랑": "FFFF00", "노란색": "FFFF00",
    "green": "00B050", "초록": "00B050", "초록색": "00B050",
    "blue": "0070C0", "파랑": "0070C0", "파란색": "0070C0",
    "dark blue": "002060", "남색": "002060",
    "purple": "7030A0", "보라": "7030A0", "보라색": "7030A0",
    "gray": "808080", "grey": "808080", "회색": "808080",
}  # fmt: skip
"""글자색 이름 → RGB. Word 의 "표준 색" 과 같은 값."""

_HIGHLIGHTS = {
    name.lower(): name
    for name in (
        "black", "blue", "cyan", "green", "magenta", "red", "yellow", "white", "darkBlue",
        "darkCyan", "darkGreen", "darkMagenta", "darkRed", "darkYellow", "darkGray", "lightGray",
    )
} | {
    "노랑": "yellow", "노란색": "yellow", "초록": "green", "초록색": "green", "연두": "green",
    "하늘": "cyan", "하늘색": "cyan", "파랑": "blue", "파란색": "blue", "분홍": "magenta",
    "분홍색": "magenta", "빨강": "red", "빨간색": "red", "회색": "lightGray", "gray": "lightGray",
    "grey": "lightGray",
}  # fmt: skip
"""형광펜 이름 → Word 형광펜 값 (``ST_HighlightColor``). Word 형광펜은 이 색들만 된다."""

_HEX = re.compile(r"#?([0-9a-fA-F]{6})")

_PropChange = tuple[tuple[str, ...], dict[str, str] | None, dict[str, str]]
"""서식 항목 하나: ``(태그들, 넣을 속성 또는 None(빼기), 스타일 서식을 끄는 속성)``."""

_OFF = {"w:val": "0"}


def _format_changes(index: int, wanted: TextFormat) -> list[_PropChange]:
    """``TextFormat`` 을 ``w:rPr`` 항목 변경으로 바꾼다. 알 수 없는 색 이름은 거절한다."""
    changes: list[_PropChange] = []
    toggles = (("bold", ("b", "bCs")), ("italic", ("i", "iCs")), ("strike", ("strike", "dstrike")))
    for name, tags in toggles:
        value = getattr(wanted, name)
        if value is True:
            changes.append((tags[:2] if name != "strike" else ("strike",), {}, _OFF))
            if name == "strike":
                changes.append((("dstrike",), None, _OFF))
        elif value is False:
            changes.append((tags, None, _OFF))
    if wanted.underline is True:
        changes.append((("u",), {"w:val": "single"}, {"w:val": "none"}))
    elif wanted.underline is False:
        changes.append((("u",), None, {"w:val": "none"}))
    if wanted.color is not None:
        name = wanted.color.strip()
        if name.lower() in ("auto", "default", "none", "기본", "기본색", "자동"):
            changes.append((("color",), None, {"w:val": "auto"}))
        else:
            match = _HEX.fullmatch(name)
            rgb = match.group(1).upper() if match else _COLORS.get(name.lower())
            if rgb is None:
                raise EditError(
                    f"Edit {index}: unknown color {wanted.color!r}. Use a color name such as "
                    'red, blue or 빨강, a code such as "#C00000", or "auto" for the default color.',
                    index,
                )
            changes.append((("color",), {"w:val": rgb}, {"w:val": "auto"}))
    if wanted.highlight is not None:
        name = wanted.highlight.strip()
        if name.lower() in ("none", "no", "off", "없음", "빼기"):
            changes.append((("highlight",), None, {"w:val": "none"}))
        else:
            value = _HIGHLIGHTS.get(name.lower()) or _HIGHLIGHTS.get(name.replace(" ", "").lower())
            if value is None:
                raise EditError(
                    f"Edit {index}: unknown highlight {wanted.highlight!r}. Word highlights only "
                    "come in these colors: yellow, green, cyan, magenta, blue, red, darkBlue, "
                    "darkCyan, darkGreen, darkMagenta, darkRed, darkYellow, darkGray, lightGray, "
                    'black, white (or "none" to remove).',
                    index,
                )
            changes.append((("highlight",), {"w:val": value}, {"w:val": "none"}))
    return changes


def _describe_format(wanted: TextFormat) -> str:
    """결과에 적을 서식 설명 (영어, 예: ``"bold, not italic, color red"``)."""
    parts = []
    for name in ("bold", "italic", "underline", "strike"):
        value = getattr(wanted, name)
        label = {"underline": "underlined", "strike": "strikethrough"}.get(name, name)
        if value is True:
            parts.append(label)
        elif value is False:
            parts.append(f"not {label}")
    if wanted.color is not None:
        parts.append(f"color {wanted.color.strip()}")
    if wanted.highlight is not None:
        name = wanted.highlight.strip()
        parts.append("no highlight" if name.lower() in ("none", "없음") else f"highlight {name}")
    return ", ".join(parts)


def _attr(name: str) -> str:
    return _w(name.removeprefix("w:"))


def _set_prop(props: etree._Element, tag: str, attrs: dict[str, str]) -> None:
    """``w:rPr`` 에 ``tag`` 요소를 (있으면 바꿔) 스키마 순서에 맞는 자리에 둔다."""
    for old in props.findall(_w(tag)):
        props.remove(old)
    element = etree.Element(_w(tag))
    for key, value in attrs.items():
        element.set(_attr(key), value)
    rank = _RUN_PROP_ORDER.index(tag)
    for position, child in enumerate(props):
        name = etree.QName(child).localname if isinstance(child.tag, str) else ""
        child_rank = (
            _RUN_PROP_ORDER.index(name)
            if etree.QName(child).namespace == W and name in _RUN_PROP_ORDER
            else len(_RUN_PROP_ORDER)
        )
        if child_rank > rank:
            props.insert(position, element)
            return
    props.append(element)


def _prop_on(tag: str, element: etree._Element) -> bool:
    """서식 요소가 켜진 상태인지 (``w:b`` 는 ``val`` 이 없거나 참, ``w:u``·``w:highlight`` 는 ``none`` 이 아님)."""
    value = element.get(_w("val"))
    if tag in ("u", "highlight"):
        return value not in (None, "none")
    if tag == "color":
        return value not in (None, "auto")
    return value not in ("0", "false", "off")


def _main_prop(props: etree._Element, tag: str) -> tuple[tuple[str, str], ...] | None:
    """``w:rPr`` 안 ``tag`` 요소의 속성 (없으면 ``None``). 서식이 바뀌었는지 비교할 때 쓴다."""
    element = props.find(_w(tag))
    return tuple(sorted(element.attrib.items())) if element is not None else None


def _find_all(text: str, value: str) -> list[int]:
    """``value`` 가 나오는 모든 위치 (겹치는 것 포함)."""
    found = []
    start = text.find(value)
    while start >= 0:
        found.append(start)
        start = text.find(value, start + 1)
    return found


_ID = re.compile(rb'w:id="(\d+)"')
_UNTRACKABLE_OPS = {"insert_column", "delete_column"}
"""추적 모드에서 추적으로 남길 수 없는 수정 종류 (Word 도 열 넣기·지우기를 행 기록처럼 남기지 못한다)."""


def _diff_ops(old: str, new: str, span: list[_Glyph]) -> list[tuple[str, int, int, int, int]]:
    """``old`` → ``new`` 로 바꿀 때 고칠 부분 (``SequenceMatcher`` opcodes, 같은 부분 제외).

    많이 다르면 통째로 바꾼다 (글자 조각마다 서식이 흩어지지 않게). 다만 서식이 다른 조각(굵은 말, 링크
    글자 등)이 새 글자에 그대로 한 번 나오면 그 조각은 "같음" 으로 남겨 서식을 지킨다. 고칠 수 없는
    글자가 있으면 그 글자를 지키도록 글자 단위로 비교한다.
    """
    matcher = SequenceMatcher(None, old, new, autojunk=False)
    if matcher.ratio() < WHOLE_REPLACE_RATIO and not any(g.wall for g in span):
        anchors, _ = _format_anchors(old, new, span)
        ops: list[tuple[str, int, int, int, int]] = []
        i = j = 0
        for a, b, c, d in [*anchors, (len(old), len(old), len(new), len(new))]:
            if a > i or c > j:
                ops.append(("replace", i, a, j, c))
            i, j = b, d
        return ops
    return [op for op in matcher.get_opcodes() if op[0] != "equal"]


_ANCHOR_MIN = 2
"""고쳐 쓰기에서 서식을 지킬 조각의 최소 글자 수 (공백 제외). 짧으면 우연히 같은 글자일 수 있다."""


def _format_key(glyph: _Glyph) -> tuple[bytes, int]:
    """글자의 서식 묶음 (run 서식, 링크·콘텐츠 컨트롤)."""
    props = glyph.run.find(_w("rPr")) if glyph.run is not None else None
    if props is None:
        return b"", id(glyph.scope)
    copied = _without_tracking(deepcopy(props))
    return etree.tostring(copied, method="c14n"), id(glyph.scope)


def _format_anchors(
    old: str, new: str, span: list[_Glyph]
) -> tuple[list[tuple[int, int, int, int]], int]:
    """통째로 바꿀 때 서식을 지킬 조각 ``(old 시작, 끝, new 시작, 끝)`` 과, 지키지 못한 조각 수.

    첫 글자와 서식이 다른 조각만 본다 (통째로 바꾸면 새 글자는 첫 글자의 서식을 따르므로). 조각 글자(앞뒤
    공백 제외)가 새 글자에 정확히 한 번 나오고 순서가 앞 조각 뒤면 지킨다.
    """
    if not span:
        return [], 0
    pieces: list[tuple[tuple[bytes, int], int, int]] = []  # (서식, 시작, 끝)
    for position, glyph in enumerate(span):
        key = _format_key(glyph)
        if pieces and pieces[-1][0] == key:
            pieces[-1] = (key, pieces[-1][1], position + 1)
        else:
            pieces.append((key, position, position + 1))
    base = pieces[0][0]
    anchors: list[tuple[int, int, int, int]] = []
    lost = 0
    after = 0
    for key, start, end in pieces:
        if key == base:
            continue
        text = old[start:end]
        core = text.strip()
        if len(core.replace(" ", "")) < _ANCHOR_MIN:
            continue
        begin = start + text.index(core)
        found = new.find(core)
        if found < 0 or new.find(core, found + 1) >= 0 or found < after:
            lost += 1
            continue
        anchors.append((begin, begin + len(core), found, found + len(core)))
        after = found + len(core)
    return anchors, lost


def lost_formatting(old: str, new: str, span: list[_Glyph]) -> int:
    """통째로 바꾸는 고쳐 쓰기에서 서식을 지키지 못한 조각 수 (결과에 알리려고). 아니면 0."""
    matcher = SequenceMatcher(None, old, new, autojunk=False)
    if matcher.ratio() >= WHOLE_REPLACE_RATIO or any(g.wall for g in span):
        return 0
    return _format_anchors(old, new, span)[1]


def _check_change(index: int, number: int, removed: list[_Glyph], text: str) -> None:
    """글자 바꾸기 한 조각을 적용할 수 있는지 (벽 글자, 링크 경계, 새 벽 글자)."""
    if WALL in text:
        raise EditError(
            f"Edit {index}: new_text contains {WALL}, which stands for an image or other object. "
            "Objects cannot be added.",
            index,
        )
    if any(g.wall for g in removed):
        raise EditError(
            f"Edit {index}: the text to change in paragraph p{number} touches an image, field, "
            f"footnote mark or symbol (shown as {WALL} or a field value) that cannot be edited. "
            "Narrow old_text so that it changes only plain text around it.",
            index,
        )
    if len({id(g.scope) for g in removed}) > 1:
        raise EditError(
            f"Edit {index}: the text to change in paragraph p{number} crosses the edge of a "
            "hyperlink or content control. Change the text inside and outside it separately.",
            index,
        )


def _insert_anchor(
    glyphs: list[_Glyph], begin: int, match: tuple[int, int], own: str
) -> tuple[_Glyph | None, bool]:
    """넣을 글자가 서식을 따를 글자와, 그 글자 뒤(``True``)에 넣을지 앞에 넣을지.

    ``old_text`` 안의 바로 앞 글자, 바로 뒤 글자, 밖의 바로 앞, 바로 뒤 순서로 본다. 고칠 수 없는 글자와
    남의 추적 글자는 따르지 않는다.
    """
    low, high = match
    candidates = [
        (begin - 1, True, begin - 1 >= low),
        (begin, False, begin < high),
        (begin - 1, True, True),
        (begin, False, True),
    ]
    for position, after, allowed in candidates:
        if not allowed or not 0 <= position < len(glyphs):
            continue
        glyph = glyphs[position]
        if not glyph.wall and glyph.run is not None and glyph.tracked in (None, own):
            return glyph, after
    return None, False


def _new_run(props: etree._Element | None, tokens: list[_Token]) -> etree._Element:
    """서식(추적 기록은 뺀 사본)과 토큰으로 새 run 을 만든다."""
    run = etree.Element(_w("r"))
    if props is not None:
        run.append(_without_tracking(deepcopy(props)))
    _write_run(run, tokens)
    return run


def _split_run(run: etree._Element, at: int) -> etree._Element | None:
    """Run 을 토큰 ``at`` 앞에서 둘로 나눈다. 원래 요소가 앞쪽을 갖고, 새로 만든 뒤쪽 run 을 돌려준다.

    ``at`` 이 처음이나 끝이면 나누지 않고 ``None``. 앞쪽 토큰의 위치가 그대로라서 앞 글자를 가리키는
    ``_Glyph`` 는 계속 맞다.
    """
    tokens = _tokens(run)
    if at <= 0 or at >= len(tokens):
        return None
    props = run.find(_w("rPr"))
    right = etree.Element(_w("r"))
    if props is not None:
        right.append(deepcopy(props))
    _write_run(right, tokens[at:])
    run.addnext(right)
    _write_run(run, tokens[:at])
    return right


def _cut_run(run: etree._Element, low: int, high: int) -> etree._Element:
    """Run 의 토큰 ``[low, high)`` 만 든 run 을 떼어 낸다 (앞·뒤는 따로 남는다)."""
    _split_run(run, high)
    middle = _split_run(run, low)
    return middle if middle is not None else run


def _record_author(run: etree._Element) -> str | None:
    """Run 을 감싼 넣기 기록(``w:ins``·``w:moveTo``)의 작성자. 감싸지 않았으면 ``None``."""
    parent = run.getparent()
    while parent is not None and parent.tag != _w("p"):
        if parent.tag in _TRACKED_SHOWN:
            return parent.get(_w("author")) or "?"
        parent = parent.getparent()
    return None


def _inside_record(run: etree._Element, stop: etree._Element) -> bool:
    """Run 이 ``stop`` 안에서 추적 기록(``w:ins``·``w:del`` 등)에 감싸여 있는지."""
    for ancestor in run.iterancestors():
        if ancestor is stop:
            return False
        if ancestor.tag in _TRACKED_ALL:
            return True
    return False


def _strip_copy(block: etree._Element) -> None:
    """옮길 블록의 사본에서 원본과 겹치면 안 되는 표시(문단 번호 표시, 문단 id, 책갈피)를 뺀다."""
    for element in list(block.iter()):
        if not isinstance(element.tag, str):
            continue
        for name in (_MARK, _PARA_ID, f"{{{W14}}}textId"):
            element.attrib.pop(name, None)
        if element.tag in (_w("bookmarkStart"), _w("bookmarkEnd")):
            parent = element.getparent()
            if parent is not None:
                parent.remove(element)
    for name in (_MARK, _PARA_ID, f"{{{W14}}}textId"):
        block.attrib.pop(name, None)


def _closest_paragraph(element: etree._Element) -> etree._Element | None:
    """요소를 감싼 가장 가까운 문단 (글상자 안 문단과 구분하려고)."""
    return next((a for a in element.iterancestors(_w("p"))), None)


def _drop_empty_records(paragraph: etree._Element) -> None:
    """글자를 모두 지워 비게 된 추적 기록(``w:ins`` 등)을 지운다."""
    for record in list(paragraph.iter(*_TRACKED_SHOWN, *_TRACKED_HIDDEN)):
        parent = record.getparent()
        if parent is not None and parent.tag != _w("rPr") and len(record) == 0:
            parent.remove(record)


_TRACKED_ALL = _TRACKED_SHOWN | _TRACKED_HIDDEN
_UNSUPPORTED_TRACKING = {
    _w(n)
    for n in (
        "cellIns",
        "cellDel",
        "cellMerge",
        "numberingChange",
        "customXmlInsRangeStart",
        "customXmlDelRangeStart",
        "customXmlMoveFromRangeStart",
        "customXmlMoveToRangeStart",
    )
}
"""수락·거절을 다루지 않는 추적 기록 (칸 단위 기록 등). ``numberingChange`` 는 수락만 한다."""
_EMPTY_PARAGRAPH_PARTS = {
    _w(n) for n in ("pPr", "hyperlink", "smartTag", "sdt", "customXml")
} | _SKIPPABLE_BLOCK
"""글자(run)가 없을 때 문단을 빈 문단으로 볼 수 있는 자식 요소."""
_HEADER_FOOTER_REFS = frozenset({_w("headerReference"), _w("footerReference")})
_PROP_CHANGE_KEEP: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    _w("tblPrChange"): (frozenset(), frozenset()),
    _w("tblPrExChange"): (frozenset(), frozenset()),
    _w("tblGridChange"): (frozenset(), frozenset()),
    _w("trPrChange"): (frozenset({_w("ins"), _w("del")}), frozenset()),
    _w("tcPrChange"): (frozenset({_w("cellIns"), _w("cellDel"), _w("cellMerge")}), frozenset()),
    _w("sectPrChange"): (_HEADER_FOOTER_REFS, _HEADER_FOOTER_REFS),
}
"""표·행·칸·구역 속성 변경 기록 → 거절할 때 ``(옛 속성에 없어 그대로 둘 태그, 그 가운데 앞에 둘 태그)``."""
_STRUCTURE_RECORDS = (*_PROP_CHANGE_KEEP, _w("ins"), _w("del"))
"""구조 기록을 찾을 태그. ``ins``·``del`` 은 ``w:trPr`` 안의 것(행 넣음·지움)만 구조 기록이다."""


def _structure_record(element: etree._Element) -> bool:
    """표·행·칸·구역의 구조 기록인지 (속성 변경 기록, 또는 ``w:trPr`` 안의 행 넣음·지움)."""
    if element.tag in _PROP_CHANGE_KEEP:
        return True
    parent = element.getparent()
    return parent is not None and parent.tag == _w("trPr")


def _table_props_around(paragraph: etree._Element) -> list[etree._Element]:
    """문단이 든 행·칸·표의 속성 요소 (구조 기록을 찾을 곳). 표 밖이면 빈 목록."""
    cell = next(paragraph.iterancestors(_w("tc")), None)
    row = cell.getparent() if cell is not None else None
    table = row.getparent() if row is not None else None
    if cell is None or row is None or table is None:
        return []
    found = [cell.find(_w("tcPr")), row.find(_w("trPr")), row.find(_w("tblPrEx"))]
    found += [table.find(_w("tblPr")), table.find(_w("tblGrid"))]
    return [e for e in found if e is not None]


def _remove_table(table: etree._Element) -> None:
    """표를 지운다. 칸의 유일한 내용이거나 지우면 앞뒤 표가 붙는 자리면 빈 문단을 남긴다."""
    parent = table.getparent()
    assert parent is not None
    previous, following = table.getprevious(), table.getnext()
    if not _keeps_structure(parent, without=table) or (
        previous is not None
        and following is not None
        and previous.tag == _w("tbl")
        and following.tag == _w("tbl")
    ):
        table.addprevious(etree.Element(_w("p")))
    parent.remove(table)


def _remove_row(row: etree._Element) -> None:
    """표 행을 지운다 (세로 병합 정리). 마지막 행이면 표를 통째로 지운다."""
    table = row.getparent()
    if table is None:
        return
    _unmerge_row(row)
    table.remove(row)
    if table.find(_w("tr")) is None:
        _remove_table(table)


_MOVE_RANGES = {
    _w(n) for n in ("moveFromRangeStart", "moveFromRangeEnd", "moveToRangeStart", "moveToRangeEnd")
}


def _unwrap(element: etree._Element) -> None:
    """요소를 벗기고 안의 내용을 그 자리에 둔다."""
    parent = element.getparent()
    if parent is None:
        return
    position = parent.index(element)
    for child in list(element):
        parent.insert(position, child)
        position += 1
    parent.remove(element)


def _review_content(paragraph: etree._Element, accept: bool, who: str | None = None) -> int:
    """문단 안 글자·서식의 추적 기록을 수락(``accept``)하거나 거절한다. 다룬 기록 수.

    ``who`` 를 주면 그 사람이 남긴 기록만 다룬다. 다른 사람의 기록이 안에 든 넣은 글자를 거절하면 안쪽
    기록도 함께 사라진다 (Word 와 같다). 문단 기호의 기록(``w:pPr/w:rPr/w:ins`` 등)은 다루지 않는다
    (문단을 합쳐야 해서 호출한 쪽이 한다).
    """
    count = 0
    for record in list(paragraph.iter(*_TRACKED_ALL)):
        parent = record.getparent()
        if parent is None or parent.tag in (_w("rPr"), _w("trPr")):
            continue
        if not _within(record, paragraph) or not _by(record, who):
            continue  # 앞에서 바깥 기록째 지워졌거나, 다른 사람의 기록
        count += 1
        shown = record.tag in _TRACKED_SHOWN
        if shown != accept:  # 넣은 글자 거절, 지운 글자 수락: 글자째 없앤다
            parent.remove(record)
            continue
        if not shown:  # 지운 글자 거절: 되살린다
            for node in record.iter(_w("delText"), _w("delInstrText")):
                node.tag = _w("t") if node.tag == _w("delText") else _w("instrText")
        _unwrap(record)
    for change in list(paragraph.iter(_w("rPrChange"))):
        if not _within(change, paragraph) or not _by(change, who):
            continue
        count += 1
        props = change.getparent()
        assert props is not None
        if not accept:
            old = change.find(_w("rPr"))
            keep = [c for c in props if c.tag in _TRACKED_ALL]
            for child in list(props):
                props.remove(child)
            for child in keep:
                props.append(child)
            for child in old if old is not None else []:
                props.append(deepcopy(child))
        else:
            props.remove(change)
    for change in list(paragraph.iter(_w("pPrChange"))):
        if not _within(change, paragraph) or not _by(change, who):
            continue
        count += 1
        props = change.getparent()
        assert props is not None
        if not accept:
            old = change.find(_w("pPr"))
            keep = [c for c in props if c.tag in (_w("rPr"), _w("sectPr"))]
            for child in list(props):
                props.remove(child)
            for child in old if old is not None else []:
                props.append(deepcopy(child))
            for child in keep:
                props.append(child)
        else:
            props.remove(change)
    for wrapper in list(paragraph.iter(_w("hyperlink"), _w("smartTag"))):
        holder = wrapper.getparent()
        if holder is not None and len(wrapper) == 0:
            holder.remove(wrapper)  # 안의 글자를 모두 지워 빈 링크가 남지 않게
    if who is None or not any(paragraph.iter(*_TRACKED_ALL)):
        for marker in list(paragraph.iter(*_MOVE_RANGES)):
            parent = marker.getparent()
            if parent is not None:
                parent.remove(marker)
    return count


def _by(record: etree._Element, who: str | None) -> bool:
    """추적 기록이 ``who`` 가 남긴 것인지 (``who`` 가 없으면 늘 ``True``). 앞뒤 공백은 무시한다."""
    return who is None or (record.get(_w("author")) or "").strip() == who.strip()


def _restore_props(
    change: etree._Element, keep: frozenset[str], first: frozenset[str] = frozenset()
) -> None:
    """속성 변경 기록(``w:tblPrChange`` 등)을 거절한다: 부모 속성을 기록 안의 옛 속성으로 바꾼다.

    Args:
        change: 속성 변경 기록. 안에 옛 속성 요소 하나가 든다.
        keep: 옛 속성에 없어서 지금 것을 그대로 둘 요소 태그 (예: 행 넣음·지움 기록).
        first: ``keep`` 가운데 앞에 둘 태그 (구역의 머리글·바닥글 참조).
    """
    props = change.getparent()
    assert props is not None
    old = next(iter(change), None)
    kept_first = [c for c in props if c.tag in first]
    kept = [c for c in props if c.tag in keep and c.tag not in first]
    for child in list(props):
        props.remove(child)
    for child in kept_first:
        props.append(child)
    for child in old if old is not None else []:
        props.append(deepcopy(child))
    for child in kept:
        props.append(child)


class _ParagraphEditor:
    """문단 하나의 글자를 run 토큰 단위로 고친다.

    한 문단 안의 여러 바꾸기는 **뒤에서부터** 적용해야 한다 (앞쪽 글자의 토큰 위치가 그대로 남도록).
    ``commit`` 이 바뀐 run 만 XML 로 다시 쓴다.
    """

    def __init__(self, paragraph: Paragraph, index: int, own: str = DEFAULT_AUTHOR) -> None:
        """문단을 고칠 준비를 한다.

        Args:
            paragraph: 고칠 문단.
            index: 수정 순서 (오류 메시지용).
            own: 우리(에이전트)의 추적 기록 작성자. 넣을 글자는 남의 추적 글자의 서식을 따르지 않는다.
        """
        self._own = own
        self._paragraph = paragraph
        self._glyphs = paragraph.glyphs
        self._index = index
        self._runs: dict[etree._Element, list[_Token]] = {}

    def _error(self, message: str) -> EditError:
        return EditError(f"Edit {self._index}: {message}", self._index)

    def _tokens(self, run: etree._Element) -> list[_Token]:
        if run not in self._runs:
            self._runs[run] = _tokens(run)
        return self._runs[run]

    def replace(self, start: int, old: str, new: str) -> None:
        """``start`` 에서 시작하는 ``old`` 를 ``new`` 로 바꾼다. 달라진 부분만 고친다.

        Args:
            start: 문단 보기 글자에서 ``old`` 가 시작하는 위치.
            old: 바꿀 글자 (문단 보기 글자와 같다).
            new: 바꿔 넣을 글자.

        Raises:
            EditError: 고칠 수 없는 요소나 링크 경계에 걸리거나, ``￼`` 를 새로 넣을 때.
        """
        ops = _diff_ops(old, new, self._glyphs[start : start + len(old)])
        for _, i1, i2, j1, j2 in reversed(ops):
            self._change(start + i1, start + i2, new[j1:j2], (start, start + len(old)))

    def insert_end(self, text: str) -> None:
        """문단 끝에 글자를 넣는다. 마지막 글자의 서식을 따른다.

        Raises:
            EditError: 따를 글자가 없을 때 (빈 문단이거나 끝이 그림 등).
        """
        size = len(self._glyphs)
        self._change(size, size, text, (0, size))

    def _change(self, begin: int, end: int, text: str, match: tuple[int, int]) -> None:
        """보기 글자 ``[begin, end)`` 를 ``text`` 로 바꾼다 (``begin == end`` 면 넣기).

        ``match`` 는 ``old_text`` 가 차지한 범위로, 넣을 글자의 서식을 고를 때 그 안의 글자를 먼저 본다.
        """
        if WALL in text:
            raise self._error(
                f"new_text contains {WALL}, which stands for an image or other object. "
                "Objects cannot be added."
            )
        removed = self._glyphs[begin:end]
        if any(g.wall for g in removed):
            raise self._error(
                f"the text to change in paragraph p{self._paragraph.number} touches an image, "
                f"field, footnote mark or symbol (shown as {WALL} or a field value) that cannot "
                "be edited. Narrow old_text so that it changes only plain text around it."
            )
        scopes = {id(g.scope) for g in removed}
        if len(scopes) > 1:
            raise self._error(
                f"the text to change in paragraph p{self._paragraph.number} crosses the edge of "
                "a hyperlink or content control. Change the text inside and outside it separately."
            )
        anchor = self._anchor(begin, end, match)
        if anchor is None:
            raise self._error(
                f"there is no plain text next to this position in paragraph "
                f"p{self._paragraph.number} to take formatting from."
            )
        run, position = anchor
        assert removed or text  # opcodes 는 빈 변경을 내지 않는다
        drop: dict[etree._Element, set[int]] = {}
        for glyph in removed:
            assert glyph.run is not None
            drop.setdefault(glyph.run, set()).add(glyph.index)
        inserted = _text_tokens(text)
        for target in {run, *drop}:
            tokens = self._tokens(target)
            gone = drop.get(target, set())
            rebuilt: list[_Token] = []
            for i, token in enumerate(tokens):
                if target is run and i == position:
                    rebuilt.extend(inserted)
                if i not in gone:
                    rebuilt.append(token)
            if target is run and position >= len(tokens):
                rebuilt.extend(inserted)
            self._runs[target] = rebuilt

    def _anchor(
        self, begin: int, end: int, match: tuple[int, int]
    ) -> tuple[etree._Element, int] | None:
        """새 글자를 넣을 ``(run, 토큰 위치)``. 넣은 글자는 그 run 의 서식을 따른다.

        바꾸기면 바뀌는 첫 글자의 run 에 넣는다. 넣기면 **``old_text`` 안의** 이웃 글자를 먼저 따른다
        (바로 앞 글자, 없으면 바로 뒤 글자). 그래야 링크 글자 맨 앞에 넣은 글자가 링크 밖으로 새지 않는다.
        ``old_text`` 안에 따를 글자가 없으면 밖의 바로 앞, 바로 뒤 글자 순서로 본다. 고칠 수 없는 글자는
        따르지 않는다.
        """
        if begin < end:
            first = self._glyphs[begin]
            assert first.run is not None
            return first.run, first.index
        low, high = match
        candidates = [
            (begin - 1, 1, begin - 1 >= low),
            (begin, 0, begin < high),
            (begin - 1, 1, True),
            (begin, 0, True),
        ]
        for position, offset, allowed in candidates:
            if not allowed or not 0 <= position < len(self._glyphs):
                continue
            glyph = self._glyphs[position]
            if not glyph.wall and glyph.run is not None and glyph.tracked in (None, self._own):
                return glyph.run, glyph.index + offset
        return None

    def commit(self) -> None:
        """바뀐 run 을 XML 로 다시 쓴다. 비게 된 run 은 지운다."""
        for run, tokens in self._runs.items():
            _write_run(run, tokens)


def _level_formats(parent: etree._Element) -> dict[int, str]:
    """``w:lvl`` 들의 수준(0부터) → 번호 형식."""
    levels = {}
    for level in parent.iterchildren(_w("lvl")):
        fmt = level.find(_w("numFmt"))
        if fmt is not None:
            levels[_int_val_attr(level, "ilvl")] = fmt.get(_w("val"), "")
    return levels


def _list_kind(paragraph: etree._Element, formats: dict[str, dict[int, str]]) -> str | None:
    """목록 문단의 종류 (``"번호"`` | ``"글머리표"``). 목록이 아니거나 정의를 알 수 없으면 ``None``."""
    level = _list_level(paragraph)
    if level is None:
        return None
    num = paragraph.find(f"{_w('pPr')}/{_w('numPr')}/{_w('numId')}")
    fmt = formats.get(num.get(_w("val"), "") if num is not None else "", {}).get(level - 1)
    if not fmt or fmt == "none":
        return None
    return "글머리표" if fmt == "bullet" else "번호"


def _list_level(paragraph: etree._Element) -> int | None:
    """문단 서식의 목록 번호(``w:numPr``)로 본 목록 수준 (1부터). ``numId`` 0 은 목록을 끈 것이다."""
    numbering = paragraph.find(f"{_w('pPr')}/{_w('numPr')}")
    if numbering is None:
        return None
    num_id = numbering.find(_w("numId"))
    if num_id is None or num_id.get(_w("val"), "0") == "0":
        return None
    level = numbering.find(_w("ilvl"))
    try:
        return int(level.get(_w("val"), "0")) + 1 if level is not None else 1
    except ValueError:
        return 1


def _in_table(paragraph: Paragraph) -> bool:
    return bool(paragraph.location and re.search(r"표\d+ \d+행", paragraph.location))


_COLOR_NAMES = {
    "000000": "검정",
    "FFFFFF": "흰색",
    "FF0000": "빨강",
    "C00000": "진한 빨강",
    "FFC000": "주황",
    "FFFF00": "노랑",
    "00B050": "초록",
    "0070C0": "파랑",
    "002060": "남색",
    "7030A0": "보라",
    "808080": "회색",
}
"""보기에 보여줄 글자색 이름. 목록에 없으면 ``#RRGGBB`` 로 보여준다."""

_HIGHLIGHT_NAMES = {
    "yellow": "노랑",
    "green": "초록",
    "cyan": "하늘",
    "magenta": "분홍",
    "blue": "파랑",
    "red": "빨강",
    "lightGray": "회색",
}
"""보기에 보여줄 형광펜 이름. 목록에 없으면 Word 값 그대로 보여준다."""

_FORMAT_FRAGMENTS = 12
"""문단 하나의 서식 줄에 보여줄 조각 수의 상한."""


def _direct_format(run: etree._Element | None) -> str:
    """Run 의 직접 서식 설명 (예: ``"굵게, 글자색 빨강"``). 없으면 빈 글자."""
    props = run.find(_w("rPr")) if run is not None else None
    if props is None:
        return ""
    labels = []
    for tag, label in (("b", "굵게"), ("i", "기울임"), ("u", "밑줄"), ("strike", "취소선")):
        element = props.find(_w(tag))
        if element is not None and _prop_on(tag, element):
            labels.append(label)
    color = props.find(_w("color"))
    if color is not None and _prop_on("color", color):
        value = (color.get(_w("val")) or "").upper()
        labels.append(f"글자색 {_COLOR_NAMES.get(value, '#' + value)}")
    highlight = props.find(_w("highlight"))
    if highlight is not None and _prop_on("highlight", highlight):
        value = highlight.get(_w("val")) or ""
        labels.append(f"형광펜 {_HIGHLIGHT_NAMES.get(value, value)}")
    return ", ".join(labels)


def _format_line(paragraph: Paragraph) -> str | None:
    """문단의 직접 서식 조각을 한 줄로 (``    (서식: 굵게 '135억' · 글자색 빨강 '주의')``)."""
    fragments: list[tuple[str, str]] = []
    cache: dict[int, str] = {}
    for glyph in paragraph.glyphs:
        key = id(glyph.run)
        if key not in cache:
            cache[key] = _direct_format(glyph.run)
        label = cache[key]
        if fragments and fragments[-1][0] == label:
            fragments[-1] = (label, fragments[-1][1] + glyph.char)
        else:
            fragments.append((label, glyph.char))
    shown = [(label, text.strip()) for label, text in fragments if label and text.strip()]
    if not shown:
        return None
    parts = [f"{label} {_head(text)!r}" for label, text in shown[:_FORMAT_FRAGMENTS]]
    if len(shown) > _FORMAT_FRAGMENTS:
        parts.append(f"외 {len(shown) - _FORMAT_FRAGMENTS}곳")
    return f"    (서식: {' · '.join(parts)})"


def _format_paragraph(paragraph: Paragraph, formatting: bool = False) -> str:
    labels = [x for x in (paragraph.location, paragraph.style) if x]
    if not paragraph.text.strip() and _in_table(paragraph):
        labels.append("위 칸과 병합" if _continued_cell(paragraph.element) is not None else "빈 칸")
    if paragraph.list_level is not None:
        level = paragraph.list_level
        name = f"{paragraph.list_kind} 목록" if paragraph.list_kind else "목록"
        labels.append(name if level == 1 else f"{name} {level}수준")
    if paragraph.tracked:
        who = ", ".join(paragraph.track_authors) or "작성자 모름"
        labels.append(" · ".join([f"변경 추적: {who}", *paragraph.track_notes]))
    prefix = f"[p{paragraph.number}]"
    if labels:
        prefix += " " + " ".join(f"({x})" for x in labels)
    text = paragraph.marked if paragraph.tracked else paragraph.text
    lines = [f"{prefix} {text}".rstrip(" ")]
    if formatting and (line := _format_line(paragraph)):
        lines.append(line)
    for comment in paragraph.comments:
        target = f" → {_head(comment.anchor, _ANCHOR_CHARS)!r}" if comment.anchor else ""
        body = _head(comment.text, _COMMENT_CHARS)
        kind = f"메모 c{comment.parent}의 답글" if comment.parent else "메모"
        done = " · 해결됨" if comment.resolved else ""
        lines.append(
            f"    ({kind} c{comment.id} · {comment.author or '작성자 모름'}: {body!r}{target}{done})"
        )
        for reply in comment.replies:
            text = _head(reply.text, _COMMENT_CHARS)
            lines.append(f"      (↳ 답글 c{reply.id} · {reply.author or '작성자 모름'}: {text!r})")
    return "\n".join(lines)


def _shown(paragraph: Paragraph) -> bool:
    """보기에 보여줄 문단인지. 빈 문단은 번호만 차지하지만, 표 칸·지운 기록·메모가 있으면 보여준다."""
    return bool(
        paragraph.text.strip()
        or _in_table(paragraph)
        or (paragraph.tracked and paragraph.marked.strip())
        or paragraph.comments
    )


# --- 다른 모듈(새 DOCX 만들기)과 같이 쓰는 패키지 도우미 ----------------------------------------------

parse_part = _parse_xml
"""ZIP 항목의 XML 을 안전하게(외부 엔티티 없이) 읽는다."""
serialize_part = _serialize
"""XML 을 원본의 선언을 붙여 쓴다."""
resolve_target = _resolve
"""관계(rels)의 대상 경로를 ZIP 항목 이름으로 바꾼다."""
rels_name = _rels_name
"""부분의 관계 파일 이름 (예: ``word/_rels/document.xml.rels``)."""
