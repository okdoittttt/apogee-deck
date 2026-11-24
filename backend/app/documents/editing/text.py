r"""텍스트 형식(TXT·MD·CSV)의 수정 로직.

수정본은 고친 곳 말고는 원본과 바이트까지 같아야 한다. 그래서 원본 바이트에서 형태(BOM, 줄바꿈)를
기억해 두고, 수정은 ``\n`` 기준 글자(파서가 만드는 ``documents.content`` 와 같은 글자)에서 한 뒤,
저장할 때 원본 형태로 되돌린다.

- 줄바꿈이 ``\r\n`` 과 ``\n`` 으로 섞인 파일은 수정하지 않는다. 하나로 맞춰 저장하면 고치지 않은 줄의
  바이트까지 바뀌기 때문이다.
- 수정(``Edit``)은 ``old_text`` 가 정확히 한 번 나올 때만 적용한다 (``replace_all`` 이면 모두).
  적용할 수 없는 수정은 건너뛰고 이유를 돌려준다 (``apply_some``). ``apply_edits`` 는 하나라도 실패하면
  아무것도 적용하지 않는다.
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

_URL = re.compile(r"(?:https?://|www\.)[!-~]+", re.IGNORECASE)
"""글자 안의 웹 주소. ``replace_all`` 이 주소 안을 바꿨으면 결과에 알린다.

ASCII 만 주소로 본다. 한국어 문서는 주소 바로 뒤에 조사를 붙여 쓰는 일이 많아서(``https://a.com에서``)
공백까지를 주소로 보면 뒤 글자까지 주소로 잘못 본다.
"""

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


EDIT_OPS = frozenset(
    {
        "replace",
        "merge",
        "insert_paragraph",
        "delete_paragraph",
        "move_paragraph",
        "insert_row",
        "delete_row",
        "insert_column",
        "delete_column",
        "delete_table",
        "insert_markdown",
        "set_style",
        "accept_changes",
        "reject_changes",
        "comment",
        "format",
    }
)
"""수정 종류. 텍스트 형식은 ``replace`` 만, 나머지는 DOCX 에서만 쓴다."""


@dataclass(frozen=True)
class TextFormat:
    """``format`` 수정에서 바꿀 글자 서식. ``None`` 인 항목은 그대로 둔다.

    Attributes:
        bold: 굵게 넣기(``True``)·빼기(``False``).
        italic: 기울임 넣기·빼기.
        underline: 밑줄 넣기·빼기.
        strike: 취소선 넣기·빼기.
        color: 글자색. 이름(``"red"``, ``"빨강"``)이나 ``"#C00000"``, 기본색은 ``"auto"``.
        highlight: 형광펜 색 (Word 형광펜 이름이나 ``"노랑"`` 같은 한국어). 빼기는 ``"none"``.
    """

    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    strike: bool | None = None
    color: str | None = None
    highlight: str | None = None

    def is_empty(self) -> bool:
        """바꿀 서식이 하나도 없는지."""
        return all(getattr(self, name) is None for name in self.__dataclass_fields__)


@dataclass(frozen=True)
class Edit:
    r"""수정 하나.

    ``op`` 마다 쓰는 필드가 다르다 (``works/02.docx-advencement.md`` 9.1절).

    Attributes:
        old_text: 바꿀 글자. ``replace_all`` 이 아니면 문서 안에서 정확히 한 번 나와야 한다.
        new_text: 바꿔 넣을 글자. 빈 글자면 ``old_text`` 를 지운다. DOCX 에서는 ``\n`` 에서 문단을 나누고,
            ``insert_paragraph`` 에서는 넣을 문단의 글자, ``comment`` 에서는 메모 내용이다.
        replace_all: ``old_text`` 가 나오는 곳을 모두 바꿀지.
        paragraph: DOCX 에서 수정할(또는 기준이 되는) 문단 (예: ``"p12"``). 텍스트 형식에서는 무시한다.
            ``accept_changes``·``reject_changes`` 에서 비우면 문서 전체다.
        op: 수정 종류 (``EDIT_OPS``). 기본은 글자 바꾸기.
        position: ``insert_paragraph``·``move_paragraph``·``insert_row`` 에서 기준의 앞(``"before"``)인지
            뒤(``"after"``)인지.
        target: ``move_paragraph`` 에서 옮겨 갈 자리의 기준 문단 (예: ``"p3"``).
        style: ``insert_paragraph`` 에서 새 문단의 스타일 (예: ``"제목 2"``, 없으면 기준 문단을 따른다).
            ``set_style`` 에서 문단에 줄 스타일이나 목록 수준 (예: ``"본문"``, ``"목록 2수준"``).
        cells: ``insert_row`` 에서 새 행의 칸 값 (왼쪽부터), ``insert_column`` 에서 새 열의 칸 값 (위부터).
        format: ``format`` 에서 바꿀 글자 서식.
        whole_section: ``delete_paragraph``·``move_paragraph`` 에서 제목 문단부터 그 섹션 끝까지 다룰지.
    """

    old_text: str = ""
    new_text: str = ""
    replace_all: bool = False
    paragraph: str | None = None
    op: str = "replace"
    position: str = "after"
    target: str | None = None
    style: str | None = None
    cells: tuple[str, ...] | None = None
    format: TextFormat | None = None
    whole_section: bool = False


@dataclass(frozen=True)
class Change:
    """적용한 수정 하나의 요약. 에이전트가 사용자에게 바뀐 곳을 설명할 때 쓴다.

    Attributes:
        index: 수정 순서 (1부터).
        line: 처음 바뀐 곳의 줄 번호 (1부터, 그 수정을 적용하기 직전 글자 기준).
        count: 바꾼 횟수 (``replace_all`` 이 아니면 1).
        before: 바뀌기 전 글자 (``SNIPPET_CHARS`` 까지).
        after: 바뀐 뒤 글자 (``SNIPPET_CHARS`` 까지).
        kind: 수정 종류 (``Edit.op``).
        note: 결과에 덧붙일 설명 (영어). 글자 바꾸기가 아니면 바뀐 내용을 이 한 줄로 설명한다
            (예: ``"deleted paragraph p7: '…'"``).
        places: 바꾼 곳마다의 줄(문단) 번호. 같은 줄에서 여러 번 바꾸면 그 번호가 여러 번 들어 있다.
            ``replace_all`` 이 여러 곳을 바꿨을 때 어디가 바뀌었는지 보여주는 데 쓴다.
        in_urls: ``replace_all`` 이 웹 주소 안에서 바꾼 곳의 줄(문단) 번호. 바꾸기는 하되 에이전트가
            의도한 것인지 확인하도록 결과에 알린다.
        skipped: 바꾸지 않고 건너뛴 곳의 수 (DOCX 에서 주소가 보이는 하이퍼링크 안).
    """

    index: int
    line: int
    count: int
    before: str
    after: str
    kind: str = "replace"
    note: str = ""
    places: tuple[int, ...] = ()
    in_urls: tuple[int, ...] = ()
    skipped: int = 0


@dataclass(frozen=True)
class LineView:
    r"""줄 번호를 붙인 본문 일부.

    Attributes:
        text: ``[L12] 줄 내용`` 형태의 줄들 (``\n`` 으로 잇는다). 범위 밖이면 빈 글자.
        first: 보여준 첫 줄 번호 (1부터).
        last: 보여준 마지막 줄 번호. 보여준 줄이 없으면 ``first - 1``.
        total: 전체 줄 수.
        matches: 찾는 글자(``query``)가 든 줄 수 (``first`` 부터 끝까지). 찾지 않았으면 ``None``.
        shown: 보여준 줄 수.
    """

    text: str
    first: int
    last: int
    total: int
    matches: int | None = None
    shown: int = 0


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


def snippets(old: str, new: str) -> tuple[str, str]:
    """바뀐 곳 요약에 보여줄 전/후 글자.

    앞부분이 같은 긴 글자는 그대로 자르면 전/후가 똑같아 보인다. 그래서 처음 달라지는 자리가 잘리는
    범위 밖이면 그 조금 앞부터 ``…`` 를 붙여 보여준다.

    Args:
        old: 바뀌기 전 글자.
        new: 바뀐 뒤 글자.

    Returns:
        ``(전, 후)``. 각각 ``SNIPPET_CHARS`` 까지.
    """
    same = 0
    for a, b in zip(old, new, strict=False):
        if a != b:
            break
        same += 1
    lead = 20
    if same > SNIPPET_CHARS - lead and min(len(old), len(new)) > SNIPPET_CHARS:
        start = same - lead
        return _shorten("…" + old[start:]), _shorten("…" + new[start:])
    return _shorten(old), _shorten(new)


def targets_url(old: str) -> bool:
    """찾을 글자가 웹 주소 자체를 가리키는지. 그러면 주소 안을 바꾸는 것이 의도이므로 알리지 않는다."""
    return "://" in old or old.lower().startswith("www.")


def url_spans(text: str) -> list[tuple[int, int]]:
    """글자 안의 웹 주소 위치 ``(시작, 끝)`` 목록."""
    return [match.span() for match in _URL.finditer(text)]


def in_url(spans: Sequence[tuple[int, int]], start: int, end: int) -> bool:
    """``[start, end)`` 가 웹 주소와 겹치는지."""
    return any(start < url_end and url_start < end for url_start, url_end in spans)


def _not_found(index: int, old: str) -> EditError:
    message = f"Edit {index}: old_text was not found in the document."
    if _LINE_PREFIX.match(old):
        message += " Do not include line number prefixes such as [L12] in old_text."
    else:
        message += " Copy it exactly from the document, including spaces and punctuation."
    return EditError(message, index)


def _apply_one(text: str, index: int, edit: Edit) -> tuple[str, Change]:
    if edit.op != "replace":
        raise EditError(
            f"Edit {index}: op {edit.op!r} only works for Word documents. Text files can only "
            "be changed with replace (old_text and new_text).",
            index,
        )
    old, new = _normalize(edit.old_text), _normalize(edit.new_text)
    if not old:
        raise EditError(f"Edit {index}: old_text is empty.", index)
    if old == new:
        raise EditError(f"Edit {index}: old_text and new_text are the same.", index)
    first = text.find(old)
    if first < 0:
        raise _not_found(index, old)
    line = text.count("\n", 0, first) + 1
    places: tuple[int, ...] = ()
    in_urls: tuple[int, ...] = ()
    if edit.replace_all:
        spans = [] if targets_url(old) else url_spans(text)
        starts: list[int] = []
        position = first
        while position >= 0:
            starts.append(position)
            position = text.find(old, position + len(old))
        places = tuple(text.count("\n", 0, start) + 1 for start in starts)
        in_urls = tuple(
            line
            for line, start in zip(places, starts, strict=True)
            if in_url(spans, start, start + len(old))
        )
        count = len(starts)
        for start in reversed(starts):
            text = text[:start] + new + text[start + len(old) :]
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
    before, after = snippets(old, new)
    return text, Change(index, line, count, before, after, places=places, in_urls=in_urls)


def apply_some(text: str, edits: Sequence[Edit]) -> tuple[str, list[Change], list[EditError]]:
    r"""수정을 순서대로 적용하고, 적용할 수 없는 수정은 건너뛴다.

    뒤의 수정은 앞의 (성공한) 수정을 적용한 글자에서 찾는다. 겹쳐서 나오는 경우(``"aa"`` 가 ``"aaa"``
    안에)도 두 번 나온 것으로 본다.

    Args:
        text: ``\n`` 기준 글자.
        edits: 적용할 수정 (1개 이상).

    Returns:
        ``(수정한 글자, 성공한 수정의 바뀐 곳 요약, 실패한 수정의 오류)``.

    Raises:
        EditError: 수정이 없거나, 결과가 빈 문서일 때 (이때는 아무것도 적용하지 않는다).
    """
    if not edits:
        raise EditError("No edits were given.")
    changes: list[Change] = []
    failures: list[EditError] = []
    for index, edit in enumerate(edits, 1):
        try:
            text, change = _apply_one(text, index, edit)
        except EditError as exc:
            failures.append(exc)
        else:
            changes.append(change)
    if changes and not text.strip():
        raise EditError("These edits would leave the document empty.")
    return text, changes, failures


def apply_edits(text: str, edits: Sequence[Edit]) -> tuple[str, list[Change]]:
    r"""수정을 순서대로 적용한다. 하나라도 실패하면 아무것도 적용하지 않는다.

    Args:
        text: ``\n`` 기준 글자.
        edits: 적용할 수정 (1개 이상).

    Returns:
        ``(수정한 글자, 바뀐 곳 요약)``.

    Raises:
        EditError: 수정이 없거나, 어느 수정이든 적용할 수 없거나(``old_text`` 가 비었거나 ``new_text`` 와
            같거나, 찾지 못했거나, ``replace_all`` 없이 두 번 이상 나옴), 결과가 빈 문서일 때.
    """
    edited, changes, failures = apply_some(text, edits)
    if failures:
        raise failures[0]
    return edited, changes


def combine_failures(failures: Sequence[EditError]) -> EditError:
    """모든 수정이 실패했을 때 에이전트에게 돌려줄 오류 하나로 합친다.

    Args:
        failures: 실패한 수정의 오류 (1개 이상).

    Returns:
        하나면 그 오류, 여럿이면 메시지를 줄마다 이은 오류.
    """
    if len(failures) == 1:
        return failures[0]
    joined = "\n".join(str(f) for f in failures)
    return EditError(f"None of the edits could be applied:\n{joined}", failures[0].index)


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


def matches_query(text: str, query: str | None) -> bool:
    """보기에서 찾는 글자(``query``)가 ``text`` 에 드는지. 대소문자는 무시하고 공백은 그대로 본다."""
    return not query or query.casefold() in text.casefold()


def numbered_lines(
    text: str,
    first: int,
    limit: int,
    max_chars: int | None = None,
    query: str | None = None,
) -> LineView:
    r"""본문의 줄에 번호를 붙여 일부를 보여준다 (``[L12] 줄 내용``).

    파일이 줄바꿈으로 끝나면 그 뒤의 빈 글자는 줄로 세지 않는다. ``max_chars`` 를 주면 결과가 그 글자 수를
    넘기 전에 멈춘다 (긴 줄이 많은 문서). 그래도 첫 줄은 길어도 보여준다. ``query`` 를 주면 그 글자가 든
    줄만 보여준다 (긴 문서에서 고칠 곳 찾기).

    Args:
        text: ``\n`` 기준 글자.
        first: 보여줄 첫 줄 번호 (1부터). 1보다 작으면 1로 본다.
        limit: 보여줄 최대 줄 수 (1 이상).
        max_chars: 결과 글자 수의 상한. ``None`` 이면 줄 수로만 자른다.
        query: 찾을 글자 (대소문자 무시). ``None`` 이나 빈 글자면 모든 줄.

    Returns:
        번호를 붙인 줄들과 범위, 전체 줄 수 (찾았으면 맞는 줄 수).
    """
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()
    first = max(first, 1)
    query = query.strip() if query and query.strip() else None
    shown: list[str] = []
    size = 0
    last = first - 1
    matches = 0
    full = False
    for number, line in enumerate(lines[first - 1 :], first):
        full = full or len(shown) >= max(limit, 1)
        if full and query is None:
            break
        if not matches_query(line, query):
            last = number if not full else last
            continue
        matches += 1
        if full:
            continue
        numbered = f"[L{number}] {line}"
        size += len(numbered) + (1 if shown else 0)
        if shown and max_chars is not None and size > max_chars:
            full = True
            if query is None:
                break
            continue
        shown.append(numbered)
        last = number
    return LineView(
        text="\n".join(shown),
        first=first,
        last=last,
        total=len(lines),
        matches=matches if query is not None else None,
        shown=len(shown),
    )
