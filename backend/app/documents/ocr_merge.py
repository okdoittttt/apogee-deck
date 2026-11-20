"""PDF 쪽의 OCR 결과와 텍스트 레이어를 합친다.

그림이 섞인 PDF 쪽은 쪽 전체를 OCR 한다. OCR 은 그림 속 글자까지 읽고 줄 순서도 화면 배치를 따르지만,
한국어 띄어쓰기를 자주 틀리고 글자를 가끔 잘못 읽는다. 텍스트 레이어는 글자와 띄어쓰기가 정확하지만,
그림 속 글자가 없고 슬라이드처럼 배치가 복잡하면 순서가 뒤섞인다.

그래서 **줄 순서와 그림 속 글자는 OCR 에서, 글자는 텍스트 레이어에서** 가져온다. OCR 의 각 줄을 공백을
빼고 비교해서 텍스트 레이어에 같은 글자 묶음이 있으면 텍스트 레이어의 원문(띄어쓰기 포함)으로 바꾸고,
없는 부분(그림 속 글자 등)은 OCR 결과를 그대로 둔다.

비교할 때는 공백을 무시하고, 모양만 다른 문자(따옴표, 가운뎃점, 대시 등)를 같은 문자로 본다.
"""

import re
import unicodedata
from dataclasses import dataclass

MIN_MATCH_CHARS = 3
"""텍스트 레이어와 맞춰 볼 최소 글자 수(공백 제외). 이보다 짧은 조각은 우연히 맞을 수 있어서 OCR 을 그대로 쓴다."""

MAX_GAP_CHARS = 2
"""OCR 이 빠뜨리거나 더 읽은 글자로 볼 최대 길이. 앞뒤 조각이 텍스트 레이어에서 이만큼만 떨어져 있으면 이어 붙인다."""

_EQUIVALENT = str.maketrans(
    {
        "“": '"',
        "”": '"',
        "„": '"',
        "‘": "'",
        "’": "'",
        "`": "'",
        "•": "·",
        "∙": "·",
        "・": "·",
        "‧": "·",
        "–": "-",
        "—": "-",
        "−": "-",
        "‐": "-",
        "∑": "Σ",
        "×": "x",
        "≤": "<",
        "≥": ">",
    }
)
"""OCR 이 비슷한 모양의 다른 문자로 읽는 경우. 비교할 때만 쓰고 출력은 텍스트 레이어 원문을 쓴다."""

_SPACES = re.compile(r"\s+")


@dataclass(frozen=True)
class MergeStats:
    """합친 결과 통계 (품질 확인용).

    Attributes:
        matched_chars: 텍스트 레이어에서 가져온 글자 수 (공백 제외, OCR 기준).
        total_chars: OCR 결과의 전체 글자 수 (공백 제외).
    """

    matched_chars: int
    total_chars: int


@dataclass(frozen=True)
class _Index:
    """공백을 뺀 비교용 문자열과, 각 글자가 원문의 몇 번째 문자인지."""

    chars: str
    positions: list[int]


@dataclass(frozen=True)
class _Segment:
    """OCR 한 줄의 조각 ``[start, end)``. 텍스트 레이어와 맞으면 그 위치 ``[source, source_end)`` 도 둔다."""

    start: int
    end: int
    source: int | None = None
    source_end: int = 0


def _canonical(text: str) -> str:
    return unicodedata.normalize("NFKC", text).translate(_EQUIVALENT).casefold()


def _index(text: str) -> _Index:
    chars: list[str] = []
    positions: list[int] = []
    for i, char in enumerate(text):
        if char.isspace():
            continue
        for c in _canonical(char):
            chars.append(c)
            positions.append(i)
    return _Index("".join(chars), positions)


def _longest_match(layer: str, line: str, start: int, cursor: int) -> tuple[int, int]:
    """``line[start:]`` 의 앞부분 중 텍스트 레이어에 있는 가장 긴 조각을 찾는다.

    부분 문자열이 있으면 그보다 짧은 앞부분도 항상 있으므로 길이를 이분 탐색한다. 같은 조각이 여러 번
    나오면 직전 조각 다음(``cursor`` 이후)에 있는 것을 고른다.

    Returns:
        ``(길이, 텍스트 레이어 위치)``. 못 찾으면 ``(0, -1)``.
    """
    lo, hi, best = MIN_MATCH_CHARS, len(line) - start, 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if line[start : start + mid] in layer:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    if not best:
        return 0, -1
    piece = line[start : start + best]
    found = layer.find(piece, cursor)
    return best, found if found >= 0 else layer.find(piece)


def _split(layer: str, line: str, cursor: int) -> tuple[list[_Segment], int]:
    """OCR 한 줄을 텍스트 레이어와 맞는 조각과 맞지 않는 조각으로 나눈다."""
    segments: list[_Segment] = []
    i = 0
    while i < len(line):
        length, found = _longest_match(layer, line, i, cursor)
        if length:
            segments.append(_Segment(i, i + length, found, found + length))
            cursor = found + length
            i += length
        elif segments and segments[-1].source is None:
            segments[-1] = _Segment(segments[-1].start, i + 1)
            i += 1
        else:
            segments.append(_Segment(i, i + 1))
            i += 1
    return _bridge(segments), cursor


def _bridge(segments: list[_Segment]) -> list[_Segment]:
    """텍스트 레이어에서 거의 붙어 있는 두 조각을 하나로 잇는다.

    OCR 이 따옴표 하나를 빠뜨리거나 글자 하나를 더 읽어서 조각이 끊긴 경우다. 이을 때는 사이의 OCR 글자를
    버리고 텍스트 레이어의 원문을 쓴다.
    """
    merged: list[_Segment] = []
    for seg in segments:
        merged.append(seg)
        if len(merged) < 2 or seg.source is None:
            continue
        # [맞음] 다음 바로 [맞음], 또는 [맞음][짧은 OCR 조각][맞음]
        prev, gap = merged[-2], None
        if prev.source is None and len(merged) >= 3:
            prev, gap = merged[-3], prev
        if prev.source is None or (gap and gap.end - gap.start > MAX_GAP_CHARS):
            continue
        if 0 <= seg.source - prev.source_end <= MAX_GAP_CHARS:
            joined = _Segment(prev.start, seg.end, prev.source, seg.source_end)
            del merged[-(3 if gap else 2) :]
            merged.append(joined)
    return merged


def merge_page(text_layer: str, ocr_text: str) -> tuple[str, MergeStats]:
    """한 쪽의 OCR 결과를 텍스트 레이어의 원문으로 고쳐 쓴다.

    Args:
        text_layer: 그 쪽의 텍스트 레이어 (OCR 없이 추출한 글자).
        ocr_text: 그 쪽 전체를 OCR 한 결과.

    Returns:
        OCR 의 줄 순서를 따르되 텍스트 레이어에 있는 글자는 원문으로 바꾼 본문, 그리고 통계.
    """
    layer = _index(text_layer)
    cursor = 0
    lines: list[str] = []
    matched = total = 0
    for raw in ocr_text.splitlines():
        line = _index(raw)
        if not line.chars:
            continue
        total += len(line.chars)
        if len(line.chars) < MIN_MATCH_CHARS:
            lines.append(raw.strip())
            continue
        segments, cursor = _split(layer.chars, line.chars, cursor)
        parts: list[str] = []
        prev_raw_end: int | None = None
        for seg in segments:
            raw_start = line.positions[seg.start]
            raw_end = line.positions[seg.end - 1]
            if seg.source is None:
                piece = raw[raw_start : raw_end + 1]
            else:
                first = layer.positions[seg.source]
                last = layer.positions[seg.source_end - 1]
                piece = text_layer[first : last + 1]
                piece = _SPACES.sub(" ", piece)
                matched += seg.end - seg.start
            if prev_raw_end is not None and raw[prev_raw_end + 1 : raw_start].isspace():
                parts.append(" ")
            parts.append(piece)
            prev_raw_end = raw_end
        lines.append("".join(parts).strip())
    return "\n".join(lines), MergeStats(matched, total)


def ocr_only_lines(text_layer: str, ocr_text: str) -> list[str]:
    """텍스트 레이어에 없는 OCR 줄 (그림 속 글자 등).

    공백을 빼고 모양만 다른 문자를 같은 문자로 본 뒤, 텍스트 레이어에 그대로 들어 있지 않은 줄만 고른다.

    Args:
        text_layer: 쪽의 텍스트 레이어.
        ocr_text: 같은 쪽의 OCR 결과.

    Returns:
        OCR 에만 있는 줄 (OCR 순서).
    """
    layer = _SPACES.sub("", _canonical(text_layer))
    lines = []
    for line in ocr_text.splitlines():
        key = _SPACES.sub("", _canonical(line))
        if key and key not in layer:
            lines.append(line.strip())
    return lines
