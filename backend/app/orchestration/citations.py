"""한 턴 안에서 출처에 이어지는 번호를 매긴다.

모델은 답변에 ``[3]`` 처럼 번호로 출처를 단다. 도구마다 1부터 세면 두 번 검색했을 때 번호가 겹치므로,
도구 노드가 끝날 때마다 이번 턴(마지막 사용자 메시지 뒤)에 이미 쓴 번호를 이어받아 새 출처에 번호를 준다.
같은 출처(같은 청크, 같은 URL)가 다시 나오면 앞의 번호를 그대로 쓴다. 번호는 artifact 의 출처마다
``n`` 으로 남아서, 화면의 출처 카드와 답변 속 번호가 같다.
"""

import re
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage

from app.core.citations import SOURCE_MARKER


def source_key(source: dict[str, Any]) -> str | None:
    """같은 출처인지 가릴 키. 프로젝트 문서는 청크, 웹은 URL. 둘 다 없으면 ``None`` (매번 새 번호)."""
    if chunk_id := source.get("chunk_id"):
        return f"chunk:{chunk_id}"
    if url := source.get("url"):
        return f"url:{url}"
    return None


def _turn_numbers(history: list[BaseMessage]) -> tuple[dict[str, int], int]:
    """이번 턴에서 이미 쓴 번호 ``{출처 키: 번호}`` 와 다음 번호."""
    used: dict[str, int] = {}
    last = 0
    for message in reversed(history):
        if isinstance(message, HumanMessage):
            break
        if not isinstance(message, ToolMessage) or not isinstance(message.artifact, dict):
            continue
        for source in message.artifact.get("sources") or []:
            n = source.get("n") if isinstance(source, dict) else None
            if not isinstance(n, int):
                continue
            last = max(last, n)
            if key := source_key(source):
                used.setdefault(key, n)
    return used, last + 1


def _replace_markers(text: str, numbers: dict[int, int]) -> str:
    """자리 표시를 ``[번호]`` 로 바꾼다. 없는 출처를 가리키면 지운다."""

    def replace(match: re.Match[str]) -> str:
        index = int(match.group(1))
        return f"[{numbers[index]}]" if index in numbers else ""

    return SOURCE_MARKER.sub(replace, text)


def number_sources(history: list[BaseMessage], new: list[BaseMessage]) -> list[BaseMessage]:
    """도구 노드가 만든 결과의 출처에 이번 턴에서 이어지는 번호를 매긴다.

    결과 글의 ``source_marker`` 자리 표시를 ``[번호]`` 로 바꾸고, artifact 의 출처마다 ``n`` 을 넣는다.
    결과는 도구 호출 순서대로 번호를 받는다.

    Args:
        history: 도구 노드 전까지의 대화 (상태의 ``messages``).
        new: 도구 노드가 만든 메시지.

    Returns:
        번호를 매긴 메시지 (출처가 없는 메시지는 그대로). 원본은 바꾸지 않는다.
    """
    used, next_n = _turn_numbers(history)
    out: list[BaseMessage] = []
    for message in new:
        artifact = message.artifact if isinstance(message, ToolMessage) else None
        if not isinstance(artifact, dict) or not artifact.get("sources"):
            out.append(message)
            continue
        sources = [dict(s) for s in artifact["sources"] if isinstance(s, dict)]
        local: dict[int, int] = {}
        for index, source in enumerate(sources, 1):
            key = source_key(source)
            n = used.get(key) if key else None
            if n is None:
                n, next_n = next_n, next_n + 1
                if key:
                    used[key] = n
            source["n"] = n
            local[index] = n
        content = message.content
        if isinstance(content, str):
            content = _replace_markers(content, local)
        out.append(
            message.model_copy(
                update={"content": content, "artifact": {**artifact, "sources": sources}}
            )
        )
    return out
