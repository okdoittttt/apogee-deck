"""텍스트 형식 수정 로직: 원본 형태 보존, 수정 적용 규칙, CSV 열 수 검사, 줄 번호 보기."""

import pytest

from app.documents.editing.text import (
    BOM,
    SNIPPET_CHARS,
    Edit,
    EditError,
    TextShape,
    apply_edits,
    apply_some,
    check_csv,
    decode,
    encode,
    numbered_lines,
    snippets,
)
from app.documents.formats import DocumentKind
from app.documents.parser import parse

# --- 원본 형태 (BOM, 줄바꿈) ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        b"hello\nworld\n",
        b"hello\nworld",  # 마지막 줄바꿈 없음
        b"hello\r\nworld\r\n",
        b"hello\r\nworld",
        BOM + b"hello\r\nworld\r\n",
        BOM + b"hello\n",
        b"no newline",
        "한글 문서\n이모지 😀\n".encode(),
        b"lone\rcarriage\nreturn\n",  # \r 단독은 파서처럼 그대로
        b"a\r\r\nb\r\n",
    ],
)
def test_decode_encode_round_trip(data):
    text, shape = decode(data)
    assert encode(text, shape) == data


@pytest.mark.parametrize(
    "data",
    [b"a\nb\n", b"a\r\nb\r\n", BOM + b"a\r\nb", "한글\r\n😀".encode(), b"x\ry\n"],
)
def test_decode_matches_parser_content(data):
    text, _ = decode(data)
    assert text == parse(data, DocumentKind.TXT, "a.txt", max_pages=10).content


def test_decode_remembers_shape():
    assert decode(b"a\nb")[1] == TextShape(bom=False, newline="\n")
    assert decode(BOM + b"a\r\nb")[1] == TextShape(bom=True, newline="\r\n")
    assert decode(BOM + b"a\r\nb")[0] == "a\nb"


def test_decode_rejects_mixed_newlines():
    with pytest.raises(EditError, match="mixes CRLF and LF"):
        decode(b"a\r\nb\nc\r\n")


def test_decode_rejects_non_utf8():
    with pytest.raises(EditError, match="UTF-8"):
        decode("한글".encode("cp949"))


def test_edit_keeps_crlf_bom_and_untouched_bytes():
    data = BOM + "제목\r\n첫 줄 오타\r\n마지막 줄".encode()
    text, shape = decode(data)
    edited, _ = apply_edits(text, [Edit("오타", "수정")])
    assert encode(edited, shape) == BOM + "제목\r\n첫 줄 수정\r\n마지막 줄".encode()


def test_new_text_with_newlines_follows_original_newline():
    text, shape = decode(b"a\r\nb\r\n")
    # 에이전트가 \n 이나 \r\n 어느 쪽으로 줄을 넣어도 원본 줄바꿈으로 저장된다.
    edited, _ = apply_edits(text, [Edit("a\n", "a\nx\n"), Edit("b\r\n", "b\r\ny\r\n")])
    assert encode(edited, shape) == b"a\r\nx\r\nb\r\ny\r\n"


# --- 수정 적용 ----------------------------------------------------------------------------------


def test_apply_single_edit_and_change_summary():
    edited, changes = apply_edits("one\ntwo\nthree\n", [Edit("two", "2")])
    assert edited == "one\n2\nthree\n"
    assert len(changes) == 1
    change = changes[0]
    assert (change.index, change.line, change.count, change.before, change.after) == (
        1,
        2,
        1,
        "two",
        "2",
    )


def test_apply_edits_in_order_on_updated_text():
    # 두 번째 수정은 첫 번째 수정이 만든 글자를 찾는다.
    edited, changes = apply_edits("a b c", [Edit("b", "B!"), Edit("B!", "done")])
    assert edited == "a done c"
    assert [c.index for c in changes] == [1, 2]


def test_insert_and_delete_with_surrounding_text():
    edited, _ = apply_edits("first\nthird\n", [Edit("first\n", "first\nsecond\n")])
    assert edited == "first\nsecond\nthird\n"
    edited, _ = apply_edits(edited, [Edit("second\n", "")])
    assert edited == "first\nthird\n"


def test_not_found():
    with pytest.raises(EditError, match="Edit 1: old_text was not found") as info:
        apply_edits("hello", [Edit("bye", "hi")])
    assert info.value.index == 1


def test_not_found_hints_line_prefix():
    with pytest.raises(EditError, match=r"line number prefixes such as \[L12\]"):
        apply_edits("hello\n", [Edit("[L1] hello", "hi")])


def test_more_than_once_needs_more_context():
    with pytest.raises(EditError, match="appears more than once"):
        apply_edits("2024년\n2024년\n", [Edit("2024", "2025")])


def test_overlapping_occurrences_count_as_more_than_once():
    with pytest.raises(EditError, match="appears more than once"):
        apply_edits("aaa", [Edit("aa", "b")])


def test_replace_all():
    edited, changes = apply_edits("x 2024\ny 2024\n", [Edit("2024", "2025", replace_all=True)])
    assert edited == "x 2025\ny 2025\n"
    assert (changes[0].line, changes[0].count) == (1, 2)


def test_replace_all_lists_every_line():
    _, changes = apply_edits("a\nb 2024\nc 2024 2024\n", [Edit("2024", "X", replace_all=True)])
    assert changes[0].places == (2, 3, 3) and changes[0].line == 2


def test_replace_all_changes_addresses_too_and_points_them_out():
    text = "old.com 소개\n주소 https://old.com/a 와 www.old.com\n"
    edited, changes = apply_edits(text, [Edit("old.com", "new.com", replace_all=True)])
    assert edited == "new.com 소개\n주소 https://new.com/a 와 www.new.com\n"
    assert (changes[0].count, changes[0].in_urls) == (3, (2, 2))


def test_address_ends_before_korean_text():
    text = "자료는 https://a.com/x에서openwebui 설명"
    _, changes = apply_edits(text, [Edit("openwebui", "X", replace_all=True)])
    assert changes[0].in_urls == ()


def test_no_address_warning_when_old_text_is_an_address():
    text = "https://docs.a.com/1 https://docs.a.com/2"
    edits = [Edit("https://docs.a.com", "https://docs.b.com", replace_all=True)]
    _, changes = apply_edits(text, edits)
    assert changes[0].in_urls == ()


def test_snippets_start_near_the_first_difference():
    old = "가" * 100 + ". 나머지"
    before, after = snippets(old, "가" * 100 + ".\n나머지")
    assert before.startswith("…") and after.startswith("…")
    assert before != after and len(before) <= SNIPPET_CHARS
    assert snippets("짧은 글", "짧은 말") == ("짧은 글", "짧은 말")


def test_replace_all_still_needs_a_match():
    with pytest.raises(EditError, match="not found"):
        apply_edits("hello", [Edit("2024", "2025", replace_all=True)])


def test_one_failure_cancels_all():
    original = "alpha\nbeta\n"
    with pytest.raises(EditError, match="Edit 2") as info:
        apply_edits(original, [Edit("alpha", "ALPHA"), Edit("gamma", "GAMMA")])
    assert info.value.index == 2


@pytest.mark.parametrize(
    ("edits", "message"),
    [
        ([], "No edits"),
        ([Edit("", "x")], "old_text is empty"),
        ([Edit("same", "same")], "are the same"),
        ([Edit("a\r\n", "a\n")], "are the same"),  # 줄바꿈만 다른 것도 바뀌는 게 없다
    ],
)
def test_rejects_useless_edits(edits, message):
    with pytest.raises(EditError, match=message):
        apply_edits("same\na\n", edits)


def test_rejects_empty_result():
    with pytest.raises(EditError, match="empty"):
        apply_edits("only\n", [Edit("only", " ")])


def test_korean_and_emoji():
    edited, changes = apply_edits("매출 😀 120억\n", [Edit("120억", "135억")])
    assert edited == "매출 😀 135억\n"
    assert changes[0].before == "120억"


def test_change_snippets_are_shortened():
    long = "가" * (SNIPPET_CHARS + 20)
    _, changes = apply_edits(long, [Edit(long, "짧게")])
    assert len(changes[0].before) == SNIPPET_CHARS
    assert changes[0].before.endswith("…")


# --- CSV 열 수 검사 ------------------------------------------------------------------------------

CSV = 'name,city,price\n"Kim","서울, 강남",1000\nLee,부산,2000\n'


def test_csv_value_edit_passes():
    edited, _ = apply_edits(CSV, [Edit("2000", "2500")])
    check_csv(CSV, edited)


def test_csv_row_insert_and_delete_pass():
    check_csv(CSV, CSV + "Park,대구,3000\n")
    check_csv(CSV, CSV.replace("Lee,부산,2000\n", ""))


def test_csv_broken_quote_is_rejected():
    edited, _ = apply_edits(CSV, [Edit('"서울, 강남"', "서울, 강남")])
    with pytest.raises(EditError, match="line 2 has 4 columns, but every row should have 3"):
        check_csv(CSV, edited)


def test_csv_unbalanced_quote_is_rejected():
    # 느슨하게 읽으면 ['Kim,서울', ' 강남"', '1000'] 으로 열 수가 맞아 보인다.
    edited, _ = apply_edits(CSV, [Edit('"Kim"', '"Kim')])
    with pytest.raises(EditError, match="cannot be read"):
        check_csv(CSV, edited)


def test_csv_unterminated_quote_is_rejected():
    with pytest.raises(EditError, match="cannot be read"):
        check_csv("a,b\nx,1\nc,d\n", 'a,b\n"x,1\nc,d\n')


def test_csv_loose_original_is_still_editable():
    # 원본부터 따옴표가 느슨하면(엄격한 규칙으로 못 읽으면) 느슨한 규칙으로 열 수만 본다.
    original = 'a,b\nx"y,1\n'
    check_csv(original, original.replace("1", "2"))
    with pytest.raises(EditError, match="columns"):
        check_csv(original, original + "c,d,e\n")


def test_csv_quoted_newline_counts_as_one_row():
    original = 'a,b\n"line1\nline2",x\nc,d\n'
    edited = original + "e,f,g\n"
    with pytest.raises(EditError, match="line 5 has 3 columns"):
        check_csv(original, edited)


def test_csv_ignores_blank_lines():
    check_csv("a,b\n1,2\n", "a,b\n\n1,2\n\n")


def test_csv_check_skipped_when_original_is_ragged():
    check_csv("a,b\n1,2,3\n", "a\n1,2,3,4\n")


# --- 줄 번호 보기 --------------------------------------------------------------------------------


def test_numbered_lines():
    view = numbered_lines("a\nb\nc\n", 1, 10)
    assert view.text == "[L1] a\n[L2] b\n[L3] c"
    assert (view.first, view.last, view.total) == (1, 3, 3)


def test_numbered_lines_range_and_no_trailing_newline():
    view = numbered_lines("a\nb\nc\nd", 2, 2)
    assert view.text == "[L2] b\n[L3] c"
    assert (view.first, view.last, view.total) == (2, 3, 4)


def test_numbered_lines_keeps_blank_lines():
    assert numbered_lines("a\n\nb\n", 1, 10).text == "[L1] a\n[L2] \n[L3] b"


def test_numbered_lines_out_of_range():
    view = numbered_lines("a\nb\n", 5, 10)
    assert view.text == ""
    assert (view.first, view.last, view.total) == (5, 4, 2)


def test_numbered_lines_clamps_start():
    assert numbered_lines("a\nb\n", 0, 1).text == "[L1] a"


def test_numbered_lines_stops_at_char_budget():
    view = numbered_lines("aaaa\nbbbb\ncccc\n", 1, 10, max_chars=20)
    # "[L1] aaaa" (9) + "\n[L2] bbbb" (10) = 19, 세 번째 줄은 넘는다
    assert view.text == "[L1] aaaa\n[L2] bbbb"
    assert (view.first, view.last, view.total) == (1, 2, 3)


def test_numbered_lines_always_shows_first_line():
    view = numbered_lines("x" * 50 + "\nb\n", 1, 10, max_chars=10)
    assert view.text == "[L1] " + "x" * 50
    assert view.last == 1


def test_edit_error_is_tool_error():
    from app.core.errors import ToolError

    assert issubclass(EditError, ToolError)


def test_apply_some_skips_failed_edits():
    edited, changes, failures = apply_some(
        "a b c", [Edit("a", "A"), Edit("z", "Z"), Edit("c", "C")]
    )
    assert edited == "A b C"
    assert [c.index for c in changes] == [1, 3]
    assert [f.index for f in failures] == [2]


def test_other_ops_only_for_word():
    _, changes, (failure,) = apply_some("a", [Edit(op="delete_paragraph", paragraph="p1")])
    assert changes == [] and "only works for Word documents" in str(failure)
