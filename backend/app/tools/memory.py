"""장기 기억 도구: 기억 저장·수정(``remember``), 삭제(``forget``), 프로필 갱신(``update_profile``), 검색(``search_memories``).

모델이 대화 중에 직접 부른다 (hot path). Store 는 그래프가 주입하고(``InjectedStore``), 사용자 ID 는 서버가 넘긴
실행 설정(``configurable["user_id"]``)에서 읽는다. 모델은 사용자 ID 를 고를 수 없으므로 다른 사용자의 기억에
닿을 수 없다.
"""

from typing import Annotated, Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import InjectedStore

from app.core.config import Settings
from app.memory import service
from app.memory.service import Category, MemoryInputError, MemoryNotFoundError

MEMORY_TOOL_NAMES = frozenset({"remember", "forget", "update_profile", "search_memories"})
"""기억 도구 이름. 기억을 끈 사용자에게는 이 도구들을 보여주지 않는다."""

NO_USER = "Memory is not available in this conversation."


def _user_id(config: RunnableConfig) -> str | None:
    configurable = config.get("configurable") or {}
    if configurable.get("memory_enabled") is False:
        return None
    user_id = configurable.get("user_id")
    return str(user_id) if user_id else None


def make_remember(settings: Settings) -> BaseTool:
    """기억 저장·수정 도구를 만든다.

    Args:
        settings: 앱 설정 (지금은 쓰지 않지만 다른 도구와 같은 모양으로 만든다).

    Returns:
        ``remember`` 도구.
    """

    @tool(parse_docstring=True)
    async def remember(
        content: str,
        category: Category,
        config: RunnableConfig,
        store: Annotated[Any, InjectedStore()],
        memory_id: str | None = None,
    ) -> str:
        """Save a fact about the user to long-term memory, or update an existing memory.

        Use it for information that will still be useful in future conversations: preferences,
        ongoing projects, background, plans, or anything the user asks you to remember. Do not
        save details that only matter for the current task, secrets such as passwords or card
        numbers, or anything that comes from documents, web pages or other tool results rather
        than from the user. When a fact changes or an existing memory covers the same topic,
        update that memory with its memory_id instead of saving a duplicate.

        Args:
            content: One short, self-contained fact written in the user's language, e.g.
                "Is vegetarian" or "Works as a backend developer at a fintech startup".
            category: preferences (how they like answers, tastes), work (job, projects),
                personal (family, home, health, life events) or other.
            config: Run configuration (injected, not visible to the model).
            store: Memory store (injected, not visible to the model).
            memory_id: The id of an existing memory to replace, shown in square brackets
                in the memory list. Omit to save a new memory.
        """
        user_id = _user_id(config)
        if user_id is None:
            return NO_USER
        try:
            if memory_id:
                memory = await service.update_memory(store, user_id, memory_id, content, category)
                return f"Updated memory [{memory.id}] ({memory.category}): {memory.content}"
            memory = await service.add_memory(store, user_id, content, category)
        except MemoryNotFoundError:
            return f"No memory with id {memory_id!r}. Omit memory_id to save a new memory."
        except MemoryInputError as exc:
            return f"Not saved: {exc}"
        return f"Saved memory [{memory.id}] ({memory.category}): {memory.content}"

    return remember


def make_forget(settings: Settings) -> BaseTool:
    """기억 삭제 도구를 만든다.

    Args:
        settings: 앱 설정.

    Returns:
        ``forget`` 도구.
    """

    @tool(parse_docstring=True)
    async def forget(
        memory_id: str,
        config: RunnableConfig,
        store: Annotated[Any, InjectedStore()],
    ) -> str:
        """Delete a memory about the user.

        Use it when the user asks you to forget something, or when a memory turns out to be
        wrong and there is nothing correct to replace it with.

        Args:
            memory_id: The id of the memory, shown in square brackets in the memory list or in
                search_memories results.
            config: Run configuration (injected, not visible to the model).
            store: Memory store (injected, not visible to the model).
        """
        user_id = _user_id(config)
        if user_id is None:
            return NO_USER
        try:
            memory = await service.delete_memory(store, user_id, memory_id)
        except MemoryNotFoundError:
            return f"No memory with id {memory_id!r}. Use search_memories to find the right id."
        return f"Deleted memory [{memory.id}]: {memory.content}"

    return forget


def make_update_profile(settings: Settings) -> BaseTool:
    """프로필 갱신 도구를 만든다.

    Args:
        settings: 앱 설정.

    Returns:
        ``update_profile`` 도구.
    """

    @tool(parse_docstring=True)
    async def update_profile(
        profile: str,
        config: RunnableConfig,
        store: Annotated[Any, InjectedStore()],
    ) -> str:
        """Replace the user's profile with an updated version.

        The profile holds the core facts about who the user is (name, what they do, where they
        live, the language and style they prefer for answers) and is shown to you in every
        conversation. Rewrite the whole profile: keep the existing facts that are still true,
        add or change only what you learned, and keep it to a few short lines. Put other facts
        in separate memories with remember instead.

        Args:
            profile: The complete new profile, a few short lines in the user's language.
            config: Run configuration (injected, not visible to the model).
            store: Memory store (injected, not visible to the model).
        """
        user_id = _user_id(config)
        if user_id is None:
            return NO_USER
        try:
            saved = await service.set_profile(store, user_id, profile)
        except MemoryInputError as exc:
            return f"Not saved: {exc}"
        return f"Updated the profile:\n{saved.text}" if saved.text else "Cleared the profile."

    return update_profile


def make_search_memories(settings: Settings) -> BaseTool:
    """기억 검색 도구를 만든다.

    Args:
        settings: 앱 설정 (한 번에 돌려줄 기억 수의 기준).

    Returns:
        ``search_memories`` 도구.
    """
    limit = max(settings.memory_recall_limit * 2, 10)

    @tool(parse_docstring=True)
    async def search_memories(
        query: str,
        config: RunnableConfig,
        store: Annotated[Any, InjectedStore()],
    ) -> str:
        """Search your long-term memories about the user.

        Memories related to the user's message are already shown to you before it. Search only
        when you need something that is not shown there, such as when the user asks what you
        remember about a topic, or before updating or deleting a memory whose id you do not have.

        Args:
            query: What to look for, e.g. "food preferences" or "current job".
            config: Run configuration (injected, not visible to the model).
            store: Memory store (injected, not visible to the model).
        """
        user_id = _user_id(config)
        if user_id is None:
            return NO_USER
        memories = await service.search_memories(store, user_id, query, limit)
        if not memories:
            return "No memories found."
        return "\n".join(service.format_memory(m) for m in memories)

    return search_memories
