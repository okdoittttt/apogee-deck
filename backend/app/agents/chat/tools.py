"""채팅 에이전트가 사용할 도구.

여기에 ``@tool`` 함수를 추가하고 ``build_tools`` 가 돌려주게 하면 그래프에 자동으로 연결된다.
설정이 필요한 도구(API 키 등)는 ``build_tools`` 안에서 설정을 보고 만든다.
화면에 보여줄 실행 상태 문구는 ``TOOL_LABELS`` 에 넣는다 (없으면 기본 문구).

도구의 docstring 은 LLM 에게 도구 설명으로 전달되므로 **영어로** 쓴다.
``parse_docstring=True`` 를 쓰면 Google Style 의 ``Args`` 설명이 인자 스키마의
설명으로 들어가 모델이 인자를 더 정확히 채운다.
"""

from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain_core.tools import BaseTool, tool

from app.agents.events import ToolLabel
from app.config import Settings
from app.web import search


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


def make_web_search(settings: Settings) -> BaseTool:
    """설정(키, 검색 깊이 등)을 담은 웹 검색 도구를 만든다.

    Args:
        settings: 앱 설정.

    Returns:
        ``web_search`` 도구.
    """

    # 모델에게는 정리한 텍스트를, 화면에는 출처 목록(artifact)을 따로 준다.
    @tool(parse_docstring=True, response_format="content_and_artifact")
    async def web_search(
        query: str,
        topic: Literal["general", "news"] = "general",
        time_range: Literal["day", "week", "month", "year"] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Search the web and return relevant excerpts with their source URLs.

        Use this for anything that may have changed after your training or that you are
        not sure about: news, current figures (prices, interest rates, scores), recent
        releases and events, schedules, local information such as restaurants, details of
        specific products or laws, and how to do something with a specific library,
        framework, or API (memory of APIs is often outdated, so check the docs). Do not use
        it for greetings, opinions, or basic concepts you already know well.

        Results are ranked by relevance, not by date, even when a period is set. For
        time-sensitive questions, limit the period; otherwise old pages with matching
        wording (for example yesterday's "tomorrow's weather" article) can rank first.

        Args:
            query: A concise search-engine query with the key terms, not a full sentence.
                Use the language most likely to find good sources (for example Korean
                for Korean local information, English for programming documentation).
                For time-sensitive questions, include the specific date instead of
                relative words like "tomorrow".
            topic: 'news' for news articles (each result has a published date);
                'general' for everything else.
            time_range: Only return pages published within this period before today.
                Use it for recent or upcoming things ('day' for today's or tomorrow's
                news and forecasts, 'week' or 'month' for recent developments). Leave it
                empty for timeless topics or past events.
            start_date: Only return pages published on or after this date (YYYY-MM-DD).
                Use with end_date to search a specific past period. Do not combine with
                time_range.
            end_date: Only return pages published on or before this date (YYYY-MM-DD).
        """
        request = search.SearchRequest(query, topic, time_range, start_date, end_date)
        results = await search.search(request, settings)
        today = datetime.now(ZoneInfo(settings.app_timezone)).date()
        return search.format_results(request, results, today), search.sources_artifact(results)

    return web_search


def build_tools(settings: Settings) -> list[BaseTool]:
    """설정에 따라 채팅 에이전트가 쓸 도구 목록을 만든다.

    키가 필요한 도구는 키가 있을 때만 넣는다. 키 없이 clone 한 경우에도 앱은 그대로 동작하고,
    해당 도구만 모델에게 보이지 않는다.

    Args:
        settings: 앱 설정.

    Returns:
        그래프에 연결할 도구 목록.
    """
    tools: list[BaseTool] = [get_current_time]
    if settings.tavily_api_key:
        tools.append(make_web_search(settings))
    return tools


TOOL_LABELS: dict[str, ToolLabel] = {
    "get_current_time": ToolLabel(running="시간을 확인하는 중", done="시간 확인함"),
    "web_search": ToolLabel(running="웹 검색 중", done="웹 검색함"),
}
