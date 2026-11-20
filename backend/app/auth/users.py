"""사용자 만들기·찾기, 관리자 판별, 로그인 기능 전 데이터 옮기기."""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import Conversation, Document, User

logger = logging.getLogger(__name__)

LOCAL_USER_EMAIL = "local@localhost"
"""인증을 끈 개발 환경의 로컬 사용자 이메일."""


def is_admin(user: User, settings: Settings) -> bool:
    """관리자인지. ``ADMIN_EMAILS`` 에 있거나, 인증을 끈 환경의 로컬 사용자다.

    Args:
        user: 사용자.
        settings: 앱 설정.

    Returns:
        관리자 여부.
    """
    return user.is_local or user.email.lower() in settings.admin_email_set


async def get_or_create_local_user(session: AsyncSession) -> User:
    """인증을 끈 환경에서 모든 요청을 처리할 로컬 사용자. 처음 부를 때 만들고 예전 데이터를 옮긴다.

    Args:
        session: DB 세션. 새로 만들면 commit 한다.

    Returns:
        승인된 로컬 사용자.
    """
    user = await session.scalar(select(User).where(User.is_local.is_(True)))
    if user is None:
        user = User(
            email=LOCAL_USER_EMAIL,
            name="로컬 사용자",
            status="approved",
            is_local=True,
            approved_at=datetime.now(UTC),
        )
        session.add(user)
        await session.flush()
        await claim_legacy_data(session, user)
        await session.commit()
    return user


async def upsert_google_user(
    session: AsyncSession, claims: dict[str, Any], settings: Settings
) -> User:
    """Google 로그인 정보로 사용자를 찾거나 만든다.

    처음 로그인한 사람은 ``pending`` (승인 대기) 이다. 관리자 이메일이면 바로 승인하고, 로그인 기능 전에
    만든 대화·문서와 로컬 사용자의 데이터를 관리자에게 옮긴다.

    Args:
        session: DB 세션. 이 함수가 commit 한다.
        claims: 검증한 Google ID 토큰 내용 (``sub``, ``email``, ``name``, ``picture``).
        settings: 앱 설정.

    Returns:
        사용자.
    """
    now = datetime.now(UTC)
    email = str(claims["email"]).lower()
    user = await session.scalar(select(User).where(User.google_sub == claims["sub"]))
    if user is None:
        # 같은 이메일로 먼저 만들어진 계정이 있으면 이어 쓴다 (관리자가 미리 등록한 경우 등).
        user = await session.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(email=email, status="pending")
        session.add(user)
    user.google_sub = claims["sub"]
    user.email = email
    user.name = str(claims.get("name") or email.split("@")[0])[:200]
    user.picture = (str(claims["picture"])[:1000]) if claims.get("picture") else None
    user.last_login_at = now
    admin = email in settings.admin_email_set
    if admin and user.status != "approved":
        user.status = "approved"
        user.approved_at = now
    await session.flush()
    if admin:
        await claim_legacy_data(session, user)
    await session.commit()
    return user


async def claim_legacy_data(session: AsyncSession, owner: User) -> None:
    """주인이 없는 대화·문서(로그인 기능 전 데이터)와 로컬 사용자의 데이터를 ``owner`` 에게 옮긴다.

    Args:
        session: DB 세션. commit 은 호출하는 쪽에서 한다.
        owner: 데이터를 받을 사용자 (관리자 또는 로컬 사용자).
    """
    local_ids = select(User.id).where(User.is_local.is_(True), User.id != owner.id)
    moved = 0
    for model in (Conversation, Document):
        res = await session.execute(
            update(model)
            .where(or_(model.user_id.is_(None), model.user_id.in_(local_ids)))
            .values(user_id=owner.id)
        )
        moved += res.rowcount or 0
    if moved:
        logger.info("moved %d legacy conversations/documents to %s", moved, owner.email)


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    """ID 로 사용자를 찾는다.

    Args:
        session: DB 세션.
        user_id: 사용자 ID.

    Returns:
        사용자. 없으면 ``None``.
    """
    return await session.get(User, user_id)
