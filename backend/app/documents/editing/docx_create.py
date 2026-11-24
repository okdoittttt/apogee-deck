r"""Markdown 으로 새 DOCX 를 만든다.

에이전트가 쓴 Markdown 을 ``markdown-it-py`` 로 읽어 WordprocessingML 본문을 lxml 로 만든다. 문서의 나머지
(스타일·목록 번호·쪽 설정)는 두 가지 중 하나다.

- **기본 양식**: 코드로 만든 최소한의 문서 (맑은 고딕, A4, 제목·인용·코드·표 스타일, 글머리표·번호 정의).
  바이너리 양식 파일을 저장소에 두지 않으려고 코드로 만든다.
- **사용자 양식**: 올린 DOCX 의 머리글·바닥글·스타일·쪽 설정을 그대로 두고 본문 내용만 바꾼다. 필요한
  스타일이 양식에 없으면 기본 양식의 정의를 더하고, 목록 번호 정의도 양식 뒤에 더한다. 고치지 않은 ZIP
  항목은 내용 그대로 복사한다 (2단계와 같은 원칙).

| Markdown | Word |
|---|---|
| ``#``~``######`` | ``heading 1``~``6`` 스타일 |
| 굵게·기울임·취소선·코드 | ``w:b``·``w:i``·``w:strike``·고정폭 문자 스타일 |
| 링크 (http·https·mailto) | ``w:hyperlink`` + 외부 관계 |
| 글머리표·번호 목록 (중첩) | ``numbering.xml`` 정의 + ``w:numPr``. 번호 목록은 목록마다 1(또는 시작 번호)부터 |
| 표 | ``Table Grid`` 스타일, 머리글 행 반복·굵게, 열 정렬 |
| 인용, 코드 블록, 가로줄 | ``Quote``·``Code`` 스타일, 문단 아래 테두리 |
| 이미지, HTML | 대체 텍스트·글자 그대로 (``notes`` 로 알린다) |

설계: ``works/02.docx-advencement.md`` 12절. 오류 메시지는 에이전트에게 그대로 가므로 영어로 쓴다.
"""

import io
import re
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from xml.sax.saxutils import escape

from lxml import etree
from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode

from app.documents.editing.docx import (
    LINE_BREAK,
    W,
    parse_part,
    rels_name,
    resolve_target,
    serialize_part,
)
from app.documents.editing.text import EditError

_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_DECLARATION = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
_FONT = "맑은 고딕"
_MONO = "Courier New"
_TEXT_WIDTH = 11906 - 2 * 1440
"""본문 너비 (A4 너비 - 좌우 여백, twip). 표 열 너비를 나눌 때 쓴다."""
_INDENT = 720
"""목록 한 수준의 들여쓰기 (twip)."""
_LINK_SCHEMES = ("http://", "https://", "mailto:")
_INVALID_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_NUMBERING_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"
_STYLES_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"

# (스타일 이름, 종류, 기본 id, 정의 XML 안쪽). 이름은 Word 내부 이름이라 한글 Word 에서도 같다.
_STYLES: list[tuple[str, str, str, str]] = [
    *[
        (
            f"heading {n}",
            "paragraph",
            f"Heading{n}",
            (
                f'<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:uiPriority w:val="9"/>'
                f'<w:qFormat/><w:pPr><w:keepNext/><w:keepLines/><w:spacing w:before="{280 - n * 20}" '
                f'w:after="120"/><w:outlineLvl w:val="{n - 1}"/></w:pPr><w:rPr><w:b/><w:bCs/>'
                f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/></w:rPr>'
            ),
        )
        for n, size in zip(range(1, 7), (36, 30, 26, 24, 22, 22), strict=True)
    ],
    (
        "quote",
        "paragraph",
        "Quote",
        (
            '<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:pBdr>'
            '<w:left w:val="single" w:sz="18" w:space="8" w:color="BFBFBF"/></w:pBdr>'
            '<w:ind w:left="360"/></w:pPr><w:rPr><w:color w:val="595959"/></w:rPr>'
        ),
    ),
    (
        "code",
        "paragraph",
        "Code",
        (
            '<w:basedOn w:val="Normal"/><w:pPr><w:shd w:val="clear" w:color="auto" w:fill="F2F2F2"/>'
            '<w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr><w:rPr>'
            f'<w:rFonts w:ascii="{_MONO}" w:hAnsi="{_MONO}" w:cs="{_MONO}"/>'
            '<w:sz w:val="20"/><w:szCs w:val="20"/></w:rPr>'
        ),
    ),
    (
        "list paragraph",
        "paragraph",
        "ListParagraph",
        (
            '<w:basedOn w:val="Normal"/><w:uiPriority w:val="34"/><w:qFormat/><w:pPr>'
            '<w:spacing w:after="60"/><w:ind w:left="720"/><w:contextualSpacing/></w:pPr>'
        ),
    ),
    (
        "hyperlink",
        "character",
        "Hyperlink",
        (
            '<w:uiPriority w:val="99"/><w:unhideWhenUsed/><w:rPr><w:color w:val="0563C1"/>'
            '<w:u w:val="single"/></w:rPr>'
        ),
    ),
    (
        "inline code",
        "character",
        "InlineCode",
        (
            f'<w:rPr><w:rFonts w:ascii="{_MONO}" w:hAnsi="{_MONO}" w:cs="{_MONO}"/>'
            '<w:shd w:val="clear" w:color="auto" w:fill="F2F2F2"/></w:rPr>'
        ),
    ),
    (
        "table grid",
        "table",
        "TableGrid",
        '<w:basedOn w:val="TableNormal"/><w:uiPriority w:val="39"/><w:pPr><w:spacing w:after="0" '
        'w:line="240" w:lineRule="auto"/></w:pPr><w:tblPr><w:tblBorders>'
        + "".join(
            f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="auto"/>'
            for side in ("top", "left", "bottom", "right", "insideH", "insideV")
        )
        + '</w:tblBorders><w:tblCellMar><w:left w:w="108" w:type="dxa"/><w:right w:w="108" '
        'w:type="dxa"/></w:tblCellMar></w:tblPr>',
    ),
]

_BASE_STYLES = (
    f'<w:styles xmlns:w="{W}"><w:docDefaults><w:rPrDefault><w:rPr>'
    f'<w:rFonts w:ascii="{_FONT}" w:eastAsia="{_FONT}" w:hAnsi="{_FONT}" w:cs="{_FONT}"/>'
    '<w:sz w:val="22"/><w:szCs w:val="22"/><w:lang w:val="ko-KR" w:eastAsia="ko-KR" w:bidi="ar-SA"/>'
    '</w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="160" w:line="276" '
    'w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/>'
    "<w:qFormat/></w:style>"
    '<w:style w:type="character" w:default="1" w:styleId="DefaultParagraphFont">'
    '<w:name w:val="Default Paragraph Font"/><w:uiPriority w:val="1"/><w:semiHidden/>'
    "<w:unhideWhenUsed/></w:style>"
    '<w:style w:type="table" w:default="1" w:styleId="TableNormal"><w:name w:val="Normal Table"/>'
    '<w:uiPriority w:val="99"/><w:semiHidden/><w:unhideWhenUsed/><w:tblPr><w:tblInd w:w="0" '
    'w:type="dxa"/><w:tblCellMar><w:top w:w="0" w:type="dxa"/><w:left w:w="108" w:type="dxa"/>'
    '<w:bottom w:w="0" w:type="dxa"/><w:right w:w="108" w:type="dxa"/></w:tblCellMar></w:tblPr>'
    "</w:style>"
    '<w:style w:type="numbering" w:default="1" w:styleId="NoList"><w:name w:val="No List"/>'
    '<w:uiPriority w:val="99"/><w:semiHidden/><w:unhideWhenUsed/></w:style>'
    "</w:styles>"
)
_SETTINGS = (
    f'<w:settings xmlns:w="{W}"><w:zoom w:percent="100"/><w:defaultTabStop w:val="800"/>'
    '<w:characterSpacingControl w:val="doNotCompress"/><w:compat><w:compatSetting '
    'w:name="compatibilityMode" w:uri="http://schemas.microsoft.com/office/word" w:val="15"/>'
    "</w:compat></w:settings>"
)
_SECTION = (
    '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" '
    'w:bottom="1440" w:left="1440" w:header="851" w:footer="992" w:gutter="0"/>'
    '<w:cols w:space="425"/><w:docGrid w:linePitch="360"/></w:sectPr>'
)
_BULLETS = ("•", "◦", "▪")
_ORDERED = (("decimal", "%{n}."), ("lowerLetter", "%{n})"), ("lowerRoman", "%{n}."))


def _w(name: str) -> str:
    return f"{{{W}}}{name}"


@dataclass
class Created:
    """만든 DOCX.

    Attributes:
        data: DOCX 파일 내용.
        paragraphs: 본문 문단 수 (표 칸 안 문단 제외).
        tables: 표 수.
        title: 첫 ``#`` 제목 (없으면 빈 글자).
        notes: 결과에 덧붙일 설명 (영어, 예: 이미지를 대체 텍스트로 넣었다).
    """

    data: bytes
    paragraphs: int
    tables: int
    title: str
    notes: list[str] = field(default_factory=list)


class _Styles:
    """필요한 스타일 이름 → 문서 안 id. 양식에 없는 스타일은 처음 쓸 때 정의를 더한다."""

    def __init__(self, root: etree._Element) -> None:
        self.root = root
        self.changed = False
        self._ids: dict[str, str] = {}
        self._used_ids = {s.get(_w("styleId"), "") for s in root.iter(_w("style"))}
        for style in root.iter(_w("style")):
            name = style.find(_w("name"))
            if name is not None:
                self._ids.setdefault(name.get(_w("val"), "").lower(), style.get(_w("styleId"), ""))

    def id(self, name: str) -> str:
        """스타일 이름(소문자)의 id. 없으면 기본 양식의 정의를 더한다."""
        if name in self._ids:
            return self._ids[name]
        _, kind, base_id, body = next(s for s in _STYLES if s[0] == name)
        style_id, n = base_id, 1
        while style_id in self._used_ids:
            n += 1
            style_id = f"{base_id}{n}"
        title = name if name not in ("code", "inline code") else name.title()
        xml = (
            f'<w:style xmlns:w="{W}" w:type="{kind}" w:styleId="{style_id}">'
            f'<w:name w:val="{title}"/>{body}</w:style>'
        )
        self.root.append(etree.fromstring(xml))
        self._ids[name] = style_id
        self._used_ids.add(style_id)
        self.changed = True
        return style_id


class _Numbering:
    """목록 번호 정의. 글머리표는 하나를 같이 쓰고, 번호 목록은 목록마다 새 번호(1부터 다시)를 만든다."""

    def __init__(self, root: etree._Element) -> None:
        self.root = root
        self.changed = False
        abstracts = [int(a.get(_w("abstractNumId"), "0")) for a in root.iter(_w("abstractNum"))]
        nums = [int(n.get(_w("numId"), "0")) for n in root.iter(_w("num"))]
        self._next_abstract = max(abstracts, default=-1) + 1
        self._next_num = max(nums, default=0) + 1
        self._abstract: dict[str, int] = {}
        self._bullet_num: int | None = None

    def _abstract_id(self, kind: str) -> int:
        if kind in self._abstract:
            return self._abstract[kind]
        value = self._next_abstract
        self._next_abstract += 1
        levels = []
        for level in range(9):
            if kind == "bullet":
                fmt, text = "bullet", _BULLETS[level % 3]
            else:
                fmt, pattern = _ORDERED[level % 3]
                text = pattern.format(n=level + 1)
            indent = _INDENT * (level + 1)
            levels.append(
                f'<w:lvl w:ilvl="{level}"><w:start w:val="1"/><w:numFmt w:val="{fmt}"/>'
                f'<w:lvlText w:val="{text}"/><w:lvlJc w:val="left"/><w:pPr>'
                f'<w:ind w:left="{indent}" w:hanging="360"/></w:pPr></w:lvl>'
            )
        xml = (
            f'<w:abstractNum xmlns:w="{W}" w:abstractNumId="{value}">'
            f'<w:multiLevelType w:val="hybridMultilevel"/>{"".join(levels)}</w:abstractNum>'
        )
        element = etree.fromstring(xml)
        first_num = self.root.find(_w("num"))  # 모든 abstractNum 은 num 보다 앞에 와야 한다
        if first_num is not None:
            first_num.addprevious(element)
        else:
            self.root.append(element)
        self._abstract[kind] = value
        self.changed = True
        return value

    def reuse(self, used: list[str]) -> None:
        """문서 본문이 이미 쓰는 목록 정의를 종류(글머리표·번호)마다 하나씩 다시 쓴다 (모양을 맞춘다).

        Args:
            used: 본문 문단이 쓰는 ``numId`` (나온 순서).
        """
        abstracts = {
            n.get(_w("numId")): n.find(_w("abstractNumId")) for n in self.root.iter(_w("num"))
        }
        definitions = {a.get(_w("abstractNumId")): a for a in self.root.iter(_w("abstractNum"))}
        for num_id in used:
            link = abstracts.get(num_id)
            definition = definitions.get(link.get(_w("val")) if link is not None else None)
            if definition is None or definition.find(_w("numStyleLink")) is not None:
                continue
            first = definition.find(f"{_w('lvl')}[@{_w('ilvl')}='0']/{_w('numFmt')}")
            if first is None or first.get(_w("val")) == "none":
                continue
            kind = "bullet" if first.get(_w("val")) == "bullet" else "ordered"
            if kind not in self._abstract:
                try:
                    self._abstract[kind] = int(definition.get(_w("abstractNumId"), ""))
                except ValueError:
                    continue

    def bullet(self) -> int:
        """글머리표 목록의 번호 id."""
        if self._bullet_num is None:
            self._bullet_num = self._num(self._abstract_id("bullet"), None)
        return self._bullet_num

    def ordered(self, level: int, start: int) -> int:
        """새 번호 목록의 번호 id (``level`` 에서 ``start`` 부터)."""
        return self._num(self._abstract_id("ordered"), (level, start))

    def _num(self, abstract: int, restart: tuple[int, int] | None) -> int:
        value = self._next_num
        self._next_num += 1
        override = ""
        if restart is not None:
            level, start = restart
            override = f'<w:lvlOverride w:ilvl="{level}"><w:startOverride w:val="{start}"/></w:lvlOverride>'
        xml = (
            f'<w:num xmlns:w="{W}" w:numId="{value}"><w:abstractNumId w:val="{abstract}"/>'
            f"{override}</w:num>"
        )
        self.root.append(etree.fromstring(xml))
        self.changed = True
        return value


@dataclass
class _Context:
    """블록을 만들 때의 위치: 목록 깊이·번호, 인용 안인지."""

    level: int = -1
    num: int | None = None
    quote: bool = False
    first_in_item: bool = False


class _Builder:
    """Markdown 문법 트리를 본문 요소(``w:p``·``w:tbl``)로 바꾼다."""

    def __init__(
        self,
        styles: _Styles,
        numbering: _Numbering,
        links: "_Links",
        text_width: int = _TEXT_WIDTH,
    ) -> None:
        self.styles = styles
        self.numbering = numbering
        self.links = links
        self.text_width = text_width
        self.blocks: list[etree._Element] = []
        self.tables = 0
        self.images = 0
        self.title = ""

    # --- 블록 -----------------------------------------------------------------------------------

    def build(self, node: SyntaxTreeNode, context: _Context) -> None:
        for child in node.children:
            self._block(child, context)

    def _block(self, node: SyntaxTreeNode, context: _Context) -> None:
        kind = node.type
        if kind == "heading":
            level = int(node.tag[1])
            paragraph = self._paragraph(self.styles.id(f"heading {level}"))
            self._inline(paragraph, node.children[0] if node.children else None)
            if level == 1 and not self.title:
                self.title = _plain(node)
        elif kind == "paragraph":
            self._text_paragraph(node, context)
        elif kind in ("bullet_list", "ordered_list"):
            level = context.level + 1
            if kind == "bullet_list":
                num = self.numbering.bullet()
            else:
                start = int(node.attrs.get("start", 1)) if node.attrs else 1
                num = self.numbering.ordered(min(level, 8), start)
            for item in node.children:
                inner = _Context(level, num, context.quote, True)
                self.build(item, inner)
        elif kind == "blockquote":
            self.build(node, _Context(context.level, context.num, True, False))
        elif kind in ("fence", "code_block"):
            self._code(node.content, context)
        elif kind == "hr":
            paragraph = self._paragraph(None)
            props = paragraph.find(_w("pPr"))
            if props is None:
                props = etree.SubElement(paragraph, _w("pPr"))
            border = etree.SubElement(props, _w("pBdr"))
            bottom = etree.SubElement(border, _w("bottom"))
            for key, value in (("val", "single"), ("sz", "6"), ("space", "1"), ("color", "auto")):
                bottom.set(_w(key), value)
        elif kind == "table":
            self._table(node)
        elif kind == "html_block":
            for line in node.content.rstrip("\n").split("\n"):
                self._add_runs(self._paragraph(None), line, {})
        else:  # 알 수 없는 블록: 안쪽을 그대로 훑는다
            self.build(node, context)

    def _paragraph(self, style: str | None, parent: list | None = None) -> etree._Element:
        paragraph = etree.Element(_w("p"))
        if style:
            props = etree.SubElement(paragraph, _w("pPr"))
            etree.SubElement(props, _w("pStyle")).set(_w("val"), style)
        (self.blocks if parent is None else parent).append(paragraph)
        return paragraph

    def _text_paragraph(self, node: SyntaxTreeNode, context: _Context) -> None:
        if context.level >= 0:
            paragraph = self._paragraph(self.styles.id("list paragraph"))
            props = paragraph.find(_w("pPr"))
            if context.first_in_item and context.num is not None:
                numbering = etree.SubElement(props, _w("numPr"))
                etree.SubElement(numbering, _w("ilvl")).set(_w("val"), str(min(context.level, 8)))
                etree.SubElement(numbering, _w("numId")).set(_w("val"), str(context.num))
                context.first_in_item = False
            else:  # 목록 항목 안의 두 번째 문단: 번호 없이 같은 들여쓰기
                indent = etree.SubElement(props, _w("ind"))
                indent.set(_w("left"), str(_INDENT * (min(context.level, 8) + 1)))
        else:
            paragraph = self._paragraph(self.styles.id("quote") if context.quote else None)
        self._inline(paragraph, node.children[0] if node.children else None)

    def _code(self, content: str, context: _Context) -> None:
        paragraph = self._paragraph(self.styles.id("code"))
        if context.level >= 0:
            indent = etree.SubElement(paragraph.find(_w("pPr")), _w("ind"))
            indent.set(_w("left"), str(_INDENT * (min(context.level, 8) + 1)))
        run = etree.SubElement(paragraph, _w("r"))
        for k, line in enumerate(content.rstrip("\n").split("\n")):
            if k:
                etree.SubElement(run, _w("br"))
            for m, piece in enumerate(line.split("\t")):
                if m:
                    etree.SubElement(run, _w("tab"))
                if piece:
                    _text(run, piece)

    def _table(self, node: SyntaxTreeNode) -> None:
        rows = [row for section in node.children for row in section.children]
        if not rows:
            return
        self.tables += 1
        columns = max(len(row.children) for row in rows)
        width = self.text_width // columns
        table = etree.Element(_w("tbl"))
        props = etree.SubElement(table, _w("tblPr"))
        etree.SubElement(props, _w("tblStyle")).set(_w("val"), self.styles.id("table grid"))
        table_width = etree.SubElement(props, _w("tblW"))
        table_width.set(_w("w"), str(width * columns))
        table_width.set(_w("type"), "dxa")
        # 표 스타일의 테두리를 모르는 프로그램(macOS 미리보기 등)도 있어 테두리를 표에도 직접 둔다.
        borders = etree.SubElement(props, _w("tblBorders"))
        for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
            line = etree.SubElement(borders, _w(side))
            for key, value in (("val", "single"), ("sz", "4"), ("space", "0"), ("color", "auto")):
                line.set(_w(key), value)
        # 열 너비를 고정해야 미리보기 프로그램도 열을 쪽 너비에 맞춰 그린다 (LibreOffice 가 쓰는 방식).
        etree.SubElement(props, _w("tblLayout")).set(_w("type"), "fixed")
        look = etree.SubElement(props, _w("tblLook"))
        look.set(_w("val"), "04A0")
        look.set(_w("firstRow"), "1")
        look.set(_w("noHBand"), "0")
        look.set(_w("noVBand"), "1")
        grid = etree.SubElement(table, _w("tblGrid"))
        for _ in range(columns):
            etree.SubElement(grid, _w("gridCol")).set(_w("w"), str(width))
        for row in rows:
            header = all(cell.type == "th" for cell in row.children)
            element = etree.SubElement(table, _w("tr"))
            if header:
                etree.SubElement(etree.SubElement(element, _w("trPr")), _w("tblHeader"))
            cells = list(row.children)
            for index in range(columns):
                cell = etree.SubElement(element, _w("tc"))
                cell_props = etree.SubElement(cell, _w("tcPr"))
                cell_width = etree.SubElement(cell_props, _w("tcW"))
                cell_width.set(_w("w"), str(width))
                cell_width.set(_w("type"), "dxa")
                paragraph = etree.SubElement(cell, _w("p"))
                source = cells[index] if index < len(cells) else None
                align = _alignment(source)
                if align:
                    paragraph_props = etree.SubElement(paragraph, _w("pPr"))
                    etree.SubElement(paragraph_props, _w("jc")).set(_w("val"), align)
                if source is not None and source.children:
                    self._inline(paragraph, source.children[0], bold=header)
        if self.blocks and self.blocks[-1].tag == _w("tbl"):
            self.blocks.append(etree.Element(_w("p")))  # 표가 이어지면 Word 가 하나로 붙인다
        self.blocks.append(table)

    # --- 글자 -----------------------------------------------------------------------------------

    def _inline(
        self, paragraph: etree._Element, node: SyntaxTreeNode | None, bold: bool = False
    ) -> None:
        if node is None:
            return
        self._walk_inline(paragraph, node, {"b": True} if bold else {})

    def _walk_inline(self, parent: etree._Element, node: SyntaxTreeNode, marks: dict) -> None:
        for child in node.children:
            kind = child.type
            if kind == "text":
                self._add_runs(parent, child.content, marks)
            elif kind == "code_inline":
                self._add_runs(parent, child.content, {**marks, "code": True})
            elif kind == "softbreak":
                self._add_runs(parent, " ", marks)
            elif kind == "hardbreak":
                run = self._run(parent, marks)
                etree.SubElement(run, _w("br"))
            elif kind in ("strong", "em", "s"):
                key = {"strong": "b", "em": "i", "s": "strike"}[kind]
                self._walk_inline(parent, child, {**marks, key: True})
            elif kind == "link":
                href = str(child.attrs.get("href", ""))
                if href.lower().startswith(_LINK_SCHEMES) and parent.tag == _w("p"):
                    link = etree.SubElement(parent, _w("hyperlink"))
                    link.set(f"{{{_R}}}id", self.links.add(href))
                    link.set(_w("history"), "1")
                    self._walk_inline(link, child, {**marks, "link": True})
                else:
                    self._walk_inline(parent, child, marks)
            elif kind == "image":
                self.images += 1
                alt = _plain(child) or str(child.attrs.get("alt", ""))
                if alt:
                    self._add_runs(parent, f"[{alt}]", marks)
            elif kind == "html_inline":
                self._add_runs(parent, child.content, marks)
            elif child.children:
                self._walk_inline(parent, child, marks)

    def _run(self, parent: etree._Element, marks: dict) -> etree._Element:
        run = etree.SubElement(parent, _w("r"))
        style = None
        if marks.get("link"):
            style = self.styles.id("hyperlink")
        elif marks.get("code"):
            style = self.styles.id("inline code")
        flags = [key for key in ("b", "i", "strike") if marks.get(key)]
        if style or flags:
            props = etree.SubElement(run, _w("rPr"))
            if style:
                etree.SubElement(props, _w("rStyle")).set(_w("val"), style)
            for key in flags:
                etree.SubElement(props, _w(key))
                if key in ("b", "i"):
                    etree.SubElement(props, _w(f"{key}Cs"))
        return run

    def _add_runs(self, parent: etree._Element, text: str, marks: dict) -> None:
        text = _INVALID_XML.sub("", text)
        if not text:
            return
        run = self._run(parent, marks)
        for n, line in enumerate(text.split(LINE_BREAK)):
            if n:
                etree.SubElement(run, _w("br"))
            for k, piece in enumerate(line.split("\t")):
                if k:
                    etree.SubElement(run, _w("tab"))
                if piece:
                    _text(run, piece)


class _Links:
    """하이퍼링크 외부 관계. 같은 주소는 관계 하나를 같이 쓴다."""

    def __init__(self, rels: etree._Element) -> None:
        self.rels = rels
        self.changed = False
        self._by_target: dict[str, str] = {}
        self._used = {rel.get("Id") for rel in rels}

    def add(self, target: str) -> str:
        if target in self._by_target:
            return self._by_target[target]
        number = 1
        while f"rId{number}" in self._used:
            number += 1
        rel_id = f"rId{number}"
        rel = etree.SubElement(self.rels, f"{{{_PKG_REL}}}Relationship")
        rel.set("Id", rel_id)
        rel.set("Type", _REL + "hyperlink")
        rel.set("Target", target)
        rel.set("TargetMode", "External")
        self._used.add(rel_id)
        self._by_target[target] = rel_id
        self.changed = True
        return rel_id


def _text(run: etree._Element, value: str) -> None:
    t = etree.SubElement(run, _w("t"))
    t.text = value
    if value[0].isspace() or value[-1].isspace() or "  " in value:
        t.set(_XML_SPACE, "preserve")


def _plain(node: SyntaxTreeNode) -> str:
    """노드 안의 글자만 이어 붙인 것."""
    if node.type in ("text", "code_inline"):
        return node.content
    if node.type == "softbreak":
        return " "
    return "".join(_plain(child) for child in node.children)


def _alignment(cell: SyntaxTreeNode | None) -> str | None:
    style = str(cell.attrs.get("style", "")) if cell is not None and cell.attrs else ""
    return {"text-align:center": "center", "text-align:right": "right"}.get(style)


def _parse_markdown(markdown: str) -> SyntaxTreeNode:
    parser = MarkdownIt("commonmark", {"html": True}).enable(["table", "strikethrough"])
    return SyntaxTreeNode(parser.parse(markdown))


# --- 기본 양식 ------------------------------------------------------------------------------------


def _base_package(author: str, title: str, now: datetime) -> dict[str, bytes]:
    """기본 양식의 ZIP 항목 (본문·관계는 빈 상태)."""
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    core = (
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/'
        'core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/'
        f'XMLSchema-instance"><dc:title>{escape(title)}</dc:title><dc:creator>{escape(author)}'
        f"</dc:creator><cp:lastModifiedBy>{escape(author)}</cp:lastModifiedBy>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{stamp}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{stamp}</dcterms:modified>'
        "</cp:coreProperties>"
    )
    app = (
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-'
        'properties"><Application>okdoit</Application></Properties>'
    )
    types = (
        f'<Types xmlns="{_CT}"><Default Extension="rels" ContentType="application/vnd.'
        'openxmlformats-package.relationships+xml"/><Default Extension="xml" '
        'ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.wordprocessingml.document.main+xml"/>'
        f'<Override PartName="/word/styles.xml" ContentType="{_STYLES_TYPE}"/>'
        f'<Override PartName="/word/numbering.xml" ContentType="{_NUMBERING_TYPE}"/>'
        '<Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.wordprocessingml.settings+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-'
        'package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.extended-properties+xml"/></Types>'
    )
    root_rels = (
        f'<Relationships xmlns="{_PKG_REL}">'
        f'<Relationship Id="rId1" Type="{_REL}officeDocument" Target="word/document.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/'
        'relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        f'<Relationship Id="rId3" Type="{_REL}extended-properties" Target="docProps/app.xml"/>'
        "</Relationships>"
    )
    document_rels = (
        f'<Relationships xmlns="{_PKG_REL}">'
        f'<Relationship Id="rId1" Type="{_REL}styles" Target="styles.xml"/>'
        f'<Relationship Id="rId2" Type="{_REL}numbering" Target="numbering.xml"/>'
        f'<Relationship Id="rId3" Type="{_REL}settings" Target="settings.xml"/>'
        "</Relationships>"
    )
    document = f'<w:document xmlns:w="{W}" xmlns:r="{_R}"><w:body>{_SECTION}</w:body></w:document>'
    parts = {
        "[Content_Types].xml": types,
        "_rels/.rels": root_rels,
        "docProps/core.xml": core,
        "docProps/app.xml": app,
        "word/document.xml": document,
        "word/_rels/document.xml.rels": document_rels,
        "word/styles.xml": _BASE_STYLES,
        "word/numbering.xml": f'<w:numbering xmlns:w="{W}"/>',
        "word/settings.xml": _SETTINGS,
    }
    return {name: _DECLARATION + xml.encode() for name, xml in parts.items()}


# --- 만들기 ---------------------------------------------------------------------------------------


@dataclass
class MarkdownBlocks:
    """기존 문서에 넣을 Markdown 내용 (``markdown_blocks`` 결과).

    Attributes:
        blocks: 본문 블록 (``w:p``·``w:tbl``), 순서대로.
        headings: 제목 문단 수.
        paragraphs: 제목·목록이 아닌 글자 있는 문단 수.
        list_items: 목록 항목 수.
        tables: 표 수.
        rows: 표 행 수 (모든 표).
        images: 글자로 대신 넣은 그림 수.
        changed: 고친 부분 종류 (``styles``·``numbering``·``rels``).
        plain: 스타일 없는 본문 문단 (넣는 쪽이 주변 문단 서식을 입힌다).
    """

    blocks: list[etree._Element]
    headings: int = 0
    paragraphs: int = 0
    list_items: int = 0
    tables: int = 0
    rows: int = 0
    images: int = 0
    changed: set[str] = field(default_factory=set)
    plain: list[etree._Element] = field(default_factory=list)


def new_list_number(root: etree._Element, used: list[str], bullet: bool, level: int) -> int:
    """기존 문서에 새 목록 번호(``numId``)를 만든다 (묶음 5 ``set_style`` 의 새 목록).

    Args:
        root: 문서의 목록 번호 부분.
        used: 본문 문단이 쓰는 ``numId`` (나온 순서). 같은 종류 목록 정의가 있으면 그 모양을 따른다.
        bullet: 글머리표 목록인지 (아니면 번호 목록, 1부터).
        level: 시작 수준 (0부터).

    Returns:
        새 ``numId``.
    """
    numbering = _Numbering(root)
    numbering.reuse(used)
    return numbering.bullet() if bullet else numbering.ordered(min(level, 8), 1)


def _has_lists(node: SyntaxTreeNode) -> bool:
    return node.type in ("bullet_list", "ordered_list") or any(
        _has_lists(child) for child in node.children
    )


def markdown_blocks(
    markdown: str,
    *,
    styles: etree._Element,
    numbering: Callable[[], etree._Element],
    rels: etree._Element,
    text_width: int,
    used_lists: list[str],
) -> MarkdownBlocks:
    """Markdown 을 기존 문서에 넣을 본문 블록으로 바꾼다 (묶음 5 ``insert_markdown``).

    스타일은 문서에 같은 이름이 있으면 그것을 쓰고 없으면 정의를 더한다. 목록은 문서가 이미 쓰는 같은 종류
    목록 정의를 따른다 (번호 목록은 1부터 다시).

    Args:
        markdown: 넣을 내용.
        styles: 문서의 스타일 부분 (고치면 ``changed`` 에 ``styles``).
        numbering: 목록 번호 부분을 돌려주는 함수 (목록이 있을 때만 부른다. 없으면 만들어야 한다).
        rels: 본문 부분의 관계 (링크를 더하면 ``changed`` 에 ``rels``).
        text_width: 표를 넣을 자리의 본문 너비 (twip).
        used_lists: 본문 문단이 쓰는 ``numId`` (나온 순서).

    Returns:
        만든 블록과 개수.

    Raises:
        EditError: 내용이 비었거나 넣을 글자가 없을 때.
    """
    text = markdown.replace("\r\n", "\n")
    if not text.strip():
        raise EditError("new_text is empty. Write the content to insert in Markdown.")
    tree = _parse_markdown(text)
    style_table = _Styles(styles)
    if _has_lists(tree):
        number_table = _Numbering(numbering())
        number_table.reuse(used_lists)
    else:
        number_table = _Numbering(etree.Element(_w("numbering")))
    links = _Links(rels)
    builder = _Builder(style_table, number_table, links, text_width)
    builder.build(tree, _Context())
    if not builder.blocks:
        raise EditError("new_text has no content to insert.")
    result = MarkdownBlocks(builder.blocks, tables=builder.tables, images=builder.images)
    headings = {
        style_table.id(f"heading {n}") for n in range(1, 7) if f"heading {n}" in style_table._ids
    }
    for block in builder.blocks:
        if block.tag == _w("tbl"):
            result.rows += len(block.findall(_w("tr")))
            continue
        style = block.find(f"{_w('pPr')}/{_w('pStyle')}")
        if style is not None and style.get(_w("val")) in headings:
            result.headings += 1
        elif block.find(f"{_w('pPr')}/{_w('numPr')}") is not None:
            result.list_items += 1
        elif "".join(t.text or "" for t in block.iter(_w("t"))).strip():
            result.paragraphs += 1
            if style is None and block.find(f"{_w('pPr')}/{_w('ind')}") is None:
                result.plain.append(block)
    if style_table.changed:
        result.changed.add("styles")
    if number_table.changed:
        result.changed.add("numbering")
    if links.changed:
        result.changed.add("rels")
    return result


def build_docx(
    markdown: str,
    *,
    author: str,
    template: bytes | None = None,
    now: datetime | None = None,
) -> Created:
    """Markdown 으로 DOCX 를 만든다.

    Args:
        markdown: 문서 내용 (Markdown).
        author: 문서 속성의 만든 이 (기본 양식일 때).
        template: 양식으로 쓸 DOCX 파일 내용. 머리글·바닥글·스타일·쪽 설정을 그대로 두고 본문만 바꾼다.
        now: 문서 속성의 시각 (테스트용). 없으면 지금.

    Returns:
        만든 DOCX 와 요약.

    Raises:
        EditError: 내용이 비었거나, 본문이 생기지 않거나, 양식이 Word 문서가 아닐 때.
    """
    body_text = markdown.replace("\r\n", "\n")
    if not body_text.strip():
        raise EditError("The content is empty. Write the full text of the document in Markdown.")
    tree = _parse_markdown(body_text)
    title = next((_plain(n) for n in tree.children if n.type == "heading"), "")
    if template is None:
        entries = [
            (name, data)
            for name, data in _base_package(author, title, now or datetime.now(UTC)).items()
        ]
        infos = None
    else:
        try:
            with zipfile.ZipFile(io.BytesIO(template)) as zf:
                infos = zf.infolist()
                entries = [(info.filename, zf.read(info)) for info in infos]
        except (zipfile.BadZipFile, OSError) as exc:
            raise EditError("The template is not a readable Word document.") from exc
    package = _Package(dict(entries))
    styles = _Styles(package.part("styles"))
    numbering = _Numbering(package.part("numbering"))
    links = _Links(package.rels())
    builder = _Builder(styles, numbering, links)
    builder.build(tree, _Context())
    if not builder.blocks:
        raise EditError("The content has no text to put in the document.")
    body = package.document.find(_w("body"))
    if body is None:
        raise EditError("The template has no document body.")
    section = body.find(_w("sectPr"))
    for child in list(body):
        if child is not section:
            body.remove(child)
    blocks = builder.blocks
    if blocks[-1].tag == _w("tbl"):  # 본문은 문단으로 끝나야 한다 (Word 가 표 뒤에 문단을 요구)
        blocks.append(etree.Element(_w("p")))
    for block in blocks:
        if section is not None:
            section.addprevious(block)
        else:
            body.append(block)
    package.dirty.add(package.main)
    if styles.changed:
        package.dirty.add(package.names["styles"])
    if numbering.changed:
        package.dirty.add(package.names["numbering"])
    if links.changed:
        package.dirty.add(rels_name(package.main))
    data = package.to_bytes(infos)
    notes = []
    if builder.images:
        notes.append(
            f"{builder.images} image(s) were written as their alt text in brackets, because "
            "images cannot be added."
        )
    paragraphs = sum(1 for b in blocks if b.tag == _w("p"))
    return Created(data, paragraphs, builder.tables, title, notes)


class _Package:
    """ZIP 항목들. 필요한 부분(스타일·번호)이 없으면 만들고 관계·내용 형식에 등록한다."""

    def __init__(self, entries: dict[str, bytes]) -> None:
        self.entries = entries
        self.roots: dict[str, etree._Element] = {}
        self.dirty: set[str] = set()
        self.main = self._main_part()
        self.document = self.root(self.main)
        self.names: dict[str, str] = {}

    def _main_part(self) -> str:
        rels = self.entries.get("_rels/.rels")
        if rels is not None:
            for rel in parse_part(rels).iter(f"{{{_PKG_REL}}}Relationship"):
                if rel.get("Type") == _REL + "officeDocument":
                    name = resolve_target("", rel.get("Target", ""))
                    if name in self.entries:
                        return name
        if "word/document.xml" in self.entries:
            return "word/document.xml"
        raise EditError("The template is not a Word document.")

    def root(self, name: str) -> etree._Element:
        if name not in self.roots:
            self.roots[name] = parse_part(self.entries[name])
        return self.roots[name]

    def rels(self) -> etree._Element:
        name = rels_name(self.main)
        if name not in self.entries and name not in self.roots:
            self.roots[name] = etree.Element(f"{{{_PKG_REL}}}Relationships", nsmap={None: _PKG_REL})
            self.dirty.add(name)
        return self.root(name) if name in self.entries else self.roots[name]

    def part(self, kind: str) -> etree._Element:
        """본문과 ``kind`` 관계(``styles``·``numbering``)로 이어진 부분. 없으면 만든다."""
        rels = self.rels()
        for rel in rels.iter(f"{{{_PKG_REL}}}Relationship"):
            if rel.get("Type") == _REL + kind and rel.get("TargetMode") != "External":
                name = resolve_target(self.main, rel.get("Target", ""))
                if name in self.entries:
                    self.names[kind] = name
                    return self.root(name)
        folder = self.main.rsplit("/", 1)[0] + "/" if "/" in self.main else ""
        name = f"{folder}{kind}.xml"
        xml = _BASE_STYLES if kind == "styles" else f'<w:numbering xmlns:w="{W}"/>'
        self.roots[name] = etree.fromstring(xml)
        self.names[kind] = name
        used = {r.get("Id") for r in rels}
        number = 1
        while f"rId{number}" in used:
            number += 1
        rel = etree.SubElement(rels, f"{{{_PKG_REL}}}Relationship")
        rel.set("Id", f"rId{number}")
        rel.set("Type", _REL + kind)
        rel.set("Target", f"{kind}.xml")
        self.dirty.add(rels_name(self.main))
        types = self.root("[Content_Types].xml")
        override = etree.SubElement(types, f"{{{_CT}}}Override")
        override.set("PartName", f"/{name}")
        override.set("ContentType", _STYLES_TYPE if kind == "styles" else _NUMBERING_TYPE)
        self.dirty.add("[Content_Types].xml")
        self.dirty.add(name)
        return self.roots[name]

    def to_bytes(self, infos: list[zipfile.ZipInfo] | None) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            written = set()
            for info in infos or []:
                data = self.entries[info.filename]
                if info.filename in self.dirty:
                    data = serialize_part(self.roots[info.filename], data)
                zf.writestr(info, data, compress_type=info.compress_type)
                written.add(info.filename)
            for name, data in self.entries.items():
                if name in written:
                    continue
                if name in self.dirty:
                    data = serialize_part(self.roots[name], data)
                zf.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
                written.add(name)
            for name, root in self.roots.items():  # 새로 만든 부분
                if name not in written:
                    zf.writestr(
                        name, serialize_part(root, _DECLARATION), compress_type=zipfile.ZIP_DEFLATED
                    )
        return buffer.getvalue()
