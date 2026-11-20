"""관리자 API: 사용자 목록, 승인·거절·정지·정지 해제."""

import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app.api.deps import AdminUser, SessionDep
from app.api.schemas import UserOut, user_out
from app.auth.approval import ApprovalError, Decision, decide
from app.core.config import get_settings
from app.models import User

router = APIRouter(prefix="/api/admin")

UserStatus = Literal["pending", "approved", "rejected", "suspended"]


class AdminUserOut(UserOut):
    """관리 화면에 보여줄 사용자 정보.

    Attributes:
        created_at: 처음 로그인한 시각 (ISO 8601).
        requested_at: 가입을 신청한 시각.
        last_login_at: 마지막 로그인 시각.
    """

    created_at: str
    requested_at: str | None
    last_login_at: str | None


def _admin_out(user: User) -> AdminUserOut:
    base = user_out(user, get_settings())
    return AdminUserOut(
        **base.model_dump(),
        created_at=user.created_at.isoformat(),
        requested_at=user.requested_at.isoformat() if user.requested_at else None,
        last_login_at=user.last_login_at.isoformat() if user.last_login_at else None,
    )


@router.get("/users")
async def list_users(
    session: SessionDep, admin: AdminUser, status: UserStatus | None = None
) -> list[AdminUserOut]:
    """사용자 목록 (승인 대기를 먼저, 최근 순). 로컬 사용자는 빼고 보여준다.

    Args:
        session: DB 세션.
        admin: 관리자.
        status: 이 상태의 사용자만 보여준다.

    Returns:
        사용자 목록.
    """
    query = select(User).where(User.is_local.is_(False))
    if status:
        query = query.where(User.status == status)
    users = list(await session.scalars(query.order_by(User.created_at.desc())))
    users.sort(key=lambda u: u.status != "pending")  # 안정 정렬: 승인 대기가 먼저
    return [_admin_out(u) for u in users]


@router.post("/users/{user_id}/{decision}")
async def decide_user(
    user_id: uuid.UUID, decision: Decision, session: SessionDep, admin: AdminUser
) -> AdminUserOut:
    """사용자를 승인·거절·정지·정지 해제한다. 신청자에게 안내 메일을 보낸다 (정지는 제외).

    Args:
        user_id: 대상 사용자 ID.
        decision: ``approve`` | ``reject`` | ``suspend`` | ``reactivate``.
        session: DB 세션.
        admin: 처리하는 관리자.

    Returns:
        바뀐 사용자 정보.

    Raises:
        HTTPException: 사용자가 없으면 404, 지금 상태에서 할 수 없는 처리면 409,
            관리자 자신을 거절·정지하려 하면 400.
    """
    user = await session.get(User, user_id)
    if user is None or user.is_local:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        await decide(session, user, decision, get_settings(), actor=admin)
    except ApprovalError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return _admin_out(user)
