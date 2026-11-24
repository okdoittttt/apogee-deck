"""SQLAlchemy 모델에서 ``docs/data/erd.md`` 의 mermaid ERD 를 다시 만든다.

문서 안의 ``<!-- erd:<영역> -->`` 와 ``<!-- /erd:<영역> -->`` 사이를 생성한 그림으로 바꾼다.
그 밖의 설명 글은 건드리지 않는다.

사용법 (``backend/`` 에서)::

    uv run python scripts/gen_erd.py           # 문서를 고친다 (make docs-erd)
    uv run python scripts/gen_erd.py --check   # 문서가 모델과 다르면 실패한다 (make docs-check)
"""

import argparse
import re
import sys
from pathlib import Path

from sqlalchemy import Column, Table
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.types import Boolean, DateTime, Integer, SmallInteger, String, Text, Uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import Base

ERD_PATH = Path(__file__).resolve().parents[2] / "docs" / "data" / "erd.md"

GROUPS: dict[str, set[str] | None] = {
    "overview": None,
    "auth": {"users", "sessions", "approval_tokens"},
    "conversation": {"projects", "conversations", "messages", "message_images"},
    "document": {"documents", "document_elements", "document_chunks"},
}
"""영역 이름 → 그 영역의 테이블. ``None`` 은 전체 관계만 그리는 개요.

영역 테이블을 자식으로 갖는 관계를 그리고, 그 관계의 부모 중 영역 밖 테이블은 기본 키만 보여 준다.
"""

BLOCK = re.compile(r"(<!-- erd:(\w+) -->\n)(.*?)(<!-- /erd:\2 -->)", re.DOTALL)


def column_type(col: Column) -> str:
    """Mermaid 가 받는 형태의 컬럼 타입 이름. 괄호를 쓸 수 없어 ``varchar_255`` 처럼 쓴다.

    Args:
        col: SQLAlchemy 컬럼.

    Returns:
        타입 이름.
    """
    t = col.type
    if isinstance(t, ARRAY):
        return f"{column_type(Column('item', t.item_type))}_array"
    if isinstance(t, JSONB):
        return "jsonb"
    if isinstance(t, TSVECTOR):
        return "tsvector"
    if type(t).__name__.lower() == "vector":
        return f"vector_{t.dim}"
    if isinstance(t, Uuid):
        return "uuid"
    if isinstance(t, DateTime):
        return "timestamptz"
    if isinstance(t, Boolean):
        return "bool"
    if isinstance(t, SmallInteger):
        return "smallint"
    if isinstance(t, Integer):
        return "int"
    if isinstance(t, Text):
        return "text"
    if isinstance(t, String):
        return f"varchar_{t.length}"
    return type(t).__name__.lower()


def relation_lines(tables: list[Table], children: set[str] | None) -> list[str]:
    """외래 키마다 관계 한 줄. 자식 FK 가 비어 있을 수 있으면 부모 쪽을 ``o|`` 로 그린다.

    Args:
        tables: 전체 테이블 (의존 순서).
        children: 이 테이블들을 자식으로 갖는 관계만 그린다. ``None`` 이면 전부.

    Returns:
        mermaid 관계 줄.
    """
    lines = []
    for table in tables:
        if children is not None and table.name not in children:
            continue
        for fk in sorted(table.foreign_keys, key=lambda f: f.parent.name):
            parent = "o|" if fk.parent.nullable else "||"
            lines.append(
                f'    {fk.column.table.name} {parent}--o{{ {table.name} : "{fk.parent.name}"'
            )
    return lines


def entity_lines(table: Table, keys_only: bool) -> list[str]:
    """테이블 하나의 엔티티 블록.

    Args:
        table: 테이블.
        keys_only: 기본 키만 보여 줄지 (영역 밖 테이블).

    Returns:
        mermaid 엔티티 줄.
    """
    lines = [f"    {table.name} {{"]
    for col in table.columns:
        if keys_only and not col.primary_key:
            continue
        marks = [
            m
            for m, on in (("PK", col.primary_key), ("FK", col.foreign_keys), ("UK", col.unique))
            if on
        ]
        mark = f" {','.join(marks)}" if marks else ""
        note = ' "nullable"' if col.nullable and not col.primary_key else ""
        lines.append(f"        {column_type(col)} {col.name}{mark}{note}")
    lines.append("    }")
    return lines


def render(group: str) -> str:
    """영역 하나의 mermaid 블록.

    Args:
        group: ``GROUPS`` 의 영역 이름.

    Returns:
        ```` ```mermaid ```` 로 감싼 ERD.
    """
    tables = list(Base.metadata.sorted_tables)
    own = GROUPS[group]
    lines = ["erDiagram", *relation_lines(tables, own)]
    if own is not None:
        parents = {fk.column.table.name for t in tables if t.name in own for fk in t.foreign_keys}
        for table in tables:
            if table.name in own:
                lines += entity_lines(table, keys_only=False)
            elif table.name in parents:
                lines += entity_lines(table, keys_only=True)
    return "```mermaid\n" + "\n".join(lines) + "\n```\n"


def rebuild(text: str) -> str:
    """문서의 모든 ERD 블록을 다시 만든다.

    Args:
        text: ``erd.md`` 내용.

    Returns:
        블록을 바꾼 내용.

    Raises:
        SystemExit: 모르는 영역 이름이 있거나, 블록이 하나도 없거나, 어느 영역에도 없는 테이블이 있을 때.
    """
    grouped = set().union(*(g for g in GROUPS.values() if g is not None))
    missing = {t.name for t in Base.metadata.sorted_tables} - grouped
    if missing:
        raise SystemExit(
            f"GROUPS 에 영역을 정하지 않은 테이블이 있습니다: {sorted(missing)} (scripts/gen_erd.py)"
        )
    found = BLOCK.findall(text)
    if not found:
        raise SystemExit(f"{ERD_PATH}: <!-- erd:... --> 블록이 없습니다")
    unknown = {name for _, name, _, _ in found} - GROUPS.keys()
    if unknown:
        raise SystemExit(f"{ERD_PATH}: 모르는 영역 {sorted(unknown)}")
    return BLOCK.sub(lambda m: m.group(1) + render(m.group(2)) + m.group(4), text)


def main() -> None:
    """문서를 고치거나(기본) 모델과 같은지 검사한다(``--check``)."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="고치지 않고, 다르면 실패")
    args = parser.parse_args()

    text = ERD_PATH.read_text(encoding="utf-8")
    new = rebuild(text)
    if args.check:
        if new != text:
            raise SystemExit(
                "docs/data/erd.md 가 models.py 와 다릅니다. make docs-erd 로 다시 만드세요."
            )
        print("erd ok")
        return
    if new != text:
        ERD_PATH.write_text(new, encoding="utf-8")
        print(f"updated {ERD_PATH}")
    else:
        print("erd already up to date")


if __name__ == "__main__":
    main()
