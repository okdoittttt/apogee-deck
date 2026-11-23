"""가입 승인 흐름 검증: 신청 메일, 링크 토큰, 승인·거절·정지, 확인 화면, 메일, 로그 가리기."""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.api.routers import approvals as approvals_router
from app.auth import approval, mail
from app.auth.tokens import hash_token
from app.core.config import Settings
from app.main import _RedactApprovalTokens
from app.models import ApprovalToken

SETTINGS = Settings(
    admin_emails="owner@gmail.com",
    smtp_username="notify@gmail.com",
    smtp_password="app-password",
    backend_public_url="http://localhost:8000",
    frontend_url="http://localhost:3001",
)


def user(status="pending", email="friend@gmail.com", **kw):
    return SimpleNamespace(
        id=uuid.uuid4(),
        email=email,
        name=kw.pop("name", "친구"),
        status=status,
        is_local=False,
        request_reason=kw.pop("request_reason", None),
        requested_at=kw.pop("requested_at", None),
        approved_at=None,
        **kw,
    )


class FakeSession:
    def __init__(self, first=None):
        self.added, self.executed, self.commits, self._first = [], [], 0, first

    def add(self, obj):
        self.added.append(obj)

    async def execute(self, stmt):
        self.executed.append(str(stmt))
        return SimpleNamespace(first=lambda: self._first, rowcount=1)

    async def commit(self):
        self.commits += 1


@pytest.fixture
def sent(monkeypatch):
    """보낸 메일을 기록한다 (실제로 보내지 않는다)."""
    box = []

    async def fake_send(settings, to, subject, body):
        box.append(SimpleNamespace(to=to, subject=subject, body=body))
        return True

    monkeypatch.setattr(approval, "send_mail", fake_send)
    return box


# --- 메일 ---------------------------------------------------------------------------------------


def test_message_subject_cannot_inject_headers():
    msg = mail.build_message(SETTINGS, ["a@x.com"], "제목\r\nBcc: evil@x.com", "본문")
    assert msg["Subject"] == "제목 Bcc: evil@x.com"
    assert msg["Bcc"] is None
    assert "notify@gmail.com" in msg["From"] and msg.get_content_type() == "text/plain"


async def test_send_mail_skips_without_config_and_survives_errors(monkeypatch):
    assert await mail.send_mail(Settings(), ["a@x.com"], "s", "b") is False

    def boom(settings, msg):
        raise OSError("connection refused")

    monkeypatch.setattr(mail, "_send_sync", boom)
    assert await mail.send_mail(SETTINGS, ["a@x.com"], "s", "b") is False
    monkeypatch.setattr(mail, "_send_sync", lambda settings, msg: None)
    assert await mail.send_mail(SETTINGS, ["a@x.com"], "s", "b") is True


# --- 신청 ---------------------------------------------------------------------------------------


async def test_request_sends_admin_mail_with_links(sent):
    applicant = user()
    session = FakeSession()
    notified = await approval.request_access(session, applicant, "  같이 쓰고 싶어요  ", SETTINGS)

    assert notified and session.commits == 1
    assert applicant.request_reason == "같이 쓰고 싶어요" and applicant.requested_at
    tokens = [o for o in session.added if isinstance(o, ApprovalToken)]
    assert sorted(t.action for t in tokens) == ["approve", "reject"]
    for t in tokens:
        assert len(t.token_hash) == 64  # 해시만 저장한다
        assert timedelta(hours=47) < t.expires_at - datetime.now(UTC) <= timedelta(hours=48)
    assert any(
        "approval_tokens" in q and "used_at" in q for q in session.executed
    )  # 이전 링크 무효

    (m,) = sent
    assert m.to == ["owner@gmail.com"] and "가입 신청: 친구" in m.subject
    assert "같이 쓰고 싶어요" in m.body and "friend@gmail.com" in m.body
    links = [
        line.split(": ", 1)[1]
        for line in m.body.splitlines()
        if line.startswith(("승인:", "거절:"))
    ]
    assert len(links) == 2
    assert all(link.startswith("http://localhost:8000/api/auth/approvals/") for link in links)
    hashes = {hash_token(link.rsplit("/", 1)[1]) for link in links}
    assert hashes == {t.token_hash for t in tokens}  # 링크의 토큰과 저장한 해시가 짝이 맞는다


async def test_request_rules(sent):
    with pytest.raises(approval.ApprovalError) as exc:
        await approval.request_access(FakeSession(), user(status="approved"), "", SETTINGS)
    assert exc.value.status_code == 409
    recent = user(requested_at=datetime.now(UTC) - timedelta(minutes=3))
    with pytest.raises(approval.ApprovalError) as exc:
        await approval.request_access(FakeSession(), recent, "", SETTINGS)
    assert exc.value.status_code == 429
    long_reason = user()
    await approval.request_access(FakeSession(), long_reason, "가" * 2000, SETTINGS)
    assert len(long_reason.request_reason) == approval.MAX_REASON_CHARS
    assert sent == [sent[0]]  # 거절된 신청은 메일을 보내지 않는다


# --- 링크 토큰 ----------------------------------------------------------------------------------


def token_row(status="pending", used=False, expired=False, action="approve"):
    now = datetime.now(UTC)
    record = SimpleNamespace(
        action=action,
        used_at=now if used else None,
        expires_at=now - timedelta(minutes=1) if expired else now + timedelta(hours=1),
    )
    return record, user(status=status)


@pytest.mark.parametrize(
    "row",
    [None, token_row(used=True), token_row(expired=True), token_row(status="approved")],
)
async def test_invalid_links_are_gone(row):
    with pytest.raises(approval.ApprovalError) as exc:
        await approval.resolve_token(FakeSession(first=row), "token")
    assert exc.value.status_code == 410


async def test_valid_link_resolves_without_changing_anything():
    session = FakeSession(first=token_row(action="reject"))
    pending = await approval.resolve_token(session, "token")
    assert pending.action == "reject" and pending.user.status == "pending"
    assert session.commits == 0


# --- 결정 ---------------------------------------------------------------------------------------


async def test_approve_notifies_applicant(sent):
    applicant = user()
    session = FakeSession()
    await approval.decide(session, applicant, "approve", SETTINGS)
    assert applicant.status == "approved" and applicant.approved_at
    assert session.commits == 1
    assert sent[0].to == ["friend@gmail.com"] and "승인" in sent[0].subject
    assert "http://localhost:3001/" in sent[0].body


async def test_reject_and_suspend_end_sessions(sent):
    for start, decision, end in [
        ("pending", "reject", "rejected"),
        ("approved", "suspend", "suspended"),
    ]:
        target = user(status=start)
        session = FakeSession()
        await approval.decide(session, target, decision, SETTINGS)
        assert target.status == end
        assert any("DELETE FROM sessions" in q for q in session.executed)
    # 정지는 메일을 보내지 않는다
    assert [m.subject for m in sent] == ["[okdoitttt] 가입 신청 결과"]


@pytest.mark.parametrize(
    ("start", "decision"),
    [
        ("approved", "approve"),
        ("approved", "reject"),
        ("pending", "suspend"),
        ("approved", "reactivate"),
    ],
)
async def test_invalid_transitions(start, decision, sent):
    with pytest.raises(approval.ApprovalError) as exc:
        await approval.decide(FakeSession(), user(status=start), decision, SETTINGS)
    assert exc.value.status_code == 409


async def test_admin_cannot_be_rejected_or_suspended(sent):
    owner = user(status="approved", email="owner@gmail.com")
    with pytest.raises(approval.ApprovalError) as exc:
        await approval.decide(FakeSession(), owner, "suspend", SETTINGS)
    assert exc.value.status_code == 400


# --- 확인 화면 ----------------------------------------------------------------------------------


async def test_confirm_page_escapes_user_text_and_sets_headers(monkeypatch):
    evil = user(name="<script>alert(1)</script>", request_reason='"><img src=x onerror=alert(1)>')

    async def fake_resolve(session, token):
        return approval.PendingDecision(evil, "approve", datetime.now(UTC))

    monkeypatch.setattr(approvals_router, "resolve_token", fake_resolve)
    res = await approvals_router.confirm_page("token", None)
    html = res.body.decode()
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<img" not in html
    assert '<form method="post">' in html
    assert res.headers["referrer-policy"] == "same-origin"  # no-referrer 면 POST 의 Origin 이 null
    assert "default-src 'none'" in res.headers["content-security-policy"]
    assert res.headers["cache-control"] == "no-store"


async def test_confirm_page_for_used_link(monkeypatch):
    async def gone(session, token):
        raise approval.ApprovalError(410, "만료되었거나 이미 사용한 링크입니다.")

    monkeypatch.setattr(approvals_router, "resolve_token", gone)
    res = await approvals_router.confirm_page("token", None)
    assert res.status_code == 410 and "이미 사용한 링크" in res.body.decode()


async def test_post_decides(monkeypatch):
    decided = user(status="approved")

    async def fake_decide(session, token, settings):
        assert token == "token"
        return decided

    monkeypatch.setattr(approvals_router, "decide_by_token", fake_decide)
    res = await approvals_router.decide_page("token", None)
    assert res.status_code == 200 and "승인했습니다" in res.body.decode()


# --- 로그 ---------------------------------------------------------------------------------------


def test_access_log_hides_approval_tokens():
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4", "GET", "/api/auth/approvals/SECRET-TOKEN", "1.1", 200),
        None,
    )
    _RedactApprovalTokens().filter(record)
    assert "SECRET-TOKEN" not in record.getMessage()
    assert "/api/auth/approvals/***" in record.getMessage()


async def test_confirm_form_post_passes_origin_check(monkeypatch):
    """확인 화면의 폼 POST(같은 출처)는 출처 확인을 통과하고, Origin: null 은 막힌다."""
    import httpx

    from app import main
    from app.main import app

    monkeypatch.setattr(main, "get_settings", lambda: SETTINGS)

    async def fake_decide(session, token, settings):
        return user(status="approved")

    monkeypatch.setattr(approvals_router, "decide_by_token", fake_decide)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
    ) as client:
        ok = await client.post("/api/auth/approvals/t", headers={"Origin": "http://localhost:8000"})
        blocked = await client.post("/api/auth/approvals/t", headers={"Origin": "null"})
    assert ok.status_code == 200 and blocked.status_code == 403
