"""로그인 세션: 만들기, 쿠키 토큰으로 찾기, 지우기."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.tokens import hash_token, new_token
from app.models import User, UserSession

SESSION_COOKIE = "session"
TOUCH_INTERVAL = timedelta(minutes=5)
"""마지막 사용 시각은 이 간격보다 오래됐을 때만 갱신한다 (요청마다 쓰지 않도록)."""


async def create_session(session: AsyncSession, user: User, ttl_days: int) -> str:
    """세션을 만들고 쿠키에 넣을 토큰을 돌려준다.

    Args:
        session: DB 세션. 이 함수가 commit 한다.
        user: 로그인한 사용자.
        ttl_days: 유지 기간(일).

    Returns:
        세션 토큰 (DB 에는 해시만 저장한다).
    """
    token = new_token()
    now = datetime.now(UTC)
    session.add(
        UserSession(
            token_hash=hash_token(token),
            user_id=user.id,
            expires_at=now + timedelta(days=ttl_days),
            last_seen_at=now,
        )
    )
    await session.commit()
    return token


async def user_for_token(session: AsyncSession, token: str | None) -> User | None:
    """쿠키의 세션 토큰으로 사용자를 찾는다. 만료된 세션은 지운다.

    Args:
        session: DB 세션.
        token: 쿠키의 세션 토큰.

    Returns:
        사용자. 토큰이 없거나 잘못됐거나 만료됐으면 ``None``.
    """
    if not token:
        return None
    now = datetime.now(UTC)
    row = (
        await session.execute(
            select(UserSession, User)
            .join(User, User.id == UserSession.user_id)
            .where(UserSession.token_hash == hash_token(token))
        )
    ).first()
    if row is None:
        return None
    user_session, user = row
    if user_session.expires_at <= now:
        await session.delete(user_session)
        await session.commit()
        return None
    if now - user_session.last_seen_at > TOUCH_INTERVAL:
        await session.execute(
            update(UserSession)
            .where(UserSession.token_hash == user_session.token_hash)
            .values(last_seen_at=now)
        )
        await session.commit()
    return user


async def delete_session(session: AsyncSession, token: str | None) -> None:
    """세션 하나를 지운다 (로그아웃).

    Args:
        session: DB 세션. 이 함수가 commit 한다.
        token: 쿠키의 세션 토큰.
    """
    if token:
        await session.execute(
            delete(UserSession).where(UserSession.token_hash == hash_token(token))
        )
        await session.commit()


async def delete_user_sessions(session: AsyncSession, user: User) -> None:
    """사용자의 모든 세션을 지운다 (정지·거절할 때 바로 끊는다). commit 은 호출하는 쪽에서 한다.

    Args:
        session: DB 세션.
        user: 사용자.
    """
    await session.execute(delete(UserSession).where(UserSession.user_id == user.id))
