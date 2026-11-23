"""PDF 쪽 배치 읽기: pdfium 텍스트 레이어를 줄 → 문단 → 제목으로 나누고, 그림 자리를 찾는다.

xberg 기본 추출은 "칸 이름 | 값" 이 나란한 표 같은 배치를 여러 단으로 보고 단별로 읽어서 줄 순서가 틀어졌다.
pdfium 은 PDF 에 그린 순서를 따라서 사람이 읽는 순서와 맞아서, PDF 본문은 pdfium 텍스트 레이어로 만든다.

- **줄**: pdfium 이 만든 줄바꿈으로 나눈다. 줄마다 위아래 위치와 대표 글자 크기(글자 수가 가장 많은 크기)를 둔다.
  같은 높이에 나란한 줄(표의 칸)은 한 줄로 잇는다.
- **문단**: 이어지는 줄은 같은 문단이다. 줄 간격이 문단 안의 줄 간격의 1.5배(본문 글자 크기를 넘지 않게)를
  넘거나, 글자 크기가 5% 넘게 다르거나, 다음 줄이 앞 줄보다 위에 있으면(단이 바뀜) 새 문단을 시작한다.
- **제목**: 본문(글자 수가 가장 많은 크기)보다 1.15배 이상 크고, 2줄·80자 이하인 문단이다. 클수록 위 단계다.
  제목은 본문 문단이 되지 않고, 뒤따르는 문단의 제목 경로가 된다 (쪽을 넘어 이어진다, Office·한글과 같은 방식).
- **그림**: 그림의 세로 가운데보다 아래에 있는 첫 문단(읽는 순서) 앞에 둔다. 문단 중간을 자르지 않는다.

이 모듈은 파싱 프로세스(``app.documents.worker``)에서만 쓴다. pdfium 은 함수 안에서 불러온다.
"""

import itertools
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from app.documents.elements import PdfBlock

logger = logging.getLogger(__name__)

# 같은 문단으로 볼 줄 간격: 문단 안의 줄 간격의 이 배수까지 (본문 글자 크기를 넘지 않는다).
PARAGRAPH_GAP_RATIO = 1.5
# 이만큼 넘게 글자 크기가 다르면 다른 문단이다 (본문과 제목, 본문과 표). 크기는 0.5 단위로 반올림한 값이라
# 본문 10.5 와 표 9.5(약 9.5% 차이)도 나뉘어야 한다.
SIZE_CHANGE_RATIO = 0.05
# 제목: 본문 크기의 이 배수 이상, 이 줄 수·글자 수 이하.
HEADING_SIZE_RATIO = 1.15
HEADING_MAX_LINES = 2
HEADING_MAX_CHARS = 80
# 같은 줄(표의 나란한 칸)로 볼 위치 차이: 글자 크기의 이 배수까지.
SAME_ROW_RATIO = 0.3

_JUNK = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


@dataclass(frozen=True)
class Line:
    """텍스트 레이어의 줄 하나.

    Attributes:
        text: 줄 글자.
        top: 위쪽 위치 (PDF 좌표, 위로 갈수록 크다).
        bottom: 아래쪽 위치.
        size: 대표 글자 크기 (0.5 단위).
    """

    text: str
    top: float
    bottom: float
    size: float


@dataclass(frozen=True)
class Paragraph:
    r"""이어지는 줄을 묶은 문단.

    Attributes:
        text: 문단 글자 (줄은 ``\\n`` 으로 잇는다).
        top: 위쪽 위치.
        bottom: 아래쪽 위치.
        size: 대표 글자 크기 (첫 줄).
        lines: 줄 수.
    """

    text: str
    top: float
    bottom: float
    size: float
    lines: int = 1

    @property
    def middle(self) -> float:
        """세로 가운데."""
        return (self.top + self.bottom) / 2


@dataclass(frozen=True)
class PageImage:
    """쪽 안의 그림 위치 (pdfium 그림 객체).

    Attributes:
        width: 픽셀 너비.
        height: 픽셀 높이.
        middle: 세로 가운데 (PDF 좌표).
    """

    width: int
    height: int
    middle: float


@dataclass
class RawPage:
    """pdfium 에서 읽은 쪽: 줄과 그림 위치.

    Attributes:
        lines: 줄 (PDF 에 그린 순서).
        images: 그림 위치.
    """

    lines: list[Line] = field(default_factory=list)
    images: list[PageImage] = field(default_factory=list)


@dataclass(frozen=True)
class FigureSlot:
    """그림이 들어갈 자리.

    Attributes:
        width: 픽셀 너비 (xberg 그림과 짝을 지을 때 쓴다).
        height: 픽셀 높이.
        slot: 이 번호의 문단 앞 (문단 수면 마지막 문단 뒤).
        heading: 그 자리의 제목 경로.
    """

    width: int
    height: int
    slot: int
    heading: str | None


@dataclass(frozen=True)
class PdfPage:
    """문단과 제목, 그림 자리로 나눈 PDF 한 쪽.

    Attributes:
        blocks: 본문 문단 (제목 경로 포함, 제목 문단은 들어가지 않는다).
        images: 그림 자리.
        heading: 쪽이 시작할 때의 제목 경로 (앞 쪽에서 이어진 것).
        text: 쪽 본문 (문단 사이 빈 줄, 제목은 ``#`` 줄). 대화 첨부 본문과 스캔 쪽 판단에 쓴다.
    """

    blocks: list[PdfBlock]
    images: list[FigureSlot]
    heading: str | None
    text: str


def body_size(pages: list[RawPage]) -> float:
    """본문 글자 크기: 문서에서 글자 수가 가장 많은 크기. 글자가 없으면 0."""
    counts: Counter[float] = Counter()
    for page in pages:
        for line in page.lines:
            counts[line.size] += sum(not c.isspace() for c in line.text)
    return counts.most_common(1)[0][0] if counts else 0.0


def line_gap(pages: list[RawPage], size: float) -> float:
    """문단 안의 줄 간격: 본문 크기 줄끼리의 간격(위 줄 아래 ~ 아래 줄 위) 중 아래 사분위수.

    본문 줄 사이에는 문단 안의 간격(작다)과 문단 사이의 간격(크다)이 섞여 있어서, 중앙값은 문단이 짧은 문서에서
    문단 사이 간격으로 끌려 올라간다. 작은 쪽(아래 사분위수)을 쓴다.

    Args:
        pages: 쪽.
        size: 본문 글자 크기.

    Returns:
        간격. 본문 줄이 이어지는 곳이 없으면 글자 크기의 0.6 배.
    """
    gaps = sorted(
        a.bottom - b.top
        for page in pages
        for a, b in itertools.pairwise(page.lines)
        if a.size == size == b.size and a.bottom - b.top > 0
    )
    return gaps[len(gaps) // 4] if gaps else size * 0.6


def gap_limit(pages: list[RawPage], size: float) -> float:
    """같은 문단으로 볼 최대 줄 간격: 문단 안의 줄 간격의 1.5 배, 본문 글자 크기를 넘지 않는다.

    한 줄짜리 문단만 있는 문서는 문단 안의 줄 간격이 없어서 문단 사이 간격을 그것으로 잘못 볼 수 있다.
    본문 크기로 상한을 두면 그런 문서에서도 문단이 하나로 합쳐지지 않는다.
    """
    limit = line_gap(pages, size) * PARAGRAPH_GAP_RATIO
    return min(limit, size) if size > 0 else limit


def merge_rows(lines: list[Line]) -> list[Line]:
    """같은 높이에 나란히 놓인 줄(표의 칸 등)을 한 줄로 잇는다."""
    merged: list[Line] = []
    for line in lines:
        prev = merged[-1] if merged else None
        if prev and abs(prev.top - line.top) <= SAME_ROW_RATIO * max(prev.size, line.size):
            merged[-1] = Line(
                f"{prev.text} {line.text}",
                max(prev.top, line.top),
                min(prev.bottom, line.bottom),
                prev.size,
            )
        else:
            merged.append(line)
    return merged


def group_paragraphs(lines: list[Line], gap_limit: float) -> list[Paragraph]:
    """이어지는 줄을 문단으로 묶는다.

    Args:
        lines: 줄 (읽는 순서, ``merge_rows`` 를 거친 것).
        gap_limit: 같은 문단으로 볼 최대 줄 간격.

    Returns:
        문단 (읽는 순서).
    """
    paragraphs: list[Paragraph] = []
    for line in lines:
        prev = paragraphs[-1] if paragraphs else None
        gap = prev.bottom - line.top if prev else 0.0
        if (
            prev is not None
            and 0 <= gap <= gap_limit
            and abs(prev.size - line.size) <= SIZE_CHANGE_RATIO * max(prev.size, line.size)
        ):
            paragraphs[-1] = Paragraph(
                f"{prev.text}\n{line.text}", prev.top, line.bottom, prev.size, prev.lines + 1
            )
        else:
            paragraphs.append(Paragraph(line.text, line.top, line.bottom, line.size))
    return paragraphs


def is_heading(paragraph: Paragraph, size: float) -> bool:
    """제목 문단인지: 본문보다 충분히 크고 짧다 (표 머리글처럼 굵기만 한 작은 글자는 아니다)."""
    return (
        size > 0
        and paragraph.size >= size * HEADING_SIZE_RATIO
        and paragraph.lines <= HEADING_MAX_LINES
        and len(paragraph.text) <= HEADING_MAX_CHARS
    )


def build_pages(raw: dict[int, RawPage]) -> dict[int, PdfPage]:
    """쪽별 줄과 그림 위치로 문단·제목·그림 자리를 만든다. 제목 경로는 쪽을 넘어 이어진다.

    Args:
        raw: 쪽 번호 → pdfium 에서 읽은 쪽.

    Returns:
        쪽 번호 → 문단으로 나눈 쪽.
    """
    pages = [raw[n] for n in sorted(raw)]
    size = body_size(pages)
    limit = gap_limit(pages, size)
    per_page = {n: group_paragraphs(merge_rows(raw[n].lines), limit) for n in sorted(raw)}
    heading_sizes = sorted(
        {p.size for paragraphs in per_page.values() for p in paragraphs if is_heading(p, size)},
        reverse=True,
    )
    level = {s: i for i, s in enumerate(heading_sizes)}

    stack: list[tuple[int, str]] = []  # (단계, 제목)

    def path() -> str | None:
        return " > ".join(text for _, text in stack) or None

    out: dict[int, PdfPage] = {}
    for number in sorted(raw):
        start = path()
        blocks: list[PdfBlock] = []
        lines: list[str] = []
        # 문단 순서마다: 그 앞에 둘 문단 수와 제목 경로 (그림 자리 계산용)
        marks: list[tuple[Paragraph, int, str | None]] = []
        for paragraph in per_page[number]:
            marks.append((paragraph, len(blocks), path()))
            if is_heading(paragraph, size):
                depth = level[paragraph.size]
                while stack and stack[-1][0] >= depth:
                    stack.pop()
                title = " ".join(paragraph.text.split())
                stack.append((depth, title))
                lines.append(f"{'#' * (depth + 1)} {title}")
            else:
                blocks.append(PdfBlock(paragraph.text, path()))
                lines.append(paragraph.text)
        images = []
        for image in raw[number].images:
            slot, heading = len(blocks), path()
            for paragraph, before, at in marks:
                if paragraph.middle < image.middle:
                    slot, heading = before, at
                    break
            images.append(FigureSlot(image.width, image.height, slot, heading))
        images.sort(key=lambda f: f.slot)  # 같은 자리면 PDF 에 그린 순서
        out[number] = PdfPage(blocks, images, start, "\n\n".join(lines))
    return out


def figure_slots(figures: list[Any], pages: dict[int, PdfPage]) -> dict[int, FigureSlot]:
    """Xberg 그림마다 쪽 안의 자리를 찾는다. 같은 쪽에서 픽셀 크기가 같은 pdfium 그림과 짝을 짓는다.

    크기가 같은 그림이 여럿이면 문서 순서대로 짝을 짓는다.

    Args:
        figures: 남길 xberg 그림 (문서 순서, ``page_number`` / ``width`` / ``height``).
        pages: 쪽 번호 → 쪽. 자리를 쓰지 않을 쪽(스캔 쪽 등)은 넣지 않는다.

    Returns:
        그림 객체의 ``id`` → 자리. 짝을 못 찾은 그림은 들어가지 않는다 (쪽 끝에 둔다).
    """
    used: set[tuple[int, int]] = set()
    slots: dict[int, FigureSlot] = {}
    for img in figures:
        page = pages.get(img.page_number)
        if page is None:
            continue
        for index, slot in enumerate(page.images):
            key = (img.page_number, index)
            if key not in used and (slot.width, slot.height) == (img.width, img.height):
                used.add(key)
                slots[id(img)] = slot
                break
    return slots


def _round_size(size: float) -> float:
    return round(size * 2) / 2


def _read_page(page: Any, raw: Any) -> RawPage:
    """Pdfium 쪽에서 줄과 그림 위치를 읽는다."""
    textpage = page.get_textpage()
    try:
        lines: list[Line] = []
        chars: list[str] = []
        top = bottom = None
        sizes: Counter[float] = Counter()

        def end_line() -> None:
            nonlocal top, bottom
            text = _JUNK.sub("", "".join(chars)).strip()
            if text and top is not None and bottom is not None:
                lines.append(Line(text, top, bottom, sizes.most_common(1)[0][0]))
            chars.clear()
            sizes.clear()
            top = bottom = None

        for index in range(textpage.count_chars()):
            code = raw.FPDFText_GetUnicode(textpage.raw, index)
            if code == 10:  # pdfium 이 넣은 줄바꿈
                end_line()
                continue
            if code == 13:
                continue
            char = chr(code)
            chars.append(char)
            _, low, _, high = textpage.get_charbox(index)
            if high > low and not char.isspace():
                top = high if top is None else max(top, high)
                bottom = low if bottom is None else min(bottom, low)
                sizes[_round_size(raw.FPDFText_GetFontSize(textpage.raw, index))] += 1
        end_line()
        images = []
        for obj in page.get_objects(filter=[raw.FPDF_PAGEOBJ_IMAGE], max_depth=3):
            _, low, _, high = obj.get_bounds()
            width, height = obj.get_px_size()
            images.append(PageImage(width, height, (low + high) / 2))
        return RawPage(lines, images)
    finally:
        textpage.close()


def read_pages(data: bytes) -> dict[int, PdfPage] | None:
    """PDF 를 pdfium 으로 읽어 쪽마다 문단·제목·그림 자리를 만든다.

    Args:
        data: PDF 바이트.

    Returns:
        쪽 번호(1부터) → 쪽. PDF 를 열지 못하면 ``None`` (호출하는 쪽이 xberg 결과를 쓴다).
    """
    import pypdfium2 as pdfium  # 파싱 프로세스에서만 불러온다
    import pypdfium2.raw as pdfium_raw

    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        logger.info("pdfium could not open the PDF, using xberg text: %s", exc)
        return None
    try:
        raw: dict[int, RawPage] = {}
        for index in range(len(pdf)):
            page = pdf[index]
            try:
                raw[index + 1] = _read_page(page, pdfium_raw)
            finally:
                page.close()
        return build_pages(raw)
    finally:
        pdf.close()
