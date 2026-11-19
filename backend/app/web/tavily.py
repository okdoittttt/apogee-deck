"""Tavily API 호출 (검색, 페이지 읽기가 같이 쓴다)."""

import logging
from typing import Any

import httpx

from app.agents.tooling import ToolError
from app.config import Settings

logger = logging.getLogger(__name__)

TAVILY_API_URL = "https://api.tavily.com"


async def post(
    endpoint: str,
    body: dict[str, Any],
    settings: Settings,
    *,
    action: str,
    error: type[ToolError],
    timeout: float,
    invalid_hint: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Tavily API 를 호출하고 JSON 응답을 돌려준다.

    실패하면 모델이 읽고 다음 행동을 정할 수 있는 짧은 영어 문구로 ``error`` 를 던진다.
    자세한 내용은 로그에만 남기고, 키는 로그에도 남기지 않는다. 응답에 사용 크레딧이 있으면
    로그에 남긴다 (요청에 ``include_usage`` 를 넣어야 온다).

    Args:
        endpoint: API 경로 (예: ``"/search"``).
        body: 요청 본문. 키는 넣지 않는다 (헤더로 보낸다).
        settings: 앱 설정 (키).
        action: 오류 문구와 로그에 쓸 동작 이름 (예: ``"Web search"``).
        error: 실패할 때 던질 예외 종류.
        timeout: 요청 타임아웃(초).
        invalid_hint: 요청이 잘못됐을 때(HTTP 400) 모델에게 줄 안내.
        transport: 테스트에서 가짜 응답을 넣을 때 쓰는 httpx transport.

    Returns:
        응답 JSON.

    Raises:
        ToolError: (``error`` 종류) 키 오류, 사용량 초과, 타임아웃, 서버 오류 등으로 실패했을 때.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            response = await client.post(
                TAVILY_API_URL + endpoint,
                json=body,
                headers={"Authorization": f"Bearer {settings.tavily_api_key}"},
            )
    except httpx.TimeoutException as e:
        logger.warning("%s timed out", action)
        raise error(f"{action} timed out. Try again once, or answer without it.") from e
    except httpx.HTTPError as e:
        logger.warning("%s request failed: %s", action, type(e).__name__)
        raise error(f"{action} failed temporarily. Answer without it.") from e

    if response.status_code != 200:
        # 응답 본문에는 키가 들어 있지 않지만, 길이를 제한해서 로그에만 남긴다.
        logger.warning("%s HTTP %s: %s", action, response.status_code, response.text[:200])
        raise error(_status_message(response.status_code, action, invalid_hint))

    data = response.json()
    if usage := data.get("usage"):
        logger.info("%s used %s Tavily credits", action, usage.get("credits"))
    return data


def _status_message(status: int, action: str, invalid_hint: str) -> str:
    """Tavily HTTP 오류 코드를 모델이 읽을 문구로 바꾼다."""
    if status == 401:
        return f"{action} is not configured correctly. Answer without it."
    if status in (429, 432, 433):  # 요청 빈도 제한, 플랜 사용량 초과, 종량제 한도 초과
        return f"{action} limit has been reached. Answer without it and tell the user."
    if status == 400:
        return invalid_hint
    return f"{action} failed temporarily. Answer without it."
