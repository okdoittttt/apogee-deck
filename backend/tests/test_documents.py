"""첨부 문서 검증: 형식 확인, 파일 이름, HWPML, 파싱, 파싱 프로세스, 업로드 API."""

import io
import uuid
import zipfile
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import subprocess_targets
from fastapi import HTTPException
from pdf_fixture import text_pdf
from starlette.datastructures import Headers, UploadFile

from app.api.routers import documents as documents_router
from app.core.config import Settings
from app.documents import formats, service
from app.documents.formats import DocumentKind, UnsupportedDocumentError, detect, safe_filename
from app.documents.parser import ParseError, hwpml_to_text, parse
from app.documents.worker import ParseTimeoutError, parse_in_subprocess
from app.models import Document


def make_zip(files: dict[str, bytes], compress: bool = True) -> bytes:
    buf = io.BytesIO()
    method = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    with zipfile.ZipFile(buf, "w", method) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


HWPML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE HWPML [
\t<!ENTITY nbsp\t"&#160;">
]>
<HWPML Version="2.1"><HEAD><DOCSUMMARY><TITLE>머리말 제목</TITLE></DOCSUMMARY></HEAD>
<BODY><SECTION>
<P><TEXT><CHAR>제1조(목적)</CHAR></TEXT><TEXT><CHAR> 이 영은&nbsp;목적을 정한다.</CHAR></TEXT></P>
<P><TEXT><CHAR></CHAR></TEXT></P>
<P><TEXT><TABLE><ROW><CELL><PARALIST><P><TEXT><CHAR>표 칸</CHAR></TEXT></P></PARALIST></CELL></ROW>
</TABLE></TEXT></P>
</SECTION></BODY></HWPML>
""".encode()


# --- 형식 확인 ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "filename", "kind"),
    [
        (b"%PDF-1.7 ...", "x.bin", DocumentKind.PDF),
        (make_zip({"[Content_Types].xml": b"<x/>", "word/document.xml": b"<w/>"}), "a", "docx"),
        (make_zip({"[Content_Types].xml": b"<x/>", "xl/workbook.xml": b"<w/>"}), "a", "xlsx"),
        (make_zip({"[Content_Types].xml": b"<x/>", "ppt/presentation.xml": b"<p/>"}), "a", "pptx"),
        (
            make_zip({"mimetype": b"application/hwp+zip", "Contents/section0.xml": b"<s/>"}),
            "a",
            "hwpx",
        ),
        (HWPML, "법령.hwp", DocumentKind.HWPML),
        ("안녕하세요".encode(), "memo.txt", DocumentKind.TXT),
        ("# 제목".encode(), "README.MD", DocumentKind.MD),
        (b"a,b\n1,2\n", "data.csv", DocumentKind.CSV),
        ("\ufeff본문".encode(), "noext", DocumentKind.TXT),
    ],
)
def test_detect_by_content(data, filename, kind):
    assert detect(data, filename).kind == kind


def test_extension_does_not_decide_binary_formats():
    # 확장자가 .pdf 여도 내용이 ZIP 이면 ZIP 기준으로 판단한다
    with pytest.raises(UnsupportedDocumentError):
        detect(make_zip({"evil.exe": b"MZ"}), "report.pdf")
    # PDF 처럼 보이는 확장자의 실행 파일
    with pytest.raises(UnsupportedDocumentError):
        detect(b"MZ\x90\x00\x03\x00\x00\x00" + bytes(100), "report.pdf")


def test_rejects_empty_and_old_office_and_non_utf8():
    with pytest.raises(UnsupportedDocumentError, match="빈 파일"):
        detect(b"", "a.txt")
    with pytest.raises(UnsupportedDocumentError, match="새 형식"):
        detect(formats._CFB_MAGIC + bytes(600), "old.doc")
    with pytest.raises(UnsupportedDocumentError):
        detect("한글".encode("cp949") + b"\xff\xfe", "a.txt")


def test_detects_hwp5_binary():
    data = formats._CFB_MAGIC + bytes(100) + formats._HWP5_SIGNATURE + bytes(100)
    assert detect(data, "a.hwp").kind == DocumentKind.HWP


def test_rejects_zip_bomb(monkeypatch):
    bomb = make_zip({"[Content_Types].xml": b"<x/>", "word/document.xml": b"\0" * 2_000_000})
    with pytest.raises(UnsupportedDocumentError, match="너무 커지는"):
        detect(bomb, "a.docx")
    monkeypatch.setattr(formats, "MAX_ZIP_ENTRIES", 3)
    many = make_zip({f"word/{i}.xml": b"x" for i in range(5)} | {"[Content_Types].xml": b"x"})
    with pytest.raises(UnsupportedDocumentError, match="너무 많습니다"):
        detect(many, "a.docx")


def test_rejects_broken_zip():
    with pytest.raises(UnsupportedDocumentError, match="손상"):
        detect(b"PK\x03\x04broken", "a.docx")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("C:\\Users\\me\\보고서.pdf", "보고서.pdf"),
        ("../../etc/passwd", "passwd"),
        ("a\x00b\nc.txt", "abc.txt"),
        ("", "document"),
        (None, "document"),
        ("...", "document"),
        ("가" * 300 + ".hwpx", "가" * 250 + ".hwpx"),
    ],
)
def test_safe_filename(name, expected):
    assert safe_filename(name) == expected


# --- 파싱 ---------------------------------------------------------------------------------------


def test_hwpml_extracts_body_paragraphs_only():
    text = hwpml_to_text(HWPML)
    assert text == "제1조(목적) 이 영은 목적을 정한다.\n\n표 칸"
    assert "머리말" not in text  # HEAD 는 본문이 아니다


def test_hwpml_ignores_entity_definitions():
    # DOCTYPE 을 지우므로 엔티티 정의(외부 엔티티·엔티티 폭탄)는 동작하지 않고 파싱이 실패한다
    evil = b"""<?xml version="1.0"?>
<!DOCTYPE HWPML [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<HWPML><BODY><SECTION><P><TEXT><CHAR>&xxe;</CHAR></TEXT></P></SECTION></BODY></HWPML>"""
    with pytest.raises(ParseError):
        hwpml_to_text(evil)


def test_hwpml_without_body():
    with pytest.raises(ParseError, match="본문"):
        hwpml_to_text(b"<?xml version='1.0'?><HWPML><HEAD/></HWPML>")


def test_parse_text_formats():
    assert parse("줄1\r\n줄2".encode(), DocumentKind.TXT, "a.txt", 10).content == "줄1\n줄2"
    with pytest.raises(ParseError, match="글자를 찾지"):
        parse(b"  \n ", DocumentKind.TXT, "a.txt", 10)


def test_parse_text_pdf_with_page_markers():
    result = parse(text_pdf(["Hello page one", "Second page"]), DocumentKind.PDF, "a.pdf", 10)
    assert result.page_count == 2
    assert "<!-- PAGE 1 -->" in result.content and "Hello page one" in result.content
    assert result.content.index("Hello page one") < result.content.index("Second page")


def test_parse_pdf_page_limit_and_broken_pdf():
    with pytest.raises(ParseError, match="쪽 수"):
        parse(text_pdf(["a", "b", "c"]), DocumentKind.PDF, "a.pdf", 2)
    with pytest.raises(ParseError, match="읽지 못했습니다"):
        parse(b"%PDF-1.4 garbage", DocumentKind.PDF, "a.pdf", 2)


# --- 파싱 프로세스 ------------------------------------------------------------------------------


async def test_subprocess_parses_real_document():
    result = await parse_in_subprocess(
        "본문".encode(), DocumentKind.TXT, "a.txt", max_pages=10, timeout=60
    )
    assert result.content == "본문"


async def test_subprocess_passes_arguments():
    result = await parse_in_subprocess(
        b"abc",
        DocumentKind.PDF,
        "f.pdf",
        max_pages=7,
        timeout=60,
        target=subprocess_targets.echo_size,
    )
    assert (result.content, result.page_count) == ("pdf:f.pdf:3", 7)
    assert result.elements is None


async def test_subprocess_passes_structured_flag():
    result = await parse_in_subprocess(
        b"abc",
        DocumentKind.PDF,
        "f.pdf",
        max_pages=7,
        timeout=60,
        structured=True,
        target=subprocess_targets.echo_size,
    )
    assert result.elements == ["structured"]


async def test_subprocess_reports_parse_error():
    with pytest.raises(ParseError, match="글자를 찾지"):
        await parse_in_subprocess(b" ", DocumentKind.TXT, "a.txt", max_pages=10, timeout=60)


async def test_subprocess_timeout_kills_parser():
    with pytest.raises(ParseTimeoutError, match="초과"):
        await parse_in_subprocess(
            b"x",
            DocumentKind.TXT,
            "a.txt",
            max_pages=1,
            timeout=1.5,
            target=subprocess_targets.sleep_forever,
        )


async def test_subprocess_crash_is_reported():
    with pytest.raises(ParseError, match="오류가 났습니다"):
        await parse_in_subprocess(
            b"x",
            DocumentKind.TXT,
            "a.txt",
            max_pages=1,
            timeout=30,
            target=subprocess_targets.crash,
        )


# --- 업로드 API (DB 없이 검사 단계만) ------------------------------------------------------------


USER = SimpleNamespace(id=uuid.uuid4(), status="approved")


def upload(data: bytes, name: str = "a.txt") -> UploadFile:
    return UploadFile(io.BytesIO(data), filename=name, headers=Headers({}))


async def test_upload_requires_storage(monkeypatch):
    monkeypatch.setattr(documents_router, "get_storage", lambda: None)
    with pytest.raises(HTTPException) as exc:
        await documents_router.upload_document(upload(b"hi"), None, USER)
    assert exc.value.status_code == 503


async def test_upload_rejects_large_file(monkeypatch):
    monkeypatch.setattr(documents_router, "get_storage", lambda: object())
    monkeypatch.setattr(documents_router, "get_settings", lambda: Settings(document_max_bytes=10))
    with pytest.raises(HTTPException) as exc:
        await documents_router.upload_document(upload(b"x" * 11), None, USER)
    assert exc.value.status_code == 413


async def test_upload_rejects_unsupported_file(monkeypatch):
    monkeypatch.setattr(documents_router, "get_storage", lambda: object())
    with pytest.raises(HTTPException) as exc:
        await documents_router.upload_document(
            upload(b"MZ\x90\x00" + bytes(64), "a.exe"), None, USER
        )
    assert exc.value.status_code == 415


async def test_upload_stores_original_and_starts_parsing(monkeypatch):
    stored, scheduled = {}, []

    class Storage:
        async def put(self, key, data, content_type):
            stored[key] = (data, content_type)

    class Session:
        def add(self, doc):
            doc.created_at = datetime.now(UTC)

        async def commit(self):
            pass

    monkeypatch.setattr(documents_router, "get_storage", lambda: Storage())
    monkeypatch.setattr(
        documents_router.service, "schedule_parse", lambda *args: scheduled.append(args)
    )
    out = await documents_router.upload_document(
        upload("메모".encode(), "dir/메모.md"), Session(), USER
    )

    assert out.status == "processing" and out.kind == "md" and out.filename == "메모.md"
    assert stored[f"documents/{out.id}"] == ("메모".encode(), "text/markdown; charset=utf-8")
    assert scheduled == [(out.id, "메모".encode(), "md", "메모.md")]


def test_storage_key_is_not_derived_from_filename():
    doc_id = uuid.uuid4()
    assert service.storage_key(doc_id) == f"documents/{doc_id}"
    assert Document.__tablename__ == "documents"


def test_prefetch_script_uses_parser_language():
    import importlib.util
    from pathlib import Path

    from app.documents.parser import OCR_LANGUAGE

    path = Path(__file__).resolve().parent.parent / "scripts" / "prefetch_ocr_models.py"
    spec = importlib.util.spec_from_file_location("prefetch_ocr_models", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.OCR_LANGUAGE == OCR_LANGUAGE
    assert module.blank_png().startswith(b"\x89PNG")
