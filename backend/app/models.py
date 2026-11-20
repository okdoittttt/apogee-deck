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
        model: 응답한 모델 ID (``"<provider>:<모델 이름>"``). 사용자 메시지는 ``None``.
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
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
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


class Document(Base):
    """사용자가 첨부한 문서. 원본은 첨부 저장소(MinIO)에, 파싱한 텍스트는 이 테이블에 둔다.

    문서는 올리는 순간에는 어느 대화에도 속하지 않는다. 메시지와 함께 보낼 때 대화와 메시지에
    연결되고, 그 대화 안에서만 읽을 수 있다. 보내지 않은 문서는 정리 작업이 지운다.

    Attributes:
        id: 문서 ID.
        conversation_id: 속한 대화 ID. 메시지로 보내기 전에는 ``None``.
        message_id: 문서를 첨부한 사용자 메시지 ID. 보내기 전에는 ``None``.
        filename: 사용자가 올린 파일 이름 (경로 부분은 뺀다).
        kind: 내용으로 확인한 형식 (``app.documents.formats.DocumentKind``).
        mime_type: 원본을 내려줄 때 쓰는 MIME 타입.
        size_bytes: 원본 크기.
        sha256: 원본의 SHA-256 (16진수).
        storage_key: 첨부 저장소의 객체 키.
        status: ``processing`` (파싱 중) | ``ready`` (준비됨) | ``failed`` (실패).
        error: 실패 사유 (사용자에게 보여줄 문구).
        page_count: 쪽수 (쪽 구분이 없는 형식은 ``None``).
        content: 파싱한 본문 (Markdown). 준비되기 전에는 ``None``.
        created_at: 올린 시각.
        parsed_at: 파싱이 끝난 시각 (성공·실패 모두).
    """

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True, nullable=True
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), index=True, nullable=True
    )
    filename: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(20))
    mime_type: Mapped[str] = mapped_column(String(150))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(20), default="processing", index=True)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
