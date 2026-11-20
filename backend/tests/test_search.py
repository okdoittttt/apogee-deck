"""프로젝트 문서 검색: 청크 나누기, 임베딩, 키워드 토큰, 결과 합치기(RRF), 검색 도구, 출처."""

import math
import uuid

import pytest
from langchain_core.messages import ToolMessage

from app.api.schemas import ToolStep
from app.core.config import Settings
from app.documents import chunking, keywords
from app.documents.reading import estimate_tokens
from app.llm import embeddings
from app.orchestration.events import tool_sources
from app.projects import search
from app.projects.service import ProjectContext
from app.tools import build_tools
from app.tools import documents as document_tools

SIZES = chunking.ChunkSizes(target=60, max_tokens=100, overlap=15)


def _el(content, kind="text", page=None, heading=None):
    return chunking.SourceElement(uuid.uuid4(), kind, content, page, heading)


def test_small_text_under_same_heading_is_grouped():
    chunks = chunking.make_chunks(
        [
            _el("첫 문단입니다.", page=1, heading="1장"),
            _el("둘째 문단입니다.", page=2, heading="1장"),
            _el("다른 제목의 문단.", page=2, heading="2장"),
        ],
        SIZES,
    )
    assert [(c.kind, c.heading, c.page_start, c.page_end) for c in chunks] == [
        ("text", "1장", 1, 2),
        ("text", "2장", 2, 2),
    ]
    assert chunks[0].content == "첫 문단입니다.\n\n둘째 문단입니다."


def test_text_is_not_grouped_past_target():
    sentence = "가" * 60  # 40 토큰
    chunks = chunking.make_chunks([_el(sentence), _el(sentence)], SIZES)
    assert len(chunks) == 2


def test_long_text_is_split_with_overlap_and_stays_under_max():
    text = " ".join(f"문장{i}은 이렇게 끝난다." for i in range(80))
    chunks = chunking.make_chunks([_el(text, page=3, heading="본문")], SIZES)
    assert len(chunks) > 3
    assert all(c.token_count <= SIZES.max_tokens for c in chunks)
    assert all((c.page_start, c.heading) == (3, "본문") for c in chunks)
    # 다음 청크는 앞 청크의 끝부분으로 시작한다
    first_sentence = chunks[1].content.split(". ")[0] + "."
    assert chunks[0].content.endswith(first_sentence) or f"{first_sentence} " in chunks[0].content
    assert first_sentence != chunks[0].content.split(". ")[0] + "."


def test_text_without_spaces_is_hard_split():
    parts = chunking.split_text("가" * 400, SIZES)
    assert len(parts) > 1
    assert all(estimate_tokens(p) <= SIZES.max_tokens for p in parts)


def test_large_table_is_split_by_rows_with_header():
    rows = "\n".join(f"| {i} | 제품{i} 설명 |" for i in range(60))
    table = f"| 번호 | 제품 |\n| --- | --- |\n{rows}"
    chunks = chunking.make_chunks([_el(table, kind="table", page=2)], SIZES)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.kind == "table"
        assert chunk.content.startswith("| 번호 | 제품 |\n| --- | --- |\n| ")
        assert chunk.token_count <= SIZES.max_tokens
    body = [line for c in chunks for line in c.content.splitlines()[2:]]
    assert len(body) == 60  # 행은 빠지거나 겹치지 않는다


def test_chunks_remember_their_source_elements():
    a, b = _el("첫 문단입니다.", page=1), _el("둘째 문단입니다.", page=1)
    long_text = _el(" ".join(f"문장{i}은 이렇게 끝난다." for i in range(80)), page=2)
    rows = "\n".join(f"| {i} | 제품{i} 설명 |" for i in range(60))
    table = _el(f"| 번호 | 제품 |\n| --- | --- |\n{rows}", kind="table", page=3)
    image = _el("그림 설명", kind="image", page=3)
    chunks = chunking.make_chunks([a, b, long_text, table, image], SIZES)

    assert chunks[0].element_ids == (a.id, b.id)  # 묶은 본문은 묶인 요소 모두
    by_kind = {}
    for chunk in chunks[1:]:
        by_kind.setdefault(chunk.kind, set()).update(chunk.element_ids)
    assert by_kind["text"] == {long_text.id}  # 나눈 본문 조각은 모두 원래 요소
    assert by_kind["table"] == {table.id}
    assert by_kind["image"] == {image.id}


def test_image_becomes_its_own_chunk_with_element_id():
    image = _el("막대 차트: 서울 120", kind="image", page=4, heading="1장")
    chunks = chunking.make_chunks([_el("본문", heading="1장"), image, _el("  ")], SIZES)
    assert [c.kind for c in chunks] == ["text", "image"]
    assert chunks[1].image_element_id == image.id


def test_embedding_text_has_document_context():
    chunk = chunking.Chunk("table", "| a |", 2, 3, "1장 > 1.1", None, 1)
    assert chunking.embedding_text("보고서.pdf", chunk) == (
        "문서: 보고서.pdf | 제목: 1장 > 1.1 | 쪽: 2-3 | 표\n\n| a |"
    )


def _cosine(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_hashing_embeddings_are_deterministic_and_similarity_based():
    model = embeddings.HashingEmbeddings(256)
    seoul, busan, query = model.embed_documents(["서울 매출 120억", "부산 인구 통계", "서울 매출"])
    assert math.isclose(_cosine(seoul, seoul), 1.0)
    assert _cosine(query, seoul) > _cosine(query, busan)
    assert model.embed_query("서울 매출") == query


def test_build_embeddings_by_provider():
    fake = Settings(embedding_provider="fake", embedding_dimensions=8)
    assert isinstance(embeddings.build_embeddings(fake), embeddings.HashingEmbeddings)
    assert embeddings.model_id(fake) == "fake:hashing:8"
    with pytest.raises(embeddings.EmbeddingUnavailableError):
        embeddings.build_embeddings(Settings(embedding_provider="openai", openai_api_key=""))
    model = embeddings.build_embeddings(
        Settings(embedding_provider="openai", openai_api_key="sk-x")
    )
    assert model.model == "text-embedding-3-small"
    assert model.dimensions == 1536
    assert model.check_embedding_ctx_length is False  # tiktoken 파일을 내려받지 않는다


def test_search_tool_is_available_without_openai_key():
    # 임베딩을 쓸 수 없어도 키워드 검색으로 찾는다
    for settings in (Settings(openai_api_key=""), Settings(embedding_provider="fake")):
        assert [t.name for t in build_tools(settings, ["search_project"])] == ["search_project"]


def test_keyword_tokens_split_korean_into_bigrams():
    assert keywords.keyword_tokens("부산의 매출은 85억, GPT-6") == [
        "부산의",
        "부산",
        "산의",
        "매출은",
        "매출",
        "출은",
        "85",
        "억",
        "gpt",
        "6",
    ]
    # 조사가 붙은 낱말도 검색어의 두 글자 조각으로 만난다
    assert "부산" in keywords.keyword_tokens("부산광역시")
    assert keywords.keyword_query("부산 매출, 부산!") == "부산 | 매출"
    assert keywords.keyword_query("?! ...") is None


def test_keyword_tokens_never_contain_tsquery_syntax():
    tokens = keywords.keyword_tokens("a & b | !c <-> 'd':* (e) 한:*글")
    assert all(t.isalnum() for t in tokens)


def test_fuse_rewards_chunks_found_by_both_searches():
    a, b, c, d = (uuid.uuid4() for _ in range(4))
    fused = search.fuse([[a, b, c], [c, d]], k=60)
    # b 와 d 는 둘 다 2위라 점수가 같다: 먼저 나온 검색(벡터)의 결과가 앞
    assert [chunk_id for chunk_id, _ in fused] == [c, a, b, d]
    assert math.isclose(dict(fused)[c], 1 / 63 + 1 / 61)
    # 한쪽 검색만 있어도 그 순위를 그대로 따른다 (임베딩을 못 쓸 때)
    assert [i for i, _ in search.fuse([[], [d, a]], k=60)] == [d, a]
    assert search.fuse([[], []], k=60) == []


def _hit(**kw):
    values = {
        "chunk_id": uuid.uuid4(),
        "document_id": uuid.uuid4(),
        "filename": "보고서.pdf",
        "kind": "text",
        "page_start": 3,
        "page_end": 3,
        "heading": "1장",
        "content": "서울 매출은 120억 원이다.",
        "image_element_id": None,
        "score": 0.8,
    }
    return search.SearchHit(**(values | kw))


def test_format_hits_shows_document_location_and_kind():
    text = search.format_hits("서울 매출", [_hit(kind="image", page_end=4)])
    assert "[1] document: 보고서.pdf (document_id: " in text
    assert "pages 3-4 | section: 1장 | description of an image" in text
    assert "서울 매출은 120억 원이다." in text
    assert "No passages found" in search.format_hits("x", [])


def test_sources_are_deduplicated_and_validated_for_the_screen():
    project_id = uuid.uuid4()
    hit = _hit(image_element_id=uuid.uuid4())
    found = search.sources(project_id, [hit, hit, _hit(page_start=None, heading=None)])
    assert len(found) == 2
    assert found[0]["location"] == "p.3 · 1장"
    assert found[1]["location"] == ""

    shown = tool_sources(
        {
            "sources": [
                *found,
                {"type": "document", "title": "x", "document_id": "../etc", "project_id": "p"},
                {"title": "웹", "url": "https://example.com"},
                {"title": "나쁜", "url": "javascript:alert(1)"},
            ]
        }
    )
    assert [s.get("type") for s in shown] == ["document", "document", None]
    assert shown[0]["image_element_id"] == str(hit.image_element_id)
    assert shown[0]["chunk_id"] == str(hit.chunk_id)  # 문서 보기 패널에서 근거 위치를 강조한다
    # 저장한 단계를 다시 읽을 때 두 종류 모두 스키마를 통과한다
    step = ToolStep(
        id="t",
        name="search_project",
        label="",
        done_label="",
        args={},
        status="done",
        sources=shown,
    )
    assert [type(s).__name__ for s in step.sources] == [
        "DocumentSource",
        "DocumentSource",
        "Source",
    ]
    assert step.sources[0].chunk_id == str(hit.chunk_id)


PROJECT = ProjectContext(id=uuid.uuid4(), name="보고서", instructions="", documents=[])


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def test_search_tool_searches_only_the_server_given_project(monkeypatch):
    calls = []

    async def fake_search(session, settings, project_id, query, top_k):
        calls.append((project_id, query, top_k))
        return [_hit()]

    monkeypatch.setattr(document_tools, "SessionLocal", _Session)
    monkeypatch.setattr(document_tools.search, "search", fake_search)
    tool = document_tools.make_search_project(Settings(project_search_top_k=5))
    assert "project" not in tool.tool_call_schema.model_json_schema()["properties"]

    call = {
        "name": "search_project",
        "args": {"query": "서울 매출"},
        "id": "1",
        "type": "tool_call",
    }
    message = await tool.ainvoke(call, config={"configurable": {"project": PROJECT}})
    assert isinstance(message, ToolMessage)
    assert "서울 매출은 120억 원이다." in message.content
    assert message.artifact["sources"][0]["project_id"] == str(PROJECT.id)
    assert calls == [(PROJECT.id, "서울 매출", 5)]

    message = await tool.ainvoke(call, config={"configurable": {}})
    assert message.content == document_tools.NOT_IN_PROJECT
    assert len(calls) == 1
