"""웹 페이지 본문 읽기 (``fetch_url`` 도구).

페이지를 직접 받아서(내부 주소 접속 방지, ``safe_http``) trafilatura 로 본문, 제목, 발행일을 뽑는다.
메뉴, 로그인 안내 같은 사이트 잡음이 빠져서 같은 길이 안에 실제 내용이 훨씬 많이 들어간다.
직접 받기가 실패하면(봇 차단, 자바스크립트로 그리는 페이지, PDF 등) Tavily extract 로 다시 시도한다.
내부 주소라서 막힌 URL 은 Tavily 로도 보내지 않는다.
"""

import logging
import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Any

import anyio
import httpx
import trafilatura

from app.agents.tooling import ToolError
from app.config import Settings
from app.web import safe_http, tavily
from app.web.safe_http import BlockedAddressError, ResponseTooLargeError
from app.web.text import age_label, external_note, untag

logger = logging.getLogger(__name__)

PAGE_TAG = "page_content"
"""모델에게 주는 페이지 본문을 감싸는 태그. 태그 안은 외부 웹 페이지 내용이다."""

MIN_TEXT_CHARS = 200
"""직접 받아 뽑은 본문이 이보다 짧으면 제대로 뽑지 못한 것으로 보고 Tavily 로 다시 시도한다."""

CACHE_SECONDS = 600
CACHE_MAX_PAGES = 32
"""같은 페이지를 ``offset`` 으로 이어 읽을 때 다시 받지 않도록 뽑은 본문을 잠시 보관한다."""

USER_AGENT = "Mozilla/5.0 (compatible; agent-page-reader/1.0)"

_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_TEXT_TYPES = frozenset({"text/plain", "text/markdown"})


class FetchError(ToolError):
    """페이지를 읽지 못했다. 메시지는 모델이 읽는 짧은 영어 문구다."""


@dataclass(frozen=True)
class Page:
    """읽은 페이지.

    Attributes:
        url: 최종 URL (리다이렉트를 따라간 뒤).
        title: 페이지 제목. 모르면 URL.
        text: 본문 (markdown).
        published_date: 발행일 (``YYYY-MM-DD``). 모르면 ``None``.
        via: 읽은 방법. ``"direct"`` (직접 받기) 또는 ``"tavily"``.
    """

    url: str
    title: str
    text: str
    published_date: str | None
    via: str


_cache: dict[str, tuple[float, Page]] = {}


def clear_cache() -> None:
    """보관한 페이지를 모두 지운다 (테스트용)."""
    _cache.clear()


async def read_page(
    url: str,
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    tavily_transport: httpx.AsyncBaseTransport | None = None,
) -> Page:
    """페이지 본문을 읽는다. 직접 받기가 실패하면 (키가 있을 때) Tavily extract 로 다시 시도한다.

    Args:
        url: 읽을 URL.
        settings: 앱 설정 (타임아웃, 크기 제한, Tavily 키).
        transport: 직접 받기에 쓸 테스트용 transport.
        tavily_transport: Tavily 호출에 쓸 테스트용 transport.

    Returns:
        읽은 페이지.

    Raises:
        FetchError: 허용되지 않는 주소이거나, 두 방법 모두 실패했을 때.
    """
    url = url.strip()
    try:
        safe_http.check_url(url)
    except BlockedAddressError as e:
        raise FetchError(_not_allowed()) from e

    if cached := _cache.get(url):
        expires, page = cached
        if expires > time.monotonic():
            return page

    try:
        page, reason = await _read_direct(url, settings, transport)
    except BlockedAddressError as e:
        # 내부 주소로 풀리거나 리다이렉트된 URL: 외부 서비스(Tavily)로도 보내지 않는다.
        logger.warning("fetch blocked: %s", e)
        raise FetchError(_not_allowed()) from e

    if page is None and settings.tavily_api_key:
        logger.info("direct fetch failed (%s); trying Tavily extract", reason)
        page = await _read_tavily(url, settings, tavily_transport)
    if page is None:
        raise FetchError(f"The page could not be read ({reason}). Try another source.")

    if len(_cache) >= CACHE_MAX_PAGES:
        _cache.pop(next(iter(_cache)))
    _cache[url] = (time.monotonic() + CACHE_SECONDS, page)
    return page


def _not_allowed() -> str:
    return "This address is not allowed. Only public web pages can be read."


async def _read_direct(
    url: str, settings: Settings, transport: httpx.AsyncBaseTransport | None
) -> tuple[Page | None, str]:
    """페이지를 직접 받아 본문을 뽑는다. 실패하면 ``(None, 이유)``.

    Raises:
        BlockedAddressError: 내부 주소로 풀리거나 리다이렉트될 때 (호출하는 쪽이 처리한다).
    """
    try:
        response = await safe_http.fetch(
            url,
            timeout=settings.fetch_timeout_seconds,
            max_bytes=settings.fetch_max_bytes,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9,*/*;q=0.5"},
            transport=transport,
        )
    except ResponseTooLargeError:
        return None, "the page is too large"
    except httpx.TimeoutException:
        return None, "timed out"
    except httpx.HTTPError as e:
        return None, f"network error: {type(e).__name__}"

    if response.status_code >= 400:
        return None, f"HTTP {response.status_code}"
    if response.content_type in _TEXT_TYPES:
        text = response.content.decode(response.encoding or "utf-8", errors="replace").strip()
        title = response.url
        return (Page(response.url, title, text, None, "direct"), "") if text else (None, "empty")
    if response.content_type not in _HTML_TYPES:
        return None, f"unsupported content type {response.content_type or '(none)'}"

    # trafilatura 는 CPU 를 쓰는 동기 함수라 이벤트 루프를 막지 않게 스레드에서 실행한다.
    page = await anyio.to_thread.run_sync(_extract_html, response.content, response.url)
    if page is None or len(page.text) < MIN_TEXT_CHARS:
        return None, "could not find the main text"
    return page, ""


def _extract_html(html: bytes, url: str) -> Page | None:
    """HTML 에서 본문(markdown), 제목, 발행일을 뽑는다."""
    text = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_tables=True,
        include_links=False,
        include_images=False,
        include_comments=False,
    )
    if not text:
        return None
    metadata = trafilatura.extract_metadata(html, default_url=url)
    title = (metadata.title if metadata and metadata.title else "") or url
    published = metadata.date if metadata and metadata.date else None
    return Page(url, title.strip(), text.strip(), published, "direct")


_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")


async def _read_tavily(
    url: str, settings: Settings, transport: httpx.AsyncBaseTransport | None
) -> Page | None:
    """Tavily extract 로 페이지를 읽는다. 읽지 못하면 ``None``.

    Raises:
        FetchError: Tavily 호출 자체가 실패했을 때 (키 오류, 사용량 초과 등).
    """
    data = await tavily.post(
        "/extract",
        {"urls": [url], "extract_depth": "advanced", "format": "markdown", "include_usage": True},
        settings,
        action="Reading the page",
        error=FetchError,
        timeout=settings.fetch_timeout_seconds * 2,  # advanced 추출은 느릴 수 있다
        invalid_hint="The page request was invalid. Check that the URL is a complete web address.",
        transport=transport,
    )
    results = data.get("results") or []
    if not results:
        logger.info("Tavily extract failed: %s", data.get("failed_results"))
        return None
    item = results[0]
    # 직접 받기처럼 깨끗하지는 않지만, 이미지와 링크 주소는 빼서 토큰을 아낀다.
    text = _MD_LINK.sub(r"\1", _MD_IMAGE.sub("", item.get("raw_content") or "")).strip()
    if not text:
        return None
    return Page(item.get("url") or url, (item.get("title") or url).strip(), text, None, "tavily")


def format_page(page: Page, offset: int, max_chars: int, today: date) -> str:
    """페이지 본문의 일부를 모델이 읽을 텍스트로 만든다. 표시 문구는 모델이 읽으므로 영어로 쓴다.

    긴 페이지는 ``max_chars`` 씩 나눠서, 다음에 이어 읽을 ``offset`` 을 알려준다.

    Args:
        page: 읽은 페이지.
        offset: 보여줄 첫 글자 위치.
        max_chars: 한 번에 보여줄 최대 글자 수.
        today: 오늘 날짜 (발행일이 며칠 전인지 계산할 때 쓴다).

    Returns:
        제목, URL, 발행일, 보여준 범위와 이어 읽는 방법, 태그로 감싼 본문.
    """
    total = len(page.text)
    if offset >= total:
        return f"offset {offset} is past the end of the page ({total:,} characters). Nothing more to read."
    chunk = page.text[offset : offset + max_chars]
    if offset + max_chars < total:
        cut = chunk.rfind("\n")
        if cut > max_chars // 2:
            chunk = chunk[:cut]
    end = offset + len(chunk)
    lines = [f"Page: {untag(page.title, PAGE_TAG)}", f"URL: {page.url}"]
    if page.published_date:
        lines.append(f"Published: {page.published_date}{age_label(page.published_date, today)}")
    progress = f"Showing characters {offset:,}-{end:,} of {total:,}."
    if end < total:
        progress += f" Call fetch_url again with offset={end} to read more."
    else:
        progress += " This is the end of the page."
    lines.append(progress)
    return "\n\n".join(
        [
            "\n".join(lines),
            external_note(PAGE_TAG),
            f"<{PAGE_TAG}>\n{untag(chunk.strip(), PAGE_TAG)}\n</{PAGE_TAG}>",
            "Do not write a sources or references list: the app shows this page to the user as a link.",
        ]
    )


def page_artifact(page: Page) -> dict[str, Any]:
    """화면의 출처 목록에 보여줄 페이지 (도구의 artifact, 모델 입력에는 들어가지 않는다).

    Args:
        page: 읽은 페이지.

    Returns:
        ``{"sources": [{"title", "url", "published_date"}]}``.
    """
    return {
        "sources": [{"title": page.title, "url": page.url, "published_date": page.published_date}]
    }
