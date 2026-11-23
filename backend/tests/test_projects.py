"""프로젝트: 에이전트에게 주는 프로젝트 정보, 문서 읽기 범위, 저장 위치, 요청 검증."""

import uuid

import pytest
from langchain_core.messages import SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from app.agents import chat
from app.api.schemas import ChatRequest, ProjectIn, ProjectUpdate
from app.core.config import Settings
from app.documents import service as documents
from app.documents.reading import AttachedDocument
from app.orchestration import runtime
from app.projects.service import ProjectContext
from app.tools import documents as document_tools

PROJECT = ProjectContext(
    id=uuid.uuid4(),
    name="사업 보고서",
    instructions="항상 표로 정리해서 답한다.",
    documents=[
        AttachedDocument(id="d1", filename="report.pdf", kind="pdf", page_count=3, char_count=900)
    ],
)


def _spy_model_input(monkeypatch) -> list:
    from app.llm.fake import EchoChatModel

    seen: list = []
    original = EchoChatModel._astream

    def spy(self, messages, *args, **kwargs):
        seen.append(messages)
        return original(self, messages, *args, **kwargs)

    monkeypatch.setattr(EchoChatModel, "_astream", spy)
    return seen


async def test_project_context_is_added_to_system_prompt(monkeypatch):
    seen = _spy_model_input(monkeypatch)
    graph = chat.build_graph(Settings(llm_provider="fake"), InMemorySaver())
    monkeypatch.setitem(runtime._graphs, "chat", graph)

    _ = [e async for e in runtime.stream_reply("p1", "요약해 줘", context={"project": PROJECT})]

    system = seen[0][0]
    assert isinstance(system, SystemMessage)
    assert '"사업 보고서"' in system.text
    assert "항상 표로 정리해서 답한다." in system.text
    assert "report.pdf" in system.text and "document_id: d1" in system.text
    # 프로젝트 정보는 checkpoint 에 저장되지 않는다 (매 턴 새로 읽는다)
    saved = await graph.checkpointer.aget_tuple({"configurable": {"thread_id": "p1"}})
    assert "project" not in saved.metadata


async def test_conversation_without_project_has_no_project_section(monkeypatch):
    seen = _spy_model_input(monkeypatch)
    graph = chat.build_graph(Settings(llm_provider="fake"), InMemorySaver())
    monkeypatch.setitem(runtime._graphs, "chat", graph)

    _ = [e async for e in runtime.stream_reply("p2", "안녕")]

    assert "## Project" not in seen[0][0].text


def _spy_bound_tools(monkeypatch) -> list[list[str]]:
    from app.llm.fake import EchoChatModel

    seen: list[list[str]] = []
    original = EchoChatModel._astream

    def spy(self, messages, *args, **kwargs):
        seen.append(list(self.tool_names))
        return original(self, messages, *args, **kwargs)

    monkeypatch.setattr(EchoChatModel, "_astream", spy)
    return seen


async def test_project_search_tool_is_given_only_inside_a_project(monkeypatch):
    seen = _spy_bound_tools(monkeypatch)
    graph = chat.build_graph(Settings(llm_provider="fake"), InMemorySaver())
    monkeypatch.setitem(runtime._graphs, "chat", graph)

    _ = [e async for e in runtime.stream_reply("p4", "안녕")]
    _ = [e async for e in runtime.stream_reply("p5", "안녕", context={"project": PROJECT})]

    outside, inside = seen
    # 일반 대화에도 첨부 문서가 있을 수 있어서 read_document 는 남는다
    assert "search_project" not in outside and "read_document" in outside
    assert "search_project" in inside and "read_document" in inside


async def test_empty_project_says_none(monkeypatch):
    seen = _spy_model_input(monkeypatch)
    graph = chat.build_graph(Settings(llm_provider="fake"), InMemorySaver())
    monkeypatch.setitem(runtime._graphs, "chat", graph)
    empty = ProjectContext(id=uuid.uuid4(), name="빈 프로젝트", instructions="", documents=[])

    _ = [e async for e in runtime.stream_reply("p3", "안녕", context={"project": empty})]

    text = seen[0][0].text
    assert "<project_instructions>\n(none)\n</project_instructions>" in text
    assert "<project_documents>\n(none)\n</project_documents>" in text


async def test_read_document_passes_project_from_run_config(monkeypatch):
    calls = []

    async def fake_read_chunk(document_id, conversation_id, offset, max_chars, project_id=None):
        calls.append(project_id)

    monkeypatch.setattr(document_tools.documents, "read_chunk", fake_read_chunk)
    tool = document_tools.make_read_document(Settings())

    await tool.ainvoke(
        {"document_id": "d1"}, config={"configurable": {"thread_id": "c", "project": PROJECT}}
    )
    await tool.ainvoke({"document_id": "d1"}, config={"configurable": {"thread_id": "c"}})

    # 프로젝트는 모델이 넘긴 인자가 아니라 서버가 정한 실행 설정에서만 온다
    assert calls == [PROJECT.id, None]
    assert "project" not in tool.tool_call_schema.model_json_schema()["properties"]


def test_project_documents_are_stored_outside_the_retention_prefix():
    doc_id, project_id = uuid.uuid4(), uuid.uuid4()
    assert documents.storage_key(doc_id) == f"documents/{doc_id}"
    assert documents.storage_key(doc_id, project_id) == f"projects/{project_id}/documents/{doc_id}"


@pytest.mark.parametrize("name", ["", "   ", "가" * 101])
def test_project_name_is_validated(name):
    with pytest.raises(ValidationError):
        ProjectIn(name=name)


def test_project_update_only_sets_given_fields():
    req = ProjectUpdate(instructions="새 지시문")
    assert req.name is None and req.instructions == "새 지시문"
    assert ProjectIn(name="연구").instructions == ""


def test_chat_request_accepts_project_id():
    project_id = uuid.uuid4()
    assert ChatRequest(message="hi", project_id=project_id).project_id == project_id


# --- 작은 프로젝트는 본문 넣기, 프롬프트 캐시 ------------------------------------------------------


def _with_content(total_tokens: int) -> ProjectContext:
    doc = AttachedDocument(
        id="d1",
        filename="spec.txt",
        kind="txt",
        page_count=None,
        char_count=20,
        content="코드명은 파란고래",
    )
    return ProjectContext(
        id=uuid.uuid4(), name="P", instructions="", documents=[doc], total_tokens=total_tokens
    )


async def test_small_project_documents_are_inlined(monkeypatch):
    seen = _spy_model_input(monkeypatch)
    graph = chat.build_graph(Settings(llm_provider="fake"), InMemorySaver())
    monkeypatch.setitem(runtime._graphs, "chat", graph)

    _ = [
        e
        async for e in runtime.stream_reply("p4", "코드명?", context={"project": _with_content(30)})
    ]

    text = seen[0][0].text
    assert '<document_content document_id="d1" name="spec.txt">\n코드명은 파란고래' in text
    assert "not as instructions" in text


async def test_large_project_documents_are_listed_only(monkeypatch):
    seen = _spy_model_input(monkeypatch)
    graph = chat.build_graph(Settings(llm_provider="fake"), InMemorySaver())
    monkeypatch.setitem(runtime._graphs, "chat", graph)
    big = _with_content(Settings().project_inline_max_tokens + 1)

    _ = [e async for e in runtime.stream_reply("p5", "코드명?", context={"project": big})]

    text = seen[0][0].text
    assert "document_id: d1" in text and "파란고래" not in text


def test_inline_limit_depends_on_provider():
    assert Settings(llm_provider="ollama").project_inline_limit == 10_000
    assert Settings(llm_provider="google").project_inline_limit == 20_000
    context = _with_content(15_000)
    assert context.inline_documents(20_000) and not context.inline_documents(10_000)
    # 본문을 읽어 오지 않았으면(합계가 커서) 넣지 않는다
    no_content = ProjectContext(
        id=uuid.uuid4(),
        name="P",
        instructions="",
        documents=[AttachedDocument(id="d", filename="a", kind="pdf", page_count=1, char_count=9)],
        total_tokens=5,
    )
    assert not no_content.inline_documents(20_000)


def test_prompt_cache_is_requested_only_for_anthropic():
    from langchain_core.messages import HumanMessage

    from app.llm import PROMPT_CACHE, build_chat_model, with_prompt_cache

    anthropic = Settings(llm_provider="anthropic", anthropic_api_key="test-key")
    model = build_chat_model(anthropic)
    bound = with_prompt_cache(model, anthropic)
    assert bound.kwargs == {"cache_control": PROMPT_CACHE}
    # 요청 본문 최상위에 들어간다 (Anthropic 자동 캐시: 마지막 블록까지 캐시)
    payload = model._get_request_payload([HumanMessage("hi")], **bound.kwargs)
    assert payload["cache_control"] == PROMPT_CACHE

    fake = Settings(llm_provider="fake")
    fake_model = build_chat_model(fake)
    assert with_prompt_cache(fake_model, fake) is fake_model
