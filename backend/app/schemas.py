"""API 요청/응답 스키마."""

import base64
import binascii
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_IMAGES_PER_MESSAGE = 4
MAX_DOCUMENTS_PER_MESSAGE_LIMIT = 20
"""요청 형식 검사용 상한. 실제 개수 제한은 설정(``MAX_DOCUMENTS_PER_MESSAGE``)으로 검사한다."""
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 디코딩 후 기준 (Anthropic API 의 이미지당 한도와 동일)

ImageMimeType = Literal["image/jpeg", "image/png", "image/webp", "image/gif"]


class ImageIn(BaseModel):
    """요청에 첨부된 이미지 하나.

    Attributes:
        mime_type: 이미지 MIME 타입.
        data: base64 로 인코딩한 이미지 (data URL 접두어 없이).
    """

    mime_type: ImageMimeType
    data: str = Field(description="base64 (data URL prefix 없이)")

    @field_validator("data")
    @classmethod
    def _check_base64(cls, v: str) -> str:
        """base64 형식과 디코딩 후 크기를 검사한다."""
        try:
            raw = base64.b64decode(v, validate=True)
        except (binascii.Error, ValueError) as e:
            raise ValueError("invalid base64 image data") from e
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError(f"image too large (max {MAX_IMAGE_BYTES // 1024 // 1024}MB)")
        return v

    def to_bytes(self) -> bytes:
        """base64 를 디코딩한 이미지 바이너리를 반환한다.

        Returns:
            이미지 원본 바이트.
        """
        return base64.b64decode(self.data)


class ChatRequest(BaseModel):
    """``POST /api/chat`` 요청. 텍스트, 이미지, 문서 중 하나는 있어야 한다.

    Attributes:
        conversation_id: 이어갈 대화 ID. 없으면 새 대화를 만든다.
        message: 사용자 메시지 텍스트.
        images: 첨부 이미지 (최대 ``MAX_IMAGES_PER_MESSAGE`` 장).
        document_ids: 첨부 문서 ID (``POST /api/documents`` 로 올리고 파싱이 끝난 문서).
        model: 응답할 모델 ID (``GET /api/models`` 의 ``id``). 없으면 기본 모델.
    """

    conversation_id: uuid.UUID | None = None
    model: str | None = Field(default=None, max_length=200)
    message: str = Field(default="", max_length=100_000)
    images: list[ImageIn] = Field(default_factory=list, max_length=MAX_IMAGES_PER_MESSAGE)
    document_ids: list[uuid.UUID] = Field(
        default_factory=list, max_length=MAX_DOCUMENTS_PER_MESSAGE_LIMIT
    )

    @model_validator(mode="after")
    def _require_content(self) -> "ChatRequest":
        """텍스트, 이미지, 문서가 모두 비어 있으면 거부하고, 중복 문서 ID 는 하나로 합친다."""
        if not self.message.strip() and not self.images and not self.document_ids:
            raise ValueError("message, images or document_ids is required")
        self.document_ids = list(dict.fromkeys(self.document_ids))
        return self


class ReasoningStep(BaseModel):
    """실행 단계: 모델의 추론 과정.

    Attributes:
        type: 항상 ``"reasoning"``.
        text: 추론 텍스트 (Claude 는 요약, qwen 등은 원문).
    """

    type: Literal["reasoning"] = "reasoning"
    text: str


class Source(BaseModel):
    """도구가 참고한 페이지 (화면의 출처 목록에 보여준다).

    Attributes:
        title: 페이지 제목.
        url: 페이지 주소.
        published_date: 발행일 (``YYYY-MM-DD``). 모르면 ``None``.
    """

    title: str
    url: str
    published_date: str | None = None


class ToolStep(BaseModel):
    """실행 단계: 도구 호출 하나.

    Attributes:
        type: 항상 ``"tool"``.
        id: 도구 호출 ID.
        name: 도구 이름.
        label: 실행 중 표시 문구 (예: ``"시간을 확인하는 중"``).
        done_label: 완료 표시 문구 (예: ``"시간 확인함"``).
        args: 모델이 채운 인자.
        output: 도구 결과 (길이 제한됨). 끝나지 않았으면 ``None``.
        status: ``running`` / ``done`` / ``error`` / ``interrupted``.
        sources: 도구가 참고한 페이지 (웹 검색 결과 등). 없으면 빈 목록.
    """

    type: Literal["tool"] = "tool"
    id: str
    name: str
    label: str
    done_label: str
    args: dict[str, Any]
    output: str | None = None
    status: Literal["running", "done", "error", "interrupted"]
    sources: list[Source] = []


Step = Annotated[ReasoningStep | ToolStep, Field(discriminator="type")]


class ModelOut(BaseModel):
    """화면에서 고를 수 있는 모델.

    Attributes:
        id: 모델 ID (``"<provider>:<모델 이름>"``). 채팅 요청의 ``model`` 로 보낸다.
        provider: provider 이름.
        provider_label: 화면 표시용 provider 이름.
        model: provider 안에서의 모델 이름.
        label: 화면 표시 이름.
    """

    id: str
    provider: str
    provider_label: str
    model: str
    label: str


class ModelList(BaseModel):
    """``GET /api/models`` 응답.

    Attributes:
        models: 지금 쓸 수 있는 모델.
        default: 처음 선택될 모델 ID. 쓸 수 있는 모델이 없으면 ``None``.
    """

    models: list[ModelOut]
    default: str | None


class MessageOut(BaseModel):
    """화면에 보여줄 메시지.

    Attributes:
        id: 메시지 ID.
        role: ``"user"`` 또는 ``"assistant"``.
        content: 메시지 텍스트.
        created_at: 생성 시각.
        image_ids: 첨부 이미지 ID. 이미지는 ``GET /api/images/{id}`` 로 따로 받는다.
        steps: assistant 응답의 실행 단계 (일어난 순서).
        model: 응답한 모델 ID. 사용자 메시지와 모델을 기록하기 전의 응답은 ``None``.
        documents: 사용자 메시지에 첨부한 문서 (본문 없이 정보만).
    """

    id: uuid.UUID
    role: str
    content: str
    created_at: datetime
    image_ids: list[uuid.UUID] = []
    steps: list[Step] = []
    model: str | None = None
    documents: list["DocumentOut"] = []


class ConversationOut(BaseModel):
    """대화 목록의 항목.

    Attributes:
        id: 대화 ID.
        title: 대화 제목.
        created_at: 생성 시각.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    created_at: datetime


class ConversationDetail(ConversationOut):
    """메시지를 포함한 대화.

    Attributes:
        messages: 대화의 메시지 (생성 순).
        running: 이 대화의 응답을 생성 중인지 (다른 창에서 진행 중일 수 있다).
    """

    messages: list[MessageOut]
    running: bool = False


class ConversationStatus(BaseModel):
    """대화의 실행 상태 (다른 창에서 생성 중인 응답이 끝났는지 확인하는 용도).

    Attributes:
        running: 응답을 생성 중인지.
    """

    running: bool


DocumentStatus = Literal["processing", "ready", "failed"]


class DocumentOut(BaseModel):
    """첨부 문서의 정보와 파싱 상태. 본문은 담지 않는다.

    Attributes:
        id: 문서 ID.
        filename: 파일 이름.
        kind: 내용으로 확인한 형식 (``pdf``, ``docx``, ``hwpx`` 등).
        mime_type: 원본 MIME 타입.
        size_bytes: 원본 크기.
        status: ``processing`` (파싱 중) | ``ready`` (준비됨) | ``failed`` (실패).
        error: 실패 사유.
        page_count: 쪽 수 (쪽 구분이 없는 형식은 ``None``).
        char_count: 파싱한 본문의 글자 수 (준비되기 전에는 ``None``).
        created_at: 올린 시각.
        parsed_at: 파싱이 끝난 시각.
    """

    id: uuid.UUID
    filename: str
    kind: str
    mime_type: str
    size_bytes: int
    status: DocumentStatus
    error: str | None = None
    page_count: int | None = None
    char_count: int | None = None
    created_at: datetime
    parsed_at: datetime | None = None


MessageOut.model_rebuild()
ConversationDetail.model_rebuild()
