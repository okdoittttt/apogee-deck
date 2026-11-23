"""Google 로그인 (OAuth 2.0 Authorization Code + PKCE, OpenID Connect).

흐름: ``authorize_url`` 로 Google 로그인 화면에 보낸다 → Google 이 ``code`` 를 붙여 콜백 주소로
돌려보낸다 → ``exchange_code`` 가 ``code`` 를 ID 토큰으로 바꾸고 서명·발급자·대상·만료·nonce 를
검증한다.

``state`` 는 로그인 요청을 시작한 브라우저와 콜백을 받은 브라우저가 같은지(CSRF), ``nonce`` 는 ID
토큰이 이번 요청에 대한 것인지(재사용 방지), PKCE ``code_verifier`` 는 ``code`` 를 가로채도
쓸 수 없게 하는지 확인한다. 세 값은 짧게 사는 쿠키에 담아 둔다.
"""

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import anyio
import httpx
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from app.core.config import Settings

AUTHORIZE_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
CALLBACK_PATH = "/api/auth/google/callback"
FLOW_COOKIE = "oauth_flow"
FLOW_TTL_SECONDS = 600
TOKEN_TIMEOUT_SECONDS = 15.0

_google_request = google_requests.Request()  # Google 공개 키를 캐시한다


class GoogleLoginError(Exception):
    """Google 로그인을 끝내지 못했다. 메시지는 사용자에게 보여준다."""


@dataclass(frozen=True)
class LoginFlow:
    """로그인 한 번의 검증 값. 쿠키에 담아 콜백에서 확인한다.

    Attributes:
        state: CSRF 방지 값. 콜백의 ``state`` 와 같아야 한다.
        nonce: ID 토큰 재사용 방지 값. ID 토큰의 ``nonce`` 와 같아야 한다.
        verifier: PKCE ``code_verifier``.
    """

    state: str
    nonce: str
    verifier: str

    @classmethod
    def new(cls) -> "LoginFlow":
        """무작위 값으로 새 로그인 흐름을 만든다.

        Returns:
            새 로그인 흐름.
        """
        return cls(secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(64))

    def to_cookie(self) -> str:
        """쿠키 값으로 인코딩한다.

        Returns:
            base64url 로 인코딩한 JSON.
        """
        raw = json.dumps({"s": self.state, "n": self.nonce, "v": self.verifier}).encode()
        return base64.urlsafe_b64encode(raw).decode()

    @classmethod
    def from_cookie(cls, value: str | None) -> "LoginFlow | None":
        """쿠키 값을 읽는다.

        Args:
            value: ``to_cookie`` 로 만든 값.

        Returns:
            로그인 흐름. 없거나 형식이 잘못됐으면 ``None``.
        """
        if not value:
            return None
        try:
            data = json.loads(base64.urlsafe_b64decode(value.encode()))
            return cls(str(data["s"]), str(data["n"]), str(data["v"]))
        except (ValueError, KeyError, TypeError):
            return None


def redirect_uri(settings: Settings) -> str:
    """Google OAuth 클라이언트에 등록할 콜백 주소.

    Args:
        settings: 앱 설정.

    Returns:
        ``<BACKEND_PUBLIC_URL>/api/auth/google/callback``.
    """
    return settings.backend_public_url.rstrip("/") + CALLBACK_PATH


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def authorize_url(settings: Settings, flow: LoginFlow) -> str:
    """Google 로그인 화면 주소.

    Args:
        settings: 앱 설정.
        flow: 이번 로그인의 검증 값.

    Returns:
        로그인 화면 URL.
    """
    params = {
        "client_id": settings.google_oauth_client_id,
        "redirect_uri": redirect_uri(settings),
        "response_type": "code",
        "scope": "openid email profile",
        "state": flow.state,
        "nonce": flow.nonce,
        "code_challenge": _code_challenge(flow.verifier),
        "code_challenge_method": "S256",
        "prompt": "select_account",  # 여러 계정이 있으면 고르게 한다
    }
    return f"{AUTHORIZE_ENDPOINT}?{urlencode(params)}"


async def exchange_code(
    settings: Settings, flow: LoginFlow, code: str, state: str
) -> dict[str, Any]:
    """콜백으로 받은 ``code`` 를 ID 토큰으로 바꾸고 검증한다.

    Args:
        settings: 앱 설정.
        flow: 쿠키에 담아 둔 이번 로그인의 검증 값.
        code: Google 이 콜백에 붙여 준 인가 코드.
        state: 콜백의 ``state``.

    Returns:
        검증한 ID 토큰 내용 (``sub``, ``email``, ``name``, ``picture`` 등).

    Raises:
        GoogleLoginError: ``state`` 가 다르거나, 코드 교환·토큰 검증에 실패했거나, 이메일이 확인되지
            않은 계정일 때.
    """
    if not secrets.compare_digest(state, flow.state):
        raise GoogleLoginError(
            "로그인 요청이 만료되었거나 올바르지 않습니다. 다시 로그인해 주세요."
        )
    try:
        async with httpx.AsyncClient(timeout=TOKEN_TIMEOUT_SECONDS) as client:
            res = await client.post(
                TOKEN_ENDPOINT,
                data={
                    "code": code,
                    "client_id": settings.google_oauth_client_id,
                    "client_secret": settings.google_oauth_client_secret,
                    "redirect_uri": redirect_uri(settings),
                    "grant_type": "authorization_code",
                    "code_verifier": flow.verifier,
                },
            )
    except httpx.HTTPError as exc:
        raise GoogleLoginError(
            "Google 에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요."
        ) from exc
    if res.status_code != 200:
        raise GoogleLoginError("Google 로그인을 확인하지 못했습니다. 다시 로그인해 주세요.")
    token = res.json().get("id_token")
    if not token:
        raise GoogleLoginError("Google 로그인을 확인하지 못했습니다. 다시 로그인해 주세요.")
    return await verify_id_token(settings, token, flow.nonce)


async def verify_id_token(settings: Settings, token: str, nonce: str) -> dict[str, Any]:
    """Google ID 토큰의 서명, 발급자, 대상(클라이언트 ID), 만료, nonce, 이메일 확인 여부를 검증한다.

    Args:
        settings: 앱 설정.
        token: ID 토큰 (JWT).
        nonce: 이번 로그인에서 보낸 nonce.

    Returns:
        ID 토큰 내용.

    Raises:
        GoogleLoginError: 검증에 실패했을 때.
    """
    try:
        # Google 공개 키를 받아 오므로(캐시됨) 이벤트 루프를 막지 않게 스레드에서 한다.
        claims = await anyio.to_thread.run_sync(
            lambda: id_token.verify_oauth2_token(
                token, _google_request, settings.google_oauth_client_id, clock_skew_in_seconds=10
            )
        )
    except ValueError as exc:
        raise GoogleLoginError(
            "Google 로그인을 확인하지 못했습니다. 다시 로그인해 주세요."
        ) from exc
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise GoogleLoginError(
            "로그인 요청이 만료되었거나 올바르지 않습니다. 다시 로그인해 주세요."
        )
    if not claims.get("email") or claims.get("email_verified") is not True:
        raise GoogleLoginError("이메일이 확인된 Google 계정으로 로그인해 주세요.")
    return claims
