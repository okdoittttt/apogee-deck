"""PDF 쪽 배치 검증: 줄 → 문단 → 제목, 쪽을 넘는 제목 경로, 그림 자리, 실제 PDF."""

from types import SimpleNamespace

from pdf_fixture import layout_pdf, text_pdf

from app.documents import pdf_layout as layout
from app.documents.elements import PdfBlock
from app.documents.pdf_layout import Line, PageImage, RawPage

BODY = 10.5


def _lines(*rows):
    """``(위, 글자[, 크기])`` 로 줄을 만든다. 줄 높이는 글자 크기와 같다."""
    out = []
    for row in rows:
        top, text, size = (*row, BODY) if len(row) == 2 else row
        out.append(Line(text, top, top - size, size))
    return out


# 시험 문서(01 실적 보고서)와 같은 배치: 제목 17, 절 제목 13, 본문 10.5 (줄 간격 약 7, 문단 간격 약 12)
REPORT = _lines(
    (782, "Annual Report", 17),
    (751, "Written by finance", 9),
    (728, "1. Summary", 13),
    (704, "Revenue grew 25 percent."),
    (687, "Overseas sales passed 10 billion."),
    (670, "Profit margin was 14 percent."),
    (646, "A second paragraph starts here."),
    (626, "2. Quarterly sales", 13),
)


def test_paragraphs_split_on_wide_gaps_and_size_changes():
    gap = layout.line_gap([RawPage(REPORT)], BODY)
    assert 6 < gap < 7.5  # 문단 안의 줄 간격 (위 줄 아래 ~ 아래 줄 위)
    paragraphs = layout.group_paragraphs(REPORT, layout.gap_limit([RawPage(REPORT)], BODY))
    assert [p.text for p in paragraphs] == [
        "Annual Report",
        "Written by finance",
        "1. Summary",
        "Revenue grew 25 percent.\nOverseas sales passed 10 billion.\nProfit margin was 14 percent.",
        "A second paragraph starts here.",  # 간격이 넓어 새 문단
        "2. Quarterly sales",
    ]


def test_gap_limit_does_not_merge_one_line_paragraphs():
    # 한 줄짜리 문단만 있으면 문단 사이 간격(13)을 문단 안의 간격으로 보게 되는데, 본문 크기로 상한을 둔다
    lines = _lines((700, "a"), (676.5, "b"), (653, "c"))
    limit = layout.gap_limit([RawPage(lines)], BODY)
    assert limit == BODY
    assert len(layout.group_paragraphs(lines, limit)) == 3


def test_table_row_and_following_body_are_separate_paragraphs():
    # 표 글자(9.5)와 본문(10.5)은 줄 간격이 좁아도 다른 문단이다
    lines = _lines((655, "5 Size differs 6.3%", 9.5), (639, "Waiting complaints peaked in summer."))
    assert len(layout.group_paragraphs(lines, 10.5)) == 2


def test_cells_on_the_same_row_become_one_line():
    rows = _lines(
        (321, "Region", 9.5), (321, "Sales", 9.5), (301, "Korea", 9.5), (300.5, "231", 9.5)
    )
    assert [line.text for line in layout.merge_rows(rows)] == ["Region Sales", "Korea 231"]


def test_headings_become_paths_and_continue_on_next_page():
    page2 = _lines(
        (783, "Still about quarterly sales."),
        (760, "3. Regions", 13),
        (736, "Korea led growth."),
        (712, "Tables have bold small headers", 9.5),  # 굵기만 하고 작은 글자는 제목이 아니다
    )
    pages = layout.build_pages({1: RawPage(REPORT), 2: RawPage(page2)})
    assert pages[1].blocks == [
        PdfBlock("Written by finance", "Annual Report"),
        PdfBlock(
            "Revenue grew 25 percent.\nOverseas sales passed 10 billion.\nProfit margin was 14 percent.",
            "Annual Report > 1. Summary",
        ),
        PdfBlock("A second paragraph starts here.", "Annual Report > 1. Summary"),
    ]
    assert pages[2].heading == "Annual Report > 2. Quarterly sales"  # 앞 쪽에서 이어진 제목
    assert [b.heading for b in pages[2].blocks] == [
        "Annual Report > 2. Quarterly sales",
        "Annual Report > 3. Regions",  # 같은 단계 제목은 앞 제목을 바꾼다
        "Annual Report > 3. Regions",
    ]
    assert pages[1].text.startswith("# Annual Report\n\nWritten by finance\n\n## 1. Summary\n\n")


def test_long_or_multi_line_large_text_is_not_a_heading():
    big = layout.Paragraph("x" * 100, 700, 687, 13)
    tall = layout.Paragraph("a\nb\nc", 700, 660, 13, lines=3)
    short = layout.Paragraph("1. Summary", 700, 687, 13)
    assert not layout.is_heading(big, BODY)
    assert not layout.is_heading(tall, BODY)
    assert layout.is_heading(short, BODY)
    assert not layout.is_heading(short, 0)  # 글자가 없는 문서


def test_figures_go_before_the_first_paragraph_below_them():
    raw = RawPage(
        REPORT, [PageImage(856, 490, 500), PageImage(300, 200, 800), PageImage(64, 64, 10)]
    )
    [page] = layout.build_pages({1: raw}).values()
    slots = [(f.width, f.slot, f.heading) for f in page.images]
    assert slots == [
        (300, 0, None),  # 맨 위: 첫 문단 앞, 아직 제목이 없다
        (856, 3, "Annual Report > 2. Quarterly sales"),  # "2. Quarterly sales" 아래
        (64, 3, "Annual Report > 2. Quarterly sales"),  # 맨 아래: 마지막 뒤
    ]


def test_figures_are_matched_to_slots_by_page_and_size():
    def slot(w, h, at):
        return layout.FigureSlot(w, h, at, None)

    pages = {
        1: layout.PdfPage([], [slot(800, 400, 0), slot(300, 300, 1)], None, ""),
        2: layout.PdfPage([], [slot(800, 400, 0)], None, ""),
    }

    def fig(page, w, h):
        return SimpleNamespace(page_number=page, width=w, height=h)

    small, big, same_size_2, unknown, scanned = (
        fig(1, 300, 300),
        fig(1, 800, 400),
        fig(2, 800, 400),
        fig(1, 640, 480),
        fig(3, 800, 400),
    )
    slots = layout.figure_slots([small, big, same_size_2, unknown, scanned], pages)
    assert {k: v.slot for k, v in slots.items()} == {id(small): 1, id(big): 0, id(same_size_2): 0}


def test_read_pages_from_a_real_pdf():
    assert layout.read_pages(b"%PDF-1.4 garbage") is None
    pages = layout.read_pages(text_pdf(["Hello page one", "Second page"]))
    assert [(p.text, p.blocks) for p in pages.values()] == [
        ("Hello page one", [PdfBlock("Hello page one")]),
        ("Second page", [PdfBlock("Second page")]),
    ]

    pdf = layout_pdf(
        [
            (72, 720, "Report Title", 18),
            (72, 690, "Intro line one", 11),
            (72, 676, "Intro line two", 11),
            (72, 640, "Second paragraph", 11),
            (72, 300, "Text below the chart", 11),
        ],
        image=(72, 400, 300, 200, 64, 32),  # 세로 400~600: 둘째 문단과 아래 문장 사이
    )
    [page] = layout.read_pages(pdf).values()
    assert page.blocks == [
        PdfBlock("Intro line one\nIntro line two", "Report Title"),
        PdfBlock("Second paragraph", "Report Title"),
        PdfBlock("Text below the chart", "Report Title"),
    ]
    assert [(f.width, f.height, f.slot) for f in page.images] == [(64, 32, 2)]
