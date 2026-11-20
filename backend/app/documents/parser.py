"""문서를 텍스트(Markdown)로 바꾼다.

xberg(Kreuzberg 후속)로 파싱하고, OCR 은 PaddleOCR 한국어 모델을 쓴다. 이 모듈은 백엔드와 다른
프로세스에서 실행된다 (``app.documents.worker``). 파서가 멈추거나 죽어도 백엔드에 영향이 없게 하려는
것이라, 여기서는 DB 나 앱 설정을 쓰지 않는다.

형식별 처리:

- PDF: 텍스트만 있는 쪽은 텍스트 레이어를 그대로 쓰고, 글자가 없는 쪽(스캔본)은 쪽 전체 OCR 결과를 쓴다.
  텍스트와 큰 그림이 섞인 쪽은 쪽 전체를 OCR 한 뒤(``force_ocr_pages``) 텍스트 레이어와 합친다
  (``app.documents.ocr_merge``): 줄 순서와 그림 속 글자는 OCR 에서, 글자와 띄어쓰기는 텍스트 레이어에서
  가져온다. xberg 기본 동작은 섞인 쪽의 그림 글자를 문서 맨 끝에 붙여서 위치가 틀어지고, OCR 결과만 쓰면
  본문 띄어쓰기가 깨지기 때문이다. 출력은 plain 텍스트다. Markdown 출력은 OCR 결과의 띄어쓰기를 지우고
  줄을 제목(``#``)으로 바꿔서 한국어 OCR 품질이 크게 떨어졌다.
- DOCX, XLSX, PPTX, HWP, HWPX: 문서 안 그림을 OCR 해서 제자리에 넣는다(``ocr_embedded_images``).
  출력은 Markdown 이다 (표가 Markdown 표로 남는다).
- HWPML(XML 로 된 .hwp): xberg 는 일반 XML 로 처리해서 태그 이름이 섞이므로 직접 본문만 뽑는다.
- TXT, MD, CSV: 그대로 쓴다.

``structured=True`` (프로젝트 문서)면 본문과 함께 요소 목록(본문·표·그림, ``app.documents.elements``)도
돌려준다. 그림 원본은 이때만 받아 온다 (WebP, 긴 변 ``IMAGE_MAX_SIDE`` 이하).
"""

import asyncio
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from typing import Any

from app.documents import elements as el
from app.documents.formats import DocumentKind
from app.documents.ocr_merge import merge_page, ocr_only_lines

OCR_LANGUAGE = "korean"
"""PaddleOCR 언어. 한국어 모델은 영문·숫자도 읽는다."""

FULL_PAGE_OCR_MIN_SIDE = 80
FULL_PAGE_OCR_MIN_AREA = 50_000
"""PDF 쪽 전체를 OCR 할 그림 크기 기준(픽셀). 이보다 작은 아이콘·로고만 있는 쪽은 텍스트 레이어를 쓴다."""

MIN_TEXT_LAYER_CHARS = 20
"""OCR 한 쪽에서 텍스트 레이어와 합칠 최소 글자 수(공백 제외). 이보다 적으면 스캔본으로 보고 OCR 결과만 쓴다."""

MIN_LAYER_COVERAGE = 0.8
"""합친 결과가 텍스트 레이어 글자의 이 비율보다 적게 담으면 OCR 이 본문을 못 읽은 것으로 본다.
(시험: 정상 슬라이드 PDF 0.92 이상, 한국어 글꼴을 OCR 하지 못한 PDF 0.40~0.53)"""

_MARKDOWN_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!~<>])")
"""xberg 가 Markdown 출력에 넣는 이스케이프 (``4\\.`` → ``4.``). 표 칸 안의 ``\\|`` 는 표가 깨지므로 남긴다."""

IMAGE_MAX_SIDE = 1600
"""구조화 적재 때 받아 오는 그림의 긴 변 상한(픽셀). vision 모델은 낮은 해상도로 보므로 충분하다."""

IMAGE_MIME_TYPES = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "webp": "image/webp",
}

PAGE_COUNT_KINDS = {DocumentKind.PDF, DocumentKind.PPTX}
"""쪽(슬라이드) 수를 기록하는 형식. 나머지는 쪽 구분이 없거나(DOCX, 한글) 의미가 다르다(XLSX 시트)."""


class ParseError(Exception):
    """문서를 읽지 못했다. 메시지는 사용자에게 그대로 보여준다."""


@dataclass(frozen=True)
class ParsedDocument:
    """파싱 결과.

    Attributes:
        content: 본문 (Markdown). PDF 는 ``<!-- PAGE n -->`` 쪽 표시가 들어간다.
        page_count: 쪽 수. 쪽 구분이 없는 형식은 ``None``.
        elements: 요소 목록 (``structured=True`` 로 파싱했을 때만).
    """

    content: str
    page_count: int | None
    elements: list[el.Element] | None = None


def parse(
    data: bytes, kind: DocumentKind, filename: str, max_pages: int, structured: bool = False
) -> ParsedDocument:
    """문서를 파싱한다.

    Args:
        data: 파일 내용.
        kind: 내용으로 확인한 형식 (``app.documents.formats.detect``).
        filename: 파일 이름. 파서가 형식을 고를 때 참고한다.
        max_pages: 최대 쪽 수. 넘으면 OCR 전에 멈춘다.
        structured: 요소 목록(본문·표·그림)도 만들지. 본문은 같다.

    Returns:
        본문과 쪽 수 (``structured`` 면 요소 목록도).

    Raises:
        ParseError: 읽을 수 없는 문서이거나, 쪽 수가 너무 많거나, 글자를 찾지 못했을 때.
    """
    kind = DocumentKind(kind)
    if kind in (DocumentKind.TXT, DocumentKind.MD, DocumentKind.CSV):
        result = ParsedDocument(data.decode("utf-8-sig").replace("\r\n", "\n"), None)
    elif kind is DocumentKind.HWPML:
        result = ParsedDocument(hwpml_to_text(data), None)
    else:
        result = asyncio.run(_parse_with_xberg(data, kind, filename, max_pages, structured))
    if not result.content.strip():
        raise ParseError("문서에서 글자를 찾지 못했습니다.")
    if structured and not result.elements:
        # 구조를 얻지 못한 형식(텍스트 등)은 본문 전체를 요소 하나로 둔다.
        result = replace(result, elements=[el.Element("text", result.content)])
    return result


# --- HWPML ----------------------------------------------------------------------------------------

_DOCTYPE = re.compile(r"<!DOCTYPE\b(?:[^\[>]|\[[^\]]*\])*>", re.IGNORECASE)


def hwpml_to_text(data: bytes) -> str:
    """XML 로 된 한글 문서(HWPML)에서 본문 단락의 글자만 뽑는다.

    DOCTYPE 을 지운 뒤 파싱해서 엔티티 정의가 동작하지 않게 한다 (외부 엔티티·엔티티 폭탄 방지).
    HWPML 이 쓰는 ``&nbsp;`` 만 문자로 바꿔 둔다.

    Args:
        data: HWPML 파일 내용 (UTF-8).

    Returns:
        단락을 빈 줄로 나눈 본문. 표 안의 단락도 문서 순서대로 들어간다.

    Raises:
        ParseError: XML 이 깨졌거나 본문(BODY)이 없을 때.
    """
    text = data.decode("utf-8-sig", errors="replace")
    text = _DOCTYPE.sub("", text, count=1).replace("&nbsp;", "&#160;")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ParseError("손상된 한글 문서입니다.") from exc
    body = root.find("BODY")
    if body is None:
        raise ParseError("한글 문서에서 본문을 찾지 못했습니다.")
    paragraphs = []
    for p in body.iter("P"):
        line = "".join("".join(char.itertext()) for char in p.findall("TEXT/CHAR"))
        line = line.replace("\xa0", " ").strip()
        if line:
            paragraphs.append(line)
    return "\n\n".join(paragraphs)


# --- xberg ---------------------------------------------------------------------------------------


def _image_data(img: Any) -> el.ImageData | None:
    """Xberg 그림을 요소용 원본으로 바꾼다. vision 모델이 받지 않는 형식이면 ``None``."""
    mime = IMAGE_MIME_TYPES.get(str(img.format or "").lower())
    if mime is None or not img.data:
        return None
    ocr = getattr(img.ocr_result, "content", None) if img.ocr_result else None
    return el.ImageData(bytes(img.data), mime, img.width or 0, img.height or 0, (ocr or "").strip())


def _image_kind(img: Any) -> str | None:
    return str(img.image_kind).lower() if img.image_kind is not None else None


def _structured_images(structured: bool) -> dict[str, Any]:
    """구조화 적재 때 그림 원본을 받아 오는 설정 (분류, WebP, 크기 상한)."""
    if not structured:
        return {}
    return {"classify": True, "output_format": "webp", "max_image_dimension": IMAGE_MAX_SIDE}


async def _parse_with_xberg(
    data: bytes, kind: DocumentKind, filename: str, max_pages: int, structured: bool = False
) -> ParsedDocument:
    import xberg  # 무거운 네이티브 모듈이라 파싱 프로세스에서만 불러온다

    def config(**kwargs: object) -> "xberg.ExtractionConfig":
        return xberg.ExtractionConfig(
            use_cache=False,  # 같은 파일을 다시 파싱할 때 설정을 바꿔도 이전 결과가 나오는 것을 막는다
            security_limits=xberg.SecurityLimits(max_pages=max_pages),
            escape_markdown=False,
            **kwargs,
        )

    async def run(cfg: "xberg.ExtractionConfig") -> "xberg.ExtractedDocument":
        source = xberg.ExtractInput(kind="bytes", bytes=data, filename=filename)
        try:
            output = await xberg.extract(source, cfg)
        except Exception as exc:
            raise ParseError(_describe_xberg_error(exc)) from exc
        if not output.results:
            raise ParseError(_describe_xberg_error(output.errors))
        return output.results[0]

    ocr = xberg.OcrConfig(backend="paddle-ocr", language=[OCR_LANGUAGE])

    if kind is not DocumentKind.PDF:
        doc = await run(
            config(
                output_format="markdown",
                ocr=ocr,
                ocr_embedded_images=True,
                # 그림 자리에 그림 링크 없이 OCR 글자만 넣는다
                images=xberg.ImageExtractionConfig(
                    extract_images=True, ocr_text_only=True, **_structured_images(structured)
                ),
                pages=xberg.PageConfig(extract_pages=True),
                include_document_structure=structured,
            )
        )
        pages = len(doc.pages or [])
        content = _MARKDOWN_ESCAPE.sub(r"\1", doc.content or "")
        result = ParsedDocument(content, pages if kind in PAGE_COUNT_KINDS else None)
        if structured:
            result = replace(result, elements=_office_elements(doc))
        return result

    # 1차: OCR 없이 쪽별 텍스트 레이어와 큰 그림이 있는 쪽을 찾는다 (빠르다).
    scan = await run(
        config(
            output_format="plain",
            disable_ocr=True,
            images=xberg.ImageExtractionConfig(
                extract_images=True,
                run_ocr_on_images=False,
                inject_placeholders=False,
                **_structured_images(structured),
            ),
            pages=xberg.PageConfig(extract_pages=True),
        )
    )
    text_pages = {p.page_number: p.content or "" for p in scan.pages or []}
    page_count = len(text_pages)
    if page_count > max_pages:
        raise ParseError(f"쪽 수가 너무 많습니다 ({page_count}쪽, 최대 {max_pages}쪽).")
    ocr_pages = {
        img.page_number
        for img in scan.images or []
        if img.page_number
        and min(img.width or 0, img.height or 0) >= FULL_PAGE_OCR_MIN_SIDE
        and (img.width or 0) * (img.height or 0) >= FULL_PAGE_OCR_MIN_AREA
    }
    ocr_pages |= {n for n, text in text_pages.items() if not text.strip()}

    # 2차: 고른 쪽만 쪽 전체 OCR. OCR 할 쪽이 없으면 건너뛴다 (OCR 모델을 불러오는 데 1초 넘게 걸린다).
    ocr_texts: dict[int, str] = {}
    if ocr_pages:
        doc = await run(
            config(
                output_format="plain",
                ocr=ocr,
                force_ocr_pages=sorted(ocr_pages),
                ocr_embedded_images=False,
                pages=xberg.PageConfig(extract_pages=True),
            )
        )
        ocr_texts = {
            p.page_number: p.content or "" for p in doc.pages or [] if p.page_number in ocr_pages
        }

    page_bodies: dict[int, str] = {}
    for number in sorted(text_pages):
        body = text_pages[number]
        if number in ocr_texts:
            body = _combine_ocr(body, ocr_texts[number])
        page_bodies[number] = body.strip()
    content = "\n\n".join(f"<!-- PAGE {n} -->\n\n{b}" for n, b in page_bodies.items())
    result = ParsedDocument(content, page_count)
    if structured:
        result = replace(result, elements=_pdf_elements(scan, page_bodies))
    return result


def _office_elements(doc: Any) -> list[el.Element]:
    """Office·한글 문서의 구조 트리로 요소 목록을 만든다. 구조가 없으면 빈 목록."""
    structure = getattr(doc, "document", None)
    if structure is None or not structure.nodes:
        return []
    nodes = []
    for node in structure.nodes:
        content = json.loads(str(node.content))
        content["page"] = node.page
        nodes.append(content)
    images = {}
    for img in doc.images or []:
        if (image := _image_data(img)) is not None:
            images[img.image_index] = (image, _image_kind(img))
    notes = {p.page_number: p.speaker_notes for p in doc.pages or [] if p.speaker_notes}
    return el.from_structure(nodes, images, notes)


def _pdf_elements(scan: Any, page_bodies: dict[int, str]) -> list[el.Element]:
    """PDF 의 쪽별 본문, 표 추출 결과, 그림으로 요소 목록을 만든다."""
    tables = [
        el.PdfTable(p.page_number, [[str(c or "") for c in row] for row in t.cells or []])
        for p in sorted(scan.pages or [], key=lambda p: p.page_number)
        for t in p.tables or []
    ]
    images = [
        (img.page_number, image, _image_kind(img))
        for img in scan.images or []
        if img.page_number and (image := _image_data(img)) is not None
    ]
    return el.from_pdf(page_bodies, tables, images)


def _combine_ocr(text_layer: str, ocr_text: str) -> str:
    """OCR 한 쪽의 본문을 정한다.

    텍스트 레이어가 충분하면 합치고, 아니면(스캔본) OCR 결과를 쓴다. OCR 이 아무것도 못 읽었으면
    텍스트 레이어를 쓴다. 합친 결과가 텍스트 레이어를 너무 적게 담으면(OCR 이 본문 글자를 못 읽은 경우,
    예: 글꼴을 그리지 못한 PDF) 텍스트 레이어를 그대로 쓰고 OCR 에만 있는 줄(그림 속 글자)을 뒤에 붙인다.
    """
    if not ocr_text.strip():
        return text_layer
    layer_chars = sum(not c.isspace() for c in text_layer)
    if layer_chars < MIN_TEXT_LAYER_CHARS:
        return ocr_text
    merged, stats = merge_page(text_layer, ocr_text)
    if stats.matched_chars < layer_chars * MIN_LAYER_COVERAGE:
        extra = ocr_only_lines(text_layer, ocr_text)
        return "\n".join([text_layer.strip(), *extra]) if extra else text_layer
    return merged


def _describe_xberg_error(exc: object) -> str:
    text = str(exc).lower()
    if "password" in text or "encrypt" in text:
        return "암호가 걸린 문서는 읽을 수 없습니다. 암호를 풀고 다시 올려 주세요."
    if "max_pages" in text or "too many pages" in text or "page limit" in text:
        return "쪽 수가 너무 많습니다."
    if "drm" in text or "distribut" in text:
        return "배포용(보안) 문서는 읽을 수 없습니다. 일반 문서로 저장해서 올려 주세요."
    return "문서를 읽지 못했습니다. 손상되었거나 지원하지 않는 구조일 수 있습니다."
