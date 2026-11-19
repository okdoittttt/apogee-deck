"""LLM 없이 파이프라인(SSE → LangGraph → checkpointer → Langfuse)을 검증하기 위한 echo 모델."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import Field

FAKE_TOOL = "get_current_time"


@dataclass
class _Plan:
    """한 번의 모델 호출에서 낼 응답."""

    reasoning: str
    text: str
    tool_call: dict[str, Any] | None


class EchoChatModel(BaseChatModel):
    """마지막 사용자 메시지를 그대로 돌려주는 테스트용 chat model.

    응답에 "모델이 본 사용자 턴 수" 와 "마지막 메시지의 이미지 수" 를 넣어서,
    checkpointer 의 대화 기억, 대화 잘라내기, 이미지 전달을 LLM 없이 확인할 수 있다.
    예: ``[echo · turn 2] 안녕 (+1 image)``.

    마지막 사용자 메시지에 아래 표시 문구가 있으면 해당 동작을 흉내 낸다 (테스트용).

    - ``FAIL_MARKER``: 답변 도중 오류를 낸다.
    - ``THINK_MARKER``: 답변 전에 추론 과정을 보낸다 (Ollama 와 같은 ``reasoning_content`` 형식).
    - ``TOOL_MARKER``: 도구가 바인딩되어 있으면 ``get_current_time`` 을 호출하고, 도구 결과를
      받은 다음 호출에서 결과를 담아 답한다.

    Attributes:
        stream_delay_seconds: 스트리밍 시 조각 하나마다 기다리는 시간. "응답 생성 중" 상황을
            재현할 때 쓴다.
        tool_names: ``bind_tools`` 로 바인딩된 도구 이름.
    """

    FAIL_MARKER: ClassVar[str] = "[[fail]]"
    THINK_MARKER: ClassVar[str] = "[[think]]"
    TOOL_MARKER: ClassVar[str] = "[[tool]]"
    stream_delay_seconds: float = 0.0
    tool_names: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "echo"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "EchoChatModel":
        """도구 이름을 기억한 복사본을 돌려준다.

        Args:
            tools: 바인딩할 도구.
            **kwargs: 추가 인자 (무시됨).

        Returns:
            ``tool_names`` 가 채워진 모델 복사본.
        """
        names = [getattr(t, "name", None) or t.__name__ for t in tools]
        return self.model_copy(update={"tool_names": names})

    def _plan(self, messages: list[BaseMessage]) -> _Plan:
        """입력 메시지로 이번 호출의 추론, 답변, 도구 호출을 정한다."""
        human = [m for m in messages if isinstance(m, HumanMessage)]
        if not human:
            return _Plan("", "[echo · turn 0]", None)
        last_human = human[-1]
        turn = f"[echo · turn {len(human)}]"
        thinking = self.THINK_MARKER in last_human.text
        reasoning = "질문을 읽고 어떻게 답할지 생각하는 중입니다. 필요한 정보를 정리합니다."

        if messages and isinstance(messages[-1], ToolMessage):
            result = messages[-1].text
            return _Plan(
                "도구 결과를 확인했습니다." if thinking else "", f"{turn} 도구 결과: {result}", None
            )
        if self.TOOL_MARKER in last_human.text and FAKE_TOOL in self.tool_names:
            call = {
                "id": f"call_{uuid.uuid4().hex[:12]}",
                "name": FAKE_TOOL,
                "args": {"timezone": "Asia/Seoul"},
            }
            return _Plan(reasoning if thinking else "", "시간을 확인해 볼게요.", call)

        n_images = (
            sum(1 for b in last_human.content if isinstance(b, dict) and b.get("type") == "image")
            if isinstance(last_human.content, list)
            else 0
        )
        suffix = f" (+{n_images} image)" if n_images else ""
        return _Plan(reasoning if thinking else "", f"{turn} {last_human.text}{suffix}", None)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        plan = self._plan(messages)
        msg = AIMessage(
            plan.text,
            tool_calls=[plan.tool_call] if plan.tool_call else [],
            additional_kwargs={"reasoning_content": plan.reasoning} if plan.reasoning else {},
            response_metadata={"stop_reason": "tool_use" if plan.tool_call else "end_turn"},
        )
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        plan = self._plan(messages)
        for word in plan.reasoning.split(" ") if plan.reasoning else []:
            yield ChatGenerationChunk(
                message=AIMessageChunk("", additional_kwargs={"reasoning_content": word + " "})
            )
        for i, word in enumerate(plan.text.split(" ")):
            if i == 2 and self.FAIL_MARKER in plan.text:
                raise RuntimeError("fake model failure (FAIL_MARKER)")
            yield ChatGenerationChunk(message=AIMessageChunk(word + " "))
        if plan.tool_call:
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    "",
                    tool_call_chunks=[
                        {
                            "id": plan.tool_call["id"],
                            "name": plan.tool_call["name"],
                            "args": json.dumps(plan.tool_call["args"]),
                            "index": 0,
                        }
                    ],
                )
            )
        stop_reason = "tool_use" if plan.tool_call else "end_turn"
        yield ChatGenerationChunk(
            message=AIMessageChunk("", response_metadata={"stop_reason": stop_reason})
        )

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        for chunk in self._stream(messages, stop, **kwargs):
            if self.stream_delay_seconds:
                await asyncio.sleep(self.stream_delay_seconds)
            if run_manager:
                await run_manager.on_llm_new_token(chunk.text, chunk=chunk)
            yield chunk
