"""시간 도구."""

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
