"""문서를 텍스트(Markdown)로 바꾼다.

xberg(Kreuzberg 후속)로 파싱하고, OCR 은 PaddleOCR 한국어 모델을 쓴다. 이 모듈은 백엔드와 다른
프로세스에서 실행된다 (``app.documents.worker``). 파서가 멈추거나 죽어도 백엔드에 영향이 없게 하려는
것이라, 여기서는 DB 나 앱 설정을 쓰지 않는다.

형식별 처리:

- PDF: 본문은 쪽별 텍스트 레이어다. 텍스트 레이어는 pypdfium2 로 뽑는다 (``_pdf_text_layers``). xberg 기본
  추출은 "칸 이름 | 값" 이 나란한 표 같은 배치를 여러 단으로 보고 단별로 읽어서, 회의록의 서로 다른 회의 내용이
  섞일 만큼 줄 순서가 틀어졌다. pdfium 은 PDF 에 그린 순서를 따라서 사람이 읽는 순서와 맞았다.
  그림 속 글자는 쪽 전체가 아니라 **남길 그림만 하나씩 OCR** 해서 본문과 섞지 않는다 (그림이 없으면 OCR 안 함).
  쪽 전체를 OCR 해서 본문과 합치면 차트의 눈금·범례가 본문 문장 사이에 끼어서, 모델이 눈금 값을 데이터로
  읽는 오답이 났다. 그림 속 글자는 프로젝트 문서면 그림 요소에(그림 설명의 참고 자료와 대체 글자), 대화 첨부면
  쪽 끝에 표시를 붙여 넣는다. 글자가 거의 없는 쪽(스캔본)만 쪽 전체 OCR 결과를 본문으로 쓴다.
  출력은 plain 텍스트다. Markdown 출력은 OCR 결과의 띄어쓰기를 지우고 줄을 제목(``#``)으로 바꿔서 한국어 OCR
  품질이 크게 떨어졌다.
- DOCX, XLSX, PPTX, HWP, HWPX: 문서 안 그림을 OCR 해서 제자리에 넣는다(``ocr_embedded_images``).
  출력은 Markdown 이다 (표가 Markdown 표로 남는다).
- HWPML(XML 로 된 .hwp): xberg 는 일반 XML 로 처리해서 태그 이름이 섞이므로 직접 본문만 뽑는다.
- TXT, MD, CSV: 그대로 쓴다.

``structured=True`` (프로젝트 문서)면 본문과 함께 요소 목록(본문·표·그림, ``app.documents.elements``)도
돌려준다. 그림 원본은 이때만 받아 온다 (WebP, 긴 변 ``IMAGE_MAX_SIDE`` 이하).
"""

import asyncio
import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from typing import Any

from app.documents import elements as el
from app.documents.formats import DocumentKind

logger = logging.getLogger(__name__)

OCR_LANGUAGE = "korean"
"""PaddleOCR 언어. 한국어 모델은 영문·숫자도 읽는다."""

MIN_TEXT_LAYER_CHARS = 20
"""PDF 쪽의 텍스트 레이어가 이보다 적으면(공백 제외) 스캔본으로 보고 쪽 전체 OCR 결과를 본문으로 쓴다."""

FIGURE_TEXT_LABEL = "[그림 속 글자 (OCR, 배치 순서가 정확하지 않을 수 있음)]"
"""대화 첨부 PDF 에서 쪽 끝에 붙이는 그림 속 글자의 표시. 모델이 본문 문장으로 읽지 않게 한다."""

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

OCR_IMAGE_FORMATS = frozenset({"png", "jpeg", "jpg", "webp", "bmp", "tiff", "tif", "gif"})
"""그림 하나를 OCR 할 때 xberg 에 넘길 수 있는 그림 형식 (확장자로 형식을 알려준다)."""

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


# --- PDF 텍스트 레이어 (pypdfium2) -----------------------------------------------------------------

# pdfium 이 텍스트에 넣는 제어 문자: 줄 끝 하이픈(\x02), 글꼴에 없는 글자 자리(\x00, \ufffe 등)
_PDFIUM_JUNK = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def _pdf_text_layers(data: bytes) -> dict[int, str] | None:
    """PDF 의 쪽별 텍스트 레이어를 pdfium 으로 뽑는다 (PDF 에 그린 순서대로).

    Args:
        data: PDF 바이트.

    Returns:
        쪽 번호(1부터) → 텍스트. PDF 를 열지 못하면 ``None`` (호출하는 쪽이 xberg 결과를 쓴다).
    """
    import pypdfium2 as pdfium  # 파싱 프로세스에서만 불러온다

    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        logger.info("pdfium could not open the PDF, using xberg text: %s", exc)
        return None
    try:
        layers: dict[int, str] = {}
        for index in range(len(pdf)):
            page = pdf[index]
            textpage = page.get_textpage()
            try:
                text = textpage.get_text_bounded()
            finally:
                textpage.close()
                page.close()
            text = _PDFIUM_JUNK.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
            layers[index + 1] = "\n".join(line.rstrip() for line in text.split("\n")).strip()
        return layers
    finally:
        pdf.close()


# --- xberg ---------------------------------------------------------------------------------------


def _image_data(img: Any) -> el.ImageData | None:
    """Xberg 그림을 요소용 원본으로 바꾼다. vision 모델이 받지 않는 형식이면 ``None``."""
    mime = IMAGE_MIME_TYPES.get(str(img.format or "").lower())
    if mime is None or not img.data:
        return None
    return el.ImageData(bytes(img.data), mime, img.width or 0, img.height or 0, _ocr_text(img))


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

    async def run(
        cfg: "xberg.ExtractionConfig", source_bytes: bytes = data, name: str = filename
    ) -> "xberg.ExtractedDocument":
        source = xberg.ExtractInput(kind="bytes", bytes=source_bytes, filename=name)
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

    # 1차: OCR 없이 쪽별 텍스트 레이어와 그림을 뽑는다 (빠르다).
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
    # 줄 순서는 pdfium 의 텍스트 레이어를 쓴다. 읽지 못하거나 쪽 수가 다르면 xberg 결과를 그대로 쓴다.
    layers = _pdf_text_layers(data)
    if layers is not None and layers.keys() == text_pages.keys():
        text_pages = layers
    page_count = len(text_pages)
    if page_count > max_pages:
        raise ParseError(f"쪽 수가 너무 많습니다 ({page_count}쪽, 최대 {max_pages}쪽).")

    # 2차: 글자가 거의 없는 쪽(스캔본)만 쪽 전체를 OCR 해서 본문으로 쓴다.
    scanned = {n for n, text in text_pages.items() if _chars(text) < MIN_TEXT_LAYER_CHARS}
    if scanned:
        doc = await run(
            config(
                output_format="plain",
                ocr=ocr,
                force_ocr_pages=sorted(scanned),
                ocr_embedded_images=False,
                pages=xberg.PageConfig(extract_pages=True),
            )
        )
        for p in doc.pages or []:
            if p.page_number in scanned and _chars(p.content or "") > _chars(
                text_pages[p.page_number]
            ):
                text_pages[p.page_number] = p.content or ""

    # 3차: 남길 그림만 하나씩 OCR 한다 (그림 속 글자). 그림이 없으면 OCR 모델을 불러오지 않는다.
    figures = _pdf_figures(scan.images or [])
    figure_ocr: dict[int, str] = {}
    for img in figures:
        ext = str(img.format or "").lower()
        if not img.data or ext not in OCR_IMAGE_FORMATS:
            continue
        try:
            found = await run(
                config(output_format="plain", ocr=ocr), bytes(img.data), f"figure.{ext}"
            )
        except ParseError:
            continue  # 그림 하나를 못 읽어도 문서는 계속 처리한다
        if text := (found.content or "").strip():
            figure_ocr[id(img)] = text

    page_bodies = {n: text_pages[n].strip() for n in sorted(text_pages)}
    if structured:
        content = _pdf_content(page_bodies, {})
        elements = _pdf_elements(scan, page_bodies, figures, figure_ocr)
        return ParsedDocument(content, page_count, elements)
    # 대화 첨부는 그림 설명이 없으므로 그림 속 글자를 쪽 끝에 표시와 함께 남긴다.
    figure_text: dict[int, list[str]] = {}
    for img in figures:
        if text := figure_ocr.get(id(img)):
            figure_text.setdefault(img.page_number, []).append(text)
    return ParsedDocument(_pdf_content(page_bodies, figure_text), page_count)


def _chars(text: str) -> int:
    return sum(not c.isspace() for c in text)


def _pdf_content(page_bodies: dict[int, str], figure_text: dict[int, list[str]]) -> str:
    """쪽 표시를 넣은 PDF 본문. 그림 속 글자가 있으면 그 쪽 끝에 표시와 함께 붙인다."""
    pages = []
    for number, body in page_bodies.items():
        parts = [body] if body else []
        for text in figure_text.get(number, []):
            # OCR 은 줄마다 한 칸(눈금, 범례 등)이라, 한 줄로 이어서 본문과 구분한다.
            joined = " / ".join(line.strip() for line in text.splitlines() if line.strip())
            parts.append(f"{FIGURE_TEXT_LABEL} {joined}")
        pages.append(f"<!-- PAGE {number} -->\n\n" + "\n\n".join(parts))
    return "\n\n".join(pages)


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


def _pdf_figures(images: list[Any]) -> list[Any]:
    """PDF 그림 중 남길 것: 투명도 마스크와 버릴 그림(아이콘, 작은 그림, 빈 그림)을 뺀다.

    Args:
        images: xberg 그림 목록 (그림마다 OCR 결과가 들어 있다).

    Returns:
        남길 xberg 그림 (문서 순서).
    """
    masks = _soft_masks(images)
    return [
        img
        for img in images
        if img.page_number
        and id(img) not in masks
        and el.keep_image(
            img.width or 0, img.height or 0, _image_kind(img), len(img.data or b"") or None
        )
    ]


def _ocr_text(img: Any) -> str:
    """Xberg 가 그림을 OCR 한 결과 (Office·한글 문서는 xberg 가 그림마다 OCR 한다)."""
    return ((getattr(img.ocr_result, "content", None) if img.ocr_result else None) or "").strip()


def _pdf_elements(
    scan: Any, page_bodies: dict[int, str], figures: list[Any], figure_ocr: dict[int, str]
) -> list[el.Element]:
    """PDF 의 쪽별 본문, 표 추출 결과, 그림(그림 속 글자 포함)으로 요소 목록을 만든다."""
    tables = [
        el.PdfTable(p.page_number, [[str(c or "") for c in row] for row in t.cells or []])
        for p in sorted(scan.pages or [], key=lambda p: p.page_number)
        for t in p.tables or []
    ]
    images = [
        (img.page_number, replace(image, ocr_text=figure_ocr.get(id(img), "")), _image_kind(img))
        for img in figures
        if (image := _image_data(img)) is not None
    ]
    return el.from_pdf(page_bodies, tables, images)


def _soft_masks(images: list[Any]) -> set[int]:
    """그림의 투명도 마스크로 보이는 그림(``id``)을 찾는다.

    PDF 는 투명한 부분이 있는 그림(차트 PNG 등)을 컬러 그림과 회색조 마스크로 나눠 저장하는데, xberg 는 둘을
    따로 돌려주고 마스크라고 표시하지 않는다. 같은 쪽에 같은 크기의 컬러 그림이 있는 회색조 그림을 마스크로 본다.
    (투명도가 단순한 마스크는 빈 그림 검사에서도 걸리지만, 투명도에 모양이 있으면 그 검사를 지나간다.)

    Args:
        images: xberg 그림 목록.

    Returns:
        마스크로 볼 그림 객체의 ``id`` 집합.
    """
    colored = {
        (img.page_number, img.width, img.height)
        for img in images
        if str(getattr(img, "colorspace", "") or "") != "DeviceGray"
    }
    return {
        id(img)
        for img in images
        if str(getattr(img, "colorspace", "") or "") == "DeviceGray"
        and (img.page_number, img.width, img.height) in colored
    }


def _describe_xberg_error(exc: object) -> str:
    text = str(exc).lower()
    if "password" in text or "encrypt" in text:
        return "암호가 걸린 문서는 읽을 수 없습니다. 암호를 풀고 다시 올려 주세요."
    if "max_pages" in text or "too many pages" in text or "page limit" in text:
        return "쪽 수가 너무 많습니다."
    if "drm" in text or "distribut" in text:
        return "배포용(보안) 문서는 읽을 수 없습니다. 일반 문서로 저장해서 올려 주세요."
    return "문서를 읽지 못했습니다. 손상되었거나 지원하지 않는 구조일 수 있습니다."
