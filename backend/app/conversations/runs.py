"""대화별 "응답 생성 중" 상태 관리.

같은 대화에 응답이 두 개 동시에 생성되면 checkpointer 상태가 갈라져 한쪽 대화가 모델의
기억에서 사라진다. 그래서 대화당 실행을 하나로 제한하고, 생성 중에 들어온 요청은 거절한다.
(같은 창 안의 추가 질문은 프론트엔드가 대기열로 처리한다.)

지금은 백엔드 프로세스가 하나라서 메모리에 기록한다. 여러 프로세스로 늘리면 같은
인터페이스로 Redis 구현(``SET NX`` + TTL)을 만들어 바꿔 끼운다.
"""

import time
from typing import Protocol

from app.core.config import get_settings


class RunRegistry(Protocol):
    """대화별 실행 상태 저장소의 인터페이스."""

    async def try_acquire(self, conversation_id: str) -> bool:
        """실행을 시작할 수 있으면 생성 중으로 표시하고 ``True`` 를 돌려준다."""
        ...

    async def touch(self, conversation_id: str) -> None:
        """실행이 살아 있음을 알린다 (오래된 기록으로 간주되지 않게)."""
        ...

    async def release(self, conversation_id: str) -> None:
        """생성 중 표시를 해제한다."""
        ...

    async def is_running(self, conversation_id: str) -> bool:
        """응답을 생성 중인지 여부."""
        ...


class InMemoryRunRegistry:
    """프로세스 메모리에 실행 상태를 기록하는 구현.

    응답 시작 직전에 연결이 끊기는 등으로 해제가 실행되지 않는 경우를 대비해,
    ``stale_after_seconds`` 동안 ``touch`` 가 없으면 끝난 실행으로 본다.
    """

    def __init__(self, stale_after_seconds: float) -> None:
        """실행 상태 저장소를 만든다.

        Args:
            stale_after_seconds: 마지막 갱신 후 이 시간이 지나면 실행 기록을 무시한다.
        """
        self._stale_after = stale_after_seconds
        self._last_seen: dict[str, float] = {}

    def _alive(self, conversation_id: str) -> bool:
        last = self._last_seen.get(conversation_id)
        return last is not None and time.monotonic() - last < self._stale_after

    async def try_acquire(self, conversation_id: str) -> bool:
        """실행을 시작할 수 있으면 생성 중으로 표시한다.

        확인과 표시 사이에 ``await`` 가 없어서 같은 이벤트 루프 안에서는 원자적이다.

        Args:
            conversation_id: 대화 ID.

        Returns:
            시작할 수 있으면 ``True``, 이미 생성 중이면 ``False``.
        """
        if self._alive(conversation_id):
            return False
        self._last_seen[conversation_id] = time.monotonic()
        return True

    async def touch(self, conversation_id: str) -> None:
        """실행이 살아 있음을 기록한다.

        Args:
            conversation_id: 대화 ID.
        """
        if conversation_id in self._last_seen:
            self._last_seen[conversation_id] = time.monotonic()

    async def release(self, conversation_id: str) -> None:
        """생성 중 표시를 해제한다.

        Args:
            conversation_id: 대화 ID.
        """
        self._last_seen.pop(conversation_id, None)

    async def is_running(self, conversation_id: str) -> bool:
        """응답을 생성 중인지 여부.

        Args:
            conversation_id: 대화 ID.

        Returns:
            생성 중이면 ``True``.
        """
        return self._alive(conversation_id)


run_registry: RunRegistry = InMemoryRunRegistry(get_settings().run_stale_seconds)
"""앱 전체가 함께 쓰는 실행 상태 저장소. 대화 서비스(``service``, ``turns``)가 쓴다."""
