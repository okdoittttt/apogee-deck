"""첨부 문서 API: 올리기, 상태 조회, 원본 받기, 지우기."""

import logging
import uuid
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy import func, select

from app.api.deps import ApprovedUser, SessionDep
from app.api.schemas import DocumentOut, document_out
from app.core.config import get_settings
from app.core.storage import ObjectNotFoundError, get_storage
from app.documents import service
from app.documents.formats import UnsupportedDocumentError
from app.models import Document, User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/documents")

STORAGE_UNAVAILABLE = "첨부 저장소를 쓸 수 없어 문서를 올릴 수 없습니다."


async def _load(
    session: SessionDep, document_id: uuid.UUID, user: User
) -> tuple[Document, int | None]:
    """내 문서와 본문 글자 수를 읽는다. 본문 자체는 읽지 않는다. 다른 사람의 문서는 404."""
    row = (
        await session.execute(
            select(Document, func.char_length(Document.content)).where(
                Document.id == document_id, Document.user_id == user.id
            )
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return row[0], row[1]


@router.post("", status_code=201)
async def upload_document(file: UploadFile, session: SessionDep, user: ApprovedUser) -> DocumentOut:
    """문서를 올린다. 파싱은 백그라운드에서 진행되고, 상태는 ``GET /api/documents/{id}`` 로 본다.

    올린 문서는 아직 어느 대화에도 속하지 않는다. 메시지와 함께 보낼 때 대화에 연결된다.

    Args:
        file: multipart 로 올린 파일.
        session: DB 세션.
        user: 승인된 사용자. 문서의 주인이 된다.

    Returns:
        ``processing`` 상태의 문서 정보.

    Raises:
        HTTPException: 파일이 너무 크면 413, 지원하지 않거나 손상된 파일이면 415,
            첨부 저장소를 쓸 수 없으면 503.
    """
    settings = get_settings()
    storage = get_storage()
    if storage is None:
        raise HTTPException(status_code=503, detail=STORAGE_UNAVAILABLE)
    limit = settings.document_max_bytes
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(
            status_code=413,
            detail=f"파일이 너무 큽니다 (최대 {limit // 1024 // 1024}MB).",
        )
    try:
        doc = await service.create_document(session, storage, data, file.filename, user.id)
    except UnsupportedDocumentError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("failed to store uploaded document")
        raise HTTPException(status_code=503, detail=STORAGE_UNAVAILABLE) from exc
    service.schedule_parse(doc.id, data, doc.kind, doc.filename)
    return document_out(doc, None)


@router.get("/{document_id}")
async def get_document(
    document_id: uuid.UUID, session: SessionDep, user: ApprovedUser
) -> DocumentOut:
    """문서 정보와 파싱 상태를 돌려준다.

    Args:
        document_id: 문서 ID.
        session: DB 세션.
        user: 승인된 사용자.

    Returns:
        문서 정보.

    Raises:
        HTTPException: 문서가 없거나 내 것이 아니면 404.
    """
    doc, char_count = await _load(session, document_id, user)
    return document_out(doc, char_count)


@router.get("/{document_id}/file")
async def download_document(
    document_id: uuid.UUID, session: SessionDep, user: ApprovedUser
) -> Response:
    """문서 원본을 내려준다.

    Args:
        document_id: 문서 ID.
        session: DB 세션.
        user: 승인된 사용자.

    Returns:
        원본 파일. 다운로드로 받도록 ``Content-Disposition: attachment`` 를 붙인다.

    Raises:
        HTTPException: 문서가 없거나 내 것이 아니면 404, 원본이 보관 기간이 지나 지워졌으면 410,
            첨부 저장소를 쓸 수 없으면 503.
    """
    doc, _ = await _load(session, document_id, user)
    storage = get_storage()
    if storage is None:
        raise HTTPException(status_code=503, detail="첨부 저장소를 쓸 수 없습니다.")
    try:
        data = await storage.get(doc.storage_key)
    except ObjectNotFoundError as exc:
        raise HTTPException(
            status_code=410, detail="보관 기간이 지나 원본이 삭제되었습니다."
        ) from exc
    return Response(
        content=data,
        media_type=doc.mime_type,
        headers={
            # 브라우저가 열지 않고 내려받게 한다 (HTML/SVG 등으로 위장한 파일 실행 방지).
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(doc.filename)}",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete("/{document_id}", status_code=204)
async def delete_document(document_id: uuid.UUID, session: SessionDep, user: ApprovedUser) -> None:
    """아직 보내지 않은 첨부 문서를 지운다 (입력창에서 첨부를 뺄 때).

    Args:
        document_id: 문서 ID.
        session: DB 세션.
        user: 승인된 사용자.

    Raises:
        HTTPException: 문서가 없거나 내 것이 아니면 404, 이미 메시지로 보낸 문서나 프로젝트 문서면 409.
    """
    doc = await session.get(Document, document_id)
    if doc is None or doc.user_id != user.id:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc.project_id is not None:
        raise HTTPException(
            status_code=409, detail="프로젝트 문서는 프로젝트 화면에서 지워 주세요."
        )
    if doc.conversation_id is not None:
        raise HTTPException(
            status_code=409, detail="이미 보낸 문서는 대화를 지울 때 함께 지워집니다."
        )
    key = doc.storage_key
    await session.delete(doc)
    await session.commit()
    await service.delete_objects([key])
