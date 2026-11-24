r"""텍스트 형식(TXT·MD·CSV)의 수정 로직.

수정본은 고친 곳 말고는 원본과 바이트까지 같아야 한다. 그래서 원본 바이트에서 형태(BOM, 줄바꿈)를
기억해 두고, 수정은 ``\n`` 기준 글자(파서가 만드는 ``documents.content`` 와 같은 글자)에서 한 뒤,
저장할 때 원본 형태로 되돌린다.

- 줄바꿈이 ``\r\n`` 과 ``\n`` 으로 섞인 파일은 수정하지 않는다. 하나로 맞춰 저장하면 고치지 않은 줄의
  바이트까지 바뀌기 때문이다.
- 수정(``Edit``)은 ``old_text`` 가 정확히 한 번 나올 때만 적용한다 (``replace_all`` 이면 모두).
  여러 수정 중 하나라도 실패하면 아무것도 적용하지 않는다.
- CSV 는 고친 뒤 다시 읽어 따옴표 짝과 열 수가 어긋나지 않았는지 확인한다.

오류 메시지는 에이전트(LLM)에게 도구 결과로 그대로 전달되므로 영어로 쓴다.
DB 와 저장소를 모르는 순수 함수라 테스트하기 쉽다.
"""

import csv
import io
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from app.core.errors import ToolError

BOM = b"\xef\xbb\xbf"
"""UTF-8 BOM."""

SNIPPET_CHARS = 80
"""바뀐 곳 요약(``Change``)에 보여줄 전/후 글자 수의 상한."""

_LINE_PREFIX = re.compile(r"\s*\[L\d+\]")
"""보기 도구(``numbered_lines``)가 줄 앞에 붙이는 번호. 에이전트가 ``old_text`` 에 잘못 넣는 경우를 찾는다."""


class EditError(ToolError):
    """수정할 수 없다. 메시지는 에이전트에게 그대로 돌려준다 (영어).

    ``ToolError`` 라서 도구에서 그대로 올라가면 화면에 그 도구 단계가 "실패" 로 표시된다.

    Attributes:
        index: 실패한 수정의 순서 (1부터). 특정 수정과 상관없는 오류면 ``None``.
    """

    def __init__(self, message: str, index: int | None = None) -> None:
        """오류를 만든다.

        Args:
            message: 에이전트에게 보여줄 설명 (영어).
            index: 실패한 수정의 순서 (1부터).
        """
        super().__init__(message)
        self.index = index


@dataclass(frozen=True)
class TextShape:
    r"""원본 텍스트 파일의 형태. 수정본을 저장할 때 그대로 되돌린다.

    Attributes:
        bom: 원본이 UTF-8 BOM 으로 시작했는지.
        newline: 원본의 줄바꿈 (``"\n"`` 또는 ``"\r\n"``). 줄바꿈이 없는 파일은 ``"\n"``.
    """

    bom: bool
    newline: Literal["\n", "\r\n"]


@dataclass(frozen=True)
class Edit:
    """수정 하나.

    Attributes:
        old_text: 바꿀 글자. ``replace_all`` 이 아니면 문서 안에서 정확히 한 번 나와야 한다.
        new_text: 바꿔 넣을 글자. 빈 글자면 ``old_text`` 를 지운다.
        replace_all: ``old_text`` 가 나오는 곳을 모두 바꿀지.
        paragraph: DOCX 에서 찾을 문단 (예: ``"p12"``). 텍스트 형식에서는 쓰지 않는다 (무시한다).
    """

    old_text: str
    new_text: str
    replace_all: bool = False
    paragraph: str | None = None


@dataclass(frozen=True)
class Change:
    """적용한 수정 하나의 요약. 에이전트가 사용자에게 바뀐 곳을 설명할 때 쓴다.

    Attributes:
        index: 수정 순서 (1부터).
        line: 처음 바뀐 곳의 줄 번호 (1부터, 그 수정을 적용하기 직전 글자 기준).
        count: 바꾼 횟수 (``replace_all`` 이 아니면 1).
        before: 바뀌기 전 글자 (``SNIPPET_CHARS`` 까지).
        after: 바뀐 뒤 글자 (``SNIPPET_CHARS`` 까지).
    """

    index: int
    line: int
    count: int
    before: str
    after: str


@dataclass(frozen=True)
class LineView:
    r"""줄 번호를 붙인 본문 일부.

    Attributes:
        text: ``[L12] 줄 내용`` 형태의 줄들 (``\n`` 으로 잇는다). 범위 밖이면 빈 글자.
        first: 보여준 첫 줄 번호 (1부터).
        last: 보여준 마지막 줄 번호. 보여준 줄이 없으면 ``first - 1``.
        total: 전체 줄 수.
    """

    text: str
    first: int
    last: int
    total: int


def decode(data: bytes) -> tuple[str, TextShape]:
    r"""원본 바이트를 수정할 글자(``\n`` 기준)와 원본 형태로 나눈다.

    파서와 같은 규칙(``utf-8-sig`` 로 읽고 ``\r\n`` → ``\n``)이라 결과 글자는 ``documents.content`` 와 같다.
    ``\r`` 단독은 파서처럼 그대로 둔다. ``encode(*decode(data)) == data`` 가 성립한다.

    Args:
        data: 원본 파일 내용.

    Returns:
        ``(\n 기준 글자, 원본 형태)``.

    Raises:
        EditError: UTF-8 이 아니거나, 줄바꿈이 ``\r\n`` 과 ``\n`` 으로 섞여 있을 때.
    """
    bom = data.startswith(BOM)
    try:
        text = data[len(BOM) if bom else 0 :].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EditError("This document is not valid UTF-8 text, so it cannot be edited.") from exc
    crlf = text.count("\r\n")
    if crlf == 0:
        return text, TextShape(bom, "\n")
    if crlf != text.count("\n"):
        raise EditError(
            "This document mixes CRLF and LF line endings, which is not supported for editing yet."
        )
    return text.replace("\r\n", "\n"), TextShape(bom, "\r\n")


def encode(text: str, shape: TextShape) -> bytes:
    r"""수정한 글자(``\n`` 기준)를 원본 형태의 바이트로 되돌린다.

    Args:
        text: ``\n`` 기준 글자.
        shape: ``decode`` 가 기억한 원본 형태.

    Returns:
        저장할 파일 내용.
    """
    if shape.newline == "\r\n":
        text = text.replace("\n", "\r\n")
    data = text.encode("utf-8")
    return BOM + data if shape.bom else data


def _normalize(value: str) -> str:
    # 에이전트가 \r\n 을 넣어도 \n 기준 글자와 맞게 한다 (저장할 때 원본 줄바꿈으로 되돌린다).
    return value.replace("\r\n", "\n")


def _shorten(value: str) -> str:
    return value if len(value) <= SNIPPET_CHARS else value[: SNIPPET_CHARS - 1] + "…"


def _not_found(index: int, old: str) -> EditError:
    message = f"Edit {index}: old_text was not found in the document."
    if _LINE_PREFIX.match(old):
        message += " Do not include line number prefixes such as [L12] in old_text."
    else:
        message += " Copy it exactly from the document, including spaces and punctuation."
    return EditError(message, index)


def apply_edits(text: str, edits: Sequence[Edit]) -> tuple[str, list[Change]]:
    r"""수정을 순서대로 적용한다. 하나라도 실패하면 아무것도 적용하지 않는다.

    뒤의 수정은 앞의 수정을 적용한 글자에서 찾는다. 겹쳐서 나오는 경우(``"aa"`` 가 ``"aaa"`` 안에)도
    두 번 나온 것으로 본다.

    Args:
        text: ``\n`` 기준 글자.
        edits: 적용할 수정 (1개 이상).

    Returns:
        ``(수정한 글자, 바뀐 곳 요약)``.

    Raises:
        EditError: 수정이 없거나, ``old_text`` 가 비었거나 ``new_text`` 와 같거나, 찾지 못했거나,
            ``replace_all`` 없이 두 번 이상 나오거나, 결과가 빈 문서일 때.
    """
    if not edits:
        raise EditError("No edits were given.")
    changes: list[Change] = []
    for index, edit in enumerate(edits, 1):
        old, new = _normalize(edit.old_text), _normalize(edit.new_text)
        if not old:
            raise EditError(f"Edit {index}: old_text is empty.", index)
        if old == new:
            raise EditError(f"Edit {index}: old_text and new_text are the same.", index)
        first = text.find(old)
        if first < 0:
            raise _not_found(index, old)
        line = text.count("\n", 0, first) + 1
        if edit.replace_all:
            count = text.count(old)
            text = text.replace(old, new)
        elif text.find(old, first + 1) >= 0:
            raise EditError(
                f"Edit {index}: old_text appears more than once in the document. Include "
                "more surrounding text so that it matches exactly one place, or set "
                "replace_all to change every occurrence.",
                index,
            )
        else:
            count = 1
            text = text[:first] + new + text[first + len(old) :]
        changes.append(Change(index, line, count, _shorten(old), _shorten(new)))
    if not text.strip():
        raise EditError("These edits would leave the document empty.")
    return text, changes


def _csv_rows(text: str, *, strict: bool) -> list[tuple[int, int]]:
    """CSV 행마다 ``(시작 줄 번호, 열 수)``. 빈 줄은 건너뛴다.

    Raises:
        csv.Error: CSV 로 읽을 수 없을 때 (``strict`` 면 따옴표 짝이 맞지 않을 때도).
    """
    reader = csv.reader(io.StringIO(text, newline=""), strict=strict)
    rows = []
    start = 1
    for row in reader:
        if row:
            rows.append((start, len(row)))
        start = reader.line_num + 1
    return rows


def check_csv(before: str, after: str) -> None:
    r"""CSV 수정으로 따옴표 짝이 깨지거나 칸이 밀리지 않았는지 확인한다.

    두 가지를 본다.

    - 따옴표: 원본이 엄격한 규칙(``strict``)으로 읽히면 수정본도 그래야 한다. 느슨하게 읽으면
      ``"Kim,"서울`` 처럼 짝이 깨진 따옴표도 열 수가 맞는 엉뚱한 칸으로 읽혀서 열 수만으로는 못 잡는다.
    - 열 수: 원본의 모든 행이 같은 열 수 N 이면 수정본의 모든 행도 N 이어야 한다. 행을 넣거나 지워도 된다.
      원본의 열 수가 행마다 다르면 열 수는 확인하지 않는다.

    원본을 CSV 로 읽을 수 없으면 아무것도 확인하지 않는다.

    Args:
        before: 수정 전 글자 (``\n`` 기준).
        after: 수정 후 글자 (``\n`` 기준).

    Raises:
        EditError: 수정본을 CSV 로 읽을 수 없거나, 어떤 행의 열 수가 원본과 다를 때.
    """
    strict = True
    try:
        original = _csv_rows(before, strict=True)
    except csv.Error:
        strict = False
        try:
            original = _csv_rows(before, strict=False)
        except csv.Error:
            return
    hint = "Check that quotes are balanced and commas inside a value are quoted."
    try:
        rows = _csv_rows(after, strict=strict)
    except csv.Error as exc:
        raise EditError(f"The edited CSV cannot be read ({exc}). {hint}") from exc
    widths = {width for _, width in original}
    if len(widths) != 1:
        return
    (expected,) = widths
    for line, width in rows:
        if width != expected:
            raise EditError(
                f"The edited CSV row starting at line {line} has {width} columns, but every "
                f"row should have {expected}. {hint}"
            )


def check_new_csv(content: str) -> None:
    r"""새로 만드는 CSV 가 제대로 된 표인지 확인한다.

    엄격한 규칙(``strict``)으로 읽혀야 하고(따옴표 짝), 모든 행의 열 수가 같아야 한다.

    Args:
        content: CSV 글자 (``\n`` 기준).

    Raises:
        EditError: CSV 로 읽을 수 없거나 행마다 열 수가 다를 때.
    """
    hint = "Quote values that contain commas, quotes or line breaks, and double inner quotes."
    try:
        rows = _csv_rows(content, strict=True)
    except csv.Error as exc:
        raise EditError(f"The CSV content cannot be read ({exc}). {hint}") from exc
    if not rows:
        return
    expected = rows[0][1]
    for line, width in rows:
        if width != expected:
            raise EditError(
                f"The CSV row starting at line {line} has {width} columns, but the first row has "
                f"{expected}. Every row needs the same number of columns. {hint}"
            )


def numbered_lines(text: str, first: int, limit: int, max_chars: int | None = None) -> LineView:
    r"""본문의 줄에 번호를 붙여 일부를 보여준다 (``[L12] 줄 내용``).

    파일이 줄바꿈으로 끝나면 그 뒤의 빈 글자는 줄로 세지 않는다. ``max_chars`` 를 주면 결과가 그 글자 수를
    넘기 전에 멈춘다 (긴 줄이 많은 문서). 그래도 첫 줄은 길어도 보여준다.

    Args:
        text: ``\n`` 기준 글자.
        first: 보여줄 첫 줄 번호 (1부터). 1보다 작으면 1로 본다.
        limit: 보여줄 최대 줄 수 (1 이상).
        max_chars: 결과 글자 수의 상한. ``None`` 이면 줄 수로만 자른다.

    Returns:
        번호를 붙인 줄들과 범위, 전체 줄 수.
    """
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()
    first = max(first, 1)
    shown: list[str] = []
    size = 0
    for number, line in enumerate(lines[first - 1 : first - 1 + max(limit, 1)], first):
        numbered = f"[L{number}] {line}"
        size += len(numbered) + (1 if shown else 0)
        if shown and max_chars is not None and size > max_chars:
            break
        shown.append(numbered)
    return LineView(
        text="\n".join(shown),
        first=first,
        last=first + len(shown) - 1,
        total=len(lines),
    )
