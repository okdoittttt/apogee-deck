"""git(로컬 파일)의 프롬프트를 Langfuse 로 동기화하는 CLI.

사용법::

    make prompt-sync     # = python -m app.prompts.sync (.env 의 PROMPT_LABEL, 기본 production)
    make prompt-check    # = python -m app.prompts.sync --dry-run (무엇이 바뀌는지만 확인)
    python -m app.prompts.sync --label staging
"""

import argparse
import sys

from app.prompts import validate_all
from app.prompts.remote import sync_all


def main() -> int:
    """프롬프트를 검증한 뒤 Langfuse 로 동기화하고 결과를 출력한다.

    Returns:
        종료 코드. 하나라도 실패하면 1, 아니면 0.

    Raises:
        PromptError: 프롬프트 검증에 실패했을 때 (잘못된 프롬프트는 올리지 않는다).
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--label", help="새 버전에 붙일 라벨 (기본: PROMPT_LABEL)")
    parser.add_argument("--dry-run", action="store_true", help="Langfuse 에 쓰지 않고 결과만 출력")
    args = parser.parse_args()

    validate_all()
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
