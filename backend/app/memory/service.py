"""장기 기억 읽기·쓰기. 에이전트 도구와 기억 관리 API 가 같이 쓴다.

길이·개수 제한과 카테고리 검사를 여기서 한 번만 해서, 모델이 저장하든 사용자가 화면에서 고치든 같은
규칙을 따른다. 사용자 ID 는 항상 서버가 정한 값(로그인 사용자, 실행 설정)을 받는다.
"""

import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, get_args

from langgraph.store.base import BaseStore, Item

Category = Literal["preferences", "work", "personal", "other"]
"""기억 카테고리: 선호(답변 방식, 취향), 일·프로젝트, 생활(가족, 사는 곳, 건강 등), 기타."""

CATEGORIES: tuple[str, ...] = get_args(Category)

MAX_MEMORY_CHARS = 500
"""기억 하나의 최대 글자 수. 기억은 짧은 사실 하나여야 한다."""

MAX_MEMORIES = 200
"""사용자 한 명이 가질 수 있는 기억 수."""

MAX_PROFILE_CHARS = 800
"""프로필 최대 글자 수 (한국어 약 500토큰). 매 턴 프롬프트에 들어가므로 작게 둔다."""

PROFILE_KEY = "profile"


class MemoryInputError(ValueError):
    """기억 내용이 비었거나, 너무 길거나, 카테고리가 잘못됐거나, 개수 한도를 넘었다.

    메시지(영어)는 도구 결과로 모델에게 보여주고, ``user_message`` 는 관리 화면에 보여준다.

    Attributes:
        user_message: 사용자에게 보여줄 한국어 안내.
    """

    def __init__(self, message: str, user_message: str) -> None:
        """오류를 만든다.

        Args:
            message: 모델에게 보여줄 영어 설명.
            user_message: 사용자에게 보여줄 한국어 안내.
        """
        super().__init__(message)
        self.user_message = user_message


class MemoryNotFoundError(LookupError):
    """그 ID 의 기억이 없다 (다른 사용자의 기억도 없는 것으로 본다)."""


@dataclass(frozen=True)
class Memory:
    """기억 하나.

    Attributes:
        id: 기억 ID (8자리). 모델이 고치거나 지울 때 쓴다.
        content: 내용.
        category: 카테고리.
        created_at: 처음 저장한 시각.
        updated_at: 마지막으로 고친 시각.
        score: 검색했을 때의 유사도. 검색이 아니면 ``None``.
    """

    id: str
    content: str
    category: str
    created_at: datetime
    updated_at: datetime
    score: float | None = None


@dataclass(frozen=True)
class Profile:
    """사용자 프로필.

    Attributes:
        text: 프로필 글. 없으면 빈 문자열.
        updated_at: 마지막으로 고친 시각. 저장한 적이 없으면 ``None``.
    """

    text: str
    updated_at: datetime | None


def profile_namespace(user_id: str) -> tuple[str, ...]:
    """프로필을 두는 네임스페이스."""
    return ("users", user_id)


def memories_namespace(user_id: str) -> tuple[str, ...]:
    """기억을 두는 네임스페이스."""
    return ("users", user_id, "memories")


def _has_index(store: BaseStore) -> bool:
    """의미 검색을 쓸 수 있는 Store 인지 (임베딩 설정이 있음)."""
    return bool(getattr(store, "index_config", None))


def _to_memory(item: Item) -> Memory:
    return Memory(
        id=item.key,
        content=str(item.value.get("content", "")),
        category=str(item.value.get("category", "other")),
        created_at=item.created_at,
        updated_at=item.updated_at,
        score=getattr(item, "score", None),
    )


def _clean_content(content: str) -> str:
    text = " ".join(content.split())
    if not text:
        raise MemoryInputError("memory content is empty", "기억 내용이 비었습니다.")
    if len(text) > MAX_MEMORY_CHARS:
        raise MemoryInputError(
            f"memory is too long ({len(text)} characters, max {MAX_MEMORY_CHARS})",
            f"기억은 {MAX_MEMORY_CHARS}자까지 쓸 수 있습니다.",
        )
    return text


def _check_category(category: str) -> str:
    if category not in CATEGORIES:
        raise MemoryInputError(
            f"unknown category {category!r} (use one of {', '.join(CATEGORIES)})",
            "알 수 없는 카테고리입니다.",
        )
    return category


# --- 프로필 ----------------------------------------------------------------------------------------


async def get_profile(store: BaseStore, user_id: str) -> Profile:
    """사용자 프로필을 읽는다.

    Args:
        store: 기억 Store.
        user_id: 사용자 ID.

    Returns:
        프로필. 저장한 적이 없으면 빈 프로필.
    """
    item = await store.aget(profile_namespace(user_id), PROFILE_KEY)
    if item is None:
        return Profile(text="", updated_at=None)
    return Profile(text=str(item.value.get("text", "")), updated_at=item.updated_at)


async def set_profile(store: BaseStore, user_id: str, text: str) -> Profile:
    """프로필 전체를 새 글로 바꾼다. 빈 글이면 프로필을 지운다.

    Args:
        store: 기억 Store.
        user_id: 사용자 ID.
        text: 새 프로필 글.

    Returns:
        저장한 프로필.

    Raises:
        MemoryInputError: 글이 ``MAX_PROFILE_CHARS`` 보다 길 때.
    """
    text = text.strip()
    if len(text) > MAX_PROFILE_CHARS:
        raise MemoryInputError(
            f"profile is too long ({len(text)} characters, max {MAX_PROFILE_CHARS}); keep only the key facts",
            f"프로필은 {MAX_PROFILE_CHARS}자까지 쓸 수 있습니다.",
        )
    if not text:
        await store.adelete(profile_namespace(user_id), PROFILE_KEY)
        return Profile(text="", updated_at=None)
    # 프로필은 항상 통째로 넣으므로 임베딩하지 않는다.
    await store.aput(profile_namespace(user_id), PROFILE_KEY, {"text": text}, index=False)
    return await get_profile(store, user_id)


# --- 기억 ------------------------------------------------------------------------------------------


async def list_memories(store: BaseStore, user_id: str) -> list[Memory]:
    """사용자의 기억을 모두 최근에 고친 순서로 돌려준다.

    Args:
        store: 기억 Store.
        user_id: 사용자 ID.

    Returns:
        기억 목록 (최대 ``MAX_MEMORIES`` 개).
    """
    items = await store.asearch(memories_namespace(user_id), limit=MAX_MEMORIES)
    memories = [_to_memory(item) for item in items]
    return sorted(memories, key=lambda m: m.updated_at, reverse=True)


async def get_memory(store: BaseStore, user_id: str, memory_id: str) -> Memory:
    """기억 하나를 읽는다.

    Args:
        store: 기억 Store.
        user_id: 사용자 ID.
        memory_id: 기억 ID.

    Returns:
        기억.

    Raises:
        MemoryNotFoundError: 그 사용자에게 그 ID 의 기억이 없을 때.
    """
    item = await store.aget(memories_namespace(user_id), memory_id)
    if item is None:
        raise MemoryNotFoundError(memory_id)
    return _to_memory(item)


async def add_memory(store: BaseStore, user_id: str, content: str, category: str) -> Memory:
    """새 기억을 저장한다.

    Args:
        store: 기억 Store.
        user_id: 사용자 ID.
        content: 내용.
        category: 카테고리 (``CATEGORIES``).

    Returns:
        저장한 기억.

    Raises:
        MemoryInputError: 내용이 비었거나 길거나, 카테고리가 잘못됐거나, 기억이 이미 ``MAX_MEMORIES`` 개일 때.
    """
    text = _clean_content(content)
    category = _check_category(category)
    existing = await store.asearch(memories_namespace(user_id), limit=MAX_MEMORIES)
    if len(existing) >= MAX_MEMORIES:
        raise MemoryInputError(
            f"there are already {MAX_MEMORIES} memories; update or delete an existing one instead",
            f"기억은 {MAX_MEMORIES}개까지 저장할 수 있습니다. 필요 없는 기억을 지워 주세요.",
        )
    memory_id = secrets.token_hex(4)
    await store.aput(
        memories_namespace(user_id), memory_id, {"content": text, "category": category}
    )
    return await get_memory(store, user_id, memory_id)


async def update_memory(
    store: BaseStore,
    user_id: str,
    memory_id: str,
    content: str | None = None,
    category: str | None = None,
) -> Memory:
    """기억의 내용이나 카테고리를 고친다. 주지 않은 값은 그대로 둔다.

    Args:
        store: 기억 Store.
        user_id: 사용자 ID.
        memory_id: 고칠 기억 ID.
        content: 새 내용.
        category: 새 카테고리.

    Returns:
        고친 기억.

    Raises:
        MemoryNotFoundError: 그 ID 의 기억이 없을 때.
        MemoryInputError: 새 내용이나 카테고리가 잘못됐을 때.
    """
    current = await get_memory(store, user_id, memory_id)
    value = {
        "content": _clean_content(content) if content is not None else current.content,
        "category": _check_category(category) if category is not None else current.category,
    }
    await store.aput(memories_namespace(user_id), memory_id, value)
    return await get_memory(store, user_id, memory_id)


async def delete_memory(store: BaseStore, user_id: str, memory_id: str) -> Memory:
    """기억 하나를 지운다.

    Args:
        store: 기억 Store.
        user_id: 사용자 ID.
        memory_id: 지울 기억 ID.

    Returns:
        지운 기억.

    Raises:
        MemoryNotFoundError: 그 ID 의 기억이 없을 때.
    """
    memory = await get_memory(store, user_id, memory_id)
    await store.adelete(memories_namespace(user_id), memory_id)
    return memory


async def delete_all(store: BaseStore, user_id: str) -> int:
    """사용자의 프로필과 기억을 모두 지운다.

    Args:
        store: 기억 Store.
        user_id: 사용자 ID.

    Returns:
        지운 기억 수 (프로필 제외).
    """
    memories = await list_memories(store, user_id)
    for memory in memories:
        await store.adelete(memories_namespace(user_id), memory.id)
    await store.adelete(profile_namespace(user_id), PROFILE_KEY)
    return len(memories)


async def search_memories(
    store: BaseStore, user_id: str, query: str, limit: int, min_score: float | None = None
) -> list[Memory]:
    """질문과 관련 있는 기억을 찾는다.

    임베딩을 쓸 수 없는 Store 면 관련도 대신 최근에 고친 기억을 돌려준다.

    Args:
        store: 기억 Store.
        user_id: 사용자 ID.
        query: 찾을 내용 (보통 사용자 메시지).
        limit: 최대 개수.
        min_score: 최소 유사도. 이보다 덜 관련된 기억은 뺀다 (임베딩이 없으면 쓰지 않는다).

    Returns:
        관련도가 높은 순서의 기억.
    """
    if not _has_index(store) or not query.strip():
        return (await list_memories(store, user_id))[:limit]
    items = await store.asearch(memories_namespace(user_id), query=query, limit=limit)
    memories = [_to_memory(item) for item in items]
    if min_score is not None:
        memories = [m for m in memories if m.score is None or m.score >= min_score]
    return memories


def format_memory(memory: Memory) -> str:
    """모델에게 보여줄 기억 한 줄: ``[ID] (카테고리, 날짜) 내용``.

    ID 는 모델이 고치거나 지울 때 쓰고, 날짜는 오래된 정보를 구분하게 한다.

    Args:
        memory: 기억.

    Returns:
        한 줄 문자열.
    """
    return f"- [{memory.id}] ({memory.category}, {memory.updated_at.date().isoformat()}) {memory.content}"
