"""내부 주소 접속 방지(SSRF) 검증."""

import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpcore
import httpx
import pytest

from app.web import safe_http
from app.web.safe_http import (
    BlockedAddressError,
    GuardedBackend,
    GuardedTransport,
    ResponseTooLargeError,
    check_url,
    is_public_ip,
)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "127.1.2.3",
        "0.0.0.0",
        "10.0.0.5",
        "172.16.0.1",
        "192.168.0.121",
        "169.254.169.254",  # 클라우드 메타데이터
        "100.64.0.1",  # CGNAT
        "224.0.0.1",
        "::1",
        "fc00::1",
        "fe80::1%eth0",
        "::ffff:127.0.0.1",  # IPv4 를 담은 IPv6
        "::ffff:10.0.0.1",
        "2002:7f00:0001::",  # 6to4 로 감싼 127.0.0.1
        "not-an-ip",
    ],
)
def test_non_public_addresses(address):
    assert not is_public_ip(address)


@pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])
def test_public_addresses(address):
    assert is_public_ip(address)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "gopher://example.com",
        "http://",
        "http://localhost:8000/health",
        "http://api.localhost/",
        "http://127.0.0.1/",
        "http://[::1]:8000/",
        "http://[::ffff:127.0.0.1]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://0.0.0.0:5432/",
    ],
)
def test_check_url_rejects(url):
    with pytest.raises(BlockedAddressError):
        check_url(url)


def test_check_url_leaves_hostnames_to_connect_time_check():
    check_url("https://example.com/page")  # 호스트 이름은 접속할 때 DNS 로 검사한다


class _RecordingBackend(httpcore.AsyncNetworkBackend):
    """실제로 접속하지 않고 어느 주소로 접속하려 했는지 기록한다."""

    def __init__(self):
        self.connected: list[str] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connected.append(host)
        raise httpcore.ConnectError("recorded")

    async def sleep(self, seconds):
        return None


def _resolver(mapping):
    async def resolve(host, port):
        return mapping[host]

    return resolve


@pytest.mark.parametrize(
    ("host", "addresses"),
    [
        ("postgres", ["172.18.0.3"]),  # compose 서비스 이름
        ("decimal", ["127.0.0.1"]),  # http://2130706433 같은 표기도 DNS 를 거치면 127.0.0.1
        ("mixed", ["93.184.216.34", "10.0.0.5"]),  # 공인 IP 와 사설 IP 를 섞어 돌려주는 DNS
        ("empty", []),
    ],
)
async def test_backend_blocks_hosts_resolving_to_non_public_addresses(host, addresses):
    inner = _RecordingBackend()
    backend = GuardedBackend(_resolver({host: addresses}), inner)
    with pytest.raises(BlockedAddressError):
        await backend.connect_tcp(host, 80)
    assert inner.connected == []  # 접속 시도 자체를 하지 않는다


async def test_backend_connects_to_the_checked_ip_only():
    # 검사한 IP 로 바로 접속하므로, 검사 뒤에 DNS 답이 바뀌어도(DNS rebinding) 영향이 없다
    answers = iter([["93.184.216.34"], ["127.0.0.1"]])

    async def rebinding_resolver(host, port):
        return next(answers)

    inner = _RecordingBackend()
    backend = GuardedBackend(rebinding_resolver, inner)
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("rebind.example", 80)
    assert inner.connected == ["93.184.216.34"]


async def test_backend_rejects_unix_sockets():
    with pytest.raises(BlockedAddressError):
        await GuardedBackend().connect_unix_socket("/var/run/docker.sock")


@pytest.fixture
def local_server():
    """127.0.0.1 에서 실제로 응답하는 HTTP 서버 (내부 서비스 역할)."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"internal secret")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


async def test_real_internal_server_is_unreachable(local_server):
    port = local_server
    # 직접 주소: 요청 전에 거른다
    with pytest.raises(BlockedAddressError):
        await safe_http.fetch(f"http://127.0.0.1:{port}/", timeout=5, max_bytes=1000)
    # 내부 IP 로 풀리는 호스트 이름: 접속하는 순간에 거른다 (실제 연결 계층 사용)
    transport = GuardedTransport(resolver=_resolver({"internal.test": ["127.0.0.1"]}))
    with pytest.raises(BlockedAddressError):
        await safe_http.fetch(
            f"http://internal.test:{port}/", timeout=5, max_bytes=1000, transport=transport
        )
    # 같은 서버에 막지 않고 접속하면 응답한다 (위 실패가 서버 문제가 아님을 확인)
    async with httpx.AsyncClient() as client:
        assert (await client.get(f"http://127.0.0.1:{port}/")).text == "internal secret"


def _mock(routes):
    def handler(request: httpx.Request) -> httpx.Response:
        return routes[str(request.url)]

    return httpx.MockTransport(handler)


async def test_redirect_to_internal_address_is_blocked():
    transport = _mock(
        {
            "https://evil.example/": httpx.Response(
                302, headers={"location": "http://127.0.0.1:5432/"}
            )
        }
    )
    with pytest.raises(BlockedAddressError):
        await safe_http.fetch(
            "https://evil.example/", timeout=5, max_bytes=1000, transport=transport
        )


async def test_relative_redirect_is_followed():
    transport = _mock(
        {
            "https://a.example/old": httpx.Response(301, headers={"location": "/new"}),
            "https://a.example/new": httpx.Response(
                200, headers={"content-type": "text/html; charset=utf-8"}, text="<p>ok</p>"
            ),
        }
    )
    response = await safe_http.fetch(
        "https://a.example/old", timeout=5, max_bytes=1000, transport=transport
    )
    assert response.url == "https://a.example/new"
    assert response.content_type == "text/html" and response.encoding == "utf-8"
    assert response.content == b"<p>ok</p>"


async def test_too_many_redirects():
    transport = _mock(
        {
            "https://loop.example/": httpx.Response(
                302, headers={"location": "https://loop.example/"}
            )
        }
    )
    with pytest.raises(httpx.TooManyRedirects):
        await safe_http.fetch(
            "https://loop.example/", timeout=5, max_bytes=1000, transport=transport
        )


@pytest.mark.parametrize("declare_length", [True, False])
async def test_large_response_is_cut_off(declare_length):
    body = b"x" * 5000
    headers = {"content-length": str(len(body))} if declare_length else {}

    async def chunks():
        yield body[:2500]
        yield body[2500:]

    def handler(request):
        return httpx.Response(200, headers=headers, content=chunks())

    with pytest.raises(ResponseTooLargeError):
        await safe_http.fetch(
            "https://big.example/",
            timeout=5,
            max_bytes=1000,
            transport=httpx.MockTransport(handler),
        )


def test_system_resolver_resolves_localhost():
    import anyio

    addresses = anyio.run(safe_http.system_resolver, "localhost", 80)
    assert addresses and all(not is_public_ip(a) for a in addresses)
    assert socket.AF_INET  # 표준 DNS 를 쓴다
