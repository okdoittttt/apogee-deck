"""에이전트 정의와 등록 목록.

각 에이전트는 ``app/agents/<이름>/`` 에 그래프(``graph.py``)와 프롬프트(``prompts/*.md``)를 두고,
쓸 도구는 ``app.tools`` 레지스트리에서 이름으로 고른다. 만든 에이전트는 아래 ``AGENTS`` 에 등록하면
앱이 시작할 때 실행 엔진(``app.orchestration.runtime``)에 등록된다.
"""

from app.agents import chat
from app.orchestration.runtime import AgentSpec

AGENTS: dict[str, AgentSpec] = {
    "chat": AgentSpec(chat.build_graph, chat.STREAM_NODES, chat.TOOL_LABELS),
}
DEFAULT_AGENT = "chat"
"""에이전트를 지정하지 않은 대화에 쓰는 에이전트."""

__all__ = ["AGENTS", "DEFAULT_AGENT"]
