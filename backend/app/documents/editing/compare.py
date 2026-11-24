"""두 버전 비교: 문단(줄) 단위로 맞추고, 바뀐 문단 안은 낱말 단위로 비교한다.

화면의 "바뀐 곳" 탭에 쓴다. 파서 결과(``documents.content``)가 아니라 원본 파일에서 뽑은 글자를 비교한다
(수정본 파싱이 끝나기 전에도 되고, 에이전트가 본 글자와 같다). DB 와 저장소를 모르는 순수 함수다.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Literal

CONTEXT = 1
"""바뀐 블록 앞뒤로 남길 같은 블록 수. 나머지 같은 블록은 접는다."""

_WORD = re.compile(r"\s+|\w+|[^\w\s]")
"""낱말 비교의 토큰: 공백 덩어리, 낱말(한글 포함), 기호 하나."""


@dataclass(frozen=True)
class Unit:
    """비교할 단위 하나 (DOCX 문단, 텍스트 줄).

    Attributes:
        label: 위치 표시 (예: ``"p12"``, ``"L3"``).
        text: 글자.
        where: 덧붙일 위치 설명 (예: ``"표1 2행 1열"``). 없으면 ``None``.
    """

    label: str
    text: str
    where: str | None = None


@dataclass(frozen=True)
class Part:
    """블록 안의 글자 조각.

    Attributes:
        op: ``equal`` (그대로) | ``insert`` (넣음) | ``delete`` (지움).
        text: 글자.
    """

    op: Literal["equal", "insert", "delete"]
    text: str


@dataclass(frozen=True)
class Block:
    """비교 결과의 블록 하나.

    Attributes:
        kind: ``same`` (같음) | ``changed`` (고침) | ``added`` (넣음) | ``removed`` (지움) |
            ``skipped`` (같은 블록 여럿을 접음).
        label: 위치 표시 (새 버전 기준, 지운 블록은 옛 버전 기준). ``skipped`` 는 빈 글자.
        where: 덧붙일 위치 설명.
        parts: 글자 조각.
        skipped: ``skipped`` 블록이 접은 블록 수.
    """

    kind: Literal["same", "changed", "added", "removed", "skipped"]
    label: str = ""
    where: str | None = None
    parts: tuple[Part, ...] = field(default_factory=tuple)
    skipped: int = 0


def word_parts(old: str, new: str) -> tuple[Part, ...]:
    """두 글자를 낱말 단위로 비교한 조각. 이어진 같은 종류 조각은 하나로 합친다."""
    a, b = _WORD.findall(old), _WORD.findall(new)
    parts: list[Part] = []

    def add(op: Literal["equal", "insert", "delete"], text: str) -> None:
        if not text:
            return
        if parts and parts[-1].op == op:
            parts[-1] = Part(op, parts[-1].text + text)
        else:
            parts.append(Part(op, text))

    for tag, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            add("equal", "".join(a[i1:i2]))
        else:
            add("delete", "".join(a[i1:i2]))
            add("insert", "".join(b[j1:j2]))
    return tuple(parts)


def compare(old: Sequence[Unit], new: Sequence[Unit], context: int = CONTEXT) -> list[Block]:
    """두 버전의 단위 목록을 비교한다.

    단위의 글자로 맞추고, 바뀐 구간에서 옛 단위와 새 단위를 앞에서부터 짝지어 ``changed`` 로 보여준다.
    짝이 없는 나머지는 ``removed``·``added``. 바뀐 블록에서 ``context`` 보다 먼 같은 블록은 접는다.

    Args:
        old: 옛 버전의 단위.
        new: 새 버전의 단위.
        context: 바뀐 블록 앞뒤로 남길 같은 블록 수.

    Returns:
        블록 목록. 바뀐 곳이 없으면 모두 접힌 블록 하나 (또는 빈 목록).
    """
    blocks: list[Block] = []
    matcher = SequenceMatcher(None, [u.text for u in old], [u.text for u in new], autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            blocks.extend(
                Block("same", u.label, u.where, (Part("equal", u.text),)) for u in new[j1:j2]
            )
            continue
        pairs = min(i2 - i1, j2 - j1)
        for k in range(pairs):
            before, after = old[i1 + k], new[j1 + k]
            blocks.append(
                Block("changed", after.label, after.where, word_parts(before.text, after.text))
            )
        blocks.extend(
            Block("removed", u.label, u.where, (Part("delete", u.text),))
            for u in old[i1 + pairs : i2]
        )
        blocks.extend(
            Block("added", u.label, u.where, (Part("insert", u.text),))
            for u in new[j1 + pairs : j2]
        )
    return _fold(blocks, context)


def _fold(blocks: list[Block], context: int) -> list[Block]:
    """바뀐 블록에서 ``context`` 보다 먼 같은 블록을 ``skipped`` 블록으로 접는다."""
    near = set()
    for position, block in enumerate(blocks):
        if block.kind != "same":
            near.update(range(position - context, position + context + 1))
    folded: list[Block] = []
    for position, block in enumerate(blocks):
        if block.kind != "same" or position in near:
            folded.append(block)
        elif folded and folded[-1].kind == "skipped":
            folded[-1] = Block("skipped", skipped=folded[-1].skipped + 1)
        else:
            folded.append(Block("skipped", skipped=1))
    return folded
