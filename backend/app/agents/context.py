"""여러 에이전트가 공용으로 쓰는 컨텍스트 도구: 대화 이력 trim, 프롬프트 동적 변수."""

from collections.abc import Callable
from datetime import datetime
from functools import partial
from zoneinfo import ZoneInfo

from langchain_core.messages import BaseMessage, SystemMessage, trim_messages
from langchain_core.messages.utils import count_tokens_approximately

from app.config import Settings

ContextSelector = Callable[[SystemMessage, list[BaseMessage]], list[BaseMessage]]
"""(system 프롬프트, 전체 대화 이력) → 모델에 보낼 대화 이력."""


def make_context_selector(settings: Settings) -> ContextSelector:
    """토큰 예산 안에서 최근 대화만 남기는 함수를 만든다.

    잘라내는 것은 모델에 보낼 입력뿐이고, checkpointer 에 저장된 원본 이력은 그대로 둔다.
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
