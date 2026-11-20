"""도구 실행 검증: 도구 오류 처리, 반복 제한."""

import pytest
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError

from app.agents import chat
from app.agents.chat import graph as chat_graph
from app.core.config import Settings
from app.core.errors import ToolError
from app.llm import describe_error
from app.llm.fake import FAKE_TOOL, EchoChatModel, _Plan
from app.orchestration import runtime
from app.orchestration.runtime import Finished, ToolFinished, ToolStarted
from app.orchestration.tooling import INTERNAL_TOOL_ERROR, handle_tool_error


def test_tool_error_message_is_passed_through():
    assert handle_tool_error(ToolError("Web search limit reached.")) == "Web search limit reached."


def test_unexpected_tool_error_is_hidden_and_logged(caplog):
    with caplog.at_level("ERROR"):
        message = handle_tool_error(RuntimeError("db password=secret"))
    assert message == INTERNAL_TOOL_ERROR
    assert "secret" not in message
    assert any(r.exc_info and "secret" in str(r.exc_info[1]) for r in caplog.records)


def _use_tools(monkeypatch, tools):
    monkeypatch.setattr(chat_graph, "build_tools", lambda settings, names: tools)
    graph = chat.build_graph(Settings(llm_provider="fake"), InMemorySaver())
    monkeypatch.setitem(runtime._graphs, "chat", graph)
    return graph


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ToolError("Web search failed temporarily."), "Web search failed temporarily."),
        (RuntimeError("connection to 10.0.0.5 refused"), INTERNAL_TOOL_ERROR),
    ],
)
async def test_failing_tool_does_not_fail_the_reply(monkeypatch, error, expected):
    @tool(FAKE_TOOL)
    def failing(timezone: str = "Asia/Seoul") -> str:
        """Always fails."""
        raise error

    _use_tools(monkeypatch, [failing])
    events = [
        e async for e in runtime.stream_reply("tool-err", f"몇 시야? {EchoChatModel.TOOL_MARKER}")
    ]

    finished = [e for e in events if isinstance(e, ToolFinished)]
    assert len(finished) == 1
    assert finished[0].status == "error"
    assert finished[0].output == expected
    # 모델은 오류 결과를 받고 답변을 이어간다
    assert isinstance(events[-1], Finished)
    assert expected in events[-1].text


async def test_endless_tool_calls_stop_at_recursion_limit(monkeypatch):
    @tool(FAKE_TOOL)
    def clock(timezone: str = "Asia/Seoul") -> str:
        """Returns a fixed time."""
        return "10:00"

    def always_call_tool(self, messages):
        call = {"id": f"call_{len(messages)}", "name": FAKE_TOOL, "args": {}}
        return _Plan("", "", call)

    monkeypatch.setattr(EchoChatModel, "_plan", always_call_tool)
    _use_tools(monkeypatch, [clock])

    started = 0
    with pytest.raises(GraphRecursionError) as info:
        async for event in runtime.stream_reply("tool-loop", "계속 불러"):
            started += isinstance(event, ToolStarted)
    assert 0 < started <= 8
    assert "너무 여러 번" in describe_error(info.value)
