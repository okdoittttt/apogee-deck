"""여러 에이전트가 공용으로 쓰는 도구 실행 보조: 도구 오류 처리, 반복 제한."""

import logging

from langgraph.prebuilt.tool_node import ToolInvocationError

from app.core.errors import ToolError

logger = logging.getLogger(__name__)

RECURSION_LIMIT = 16
"""한 턴에 실행할 수 있는 그래프 단계 수. 도구를 한 번 쓰면 agent, tools 두 단계가 실행되므로
도구 호출은 한 턴에 약 7번까지 할 수 있다. 모델이 도구를 끝없이 부르는 것을 막는다."""

INTERNAL_TOOL_ERROR = (
    "The tool failed because of an internal error. Do not retry it; answer without it."
)
"""예상하지 못한 도구 예외 대신 모델에게 주는 결과. 모델이 읽는 내용이라 영어로 쓴다."""


def handle_tool_error(error: Exception) -> str:
    """도구 실행 중 난 예외를 모델에게 줄 도구 결과 문구로 바꾼다.

    ``ToolNode(handle_tool_errors=...)`` 에 넘긴다. 이 함수가 문구를 돌려주면 ``ToolNode`` 는
    ``status="error"`` 인 ``ToolMessage`` 를 만들어서, 응답 전체가 실패하지 않고 화면에는
    해당 도구 단계가 "실패" 로 표시된다.

    - 인자 검증 실패(``ToolInvocationError``): 모델이 인자를 고칠 수 있게 검증 메시지를 준다.
    - ``ToolError``: 도구가 정한 문구를 준다.
    - 그 외: 예외 원문은 로그에만 남기고 일반 문구를 준다.

    Args:
        error: 도구 실행 중 발생한 예외.

    Returns:
        모델에게 줄 도구 결과 문구.
    """
    if isinstance(error, ToolInvocationError):
        return error.message
    if isinstance(error, ToolError):
        return str(error)
    logger.error("tool failed unexpectedly", exc_info=error)
    return INTERNAL_TOOL_ERROR
