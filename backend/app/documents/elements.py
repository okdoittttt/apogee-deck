"""문서를 요소(본문·표·그림) 목록으로 만든다. 프로젝트 문서의 구조화 적재에 쓴다.

파서(``app.documents.parser``)가 xberg 결과를 단순한 값(딕셔너리, 문자열)으로 바꿔 넘기면 여기서 요소를
만든다. xberg 에 의존하지 않아서 테스트하기 쉽다.

- Office·한글: xberg 문서 구조 트리의 노드를 순서대로 돌며 제목 경로를 따라가고, 단락은 같은 제목·쪽끼리
  묶고, 표는 Markdown 표로, 그림은 그림 요소로 만든다. PPTX 발표자 노트는 그 슬라이드 끝에 붙인다.
- PDF: 본문은 쪽마다 하나(``ocr_merge`` 로 합친 본문), 표는 쪽별 표 추출 결과로 만든다. 쪽을 넘는 표는
  열 수가 같으면 앞 표의 머리글을 이어 붙인다 (다음 쪽 조각의 첫 행이 머리글로 잘못 잡히기 때문).
"""

from dataclasses import dataclass, field
from typing import Any, Literal

ElementKind = Literal["text", "table", "image"]

MIN_IMAGE_SIDE = 80
MIN_IMAGE_AREA = 50_000
"""이보다 작은 그림(아이콘·글머리표 등)은 버린다 (픽셀)."""

BLANK_MAX_BYTES_PER_PIXEL = 0.002
"""압축한 그림 용량이 픽셀당 이보다 작으면 거의 한 가지 색인 빈 그림으로 본다. PDF 그림의 투명도 마스크가
흰색 그림으로 따로 나오는 경우 등이다 (실측: 차트는 픽셀당 약 0.03바이트, 빈 마스크는 0.0006바이트)."""

SKIPPED_IMAGE_KINDS = frozenset({"icon", "logo", "decoration", "mask", "tile_fragment"})
"""xberg 그림 분류에서 버리는 종류. 차트·사진 등 나머지는 vision 모델이 설명한다."""

TEXT_NODE_TYPES = frozenset(
    {
        "paragraph",
        "list_item",
        "quote",
        "code",
        "formula",
        "title",
        "definition_item",
        "footnote",
        "admonition",
        "raw_block",
    }
)


@dataclass
class ImageData:
    """요소에 딸린 그림 원본.

    Attributes:
        data: 그림 바이트.
        mime_type: MIME 타입 (``image/webp`` 등).
        width: 너비(픽셀).
        height: 높이(픽셀).
        ocr_text: 그림 속 글자 (OCR). 설명을 만들지 못했을 때 대신 쓴다.
    """

    data: bytes
    mime_type: str
    width: int
    height: int
    ocr_text: str = ""


@dataclass
class Element:
    """문서 요소 하나.

    Attributes:
        kind: ``text`` (본문) | ``table`` (Markdown 표) | ``image`` (그림).
        content: 본문, 표 Markdown, 또는 그림의 OCR 글자 (설명을 만든 뒤에는 설명으로 바뀐다).
        page: 쪽(슬라이드·시트) 번호. 쪽 구분이 없으면 ``None``.
        heading: 제목 경로 (예: ``"1장 매출 현황 > 1.1 지역별 매출"``). 없으면 ``None``.
        image: 그림 요소의 원본.
    """

    kind: ElementKind
    content: str
    page: int | None = None
    heading: str | None = None
    image: ImageData | None = field(default=None, repr=False)


def keep_image(
    width: int, height: int, image_kind: str | None, data_size: int | None = None
) -> bool:
    """설명할 만한 그림인지: 아이콘·로고·장식, 작은 그림, 빈 그림을 버린다.

    Args:
        width: 너비(픽셀).
        height: 높이(픽셀).
        image_kind: xberg 그림 분류 (``icon``, ``chart`` 등). 모르면 ``None``.
        data_size: 압축한 그림의 바이트 수 (WebP·PNG). 주면 빈 그림인지도 확인한다.

    Returns:
        남길 그림이면 ``True``.
    """
    if image_kind and image_kind.lower() in SKIPPED_IMAGE_KINDS:
        return False
    if min(width, height) < MIN_IMAGE_SIDE or width * height < MIN_IMAGE_AREA:
        return False
    # 거의 한 가지 색인 그림은 아주 작게 압축된다. 설명해도 "흰 바탕만 보인다" 가 되므로 버린다.
    return data_size is None or data_size >= width * height * BLANK_MAX_BYTES_PER_PIXEL


def table_markdown(rows: list[list[str]]) -> str:
    """행 목록을 Markdown 표로 만든다. 첫 행이 머리글이다.

    Args:
        rows: 칸 글자의 2차원 목록. 행마다 칸 수가 달라도 된다 (가장 긴 행에 맞춘다).

    Returns:
        Markdown 표. 행이 없으면 빈 문자열.
    """
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)

    def line(cells: list[str]) -> str:
        cells = [c.replace("\n", " ").replace("|", "\\|").strip() for c in cells]
        return "| " + " | ".join(cells + [""] * (width - len(cells))) + " |"

    return "\n".join(
        [line(rows[0]), "| " + " | ".join(["---"] * width) + " |"] + [line(r) for r in rows[1:]]
    )


def grid_rows(grid: dict[str, Any]) -> list[list[str]]:
    """Xberg 표 격자(``{"rows", "cols", "cells": [{content, row, col}]}``)를 행 목록으로 만든다.

    Args:
        grid: 표 노드의 ``grid``.

    Returns:
        칸 글자의 2차원 목록. 합친 칸은 첫 칸에만 글자가 있다.
    """
    rows = [[""] * int(grid.get("cols") or 0) for _ in range(int(grid.get("rows") or 0))]
    for cell in grid.get("cells") or []:
        r, c = int(cell.get("row", 0)), int(cell.get("col", 0))
        if 0 <= r < len(rows) and 0 <= c < len(rows[r]):
            rows[r][c] = str(cell.get("content") or "")
    return rows


def from_structure(
    nodes: list[dict[str, Any]],
    images: dict[int, tuple[ImageData, str | None]],
    speaker_notes: dict[int, str] | None = None,
) -> list[Element]:
    """문서 구조 트리의 노드로 요소 목록을 만든다 (Office·한글).

    Args:
        nodes: 문서 순서대로의 노드. 각 노드는 xberg 노드 내용(``node_type`` 등)에 ``page`` 를 더한 것.
        images: 그림 번호 → ``(원본, xberg 분류)``. 노드의 ``image_index`` 로 찾는다.
        speaker_notes: 쪽 번호 → 발표자 노트 (PPTX).

    Returns:
        요소 목록 (문서 순서). 버린 그림은 들어가지 않는다.
    """
    elements: list[Element] = []
    headings: list[tuple[int, str]] = []
    buffer: list[str] = []
    buffer_page: int | None = None

    def path() -> str | None:
        return " > ".join(t for _, t in headings) or None

    def flush() -> None:
        nonlocal buffer
        text = "\n\n".join(t for t in buffer if t.strip())
        if text.strip():
            elements.append(Element("text", text, buffer_page, path()))
        buffer = []

    for node in nodes:
        node_type = node.get("node_type")
        page = node.get("page")
        if node_type == "heading":
            flush()
            level = int(node.get("level") or 1)
            headings[:] = [h for h in headings if h[0] < level]
            if text := str(node.get("text") or "").strip():
                headings.append((level, text))
        elif node_type == "table":
            flush()
            if md := table_markdown(grid_rows(node.get("grid") or {})):
                elements.append(Element("table", md, page, path()))
        elif node_type == "image":
            flush()
            found = images.get(int(node.get("image_index", -1)))
            if found is not None:
                image, image_kind = found
                if keep_image(image.width, image.height, image_kind, len(image.data)):
                    elements.append(Element("image", image.ocr_text, page, path(), image))
        elif node_type in TEXT_NODE_TYPES and isinstance(node.get("text"), str):
            if buffer and page != buffer_page:
                flush()
            buffer_page = page
            buffer.append(node["text"])
    flush()
    return _add_speaker_notes(elements, speaker_notes or {})


def _add_speaker_notes(elements: list[Element], notes: dict[int, str]) -> list[Element]:
    """발표자 노트를 그 슬라이드의 마지막 요소 뒤에 넣는다."""
    for page, note in sorted(notes.items()):
        if not note.strip():
            continue
        note_element = Element("text", f"발표자 노트: {note.strip()}", page)
        last = max((i for i, e in enumerate(elements) if e.page == page), default=None)
        if last is None:
            elements.append(note_element)
        else:
            elements.insert(last + 1, note_element)
    return elements


@dataclass
class PdfTable:
    """PDF 한 쪽에서 뽑은 표.

    Attributes:
        page: 쪽 번호.
        rows: 칸 글자의 2차원 목록 (첫 행은 xberg 가 머리글로 본 행).
    """

    page: int
    rows: list[list[str]]


def merge_continued_tables(tables: list[PdfTable]) -> list[PdfTable]:
    """쪽을 넘어 이어지는 표에 앞 표의 머리글을 붙인다.

    앞 쪽의 마지막 표와 바로 다음 쪽의 첫 표가 열 수가 같으면 이어지는 표로 본다. 다음 쪽 조각의 첫 행이
    앞 표의 머리글과 같으면(머리글을 반복해서 찍은 문서) 그대로 둔다.

    Args:
        tables: 쪽 순서대로의 표.

    Returns:
        머리글을 붙인 표 목록 (개수는 같다).
    """
    result: list[PdfTable] = []
    for table in tables:
        prev = result[-1] if result else None
        # 바로 앞 표가 앞 쪽에 있으면, 그 표는 앞 쪽의 마지막 표이고 이 표는 이 쪽의 첫 표다.
        if (
            prev is not None
            and table.page == prev.page + 1
            and table.rows
            and prev.rows
            and len(table.rows[0]) == len(prev.rows[0])
            and table.rows[0] != prev.rows[0]
        ):
            result.append(PdfTable(table.page, [prev.rows[0], *table.rows]))
        else:
            result.append(table)
    return result


def from_pdf(
    pages: dict[int, str],
    tables: list[PdfTable],
    images: list[tuple[int, ImageData, str | None]],
) -> list[Element]:
    """PDF 의 쪽별 본문·표·그림으로 요소 목록을 만든다.

    Args:
        pages: 쪽 번호 → 본문 (OCR 과 합친 것).
        tables: 쪽별 표 (쪽 순서).
        images: ``(쪽 번호, 원본, xberg 분류)`` 목록.

    Returns:
        쪽마다 본문 → 표 → 그림 순서의 요소 목록. 버린 그림은 들어가지 않는다.
    """
    by_page: dict[int, list[Element]] = {}
    for page, body in pages.items():
        if body.strip():
            by_page.setdefault(page, []).append(Element("text", body.strip(), page))
    for table in merge_continued_tables(tables):
        if md := table_markdown(table.rows):
            by_page.setdefault(table.page, []).append(Element("table", md, table.page))
    for page, image, image_kind in images:
        if keep_image(image.width, image.height, image_kind, len(image.data)):
            by_page.setdefault(page, []).append(Element("image", image.ocr_text, page, None, image))
    return [e for page in sorted(by_page) for e in by_page[page]]


def limit_images(elements: list[Element], max_images: int) -> list[Element]:
    """그림 요소를 앞에서부터 ``max_images`` 개만 남긴다 (설명 비용 상한).

    Args:
        elements: 요소 목록.
        max_images: 남길 그림 수.

    Returns:
        넘는 그림을 뺀 요소 목록.
    """
    kept: list[Element] = []
    count = 0
    for e in elements:
        if e.kind == "image":
            count += 1
            if count > max_images:
                continue
        kept.append(e)
    return kept
