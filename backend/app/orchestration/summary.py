"""여러 에이전트가 공용으로 쓰는 대화 요약 도구.

대화 이력이 토큰 예산에 가까워지면 오래된 앞부분을 요약해서, 잘라내더라도 핵심 정보가
남게 한다. 원본 메시지는 checkpointer 상태에 그대로 두고, 상태에는 요약 텍스트와
"요약에 포함된 마지막 메시지 ID" 만 추가로 저장한다.

::

    [요약된 앞부분 ............][원문으로 보내는 최근 부분]
                               ^ summarized_until 다음 메시지, 항상 사용자 메시지
"""

import logging
from dataclasses import dataclass

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from app import prompts
from app.orchestration.context import TokenCounter

logger = logging.getLogger(__name__)

TRIGGER_RATIO = 0.8
"""요약하지 않은 이력이 예산의 이 비율을 넘으면 요약한다."""

KEEP_RATIO = 0.4
"""요약할 때 최근 이력을 예산의 이 비율만큼 원문으로 남긴다."""

CHUNK_RATIO = 0.5
"""요약 호출 한 번에 넣는 대화 기록의 최대 크기 (예산 대비). 넘으면 나눠서 이어 요약한다."""

MAX_MESSAGE_CHARS = 4000
"""요약 입력에서 메시지 하나의 최대 글자 수. 긴 도구 결과 하나가 요약 입력을 차지하지 않게 한다."""


NO_SUMMARY = "(none yet)"
"""처음 요약할 때 요약 프롬프트의 ``previous_summary`` 에 넣는 값."""


@dataclass(frozen=True)
class SummaryPlan:
    """이번에 요약할 메시지.

    Attributes:
        messages: 기존 요약 뒤에 이어서 요약할 메시지 (오래된 것부터).
        until_id: 요약에 포함되는 마지막 메시지 ID. 이 다음 메시지부터 원문으로 보낸다.
    """

    messages: list[BaseMessage]
    until_id: str


def unsummarized(messages: list[BaseMessage], summarized_until: str | None) -> list[BaseMessage]:
    """요약에 포함되지 않은 메시지(``summarized_until`` 다음부터)를 돌려준다.

    Args:
        messages: checkpointer 에 저장된 전체 대화 이력.
        summarized_until: 요약에 포함된 마지막 메시지 ID. 없으면 전체가 요약되지 않은 것이다.

    Returns:
        요약 이후의 메시지. ID 를 찾지 못하면 전체.
    """
    if summarized_until is None:
        return messages
    for i, message in enumerate(messages):
        if message.id == summarized_until:
            return messages[i + 1 :]
    logger.warning("summarized_until %s not found in history; using full history", summarized_until)
    return messages


def plan_summary(
    messages: list[BaseMessage],
    summarized_until: str | None,
    budget: int,
    count_tokens: TokenCounter,
) -> SummaryPlan | None:
    """요약이 필요한지 판단하고, 필요하면 어디까지 요약할지 정한다.

    요약하지 않은 이력이 예산의 ``TRIGGER_RATIO`` 를 넘으면, 최근 이력을 예산의
    ``KEEP_RATIO`` 안에서 최대한 원문으로 남기고 그 앞을 요약한다. 남기는 부분은 반드시
    사용자 메시지로 시작해서 도구 호출과 결과가 갈라지지 않게 한다. 마지막 사용자 턴 하나가
    그보다 커도 그 턴은 원문으로 남긴다.

    Args:
        messages: 전체 대화 이력.
        summarized_until: 지금까지 요약에 포함된 마지막 메시지 ID.
        budget: 대화 이력 토큰 예산.
        count_tokens: 토큰 추정 함수.

    Returns:
        요약할 메시지와 새 경계. 요약할 필요가 없거나 요약할 것이 없으면 ``None``.
    """
    pending = unsummarized(messages, summarized_until)
    if count_tokens(pending) <= budget * TRIGGER_RATIO:
        return None

    human_starts = [i for i, m in enumerate(pending) if isinstance(m, HumanMessage)]
    if not human_starts:
        return None
    keep_limit = budget * KEEP_RATIO
    # 남길 수 있는 가장 앞쪽 사용자 메시지. 하나도 맞지 않으면 마지막 사용자 턴만 남긴다.
    cut = next((i for i in human_starts if count_tokens(pending[i:]) <= keep_limit), None)
    if cut is None:
        cut = human_starts[-1]
    if cut == 0:
        return None
    to_summarize = pending[:cut]
    until_id = to_summarize[-1].id
    if until_id is None:  # checkpointer 에 저장된 메시지는 항상 ID 가 있다
        return None
    return SummaryPlan(messages=to_summarize, until_id=until_id)


def format_transcript(messages: list[BaseMessage]) -> list[str]:
    """요약 모델에 넣을 대화 기록을 메시지마다 한 덩어리의 텍스트로 만든다.

    이미지 데이터는 넣지 않고 장 수만 적는다. 추론 과정은 넣지 않는다.
    요약 모델이 읽는 내용이므로 표시 문구는 영어로 쓴다.

    Args:
        messages: 요약할 메시지.

    Returns:
        메시지별 텍스트 (``"User: ..."``, ``"Assistant: ..."``, ``"Tool result (name): ..."``).
    """
    lines: list[str] = []
    for message in messages:
        text = message.text.strip()
        if len(text) > MAX_MESSAGE_CHARS:
            text = text[:MAX_MESSAGE_CHARS] + " …(truncated)"
        if isinstance(message, HumanMessage):
            images = (
                sum(1 for b in message.content if isinstance(b, dict) and b.get("type") == "image")
                if isinstance(message.content, list)
                else 0
            )
            note = f" [{images} image(s) attached]" if images else ""
            lines.append(f"User: {text}{note}")
        elif isinstance(message, AIMessage):
            calls = "".join(
                f"\n(called tool {call['name']} with {call['args']})" for call in message.tool_calls
            )
            if text or calls:
                lines.append(f"Assistant: {text}{calls}".rstrip())
        elif isinstance(message, ToolMessage):
            lines.append(f"Tool result ({message.name or 'tool'}): {text}")
    return lines


def _chunks(lines: list[str], limit: int, count_tokens: TokenCounter) -> list[str]:
    """대화 기록을 요약 호출 한 번에 들어갈 크기로 나눈다 (메시지 경계에서 자른다)."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        tokens = count_tokens([HumanMessage(line)])
        if current and size + tokens > limit:
            chunks.append("\n\n".join(current))
            current, size = [], 0
        current.append(line)
        size += tokens
    if current:
        chunks.append("\n\n".join(current))
    return chunks


async def summarize(
    model: BaseChatModel,
    prompt_name: str,
    previous_summary: str,
    messages: list[BaseMessage],
    budget: int,
    count_tokens: TokenCounter,
) -> str:
    """기존 요약에 새 메시지를 합쳐 새 요약을 만든다.

    새 메시지가 한 번에 넣기에 크면(처음 요약하는 긴 대화 등) 나눠서 차례로 이어 요약한다.

    Args:
        model: 요약에 쓸 chat model (도구 없음, 추론 끔).
        prompt_name: 요약 프롬프트 이름. 변수 ``previous_summary``, ``transcript`` 를 받는다.
        previous_summary: 지금까지의 요약. 없으면 빈 문자열.
        messages: 새로 요약에 합칠 메시지.
        budget: 대화 이력 토큰 예산. 요약 호출 한 번의 입력 크기를 정할 때 쓴다.
        count_tokens: 토큰 추정 함수.

    Returns:
        새 요약 텍스트.

    Raises:
        ValueError: 모델이 빈 요약을 돌려줬을 때.
    """
    summary = previous_summary
    for transcript in _chunks(format_transcript(messages), int(budget * CHUNK_RATIO), count_tokens):
        prompt = await prompts.arender(
            prompt_name, previous_summary=summary or NO_SUMMARY, transcript=transcript
        )
        response = await model.ainvoke(
            [HumanMessage(prompt.text)], config={"metadata": prompt.trace_metadata()}
        )
        summary = response.text.strip()
        if not summary:
            raise ValueError("summary model returned an empty summary")
    return summary
