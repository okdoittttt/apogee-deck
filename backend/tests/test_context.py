"""컨텍스트 관리 검증: 토큰 추정, 대화 요약."""

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field

from app.agents import chat, runtime, summary
from app.agents.chat import graph as chat_graph
from app.agents.context import make_token_counter
from app.agents.runtime import TextDelta
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


# --- 대화 요약 ------------------------------------------------------------------------------------


def _ids(messages):
    for i, m in enumerate(messages):
        m.id = f"m{i}"
    return messages


def test_plan_summary_skips_when_under_trigger():
    count = make_token_counter(Settings())
    history = _ids([HumanMessage("안녕"), AIMessage("안녕하세요")])
    assert summary.plan_summary(history, None, budget=1000, count_tokens=count) is None


def test_plan_summary_keeps_recent_turns_starting_on_user_message():
    count = make_token_counter(Settings())
    long = "가" * 300  # 약 200 토큰
    history = _ids(
        [
            HumanMessage(f"첫 질문 {long}"),
            AIMessage("", tool_calls=[{"id": "t1", "name": "get_current_time", "args": {}}]),
            ToolMessage(long, tool_call_id="t1", name="get_current_time"),
            AIMessage(f"첫 답 {long}"),
            HumanMessage(f"둘째 질문 {long}"),
            AIMessage(f"둘째 답 {long}"),
            HumanMessage("셋째 질문"),
        ]
    )
    plan = summary.plan_summary(history, None, budget=1000, count_tokens=count)
    assert plan is not None
    kept = summary.unsummarized(history, plan.until_id)
    assert isinstance(kept[0], HumanMessage)
    assert count(kept) <= 1000 * summary.KEEP_RATIO
    # 도구 호출과 결과는 둘 다 요약 쪽에 있다 (갈라지지 않음)
    assert not any(isinstance(m, ToolMessage) for m in kept)
    assert plan.messages + kept == history


def test_plan_summary_keeps_last_turn_even_if_too_big():
    count = make_token_counter(Settings())
    history = _ids([HumanMessage("짧은 질문"), AIMessage("짧은 답"), HumanMessage("가" * 3000)])
    plan = summary.plan_summary(history, None, budget=1000, count_tokens=count)
    assert plan is not None and plan.until_id == "m1"


def test_transcript_replaces_images_with_a_note():
    message = HumanMessage(
        [
            {"type": "text", "text": "이게 뭐야?"},
            {"type": "image", "source_type": "base64", "mime_type": "image/png", "data": "QUJD"},
        ]
    )
    [line] = summary.format_transcript([message])
    assert line == "User: 이게 뭐야? [1 image(s) attached]"


class _SummaryModel(BaseChatModel):
    """받은 요약 프롬프트를 기록하고 ``SUMMARY n`` 을 돌려주는 요약 모델."""

    prompts_seen: list[str] = Field(default_factory=list)
    fail: bool = False

    @property
    def _llm_type(self) -> str:
        return "test-summary"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.prompts_seen.append(messages[0].text)
        if self.fail:
            raise RuntimeError("summary model down")
        text = f"SUMMARY {len(self.prompts_seen)}"
        return ChatResult(generations=[ChatGeneration(message=AIMessage(text))])


def _summarizing_graph(monkeypatch, summary_model, **settings):
    """요약이 일어나도록 예산을 작게 잡은 fake 그래프와, agent 모델이 받은 입력 기록."""
    from app.llm.fake import EchoChatModel

    seen: list = []
    original = EchoChatModel._astream

    def spy(self, messages, *args, **kwargs):
        seen.append(messages)
        return original(self, messages, *args, **kwargs)

    monkeypatch.setattr(EchoChatModel, "_astream", spy)
    monkeypatch.setattr(chat_graph, "build_summary_model", lambda s: summary_model)
    graph = chat.build_graph(
        Settings(llm_provider="fake", context_max_tokens=1500, **settings), InMemorySaver()
    )
    monkeypatch.setitem(runtime._graphs, "chat", graph)
    return graph, seen


async def _turns(thread_id: str, n: int) -> str:
    streamed = ""
    for i in range(n):
        events = [e async for e in runtime.stream_reply(thread_id, f"질문 {i} " + "가" * 300)]
        streamed += "".join(e.text for e in events if isinstance(e, TextDelta))
    return streamed


async def test_long_conversation_is_summarized(monkeypatch):
    model = _SummaryModel()
    graph, seen = _summarizing_graph(monkeypatch, model)
    streamed = await _turns("s1", 8)

    state = (await graph.aget_state({"configurable": {"thread_id": "s1"}})).values
    assert state["summary"].startswith("SUMMARY")
    assert len(state["messages"]) == 16  # 원본 이력은 그대로 남는다

    # agent 는 system + 요약과, 요약 이후의 메시지(사용자 메시지로 시작)만 받는다
    last_input = seen[-1]
    assert "<conversation_summary>" in last_input[0].text
    assert state["summary"] in last_input[0].text
    assert isinstance(last_input[1], HumanMessage)
    assert "질문 0" not in "".join(m.text for m in last_input[1:])

    # 첫 요약에는 첫 질문이 들어가고, 이후 요약은 이전 요약을 이어받는다
    assert summary.NO_SUMMARY in model.prompts_seen[0] and "질문 0" in model.prompts_seen[0]
    if len(model.prompts_seen) > 1:
        assert "SUMMARY 1" in model.prompts_seen[1]

    # 요약 모델의 출력은 화면으로 스트리밍되지 않는다
    assert "SUMMARY" not in streamed


async def test_summary_failure_falls_back_to_trimming(monkeypatch, caplog):
    graph, seen = _summarizing_graph(monkeypatch, _SummaryModel(fail=True))
    with caplog.at_level("WARNING"):
        streamed = await _turns("s2", 6)

    assert "질문 5" in streamed  # 답변은 정상적으로 나온다
    state = (await graph.aget_state({"configurable": {"thread_id": "s2"}})).values
    assert "summary" not in state
    assert "<conversation_summary>" not in seen[-1][0].text
    assert any("summary failed" in r.getMessage() for r in caplog.records)


def test_no_summary_node_without_budget():
    graph = chat.build_graph(
        Settings(llm_provider="anthropic", anthropic_api_key="x"), InMemorySaver()
    )
    assert chat_graph.SUMMARIZE_NODE not in graph.nodes
    graph = chat.build_graph(
        Settings(llm_provider="fake", context_max_tokens=1500), InMemorySaver()
    )
    assert chat_graph.SUMMARIZE_NODE in graph.nodes
