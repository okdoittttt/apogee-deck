"""프로젝트 서비스: 만들기·고치기·조회, 프로젝트 문서 올리기·지우기, 에이전트에게 줄 프로젝트 정보.

HTTP 를 모른다. 사용자에게 보여줄 실패는 ``ProjectError`` (상태 코드 포함)로 던진다. 프로젝트 삭제는
대화 기록(checkpoint)도 지워야 해서 대화 서비스(``app.conversations.service.delete_project``)가 맡는다.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.core.config import get_settings
from app.core.storage import ObjectStorage
from app.documents import service as documents
from app.documents.reading import AttachedDocument
from app.models import Conversation, Document, Project


class ProjectError(Exception):
    """프로젝트 요청을 처리할 수 없다. 메시지는 사용자에게 그대로 보여준다.

    Attributes:
        status_code: 돌려줄 HTTP 상태 코드.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        """오류를 만든다.

        Args:
            status_code: HTTP 상태 코드.
            detail: 사용자에게 보여줄 문구.
        """
        super().__init__(detail)
        self.status_code = status_code


async def get_own(session: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID) -> Project:
    """내 프로젝트를 읽는다. 없거나 다른 사람의 프로젝트면 404 (존재 여부도 숨긴다).

    Args:
        session: DB 세션.
        project_id: 프로젝트 ID.
        user_id: 요청한 사용자 ID.

    Returns:
        프로젝트.

    Raises:
        ProjectError: 프로젝트가 없거나 내 것이 아니면 404.
    """
    project = await session.get(Project, project_id)
    if project is None or project.user_id != user_id:
        raise ProjectError(404, "Project not found")
    return project


async def list_projects(session: AsyncSession, user_id: uuid.UUID) -> list[Project]:
    """내 프로젝트 목록을 최근에 바꾼 순으로 돌려준다.

    Args:
        session: DB 세션.
        user_id: 사용자 ID.

    Returns:
        프로젝트 목록.
    """
    result = await session.scalars(
        select(Project).where(Project.user_id == user_id).order_by(Project.updated_at.desc())
    )
    return list(result.all())


async def create(
    session: AsyncSession, user_id: uuid.UUID, name: str, instructions: str
) -> Project:
    """프로젝트를 만든다.

    Args:
        session: DB 세션. 이 함수가 commit 한다.
        user_id: 주인이 될 사용자 ID.
        name: 이름 (앞뒤 공백을 뺀다).
        instructions: 지시문.

    Returns:
        만든 프로젝트.
    """
    project = Project(user_id=user_id, name=name.strip(), instructions=instructions.strip())
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


async def update(
    session: AsyncSession,
    project: Project,
    *,
    name: str | None = None,
    instructions: str | None = None,
) -> Project:
    """프로젝트 이름이나 지시문을 바꾼다. 지시문은 다음 응답부터 적용된다.

    Args:
        session: DB 세션. 이 함수가 commit 한다.
        project: 바꿀 프로젝트 (``get_own`` 으로 확인한 것).
        name: 새 이름. ``None`` 이면 그대로 둔다.
        instructions: 새 지시문. ``None`` 이면 그대로 둔다.

    Returns:
        바꾼 프로젝트.
    """
    if name is not None:
        project.name = name.strip()
    if instructions is not None:
        project.instructions = instructions.strip()
    await session.commit()
    await session.refresh(project)
    return project


async def list_documents(
    session: AsyncSession, project_id: uuid.UUID
) -> list[tuple[Document, int | None]]:
    """프로젝트 문서와 본문 글자 수를 올린 순으로 돌려준다. 본문은 읽지 않는다.

    Args:
        session: DB 세션.
        project_id: 프로젝트 ID.

    Returns:
        ``(문서, 본문 글자 수)`` 목록.
    """
    rows = await session.execute(
        select(Document, func.char_length(Document.content))
        .options(defer(Document.content))
        .where(Document.project_id == project_id)
        .order_by(Document.created_at)
    )
    return [(doc, chars) for doc, chars in rows.all()]


async def list_conversations(session: AsyncSession, project_id: uuid.UUID) -> list[Conversation]:
    """프로젝트 안의 대화를 최신순으로 돌려준다.

    Args:
        session: DB 세션.
        project_id: 프로젝트 ID.

    Returns:
        대화 목록.
    """
    result = await session.scalars(
        select(Conversation)
        .where(Conversation.project_id == project_id)
        .order_by(Conversation.created_at.desc())
    )
    return list(result.all())


async def usage(session: AsyncSession, project_id: uuid.UUID) -> int:
    """프로젝트가 쓰고 있는 용량 (준비된 문서의 추정 토큰 수 합계).

    Args:
        session: DB 세션.
        project_id: 프로젝트 ID.

    Returns:
        토큰 수 합계.
    """
    return await documents.project_usage(session, project_id)


async def add_document(
    session: AsyncSession,
    storage: ObjectStorage,
    project: Project,
    data: bytes,
    filename: str | None,
) -> Document:
    """프로젝트에 문서를 올린다. 파싱은 호출하는 쪽에서 ``documents.schedule_parse`` 로 시작한다.

    문서의 분량은 파싱해야 알 수 있으므로, 여기서는 이미 꽉 찬 프로젝트만 거절한다. 파싱이 끝났을 때
    용량을 넘으면 그 문서는 실패로 표시된다.

    Args:
        session: DB 세션. 이 함수가 commit 한다.
        storage: 첨부 저장소.
        project: 문서를 넣을 프로젝트 (``get_own`` 으로 확인한 것).
        data: 파일 내용. 크기 제한은 호출하는 쪽에서 먼저 확인한다.
        filename: 사용자가 올린 파일 이름.

    Returns:
        ``processing`` 상태로 기록한 문서.

    Raises:
        ProjectError: 프로젝트 용량이 이미 꽉 찼으면 409.
        UnsupportedDocumentError: 빈 파일, 지원하지 않는 형식, 손상된 파일일 때.
    """
    if await usage(session, project.id) >= get_settings().project_max_tokens:
        raise ProjectError(409, documents.PROJECT_FULL_ERROR)
    return await documents.create_document(
        session, storage, data, filename, project.user_id, project_id=project.id
    )


async def delete_document(session: AsyncSession, project: Project, document_id: uuid.UUID) -> None:
    """프로젝트 문서를 지운다 (DB 기록과 원본).

    Args:
        session: DB 세션. 이 함수가 commit 한다.
        project: 프로젝트 (``get_own`` 으로 확인한 것).
        document_id: 문서 ID.

    Raises:
        ProjectError: 이 프로젝트의 문서가 아니면 404.
    """
    doc = await session.get(Document, document_id, options=[defer(Document.content)])
    if doc is None or doc.project_id != project.id:
        raise ProjectError(404, "Document not found")
    key = doc.storage_key
    await session.delete(doc)
    await session.commit()
    await documents.delete_objects([key])


@dataclass(frozen=True)
class ProjectContext:
    """에이전트에게 줄 프로젝트 정보.

    Attributes:
        id: 프로젝트 ID. ``read_document`` 가 이 프로젝트의 문서를 읽게 할 때 쓴다.
        name: 프로젝트 이름.
        instructions: 지시문.
        documents: 읽을 수 있는(준비된) 프로젝트 문서. 본문은 담지 않는다.
    """

    id: uuid.UUID
    name: str
    instructions: str
    documents: list[AttachedDocument]


async def load_context(session: AsyncSession, project_id: uuid.UUID) -> ProjectContext | None:
    """에이전트에게 줄 프로젝트 정보를 읽는다. 문서는 준비된 것만, 올린 순으로 담는다.

    Args:
        session: DB 세션.
        project_id: 프로젝트 ID.

    Returns:
        프로젝트 정보. 프로젝트가 없으면 ``None``.
    """
    project = await session.get(Project, project_id)
    if project is None:
        return None
    rows = await session.execute(
        select(
            Document.id,
            Document.filename,
            Document.kind,
            Document.page_count,
            func.char_length(Document.content).label("chars"),
        )
        .where(Document.project_id == project_id, Document.status == "ready")
        .order_by(Document.created_at)
    )
    docs = [
        AttachedDocument(
            id=str(r.id),
            filename=r.filename,
            kind=r.kind,
            page_count=r.page_count,
            char_count=r.chars or 0,
        )
        for r in rows.all()
    ]
    return ProjectContext(
        id=project.id, name=project.name, instructions=project.instructions, documents=docs
    )
