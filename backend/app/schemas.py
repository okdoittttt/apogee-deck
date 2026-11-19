import base64
import binascii
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_IMAGES_PER_MESSAGE = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 디코딩 후 기준 (Anthropic API 의 이미지당 한도와 동일)

ImageMimeType = Literal["image/jpeg", "image/png", "image/webp", "image/gif"]


class ImageIn(BaseModel):
    mime_type: ImageMimeType
    data: str = Field(description="base64 (data URL prefix 없이)")

    @field_validator("data")
    @classmethod
    def _check_base64(cls, v: str) -> str:
        try:
            raw = base64.b64decode(v, validate=True)
        except (binascii.Error, ValueError) as e:
            raise ValueError("invalid base64 image data") from e
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError(f"image too large (max {MAX_IMAGE_BYTES // 1024 // 1024}MB)")
        return v

    def to_bytes(self) -> bytes:
        return base64.b64decode(self.data)


class ChatRequest(BaseModel):
    conversation_id: uuid.UUID | None = None
    message: str = Field(default="", max_length=100_000)
    images: list[ImageIn] = Field(default_factory=list, max_length=MAX_IMAGES_PER_MESSAGE)

    @model_validator(mode="after")
    def _require_content(self) -> "ChatRequest":
        if not self.message.strip() and not self.images:
            raise ValueError("message or images is required")
        return self


class MessageOut(BaseModel):
    id: uuid.UUID
    role: str
    content: str
    created_at: datetime
    image_ids: list[uuid.UUID] = []


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    created_at: datetime


class ConversationDetail(ConversationOut):
    messages: list[MessageOut]
