"""요청별 현재 사용자를 정하는 FastAPI 의존성.

- ``CurrentUser``: 로그인한 사용자 (상태는 따지지 않는다). 로그인하지 않았으면 401.
- ``ApprovedUser``: 승인된 사용자. 승인 대기·거절·정지면 403 (``detail.code`` 로 상태를 알려준다).
- ``AdminUser``: 관리자. 아니면 403.

``AUTH_REQUIRED=false`` 면 모든 요청을 로컬 사용자(승인된 관리자) 한 명으로 처리한다.
"""

from typing import Annotated

from fastapi import Cookie, Depends, HTTPException

from app.auth.sessions import SESSION_COOKIE, user_for_token
from app.auth.users import get_or_create_local_user, is_admin
from app.config import get_settings
from app.db import SessionDep
from app.models import User

STATUS_MESSAGES = {
    "pending": "관리자의 승인을 기다리고 있습니다.",
    "rejected": "가입 신청이 거절되었습니다.",
    "suspended": "사용이 정지된 계정입니다.",
}


async def current_user(
    session: SessionDep,
    token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> User:
    """로그인한 사용자.

    Args:
        session: DB 세션.
        token: 세션 쿠키.

    Returns:
        사용자 (상태와 상관없이).

    Raises:
        HTTPException: 로그인하지 않았거나 세션이 만료되면 401.
    """
    if not get_settings().auth_required:
        return await get_or_create_local_user(session)
    user = await user_for_token(session, token)
    if user is None:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


async def approved_user(user: CurrentUser) -> User:
    """승인된 사용자.

    Args:
        user: 로그인한 사용자.

    Returns:
        승인된 사용자.

    Raises:
        HTTPException: 승인 대기·거절·정지면 403. ``detail`` 은 ``{"code": 상태, "message": 문구}``.
    """
    if user.status != "approved":
        raise HTTPException(
            status_code=403,
            detail={"code": user.status, "message": STATUS_MESSAGES.get(user.status, "")},
        )
    return user


ApprovedUser = Annotated[User, Depends(approved_user)]


async def admin_user(user: ApprovedUser) -> User:
    """관리자.

    Args:
        user: 승인된 사용자.

    Returns:
        관리자.

    Raises:
        HTTPException: 관리자가 아니면 403.
    """
    if not is_admin(user, get_settings()):
        raise HTTPException(status_code=403, detail="관리자만 쓸 수 있습니다.")
    return user


AdminUser = Annotated[User, Depends(admin_user)]
