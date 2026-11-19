"""내부 주소에 접속하지 못하는 HTTP 클라이언트 (SSRF 방지).

모델이 정한 URL 에 백엔드가 직접 접속하므로, 막지 않으면 같은 Docker 네트워크의 postgres,
Langfuse 나 클라우드 메타데이터 주소(169.254.169.254)를 읽을 수 있다. 검색 결과 페이지에 숨긴
지시로 모델을 속여 내부 주소를 읽게 하는 공격도 가능하다. 그래서 다음을 지킨다.

- **접속하는 순간에 IP 를 검사한다.** httpcore 의 연결 계층(network backend)을 감싸서, 호스트를
  DNS 로 푼 뒤 모든 IP 가 공인 주소일 때만 그 IP 로 바로 접속한다. 검사한 IP 와 접속한 IP 가 같아서
  DNS rebinding 이 통하지 않는다. TLS 인증서는 원래 호스트 이름으로 확인한다.
- **리다이렉트는 직접 따라간다.** 단계마다 URL 을 다시 검사하고(IP 는 접속할 때 다시 검사된다),
  횟수를 제한한다.
- 환경 변수의 프록시를 쓰지 않는다 (프록시를 거치면 IP 검사를 우회한다).
- 받는 크기를 제한한다 (스트리밍으로 받다가 넘으면 중단).
"""

import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import anyio
import httpcore
import httpx

Resolver = Callable[[str, int], Awaitable[list[str]]]
"""(호스트, 포트) → IP 주소 목록."""

MAX_REDIRECTS = 5

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class BlockedAddressError(Exception):
    """접속이 허용되지 않는 주소 (내부망, 루프백, 메타데이터 주소 등, 또는 http(s) 가 아닌 URL)."""


class ResponseTooLargeError(Exception):
    """응답이 크기 제한을 넘었다."""


def is_public_ip(address: str) -> bool:
    """인터넷에서 접속할 수 있는 공인 IP 인지 확인한다.

    루프백, 사설망, link-local(메타데이터 주소 포함), CGNAT, 멀티캐스트, 예약 대역,
    ``0.0.0.0`` 등은 모두 공인이 아니다. IPv4 를 담은 IPv6(``::ffff:127.0.0.1``)는 안의 IPv4 로 판단한다.

    Args:
        address: IP 주소 문자열.

    Returns:
        공인 IP 면 ``True``. 해석할 수 없는 값이면 ``False``.
    """
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])  # IPv6 zone id 제거
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped or ip.sixtofour
        if mapped is not None:
            ip = mapped
    return ip.is_global and not ip.is_multicast


def check_url(url: str) -> None:
    """요청 전에 URL 을 검사한다: ``http``/``https`` 이고, 호스트가 분명한 내부 주소가 아니어야 한다.

    호스트 이름은 접속할 때 ``GuardedBackend`` 가 DNS 로 풀어서 다시 검사한다. 여기서는 IP 리터럴과
    ``localhost`` 처럼 DNS 없이도 내부 주소임을 알 수 있는 경우를 먼저 거른다.

    Args:
        url: 검사할 URL.

    Raises:
        BlockedAddressError: 허용되지 않는 URL 일 때.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedAddressError(f"scheme not allowed: {parts.scheme or '(none)'}")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise BlockedAddressError("missing host")
    if host == "localhost" or host.endswith(".localhost"):
        raise BlockedAddressError("localhost is not allowed")
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return  # 호스트 이름: 접속할 때 검사한다
    if not is_public_ip(host):
        raise BlockedAddressError("non-public address")


async def system_resolver(host: str, port: int) -> list[str]:
    """운영체제 DNS 로 호스트의 IP 주소를 모두 찾는다.

    Args:
        host: 호스트 이름 또는 IP.
        port: 포트.

    Returns:
        IP 주소 목록 (중복 제거, 순서 유지).
    """
    infos = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


class GuardedBackend(httpcore.AsyncNetworkBackend):
    """접속하기 직전에 IP 를 검사하고, 검사한 IP 로만 접속하는 httpcore 연결 계층.

    Attributes:
        resolver: 호스트 → IP 목록 함수 (테스트에서 바꿔 끼운다).
        inner: 실제 접속을 맡는 연결 계층.
    """

    def __init__(
        self,
        resolver: Resolver = system_resolver,
        inner: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        """연결 계층을 만든다.

        Args:
            resolver: 호스트 → IP 목록 함수.
            inner: 실제 접속을 맡는 연결 계층. 없으면 httpcore 기본값.
        """
        self.resolver = resolver
        self.inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        """호스트의 IP 를 모두 검사한 뒤, 공인 IP 로만 접속한다.

        하나라도 공인 IP 가 아니면 접속하지 않는다 (공인 IP 와 사설 IP 를 섞어 돌려주는 DNS 대비).

        Args:
            host: 접속할 호스트.
            port: 포트.
            timeout: 접속 타임아웃.
            local_address: 로컬 주소.
            socket_options: 소켓 옵션.

        Returns:
            연결된 스트림.

        Raises:
            BlockedAddressError: 공인 IP 가 아닌 주소가 섞여 있거나 IP 를 찾지 못했을 때.
        """
        with anyio.fail_after(timeout):
            addresses = await self.resolver(host, port)
        if not addresses:
            raise BlockedAddressError("host did not resolve")
        if not all(is_public_ip(a) for a in addresses):
            raise BlockedAddressError("host resolves to a non-public address")
        last_error: Exception | None = None
        for address in addresses:
            try:
                return await self.inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except httpcore.ConnectError as e:
                last_error = e
        assert last_error is not None
        raise last_error

    async def connect_unix_socket(
        self, *args: object, **kwargs: object
    ) -> httpcore.AsyncNetworkStream:
        """유닉스 소켓 접속은 허용하지 않는다.

        Raises:
            BlockedAddressError: 항상.
        """
        raise BlockedAddressError("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        """재시도 사이에 기다린다.

        Args:
            seconds: 기다릴 시간(초).
        """
        await self.inner.sleep(seconds)


class GuardedTransport(httpx.AsyncHTTPTransport):
    """``GuardedBackend`` 로 접속하는 httpx transport (프록시 환경 변수는 무시)."""

    def __init__(self, resolver: Resolver = system_resolver) -> None:
        """Transport 를 만든다.

        Args:
            resolver: 호스트 → IP 목록 함수.
        """
        super().__init__(trust_env=False)
        # httpx 는 연결 계층을 바꾸는 공개 옵션이 없어서, 같은 설정의 연결 풀을 직접 만든다.
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(trust_env=False),
            network_backend=GuardedBackend(resolver),
        )


@dataclass(frozen=True)
class FetchedResponse:
    """받은 응답.

    Attributes:
        url: 리다이렉트를 따라간 뒤의 최종 URL.
        status_code: HTTP 상태 코드.
        content_type: ``Content-Type`` 의 형식 부분 (예: ``"text/html"``).
        content: 본문 바이트.
        encoding: 응답이 알려준 문자 인코딩. 없으면 ``None``.
    """

    url: str
    status_code: int
    content_type: str
    content: bytes
    encoding: str | None


async def fetch(
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    headers: dict[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FetchedResponse:
    """내부 주소에 접속하지 않도록 검사하면서 URL 을 GET 으로 받는다.

    Args:
        url: 받을 URL.
        timeout: 요청 하나의 타임아웃(초).
        max_bytes: 최대 응답 크기. 넘으면 받는 도중에 중단한다.
        headers: 요청 헤더.
        transport: 테스트용 transport. 없으면 ``GuardedTransport``.

    Returns:
        최종 응답.

    Raises:
        BlockedAddressError: URL 이나 리다이렉트 대상이 허용되지 않는 주소일 때.
        ResponseTooLargeError: 응답이 ``max_bytes`` 를 넘을 때.
        httpx.HTTPError: 네트워크 오류, 타임아웃, 리다이렉트가 너무 많을 때.
    """
    async with httpx.AsyncClient(
        transport=transport or GuardedTransport(),
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
        headers=headers,
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            check_url(url)
            async with client.stream("GET", url) as response:
                if response.status_code in _REDIRECT_STATUSES and "location" in response.headers:
                    url = urljoin(url, response.headers["location"])
                    continue
                content = await _read_limited(response, max_bytes)
                return FetchedResponse(
                    url=str(response.url),
                    status_code=response.status_code,
                    content_type=response.headers.get("content-type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower(),
                    content=content,
                    encoding=response.charset_encoding,
                )
        raise httpx.TooManyRedirects(f"more than {MAX_REDIRECTS} redirects")


async def _read_limited(response: httpx.Response, max_bytes: int) -> bytes:
    """응답 본문을 ``max_bytes`` 까지만 읽는다."""
    declared = response.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise ResponseTooLargeError(f"declared size {declared} exceeds {max_bytes}")
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > max_bytes:
            raise ResponseTooLargeError(f"response exceeds {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)
