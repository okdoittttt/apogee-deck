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
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Literal

from lxml import etree

from app.documents.editing.text import SNIPPET_CHARS, Change, Edit, EditError

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

WALL = "￼"
"""고칠 수 없는 자리(그림, 각주 번호 등)를 보기에서 나타내는 글자 (OBJECT REPLACEMENT CHARACTER)."""

_PARAGRAPH_PREFIX = re.compile(r"\s*\[p\d+\]", re.IGNORECASE)
"""보기(``view``)가 문단 앞에 붙이는 번호. 에이전트가 ``old_text`` 에 잘못 넣는 경우를 찾는다."""

_PARAGRAPH_REF = re.compile(r"\[?\s*p?(\d+)\s*\]?", re.IGNORECASE)

_FORBIDDEN_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
"""XML 에 넣을 수 없거나 Word 문단에 맞지 않는 제어 문자 (``\t`` 와 ``\n`` 은 따로 다룬다)."""

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

_HEADING = re.compile(r"heading (\d)$", re.IGNORECASE)


def _shorten(value: str) -> str:
    return value if len(value) <= SNIPPET_CHARS else value[: SNIPPET_CHARS - 1] + "…"


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
            tokens.append(_Token("br", "\n", child))
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
        else:  # 새로 넣은 탭
            etree.SubElement(run, _w("tab"))
    flush()


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
    """

    char: str
    run: etree._Element | None
    index: int
    wall: bool
    scope: etree._Element | None


@dataclass
class Paragraph:
    r"""고칠 수 있는 단위인 문단 하나.

    Attributes:
        number: 문단 번호 (1부터, 본문 → 머리글 → 바닥글 순서). 보기의 ``[p12]`` 와 같다.
        location: 위치 표시 (예: ``"표1 2행 1열"``, ``"머리글 1"``). 본문의 일반 문단이면 ``None``.
        style: 제목 스타일 표시 (예: ``"제목 1"``). 제목이 아니면 ``None``.
        text: 보기 글자 (탭 ``\t``, 줄바꿈 ``\n``, 고칠 수 없는 자리 ``￼``).
        tracked: 변경 내용 추적 기록이 있는지 (있으면 고칠 수 없다).
        element: 문단 XML 요소 (``w:p``).
        part: 문단이 든 XML 부분 이름 (예: ``"word/document.xml"``).
    """

    number: int
    location: str | None
    style: str | None
    text: str
    tracked: bool
    element: etree._Element
    part: str
    glyphs: list[_Glyph] = field(repr=False, default_factory=list)


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


def _flatten(paragraph: etree._Element, fields: _FieldState) -> tuple[list[_Glyph], bool]:
    """문단을 글자 목록으로 펼친다. ``(글자 목록, 변경 추적 기록이 있는지)``."""
    glyphs: list[_Glyph] = []
    tracked = False

    def wall(scope: etree._Element | None, char: str = WALL) -> None:
        glyphs.append(_Glyph(char, None, -1, True, scope))

    def run(element: etree._Element, scope: etree._Element | None, forced_wall: bool) -> None:
        for index, token in enumerate(_tokens(element)):
            if token.kind == "x":
                assert token.element is not None
                tag = token.element.tag
                if tag == _w("fldChar"):
                    fields.update(token.element)
                elif tag == _w("noBreakHyphen"):
                    glyphs.append(_Glyph("-", element, index, True, scope))
                elif tag not in _RUN_SILENT:
                    glyphs.append(_Glyph(WALL, element, index, True, scope))
                continue
            locked = forced_wall or fields.depth > 0
            glyphs.append(_Glyph(token.char, element, index, locked, scope))

    def walk(parent: etree._Element, scope: etree._Element | None, forced_wall: bool) -> None:
        nonlocal tracked
        for child in parent:
            tag = child.tag
            if not isinstance(tag, str) or tag in _PARAGRAPH_SILENT:
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
                tracked = True
                walk(child, scope, forced_wall)
            elif tag in _TRACKED_HIDDEN:
                tracked = True
            else:
                # 수식, mc:AlternateContent, 알 수 없는 요소: 통째로 벽 하나.
                wall(scope)

    walk(paragraph, None, False)
    return glyphs, tracked


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
    """

    text: str
    first: int
    last: int
    total: int


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

    # --- 문단 ------------------------------------------------------------------------------------

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
            for name, label in self._parts:
                fields = _FieldState()
                blocks = self._walk_blocks(self._roots[name], tables, label, label)
                for element, where in blocks:
                    glyphs, tracked = _flatten(element, fields)
                    result.append(
                        Paragraph(
                            number=len(result) + 1,
                            location=where,
                            style=self._style_label(element),
                            text="".join(g.char for g in glyphs),
                            tracked=tracked,
                            element=element,
                            part=name,
                            glyphs=glyphs,
                        )
                    )
            self._paragraphs = result
        return self._paragraphs

    def view(self, first: int, limit: int, max_chars: int | None = None) -> ParagraphView:
        """문단에 번호를 붙여 일부를 보여준다 (``[p12] (표1 1행 1열) 글자``).

        빈 문단은 번호만 차지하고 보여주지 않는다. ``max_chars`` 를 주면 결과가 그 글자 수를 넘기 전에
        멈춘다. 그래도 첫 문단은 길어도 보여준다.

        Args:
            first: 보여줄 첫 문단 번호 (1부터). 1보다 작으면 1로 본다.
            limit: 보여줄 최대 문단 수 (1 이상, 빈 문단은 세지 않는다).
            max_chars: 결과 글자 수의 상한. ``None`` 이면 문단 수로만 자른다.

        Returns:
            번호를 붙인 문단들과 범위, 전체 문단 수.
        """
        paragraphs = self.paragraphs
        first = max(first, 1)
        shown: list[str] = []
        last = first - 1
        size = 0
        for paragraph in paragraphs[first - 1 :]:
            if len(shown) >= max(limit, 1):
                break
            if paragraph.text.strip():
                line = _format_paragraph(paragraph)
                size += len(line) + (1 if shown else 0)
                if shown and max_chars is not None and size > max_chars:
                    break
                shown.append(line)
            last = paragraph.number
        return ParagraphView("\n".join(shown), first, last, len(paragraphs))

    # --- 바꾸기 ----------------------------------------------------------------------------------

    def apply_edits(self, edits: Sequence[Edit]) -> list[Change]:
        r"""수정을 순서대로 적용한다. 하나라도 실패하면 아무것도 적용하지 않는다.

        뒤의 수정은 앞의 수정을 적용한 문서에서 찾는다. ``old_text`` 는 문단 하나 안에서 찾고, 겹쳐서
        나오는 경우도 두 번 나온 것으로 본다. 바뀐 곳 요약의 ``line`` 은 문단 번호다.

        Args:
            edits: 적용할 수정 (1개 이상).

        Returns:
            바뀐 곳 요약.

        Raises:
            EditError: 수정이 없거나, ``old_text`` 가 비었거나 ``new_text`` 와 같거나, 찾지 못했거나
                문단을 넘거나, ``replace_all`` 없이 두 번 이상 나오거나, 고칠 수 없는 요소·링크 경계에
                걸리거나, 변경 내용 추적 기록이 있는 문단이거나, ``new_text`` 에 줄바꿈이 있을 때.
        """
        if not edits:
            raise EditError("No edits were given.")
        snapshot = {name: etree.tostring(root) for name, root in self._roots.items()}
        dirty = set(self._dirty)
        try:
            changes = [self._apply(index, edit) for index, edit in enumerate(edits, 1)]
        except EditError:
            parser = etree.XMLParser(resolve_entities=False, no_network=True)
            self._roots = {n: etree.fromstring(x, parser) for n, x in snapshot.items()}
            self._dirty = dirty
            self._paragraphs = None
            raise
        return changes

    def _target(self, index: int, ref: str | None) -> list[Paragraph]:
        if ref is None:
            return self.paragraphs
        match = _PARAGRAPH_REF.fullmatch(ref.strip())
        if not match:
            raise EditError(f'Edit {index}: paragraph must look like "p12".', index)
        number = int(match.group(1))
        if not 1 <= number <= len(self.paragraphs):
            raise EditError(
                f"Edit {index}: paragraph p{number} does not exist "
                f"(the document has p1 to p{len(self.paragraphs)}).",
                index,
            )
        return [self.paragraphs[number - 1]]

    def _apply(self, index: int, edit: Edit) -> Change:
        old = edit.old_text.replace("\r\n", "\n")
        new = edit.new_text.replace("\r\n", "\n")
        if not old:
            raise EditError(f"Edit {index}: old_text is empty.", index)
        if old == new:
            raise EditError(f"Edit {index}: old_text and new_text are the same.", index)
        if _FORBIDDEN_CHARS.search(new):
            raise EditError(f"Edit {index}: new_text contains control characters.", index)
        targets = self._target(index, edit.paragraph or None)
        hits = [(p, start) for p in targets for start in _find_all(p.text, old)]
        if not hits:
            raise self._not_found(index, old, targets, edit.paragraph)
        if len(hits) > 1 and not edit.replace_all:
            raise EditError(
                f"Edit {index}: old_text appears {len(hits)} times. Include more surrounding text "
                'so that it matches exactly one place, set paragraph (for example "p12") to '
                "search only one paragraph, or set replace_all to change every occurrence.",
                index,
            )
        for paragraph, _ in hits:
            if paragraph.tracked:
                raise EditError(
                    f"Edit {index}: paragraph p{paragraph.number} has tracked changes, which "
                    "cannot be edited yet. Ask the user to accept or reject them in Word first.",
                    index,
                )
        first = hits[0][0].number
        count = 0
        # 같은 문단 안에서는 뒤에서부터 고쳐야 앞쪽 글자 위치가 그대로다. replace_all 은 겹치지 않게.
        by_paragraph: dict[int, list[int]] = {}
        for paragraph, start in hits:
            starts = by_paragraph.setdefault(paragraph.number, [])
            if not starts or start >= starts[-1] + len(old):
                starts.append(start)
        for number, starts in by_paragraph.items():
            paragraph = self.paragraphs[number - 1]
            editor = _ParagraphEditor(paragraph, index)
            for start in reversed(starts):
                editor.replace(start, old, new)
                count += 1
            editor.commit()
            self._dirty.add(paragraph.part)
        self._paragraphs = None
        return Change(index, first, count, _shorten(old), _shorten(new))

    def _not_found(
        self, index: int, old: str, targets: list[Paragraph], ref: str | None
    ) -> EditError:
        where = f"paragraph {ref}" if ref is not None else "the document"
        message = f"Edit {index}: old_text was not found in {where}."
        joined = "\n".join(p.text for p in targets)
        if ref is None and "\n" in old and old in joined:
            message += (
                " It spans more than one paragraph; edit each paragraph separately "
                "(joining or splitting paragraphs is not supported yet)."
            )
        elif _PARAGRAPH_PREFIX.match(old):
            message += " Do not include paragraph number prefixes such as [p12] in old_text."
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
        return buffer.getvalue()


def _find_all(text: str, value: str) -> list[int]:
    """``value`` 가 나오는 모든 위치 (겹치는 것 포함)."""
    found = []
    start = text.find(value)
    while start >= 0:
        found.append(start)
        start = text.find(value, start + 1)
    return found


class _ParagraphEditor:
    """문단 하나의 글자를 run 토큰 단위로 고친다.

    한 문단 안의 여러 바꾸기는 **뒤에서부터** 적용해야 한다 (앞쪽 글자의 토큰 위치가 그대로 남도록).
    ``commit`` 이 바뀐 run 만 XML 로 다시 쓴다.
    """

    def __init__(self, paragraph: Paragraph, index: int) -> None:
        """문단을 고칠 준비를 한다.

        Args:
            paragraph: 고칠 문단.
            index: 수정 순서 (오류 메시지용).
        """
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
            EditError: 고칠 수 없는 요소나 링크 경계에 걸리거나, 줄바꿈·``￼`` 를 새로 넣을 때.
        """
        matcher = SequenceMatcher(None, old, new, autojunk=False)
        span = self._glyphs[start : start + len(old)]
        if matcher.ratio() < WHOLE_REPLACE_RATIO and not any(g.wall for g in span):
            ops = [("replace", 0, len(old), 0, len(new))]
        else:
            ops = [op for op in matcher.get_opcodes() if op[0] != "equal"]
        for _, i1, i2, j1, j2 in reversed(ops):
            self._change(start + i1, start + i2, new[j1:j2], (start, start + len(old)))

    def _change(self, begin: int, end: int, text: str, match: tuple[int, int]) -> None:
        """보기 글자 ``[begin, end)`` 를 ``text`` 로 바꾼다 (``begin == end`` 면 넣기).

        ``match`` 는 ``old_text`` 가 차지한 범위로, 넣을 글자의 서식을 고를 때 그 안의 글자를 먼저 본다.
        """
        if "\n" in text:
            raise self._error(
                "new_text adds a line break. Splitting paragraphs or adding line breaks is not "
                "supported yet; edit the text within the paragraph instead."
            )
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
        inserted = [_Token("tab", "\t") if ch == "\t" else _Token("c", ch) for ch in text]
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
            if not glyph.wall and glyph.run is not None:
                return glyph.run, glyph.index + offset
        return None

    def commit(self) -> None:
        """바뀐 run 을 XML 로 다시 쓴다. 비게 된 run 은 지운다."""
        for run, tokens in self._runs.items():
            _write_run(run, tokens)


def _format_paragraph(paragraph: Paragraph) -> str:
    labels = [x for x in (paragraph.location, paragraph.style) if x]
    if paragraph.tracked:
        labels.append("변경 추적 기록 있음 · 고칠 수 없음")
    prefix = f"[p{paragraph.number}]"
    if labels:
        prefix += " " + " ".join(f"({x})" for x in labels)
    return f"{prefix} {paragraph.text}"
