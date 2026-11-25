"""프로젝트 문서 검색: 하이브리드(벡터 + 키워드) 검색과 결과 합치기(RRF).

- 벡터 검색: 검색어를 임베딩해서 코사인 거리가 가까운 청크를 찾는다 (pgvector HNSW). 뜻이 비슷한 글을 찾는다.
- 키워드 검색: 검색어 토큰(한글은 두 글자 조각, ``app.documents.keywords``)이 들어 있는 청크를 Postgres 전문
  검색(GIN)으로 찾는다. 코드명·숫자·고유명사처럼 글자가 정확히 같아야 하는 것을 잘 찾는다.
- 두 결과의 순위를 RRF(Reciprocal Rank Fusion)로 합친다: 청크마다 ``1 / (k + 순위)`` 를 더한다. 점수 단위가
  다른 두 검색을 순위만으로 공평하게 합칠 수 있다.

임베딩을 쓸 수 없거나(키 없음) 실패하면 키워드 검색만 쓴다. 검색 전에 임베딩·키워드 토큰이 빠졌거나 지금
설정과 다른 모델로 만든 청크를 다시 만든다 (적재 때 실패했거나, 모델을 바꿨거나, 기능 추가 전에 만든 청크).
"""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.embeddings import Embeddings
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.citations import source_marker
from app.core.config import Settings
from app.documents import chunking, keywords
from app.llm import embeddings
from app.models import Document, DocumentChunk

logger = logging.getLogger(__name__)

REFRESH_MAX_CHUNKS = 2000
"""검색 한 번에 다시 만들 최대 청크 수 (임베딩, 키워드 토큰 각각). 넘으면 다음 검색에서 이어서 한다."""


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
        score: RRF 점수 (클수록 앞). 두 검색 모두 1위면 ``2 / (k + 1)``.
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


def fuse(rankings: Sequence[Sequence[uuid.UUID]], k: int) -> list[tuple[uuid.UUID, float]]:
    """여러 검색 결과의 순위를 RRF 로 합친다.

    Args:
        rankings: 검색마다 앞에서부터 순위대로의 청크 ID.
        k: RRF 상수. 클수록 위아래 순위 차이를 덜 반영한다.

    Returns:
        ``(청크 ID, 점수)`` 를 점수가 큰 순서로. 점수가 같으면 먼저 나온 검색의 순위를 따른다.
    """
    scores: dict[uuid.UUID, float] = {}
    for ranking in rankings:
        for rank, chunk_id in enumerate(ranking, 1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1 / (k + rank)
    return sorted(scores.items(), key=lambda item: item[1], reverse=True)


async def search(
    session: AsyncSession,
    settings: Settings,
    project_id: uuid.UUID,
    query: str,
    top_k: int,
    document_id: uuid.UUID | None = None,
) -> list[SearchHit]:
    """프로젝트 문서에서 검색어에 맞는 청크를 하이브리드 검색으로 찾는다.

    Args:
        session: DB 세션. 다시 만든 임베딩·키워드 토큰을 저장하려고 commit 한다.
        settings: 앱 설정 (임베딩, 후보 수, RRF 상수).
        project_id: 프로젝트 ID. 이 프로젝트의 준비된 문서만 찾는다.
        query: 검색어.
        top_k: 돌려줄 청크 수.
        document_id: 주면 이 문서 안에서만 찾는다 (프로젝트 조건은 그대로).

    Returns:
        합친 순위대로의 청크.
    """
    candidates = max(top_k, settings.project_search_candidates)
    await _fill_search_vectors(session, project_id)
    scope = _scope(project_id, document_id)
    keyword_ids = await _keyword_search(session, scope, query, candidates)
    vector_ids: list[uuid.UUID] = []
    try:
        model = embeddings.build_embeddings(settings)
        current = embeddings.model_id(settings)
        await _embed_stale_chunks(session, model, current, project_id)
        vector = await model.aembed_query(query)
        vector_ids = await _vector_search(session, scope, vector, current, candidates)
    except embeddings.EmbeddingUnavailableError as exc:
        logger.info("vector search skipped: %s", exc)
    except Exception:
        await session.rollback()
        logger.warning("vector search failed, using keyword search only", exc_info=True)
    fused = fuse([vector_ids, keyword_ids], settings.project_search_rrf_k)[:top_k]
    return await _load_hits(session, fused)


def _scope(project_id: uuid.UUID, document_id: uuid.UUID | None) -> list[Any]:
    """검색 범위 조건: 이 프로젝트의 준비된 문서, ``document_id`` 를 주면 그 문서만."""
    conditions: list[Any] = [DocumentChunk.project_id == project_id, Document.status == "ready"]
    if document_id is not None:
        conditions.append(DocumentChunk.document_id == document_id)
    return conditions


async def _vector_search(
    session: AsyncSession,
    scope: list[Any],
    vector: list[float],
    model_id: str,
    limit: int,
) -> list[uuid.UUID]:
    """코사인 거리가 가까운 청크 ID (가까운 순)."""
    distance = DocumentChunk.embedding.cosine_distance(vector)
    # 프로젝트로 거르면 HNSW 가 찾은 후보가 다른 프로젝트 것뿐일 수 있다. 결과가 모자라면 더 찾게 한다.
    await session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
    rows = await session.execute(
        select(DocumentChunk.id, distance.label("distance"))
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(
            *scope,
            DocumentChunk.embedding_model == model_id,
            DocumentChunk.embedding.is_not(None),
        )
        .order_by(distance)
        .limit(limit)
    )
    # relaxed_order 는 순서가 조금 어긋날 수 있어서 다시 정렬한다.
    return [chunk_id for chunk_id, _ in sorted(rows.all(), key=lambda r: r[1])]


async def _keyword_search(
    session: AsyncSession, scope: list[Any], query: str, limit: int
) -> list[uuid.UUID]:
    """검색어 토큰이 들어 있는 청크 ID (``ts_rank`` 순). 토큰이 많이, 짧은 청크에 들어 있을수록 앞."""
    words = keywords.keyword_query(query)
    if words is None:
        return []
    tsquery = func.to_tsquery("simple", words)
    # 정규화 1: 점수를 1 + log(청크 길이) 로 나눠 긴 청크가 단어 수만으로 앞서지 않게 한다.
    rank = func.ts_rank(DocumentChunk.search_vector, tsquery, 1)
    rows = await session.scalars(
        select(DocumentChunk.id)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(
            *scope,
            DocumentChunk.search_vector.op("@@")(tsquery),
        )
        .order_by(rank.desc(), DocumentChunk.id)
        .limit(limit)
    )
    return list(rows.all())


async def _load_hits(
    session: AsyncSession, fused: list[tuple[uuid.UUID, float]]
) -> list[SearchHit]:
    """합친 순위의 청크를 읽어 검색 결과로 만든다 (순서 유지)."""
    if not fused:
        return []
    rows = await session.execute(
        select(DocumentChunk, Document.filename)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(DocumentChunk.id.in_([chunk_id for chunk_id, _ in fused]))
    )
    found = {chunk.id: (chunk, filename) for chunk, filename in rows.all()}
    hits = []
    for chunk_id, score in fused:
        if chunk_id not in found:
            continue
        chunk, filename = found[chunk_id]
        hits.append(
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
                score=score,
            )
        )
    return hits


async def _stale_chunks(
    session: AsyncSession, project_id: uuid.UUID, condition: Any
) -> list[tuple[DocumentChunk, str]]:
    """조건에 맞는(다시 만들어야 할) 준비된 문서의 청크와 문서 이름."""
    rows = await session.execute(
        select(DocumentChunk, Document.filename)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(DocumentChunk.project_id == project_id, Document.status == "ready", condition)
        .order_by(DocumentChunk.document_id, DocumentChunk.position)
        .limit(REFRESH_MAX_CHUNKS)
    )
    return [(chunk, filename) for chunk, filename in rows.all()]


async def _fill_search_vectors(session: AsyncSession, project_id: uuid.UUID) -> None:
    """키워드 토큰이 없는 청크(키워드 검색 추가 전에 만든 것)의 토큰을 만든다."""
    rows = await _stale_chunks(session, project_id, DocumentChunk.search_vector.is_(None))
    if not rows:
        return
    for chunk, filename in rows:
        document = keywords.keyword_document(chunking.embedding_text(filename, _as_chunk(chunk)))
        await session.execute(
            update(DocumentChunk)
            .where(DocumentChunk.id == chunk.id)
            .values(search_vector=func.to_tsvector("simple", document))
        )
    await session.commit()
    logger.info("filled keyword tokens of %d chunks in project %s", len(rows), project_id)


async def _embed_stale_chunks(
    session: AsyncSession, model: Embeddings, current: str, project_id: uuid.UUID
) -> None:
    """임베딩이 없거나 다른 모델로 만든 청크를 다시 임베딩한다."""
    rows = await _stale_chunks(
        session,
        project_id,
        or_(
            DocumentChunk.embedding.is_(None),
            DocumentChunk.embedding_model.is_distinct_from(current),
        ),
    )
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


def format_hits(query: str, hits: list[SearchHit], document: str | None = None) -> str:
    """검색 결과를 모델에게 줄 글로 만든다.

    Args:
        query: 검색어.
        hits: 검색 결과.
        document: 문서 하나로 좁혀 찾았으면 그 문서 이름.

    Returns:
        결과마다 번호 자리 표시(``source_marker``), 문서 이름·ID, 쪽, 제목 경로, 종류와 본문을 담은 글.
        자리 표시는 도구 노드가 끝나면 한 턴 안에서 이어지는 번호로 바뀐다.
    """
    where = f"the project document {document!r}" if document else "the project documents"
    if not hits:
        retry = " or search all documents (leave document_id empty)" if document else ""
        return (
            f"No passages found in {where} for {query!r}. "
            f"Try different keywords{retry}, or read a document with read_document."
        )
    lines = [f"Search results for {query!r} in {where} (most relevant first):"]
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
            # 그림 id 는 Word 문서에 그림을 넣을 때 쓴다 (edit_document 의 image: "img:<id>").
            image = f" (image: img:{hit.image_element_id})" if hit.image_element_id else ""
            meta.append(f"description of an image{image}")
        elif hit.kind == "table":
            meta.append("table")
        lines.append(f"\n{source_marker(i)} {' | '.join(meta)}\n{hit.content}")
    # 웹 검색 결과처럼 끝에 인용 방법을 붙인다. 없으면 모델이 문서 이름·쪽(【…】)으로 따로 적는다.
    lines.append(
        "\nCite the passages you use only by their numbers, such as [2]. Do not add document "
        "names or pages to a citation and do not use other citation formats such as 【…】: "
        "the app shows the document and page next to each number."
    )
    return "\n".join(lines)


def sources(project_id: uuid.UUID, hits: list[SearchHit]) -> list[dict[str, Any]]:
    """검색 결과를 화면의 출처 카드 목록으로 만든다 (도구 artifact).

    결과 하나가 출처 하나다 (모델에게 준 번호와 순서가 같아야 답변 속 번호가 카드와 맞는다).

    Args:
        project_id: 프로젝트 ID (그림 미리보기 주소에 쓴다).
        hits: 검색 결과.

    Returns:
        ``{"type": "document", "title", "document_id", "project_id", "location", "image_element_id",
        "chunk_id"}`` 목록. ``chunk_id`` 는 문서 보기 패널에서 근거 위치를 강조할 때 쓴다.
    """
    found: list[dict[str, Any]] = []
    for hit in hits:
        parts = []
        if hit.page_start is not None:
            parts.append(
                f"p.{hit.page_start}"
                if hit.page_end in (None, hit.page_start)
                else f"p.{hit.page_start}-{hit.page_end}"
            )
        if hit.heading:
            # PPTX 슬라이드 제목 등은 제목 경로에 Markdown 굵게 표시(**)가 남아 있다.
            parts.append(hit.heading.replace("**", "").strip())
        location = " · ".join(parts)
        image = str(hit.image_element_id) if hit.image_element_id else None
        found.append(
            {
                "type": "document",
                "title": hit.filename,
                "document_id": str(hit.document_id),
                "project_id": str(project_id),
                "location": location,
                "image_element_id": image,
                "chunk_id": str(hit.chunk_id),
            }
        )
    return found
