"""프롬프트 진입점. 에이전트 코드는 render(이름, **변수) 만 사용한다.

PROMPT_SOURCE=langfuse 이면 Langfuse 에서 라벨(PROMPT_LABEL)이 붙은 버전을 쓰고,
가져올 수 없으면 로컬 파일(git 원본)로 렌더링한다. PROMPT_SOURCE=file 이면 항상 로컬 파일.
"""

from typing import Any

from app.config import get_settings
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
    "compile_for_langfuse",
    "list_prompts",
    "render",
    "render_file",
    "validate_all",
]


def render(name: str, /, **variables: Any) -> Prompt:
    # 변수 검증은 항상 로컬 템플릿 기준 (Langfuse 를 쓰더라도 호출 계약은 동일)
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
