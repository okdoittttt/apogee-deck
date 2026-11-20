"""UI 표시용 ORM 모델.

LLM 에 들어가는 대화 이력은 LangGraph checkpointer 가 관리한다. 이 테이블들은
화면에 대화 목록과 메시지를 보여주는 용도다. 스키마를 바꾸면 ``make db-revision`` 으로
마이그레이션을 만든다.
"""

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base

EMBEDDING_DIMENSIONS = 1536
"""청크 임베딩 차원 (OpenAI ``text-embedding-3-small``). 바꾸려면 마이그레이션으로 열을 바꾸고 다시 임베딩한다."""


class User(Base):
    """앱 사용자. Google 계정으로 로그인하고, 관리자가 승인해야 쓸 수 있다.

    Attributes:
        id: 사용자 ID.
        google_sub: Google 계정 고유 ID (OpenID Connect ``sub``). 이메일은 바뀔 수 있어서 이 값으로 찾는다.
        email: Google 계정 이메일 (Google 이 확인한 주소).
        name: 표시 이름.
        picture: 프로필 사진 주소.
        status: ``pending`` (승인 대기) | ``approved`` | ``rejected`` | ``suspended``.
        request_reason: 가입 신청 사유.
        requested_at: 마지막으로 가입을 신청한 시각 (관리자에게 알림 메일을 보낸 시각).
        created_at: 처음 로그인한 시각.
        approved_at: 승인된 시각.
        last_login_at: 마지막 로그인 시각.
        is_local: 인증을 끈 개발 환경에서 쓰는 로컬 사용자인지.
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    google_sub: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    picture: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    request_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_local: Mapped[bool] = mapped_column(Boolean, default=False)


class UserSession(Base):
    """로그인 세션. 쿠키에는 무작위 토큰을, DB 에는 그 해시만 둔다 (DB 가 새도 세션을 쓸 수 없다).

    Attributes:
        token_hash: 세션 토큰의 SHA-256 (16진수).
        user_id: 사용자 ID.
        created_at: 로그인 시각.
        expires_at: 만료 시각.
        last_seen_at: 마지막으로 쓴 시각.
    """

    __tablename__ = "sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ApprovalToken(Base):
    """관리자 메일의 승인·거절 링크 토큰. DB 에는 해시만 두고, 한 번 쓰면 같은 신청의 토큰이 모두 무효가 된다.

    Attributes:
        token_hash: 토큰의 SHA-256 (16진수).
        user_id: 신청한 사용자 ID.
        action: ``approve`` | ``reject``.
        expires_at: 만료 시각 (만든 뒤 48시간).
        used_at: 쓴 시각. 쓰지 않았으면 ``None``.
        created_at: 만든 시각.
    """

    __tablename__ = "approval_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    action: Mapped[str] = mapped_column(String(10))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Project(Base):
    """프로젝트: 문서 묶음과 지시문, 그 안의 대화들.

    프로젝트 문서는 첨부 문서와 달리 보관 기간이 없다 (사용자가 지울 때까지 둔다). 프로젝트를 지우면
    그 안의 대화와 문서가 함께 지워진다.

    Attributes:
        id: 프로젝트 ID.
        user_id: 프로젝트의 주인.
        name: 프로젝트 이름.
        instructions: 이 프로젝트의 대화에서 모델이 따를 지시문 (system 프롬프트에 붙는다).
        created_at: 만든 시각.
        updated_at: 이름이나 지시문을 마지막으로 바꾼 시각.
    """

    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100))
    instructions: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Conversation(Base):
    """대화 하나. ``id`` 가 LangGraph checkpointer 의 ``thread_id`` 로도 쓰인다.

    Attributes:
        id: 대화 ID.
        user_id: 대화의 주인. 로그인 기능 전에 만든 대화는 ``None`` 이고, 관리자가 처음 로그인할 때
            관리자에게 옮긴다.
        project_id: 속한 프로젝트. 프로젝트 밖의 대화는 ``None``.
        title: 사이드바에 보이는 제목 (첫 메시지 앞부분).
        created_at: 생성 시각.
        messages: 대화의 메시지 (생성 순).
    """

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=True
    )
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
            형식은 ``app.api.schemas.ReasoningStep`` / ``ToolStep``. 사용자 메시지와 실행 단계가
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
    """사용자가 첨부한 이미지. 원본은 첨부 저장소(MinIO)에 두고 여기에는 위치만 둔다 (``app.images``).

    Attributes:
        id: 이미지 ID. ``GET /api/images/{id}`` 로 내려주고, checkpoint 의 이미지 참조 블록도 이 ID 를 쓴다.
        message_id: 속한 메시지 ID.
        position: 메시지 안에서의 순서.
        mime_type: 이미지 MIME 타입.
        storage_key: 첨부 저장소의 객체 키 (``images/<ID>``).
        message: 속한 메시지.
    """

    __tablename__ = "message_images"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0)
    mime_type: Mapped[str] = mapped_column(String(50))
    storage_key: Mapped[str] = mapped_column(String(200))

    message: Mapped[Message] = relationship(back_populates="images")


class Document(Base):
    """사용자가 올린 문서. 원본은 첨부 저장소(MinIO)에, 파싱한 텍스트는 이 테이블에 둔다.

    두 종류가 있다.

    - 대화 첨부: 올리는 순간에는 어느 대화에도 속하지 않는다. 메시지와 함께 보낼 때 대화와 메시지에
      연결되고, 그 대화 안에서만 읽을 수 있다. 보내지 않은 문서와 보관 기간이 지난 문서는 정리 작업이 지운다.
    - 프로젝트 문서 (``project_id`` 가 있음): 대화에 붙지 않고, 그 프로젝트의 모든 대화에서 읽을 수 있다.
      보관 기간 없이 사용자가 지울 때까지 둔다.

    Attributes:
        id: 문서 ID.
        user_id: 문서를 올린 사용자. 로그인 기능 전에 올린 문서는 ``None``.
        project_id: 속한 프로젝트 ID. 대화 첨부는 ``None``.
        conversation_id: 속한 대화 ID. 메시지로 보내기 전이나 프로젝트 문서는 ``None``.
        message_id: 문서를 첨부한 사용자 메시지 ID. 보내기 전에는 ``None``.
        filename: 사용자가 올린 파일 이름 (경로 부분은 뺀다).
        kind: 내용으로 확인한 형식 (``app.documents.formats.DocumentKind``).
        mime_type: 원본을 내려줄 때 쓰는 MIME 타입.
        size_bytes: 원본 크기.
        sha256: 원본의 SHA-256 (16진수).
        storage_key: 첨부 저장소의 객체 키.
        status: ``processing`` (파싱 중) | ``analyzing`` (프로젝트 문서의 그림 설명 중) |
            ``indexing`` (프로젝트 문서의 검색 준비 중) | ``ready`` (준비됨) |
            ``failed`` (실패).
        error: 실패 사유 (사용자에게 보여줄 문구).
        page_count: 쪽수 (쪽 구분이 없는 형식은 ``None``).
        content: 파싱한 본문 (Markdown). 준비되기 전에는 ``None``.
        token_count: 본문과 그림 설명의 추정 토큰 수 (프로젝트 용량 계산에 쓴다). 준비되기 전에는 ``None``.
        image_count: 설명할 그림 수 (프로젝트 문서만).
        image_fallback_count: 설명을 만들지 못해 OCR 글자로 대신한 그림 수 (프로젝트 문서만).
        created_at: 올린 시각.
        parsed_at: 파싱이 끝난 시각 (성공·실패 모두).
    """

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=True
    )
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
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    image_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    image_fallback_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DocumentElement(Base):
    """프로젝트 문서의 요소 하나 (본문·표·그림). 검색(RAG)용 청크를 이 요소들로 만든다.

    Attributes:
        id: 요소 ID.
        document_id: 속한 문서 ID.
        position: 문서 안의 순서 (0부터).
        kind: ``text`` (본문) | ``table`` (Markdown 표) | ``image`` (그림).
        page: 쪽(슬라이드·시트) 번호. 쪽 구분이 없으면 ``None``.
        heading: 제목 경로 (예: ``"1장 > 1.1 결과"``). 없으면 ``None``.
        content: 본문, 표 Markdown, 그림 설명 (설명을 만들지 못했으면 그림 속 OCR 글자).
        image_key: 그림 원본의 객체 키 (그림 요소만).
        image_mime: 그림 원본의 MIME 타입 (그림 요소만).
        image_status: 그림 요소의 설명 상태. ``pending`` (설명 전) | ``described`` | ``fallback`` (OCR 글자로 대신).
        token_count: ``content`` 의 추정 토큰 수.
    """

    __tablename__ = "document_elements"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(10))
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    heading: Mapped[str | None] = mapped_column(String(500), nullable=True)
    content: Mapped[str] = mapped_column(Text, default="")
    image_key: Mapped[str | None] = mapped_column(String(300), nullable=True)
    image_mime: Mapped[str | None] = mapped_column(String(50), nullable=True)
    image_status: Mapped[str | None] = mapped_column(String(10), nullable=True)
    token_count: Mapped[int] = mapped_column(Integer, default=0)


class DocumentChunk(Base):
    """프로젝트 문서의 검색 단위(청크). 요소(``DocumentElement``)를 묶거나 나눠서 만든다.

    작은 본문 요소는 같은 제목 아래에서 목표 크기까지 묶고, 큰 요소는 나눈다. 표와 그림은 따로 청크가 된다.
    임베딩은 청크 앞에 문서 이름과 제목 경로를 붙인 글자로 만든다.

    Attributes:
        id: 청크 ID.
        document_id: 속한 문서 ID.
        project_id: 속한 프로젝트 ID (검색할 때 프로젝트로 거른다).
        position: 문서 안의 순서 (0부터).
        kind: ``text`` | ``table`` | ``image``.
        page_start: 첫 쪽 번호. 쪽 구분이 없으면 ``None``.
        page_end: 마지막 쪽 번호.
        heading: 제목 경로. 없으면 ``None``.
        content: 청크 본문 (그림은 설명).
        image_element_id: 그림 청크의 그림 요소 ID (원본 그림을 보여줄 때 쓴다).
        token_count: ``content`` 의 추정 토큰 수.
        embedding: 임베딩. 아직 만들지 못했으면 ``None`` (검색할 때 다시 만든다).
        embedding_model: 임베딩을 만든 모델 (``"openai:text-embedding-3-small"``).
        search_vector: 키워드 검색용 tsvector (``app.documents.keywords`` 토큰, ``simple`` 설정).
            아직 만들지 못했으면 ``None`` (검색할 때 만든다).
    """

    __tablename__ = "document_chunks"
    __table_args__ = (
        # 코사인 거리로 찾는 근사 최근접 이웃 인덱스
        Index(
            "ix_document_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        # 키워드 검색 (전문 검색)
        Index("ix_document_chunks_search_vector", "search_vector", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(10))
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    heading: Mapped[str | None] = mapped_column(String(500), nullable=True)
    content: Mapped[str] = mapped_column(Text)
    image_element_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_elements.id", ondelete="SET NULL"), nullable=True
    )
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIMENSIONS), nullable=True
    )
    embedding_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    search_vector: Mapped[Any | None] = mapped_column(TSVECTOR, nullable=True)
