"""Langfuse 프롬프트 관리 연동.

- ``fetch``: 실행 중 라벨(``production`` 등)이 붙은 버전을 가져온다. 실패하면 ``None`` 을
  돌려주고, 호출하는 쪽이 로컬 파일을 쓴다.
- ``sync_all``: git(로컬 파일)의 프롬프트를 Langfuse 에 버전으로 올린다. 내용이 바뀐 경우에만
  새 버전을 만든다.

원본은 git 이다. Langfuse 화면에서의 수정은 실험용으로만 쓰고, 정식 변경은 파일 수정 → sync
순서로 한다.
"""

import hashlib
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Literal

from app.core.config import get_settings
from app.prompts.loader import compile_for_langfuse, list_prompts, load_template

logger = logging.getLogger(__name__)

# Langfuse 에 연결할 수 없을 때 매 요청마다 타임아웃을 기다리지 않도록 잠시 시도를 멈춘다.
_FAILURE_COOLDOWN_SECONDS = 30.0
_FETCH_TIMEOUT_SECONDS = 2
_last_failure_at = 0.0
_lock = threading.Lock()


def _client() -> Any:
    """Langfuse 클라이언트 (환경 변수로 설정된 싱글턴)."""
    from langfuse import get_client

    return get_client()


def _in_cooldown() -> bool:
    """최근 실패 후 재시도를 멈추는 시간인지 여부."""
    return time.monotonic() - _last_failure_at < _FAILURE_COOLDOWN_SECONDS


def _mark_failure() -> None:
    """가져오기 실패 시각을 기록한다."""
    global _last_failure_at
    with _lock:
        _last_failure_at = time.monotonic()


def reset_failure_state() -> None:
    """실패 기록을 지워 바로 다시 Langfuse 를 쓰게 한다 (동기화 성공 후, 테스트에서 사용)."""
    global _last_failure_at
    _last_failure_at = 0.0


def fetch(name: str) -> Any | None:
    """라벨이 붙은 Langfuse 프롬프트를 가져온다.

    SDK 가 ``prompt_cache_ttl_seconds`` 동안 캐시하고, 만료되면 이전 값을 돌려주면서
    백그라운드로 갱신한다. 실패하면 잠시(``_FAILURE_COOLDOWN_SECONDS``) Langfuse 를 부르지 않는다.

    Args:
        name: 프롬프트 이름.

    Returns:
        Langfuse ``TextPromptClient``. 트레이싱이 꺼져 있거나, 가져오지 못했거나,
        Langfuse 버전의 변수 구성이 로컬 템플릿과 다르면 ``None``.
    """
    settings = get_settings()
    if not settings.tracing_enabled or _in_cooldown():
        return None
    try:
        prompt = _client().get_prompt(
            name,
            label=settings.prompt_label,
            type="text",
            cache_ttl_seconds=settings.prompt_cache_ttl_seconds,
            max_retries=0,
            fetch_timeout_seconds=_FETCH_TIMEOUT_SECONDS,
        )
    except Exception as e:  # noqa: BLE001 — 네트워크/404/인증 등 무엇이든 로컬 파일로 대체
        _mark_failure()
        logger.warning("Langfuse prompt fetch failed (%s), using local file: %s", name, e)
        return None

    # Langfuse 화면에서 수정된 버전의 변수가 코드와 다르면 렌더링이 깨지므로 쓰지 않는다.
    declared = load_template(name).variables
    if set(prompt.variables) != declared:
        logger.warning(
            "Langfuse prompt %s v%s variables %s != local %s, using local file",
            name,
            prompt.version,
            sorted(prompt.variables),
            sorted(declared),
        )
        return None
    return prompt


# --- sync ---------------------------------------------------------------------------------------


def content_hash(text: str) -> str:
    """프롬프트 내용의 짧은 해시. 최신 버전과 내용이 같은지 비교하는 데 쓴다.

    Args:
        text: Langfuse 에 올릴 프롬프트 텍스트.

    Returns:
        SHA-256 앞 16자리.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _git_commit() -> str | None:
    """현재 커밋 해시. ``GIT_COMMIT`` 환경 변수가 우선이고, 없으면 git 에서 읽는다."""
    if commit := os.environ.get("GIT_COMMIT"):
        return commit
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


@dataclass
class SyncResult:
    """프롬프트 하나의 동기화 결과.

    Attributes:
        name: 프롬프트 이름.
        status: ``created`` (새 버전 생성) / ``unchanged`` (최신과 같음) / ``error``.
        version: 생성되었거나 최신인 Langfuse 버전.
        detail: 오류 메시지 등 추가 정보.
    """

    name: str
    status: Literal["created", "unchanged", "error"]
    version: int | None = None
    detail: str = ""


def _latest(name: str) -> Any | None:
    """Langfuse 의 최신 버전(자동으로 붙는 ``latest`` 라벨). 아직 없으면 ``None``."""
    try:
        return _client().get_prompt(
            name, label="latest", type="text", cache_ttl_seconds=0, max_retries=0
        )
    except Exception as e:
        if "404" in str(e) or "not found" in str(e).lower():
            return None
        raise


def sync_one(name: str, label: str, dry_run: bool = False) -> SyncResult:
    """프롬프트 하나를 Langfuse 에 동기화한다.

    최신 버전과 내용(해시)이 같으면 새 버전을 만들지 않고 라벨도 건드리지 않는다.
    그래서 ``production`` 라벨을 이전 버전으로 옮겨 롤백한 상태가 sync 로 덮어써지지 않는다.

    Args:
        name: 프롬프트 이름.
        label: 새 버전에 붙일 라벨.
        dry_run: ``True`` 면 Langfuse 에 쓰지 않고 결과만 돌려준다.

    Returns:
        동기화 결과.
    """
    text = compile_for_langfuse(name)
    digest = content_hash(text)
    latest = _latest(name)

    if latest is not None and (latest.config or {}).get("source_hash") == digest:
        return SyncResult(name, "unchanged", latest.version)
    if dry_run:
        return SyncResult(name, "created", None, "dry-run: 새 버전이 생성될 예정")

    tpl = load_template(name)
    commit = _git_commit()
    created = _client().create_prompt(
        name=name,
        prompt=text,
        type="text",
        labels=[label],
        config={"source": "git", "source_hash": digest, "git_commit": commit},
        tags=[name.split("/", 1)[0]],
        commit_message=(f"sync from git {commit}" if commit else "sync from local files")
        + (f" — {tpl.description}" if tpl.description else ""),
    )
    return SyncResult(name, "created", created.version)


def sync_all(label: str | None = None, dry_run: bool = False) -> list[SyncResult]:
    """모든 프롬프트를 Langfuse 에 동기화한다.

    한 프롬프트가 실패해도 나머지는 계속 진행한다. 새 버전이 생기면 SDK 캐시를 비워
    실행 중인 프로세스가 바로 새 버전을 쓰게 한다.

    Args:
        label: 새 버전에 붙일 라벨. 없으면 ``PROMPT_LABEL``.
        dry_run: ``True`` 면 Langfuse 에 쓰지 않고 결과만 돌려준다.

    Returns:
        프롬프트별 동기화 결과.

    Raises:
        RuntimeError: Langfuse 키가 설정되지 않았을 때.
    """
    settings = get_settings()
    if not settings.tracing_enabled:
        raise RuntimeError("LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY 가 설정되지 않았습니다.")
    label = label or settings.prompt_label
    results = []
    for name in list_prompts():
        try:
            results.append(sync_one(name, label, dry_run))
        except Exception as e:  # noqa: BLE001 — 한 프롬프트 실패가 나머지 동기화를 막지 않게
            results.append(SyncResult(name, "error", detail=str(e)[:300]))
    if not dry_run and any(r.status == "created" for r in results):
        _client().clear_prompt_cache()
    return results


def sync_in_background(attempts: int = 10, interval_seconds: float = 6.0) -> None:
    """앱 시작 시 백그라운드 스레드에서 동기화한다.

    Langfuse 가 아직 기동 중일 수 있어 재시도하며, 앱 기동을 막지 않는다.

    Args:
        attempts: 최대 시도 횟수.
        interval_seconds: 재시도 간격 (초).
    """

    def run() -> None:
        for i in range(1, attempts + 1):
            try:
                results = sync_all()
            except Exception as e:  # noqa: BLE001 — 백그라운드 스레드: 실패는 로그 후 재시도
                logger.info("prompt sync attempt %d/%d failed: %s", i, attempts, e)
            else:
                errors = [r for r in results if r.status == "error"]
                for r in results:
                    log = logger.warning if r.status == "error" else logger.info
                    log("prompt sync: %s %s v%s %s", r.name, r.status, r.version, r.detail)
                if not errors:
                    reset_failure_state()
                    return
            time.sleep(interval_seconds)
        logger.warning("prompt sync gave up after %d attempts; using local files", attempts)

    threading.Thread(target=run, name="prompt-sync", daemon=True).start()
