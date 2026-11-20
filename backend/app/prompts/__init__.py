"""프롬프트 진입점. 에이전트 코드는 ``render(이름, **변수)`` 만 사용한다.

``PROMPT_SOURCE=langfuse`` 이면 Langfuse 에서 라벨(``PROMPT_LABEL``)이 붙은 버전을 쓰고,
가져올 수 없으면 로컬 파일(git 원본)로 렌더링한다. ``PROMPT_SOURCE=file`` 이면 항상 로컬 파일을 쓴다.
"""

import asyncio
from typing import Any

from app.core.config import get_settings
from app.prompts.loader import (
    Prompt,
    PromptError,
    compile_for_langfuse,
    list_prompts,
    load_template,
    validate_all,
)
from app.prompts.loader import render as render_file

__all__ = [
    "Prompt",
    "PromptError",
    "arender",
    "compile_for_langfuse",
    "list_prompts",
    "render",
    "render_file",
    "validate_all",
]


def render(name: str, /, **variables: Any) -> Prompt:
    """이름으로 프롬프트를 렌더링한다.

    변수 검증은 Langfuse 를 쓰더라도 항상 로컬 템플릿 기준으로 한다. 그래서 프롬프트
    출처와 상관없이 호출하는 쪽의 계약은 같다.

    Args:
        name: 프롬프트 이름 (예: ``"chat/system"``). positional-only 라서 프롬프트 변수
            이름으로 ``name`` 을 써도 충돌하지 않는다.
        **variables: 템플릿 변수. frontmatter 의 ``variables`` 와 정확히 일치해야 한다.

    Returns:
        렌더링된 프롬프트. Langfuse 버전을 썼으면 ``source="langfuse"`` 와 버전,
        트레이스 연결용 ``langfuse_prompt`` 가 들어 있다.

    Raises:
        PromptError: 이름이 잘못됐거나 변수가 빠지거나 남을 때.
    """
    local = render_file(name, **variables)

    if get_settings().prompt_source != "langfuse":
        return local

    from app.prompts import remote

    remote_prompt = remote.fetch(name)
    if remote_prompt is None:
        return local

    declared = load_template(name).variables
    text = remote_prompt.compile(**{k: v for k, v in variables.items() if k in declared})
    return Prompt(
        name=name,
        text=text.strip(),
        source="langfuse",
        version=remote_prompt.version,
        langfuse_prompt=remote_prompt,
    )


async def arender(name: str, /, **variables: Any) -> Prompt:
    """``render`` 의 비동기 버전. 그래프 노드 등 비동기 코드에서는 이것을 쓴다.

    Langfuse SDK 의 프롬프트 조회는 동기 HTTP 호출이라, 캐시가 비어 있을 때(기동 직후,
    Langfuse 장애 후 재시도) 이벤트 루프에서 그대로 부르면 다른 요청과 스트리밍까지 함께
    멈춘다. 그래서 별도 스레드에서 실행한다.

    Args:
        name: 프롬프트 이름 (예: ``"chat/system"``).
        **variables: 템플릿 변수. frontmatter 의 ``variables`` 와 정확히 일치해야 한다.

    Returns:
        렌더링된 프롬프트 (``render`` 와 같음).

    Raises:
        PromptError: 이름이 잘못됐거나 변수가 빠지거나 남을 때.
    """
    return await asyncio.to_thread(render, name, **variables)
