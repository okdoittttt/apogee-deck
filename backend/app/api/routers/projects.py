"""프로젝트 API: 프로젝트 만들기·조회·고치기·지우기, 프로젝트 문서 올리기·지우기.

프로젝트 안에서 대화를 시작할 때는 ``POST /api/chat`` 에 ``project_id`` 를 넣는다. 프로젝트 문서의
상태 조회와 원본 받기는 첨부 문서와 같은 ``/api/documents/{id}`` 를 쓴다.
"""

import logging
import uuid

from fastapi import APIRouter, HTTPException, Response, UploadFile

from app.api.deps import ApprovedUser, SessionDep
from app.api.schemas import (
    ConversationOut,
    DocumentOut,
    ProjectDetail,
    ProjectIn,
    ProjectOut,
    ProjectUpdate,
    document_out,
    project_out,
)
from app.conversations import service as conversations
from app.conversations.service import ConversationError
from app.core.config import get_settings
from app.core.storage import get_storage
from app.documents import service as documents
from app.documents.formats import UnsupportedDocumentError
from app.models import Project, User
from app.projects import service
from app.projects.service import ProjectError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/projects")

STORAGE_UNAVAILABLE = "첨부 저장소를 쓸 수 없어 문서를 올릴 수 없습니다."


def _http_error(exc: ProjectError | ConversationError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


async def _own(session: SessionDep, project_id: uuid.UUID, user: User) -> Project:
    """내 프로젝트를 읽는다. 다른 사람의 프로젝트는 404."""
    try:
        return await service.get_own(session, project_id, user.id)
    except ProjectError as exc:
        raise _http_error(exc) from exc


@router.get("")
async def list_projects(session: SessionDep, user: ApprovedUser) -> list[ProjectOut]:
    """내 프로젝트 목록을 최근에 바꾼 순으로 돌려준다.

    Args:
        session: DB 세션.
        user: 승인된 사용자.

    Returns:
        프로젝트 목록.
    """
    return [project_out(p) for p in await service.list_projects(session, user.id)]


@router.post("", status_code=201)
async def create_project(req: ProjectIn, session: SessionDep, user: ApprovedUser) -> ProjectOut:
    """프로젝트를 만든다.

    Args:
        req: 이름과 지시문.
        session: DB 세션.
        user: 승인된 사용자. 프로젝트의 주인이 된다.

    Returns:
        만든 프로젝트.
    """
    return project_out(await service.create(session, user.id, req.name, req.instructions))


@router.get("/{project_id}")
async def get_project(
    project_id: uuid.UUID, session: SessionDep, user: ApprovedUser
) -> ProjectDetail:
    """프로젝트와 그 문서, 대화, 용량을 돌려준다.

    Args:
        project_id: 프로젝트 ID.
        session: DB 세션.
        user: 승인된 사용자.

    Returns:
        프로젝트 상세.

    Raises:
        HTTPException: 프로젝트가 없거나 내 것이 아니면 404.
    """
    project = await _own(session, project_id, user)
    docs = await service.list_documents(session, project.id)
    convs = await service.list_conversations(session, project.id)
    return ProjectDetail(
        **project_out(project).model_dump(),
        documents=[document_out(doc, chars) for doc, chars in docs],
        conversations=[ConversationOut.model_validate(c) for c in convs],
        used_tokens=await service.usage(session, project.id),
        max_tokens=get_settings().project_max_tokens,
    )


@router.patch("/{project_id}")
async def update_project(
    project_id: uuid.UUID, req: ProjectUpdate, session: SessionDep, user: ApprovedUser
) -> ProjectOut:
    """프로젝트 이름이나 지시문을 바꾼다.

    Args:
        project_id: 프로젝트 ID.
        req: 바꿀 값.
        session: DB 세션.
        user: 승인된 사용자.

    Returns:
        바꾼 프로젝트.

    Raises:
        HTTPException: 프로젝트가 없거나 내 것이 아니면 404.
    """
    project = await _own(session, project_id, user)
    updated = await service.update(session, project, name=req.name, instructions=req.instructions)
    return project_out(updated)


@router.delete("/{project_id}", status_code=204)
async def delete_project(project_id: uuid.UUID, session: SessionDep, user: ApprovedUser) -> None:
    """프로젝트와 그 안의 대화·문서를 모두 지운다.

    Args:
        project_id: 프로젝트 ID.
        session: DB 세션.
        user: 승인된 사용자.

    Raises:
        HTTPException: 프로젝트가 없거나 내 것이 아니면 404, 응답 생성 중인 대화가 있으면 409.
    """
    try:
        await conversations.delete_project(session, project_id, user.id)
    except (ProjectError, ConversationError) as exc:
        raise _http_error(exc) from exc


@router.post("/{project_id}/documents", status_code=201)
async def upload_project_document(
    project_id: uuid.UUID, file: UploadFile, session: SessionDep, user: ApprovedUser
) -> DocumentOut:
    """프로젝트에 문서를 올린다. 파싱은 백그라운드에서 진행된다 (``GET /api/documents/{id}`` 로 확인).

    Args:
        project_id: 프로젝트 ID.
        file: multipart 로 올린 파일.
        session: DB 세션.
        user: 승인된 사용자.

    Returns:
        ``processing`` 상태의 문서 정보.

    Raises:
        HTTPException: 프로젝트가 없거나 내 것이 아니면 404, 용량이 꽉 찼으면 409, 파일이 너무 크면
            413, 지원하지 않거나 손상된 파일이면 415, 첨부 저장소를 쓸 수 없으면 503.
    """
    project = await _own(session, project_id, user)
    storage = get_storage()
    if storage is None:
        raise HTTPException(status_code=503, detail=STORAGE_UNAVAILABLE)
    limit = get_settings().document_max_bytes
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(
            status_code=413, detail=f"파일이 너무 큽니다 (최대 {limit // 1024 // 1024}MB)."
        )
    try:
        doc = await service.add_document(session, storage, project, data, file.filename)
    except ProjectError as exc:
        raise _http_error(exc) from exc
    except UnsupportedDocumentError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("failed to store project document")
        raise HTTPException(status_code=503, detail=STORAGE_UNAVAILABLE) from exc
    documents.schedule_parse(doc.id, data, doc.kind, doc.filename)
    return document_out(doc, None)


@router.delete("/{project_id}/documents/{document_id}", status_code=204)
async def delete_project_document(
    project_id: uuid.UUID, document_id: uuid.UUID, session: SessionDep, user: ApprovedUser
) -> None:
    """프로젝트 문서를 지운다 (DB 기록과 원본).

    Args:
        project_id: 프로젝트 ID.
        document_id: 문서 ID.
        session: DB 세션.
        user: 승인된 사용자.

    Raises:
        HTTPException: 프로젝트나 문서가 없거나 내 것이 아니면 404.
    """
    project = await _own(session, project_id, user)
    try:
        await service.delete_document(session, project, document_id)
    except ProjectError as exc:
        raise _http_error(exc) from exc


@router.get("/{project_id}/images/{element_id}")
async def get_project_image(
    project_id: uuid.UUID, element_id: uuid.UUID, session: SessionDep, user: ApprovedUser
) -> Response:
    """프로젝트 문서 속 그림 원본을 돌려준다 (검색 결과 출처 카드의 미리보기).

    그림은 저장 후 바뀌지 않으므로 브라우저가 오래 캐시하도록 한다.

    Args:
        project_id: 프로젝트 ID.
        element_id: 그림 요소 ID.
        session: DB 세션.
        user: 승인된 사용자.

    Returns:
        그림 바이너리 응답.

    Raises:
        HTTPException: 프로젝트나 그림이 없거나 내 것이 아니면 404, 첨부 저장소를 쓸 수 없으면 503.
    """
    project = await _own(session, project_id, user)
    storage = get_storage()
    if storage is None:
        raise HTTPException(status_code=503, detail="첨부 저장소를 쓸 수 없습니다.")
    try:
        data, mime_type = await service.read_element_image(session, storage, project, element_id)
    except ProjectError as exc:
        raise _http_error(exc) from exc
    return Response(
        content=data,
        media_type=mime_type,
        headers={
            "Cache-Control": "private, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )
