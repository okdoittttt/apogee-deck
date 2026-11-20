"""로그인 검증: 토큰, Google 로그인 흐름(state·nonce·PKCE), 사용자 상태, 쿠키, 출처 확인."""

import base64
import hashlib
import uuid
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import HTTPException

from app import main
from app.auth import deps, google
from app.auth.tokens import hash_token, new_token
from app.auth.users import is_admin
from app.config import Settings
from app.main import app
from app.routers import auth as auth_router

SETTINGS = Settings(
    google_oauth_client_id="client-id",
    google_oauth_client_secret="client-secret",
    admin_emails="Owner@Gmail.com, second@example.com",
    frontend_url="http://localhost:3001",
    backend_public_url="http://localhost:8000",
    cors_origins="http://localhost:3001",
)


def user(status="approved", email="friend@gmail.com", is_local=False):
    return SimpleNamespace(
        id=uuid.uuid4(),
        email=email,
        name="친구",
        picture=None,
        status=status,
        is_local=is_local,
        request_reason=None,
    )


# --- 토큰 ---------------------------------------------------------------------------------------


def test_tokens_are_random_and_hashed():
    a, b = new_token(), new_token()
    assert a != b and len(a) >= 40
    assert hash_token(a) == hashlib.sha256(a.encode()).hexdigest() != a


# --- Google 로그인 흐름 -------------------------------------------------------------------------


def test_login_flow_cookie_round_trip():
    flow = google.LoginFlow.new()
    assert google.LoginFlow.from_cookie(flow.to_cookie()) == flow
    assert google.LoginFlow.from_cookie(None) is None
    assert google.LoginFlow.from_cookie("not-base64!!") is None


def test_authorize_url_uses_pkce_state_and_nonce():
    flow = google.LoginFlow.new()
    url = urlparse(google.authorize_url(SETTINGS, flow))
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert url.netloc == "accounts.google.com"
    assert q["client_id"] == "client-id"
    assert q["redirect_uri"] == "http://localhost:8000/api/auth/google/callback"
    assert q["scope"] == "openid email profile" and q["response_type"] == "code"
    assert q["state"] == flow.state and q["nonce"] == flow.nonce
    expected = base64.urlsafe_b64encode(hashlib.sha256(flow.verifier.encode()).digest())
    assert q["code_challenge"] == expected.rstrip(b"=").decode()
    assert q["code_challenge_method"] == "S256"
    assert flow.verifier not in url.query  # verifier 는 보내지 않는다


async def test_exchange_code_rejects_wrong_state():
    flow = google.LoginFlow.new()
    with pytest.raises(google.GoogleLoginError, match="만료"):
        await google.exchange_code(SETTINGS, flow, "code", "other-state")


async def test_exchange_code_posts_verifier_and_verifies_token(monkeypatch):
    flow = google.LoginFlow.new()
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"id_token": "jwt", "access_token": "x"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        google.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )

    async def fake_verify(settings, token, nonce):
        assert (token, nonce) == ("jwt", flow.nonce)
        return {"sub": "1", "email": "a@gmail.com", "email_verified": True}

    monkeypatch.setattr(google, "verify_id_token", fake_verify)
    claims = await google.exchange_code(SETTINGS, flow, "the-code", flow.state)
    assert claims["sub"] == "1"
    assert sent["code_verifier"] == [flow.verifier] and sent["code"] == ["the-code"]
    assert sent["client_secret"] == ["client-secret"]


async def test_exchange_code_handles_google_error(monkeypatch):
    flow = google.LoginFlow.new()
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        google.httpx,
        "AsyncClient",
        lambda **kw: real_client(
            transport=httpx.MockTransport(lambda r: httpx.Response(400, json={})), **kw
        ),
    )
    with pytest.raises(google.GoogleLoginError, match="확인하지 못했습니다"):
        await google.exchange_code(SETTINGS, flow, "code", flow.state)


@pytest.mark.parametrize(
    ("claims", "message"),
    [
        ({"nonce": "other", "email": "a@x.com", "email_verified": True}, "만료"),
        ({"nonce": "n", "email": "a@x.com", "email_verified": False}, "이메일이 확인된"),
        ({"nonce": "n", "email_verified": True}, "이메일이 확인된"),
    ],
)
async def test_verify_id_token_checks_nonce_and_email(monkeypatch, claims, message):
    monkeypatch.setattr(google.id_token, "verify_oauth2_token", lambda *a, **k: claims)
    with pytest.raises(google.GoogleLoginError, match=message):
        await google.verify_id_token(SETTINGS, "jwt", "n")


async def test_verify_id_token_passes_audience_and_rejects_bad_signature(monkeypatch):
    seen = {}

    def fake(token, request, audience, clock_skew_in_seconds):
        seen["audience"] = audience
        raise ValueError("Could not verify token signature.")

    monkeypatch.setattr(google.id_token, "verify_oauth2_token", fake)
    with pytest.raises(google.GoogleLoginError):
        await google.verify_id_token(SETTINGS, "jwt", "n")
    assert seen["audience"] == "client-id"


# --- 사용자 상태 --------------------------------------------------------------------------------


def test_admin_is_decided_by_email_case_insensitively():
    assert is_admin(user(email="owner@gmail.com"), SETTINGS)
    assert not is_admin(user(email="friend@gmail.com"), SETTINGS)
    assert is_admin(user(email="local@localhost", is_local=True), SETTINGS)


@pytest.mark.parametrize("status", ["pending", "rejected", "suspended"])
async def test_only_approved_users_pass(status):
    with pytest.raises(HTTPException) as exc:
        await deps.approved_user(user(status=status))
    assert exc.value.status_code == 403 and exc.value.detail["code"] == status
    approved = user()
    assert await deps.approved_user(approved) is approved


async def test_admin_dependency(monkeypatch):
    monkeypatch.setattr(deps, "get_settings", lambda: SETTINGS)
    with pytest.raises(HTTPException) as exc:
        await deps.admin_user(user(email="friend@gmail.com"))
    assert exc.value.status_code == 403
    await deps.admin_user(user(email="owner@gmail.com"))


async def test_current_user_requires_session_when_auth_on(monkeypatch):
    monkeypatch.setattr(deps, "get_settings", lambda: SETTINGS)

    async def no_user(session, token):
        return None

    monkeypatch.setattr(deps, "user_for_token", no_user)
    with pytest.raises(HTTPException) as exc:
        await deps.current_user(None, None)
    assert exc.value.status_code == 401


# --- 로그인 API (DB 없이: 앱의 시작 처리를 돌리지 않는 ASGI 클라이언트) ------------------------


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth_router, "get_settings", lambda: SETTINGS)
    monkeypatch.setattr(main, "get_settings", lambda: SETTINGS)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://localhost:8000")


async def test_login_redirects_to_google_with_flow_cookie(client):
    res = await client.get("/api/auth/google/login")
    assert res.status_code == 303
    assert res.headers["location"].startswith(google.AUTHORIZE_ENDPOINT)
    cookie = res.headers["set-cookie"]
    assert cookie.startswith(f"{google.FLOW_COOKIE}=")
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/api/auth" in cookie


async def test_login_unavailable_without_client(client, monkeypatch):
    monkeypatch.setattr(auth_router, "get_settings", lambda: Settings())
    res = await client.get("/api/auth/google/login")
    assert res.status_code == 303 and "login_error=" in res.headers["location"]


async def test_callback_errors_go_back_to_app(client):
    res = await client.get("/api/auth/google/callback", params={"error": "access_denied"})
    assert res.status_code == 303
    assert res.headers["location"].startswith("http://localhost:3001/?login_error=")
    res = await client.get("/api/auth/google/callback", params={"code": "c", "state": "s"})
    assert "login_error" in res.headers["location"]  # 쿠키(로그인 흐름)가 없다


async def test_callback_sets_session_cookie(client, monkeypatch):
    flow = google.LoginFlow.new()
    logged_in = user(status="pending")

    async def fake_exchange(settings, f, code, state):
        assert f == flow and (code, state) == ("c", flow.state)
        return {"sub": "1", "email": logged_in.email}

    async def fake_upsert(session, claims, settings):
        return logged_in

    async def fake_create(session, u, ttl):
        return "session-token"

    monkeypatch.setattr(google, "exchange_code", fake_exchange)
    monkeypatch.setattr(auth_router, "upsert_google_user", fake_upsert)
    monkeypatch.setattr(auth_router, "create_session", fake_create)
    client.cookies.set(google.FLOW_COOKIE, flow.to_cookie(), path="/api/auth")

    res = await client.get("/api/auth/google/callback", params={"code": "c", "state": flow.state})
    assert res.status_code == 303 and res.headers["location"] == "http://localhost:3001/"
    cookies = res.headers.get_list("set-cookie")
    session_cookie = next(c for c in cookies if c.startswith("session="))
    assert "session=session-token" in session_cookie
    assert "HttpOnly" in session_cookie and "SameSite=lax" in session_cookie
    assert "Secure" not in session_cookie  # http://localhost 개발 환경
    assert any(c.startswith(f"{google.FLOW_COOKIE}=") and "Max-Age=0" in c for c in cookies)


def test_cookie_secure_follows_https():
    assert Settings(frontend_url="https://chat.example.com").session_cookie_secure
    assert not Settings(frontend_url="http://localhost:3001").session_cookie_secure
    assert Settings(frontend_url="http://x", cookie_secure=True).session_cookie_secure


async def test_unsafe_request_from_other_origin_is_rejected(client):
    res = await client.post("/api/auth/logout", headers={"Origin": "https://evil.example"})
    assert res.status_code == 403
    res = await client.post("/api/auth/logout", headers={"Origin": "http://localhost:3001"})
    assert res.status_code == 204  # 세션 쿠키가 없으면 DB 를 건드리지 않는다
