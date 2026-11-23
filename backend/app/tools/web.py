"""웹 도구: 검색(``web_search``)과 페이지 읽기(``fetch_url``).

실제 검색·읽기는 ``app.web`` 이 하고, 여기서는 모델이 부를 수 있는 도구 모양으로 감싼다.
"""

from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from langchain_core.tools import BaseTool, tool

from app.core.config import Settings
from app.web import fetch, search


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

        Results contain only short excerpts. To read a result in full, call fetch_url
        with its URL.

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


def make_fetch_url(settings: Settings) -> BaseTool:
    """설정(길이, 타임아웃, 대체용 Tavily 키)을 담은 페이지 읽기 도구를 만든다.

    Args:
        settings: 앱 설정.

    Returns:
        ``fetch_url`` 도구.
    """

    # 모델에게는 본문을, 화면에는 출처(artifact)를 따로 준다.
    @tool(parse_docstring=True, response_format="content_and_artifact")
    async def fetch_url(url: str, offset: int = 0) -> tuple[str, dict[str, Any]]:
        """Read the main text of a web page, without menus or ads, with its title and date.

        Use it when the user gives you a link, when search excerpts are not enough and you
        need the full content of a result, or when you need exact details from official
        documentation. Long pages are returned in parts; the result tells you the offset
        to continue from. Only public web pages can be read.

        Args:
            url: The full http(s) address of the page.
            offset: Character position to start from, for reading the next part of a long
                page (use the value given in the previous result). 0 for the beginning.
        """
        page = await fetch.read_page(url, settings)
        today = datetime.now(ZoneInfo(settings.app_timezone)).date()
        text = fetch.format_page(page, max(offset, 0), settings.fetch_max_chars, today)
        return text, fetch.page_artifact(page)

    return fetch_url
