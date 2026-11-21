"""대화별 "응답 생성 중" 상태 관리와 백그라운드 실행.

같은 대화에 응답이 두 개 동시에 생성되면 checkpointer 상태가 갈라져 한쪽 대화가 모델의
기억에서 사라진다. 그래서 대화당 실행을 하나로 제한하고, 생성 중에 들어온 요청은 거절한다.
(같은 창 안의 추가 질문은 프론트엔드가 대기열로 처리한다.)

지금은 백엔드 프로세스가 하나라서 메모리에 기록한다. 여러 프로세스로 늘리면 같은
인터페이스로 Redis 구현(``SET NX`` + TTL)을 만들어 바꿔 끼운다.

응답 생성은 HTTP 요청이 아니라 **백그라운드 작업**에서 돈다 (``start_run``). 작업은 이벤트를 번호 순서로
쌓고(``RunStream``), 화면은 그 기록을 구독한다. 그래서 연결이 끊겨도(새로고침, 탭 닫기) 응답은 끝까지 만들어지고
저장된다. 끝난 실행의 기록은 잠깐(``RETAIN_SECONDS``) 남겨서 늦게 붙은 화면도 끝부분을 받게 한다.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Any, Protocol

from app.core.config import get_settings

logger = logging.getLogger(__name__)


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


# --- 백그라운드 실행 -------------------------------------------------------------------------------

RETAIN_SECONDS = 60.0
"""끝난 실행의 이벤트 기록을 남겨 두는 시간 (늦게 붙은 화면이 끝부분을 받을 수 있게)."""


class RunStream:
    """백그라운드에서 도는 응답 실행 하나와 그 이벤트 기록.

    이벤트는 1부터 번호가 붙고(``events[i - 1]`` 이 ``i`` 번), 구독자는 원하는 번호 다음부터 받는다.
    구독자가 여럿이어도 같은 기록을 읽고, 구독을 끊어도 실행은 계속된다.

    Attributes:
        conversation_id: 대화 ID.
        events: 지금까지의 이벤트 (화면으로 보낼 ``turns.StreamEvent``).
        done: 실행이 끝났는지 (정상, 오류, 중단, 시간 초과 모두).
        task: 실행 작업.
    """

    def __init__(self, conversation_id: str) -> None:
        """빈 기록을 만든다.

        Args:
            conversation_id: 대화 ID.
        """
        self.conversation_id = conversation_id
        self.events: list[Any] = []
        self.done = False
        self.task: asyncio.Task[None] | None = None
        self._changed = asyncio.Event()

    def _notify(self) -> None:
        # 기다리던 구독자를 모두 깨우고, 다음 변화는 새 Event 로 기다리게 한다.
        self._changed.set()
        self._changed = asyncio.Event()

    def append(self, event: Any) -> None:
        """이벤트를 기록하고 구독자를 깨운다.

        Args:
            event: 이벤트.
        """
        self.events.append(event)
        self._notify()

    def finish(self) -> None:
        """실행이 끝났다고 표시하고 구독자를 깨운다."""
        self.done = True
        self._notify()

    async def follow(self, after: int = 0) -> AsyncIterator[tuple[int, Any]]:
        """``after`` 번 다음 이벤트부터 끝날 때까지 받는다. 이미 쌓인 이벤트는 바로 받는다.

        Args:
            after: 이미 받은 마지막 이벤트 번호 (처음부터면 0).

        Yields:
            ``(번호, 이벤트)``.
        """
        index = max(after, 0)
        while True:
            waiter = self._changed  # 확인하기 전에 잡아 둬야 그 사이의 변화를 놓치지 않는다
            while index < len(self.events):
                index += 1
                yield index, self.events[index - 1]
            if self.done:
                return
            await waiter.wait()

    async def follow_events(self, after: int = 0) -> AsyncIterator[Any]:
        """``follow`` 에서 번호를 뺀 이벤트만 받는다.

        Args:
            after: 이미 받은 마지막 이벤트 번호.

        Yields:
            이벤트.
        """
        async for _, event in self.follow(after):
            yield event


_streams: dict[str, RunStream] = {}


def start_run(
    conversation_id: str,
    events: AsyncIterator[Any],
    *,
    max_seconds: float,
    timeout_event: Any,
) -> RunStream:
    """응답 실행을 백그라운드 작업으로 시작한다. 요청 연결과 관계없이 끝까지 돈다.

    ``events`` (``turns.run_turn``)를 끝까지 읽어 기록에 쌓는다. 저장과 생성 중 표시 해제는 ``events``
    안에서 한다. ``max_seconds`` 가 지나면 실행을 취소하고(``events`` 는 중단으로 저장한다) ``timeout_event`` 를
    기록한다. 아무도 보지 않는 실행이 끝없이 돌며 비용을 쓰지 않게 하는 안전장치다.

    Args:
        conversation_id: 대화 ID. 호출 전에 ``run_registry`` 에서 생성 중으로 표시되어 있어야 한다.
        events: 실행 이벤트 스트림.
        max_seconds: 실행 시간 상한.
        timeout_event: 시간 초과로 멈췄을 때 기록할 이벤트.

    Returns:
        실행 기록. 구독은 ``RunStream.follow``.
    """
    stream = RunStream(conversation_id)

    async def pump() -> None:
        try:
            async with asyncio.timeout(max_seconds):
                async for event in events:
                    stream.append(event)
        except TimeoutError:
            logger.warning(
                "run for %s exceeded %.0fs and was stopped", conversation_id, max_seconds
            )
            stream.append(timeout_event)
        except asyncio.CancelledError:
            raise  # 앱 종료 (shutdown_runs). events 가 중단으로 저장했다
        except (
            Exception
        ):  # events 가 오류를 이벤트로 바꾸므로 여기까지 오면 안 되지만, 작업이 조용히 죽지 않게
            logger.exception("run for %s failed", conversation_id)
        finally:
            stream.finish()
            asyncio.get_running_loop().call_later(RETAIN_SECONDS, _forget, stream)

    stream.task = asyncio.create_task(pump(), name=f"run:{conversation_id}")
    _streams[conversation_id] = stream
    return stream


def _forget(stream: RunStream) -> None:
    """끝난 실행의 기록을 지운다 (그 사이 같은 대화의 새 실행이 시작됐으면 그대로 둔다)."""
    if _streams.get(stream.conversation_id) is stream:
        del _streams[stream.conversation_id]


def get_stream(conversation_id: str) -> RunStream | None:
    """대화의 진행 중이거나 막 끝난 실행 기록.

    Args:
        conversation_id: 대화 ID.

    Returns:
        실행 기록. 없으면(실행한 적 없음, 기록을 지움, 서버 재시작) ``None``.
    """
    return _streams.get(conversation_id)


async def shutdown_runs() -> None:
    """진행 중인 실행을 모두 취소하고 끝날 때까지 기다린다 (앱 종료).

    취소된 실행은 그때까지의 응답을 중단 표시와 함께 저장한다.
    """
    tasks = [s.task for s in _streams.values() if s.task is not None and not s.task.done()]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    _streams.clear()
