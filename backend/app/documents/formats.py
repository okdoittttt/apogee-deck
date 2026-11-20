"""첨부 문서의 형식을 파일 내용으로 확인한다.

확장자나 브라우저가 보낸 Content-Type 은 믿지 않는다. 내용의 서명(magic bytes)과
ZIP 안의 구성을 보고 형식을 정한다. 확장자는 내용으로 구분할 수 없는 텍스트 형식
(TXT/MD/CSV)을 나눌 때만 쓴다.
"""

import io
import zipfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath

MAX_ZIP_ENTRIES = 5_000
MAX_ZIP_UNCOMPRESSED_BYTES = 300 * 1024 * 1024
MAX_ZIP_RATIO = 100
"""ZIP 기반 문서(DOCX, XLSX, PPTX, HWPX)의 압축 폭탄 검사 기준. 압축을 풀기 전에 목록만 본다."""

_CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_HWP5_SIGNATURE = "HWP Document File".encode("utf-16-le")


class DocumentKind(StrEnum):
    """지원하는 문서 형식."""

    PDF = "pdf"
    DOCX = "docx"
    XLSX = "xlsx"
    PPTX = "pptx"
    HWP = "hwp"
    """한글 바이너리 문서 (HWP 5.0)."""
    HWPML = "hwpml"
    """XML 로 된 한글 문서. 국가법령정보센터 등이 ``.hwp`` 확장자로 내려준다."""
    HWPX = "hwpx"
    TXT = "txt"
    MD = "md"
    CSV = "csv"


MIME_TYPES = {
    DocumentKind.PDF: "application/pdf",
    DocumentKind.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    DocumentKind.XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    DocumentKind.PPTX: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    DocumentKind.HWP: "application/x-hwp",
    DocumentKind.HWPML: "application/x-hwp",
    DocumentKind.HWPX: "application/haansofthwpx",
    DocumentKind.TXT: "text/plain; charset=utf-8",
    DocumentKind.MD: "text/markdown; charset=utf-8",
    DocumentKind.CSV: "text/csv; charset=utf-8",
}
"""형식 → 원본을 내려줄 때 쓰는 MIME 타입."""

SUPPORTED_EXTENSIONS = ("pdf", "docx", "xlsx", "pptx", "hwp", "hwpx", "txt", "md", "csv")
"""화면의 파일 선택 창에 보여줄 확장자."""


class UnsupportedDocumentError(ValueError):
    """지원하지 않는 형식이거나 손상된 파일. 메시지는 사용자에게 그대로 보여준다."""


@dataclass(frozen=True)
class DetectedFormat:
    """내용으로 확인한 문서 형식.

    Attributes:
        kind: 문서 형식.
        mime_type: 원본을 내려줄 때 쓰는 MIME 타입.
    """

    kind: DocumentKind
    mime_type: str


def safe_filename(name: str | None, limit: int = 255) -> str:
    """사용자가 보낸 파일 이름에서 경로와 제어 문자를 뺀다.

    Args:
        name: 업로드한 파일 이름. 브라우저에 따라 경로가 붙어 올 수 있다.
        limit: 최대 글자 수. 넘으면 확장자를 살리고 앞부분을 자른다.

    Returns:
        화면에 보여주고 다운로드 이름으로 쓸 파일 이름. 비어 있으면 ``"document"``.
    """
    base = PureWindowsPath(PurePosixPath(name or "").name).name
    base = "".join(ch for ch in base if ch.isprintable()).strip().strip(".")
    if not base:
        return "document"
    if len(base) > limit:
        stem, dot, ext = base.rpartition(".")
        if dot and 0 < len(ext) <= 10:
            base = stem[: limit - len(ext) - 1] + "." + ext
        else:
            base = base[:limit]
    return base


def _extension(filename: str) -> str:
    _, dot, ext = filename.rpartition(".")
    return ext.lower() if dot else ""


def _check_zip(zf: zipfile.ZipFile, size: int) -> list[str]:
    """압축 폭탄을 막기 위해 ZIP 목록을 검사하고 파일 이름 목록을 돌려준다."""
    infos = zf.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        raise UnsupportedDocumentError("문서 안의 파일이 너무 많습니다.")
    total = sum(i.file_size for i in infos)
    if total > MAX_ZIP_UNCOMPRESSED_BYTES or total > max(size, 1) * MAX_ZIP_RATIO:
        raise UnsupportedDocumentError("압축을 풀면 너무 커지는 문서입니다.")
    return [i.filename for i in infos]


def _detect_zip(data: bytes) -> DocumentKind:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = _check_zip(zf, len(data))
            if "mimetype" in names:
                mimetype = zf.read("mimetype")[:100].decode("ascii", "replace").strip()
                if mimetype == "application/hwp+zip":
                    return DocumentKind.HWPX
    except zipfile.BadZipFile as exc:
        raise UnsupportedDocumentError("손상된 문서입니다.") from exc
    if "[Content_Types].xml" in names:
        prefixes = {n.split("/", 1)[0] for n in names}
        if "word" in prefixes:
            return DocumentKind.DOCX
        if "xl" in prefixes:
            return DocumentKind.XLSX
        if "ppt" in prefixes:
            return DocumentKind.PPTX
    raise UnsupportedDocumentError("지원하지 않는 문서 형식입니다.")


def _decode_text(data: bytes) -> str | None:
    """UTF-8(또는 BOM 이 있는 UTF-8) 텍스트면 문자열, 아니면 ``None``."""
    if b"\x00" in data[:8192]:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def detect(data: bytes, filename: str) -> DetectedFormat:
    """파일 내용으로 문서 형식을 정한다.

    Args:
        data: 파일 내용.
        filename: 파일 이름. 텍스트 형식(TXT/MD/CSV)을 나눌 때만 쓴다.

    Returns:
        확인한 형식과 MIME 타입.

    Raises:
        UnsupportedDocumentError: 빈 파일, 지원하지 않는 형식, 손상된 파일, 압축 폭탄일 때.
    """
    if not data:
        raise UnsupportedDocumentError("빈 파일입니다.")
    if data.startswith(b"%PDF-"):
        kind = DocumentKind.PDF
    elif data.startswith(b"PK\x03\x04"):
        kind = _detect_zip(data)
    elif data.startswith(_CFB_MAGIC):
        if _HWP5_SIGNATURE not in data[: 64 * 1024]:
            raise UnsupportedDocumentError(
                "지원하지 않는 문서 형식입니다. 예전 MS Office 문서(.doc, .xls, .ppt)는 "
                "새 형식(.docx, .xlsx, .pptx)으로 저장해서 올려 주세요."
            )
        kind = DocumentKind.HWP
    elif (text := _decode_text(data)) is not None:
        head = text[:2048].lstrip()
        if head.startswith("<?xml") and "<HWPML" in head:
            kind = DocumentKind.HWPML
        else:
            ext = _extension(filename)
            kind = {
                "md": DocumentKind.MD,
                "markdown": DocumentKind.MD,
                "csv": DocumentKind.CSV,
            }.get(ext, DocumentKind.TXT)
    else:
        raise UnsupportedDocumentError(
            "지원하지 않는 문서 형식입니다. PDF, Word, Excel, PowerPoint, 한글, "
            "텍스트(UTF-8) 파일을 올려 주세요."
        )
    return DetectedFormat(kind, MIME_TYPES[kind])
