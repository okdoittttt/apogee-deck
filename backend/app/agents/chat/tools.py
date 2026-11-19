"""에이전트가 사용할 도구. 여기에 @tool 함수를 추가하면 그래프에 자동으로 연결된다."""

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain_core.tools import tool


@tool
def get_current_time(timezone: str = "Asia/Seoul") -> str:
    """Return the current date and time in the given IANA timezone (e.g. 'Asia/Seoul')."""
    try:
        now = datetime.now(ZoneInfo(timezone))
    except (ZoneInfoNotFoundError, ValueError):
        return f"Unknown timezone: {timezone}"
    return now.strftime("%Y-%m-%d %H:%M:%S %Z (%A)")


TOOLS = [get_current_time]
