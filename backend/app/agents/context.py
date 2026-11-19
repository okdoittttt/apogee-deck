"""여러 에이전트가 공용으로 쓰는 컨텍스트 도구: 대화 이력 trim, 프롬프트 동적 변수."""

from collections.abc import Callable
from datetime import datetime
from functools import partial
from zoneinfo import ZoneInfo

from langchain_core.messages import BaseMessage, SystemMessage, trim_messages
from langchain_core.messages.utils import count_tokens_approximately

from app.config import Settings

ContextSelector = Callable[[SystemMessage, list[BaseMessage]], list[BaseMessage]]


def make_context_selector(settings: Settings) -> ContextSelector:
    """토큰 예산 안에서 최근 대화만 남기는 함수를 만든다. 상태(checkpointer)의 원본은 그대로 둔다."""
    # 이미지는 base64 길이가 아니라 장당 고정 토큰으로 계산한다.
    count_tokens = partial(
        count_tokens_approximately, tokens_per_image=settings.image_token_estimate
    )
    budget = settings.history_token_budget

    def select(system: SystemMessage, messages: list[BaseMessage]) -> list[BaseMessage]:
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
        # 마지막 메시지 하나가 예산보다 크면 빈 리스트가 되므로, 최소한 마지막 턴은 보낸다.
        return trimmed or messages[-1:]

    return select


def today(settings: Settings) -> str:
    """프롬프트용 오늘 날짜. 날짜 단위로만 바뀌므로 프롬프트 캐시를 깨지 않는다."""
    return datetime.now(ZoneInfo(settings.app_timezone)).strftime("%Y-%m-%d (%A)")
