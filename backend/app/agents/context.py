"""여러 에이전트가 공용으로 쓰는 컨텍스트 도구: 대화 이력 정리·trim, 프롬프트 동적 변수."""

import json
from collections.abc import Callable, Iterable
from datetime import datetime
from zoneinfo import ZoneInfo

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    SystemMessage,
    ToolMessage,
    trim_messages,
)
from langchain_core.messages.utils import count_tokens_approximately

from app.config import Settings

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


# ASCII 가 아닌 글자(한글 등)는 ASCII 보다 토큰을 훨씬 많이 쓴다. qwen3.5 로 실측하면
# 한국어 문장은 글자당 약 0.61 토큰이었다(ASCII 는 글자당 약 0.25). 넘치지 않도록 조금 크게 잡는다.
NON_ASCII_CHARS_PER_TOKEN = 1.5
ASCII_CHARS_PER_TOKEN = 4.0


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
