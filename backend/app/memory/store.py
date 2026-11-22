"""장기 기억을 저장하는 LangGraph Store 의 생명주기.

앱이 시작할 때 실행 엔진(``runtime.startup``)이 checkpointer 와 같은 연결 풀로 Store 를 연다.
그래프(노드와 도구)는 컴파일할 때 받은 Store 를 쓰고, 기억 관리 API 는 ``get_store`` 로 같은 Store 를 쓴다.
"""

import logging

from langgraph.store.base import BaseStore, IndexConfig
from langgraph.store.postgres.aio import AsyncPostgresStore
from psycopg_pool import AsyncConnectionPool

from app.core.config import Settings
from app.llm.embeddings import EmbeddingUnavailableError, build_embeddings

logger = logging.getLogger(__name__)

INDEXED_FIELDS = ["content"]
"""의미 검색에 쓰는 값의 필드. 프로필처럼 이 필드가 없는 항목은 임베딩하지 않는다."""

_store: BaseStore | None = None


class StoreUnavailableError(Exception):
    """기억 저장소가 열리지 않았다 (앱 시작 전이거나 기억 기능이 꺼짐)."""


def index_config(settings: Settings) -> IndexConfig | None:
    """의미 검색 설정. 임베딩을 쓸 수 없으면(키 없음) ``None`` 이고, 검색은 최근 기억으로 대신한다.

    Args:
        settings: 앱 설정 (임베딩 provider, 모델, 차원).

    Returns:
        Store 의 ``index`` 설정, 또는 ``None``.
    """
    try:
        embeddings = build_embeddings(settings)
    except EmbeddingUnavailableError:
        logger.warning("no embeddings for memory search; recent memories are used instead")
        return None
    return {"dims": settings.embedding_dimensions, "embed": embeddings, "fields": INDEXED_FIELDS}


async def open_store(pool: AsyncConnectionPool, settings: Settings) -> BaseStore:
    """Postgres Store 를 열고 테이블을 준비한다 (LangGraph 자체 마이그레이션, Alembic 관리 대상 아님).

    벡터 테이블의 차원은 처음 만들 때 정해진다. ``EMBEDDING_DIMENSIONS`` 를 바꾸면 기억을 다시 인덱싱해야 한다.

    Args:
        pool: checkpointer 와 같은 연결 풀 (autocommit, dict_row).
        settings: 앱 설정.

    Returns:
        열린 Store. ``get_store`` 로도 받을 수 있다.
    """
    global _store
    store = AsyncPostgresStore(pool, index=index_config(settings))
    await store.setup()
    _store = store
    return store


def set_store(store: BaseStore | None) -> None:
    """쓸 Store 를 정한다 (테스트에서 ``InMemoryStore`` 를 넣거나, 앱 종료 때 비운다).

    Args:
        store: 쓸 Store. ``None`` 이면 비운다.
    """
    global _store
    _store = store


def get_store() -> BaseStore:
    """열린 Store 를 돌려준다.

    Returns:
        Store.

    Raises:
        StoreUnavailableError: Store 가 열리지 않았을 때.
    """
    if _store is None:
        raise StoreUnavailableError("memory store is not open")
    return _store
