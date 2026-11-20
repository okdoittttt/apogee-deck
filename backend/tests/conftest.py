"""테스트 공용 설정."""

import pytest

from app.agents import AGENTS, DEFAULT_AGENT
from app.orchestration import runtime


@pytest.fixture(autouse=True)
def registered_agents():
    """앱 시작 때처럼 에이전트를 실행 엔진에 등록해 둔다 (테스트마다 그래프 캐시도 비운다)."""
    runtime.register(AGENTS, DEFAULT_AGENT)
    yield
    runtime.register(AGENTS, DEFAULT_AGENT)
