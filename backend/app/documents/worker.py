"""문서 파싱을 별도 프로세스에서 돌린다.

OCR 은 CPU 를 오래 쓰고, 네이티브 파서는 멈추거나 비정상 종료할 수 있다. 문서마다 새 프로세스를
띄우고 시간 제한을 넘기면 강제로 끝내서, 백엔드의 이벤트 루프와 프로세스를 지킨다.
프로세스를 문서마다 새로 띄우면 시작 비용(모듈·OCR 모델 로드, 1~2초)이 들지만, 끝날 때 메모리가
모두 돌아오고 멈춘 파서를 확실히 끝낼 수 있다.
"""

import logging
import multiprocessing
import traceback
from collections.abc import Callable
from multiprocessing.connection import Connection
from typing import Any

import anyio

from app.documents.formats import DocumentKind
from app.documents.parser import ParsedDocument, ParseError, parse

logger = logging.getLogger(__name__)

_CTX = multiprocessing.get_context("spawn")  # fork 는 스레드가 있는 프로세스에서 안전하지 않다


class ParseTimeoutError(ParseError):
    """시간 제한 안에 파싱이 끝나지 않았다."""


def _child(
    conn: Connection,
    data: bytes,
    kind: str,
    filename: str,
    max_pages: int,
    structured: bool = False,
) -> None:
    """파싱 프로세스의 본체. 결과를 파이프로 돌려준다."""
    try:
        result = parse(data, DocumentKind(kind), filename, max_pages, structured)
        conn.send(("ok", result.content, result.page_count, result.elements))
    except ParseError as exc:
        conn.send(("error", str(exc)))
    except Exception:  # noqa: BLE001 — 어떤 오류든 부모에게 알려야 "비정상 종료" 와 구분된다
        conn.send(("crash", traceback.format_exc(limit=5)))
    finally:
        conn.close()


async def parse_in_subprocess(
    data: bytes,
    kind: DocumentKind,
    filename: str,
    *,
    max_pages: int,
    timeout: float,
    structured: bool = False,
    target: Callable[..., Any] = _child,
) -> ParsedDocument:
    """새 프로세스에서 문서를 파싱한다.

    Args:
        data: 파일 내용.
        kind: 문서 형식.
        filename: 파일 이름.
        max_pages: 최대 쪽 수.
        timeout: 시간 제한(초). 넘으면 프로세스를 강제로 끝낸다.
        structured: 요소 목록(본문·표·그림)도 만들지 (프로젝트 문서).
        target: 프로세스에서 실행할 함수. 테스트에서만 바꾼다.

    Returns:
        파싱 결과.

    Raises:
        ParseTimeoutError: 시간 제한을 넘었을 때.
        ParseError: 문서를 읽지 못했거나 파싱 프로세스가 비정상 종료했을 때.
    """
    receiver, sender = _CTX.Pipe(duplex=False)
    proc = _CTX.Process(
        target=target,
        args=(sender, data, str(kind), filename, max_pages, structured),
        daemon=True,
        name="document-parser",
    )
    proc.start()
    sender.close()  # 자식만 쓰도록 부모 쪽 끝은 닫는다 (자식이 죽으면 EOF 를 받기 위해)

    def wait() -> tuple | None:
        try:
            return receiver.recv() if receiver.poll(timeout) else None
        except EOFError:
            return ("crash", f"parser exited with code {proc.exitcode}")

    try:
        message = await anyio.to_thread.run_sync(wait, abandon_on_cancel=True)
    finally:
        receiver.close()
        if proc.is_alive():
            proc.kill()
        await anyio.to_thread.run_sync(proc.join, 5)

    if message is None:
        raise ParseTimeoutError(f"처리 시간이 너무 오래 걸려 중단했습니다 ({timeout:.0f}초 초과).")
    status, *rest = message
    if status == "ok":
        elements = rest[2] if len(rest) > 2 else None
        return ParsedDocument(content=rest[0], page_count=rest[1], elements=elements)
    if status == "error":
        raise ParseError(rest[0])
    logger.error("document parser crashed: %s", rest[0])
    raise ParseError(
        "문서를 처리하다가 오류가 났습니다. 손상되었거나 지원하지 않는 구조일 수 있습니다."
    )
