"""요소(본문·표·그림)를 검색용 청크로 묶거나 나눈다.

규칙:

- 본문: 같은 제목 아래의 이어지는 본문 요소를 목표 크기(``target``)까지 묶는다. 제목이 바뀌면 새 청크를 연다.
  최대 크기(``max_tokens``)를 넘는 본문은 문단 → 줄 → 문장 → 글자 순으로 잘라 나누고, 나눈 청크끼리는
  앞 청크의 끝(``overlap``)을 다음 청크 앞에 겹쳐서 경계에 걸친 내용도 찾을 수 있게 한다.
- 표: 따로 청크가 된다. 크면 행 단위로 나누고, 나눈 조각마다 머리글 행을 다시 붙인다.
- 그림: 설명 하나가 청크 하나가 된다 (원본 그림을 보여줄 수 있게 요소 ID 를 남긴다).

크기는 모두 추정 토큰 수(``estimate_tokens``)로 잰다. DB 와 임베딩을 모르는 순수 함수라 테스트하기 쉽다.
"""

import itertools
import re
import uuid
from dataclasses import dataclass

from app.documents.reading import estimate_tokens
from app.llm.tokens import ASCII_CHARS_PER_TOKEN, NON_ASCII_CHARS_PER_TOKEN


@dataclass(frozen=True)
class SourceElement:
    """청크를 만들 요소 하나 (``DocumentElement`` 에서 필요한 값만).

    Attributes:
        id: 요소 ID.
        kind: ``text`` | ``table`` | ``image``.
        content: 본문, 표 Markdown, 그림 설명.
        page: 쪽 번호.
        heading: 제목 경로.
    """

    id: uuid.UUID
    kind: str
    content: str
    page: int | None = None
    heading: str | None = None


@dataclass(frozen=True)
class Chunk:
    """검색 단위 하나.

    Attributes:
        kind: ``text`` | ``table`` | ``image``.
        content: 청크 본문.
        page_start: 첫 쪽 번호.
        page_end: 마지막 쪽 번호.
        heading: 제목 경로.
        image_element_id: 그림 청크의 그림 요소 ID.
        token_count: 추정 토큰 수.
        element_ids: 이 청크를 만든 요소들의 ID (문서 보기 패널에서 근거 위치를 강조할 때 쓴다).
    """

    kind: str
    content: str
    page_start: int | None
    page_end: int | None
    heading: str | None
    image_element_id: uuid.UUID | None
    token_count: int
    element_ids: tuple[uuid.UUID, ...] = ()


@dataclass(frozen=True)
class ChunkSizes:
    """청크 크기 기준 (추정 토큰 수).

    Attributes:
        target: 작은 본문을 묶을 목표 크기.
        max_tokens: 청크 최대 크기.
        overlap: 긴 본문을 나눌 때 겹치는 분량.
    """

    target: int = 500
    max_tokens: int = 800
    overlap: int = 80


def make_chunks(elements: list[SourceElement], sizes: ChunkSizes | None = None) -> list[Chunk]:
    """요소 목록(문서 순서)으로 청크 목록을 만든다.

    Args:
        elements: 문서 순서대로의 요소. 내용이 빈 요소는 건너뛴다.
        sizes: 크기 기준. 없으면 기본값.

    Returns:
        문서 순서대로의 청크.
    """
    sizes = sizes or ChunkSizes()
    chunks: list[Chunk] = []
    buffer: list[SourceElement] = []

    def flush() -> None:
        if buffer:
            text = "\n\n".join(e.content.strip() for e in buffer)
            pages = [e.page for e in buffer if e.page is not None]
            chunks.append(
                _chunk(
                    "text",
                    text,
                    min(pages, default=None),
                    max(pages, default=None),
                    buffer[0].heading,
                    element_ids=tuple(e.id for e in buffer),
                )
            )
            buffer.clear()

    for element in elements:
        content = element.content.strip()
        if not content:
            continue
        tokens = estimate_tokens(content)
        if element.kind == "image":
            flush()
            chunks.append(
                _chunk(
                    "image",
                    content,
                    element.page,
                    element.page,
                    element.heading,
                    element.id,
                    (element.id,),
                )
            )
        elif element.kind == "table":
            flush()
            for part in split_table(content, sizes.max_tokens):
                chunks.append(
                    _chunk(
                        "table",
                        part,
                        element.page,
                        element.page,
                        element.heading,
                        None,
                        (element.id,),
                    )
                )
        elif tokens > sizes.max_tokens:
            flush()
            for part in split_text(content, sizes):
                chunks.append(
                    _chunk(
                        "text",
                        part,
                        element.page,
                        element.page,
                        element.heading,
                        None,
                        (element.id,),
                    )
                )
        else:
            buffered = sum(estimate_tokens(e.content) for e in buffer)
            if buffer and (
                element.heading != buffer[0].heading or buffered + tokens > sizes.target
            ):
                flush()
            buffer.append(element)
    flush()
    return chunks


def _chunk(
    kind: str,
    content: str,
    page_start: int | None,
    page_end: int | None,
    heading: str | None,
    image_element_id: uuid.UUID | None = None,
    element_ids: tuple[uuid.UUID, ...] = (),
) -> Chunk:
    return Chunk(
        kind,
        content,
        page_start,
        page_end,
        heading,
        image_element_id,
        estimate_tokens(content),
        element_ids,
    )


# 큰 단위부터 잘라 본다: 문단, 줄, 문장 끝(마침표·물음표·느낌표 뒤 공백), 공백.
_SEPARATORS = (
    re.compile(r"\n\s*\n"),
    re.compile(r"\n"),
    re.compile(r"(?<=[.!?。])\s+"),
    re.compile(r"\s+"),
)


def split_text(text: str, sizes: ChunkSizes) -> list[str]:
    """긴 본문을 ``max_tokens`` 이하의 조각으로 나누고, 조각끼리 ``overlap`` 만큼 겹친다.

    Args:
        text: 본문.
        sizes: 크기 기준.

    Returns:
        조각 목록. 각 조각은 ``max_tokens`` 를 넘지 않는다 (겹친 부분 포함).
    """
    # 겹칠 자리를 남겨 두고 나눈다.
    room = max(1, sizes.max_tokens - sizes.overlap)
    units = _split_units(text.strip(), min(room, sizes.target))
    parts: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for unit in units:
        unit_tokens = estimate_tokens(unit)
        if current and current_tokens + unit_tokens > min(room, sizes.target):
            parts.append(" ".join(current))
            current, current_tokens = [], 0
        current.append(unit)
        current_tokens += unit_tokens
    if current:
        parts.append(" ".join(current))
    if sizes.overlap <= 0:
        return parts
    return [parts[0]] + [
        f"{tail} {part}" if (tail := _tail(prev, sizes.overlap)) else part
        for prev, part in itertools.pairwise(parts)
    ]


def _split_units(text: str, limit: int, level: int = 0) -> list[str]:
    """``limit`` 토큰 이하의 조각으로 자른다. 큰 구분자부터 써 보고, 그래도 크면 더 작은 구분자로."""
    if estimate_tokens(text) <= limit:
        return [text] if text else []
    if level >= len(_SEPARATORS):
        return _hard_split(text, limit)
    units: list[str] = []
    for piece in _SEPARATORS[level].split(text):
        piece = piece.strip()
        if piece:
            units.extend(_split_units(piece, limit, level + 1))
    return units


def _char_tokens(ch: str) -> float:
    return 1 / (NON_ASCII_CHARS_PER_TOKEN if ord(ch) > 127 else ASCII_CHARS_PER_TOKEN)


def _hard_split(text: str, limit: int) -> list[str]:
    """구분자가 없는 긴 글자(띄어쓰기 없는 문자열 등)를 글자 단위로 자른다."""
    parts: list[str] = []
    start, tokens = 0, 0.0
    for i, ch in enumerate(text):
        tokens += _char_tokens(ch)
        if tokens > limit:
            parts.append(text[start:i])
            start, tokens = i, _char_tokens(ch)
    parts.append(text[start:])
    return [p for p in parts if p]


def _tail(text: str, overlap: int) -> str:
    """본문 끝에서 ``overlap`` 토큰 이하를 단어 경계로 잘라 돌려준다."""
    tokens = 0.0
    start = len(text)
    for i in range(len(text) - 1, -1, -1):
        tokens += _char_tokens(text[i])
        if tokens > overlap:
            break
        start = i
    tail = text[start:]
    # 단어 중간에서 시작하지 않게 첫 공백 뒤부터 쓴다 (공백이 없으면 그대로).
    if start > 0 and not text[start - 1].isspace() and (cut := tail.find(" ")) != -1:
        tail = tail[cut + 1 :]
    return tail.strip()


def split_table(markdown: str, max_tokens: int) -> list[str]:
    """큰 Markdown 표를 행 단위로 나누고, 조각마다 머리글(머리글 행 + 구분선)을 붙인다.

    Args:
        markdown: Markdown 표 (``elements.table_markdown`` 결과).
        max_tokens: 조각 최대 크기. 행 하나가 이보다 크면 그 행만으로 한 조각이 된다.

    Returns:
        표 조각 목록. 작은 표는 그대로 하나.
    """
    if estimate_tokens(markdown) <= max_tokens:
        return [markdown]
    lines = markdown.splitlines()
    header, rows = lines[:2], lines[2:]
    header_tokens = estimate_tokens("\n".join(header))
    parts: list[str] = []
    current: list[str] = []
    current_tokens = header_tokens
    for row in rows:
        row_tokens = estimate_tokens(row) + 1
        if current and current_tokens + row_tokens > max_tokens:
            parts.append("\n".join(header + current))
            current, current_tokens = [], header_tokens
        current.append(row)
        current_tokens += row_tokens
    if current:
        parts.append("\n".join(header + current))
    return parts


def embedding_text(filename: str, chunk: Chunk) -> str:
    """임베딩할 글자: 청크 앞에 문서 이름·제목 경로·쪽을 붙여 맥락을 준다 (contextual chunk header).

    Args:
        filename: 문서 이름.
        chunk: 청크.

    Returns:
        임베딩 입력.
    """
    where = [f"문서: {filename}"]
    if chunk.heading:
        where.append(f"제목: {chunk.heading}")
    if chunk.page_start is not None:
        pages = (
            f"{chunk.page_start}"
            if chunk.page_end in (None, chunk.page_start)
            else f"{chunk.page_start}-{chunk.page_end}"
        )
        where.append(f"쪽: {pages}")
    if chunk.kind == "image":
        where.append("그림 설명")
    elif chunk.kind == "table":
        where.append("표")
    return " | ".join(where) + "\n\n" + chunk.content
