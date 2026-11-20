"""문서 속 그림을 글로 설명한다 (프로젝트 문서 적재용).

그림은 검색되지 않으므로, OpenAI vision 모델(``VISION_MODEL``, 기본 ``gpt-6-luna``)로 그림의 내용을
한국어 글로 바꿔 둔다. 비용을 줄이려고 낮은 해상도(``detail: "low"``)로 보내고 추론을 짧게 한다.
"""

import base64

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage

from app.core.config import Settings

DESCRIBE_PROMPT = """\
You are describing an image taken from a document so that it can be found by search and \
understood without seeing it.

Write the description in Korean. Describe what the image shows and what it means in the \
document. For charts and diagrams, write out every number, axis name, label, legend item and \
unit you can read, and the key comparison or trend. For tables in the image, write out the \
rows. For photos, describe the subject briefly. If text in the image is unreadable, say so \
instead of guessing. Do not add anything that is not in the image or the context. Answer with \
the description only, in at most about 300 Korean words.

Context from the document (may be empty):
{context}"""


class VisionUnavailableError(Exception):
    """그림 설명을 쓸 수 없다 (OpenAI 키가 없음)."""


def build_vision_model(settings: Settings) -> BaseChatModel:
    """그림 설명용 OpenAI 모델을 만든다.

    Args:
        settings: 앱 설정 (``vision_model``, OpenAI 키, 시간 제한).

    Returns:
        chat model.

    Raises:
        VisionUnavailableError: OpenAI 키가 없을 때.
    """
    if not settings.openai_api_key:
        raise VisionUnavailableError("OPENAI_API_KEY 가 없어 그림을 설명할 수 없습니다.")
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=settings.vision_model,
        api_key=settings.openai_api_key,
        reasoning_effort="low",
        timeout=settings.vision_timeout_seconds,
        max_retries=2,
    )


async def describe_image(model: BaseChatModel, data: bytes, mime_type: str, context: str) -> str:
    """그림 하나를 한국어 글로 설명한다.

    Args:
        model: ``build_vision_model`` 로 만든 모델.
        data: 그림 바이트.
        mime_type: 그림 MIME 타입 (``image/webp`` 등).
        context: 그림 주변 정보 (문서 이름, 제목 경로, 쪽, 그림 속 OCR 글자).

    Returns:
        설명. 모델이 빈 답을 주면 빈 문자열.
    """
    url = f"data:{mime_type};base64,{base64.b64encode(data).decode()}"
    message = HumanMessage(
        content=[
            {"type": "text", "text": DESCRIBE_PROMPT.format(context=context.strip() or "(none)")},
            {"type": "image_url", "image_url": {"url": url, "detail": "low"}},
        ]
    )
    response = await model.ainvoke([message])
    return response.text.strip()
