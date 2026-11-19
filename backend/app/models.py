"""UI 표시용 ORM 모델.

LLM 에 들어가는 대화 이력은 LangGraph checkpointer 가 관리한다. 이 테이블들은
화면에 대화 목록과 메시지를 보여주는 용도다. 스키마를 바꾸면 ``make db-revision`` 으로
마이그레이션을 만든다.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, LargeBinary, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Conversation(Base):
    """대화 하나. ``id`` 가 LangGraph checkpointer 의 ``thread_id`` 로도 쓰인다.

    Attributes:
        id: 대화 ID.
        title: 사이드바에 보이는 제목 (첫 메시지 앞부분).
        created_at: 생성 시각.
        messages: 대화의 메시지 (생성 순).
    """

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )


class Message(Base):
    """화면에 보여줄 메시지 하나.

    Attributes:
        id: 메시지 ID.
        conversation_id: 속한 대화 ID.
        role: ``"user"`` 또는 ``"assistant"``.
        content: 메시지 텍스트. assistant 는 화면에 스트리밍된 텍스트 그대로 저장한다.
        steps: assistant 응답의 실행 단계(추론 과정, 도구 호출)를 일어난 순서대로 담은 JSON.
            형식은 ``app.schemas.ReasoningStep`` / ``ToolStep``. 사용자 메시지와 실행 단계가
            없는 응답은 ``None``.
        created_at: 생성 시각.
        conversation: 속한 대화.
        images: 사용자가 첨부한 이미지 (첨부 순).
    """

    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    steps: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    conversation: Mapped[Conversation] = relationship(back_populates="messages")
    images: Mapped[list["MessageImage"]] = relationship(
        back_populates="message",
        cascade="all, delete-orphan",
        order_by="MessageImage.position",
    )


class MessageImage(Base):
    """사용자가 첨부한 이미지 원본 (UI 표시용).

    Attributes:
        id: 이미지 ID. ``GET /api/images/{id}`` 로 내려준다.
        message_id: 속한 메시지 ID.
        position: 메시지 안에서의 순서.
        mime_type: 이미지 MIME 타입.
        data: 이미지 바이너리.
        message: 속한 메시지.
    """

    __tablename__ = "message_images"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0)
    mime_type: Mapped[str] = mapped_column(String(50))
    data: Mapped[bytes] = mapped_column(LargeBinary)

    message: Mapped[Message] = relationship(back_populates="images")
