"""여러 에이전트가 공용으로 쓰는 컨텍스트 도구: 대화 이력 정리·trim, 프롬프트 동적 변수."""

import json
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    trim_messages,
)
from langchain_core.messages.utils import count_tokens_approximately

from app.core.config import Settings
from app.llm.tokens import ASCII_CHARS_PER_TOKEN, NON_ASCII_CHARS_PER_TOKEN

TokenCounter = Callable[[Iterable[BaseMessage]], int]
"""메시지 목록 → 추정 토큰 수."""

ContextSelector = Callable[[SystemMessage, list[BaseMessage]], list[BaseMessage]]
"""(system 프롬프트, 전체 대화 이력) → 모델에 보낼 대화 이력."""

# 결과 없이 끊긴 도구 호출에 채워 넣는 결과. 모델이 읽는 내용이라 영어로 쓴다.
INTERRUPTED_TOOL_RESULT = (
    "Tool execution was interrupted before it finished because the user stopped the response. "
    "No result is available."
)


def close_dangling_tool_calls(messages: list[BaseMessage]) -> list[BaseMessage]:
    """결과가 없는 도구 호출 바로 뒤에 "중단됨" 도구 결과를 채워 넣는다.

    도구 실행 중에 응답이 중지되거나 연결이 끊기면, 도구 호출 요청(``AIMessage.tool_calls``)만
    상태에 저장되고 도구 결과(``ToolMessage``)는 없을 수 있다. 이 상태로 모델을 부르면
    Claude 는 요청을 400 으로 거부해 그 대화를 더 쓸 수 없게 되고, 다른 모델도 혼란스러워한다.

    저장된 상태는 바꾸지 않고 모델 입력만 고친다. 그래서 이미 뒤에 다른 메시지가 쌓인
    대화도 올바른 위치에 결과를 넣을 수 있고, 매번 같은 내용을 넣으므로 프롬프트 캐시도 유지된다.

    Args:
        messages: 대화 이력.

    Returns:
        모든 도구 호출에 결과가 짝지어진 대화 이력. 고칠 것이 없으면 같은 내용 그대로.
    """
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    result: list[BaseMessage] = []
    for message in messages:
        result.append(message)
        if isinstance(message, AIMessage):
            result.extend(
                ToolMessage(
                    content=INTERRUPTED_TOOL_RESULT,
                    tool_call_id=call["id"],
                    name=call["name"],
                    status="error",
                )
                for call in message.tool_calls
                if call["id"] not in answered
            )
    return result


def _text_of(message: BaseMessage) -> str:
    """토큰 수를 셀 때 쓰는 메시지의 텍스트 (본문 텍스트 블록과 도구 호출 인자)."""
    if isinstance(message.content, str):
        parts = [message.content]
    else:
        parts = [
            block if isinstance(block, str) else str(block.get("text", ""))
            for block in message.content
            if isinstance(block, str) or block.get("type") == "text"
        ]
    if isinstance(message, AIMessage):
        parts.extend(json.dumps(call["args"], ensure_ascii=False) for call in message.tool_calls)
    return "".join(parts)


def make_token_counter(settings: Settings) -> TokenCounter:
    """한국어도 과소 추정하지 않는 토큰 추정 함수를 만든다.

    ``count_tokens_approximately`` 는 모든 글자를 4자당 1토큰으로 세서, 한국어는 실제의
    절반 정도로 센다. 그러면 예산 안에 들어간 줄 알았던 입력이 ``num_ctx`` 를 넘고, Ollama 는
    오류 없이 입력 앞부분을 잘라낸다. 그래서 기본 추정값에 ASCII 가 아닌 글자 몫을 더한다.
    이미지는 base64 길이가 아니라 장당 ``image_token_estimate`` 토큰으로 센다.

    Args:
        settings: 앱 설정.

    Returns:
        메시지 목록을 받아 추정 토큰 수를 돌려주는 함수.
    """
    extra_per_char = 1 / NON_ASCII_CHARS_PER_TOKEN - 1 / ASCII_CHARS_PER_TOKEN

    def count(messages: Iterable[BaseMessage]) -> int:
        """메시지 목록의 토큰 수를 추정한다.

        Args:
            messages: 셀 메시지.

        Returns:
            추정 토큰 수.
        """
        messages = list(messages)
        base = count_tokens_approximately(
            messages,
            chars_per_token=ASCII_CHARS_PER_TOKEN,
            tokens_per_image=settings.image_token_estimate,
        )
        non_ascii = sum(1 for m in messages for ch in _text_of(m) if ord(ch) > 127)
        return base + round(non_ascii * extra_per_char)

    return count


ToolResultCompactor = Callable[[ToolMessage, dict[str, Any]], str | None]
"""(지난 도구 결과, 그 도구를 부른 인자) → 줄인 결과. ``None`` 이면 기본 규칙을 쓴다."""

COMPACT_THRESHOLD_CHARS = 500
"""기본 규칙: 이보다 긴 지난 도구 결과만 줄인다 (시간 조회처럼 짧은 결과는 그대로)."""

COMPACT_KEEP_CHARS = 300
"""기본 규칙: 줄일 때 앞부분을 이만큼 남긴다."""


def compact_old_tool_results(
    messages: list[BaseMessage],
    compactors: Mapping[str, ToolResultCompactor] | None = None,
) -> list[BaseMessage]:
    """이전 턴의 도구 결과를 짧게 줄인 이력을 돌려준다 (모델에 보낼 입력용).

    도구 결과(웹 검색 한 번에 수천 토큰)는 상태에 남아 이후 모든 턴에 다시 입력된다.
    필요한 내용은 그 턴의 답변에 이미 정리돼 있으므로, 마지막 사용자 메시지보다 앞에 있는
    도구 결과는 줄여서 보낸다. 이번 턴의 결과는 도구를 반복하는 중이라도 그대로 둔다.

    줄이는 규칙 (모델이 읽는 내용이라 표시 문구는 영어로 쓴다):

    - ``compactors`` 에 그 도구의 함수가 있으면 그 결과를 쓴다.
    - artifact 에 출처(``sources``)가 있으면(웹 검색 등) 호출 인자와 출처 목록만 남긴다.
    - 그 밖에 ``COMPACT_THRESHOLD_CHARS`` 보다 길면 앞부분 ``COMPACT_KEEP_CHARS`` 자만 남긴다.
    - 오류 결과와 짧은 결과는 그대로 둔다.

    원본은 바꾸지 않고, 줄인 메시지는 같은 ID 의 복사본이다 (요약 경계를 ID 로 찾기 때문).
    도구 결과는 다음 사용자 턴이 시작될 때 한 번만 바뀌고 그 뒤로는 그대로라, Anthropic
    프롬프트 캐시도 그 지점에서 한 번만 다시 만들어진다.

    Args:
        messages: 대화 이력.
        compactors: 도구 이름 → 줄이는 함수. 도구마다 다른 방식이 필요할 때 넘긴다.

    Returns:
        지난 도구 결과를 줄인 이력. 줄일 것이 없으면 같은 내용 그대로.
    """
    last_human = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=-1)
    calls = {
        call["id"]: call["args"]
        for m in messages[: last_human + 1]
        if isinstance(m, AIMessage)
        for call in m.tool_calls
    }
    result: list[BaseMessage] = []
    for i, message in enumerate(messages):
        if i < last_human and isinstance(message, ToolMessage) and message.status != "error":
            args = calls.get(message.tool_call_id, {})
            compactor = (compactors or {}).get(message.name or "")
            short = (compactor(message, args) if compactor else None) or _default_compact(
                message, args
            )
            if short is not None:
                message = message.model_copy(update={"content": short})
        result.append(message)
    return result


MODEL_TAG = "app_model"
"""AI 메시지의 ``response_metadata`` 에 응답한 모델 ID 를 적는 키 (``tag_model`` 참고)."""

_REASONING_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking", "reasoning"})
# 추론 원문과, 추론을 이어가기 위해 provider 가 메시지에 붙이는 값.
_REASONING_KWARGS = ("reasoning_content", "__gemini_function_call_thought_signatures__")


def tag_model(message: AIMessage, model: str) -> AIMessage:
    """응답한 모델 ID 를 메시지에 기록한다 (``drop_foreign_reasoning`` 이 쓴다).

    Args:
        message: 모델 응답.
        model: 모델 ID.

    Returns:
        같은 메시지 (``response_metadata`` 에 ``MODEL_TAG`` 를 추가).
    """
    message.response_metadata = {**message.response_metadata, MODEL_TAG: model}
    return message


def drop_foreign_reasoning(messages: list[BaseMessage], model: str) -> list[BaseMessage]:
    """다른 모델이 만든 AI 메시지에서 추론 블록을 뺀 이력을 돌려준다 (모델에 보낼 입력용).

    대화 도중 모델을 바꾸면 이전 답변의 추론 블록이 다른 provider 로 간다. 추론 블록은
    만든 provider 의 서명이 붙어 있어서, 다른 모델에 보내면 요청이 거절되거나
    (Anthropic 은 서명을 검증한다) 쓸모없는 입력이 된다. 답변 텍스트와 도구 호출은 그대로 둔다.
    모델을 기록하기 전의 메시지도 어느 모델이 만들었는지 모르므로 추론을 뺀다.

    Args:
        messages: 대화 이력.
        model: 지금 호출할 모델 ID.

    Returns:
        추론을 뺀 이력. 바꾼 메시지는 같은 ID 의 복사본이고, 원본은 바꾸지 않는다.
    """
    out: list[BaseMessage] = []
    for m in messages:
        if not isinstance(m, AIMessage) or m.response_metadata.get(MODEL_TAG) == model:
            out.append(m)
            continue
        content = m.content
        if isinstance(content, list):
            content = [
                b
                for b in content
                if not (isinstance(b, dict) and b.get("type") in _REASONING_BLOCK_TYPES)
            ]
        kwargs = {k: v for k, v in m.additional_kwargs.items() if k not in _REASONING_KWARGS}
        if content == m.content and kwargs == m.additional_kwargs:
            out.append(m)
        else:
            out.append(m.model_copy(update={"content": content or "", "additional_kwargs": kwargs}))
    return out


def _default_compact(message: ToolMessage, args: dict[str, Any]) -> str | None:
    """기본 규칙으로 지난 도구 결과를 줄인다. 줄일 필요가 없으면 ``None``."""
    name = message.name or "tool"
    called = f"Called with: {json.dumps(args, ensure_ascii=False)}" if args else ""
    sources = message.artifact.get("sources") if isinstance(message.artifact, dict) else None
    if sources:
        lines = [f"[Earlier {name} result, shortened. Call it again if you need the details.]"]
        if called:
            lines.append(called)
        for i, source in enumerate(sources, 1):
            n = source.get("n") or i
            if source.get("type") == "document":
                where = f" · {source['location']}" if source.get("location") else ""
                lines.append(f"[{n}] {source.get('title', '')}{where}")
                continue
            date = f" ({source['published_date']})" if source.get("published_date") else ""
            lines.append(f"[{n}] {source.get('title', '')} - {source.get('url', '')}{date}")
        return "\n".join(lines)
    text = message.text
    if len(text) <= COMPACT_THRESHOLD_CHARS:
        return None
    header = f"[Earlier {name} result, shortened to the first {COMPACT_KEEP_CHARS} characters.]"
    return "\n".join(part for part in (header, called, text[:COMPACT_KEEP_CHARS] + " …") if part)


def make_context_selector(settings: Settings) -> ContextSelector:
    """모델에 보낼 대화 이력을 고르는 함수를 만든다.

    결과 없이 끊긴 도구 호출을 먼저 메운 뒤(``close_dangling_tool_calls``), 토큰 예산 안에서
    최근 대화만 남긴다. 바꾸는 것은 모델에 보낼 입력뿐이고, checkpointer 에 저장된 원본
    이력은 그대로 둔다.
    토큰 수는 ``make_token_counter`` 로 센다.

    Args:
        settings: 앱 설정. ``history_token_budget`` 이 ``None`` 이면 자르지 않는다.

    Returns:
        ``(system, messages)`` 를 받아 예산 안의 최근 메시지를 돌려주는 함수.
    """
    count_tokens = make_token_counter(settings)
    budget = settings.history_token_budget

    def select(system: SystemMessage, messages: list[BaseMessage]) -> list[BaseMessage]:
        """System 프롬프트를 뺀 예산 안에서 최근 메시지를 고른다.

        Args:
            system: 모델 입력 맨 앞에 붙일 system 프롬프트 (예산 계산에만 쓴다).
            messages: checkpointer 에 저장된 전체 대화 이력.

        Returns:
            사용자 메시지로 시작하는 최근 메시지. 마지막 메시지 하나가 예산보다 커도
            최소한 그 메시지는 보낸다.
        """
        messages = close_dangling_tool_calls(messages)
        if budget is None:
            return messages
        trimmed = trim_messages(
            messages,
            max_tokens=budget - count_tokens([system]),
            token_counter=count_tokens,
            strategy="last",
            start_on="human",  # 잘린 뒤에도 사용자 메시지로 시작 (tool 결과가 고아가 되지 않게)
            end_on=("human", "tool"),
            allow_partial=False,
        )
        return trimmed or messages[-1:]

    return select


def today(settings: Settings) -> str:
    """프롬프트에 넣을 오늘 날짜.

    날짜 단위로만 바뀌므로 Claude 의 프롬프트 캐시를 깨지 않는다.

    Args:
        settings: 앱 설정 (``app_timezone`` 기준).

    Returns:
        ``"2026-11-19 (Thursday)"`` 형식의 문자열.
    """
    return datetime.now(ZoneInfo(settings.app_timezone)).strftime("%Y-%m-%d (%A)")
