"""에이전트 실행 중에 화면으로 보내는 이벤트와 실행 단계 기록.

응답 하나는 "실행 단계" (추론 과정, 도구 호출) 와 "답변 텍스트" 로 이루어진다.
실행 단계는 일어난 순서대로 ``StepRecorder`` 에 모아서, 답변과 함께 저장해 새로고침 후에도 보인다.
"""

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from langchain_core.messages import BaseMessage

from app.tools import ToolLabel

MAX_TOOL_OUTPUT_CHARS = 2000
"""저장·표시할 도구 결과의 최대 길이. 넘치면 잘라서 표시한다."""


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
        sources: 화면에 출처로 보여줄 웹 페이지나 프로젝트 문서 (``tool_sources`` 참고).
            웹 검색처럼 출처가 있는 도구만 채운다.
        documents: 도구가 만든 문서 (``tool_documents`` 참고). 화면에 파일 칩으로 보여준다.
            문서를 고치는 도구만 채운다.
    """

    id: str
    output: str
    status: Literal["done", "error"]
    sources: list[dict[str, str | None]] = field(default_factory=list)
    documents: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Finished:
    """응답 스트리밍 종료.

    Attributes:
        text: 화면에 보여준 전체 답변 텍스트.
        stop_reason: 공통 이름으로 맞춘 종료 사유 (``end_turn`` / ``max_tokens`` / ``refusal`` 등).
        model: 응답한 모델 ID (``"<provider>:<모델 이름>"``).
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
    - OpenAI Responses API: ``{"type": "reasoning", "summary": [{"type": "summary_text", ...}]}``.
      요약이 여러 문단이면 새 문단이 시작될 때(두 번째 문단부터) 빈 줄을 넣는다.

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
                for item in block.get("summary") or []:
                    if not isinstance(item, dict) or item.get("type") != "summary_text":
                        continue
                    text = item.get("text") or ""
                    if not text and (item.get("index") or 0) > 0:
                        text = "\n\n"  # 새 요약 문단 시작
                    parts.append(text)
    if reasoning := message.additional_kwargs.get("reasoning_content"):
        parts.append(str(reasoning))
    return "".join(parts)


def tool_sources(artifact: Any) -> list[dict[str, Any]]:
    """도구 결과의 artifact 에서 화면에 보여줄 출처 목록을 꺼낸다.

    출처가 있는 도구는 ``response_format="content_and_artifact"`` 로 선언하고 ``{"sources": [...]}`` 를
    artifact 로 돌려준다. artifact 는 모델 입력에 들어가지 않는다. 출처는 두 종류다.

    - 웹 페이지: ``{"title", "url", "published_date"}``. ``http``/``https`` 주소만 남긴다.
    - 프로젝트 문서 (``"type": "document"``): ``{"title", "document_id", "project_id", "location",
      "image_element_id", "chunk_id"}``. ID 는 UUID 형식만 남긴다.

    두 종류 모두 한 턴 안의 출처 번호 ``n`` 을 함께 남긴다 (``app.orchestration.citations``).

    Args:
        artifact: ``ToolMessage.artifact``.

    Returns:
        출처 목록. 없으면 빈 목록.
    """
    if not isinstance(artifact, dict):
        return []
    sources: list[dict[str, Any]] = []
    for item in artifact.get("sources") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "document":
            if source := _document_source(item):
                sources.append(source)
            continue
        url = str(item.get("url") or "")
        if not url.startswith(("http://", "https://")):
            continue
        sources.append(
            {
                "title": str(item.get("title") or url),
                "url": url,
                "published_date": item.get("published_date"),
                "n": _number_or_none(item.get("n")),
            }
        )
    return sources


def tool_documents(artifact: Any) -> list[dict[str, Any]]:
    """도구 결과의 artifact 에서 도구가 만든 문서(수정본) 목록을 꺼낸다.

    문서를 만드는 도구(``edit_document``)는 ``{"documents": [...]}`` 를 artifact 로 돌려준다.
    화면이 다운로드 주소를 만드는 값이라 문서 ID 는 UUID 형식만 남긴다.

    Args:
        artifact: ``ToolMessage.artifact``.

    Returns:
        ``{"document_id", "filename", "revision", "source_document_id", "kind", "size_bytes"}`` 목록.
        없으면 빈 목록.
    """
    if not isinstance(artifact, dict):
        return []
    documents: list[dict[str, Any]] = []
    for item in artifact.get("documents") or []:
        if not isinstance(item, dict):
            continue
        document_id = _uuid_or_none(item.get("document_id"))
        if document_id is None:
            continue
        documents.append(
            {
                "document_id": document_id,
                "filename": str(item.get("filename") or "문서"),
                "revision": _number_or_none(item.get("revision")),
                "source_document_id": _uuid_or_none(item.get("source_document_id")),
                "kind": str(item.get("kind") or ""),
                "size_bytes": _number_or_none(item.get("size_bytes")),
            }
        )
    return documents


def _number_or_none(value: Any) -> int | None:
    """출처 번호. 양의 정수가 아니면 ``None``."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _uuid_or_none(value: Any) -> str | None:
    try:
        return str(uuid.UUID(str(value))) if value else None
    except ValueError:
        return None


def _document_source(item: dict[str, Any]) -> dict[str, Any] | None:
    """프로젝트 문서 출처를 검사해서 화면에 줄 값만 남긴다. 문서·프로젝트 ID 가 없으면 ``None``."""
    document_id = _uuid_or_none(item.get("document_id"))
    project_id = _uuid_or_none(item.get("project_id"))
    if document_id is None or project_id is None:
        return None
    return {
        "type": "document",
        "title": str(item.get("title") or "문서"),
        "document_id": document_id,
        "project_id": project_id,
        "location": str(item.get("location") or ""),
        "image_element_id": _uuid_or_none(item.get("image_element_id")),
        "chunk_id": _uuid_or_none(item.get("chunk_id")),
        "n": _number_or_none(item.get("n")),
    }


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
    딕셔너리 목록이며, ``app.api.schemas.ReasoningStep`` / ``ToolStep`` 과 같은 모양이다.

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
                    if event.documents:
                        step["documents"] = event.documents

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
