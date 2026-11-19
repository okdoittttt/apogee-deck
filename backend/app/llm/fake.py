"""LLM 없이 파이프라인(SSE → LangGraph → checkpointer → Langfuse)을 검증하기 위한 echo 모델."""

from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult


class EchoChatModel(BaseChatModel):
    """마지막 사용자 메시지와 지금까지의 대화 턴 수를 돌려준다 (checkpointer 동작 확인용)."""

    @property
    def _llm_type(self) -> str:
        return "echo"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "EchoChatModel":
        return self  # 도구를 호출하지 않음

    def _reply(self, messages: list[BaseMessage]) -> str:
        human = [m for m in messages if isinstance(m, HumanMessage)]
        if not human:
            return "[echo · turn 0]"
        last = human[-1]
        n_images = (
            sum(1 for b in last.content if isinstance(b, dict) and b.get("type") == "image")
            if isinstance(last.content, list)
            else 0
        )
        suffix = f" (+{n_images} image)" if n_images else ""
        return f"[echo · turn {len(human)}] {last.text}{suffix}"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        msg = AIMessage(self._reply(messages), response_metadata={"stop_reason": "end_turn"})
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        for word in self._reply(messages).split(" "):
            yield ChatGenerationChunk(message=AIMessageChunk(word + " "))
        yield ChatGenerationChunk(
            message=AIMessageChunk("", response_metadata={"stop_reason": "end_turn"})
        )

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        for chunk in self._stream(messages, stop, **kwargs):
            if run_manager:
                await run_manager.on_llm_new_token(chunk.text, chunk=chunk)
            yield chunk
