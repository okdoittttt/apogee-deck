"""도구 결정: 결정 모델(d3) 요청, 그룹 고르기·누적·실패 처리, 그래프에서 도구 바인딩."""

import json
import uuid

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.agents import chat
from app.core.config import Settings
from app.llm.fake import EchoChatModel
from app.orchestration import decision
from app.orchestration.decision import DecisionUnavailableError
from app.tools import GROUPS, TOOLS, tool_group

SETTINGS = Settings(
    llm_provider="fake",
    enable_tool_decision=True,
    enable_memory=False,
    ollama_base_url="http://ollama:11434",
)


# --- 레지스트리 ------------------------------------------------------------------------------------


def test_every_tool_belongs_to_a_known_group():
    assert all(tool_group(name) in GROUPS for name in TOOLS)
    assert all(group.question.strip() for group in GROUPS.values())


# --- d3 요청 ---------------------------------------------------------------------------------------


async def test_ask_d3_sends_one_noul_question_per_group():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "model": "d3:2b",
                "answers": {
                    "web": {"type": "noul", "noul": 0.9},
                    "time": {"type": "noul", "noul": 0.1},
                },
                "usage": {"input_tokens": 100, "output_tokens": 0},
            },
        )

    result = await decision.ask_d3(
        SETTINGS,
        {"latest_message": "뉴스 찾아 줘"},
        {"web": "web?", "time": "time?"},
        transport=httpx.MockTransport(handler),
    )

    assert result == {"web": 0.9, "time": 0.1}
    [request] = seen
    assert str(request.url) == "http://ollama:11434/v1/systemone"
    body = json.loads(request.content)
    assert body["model"] == "d3:2b" and body["keep_alive"] == "30m"
    assert body["state"] == {"latest_message": "뉴스 찾아 줘"}
    assert body["questions"] == {
        "web": {"type": "noul", "instructions": "web?"},
        "time": {"type": "noul", "instructions": "time?"},
    }


def test_decision_url_prefers_its_own_setting():
    assert decision.decision_url(SETTINGS) == "http://ollama:11434/v1/systemone"
    own = SETTINGS.model_copy(update={"tool_decision_url": "http://d3:9000/"})
    assert decision.decision_url(own) == "http://d3:9000/v1/systemone"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404, json={"error": 'model "d3:2b" not found'}),
        httpx.Response(200, json={"answers": {}}),
        httpx.Response(200, text="not json"),
    ],
)
async def test_ask_d3_raises_when_there_is_no_usable_answer(response):
    with pytest.raises(DecisionUnavailableError):
        await decision.ask_d3(
            SETTINGS,
            {"latest_message": "x"},
            {"web": "web?"},
            transport=httpx.MockTransport(lambda _: response),
        )


async def test_ask_d3_raises_when_ollama_is_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(DecisionUnavailableError):
        await decision.ask_d3(
            SETTINGS,
            {"latest_message": "x"},
            {"web": "web?"},
            transport=httpx.MockTransport(handler),
        )


# --- 상태 -----------------------------------------------------------------------------------------


def test_state_has_the_message_without_attachments_and_recent_turns():
    messages = [
        HumanMessage("오늘 반도체 뉴스 찾아 줘"),
        AIMessage("", tool_calls=[{"id": "1", "name": "web_search", "args": {"query": "q"}}]),
        ToolMessage("검색 결과", tool_call_id="1"),
        AIMessage("뉴스 3건입니다."),
        HumanMessage("이 문서랑 비교해 줘\n\n<attachments>\nThe user attached...\n긴 본문"),
    ]

    state, attached = decision.decision_state(messages)

    assert attached
    assert state == {
        "latest_message": "이 문서랑 비교해 줘",
        "previous_turns": [
            {"role": "user", "text": "오늘 반도체 뉴스 찾아 줘"},
            {"role": "assistant", "text": "뉴스 3건입니다."},
        ],
    }


def test_state_of_the_first_message_has_no_previous_turns():
    state, attached = decision.decision_state([HumanMessage("안녕")])
    assert state == {"latest_message": "안녕"} and not attached


# --- 결정 -----------------------------------------------------------------------------------------


def _answers(**probabilities: float):
    asked: list[dict] = []

    async def ask(state, questions):
        asked.append(questions)
        return {name: probabilities.get(name, 0.0) for name in questions}

    return ask, asked


ALL = {"time", "web", "documents", "memory"}


async def test_groups_above_the_threshold_are_turned_on():
    ask, _ = _answers(web=0.99, time=0.65, memory=0.2)
    result = await decision.decide(SETTINGS, [HumanMessage("뉴스")], ALL, [], ask)
    assert result.groups == result.active == ("time", "web")
    assert result.probabilities["memory"] == 0.2


async def test_no_group_means_no_tools():
    ask, _ = _answers()
    result = await decision.decide(SETTINGS, [HumanMessage("안녕")], ALL, [], ask)
    assert result.groups == result.active == ()


async def test_groups_accumulate_and_are_not_asked_again():
    ask, asked = _answers()
    result = await decision.decide(SETTINGS, [HumanMessage("고마워")], ALL, ["web"], ask)
    assert result.active == ("web",)
    assert "web" not in asked[0]


async def test_groups_of_tools_used_earlier_stay_on():
    ask, _ = _answers()
    messages = [
        HumanMessage("몇 시야?"),
        AIMessage("", tool_calls=[{"id": "1", "name": "get_current_time", "args": {}}]),
        ToolMessage("10시", tool_call_id="1"),
        AIMessage("10시입니다."),
        HumanMessage("고마워"),
    ]
    result = await decision.decide(SETTINGS, messages, ALL, [], ask)
    assert result.active == ("time",)


async def test_attachment_turns_on_documents_without_asking():
    ask, asked = _answers()
    message = HumanMessage("요약해 줘\n\n<attachments>\n...")
    result = await decision.decide(SETTINGS, [message], ALL, [], ask)
    assert result.active == ("documents",)
    assert "documents" not in asked[0]


async def test_unavailable_groups_are_not_asked():
    ask, asked = _answers(memory=0.99)
    result = await decision.decide(SETTINGS, [HumanMessage("기억해 줘")], {"time", "web"}, [], ask)
    assert set(asked[0]) == {"time", "web"} and result.active == ()


async def test_nothing_is_asked_when_every_candidate_is_on():
    ask, asked = _answers()
    result = await decision.decide(SETTINGS, [HumanMessage("x")], {"web"}, ["web"], ask)
    assert asked == [] and result.active == ("web",)


@pytest.mark.parametrize("error", [DecisionUnavailableError("model not found"), TimeoutError()])
async def test_failure_gives_all_tools_this_turn_and_keeps_the_groups(error):
    async def ask(state, questions):
        raise error

    result = await decision.decide(SETTINGS, [HumanMessage("x")], ALL, ["web"], ask)
    assert result.active is None and result.groups == ("web",) and result.error


async def test_slow_decision_times_out():
    import asyncio

    async def ask(state, questions):
        await asyncio.sleep(1)
        return {}

    fast = SETTINGS.model_copy(update={"tool_decision_timeout_seconds": 0.01})
    result = await decision.decide(fast, [HumanMessage("x")], ALL, [], ask)
    assert result.active is None


# --- 그래프 ---------------------------------------------------------------------------------------


def _spy_bound_tools(monkeypatch) -> list[list[str]]:
    seen: list[list[str]] = []
    original = EchoChatModel._plan

    def spy(self, messages):
        seen.append(list(self.tool_names))
        return original(self, messages)

    monkeypatch.setattr(EchoChatModel, "_plan", spy)
    return seen


def _fake_d3(monkeypatch, answers: dict[str, dict[str, float]]) -> list[dict]:
    """메시지별로 정한 확률을 돌려주는 가짜 결정 모델."""
    asked: list[dict] = []

    async def ask_d3(settings, state, questions):
        asked.append({"state": state, "questions": questions})
        probabilities = answers.get(state["latest_message"], {})
        return {name: probabilities.get(name, 0.0) for name in questions}

    monkeypatch.setattr(decision, "ask_d3", ask_d3)
    return asked


async def _ask(graph, text: str, thread: str) -> dict:
    config = {"configurable": {"thread_id": thread}}
    return await graph.ainvoke({"messages": [HumanMessage(text)]}, config)


async def test_graph_gives_only_the_chosen_groups_and_keeps_them(monkeypatch):
    seen = _spy_bound_tools(monkeypatch)
    _fake_d3(monkeypatch, {"지금 몇 시야?": {"time": 0.99}})
    graph = chat.build_graph(SETTINGS, InMemorySaver())
    thread = f"t-{uuid.uuid4()}"

    await _ask(graph, "안녕!", thread)
    await _ask(graph, "지금 몇 시야?", thread)
    result = await _ask(graph, "고마워", thread)

    assert seen == [[], ["get_current_time"], ["get_current_time"]]
    assert result["tool_groups"] == ["time"] and result["active_groups"] == ["time"]


async def test_graph_decides_once_per_turn_even_with_tool_calls(monkeypatch):
    seen = _spy_bound_tools(monkeypatch)
    text = f"{EchoChatModel.TOOL_MARKER} 몇 시야?"
    asked = _fake_d3(monkeypatch, {text: {"time": 0.99}})
    graph = chat.build_graph(SETTINGS, InMemorySaver())

    result = await _ask(graph, text, f"t-{uuid.uuid4()}")

    assert len(asked) == 1
    assert seen == [["get_current_time"], ["get_current_time"]]  # 도구 실행 뒤에도 같은 도구 목록
    assert "도구 결과" in result["messages"][-1].text


async def test_graph_gives_all_tools_when_the_decision_fails(monkeypatch):
    seen = _spy_bound_tools(monkeypatch)

    async def ask_d3(settings, state, questions):
        raise DecisionUnavailableError("connection refused")

    monkeypatch.setattr(decision, "ask_d3", ask_d3)
    graph = chat.build_graph(SETTINGS, InMemorySaver())
    everything = chat.build_graph(
        SETTINGS.model_copy(update={"enable_tool_decision": False}), InMemorySaver()
    )

    result = await _ask(graph, "안녕", f"t-{uuid.uuid4()}")
    await _ask(everything, "안녕", f"t-{uuid.uuid4()}")

    assert seen[0] == seen[1] and seen[0]
    assert result["active_groups"] is None and result["tool_groups"] == []


async def test_graph_without_decision_has_no_decide_node(monkeypatch):
    asked = _fake_d3(monkeypatch, {})
    graph = chat.build_graph(
        SETTINGS.model_copy(update={"enable_tool_decision": False}), InMemorySaver()
    )
    assert "decide" not in graph.nodes
    result = await _ask(graph, "안녕", f"t-{uuid.uuid4()}")
    assert asked == [] and "tool_groups" not in result
