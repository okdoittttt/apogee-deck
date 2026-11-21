"""테스트용 PDF 를 만든다 (외부 도구 없이, 텍스트 레이어만 있는 쪽)."""


def text_pdf(pages: list[str]) -> bytes:
    """쪽마다 ASCII 텍스트 한 줄이 있는 PDF."""
    objects: list[bytes] = []
    n = len(pages)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    font_id = 3 + 2 * n
    for i, text in enumerate(pages):
        content_id = 4 + 2 * i
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode()
        )
        stream = f"BT /F1 18 Tf 72 700 Td ({text}) Tj ET".encode()
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(out)


def layout_pdf(
    items: list[tuple[float, float, str]] | list[tuple[float, float, str, float]],
    image: tuple[float, float, float, float, int, int] | None = None,
) -> bytes:
    """글자를 정한 위치에 놓은 한 쪽짜리 PDF. ``items`` 순서가 곧 PDF 안에 그리는 순서(읽는 순서)다.

    Args:
        items: ``(x, y, ASCII 텍스트[, 글자 크기])`` 목록. 좌표는 PDF 단위(왼쪽 아래가 0, 0). 크기 기본 11.
        image: ``(x, y, 너비, 높이, 픽셀 너비, 픽셀 높이)`` 회색 그림 하나를 글자 뒤에 그린다.
    """
    ops = " ".join(
        f"BT /F1 {item[3] if len(item) > 3 else 11} Tf 1 0 0 1 {item[0]} {item[1]} Tm ({item[2]}) Tj ET"
        for item in items
    )
    xobject = b""
    extra: list[bytes] = []
    if image is not None:
        x, y, w, h, pw, ph = image
        ops += f" q {w} 0 0 {h} {x} {y} cm /Im1 Do Q"
        pixels = bytes(range(256)) * (pw * ph // 256 + 1)
        extra.append(
            b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceGray "
            b"/BitsPerComponent 8 /Length %d >>\nstream\n"
            % (pw, ph, pw * ph)
            + pixels[: pw * ph]
            + b"\nendstream"
        )
        xobject = b" /XObject << /Im1 6 0 R >>"
    stream = ops.encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >>" + xobject + b" >> >>"
        ),
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        *extra,
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(out)


# 회의록처럼 "칸 이름 | 값" 이 나란한 표 두 개와 그 사이 문단. xberg 기본 추출은 이런 배치를
# 여러 단으로 보고 단별로 읽어서, 첫 회의의 값과 둘째 회의의 값이 섞였다.
MEETING_LAYOUT = [
    (72, 720, "Meeting 1 notes"),
    (72, 700, "Date"),
    (150, 700, "Nov 3 10AM"),
    (330, 700, "Room"),
    (400, 700, "A"),
    (72, 685, "People"),
    (150, 685, "Kim, Lee"),
    (330, 685, "Topic"),
    (400, 685, "Launch date"),
    (72, 665, "Decision: launch moves to Nov 24 because of a refund bug."),
    (72, 640, "Meeting 2 notes"),
    (72, 620, "Date"),
    (150, 620, "Nov 10 10AM"),
    (330, 620, "Room"),
    (400, 620, "B"),
    (72, 605, "People"),
    (150, 605, "Kim, Han"),
    (330, 605, "Topic"),
    (400, 605, "Easy pay"),
    (72, 585, "Decision: easy pay opens separately on Dec 1."),
]
