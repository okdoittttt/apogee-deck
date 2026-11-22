"""장기 기억 관리 API: 내 프로필과 기억 보기·고치기·지우기, 기억 기능 켜기/끄기.

에이전트가 대화 중에 기억 도구로 저장한 것과 같은 저장소(LangGraph Store)를 다룬다. 항상 로그인한 사용자
자신의 기억만 다루고, 다른 사용자의 기억 ID 는 없는 것(404)으로 본다.
"""

from fastapi import APIRouter, HTTPException
from langgraph.store.base import BaseStore
from sqlalchemy import update

from app.api.deps import ApprovedUser, SessionDep
from app.api.schemas import (
    MemoryIn,
    MemoryOut,
    MemoryOverview,
    MemorySettingsIn,
    MemoryUpdate,
    ProfileIn,
    ProfileOut,
    memory_out,
)
from app.memory import service
from app.memory.service import MemoryInputError, MemoryNotFoundError
from app.memory.store import StoreUnavailableError, get_store
from app.models import User

router = APIRouter(prefix="/api/memory")

UNAVAILABLE = "기억 기능을 쓸 수 없습니다."
NOT_FOUND = "기억을 찾을 수 없습니다."


def _store() -> BaseStore:
    try:
        return get_store()
    except StoreUnavailableError as exc:
        raise HTTPException(status_code=503, detail=UNAVAILABLE) from exc


def _input_error(exc: MemoryInputError) -> HTTPException:
    return HTTPException(status_code=422, detail=exc.user_message)


@router.get("")
async def overview(user: ApprovedUser) -> MemoryOverview:
    """내 기억 설정, 프로필, 기억 목록을 돌려준다. 서버에서 기억 기능을 끄면 ``available`` 이 거짓이다.

    Args:
        user: 승인된 사용자.

    Returns:
        기억 관리 화면에 필요한 정보.
    """
    limits = {
        "max_memories": service.MAX_MEMORIES,
        "max_memory_chars": service.MAX_MEMORY_CHARS,
        "max_profile_chars": service.MAX_PROFILE_CHARS,
    }
    try:
        store = get_store()
    except StoreUnavailableError:
        return MemoryOverview(
            available=False,
            enabled=user.memory_enabled,
            profile=ProfileOut(text="", updated_at=None),
            memories=[],
            **limits,
        )
    profile = await service.get_profile(store, str(user.id))
    memories = await service.list_memories(store, str(user.id))
    return MemoryOverview(
        available=True,
        enabled=user.memory_enabled,
        profile=ProfileOut(text=profile.text, updated_at=profile.updated_at),
        memories=[memory_out(m) for m in memories],
        **limits,
    )


@router.put("/settings")
async def set_enabled(req: MemorySettingsIn, session: SessionDep, user: ApprovedUser) -> dict:
    """기억 기능을 켜거나 끈다. 끄면 저장된 기억은 남지만, 대화에서 쓰거나 새로 저장하지 않는다.

    Args:
        req: 켤지 끌지.
        session: DB 세션.
        user: 승인된 사용자.

    Returns:
        ``{"enabled": 바뀐 값}``.
    """
    await session.execute(update(User).where(User.id == user.id).values(memory_enabled=req.enabled))
    await session.commit()
    return {"enabled": req.enabled}


@router.put("/profile")
async def set_profile(req: ProfileIn, user: ApprovedUser) -> ProfileOut:
    """프로필을 새 글로 바꾼다. 비우면 프로필을 지운다.

    Args:
        req: 새 프로필.
        user: 승인된 사용자.

    Returns:
        저장한 프로필.

    Raises:
        HTTPException: 저장소를 쓸 수 없으면 503, 글이 너무 길면 422.
    """
    try:
        profile = await service.set_profile(_store(), str(user.id), req.text)
    except MemoryInputError as exc:
        raise _input_error(exc) from exc
    return ProfileOut(text=profile.text, updated_at=profile.updated_at)


@router.post("/memories", status_code=201)
async def add_memory(req: MemoryIn, user: ApprovedUser) -> MemoryOut:
    """기억을 직접 추가한다.

    Args:
        req: 내용과 카테고리.
        user: 승인된 사용자.

    Returns:
        저장한 기억.

    Raises:
        HTTPException: 저장소를 쓸 수 없으면 503, 내용이 잘못됐거나 개수 한도를 넘으면 422.
    """
    try:
        memory = await service.add_memory(_store(), str(user.id), req.content, req.category)
    except MemoryInputError as exc:
        raise _input_error(exc) from exc
    return memory_out(memory)


@router.patch("/memories/{memory_id}")
async def update_memory(memory_id: str, req: MemoryUpdate, user: ApprovedUser) -> MemoryOut:
    """기억의 내용이나 카테고리를 고친다.

    Args:
        memory_id: 기억 ID.
        req: 바꿀 값.
        user: 승인된 사용자.

    Returns:
        고친 기억.

    Raises:
        HTTPException: 저장소를 쓸 수 없으면 503, 내 기억이 아니면 404, 내용이 잘못됐으면 422.
    """
    try:
        memory = await service.update_memory(
            _store(), str(user.id), memory_id, req.content, req.category
        )
    except MemoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=NOT_FOUND) from exc
    except MemoryInputError as exc:
        raise _input_error(exc) from exc
    return memory_out(memory)


@router.delete("/memories/{memory_id}", status_code=204)
async def delete_memory(memory_id: str, user: ApprovedUser) -> None:
    """기억 하나를 지운다.

    Args:
        memory_id: 기억 ID.
        user: 승인된 사용자.

    Raises:
        HTTPException: 저장소를 쓸 수 없으면 503, 내 기억이 아니면 404.
    """
    try:
        await service.delete_memory(_store(), str(user.id), memory_id)
    except MemoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=NOT_FOUND) from exc


@router.delete("", status_code=204)
async def delete_all(user: ApprovedUser) -> None:
    """내 프로필과 기억을 모두 지운다.

    Args:
        user: 승인된 사용자.

    Raises:
        HTTPException: 저장소를 쓸 수 없으면 503.
    """
    await service.delete_all(_store(), str(user.id))
