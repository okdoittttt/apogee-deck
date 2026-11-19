"""git(로컬 파일)의 프롬프트를 Langfuse 로 동기화한다.

    uv run python -m app.prompts.sync              # .env 의 PROMPT_LABEL(기본 production)로 동기화
    uv run python -m app.prompts.sync --dry-run    # 무엇이 바뀌는지만 확인
    uv run python -m app.prompts.sync --label staging

Docker 안에서:  docker compose exec backend python -m app.prompts.sync
"""

import argparse
import sys

from app.prompts import validate_all
from app.prompts.remote import sync_all


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--label", help="새 버전에 붙일 라벨 (기본: PROMPT_LABEL)")
    parser.add_argument("--dry-run", action="store_true", help="Langfuse 에 쓰지 않고 결과만 출력")
    args = parser.parse_args()

    validate_all()  # 잘못된 프롬프트는 올리지 않는다
    results = sync_all(label=args.label, dry_run=args.dry_run)

    marks = {"created": "+", "unchanged": "=", "error": "!"}
    for r in results:
        version = f"v{r.version}" if r.version else ""
        print(f" {marks[r.status]} {r.name:<30} {r.status:<10} {version} {r.detail}".rstrip())

    from langfuse import get_client

    get_client().flush()
    return 1 if any(r.status == "error" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
