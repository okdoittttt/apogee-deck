"""Docker 이미지를 만들 때 PaddleOCR 모델을 미리 받는다.

xberg 는 OCR 을 처음 할 때 모델을 Hugging Face 에서 받는다. 이미지에 미리 넣어 두면 첫 업로드가
느려지거나 네트워크가 없어서 실패하는 일이 없다. 빈 이미지를 한 번 OCR 해서 받게 한다.

앱 코드가 바뀔 때마다 모델을 다시 받지 않도록 Dockerfile 에서 앱 코드보다 먼저 실행한다.
그래서 앱 모듈을 불러오지 않고 언어 값을 따로 둔다 (``app.documents.parser.OCR_LANGUAGE`` 와 같아야
하며, 테스트가 확인한다).
"""

import asyncio
import struct
import zlib

import xberg

OCR_LANGUAGE = "korean"


def blank_png(width: int = 64, height: int = 32) -> bytes:
    """흰색 PNG 를 만든다 (이미지 라이브러리 없이)."""

    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body))

    raw = b"".join(b"\x00" + b"\xff" * width * 3 for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


async def main() -> None:
    """빈 이미지를 OCR 해서 모델을 받는다."""
    config = xberg.ExtractionConfig(
        use_cache=False, ocr=xberg.OcrConfig(backend="paddle-ocr", language=[OCR_LANGUAGE])
    )
    source = xberg.ExtractInput(kind="bytes", bytes=blank_png(), filename="blank.png")
    await xberg.extract(source, config)
    print("PaddleOCR models are ready")


if __name__ == "__main__":
    asyncio.run(main())
