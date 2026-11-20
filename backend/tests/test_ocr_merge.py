"""PDF 쪽의 OCR 결과와 텍스트 레이어 합치기 검증."""

from app.documents.ocr_merge import merge_page
from app.documents.parser import _combine_ocr


def merged(layer: str, ocr: str) -> str:
    return merge_page(layer, ocr)[0]


def test_restores_spacing_from_text_layer():
    layer = "핵심: 판단용 표준 인터페이스(Choice / Noul / Score) + 오픈 웨이트 모델 6종 공개"
    ocr = "핵심:판단용 표준인터페이스(Choice/Noul/Score) +오픈 웨이트 모델6종 공개"
    assert merged(layer, ocr) == layer


def test_keeps_ocr_line_order():
    # 슬라이드처럼 텍스트 레이어 순서가 뒤섞여 있어도 OCR 의 줄 순서를 따른다
    layer = "Kai-0.6B\nLex-0.6B\n0.57B\n0.57B\n양방향 · 1K · 범용"
    ocr = "Kai-0.6B\n0.57B\n양방향·1K·범용\nLex-0.6B\n0.57B"
    assert merged(layer, ocr).splitlines() == [
        "Kai-0.6B",
        "0.57B",
        "양방향 · 1K · 범용",
        "Lex-0.6B",
        "0.57B",
    ]


def test_keeps_text_found_only_in_image():
    layer = "판단 인터페이스의 정형화\n식 (1) — T: 추론 온도"
    ocr = "판단 인터페이스의정형화\nexp(zi/T)\n식(1)-T:추론 온도"
    assert merged(layer, ocr).splitlines() == [
        "판단 인터페이스의 정형화",
        "exp(zi/T)",
        "식 (1) — T: 추론 온도",
    ]


def test_line_mixing_text_layer_and_image_text():
    layer = "반환: 전체 분포 + argmax 식별자"
    ocr = "그림설명 반환:전체 분포+argmax 식별자"
    assert merged(layer, ocr) == "그림설명 반환: 전체 분포 + argmax 식별자"


def test_bridges_character_ocr_dropped_or_added():
    layer = "Liu et al., “Decision 1.0,” 2026\n77.22 (2위) · Decisions 1위"
    ocr = 'Liu et al., Decision 1.0," 2026\n77.22 (2위I) · Decisions 1위'
    assert merged(layer, ocr).splitlines() == [
        "Liu et al., “Decision 1.0,” 2026",
        "77.22 (2위) · Decisions 1위",
    ]


def test_repeated_text_follows_reading_position():
    layer = "가나다 첫째\n가나다 둘째"
    ocr = "가나다첫째\n가나다둘째"
    assert merged(layer, ocr) == "가나다 첫째\n가나다 둘째"


def test_short_lines_and_stats():
    text, stats = merge_page("소모품비 0.03", "A\n소모품비\n0.03\n12")
    assert text.splitlines() == ["A", "소모품비", "0.03", "12"]
    assert stats.total_chars == 1 + 4 + 4 + 2
    assert stats.matched_chars == 8  # 3자 미만 줄("A", "12")은 맞춰 보지 않는다


def test_combine_ocr_chooses_source():
    scanned_ocr = "스캔본에서 읽은 글자"
    assert _combine_ocr("", scanned_ocr) == scanned_ocr  # 텍스트 레이어가 없으면 OCR
    layer = "텍스트 레이어에 있는 충분히 긴 본문 문장입니다."
    assert _combine_ocr(layer, "   ") == layer  # OCR 이 못 읽었으면 텍스트 레이어
    assert _combine_ocr(layer, "텍스트레이어에 있는 충분히 긴본문 문장입니다.") == layer


def test_combine_ocr_keeps_text_layer_when_ocr_missed_it():
    # OCR 이 한국어 본문을 못 읽고 숫자와 그림 속 글자만 읽은 쪽 (글꼴을 그리지 못한 PDF)
    layer = "2024년 사업 보고서\n올해 매출은 서울을 중심으로 늘었다.\n서울 110 120"
    ocr = "2024\n110\n120\n지역별 매출 차트"
    combined = _combine_ocr(layer, ocr)
    assert combined.startswith(layer)
    assert combined.endswith("지역별 매출 차트")  # 그림 속 글자는 뒤에 붙는다
    assert combined.count("110") == 1  # 텍스트 레이어에 있는 줄은 다시 붙이지 않는다


def test_combine_ocr_still_merges_when_ocr_covers_text_layer():
    # 정상적인 쪽: OCR 이 본문을 읽었으면 지금처럼 OCR 줄 순서 + 텍스트 레이어 글자로 합친다
    layer = "판단 인터페이스의 정형화\n식 (1) — T: 추론 온도"
    ocr = "판단 인터페이스의정형화\nexp(zi/T)\n식(1)-T:추론 온도"
    assert _combine_ocr(layer, ocr).splitlines() == [
        "판단 인터페이스의 정형화",
        "exp(zi/T)",
        "식 (1) — T: 추론 온도",
    ]
