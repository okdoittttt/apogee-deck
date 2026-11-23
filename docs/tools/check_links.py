"""Markdown 의 상대 링크와 ``#앵커`` 가 실제로 있는지 검사한다.

문서를 옮기거나 제목을 바꾸면 다른 문서의 링크가 조용히 깨진다. 외부 주소(http, mailto)는 보지 않는다.
앵커는 GitHub 방식(소문자, 문장 부호 제거, 공백 → ``-``)으로 만든다. 코드 블록 안은 건너뛴다.

사용법: python3 check_links.py <md 파일 ...>   (make docs-check)
"""

import re
import sys
from pathlib import Path

CODE_BLOCK = re.compile(r"```.*?```", re.DOTALL)
HEADING = re.compile(r"^#+ (.+)$", re.MULTILINE)
LINK = re.compile(r"\]\(([^)\s]+)\)")


def slug(heading: str) -> str:
    """GitHub 가 제목에 붙이는 앵커.

    Args:
        heading: 제목 글자.

    Returns:
        앵커 (``#`` 제외).
    """
    text = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return text.replace(" ", "-")


def anchors(path: Path, cache: dict[Path, set[str]]) -> set[str]:
    """문서의 앵커 목록.

    Args:
        path: Markdown 파일.
        cache: 이미 읽은 문서의 앵커.

    Returns:
        앵커 집합.
    """
    if path not in cache:
        text = CODE_BLOCK.sub("", path.read_text(encoding="utf-8"))
        cache[path] = {slug(h) for h in HEADING.findall(text)}
    return cache[path]


def main() -> None:
    """인자로 받은 파일의 링크를 검사하고, 깨진 링크가 있으면 실패한다."""
    cache: dict[Path, set[str]] = {}
    broken = 0
    files = [Path(f).resolve() for f in sys.argv[1:]]
    for path in files:
        text = CODE_BLOCK.sub("", path.read_text(encoding="utf-8"))
        for link in LINK.findall(text):
            if link.startswith(("http://", "https://", "mailto:")):
                continue
            target, _, anchor = link.partition("#")
            dest = (path.parent / target).resolve() if target else path
            if not dest.exists():
                print(f"{path}: 없는 파일 {link}")
                broken += 1
            elif anchor and dest.suffix == ".md" and anchor not in anchors(dest, cache):
                print(f"{path}: 없는 앵커 {link}")
                broken += 1
    print(f"links: {len(files)} files, {broken} broken")
    sys.exit(1 if broken else 0)


if __name__ == "__main__":
    main()
