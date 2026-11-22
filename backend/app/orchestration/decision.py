"""도구 결정: 결정 모델(d3)이 턴마다 이번 메시지에 필요한 도구 그룹을 고른다.

도구 정의도 매 요청 프롬프트에 들어가므로, 쓸 일이 없는 도구는 모델에게 주지 않는다. 결정 모델은 글을 생성하지 않고
질문마다 확률만 돌려주는 분류 모델이라(Ollama ``/v1/systemone``) 빠르고 형식 오류가 없다.

- 그룹마다 예/아니오(noul) 질문을 하나씩, 요청 한 번에 묻는다. 확률이 기준 이상인 그룹을 켜고, 모두 기준
  아래면 도구 없이 답한다.
- 켜진 그룹은 대화가 이어지는 동안 누적한다. 도구 정의는 프롬프트 맨 앞에 있어서, 목록이 바뀌면 그 뒤의 대화
  이력 전체가 프롬프트 캐시에서 빠지기 때문이다. 이미 켜진 그룹은 다시 묻지 않는다.
- 이전 대화에서 실제로 부른 도구의 그룹도 켜진 것으로 본다. 이력에 도구 호출이 있는데 그 도구 정의가 없으면
  모델이 이력을 이해하지 못하거나 provider 가 요청을 거절할 수 있다.
- 결정에 실패하면(시간 초과, 연결 실패, 모델 없음) 이번 턴만 도구를 모두 주고 누적 목록은 바꾸지 않는다.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from app.core.config import Settings
from app.documents.reading import split_attachments
from app.tools import GROUPS, TOOLS, tool_group

logger = logging.getLogger(__name__)

DECISION_PATH = "/v1/systemone"
DOCUMENTS_GROUP = "documents"
"""이번 메시지에 첨부 문서가 있으면 결정 없이 켜는 그룹."""

PREVIOUS_MESSAGES = 2
"""결정 모델에게 함께 보여줄 직전 메시지 수. "두 번째 기사 더 자세히" 같은 이어지는 질문을 판단하게 한다."""

MAX_MESSAGE_CHARS = 500
"""결정 모델에게 보여줄 메시지 하나의 최대 글자 수. 결정에는 앞부분이면 충분하다."""

Ask = Callable[[dict[str, Any], dict[str, str]], Awaitable[dict[str, float]]]
"""결정 모델에 묻는 함수: ``(state, {그룹: 질문}) -> {그룹: 확률}``."""


class DecisionUnavailableError(RuntimeError):
    """결정 모델에게 답을 받지 못했다 (연결 실패, 시간 초과, 모델 없음, 응답 형식 이상)."""


@dataclass(frozen=True)
class Decision:
    """도구 결정 결과.

    Attributes:
        groups: 누적한 그룹. 상태에 저장해서 다음 턴에 이어 쓴다.
        active: 이번 턴에 모델에게 줄 그룹. ``None`` 이면 도구를 모두 준다 (결정 실패).
        probabilities: 이번에 물은 그룹별 확률. 묻지 않았으면 비어 있다.
        error: 결정에 실패한 이유. 성공하면 ``None``.
    """

    groups: tuple[str, ...]
    active: tuple[str, ...] | None
    probabilities: dict[str, float] = field(default_factory=dict)
    error: str | None = None


def decision_url(settings: Settings) -> str:
    """결정 요청을 보낼 주소. ``tool_decision_url`` 이 비어 있으면 ``ollama_base_url`` 을 쓴다."""
    base = settings.tool_decision_url or settings.ollama_base_url
    return f"{base.rstrip('/')}{DECISION_PATH}"


async def ask_d3(
    settings: Settings,
    state: dict[str, Any],
    questions: dict[str, str],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, float]:
    """결정 모델에게 그룹별 예/아니오 질문을 요청 한 번으로 묻는다.

    Args:
        settings: 앱 설정 (모델, 주소, 시간 제한, keep_alive).
        state: 판단할 내용 (``latest_message``, ``previous_turns``).
        questions: 그룹 이름 → 질문.
        transport: 테스트용 httpx transport.

    Returns:
        그룹 이름 → "예" 확률 (0~1).

    Raises:
        DecisionUnavailableError: 요청이 실패했거나 응답이 예상과 다를 때.
    """
    body = {
        "model": settings.tool_decision_model,
        "state": state,
        "questions": {
            name: {"type": "noul", "instructions": question} for name, question in questions.items()
        },
        "keep_alive": settings.tool_decision_keep_alive,
    }
    try:
        async with httpx.AsyncClient(
            timeout=settings.tool_decision_timeout_seconds, transport=transport
        ) as client:
            response = await client.post(decision_url(settings), json=body)
    except httpx.HTTPError as exc:
        raise DecisionUnavailableError(f"decision request failed: {exc!r}") from exc
    if response.status_code != 200:
        raise DecisionUnavailableError(
            f"decision request failed ({response.status_code}): {response.text[:200]}"
        )
    try:
        answers = response.json()["answers"]
        return {name: float(answers[name]["noul"]) for name in questions}
    except (ValueError, KeyError, TypeError) as exc:
        raise DecisionUnavailableError(f"unexpected decision response: {exc!r}") from exc


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_MESSAGE_CHARS else f"{text[:MAX_MESSAGE_CHARS]}…"


def decision_state(messages: Sequence[BaseMessage]) -> tuple[dict[str, Any], bool]:
    """결정 모델에게 보여줄 상태: 이번 사용자 메시지와 직전 메시지 몇 개.

    첨부 목록(문서 본문이 들어 있을 수 있다)은 떼어 내고 사용자가 쓴 글만 보낸다. 도구 호출과 결과는 넣지 않는다.

    Args:
        messages: 대화 이력 (마지막 사용자 메시지가 이번 턴).

    Returns:
        ``(상태, 이번 메시지에 첨부 문서가 있는지)``.
    """
    last = next(
        (i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], HumanMessage)),
        None,
    )
    if last is None:
        return {"latest_message": "(empty)"}, False
    latest, attached = split_attachments(messages[last].text)
    previous: list[dict[str, str]] = []
    for message in messages[:last]:
        if isinstance(message, HumanMessage):
            text, _ = split_attachments(message.text)
            role = "user"
        elif isinstance(message, AIMessage) and message.text.strip():
            text, role = message.text, "assistant"
        else:
            continue
        if text.strip():
            previous.append({"role": role, "text": _clip(text)})
    state: dict[str, Any] = {"latest_message": _clip(latest) or "(no text, only attachments)"}
    if previous[-PREVIOUS_MESSAGES:]:
        state["previous_turns"] = previous[-PREVIOUS_MESSAGES:]
    return state, attached


def used_groups(messages: Iterable[BaseMessage]) -> set[str]:
    """대화 이력에서 모델이 실제로 부른 도구의 그룹."""
    return {
        tool_group(call["name"])
        for message in messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls
        if call["name"] in TOOLS
    }


def _ordered(groups: Iterable[str]) -> tuple[str, ...]:
    chosen = set(groups)
    return tuple(name for name in GROUPS if name in chosen)


async def decide(
    settings: Settings,
    messages: Sequence[BaseMessage],
    candidates: Iterable[str],
    previous: Iterable[str],
    ask: Ask,
) -> Decision:
    """이번 턴에 모델에게 줄 도구 그룹을 정한다.

    Args:
        settings: 앱 설정 (기준 확률, 시간 제한).
        messages: 대화 이력.
        candidates: 이번 턴에 쓸 수 있는 도구의 그룹 (설정·기억·프로젝트로 거른 뒤).
        previous: 지난 턴까지 누적한 그룹.
        ask: 결정 모델에 묻는 함수.

    Returns:
        누적 그룹과 이번 턴에 줄 그룹. 결정에 실패하면 ``active`` 가 ``None`` 이다.
    """
    candidates = set(candidates)
    state, attached = decision_state(messages)
    kept = set(previous) | used_groups(messages)
    if attached and DOCUMENTS_GROUP in candidates:
        kept.add(DOCUMENTS_GROUP)
    questions = {
        name: GROUPS[name].question for name in GROUPS if name in candidates and name not in kept
    }
    probabilities: dict[str, float] = {}
    if questions:
        try:
            async with asyncio.timeout(settings.tool_decision_timeout_seconds):
                probabilities = await ask(state, questions)
        except (DecisionUnavailableError, TimeoutError) as exc:
            logger.warning("tool decision failed; giving all tools this turn: %s", exc)
            return Decision(groups=_ordered(kept), active=None, error=str(exc) or repr(exc))
    chosen = {name for name, p in probabilities.items() if p >= settings.tool_decision_threshold}
    groups = _ordered(kept | chosen)
    return Decision(groups=groups, active=groups, probabilities=probabilities)
