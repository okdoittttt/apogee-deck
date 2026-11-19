"""채팅 에이전트가 사용할 도구.

여기에 ``@tool`` 함수를 추가하고 ``TOOLS`` 에 넣으면 그래프에 자동으로 연결된다.

도구의 docstring 은 LLM 에게 도구 설명으로 전달되므로 **영어로** 쓴다.
``parse_docstring=True`` 를 쓰면 Google Style 의 ``Args`` 설명이 인자 스키마의
설명으로 들어가 모델이 인자를 더 정확히 채운다.
"""

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain_core.tools import tool


@tool(parse_docstring=True)
def get_current_time(timezone: str = "Asia/Seoul") -> str:
    """Return the current date and time in the given timezone.

    Args:
        timezone: IANA timezone name, for example 'Asia/Seoul' or 'America/New_York'.

    Returns:
        The current date and time, such as '2026-11-19 15:21:00 KST (Thursday)',
        or an error message if the timezone is unknown.
    """
    try:
        now = datetime.now(ZoneInfo(timezone))
    except (ZoneInfoNotFoundError, ValueError):
        return f"Unknown timezone: {timezone}"
    return now.strftime("%Y-%m-%d %H:%M:%S %Z (%A)")


TOOLS = [get_current_time]
