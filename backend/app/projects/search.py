"""프로젝트 문서 검색 (벡터 검색, pgvector).

검색어를 임베딩해서 프로젝트의 준비된 문서 청크 중 코사인 거리가 가까운 것을 찾는다. 검색 전에 임베딩이
빠졌거나 지금 설정과 다른 모델로 만든 청크를 다시 임베딩한다 (적재 때 임베딩에 실패했거나 모델을 바꾼 경우).
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from langchain_core.embeddings import Embeddings
from sqlalchemy import or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.documents import chunking
from app.llm import embeddings
from app.models import Document, DocumentChunk

logger = logging.getLogger(__name__)

REEMBED_MAX_CHUNKS = 2000
"""검색 한 번에 다시 임베딩할 최대 청크 수. 넘으면 다음 검색에서 이어서 한다."""


@dataclass(frozen=True)
class SearchHit:
    """검색 결과 청크 하나.

    Attributes:
        chunk_id: 청크 ID.
        document_id: 문서 ID.
        filename: 문서 이름.
        kind: ``text`` | ``table`` | ``image``.
        page_start: 첫 쪽 번호.
        page_end: 마지막 쪽 번호.
        heading: 제목 경로.
        content: 청크 본문 (그림은 설명).
        image_element_id: 그림 청크의 그림 요소 ID.
        score: 코사인 유사도 (1 에 가까울수록 비슷하다).
    """

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    kind: str
    page_start: int | None
    page_end: int | None
    heading: str | None
    content: str
    image_element_id: uuid.UUID | None
    score: float


async def search(
    session: AsyncSession, settings: Settings, project_id: uuid.UUID, query: str, top_k: int
) -> list[SearchHit]:
    """프로젝트 문서에서 검색어와 가까운 청크를 찾는다.

    Args:
        session: DB 세션. 다시 임베딩한 청크를 저장하려고 commit 한다.
        settings: 앱 설정 (임베딩).
        project_id: 프로젝트 ID. 이 프로젝트의 준비된 문서만 찾는다.
        query: 검색어.
        top_k: 돌려줄 청크 수.

    Returns:
        가까운 순서의 청크.

    Raises:
        EmbeddingUnavailableError: 임베딩을 쓸 수 없을 때 (OpenAI 키 없음).
    """
    model = embeddings.build_embeddings(settings)
    current = embeddings.model_id(settings)
    await _embed_stale_chunks(session, model, current, project_id)
    vector = await model.aembed_query(query)

    distance = DocumentChunk.embedding.cosine_distance(vector)
    # 프로젝트로 거르면 HNSW 가 찾은 후보가 다른 프로젝트 것뿐일 수 있다. 결과가 모자라면 더 찾게 한다.
    await session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
    rows = await session.execute(
        select(DocumentChunk, Document.filename, distance.label("distance"))
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(
            DocumentChunk.project_id == project_id,
            Document.status == "ready",
            DocumentChunk.embedding_model == current,
            DocumentChunk.embedding.is_not(None),
        )
        .order_by(distance)
        .limit(top_k)
    )
    hits = [
        SearchHit(
            chunk_id=chunk.id,
            document_id=chunk.document_id,
            filename=filename,
            kind=chunk.kind,
            page_start=chunk.page_start,
            page_end=chunk.page_end,
            heading=chunk.heading,
            content=chunk.content,
            image_element_id=chunk.image_element_id,
            score=1 - float(dist),
        )
        for chunk, filename, dist in rows.all()
    ]
    # relaxed_order 는 순서가 조금 어긋날 수 있다.
    return sorted(hits, key=lambda h: h.score, reverse=True)


async def _embed_stale_chunks(
    session: AsyncSession, model: Embeddings, current: str, project_id: uuid.UUID
) -> None:
    """임베딩이 없거나 다른 모델로 만든 청크를 다시 임베딩한다."""
    rows = (
        await session.execute(
            select(DocumentChunk, Document.filename)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(
                DocumentChunk.project_id == project_id,
                Document.status == "ready",
                or_(
                    DocumentChunk.embedding.is_(None),
                    DocumentChunk.embedding_model.is_distinct_from(current),
                ),
            )
            .order_by(DocumentChunk.document_id, DocumentChunk.position)
            .limit(REEMBED_MAX_CHUNKS)
        )
    ).all()
    if not rows:
        return
    texts = [chunking.embedding_text(filename, _as_chunk(chunk)) for chunk, filename in rows]
    vectors = await model.aembed_documents(texts)
    for (chunk, _), vector in zip(rows, vectors, strict=True):
        await session.execute(
            update(DocumentChunk)
            .where(DocumentChunk.id == chunk.id)
            .values(embedding=vector, embedding_model=current)
        )
    await session.commit()
    logger.info("re-embedded %d chunks of project %s", len(rows), project_id)


def _as_chunk(row: DocumentChunk) -> chunking.Chunk:
    return chunking.Chunk(
        row.kind,
        row.content,
        row.page_start,
        row.page_end,
        row.heading,
        row.image_element_id,
        row.token_count,
    )


def format_hits(query: str, hits: list[SearchHit]) -> str:
    """검색 결과를 모델에게 줄 글로 만든다.

    Args:
        query: 검색어.
        hits: 검색 결과.

    Returns:
        결과마다 번호, 문서 이름·ID, 쪽, 제목 경로, 종류와 본문을 담은 글.
    """
    if not hits:
        return (
            f"No passages found in the project documents for {query!r}. "
            "Try different keywords, or read a document with read_document."
        )
    lines = [f"Search results for {query!r} in the project documents (most relevant first):"]
    for i, hit in enumerate(hits, 1):
        meta = [f"document: {hit.filename} (document_id: {hit.document_id})"]
        if hit.page_start is not None:
            meta.append(
                f"page {hit.page_start}"
                if hit.page_end in (None, hit.page_start)
                else f"pages {hit.page_start}-{hit.page_end}"
            )
        if hit.heading:
            meta.append(f"section: {hit.heading}")
        if hit.kind == "image":
            meta.append("description of an image")
        elif hit.kind == "table":
            meta.append("table")
        lines.append(f"\n[{i}] {' | '.join(meta)}\n{hit.content}")
    return "\n".join(lines)


def sources(project_id: uuid.UUID, hits: list[SearchHit]) -> list[dict[str, Any]]:
    """검색 결과를 화면의 출처 카드 목록으로 만든다 (도구 artifact). 같은 문서·위치는 한 번만 넣는다.

    Args:
        project_id: 프로젝트 ID (그림 미리보기 주소에 쓴다).
        hits: 검색 결과.

    Returns:
        ``{"type": "document", "title", "document_id", "project_id", "location", "image_element_id"}`` 목록.
    """
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str | None]] = set()
    for hit in hits:
        parts = []
        if hit.page_start is not None:
            parts.append(
                f"p.{hit.page_start}"
                if hit.page_end in (None, hit.page_start)
                else f"p.{hit.page_start}-{hit.page_end}"
            )
        if hit.heading:
            parts.append(hit.heading)
        location = " · ".join(parts)
        image = str(hit.image_element_id) if hit.image_element_id else None
        key = (str(hit.document_id), location, image)
        if key in seen:
            continue
        seen.add(key)
        found.append(
            {
                "type": "document",
                "title": hit.filename,
                "document_id": str(hit.document_id),
                "project_id": str(project_id),
                "location": location,
                "image_element_id": image,
            }
        )
    return found
