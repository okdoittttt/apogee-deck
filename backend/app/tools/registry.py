"""도구 레지스트리: 에이전트가 이름으로 골라 쓰는 공용 도구 모음.

새 도구는 ``app/tools/<주제>.py`` 에 만들고 아래 ``TOOLS`` 에 등록한다. 에이전트는 쓸 도구의 이름 목록만
정하고 ``build_tools`` / ``tool_labels`` 로 실제 도구와 화면 문구를 받는다.

도구마다 그룹(``GROUPS``)을 정한다. 도구 결정을 켜면 결정 모델이 턴마다 그룹 단위로 필요한 도구를 고른다
(``app.orchestration.decision``). 새 그룹을 만들면 결정 모델에게 물을 질문도 같이 쓴다.

도구의 docstring 은 LLM 에게 도구 설명으로 전달되므로 **영어로** 쓴다.
``parse_docstring=True`` 를 쓰면 Google Style 의 ``Args`` 설명이 인자 스키마의
설명으로 들어가 모델이 인자를 더 정확히 채운다.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from langchain_core.tools import BaseTool

from app.core.config import Settings
from app.tools.documents import (
    make_create_document,
    make_edit_document,
    make_read_document,
    make_search_project,
    make_view_document_for_edit,
)
from app.tools.memory import (
    make_forget,
    make_remember,
    make_search_memories,
    make_update_profile,
)
from app.tools.time import get_current_time
from app.tools.web import make_fetch_url, make_web_search


@dataclass(frozen=True)
class ToolLabel:
    """도구 실행 상태를 화면에 보여줄 문구.

    Attributes:
        running: 실행 중 문구 (예: ``"시간을 확인하는 중"``).
        done: 완료 문구 (예: ``"시간 확인함"``).
    """

    running: str
    done: str


def default_label(tool_name: str) -> ToolLabel:
    """문구가 정의되지 않은 도구의 기본 문구.

    Args:
        tool_name: 도구 이름.

    Returns:
        ``"도구 실행 중: <이름>"`` / ``"도구 실행함: <이름>"``.
    """
    return ToolLabel(running=f"도구 실행 중: {tool_name}", done=f"도구 실행함: {tool_name}")


@dataclass(frozen=True)
class ToolGroup:
    """도구 결정 모델이 고르는 도구 묶음.

    Attributes:
        name: 그룹 이름.
        question: 결정 모델에게 묻는 예/아니오 질문 (영어). 상태의 필드를 백틱으로 가리킨다
            (``latest_message``: 이번 사용자 메시지, ``previous_turns``: 직전 대화).
    """

    name: str
    question: str


GROUPS: dict[str, ToolGroup] = {
    group.name: group
    for group in (
        ToolGroup("time", "Does answering `latest_message` need the current date or time?"),
        ToolGroup(
            "web",
            "Does answering `latest_message` need web search or reading a web page "
            "(news, recent or changing facts, prices, a URL)?",
        ),
        ToolGroup(
            "documents",
            "Is `latest_message` asking about an attached file or a project document, "
            "asking to change, fix or add to an attached file, or asking to create a file "
            "(text, markdown or CSV)?",
        ),
        ToolGroup(
            "memory",
            "Should the assistant save, update or delete a note about the user because of "
            "`latest_message` (the user reveals a lasting personal fact or preference, or asks "
            "to remember or forget something)?",
        ),
    )
}


@dataclass(frozen=True)
class ToolSpec:
    """레지스트리에 등록하는 도구 정의.

    Attributes:
        build: 설정을 받아 도구를 만드는 함수. 지금 설정으로 쓸 수 없으면(키 없음, 꺼짐) ``None``.
        label: 화면에 보여줄 실행 상태 문구.
        group: 도구 그룹 (``GROUPS`` 의 이름).
    """

    build: Callable[[Settings], BaseTool | None]
    label: ToolLabel
    group: str


def _web_search(settings: Settings) -> BaseTool | None:
    # 키가 없으면 도구를 빼서, 키 없이 clone 해도 앱은 그대로 동작하게 한다.
    return make_web_search(settings) if settings.tavily_api_key else None


def _fetch_url(settings: Settings) -> BaseTool | None:
    # 페이지 읽기는 키 없이도 동작한다.
    return make_fetch_url(settings) if settings.enable_fetch_url else None


def _memory_tool(make: Callable[[Settings], BaseTool]) -> Callable[[Settings], BaseTool | None]:
    # 기억 기능을 끄면 기억 도구를 모두 뺀다.
    return lambda settings: make(settings) if settings.enable_memory else None


TOOLS: dict[str, ToolSpec] = {
    "get_current_time": ToolSpec(
        lambda _: get_current_time,
        ToolLabel(running="시간을 확인하는 중", done="시간 확인함"),
        "time",
    ),
    "web_search": ToolSpec(_web_search, ToolLabel(running="웹 검색 중", done="웹 검색함"), "web"),
    "fetch_url": ToolSpec(
        _fetch_url, ToolLabel(running="페이지 읽는 중", done="페이지 읽음"), "web"
    ),
    "read_document": ToolSpec(
        make_read_document, ToolLabel(running="문서 읽는 중", done="문서 읽음"), "documents"
    ),
    "view_document_for_edit": ToolSpec(
        make_view_document_for_edit,
        ToolLabel(running="수정할 곳 보는 중", done="수정할 곳 봄"),
        "documents",
    ),
    "edit_document": ToolSpec(
        make_edit_document,
        ToolLabel(running="문서 수정하는 중", done="문서 수정함"),
        "documents",
    ),
    "create_document": ToolSpec(
        make_create_document,
        ToolLabel(running="문서 만드는 중", done="문서 만듦"),
        "documents",
    ),
    "search_project": ToolSpec(
        make_search_project,
        ToolLabel(running="프로젝트 문서 찾는 중", done="프로젝트 문서 찾음"),
        "documents",
    ),
    "remember": ToolSpec(
        _memory_tool(make_remember), ToolLabel(running="기억하는 중", done="기억함"), "memory"
    ),
    "forget": ToolSpec(
        _memory_tool(make_forget), ToolLabel(running="기억 지우는 중", done="기억 지움"), "memory"
    ),
    "update_profile": ToolSpec(
        _memory_tool(make_update_profile),
        ToolLabel(running="프로필 갱신하는 중", done="프로필 갱신함"),
        "memory",
    ),
    "search_memories": ToolSpec(
        _memory_tool(make_search_memories),
        ToolLabel(running="기억 찾는 중", done="기억 찾음"),
        "memory",
    ),
}


def build_tools(settings: Settings, names: Iterable[str]) -> list[BaseTool]:
    """이름으로 고른 도구를 설정에 맞게 만든다.

    지금 설정으로 쓸 수 없는 도구는 빠진다 (모델에게 보이지 않는다).

    Args:
        settings: 앱 설정.
        names: 도구 이름. 이 순서대로 모델에게 보인다.

    Returns:
        그래프에 연결할 도구 목록.

    Raises:
        KeyError: 등록되지 않은 도구 이름이 있을 때.
    """
    return [tool for name in names if (tool := TOOLS[name].build(settings)) is not None]


def tool_group(name: str) -> str:
    """도구가 속한 그룹 이름.

    Args:
        name: 도구 이름.

    Returns:
        그룹 이름 (``GROUPS`` 의 키).

    Raises:
        KeyError: 등록되지 않은 도구 이름일 때.
    """
    return TOOLS[name].group


def tool_labels(names: Iterable[str]) -> dict[str, ToolLabel]:
    """이름으로 고른 도구의 화면 문구.

    Args:
        names: 도구 이름.

    Returns:
        도구 이름 → 화면 문구.

    Raises:
        KeyError: 등록되지 않은 도구 이름이 있을 때.
    """
    return {name: TOOLS[name].label for name in names}
