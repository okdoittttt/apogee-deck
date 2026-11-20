"""문서 구조화 적재: 요소 만들기(본문·표·그림), 쪽을 넘는 표, 그림 설명 요청."""

from langchain_core.messages import AIMessage

from app.documents import elements as el
from app.llm import vision


def _image(width=600, height=400, ocr=""):
    # 실제 그림처럼 픽셀당 약 0.01바이트 (빈 그림 검사에 걸리지 않게)
    return el.ImageData(b"x" * (width * height // 100), "image/webp", width, height, ocr)


def _grid(rows):
    return {
        "rows": len(rows),
        "cols": len(rows[0]),
        "cells": [
            {"content": v, "row": r, "col": c}
            for r, row in enumerate(rows)
            for c, v in enumerate(row)
        ],
    }


NODES = [
    {"node_type": "group", "heading_text": "보고서", "page": 1},
    {"node_type": "heading", "level": 1, "text": "1장 매출", "page": 1},
    {"node_type": "paragraph", "text": "매출이 늘었다.", "page": 1},
    {"node_type": "paragraph", "text": "서울이 가장 컸다.", "page": 1},
    {"node_type": "heading", "level": 2, "text": "1.1 지역별", "page": 1},
    {"node_type": "table", "grid": _grid([["지역", "매출"], ["서울", "120"]]), "page": 1},
    {"node_type": "image", "image_index": 0, "page": 1},
    {"node_type": "image", "image_index": 1, "page": 1},
    {"node_type": "heading", "level": 1, "text": "2장 재고", "page": 2},
    {"node_type": "list_item", "text": "가전 7개", "page": 2},
]


def test_structure_tracks_heading_path_and_groups_paragraphs():
    images = {0: (_image(ocr="120 85"), "unknown"), 1: (_image(40, 40), "icon")}
    out = el.from_structure(NODES, images)

    assert [(e.kind, e.heading) for e in out] == [
        ("text", "1장 매출"),
        ("table", "1장 매출 > 1.1 지역별"),
        ("image", "1장 매출 > 1.1 지역별"),  # 아이콘은 버린다
        ("text", "2장 재고"),  # 같은 단계의 제목이 오면 앞 제목을 대신한다
    ]
    assert out[0].content == "매출이 늘었다.\n\n서울이 가장 컸다."
    assert out[1].content == "| 지역 | 매출 |\n| --- | --- |\n| 서울 | 120 |"
    assert out[2].content == "120 85" and out[2].image is not None  # 설명 전에는 OCR 글자
    assert out[3].page == 2


def test_speaker_notes_follow_their_slide():
    nodes = [
        {"node_type": "heading", "level": 1, "text": "슬라이드 1", "page": 1},
        {"node_type": "paragraph", "text": "본문", "page": 1},
        {"node_type": "heading", "level": 1, "text": "슬라이드 2", "page": 2},
        {"node_type": "paragraph", "text": "둘째", "page": 2},
    ]
    out = el.from_structure(nodes, {}, {1: "강조할 것"})
    assert [e.content for e in out] == ["본문", "발표자 노트: 강조할 것", "둘째"]


def test_keep_image_drops_small_and_decorative():
    assert el.keep_image(600, 400, None)
    assert el.keep_image(600, 400, "unknown")
    assert not el.keep_image(600, 400, "logo")
    assert not el.keep_image(600, 400, "DECORATION")
    assert not el.keep_image(60, 900, None)  # 짧은 변이 작다
    assert not el.keep_image(200, 200, None)  # 넓이가 작다


def test_keep_image_drops_blank_images():
    # 실측값: 차트 856x490 은 WebP 12,446바이트, 같은 크기의 투명도 마스크는 244바이트
    assert el.keep_image(856, 490, None, 12_446)
    assert not el.keep_image(856, 490, None, 244)
    assert el.keep_image(856, 490, None)  # 용량을 모르면 빈 그림 검사를 하지 않는다


def test_table_markdown_escapes_and_pads():
    md = el.table_markdown([["a|b", "c"], ["1"], ["", ""]])
    assert md == "| a\\|b | c |\n| --- | --- |\n| 1 |  |"
    assert el.table_markdown([]) == ""


def test_continued_pdf_table_gets_previous_header():
    header = ["번호", "제품"]
    tables = [
        el.PdfTable(2, [header, ["1", "가"], ["2", "나"]]),
        el.PdfTable(3, [["3", "다"], ["4", "라"]]),  # 다음 쪽 조각: 첫 행이 데이터
        el.PdfTable(4, [header, ["5", "마"]]),  # 머리글을 반복해서 찍은 쪽은 그대로
        el.PdfTable(6, [["x", "y"]]),  # 쪽이 이어지지 않으면 그대로
    ]
    merged = el.merge_continued_tables(tables)
    assert merged[1].rows == [header, ["3", "다"], ["4", "라"]]
    assert merged[2].rows == [header, ["5", "마"]]
    assert merged[3].rows == [["x", "y"]]


def test_pdf_elements_are_ordered_by_page():
    out = el.from_pdf(
        {1: "첫 쪽", 2: "둘째 쪽"},
        [el.PdfTable(1, [["a"], ["1"]])],
        [(2, _image(), None), (1, _image(30, 30), None)],
    )
    assert [(e.page, e.kind) for e in out] == [(1, "text"), (1, "table"), (2, "text"), (2, "image")]


def test_limit_images_keeps_first_ones():
    elements = [el.Element("image", str(i), image=_image()) for i in range(5)]
    elements.insert(2, el.Element("text", "본문"))
    kept = el.limit_images(elements, 3)
    assert [e.content for e in kept] == ["0", "1", "본문", "2"]


async def test_describe_image_sends_low_detail_image_and_korean_instruction():
    seen = []

    class FakeModel:
        async def ainvoke(self, messages):
            seen.append(messages)
            return AIMessage(content=" 막대 차트: 서울 120 ")

    text = await vision.describe_image(FakeModel(), b"\x89PNG", "image/png", "Document: a.pdf")
    assert text == "막대 차트: 서울 120"
    prompt, image = seen[0][0].content
    assert "in Korean" in prompt["text"] and "Document: a.pdf" in prompt["text"]
    assert image["image_url"]["detail"] == "low"
    assert image["image_url"]["url"].startswith("data:image/png;base64,")


def test_vision_model_needs_openai_key():
    import pytest

    from app.core.config import Settings

    with pytest.raises(vision.VisionUnavailableError):
        vision.build_vision_model(Settings(openai_api_key=""))
    model = vision.build_vision_model(Settings(openai_api_key="sk-test"))
    assert model.model_name == "gpt-6-luna"
