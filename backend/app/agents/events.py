"""에이전트 실행 중에 화면으로 보내는 이벤트와 실행 단계 기록.

응답 하나는 "실행 단계" (추론 과정, 도구 호출) 와 "답변 텍스트" 로 이루어진다.
실행 단계는 일어난 순서대로 ``StepRecorder`` 에 모아서, 답변과 함께 저장해 새로고침 후에도 보인다.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from langchain_core.messages import BaseMessage

MAX_TOOL_OUTPUT_CHARS = 2000
"""저장·표시할 도구 결과의 최대 길이. 넘치면 잘라서 표시한다."""


@dataclass(frozen=True)
class ToolLabel:
    """도구 실행 상태를 화면에 보여줄 문구.

    Attributes:
        running: 실행 중 문구 (예: ``"시간을 확인하는 중"``).
        done: 완료 문구 (예: ``"시간 확인함"``).
    """

    running: str
    done: str


def default_label(tool_name: str) -> ToolLabel:
    """문구가 정의되지 않은 도구의 기본 문구.

    Args:
        tool_name: 도구 이름.

    Returns:
        ``"도구 실행 중: <이름>"`` / ``"도구 실행함: <이름>"``.
    """
    return ToolLabel(running=f"도구 실행 중: {tool_name}", done=f"도구 실행함: {tool_name}")


# --- 이벤트 (runtime.stream_reply 가 순서대로 내보낸다) ---------------------------------------------


@dataclass
class TextDelta:
    """스트리밍 중인 답변 텍스트 조각.

    Attributes:
        text: 화면에 이어 붙일 텍스트.
    """

    text: str


@dataclass
class ReasoningDelta:
    """스트리밍 중인 추론 과정 조각.

    Claude 는 추론 요약을, qwen 같은 thinking 모델은 추론 원문을 보낸다.

    Attributes:
        text: 이어 붙일 추론 텍스트.
    """

    text: str


@dataclass
class ToolStarted:
    """모델이 도구 호출을 결정함.

    Attributes:
        id: 도구 호출 ID (``ToolFinished`` 와 짝을 맞춘다).
        name: 도구 이름.
        args: 모델이 채운 인자.
        label: 화면 표시 문구.
    """

    id: str
    name: str
    args: dict[str, Any]
    label: ToolLabel


@dataclass
class ToolFinished:
    """도구 실행이 끝남.

    Attributes:
        id: 도구 호출 ID.
        output: 도구 결과 (``MAX_TOOL_OUTPUT_CHARS`` 까지).
        status: ``"done"`` 또는 ``"error"``.
        sources: 화면에 출처로 보여줄 페이지 (``title``, ``url``, ``published_date``).
            웹 검색처럼 출처가 있는 도구만 채운다.
    """

    id: str
    output: str
    status: Literal["done", "error"]
    sources: list[dict[str, str | None]] = field(default_factory=list)


@dataclass
class Finished:
    """응답 스트리밍 종료.

    Attributes:
        text: 화면에 보여준 전체 답변 텍스트.
        stop_reason: 공통 이름으로 맞춘 종료 사유 (``end_turn`` / ``max_tokens`` / ``refusal`` 등).
        model: 응답한 모델 이름.
    """

    text: str
    stop_reason: str | None
    model: str


AgentEvent = TextDelta | ReasoningDelta | ToolStarted | ToolFinished | Finished


# --- 메시지에서 정보 꺼내기 -------------------------------------------------------------------------


def reasoning_text(message: BaseMessage) -> str:
    """메시지(또는 스트리밍 조각)에서 추론 텍스트를 꺼낸다.

    provider 마다 형식이 다르다.

    - Anthropic, Google: content 안의 ``{"type": "thinking", "thinking": ...}`` 블록
    - Ollama 등: ``additional_kwargs["reasoning_content"]``
    - LangChain 표준 블록: ``{"type": "reasoning", "reasoning": ...}``

    Args:
        message: AI 메시지 또는 스트리밍 조각.

    Returns:
        추론 텍스트. 없으면 빈 문자열.
    """
    parts: list[str] = []
    if isinstance(message.content, list):
        for block in message.content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "thinking":
                parts.append(block.get("thinking") or "")
            elif block.get("type") == "reasoning":
                parts.append(block.get("reasoning") or "")
    if reasoning := message.additional_kwargs.get("reasoning_content"):
        parts.append(str(reasoning))
    return "".join(parts)


def tool_sources(artifact: Any) -> list[dict[str, str | None]]:
    """도구 결과의 artifact 에서 화면에 보여줄 출처 목록을 꺼낸다.

    출처가 있는 도구는 ``response_format="content_and_artifact"`` 로 선언하고
    ``{"sources": [{"title": ..., "url": ..., "published_date": ...}, ...]}`` 를 artifact 로 돌려준다.
    artifact 는 모델 입력에 들어가지 않는다.

    Args:
        artifact: ``ToolMessage.artifact``.

    Returns:
        ``http``/``https`` URL 을 가진 출처 목록. 없으면 빈 목록.
    """
    if not isinstance(artifact, dict):
        return []
    sources = []
    for item in artifact.get("sources") or []:
        url = str(item.get("url") or "")
        if not url.startswith(("http://", "https://")):
            continue
        sources.append(
            {
                "title": str(item.get("title") or url),
                "url": url,
                "published_date": item.get("published_date"),
            }
        )
    return sources


def tool_output_text(content: Any) -> str:
    """도구 결과를 화면에 보여줄 문자열로 바꾸고 길이를 제한한다.

    Args:
        content: ``ToolMessage.content`` (문자열 또는 content 블록 목록).

    Returns:
        ``MAX_TOOL_OUTPUT_CHARS`` 이하의 문자열.
    """
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in content
        ) or json.dumps(content, ensure_ascii=False, default=str)
    else:
        text = json.dumps(content, ensure_ascii=False, default=str)
    if len(text) > MAX_TOOL_OUTPUT_CHARS:
        return text[:MAX_TOOL_OUTPUT_CHARS] + f"\n… ({len(text) - MAX_TOOL_OUTPUT_CHARS}자 생략)"
    return text


# --- 실행 단계 기록 -------------------------------------------------------------------------------


@dataclass
class StepRecorder:
    """응답 하나의 실행 단계(추론 과정, 도구 호출)를 일어난 순서대로 모은다.

    연속된 추론 조각은 하나의 추론 단계로 합친다. 저장 형식은 JSON 으로 바꿀 수 있는
    딕셔너리 목록이며, ``app.schemas.ReasoningStep`` / ``ToolStep`` 과 같은 모양이다.

    Attributes:
        steps: 실행 단계 목록.
    """

    steps: list[dict[str, Any]] = field(default_factory=list)

    def apply(self, event: AgentEvent) -> None:
        """이벤트를 실행 단계에 반영한다. 답변 텍스트와 종료 이벤트는 무시한다.

        Args:
            event: ``stream_reply`` 가 내보낸 이벤트.
        """
        if isinstance(event, ReasoningDelta):
            if self.steps and self.steps[-1]["type"] == "reasoning":
                self.steps[-1]["text"] += event.text
            else:
                self.steps.append({"type": "reasoning", "text": event.text})
        elif isinstance(event, ToolStarted):
            self.steps.append(
                {
                    "type": "tool",
                    "id": event.id,
                    "name": event.name,
                    "label": event.label.running,
                    "done_label": event.label.done,
                    "args": event.args,
                    "output": None,
                    "status": "running",
                }
            )
        elif isinstance(event, ToolFinished):
            for step in self.steps:
                if step["type"] == "tool" and step["id"] == event.id:
                    step["output"] = event.output
                    step["status"] = event.status
                    if event.sources:
                        step["sources"] = event.sources

    def finalize(self, outcome: Literal["finished", "error", "interrupted"]) -> None:
        """응답이 끝났을 때 아직 실행 중으로 남은 도구의 상태를 정리한다.

        Args:
            outcome: 응답이 끝난 방식. 오류면 ``error``, 그 밖에는 ``interrupted`` 로 바꾼다.
        """
        for step in self.steps:
            if step["type"] == "tool" and step["status"] == "running":
                step["status"] = "error" if outcome == "error" else "interrupted"

    def to_json(self) -> list[dict[str, Any]] | None:
        """저장할 실행 단계. 없으면 ``None``.

        Returns:
            실행 단계 목록 또는 ``None``.
        """
        return self.steps or None
