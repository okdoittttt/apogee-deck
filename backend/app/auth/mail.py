"""알림 메일 보내기 (SMTP, STARTTLS).

Gmail 은 계정 비밀번호로는 보낼 수 없고 **앱 비밀번호**가 필요하다 (``SMTP_PASSWORD``).
본문은 일반 텍스트만 쓴다. 신청 사유처럼 사용자가 쓴 글이 들어가므로, HTML 메일로 만들면
메일 안에 링크나 서식을 심을 수 있기 때문이다.
"""

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

import anyio

from app.core.config import Settings

logger = logging.getLogger(__name__)

SMTP_TIMEOUT_SECONDS = 20
APP_NAME = "Apogee Deck"


def build_message(settings: Settings, to: list[str], subject: str, body: str) -> EmailMessage:
    """메일을 만든다.

    Args:
        settings: 앱 설정 (보내는 사람).
        to: 받는 사람 주소.
        subject: 제목. 줄바꿈은 빼서 헤더를 조작할 수 없게 한다.
        body: 일반 텍스트 본문.

    Returns:
        보낼 메일.
    """
    msg = EmailMessage()
    msg["From"] = formataddr((APP_NAME, settings.mail_from or settings.smtp_username))
    msg["To"] = ", ".join(to)
    msg["Subject"] = " ".join(subject.split())
    msg.set_content(body)
    return msg


def _send_sync(settings: Settings, msg: EmailMessage) -> None:
    context = ssl.create_default_context()
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT_SECONDS) as smtp:
        smtp.starttls(context=context)  # 인증서를 확인하는 TLS 로 올린 뒤에 로그인한다
        smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(msg)


async def send_mail(settings: Settings, to: list[str], subject: str, body: str) -> bool:
    """메일을 보낸다. 실패해도 예외를 던지지 않고 기록만 남긴다 (메일 때문에 요청이 실패하지 않게).

    Args:
        settings: 앱 설정.
        to: 받는 사람 주소.
        subject: 제목.
        body: 일반 텍스트 본문.

    Returns:
        보냈으면 ``True``. 메일 설정이 없거나 보내지 못했으면 ``False``.
    """
    if not settings.mail_enabled or not to:
        logger.warning("mail is not configured; skipped %r", subject)
        return False
    msg = build_message(settings, to, subject, body)
    try:
        await anyio.to_thread.run_sync(_send_sync, settings, msg)
    except (OSError, smtplib.SMTPException):
        logger.exception("failed to send mail %r", subject)
        return False
    logger.info("sent mail %r to %d recipient(s)", subject, len(to))
    return True
