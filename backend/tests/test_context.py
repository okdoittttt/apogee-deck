"""컨텍스트 관리 검증: 토큰 추정."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.messages.utils import count_tokens_approximately

from app.agents.context import make_token_counter
from app.config import Settings

# qwen3.5:9b 에 실제로 보내서 잰 입력 토큰 수 (prompt_eval_count, chat 템플릿 포함).
KO_TEXT = (
    "안녕하세요. 저는 다음 주에 부산으로 여행을 갈 계획인데, 해운대 근처에서 묵을 만한 숙소와 "
    "맛집을 추천해 주실 수 있나요? 예산은 하루 15만 원 정도이고, 해산물을 좋아합니다. "
) * 10
EN_TEXT = (
    "Hello. I am planning a trip to Busan next week. Could you recommend places to stay near "
    "Haeundae and good restaurants? My budget is about 150,000 won per day and I like seafood. "
) * 10
MEASURED = {"ko": 504, "en": 492}


@pytest.mark.parametrize(("lang", "text"), [("ko", KO_TEXT), ("en", EN_TEXT)])
def test_token_estimate_is_close_and_not_under(lang, text):
    estimate = make_token_counter(Settings())([HumanMessage(text)])
    actual = MEASURED[lang]
    # 넘치지 않도록 과소 추정은 10% 까지만 허용하고, 과대 추정도 20% 안으로 둔다.
    assert actual * 0.9 <= estimate <= actual * 1.2


def test_korean_was_underestimated_by_default_counter():
    # 보정 전 기본 추정은 한국어를 실제의 절반 정도로 센다 (보정이 필요한 이유).
    assert count_tokens_approximately([HumanMessage(KO_TEXT)]) < MEASURED["ko"] * 0.6


def test_tool_call_arguments_are_counted():
    count = make_token_counter(Settings())
    plain = AIMessage("")
    with_call = AIMessage(
        "", tool_calls=[{"id": "t1", "name": "search", "args": {"query": "부산 해운대 맛집 " * 20}}]
    )
    assert count([with_call]) - count([plain]) > 100
