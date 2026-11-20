"""세션·승인 링크에 쓰는 무작위 토큰. 쿠키·링크에는 토큰을, DB 에는 해시만 둔다."""

import hashlib
import secrets


def new_token() -> str:
    """추측할 수 없는 무작위 토큰 (256비트, URL 에 넣을 수 있는 문자).

    Returns:
        토큰 문자열.
    """
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """DB 에 저장할 토큰 해시.

    토큰 자체가 충분히 길고 무작위라서 느린 해시(bcrypt 등)는 필요 없다.

    Args:
        token: 토큰.

    Returns:
        SHA-256 16진수.
    """
    return hashlib.sha256(token.encode()).hexdigest()
