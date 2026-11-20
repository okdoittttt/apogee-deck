"""로그인 API: Google 로그인 시작·콜백, 로그아웃, 내 정보."""

import logging
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Cookie, HTTPException, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from app.auth import google
from app.auth.approval import MAX_REASON_CHARS, ApprovalError, request_access
from app.auth.deps import CurrentUser
from app.auth.sessions import SESSION_COOKIE, create_session, delete_session, user_for_token
from app.auth.users import get_or_create_local_user, is_admin, upsert_google_user
from app.config import Settings, get_settings
from app.db import SessionDep
from app.models import User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth")


class UserOut(BaseModel):
    """화면에 보여줄 사용자 정보.

    Attributes:
        id: 사용자 ID.
        email: 이메일.
        name: 표시 이름.
        picture: 프로필 사진 주소.
        status: ``pending`` | ``approved`` | ``rejected`` | ``suspended``.
        is_admin: 관리자인지.
        request_reason: 가입 신청 사유 (적었으면).
    """

    id: str
    email: str
    name: str
    picture: str | None
    status: str
    is_admin: bool
    request_reason: str | None = None


class Me(BaseModel):
    """``GET /api/auth/me`` 응답.

    Attributes:
        auth_required: 로그인을 요구하는 설정인지.
        google_login_enabled: Google 로그인을 쓸 수 있는지 (OAuth 클라이언트 설정).
        user: 로그인한 사용자. 로그인하지 않았으면 ``None``.
    """

    auth_required: bool
    google_login_enabled: bool
    user: UserOut | None


def user_out(user: User, settings: Settings) -> UserOut:
    """사용자를 응답 형식으로 바꾼다.

    Args:
        user: 사용자.
        settings: 앱 설정.

    Returns:
        화면용 사용자 정보.
    """
    return UserOut(
        id=str(user.id),
        email=user.email,
        name=user.name,
        picture=user.picture,
        status=user.status,
        is_admin=is_admin(user, settings),
        request_reason=user.request_reason,
    )


def _set_cookie(response: Response, name: str, value: str, max_age: int, path: str = "/") -> None:
    settings = get_settings()
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        path=path,
        httponly=True,  # 자바스크립트로 읽을 수 없게 (XSS 로 세션을 훔치지 못하게)
        secure=settings.session_cookie_secure,
        samesite="lax",  # 다른 사이트에서 보낸 POST 에는 쿠키가 붙지 않는다 (CSRF)
    )


def _back_to_app(error: str | None = None) -> RedirectResponse:
    """로그인이 끝나면 화면으로 돌려보낸다. 실패하면 사유를 쿼리로 붙인다."""
    url = get_settings().frontend_url.rstrip("/") + "/"
    if error:
        url += "?" + urlencode({"login_error": error})
    return RedirectResponse(url, status_code=303)


@router.get("/me")
async def me(
    session: SessionDep, token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None
) -> Me:
    """로그인 상태와 내 정보. 로그인하지 않아도 200 으로 ``user: null`` 을 돌려준다.

    Args:
        session: DB 세션.
        token: 세션 쿠키.

    Returns:
        로그인 설정과 사용자 정보.
    """
    settings = get_settings()
    if not settings.auth_required:
        user: User | None = await get_or_create_local_user(session)
    else:
        user = await user_for_token(session, token)
    return Me(
        auth_required=settings.auth_required,
        google_login_enabled=settings.google_login_enabled,
        user=user_out(user, settings) if user else None,
    )


@router.get("/google/login")
async def google_login() -> RedirectResponse:
    """Google 로그인 화면으로 보낸다. 검증 값(state, nonce, PKCE)은 짧게 사는 쿠키에 담는다.

    브라우저가 직접 여는 주소라서, 설정이 없으면 오류 JSON 대신 화면으로 돌려보내고 사유를 보여준다.

    Returns:
        Google 로그인 화면으로의 리디렉션. 설정이 없으면 화면으로의 리디렉션.
    """
    settings = get_settings()
    if not settings.google_login_enabled:
        return _back_to_app("Google 로그인이 설정되지 않았습니다. 관리자에게 문의해 주세요.")
    flow = google.LoginFlow.new()
    response = RedirectResponse(google.authorize_url(settings, flow), status_code=303)
    _set_cookie(
        response, google.FLOW_COOKIE, flow.to_cookie(), google.FLOW_TTL_SECONDS, "/api/auth"
    )
    return response


@router.get("/google/callback")
async def google_callback(
    session: SessionDep,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    flow_cookie: Annotated[str | None, Cookie(alias=google.FLOW_COOKIE)] = None,
) -> RedirectResponse:
    """Google 로그인 콜백. 사용자를 찾거나 만들고 세션 쿠키를 붙여 화면으로 돌려보낸다.

    처음 로그인한 사람은 승인 대기 상태가 된다 (관리자 이메일은 바로 승인).
    실패하면 화면 주소에 ``login_error`` 를 붙여 돌려보낸다.

    Args:
        session: DB 세션.
        code: 인가 코드.
        state: 로그인 요청 때 보낸 state.
        error: 사용자가 취소했거나 Google 이 거절한 사유.
        flow_cookie: 로그인 요청 때 담아 둔 검증 값 쿠키.

    Returns:
        화면으로의 리디렉션.
    """
    settings = get_settings()
    flow = google.LoginFlow.from_cookie(flow_cookie)
    if error:
        return _back_to_app("Google 로그인을 취소했습니다.")
    if not settings.google_login_enabled or not code or not state or flow is None:
        return _back_to_app("로그인 요청이 만료되었거나 올바르지 않습니다. 다시 로그인해 주세요.")
    try:
        claims = await google.exchange_code(settings, flow, code, state)
    except google.GoogleLoginError as exc:
        logger.info("google login failed: %s", exc)
        return _back_to_app(str(exc))
    user = await upsert_google_user(session, claims, settings)
    token = await create_session(session, user, settings.session_ttl_days)
    logger.info("user %s logged in (status=%s)", user.email, user.status)
    response = _back_to_app()
    _set_cookie(response, SESSION_COOKIE, token, settings.session_ttl_days * 24 * 60 * 60)
    response.delete_cookie(google.FLOW_COOKIE, path="/api/auth")
    return response


class AccessRequest(BaseModel):
    """가입 신청.

    Attributes:
        reason: 신청 사유 (관리자에게 메일로 보낸다).
    """

    reason: str = Field(default="", max_length=MAX_REASON_CHARS)


class AccessRequestResult(BaseModel):
    """가입 신청 결과.

    Attributes:
        notified: 관리자에게 알림 메일을 보냈는지 (메일 설정이 없거나 실패하면 ``False``).
    """

    notified: bool


@router.post("/request", status_code=202)
async def request_approval(
    body: AccessRequest, session: SessionDep, user: CurrentUser
) -> AccessRequestResult:
    """가입을 신청한다. 관리자에게 승인·거절 링크가 담긴 메일을 보낸다.

    Args:
        body: 신청 사유.
        session: DB 세션.
        user: 로그인한 사용자 (승인 대기 상태).

    Returns:
        관리자에게 메일을 보냈는지.

    Raises:
        HTTPException: 승인 대기 상태가 아니면 409, 너무 자주 신청하면 429.
    """
    try:
        notified = await request_access(session, user, body.reason, get_settings())
    except ApprovalError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return AccessRequestResult(notified=notified)


@router.post("/logout", status_code=204)
async def logout(
    session: SessionDep, token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None
) -> Response:
    """로그아웃한다 (세션을 지우고 쿠키를 지운다).

    Args:
        session: DB 세션.
        token: 세션 쿠키.

    Returns:
        204 응답.
    """
    await delete_session(session, token)
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
