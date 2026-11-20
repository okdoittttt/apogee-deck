"""가입 승인 흐름: 신청 → 관리자 알림 메일(승인·거절 링크) → 승인·거절·정지 → 신청자 안내 메일.

승인·거절 링크는 무작위 토큰이고 DB 에는 해시만 둔다. 링크는 정해진 시간(기본 48시간) 뒤에 만료되고,
한 번 쓰면 같은 사용자의 링크가 모두 무효가 된다.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.mail import APP_NAME, send_mail
from app.auth.sessions import delete_user_sessions
from app.auth.tokens import hash_token, new_token
from app.auth.users import is_admin
from app.core.config import Settings
from app.models import ApprovalToken, User

logger = logging.getLogger(__name__)

APPROVAL_PATH = "/api/auth/approvals/"
MAX_REASON_CHARS = 1000

Decision = Literal["approve", "reject", "suspend", "reactivate"]
STATUS_AFTER: dict[str, str] = {
    "approve": "approved",
    "reject": "rejected",
    "suspend": "suspended",
    "reactivate": "approved",
}
ALLOWED_FROM: dict[str, set[str]] = {
    "approve": {"pending", "rejected"},
    "reject": {"pending"},
    "suspend": {"approved"},
    "reactivate": {"suspended"},
}
"""결정별로 바꿀 수 있는 현재 상태."""


class ApprovalError(Exception):
    """승인 흐름을 진행할 수 없다. 메시지는 사용자에게 보여준다.

    Attributes:
        status_code: 돌려줄 HTTP 상태 코드.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        """오류를 만든다.

        Args:
            status_code: HTTP 상태 코드.
            detail: 사용자에게 보여줄 문구.
        """
        super().__init__(detail)
        self.status_code = status_code


def approval_url(settings: Settings, token: str) -> str:
    """승인·거절 링크 주소.

    Args:
        settings: 앱 설정.
        token: 링크 토큰.

    Returns:
        ``<BACKEND_PUBLIC_URL>/api/auth/approvals/<토큰>``.
    """
    return settings.backend_public_url.rstrip("/") + APPROVAL_PATH + token


async def _issue_tokens(session: AsyncSession, user: User, settings: Settings) -> dict[str, str]:
    """이전 링크를 무효로 하고 승인·거절 링크 토큰을 새로 만든다. commit 은 호출하는 쪽에서 한다."""
    now = datetime.now(UTC)
    await _revoke_tokens(session, user.id, now)
    expires = now + timedelta(hours=settings.approval_link_ttl_hours)
    tokens = {}
    for action in ("approve", "reject"):
        token = new_token()
        session.add(
            ApprovalToken(
                token_hash=hash_token(token), user_id=user.id, action=action, expires_at=expires
            )
        )
        tokens[action] = token
    return tokens


async def _revoke_tokens(session: AsyncSession, user_id: uuid.UUID, now: datetime) -> None:
    await session.execute(
        update(ApprovalToken)
        .where(ApprovalToken.user_id == user_id, ApprovalToken.used_at.is_(None))
        .values(used_at=now)
    )


async def request_access(
    session: AsyncSession, user: User, reason: str, settings: Settings
) -> bool:
    """가입을 신청한다. 신청 사유를 저장하고 관리자에게 승인·거절 링크가 담긴 메일을 보낸다.

    Args:
        session: DB 세션. 이 함수가 commit 한다.
        user: 신청하는 사용자 (승인 대기 상태여야 한다).
        reason: 신청 사유.
        settings: 앱 설정.

    Returns:
        관리자에게 메일을 보냈으면 ``True`` (메일 설정이 없거나 실패하면 ``False``, 신청은 저장된다).

    Raises:
        ApprovalError: 승인 대기 상태가 아니면 409, 너무 자주 신청하면 429.
    """
    if user.status != "pending":
        raise ApprovalError(409, "승인 대기 중인 계정만 신청할 수 있습니다.")
    now = datetime.now(UTC)
    cooldown = timedelta(minutes=settings.access_request_cooldown_minutes)
    if user.requested_at and now - user.requested_at < cooldown:
        minutes = settings.access_request_cooldown_minutes
        raise ApprovalError(429, f"이미 신청했습니다. {minutes}분 뒤에 다시 보낼 수 있습니다.")
    user.request_reason = reason.strip()[:MAX_REASON_CHARS] or None
    user.requested_at = now
    tokens = await _issue_tokens(session, user, settings)
    await session.commit()

    admins = sorted(settings.admin_email_set)
    body = "\n".join(
        [
            "새 가입 신청이 들어왔습니다.",
            "",
            f"이름: {user.name}",
            f"이메일: {user.email}",
            "신청 사유:",
            user.request_reason or "(없음)",
            "",
            f"승인: {approval_url(settings, tokens['approve'])}",
            f"거절: {approval_url(settings, tokens['reject'])}",
            "",
            f"링크는 {settings.approval_link_ttl_hours}시간 동안 한 번만 쓸 수 있습니다.",
            "링크를 열면 확인 화면이 나오고, 버튼을 눌러야 처리됩니다.",
            "이 메일을 다른 사람에게 전달하지 마세요. 링크를 가진 사람은 누구나 승인할 수 있습니다.",
        ]
    )
    return await send_mail(settings, admins, f"[{APP_NAME}] 가입 신청: {user.name}", body)


@dataclass(frozen=True)
class PendingDecision:
    """링크로 확인한 처리 대기 중인 결정.

    Attributes:
        user: 신청한 사용자.
        action: ``approve`` | ``reject``.
        expires_at: 링크 만료 시각.
    """

    user: User
    action: Literal["approve", "reject"]
    expires_at: datetime


async def resolve_token(session: AsyncSession, token: str) -> PendingDecision:
    """링크 토큰을 확인한다 (처리하지는 않는다).

    Args:
        session: DB 세션.
        token: 링크의 토큰.

    Returns:
        처리 대기 중인 결정.

    Raises:
        ApprovalError: 없거나, 이미 썼거나, 만료됐거나, 신청자가 더 이상 승인 대기가 아니면 410.
    """
    row = (
        await session.execute(
            select(ApprovalToken, User)
            .join(User, User.id == ApprovalToken.user_id)
            .where(ApprovalToken.token_hash == hash_token(token))
        )
    ).first()
    gone = ApprovalError(
        410, "만료되었거나 이미 사용한 링크입니다. 필요하면 관리 화면에서 처리해 주세요."
    )
    if row is None:
        raise gone
    record, user = row
    if record.used_at is not None or record.expires_at <= datetime.now(UTC):
        raise gone
    if user.status != "pending":
        raise gone
    return PendingDecision(user=user, action=record.action, expires_at=record.expires_at)


async def decide_by_token(session: AsyncSession, token: str, settings: Settings) -> User:
    """링크로 승인하거나 거절한다. 같은 사용자의 링크는 모두 무효가 된다.

    Args:
        session: DB 세션. 이 함수가 commit 한다.
        token: 링크의 토큰.
        settings: 앱 설정.

    Returns:
        처리한 사용자.

    Raises:
        ApprovalError: 링크가 유효하지 않을 때 (``resolve_token`` 참고).
    """
    pending = await resolve_token(session, token)
    return await decide(session, pending.user, pending.action, settings)


async def decide(
    session: AsyncSession,
    user: User,
    decision: Decision,
    settings: Settings,
    actor: User | None = None,
) -> User:
    """사용자 상태를 바꾸고 신청자에게 안내 메일을 보낸다 (링크·관리 화면 공통).

    거절·정지하면 그 사용자의 세션을 모두 지워서 바로 끊는다. 처리하면 남은 승인·거절 링크는 무효가 된다.

    Args:
        session: DB 세션. 이 함수가 commit 한다.
        user: 대상 사용자.
        decision: ``approve`` | ``reject`` | ``suspend`` | ``reactivate``.
        settings: 앱 설정.
        actor: 처리하는 관리자 (관리 화면). 링크로 처리하면 ``None``.

    Raises:
        ApprovalError: 지금 상태에서 할 수 없는 결정이면 409, 관리자 자신을 거절·정지하려 하면 400.

    Returns:
        처리한 사용자.
    """
    if decision not in ALLOWED_FROM or user.status not in ALLOWED_FROM[decision]:
        raise ApprovalError(409, f"지금 상태({user.status})에서는 할 수 없는 처리입니다.")
    if decision in ("reject", "suspend") and (
        is_admin(user, settings) or (actor and actor.id == user.id)
    ):
        raise ApprovalError(400, "관리자 계정은 거절하거나 정지할 수 없습니다.")
    now = datetime.now(UTC)
    user.status = STATUS_AFTER[decision]
    if decision in ("approve", "reactivate"):
        user.approved_at = now
    else:
        await delete_user_sessions(session, user)
    await _revoke_tokens(session, user.id, now)
    await session.commit()
    logger.info("user %s → %s (%s)", user.id, user.status, "admin" if actor else "link")
    await _notify_user(user, decision, settings)
    return user


async def _notify_user(user: User, decision: Decision, settings: Settings) -> None:
    app_url = settings.frontend_url.rstrip("/") + "/"
    messages = {
        "approve": (
            "가입이 승인되었습니다",
            f"{user.name}님, {APP_NAME} 가입이 승인되었습니다.\n\n지금 사용할 수 있습니다: {app_url}",
        ),
        "reject": (
            "가입 신청 결과",
            f"{user.name}님, 이번 {APP_NAME} 가입 신청은 승인되지 않았습니다.",
        ),
        "reactivate": (
            "계정 사용이 다시 허용되었습니다",
            f"{user.name}님, {APP_NAME} 계정을 다시 사용할 수 있습니다: {app_url}",
        ),
    }
    if decision in messages:  # 정지는 따로 알리지 않는다
        subject, body = messages[decision]
        await send_mail(settings, [user.email], f"[{APP_NAME}] {subject}", body)
