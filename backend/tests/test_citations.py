"""출처 번호 검증: 한 턴 안에서 이어지는 번호, 같은 출처의 번호 재사용, 지난 결과 줄이기."""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.core.citations import source_marker
from app.orchestration.citations import number_sources
from app.orchestration.context import compact_old_tool_results


def _web(call_id, *urls):
    text = "\n".join(f"{source_marker(i)} 제목 {u}" for i, u in enumerate(urls, 1))
    artifact = {"sources": [{"title": f"제목 {u}", "url": u, "published_date": None} for u in urls]}
    return ToolMessage(text, tool_call_id=call_id, name="web_search", artifact=artifact)


def _docs(call_id, *chunks):
    text = "\n".join(f"{source_marker(i)} 청크 {c}" for i, c in enumerate(chunks, 1))
    artifact = {
        "sources": [
            {"type": "document", "title": "보고서.pdf", "location": "p.1", "chunk_id": c}
            for c in chunks
        ]
    }
    return ToolMessage(text, tool_call_id=call_id, name="search_project", artifact=artifact)


def _numbers(message):
    return [s["n"] for s in message.artifact["sources"]]


def test_parallel_results_get_consecutive_numbers():
    history = [HumanMessage("질문"), AIMessage("", tool_calls=[])]
    first, second = number_sources(
        history, [_web("a", "https://a", "https://b"), _docs("b", "c1", "c2")]
    )
    assert _numbers(first) == [1, 2]
    assert _numbers(second) == [3, 4]
    assert first.content == "[1] 제목 https://a\n[2] 제목 https://b"
    assert second.content == "[3] 청크 c1\n[4] 청크 c2"


def test_later_search_in_the_same_turn_continues_and_reuses_numbers():
    history = [HumanMessage("질문")]
    [first] = number_sources(history, [_docs("a", "c1", "c2")])
    history += [AIMessage("", tool_calls=[]), first, AIMessage("", tool_calls=[])]
    [again] = number_sources(history, [_docs("b", "c2", "c3")])
    # 이미 나온 청크(c2)는 앞의 번호, 새 청크는 이어서
    assert _numbers(again) == [2, 3]
    assert again.content == "[2] 청크 c2\n[3] 청크 c3"


def test_new_turn_starts_from_one():
    [old] = number_sources([HumanMessage("첫 질문")], [_web("a", "https://a", "https://b")])
    history = [HumanMessage("첫 질문"), old, AIMessage("답 [1]"), HumanMessage("다음 질문")]
    [new] = number_sources(history, [_web("b", "https://b")])
    assert _numbers(new) == [1]


def test_messages_without_sources_and_stray_markers():
    plain = ToolMessage("지금은 10시", tool_call_id="t", name="get_current_time")
    stray = ToolMessage(
        f"{source_marker(1)} 제목 / {source_marker(5)} 없는 출처",
        tool_call_id="w",
        name="web_search",
        artifact={"sources": [{"title": "제목", "url": "https://a"}]},
    )
    out_plain, out_stray = number_sources([HumanMessage("질문")], [plain, stray])
    assert out_plain is plain
    assert out_stray.content == "[1] 제목 /  없는 출처"  # 없는 출처를 가리키는 자리 표시는 지운다
    assert "n" not in stray.artifact["sources"][0]  # 원본은 바꾸지 않는다
    assert out_stray.id == stray.id


def test_compacted_old_results_keep_their_numbers():
    [docs] = number_sources([HumanMessage("첫 질문")], [_docs("t1", "c1")])
    [web] = number_sources([HumanMessage("첫 질문"), docs], [_web("t2", "https://a")])
    history = [
        HumanMessage("첫 질문"),
        AIMessage("", tool_calls=[{"id": "t1", "name": "search_project", "args": {"query": "q"}}]),
        docs.model_copy(update={"content": docs.content * 200}),
        AIMessage("", tool_calls=[{"id": "t2", "name": "web_search", "args": {"query": "q"}}]),
        web.model_copy(update={"content": web.content * 200}),
        AIMessage("답 [1][2]"),
        HumanMessage("다음 질문"),
    ]
    compacted = compact_old_tool_results(history)
    assert "[1] 보고서.pdf · p.1" in compacted[2].text
    assert "[2] 제목 https://a - https://a" in compacted[4].text
