"""여러 에이전트가 공용으로 쓰는 컨텍스트 도구: 대화 이력 정리·trim, 프롬프트 동적 변수."""

from collections.abc import Callable
from datetime import datetime
from functools import partial
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


def make_context_selector(settings: Settings) -> ContextSelector:
    """모델에 보낼 대화 이력을 고르는 함수를 만든다.

    결과 없이 끊긴 도구 호출을 먼저 메운 뒤(``close_dangling_tool_calls``), 토큰 예산 안에서
    최근 대화만 남긴다. 바꾸는 것은 모델에 보낼 입력뿐이고, checkpointer 에 저장된 원본
    이력은 그대로 둔다.
    이미지는 base64 길이가 아니라 장당 ``image_token_estimate`` 토큰으로 계산한다.

    Args:
        settings: 앱 설정. ``history_token_budget`` 이 ``None`` 이면 자르지 않는다.

    Returns:
        ``(system, messages)`` 를 받아 예산 안의 최근 메시지를 돌려주는 함수.
    """
    count_tokens = partial(
        count_tokens_approximately, tokens_per_image=settings.image_token_estimate
    )
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
