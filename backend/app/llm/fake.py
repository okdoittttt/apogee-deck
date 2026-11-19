"""LLM 없이 파이프라인(SSE → LangGraph → checkpointer → Langfuse)을 검증하기 위한 echo 모델."""

from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult


class EchoChatModel(BaseChatModel):
    """마지막 사용자 메시지를 그대로 돌려주는 테스트용 chat model.

    응답에 "모델이 본 사용자 턴 수" 와 "마지막 메시지의 이미지 수" 를 넣어서,
    checkpointer 의 대화 기억, 대화 잘라내기, 이미지 전달을 LLM 없이 확인할 수 있다.
    예: ``[echo · turn 2] 안녕 (+1 image)``.
    """

    @property
    def _llm_type(self) -> str:
        return "echo"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "EchoChatModel":
        """도구를 무시하고 자기 자신을 돌려준다 (도구를 호출하지 않는 모델).

        Args:
            tools: 바인딩할 도구 (무시됨).
            **kwargs: 추가 인자 (무시됨).

        Returns:
            이 모델 그대로.
        """
        return self

    def _reply(self, messages: list[BaseMessage]) -> str:
        """입력 메시지로 echo 응답 텍스트를 만든다."""
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
