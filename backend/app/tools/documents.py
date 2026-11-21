"""문서 도구: 첨부·프로젝트 문서 읽기(``read_document``), 프로젝트 문서 검색(``search_project``)."""

import uuid
from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool

from app.core.config import Settings
from app.core.db import SessionLocal
from app.documents import reading
from app.documents import service as documents
from app.projects import search

NOT_IN_PROJECT = (
    "This conversation is not in a project, so there are no project documents to search."
)


def make_read_document(settings: Settings) -> BaseTool:
    """첨부 문서 읽기 도구를 만든다.

    지금 대화(``thread_id``)에 붙은 문서와, 대화가 프로젝트에 속하면 그 프로젝트의 문서만 읽는다.
    프로젝트는 서버가 넘긴 실행 설정(``configurable["project"]``)에서 정한다. 모델이 다른 대화나
    프로젝트의 문서 ID 를 넣어도 찾을 수 없다고 답한다.

    Args:
        settings: 앱 설정 (한 번에 보여줄 글자 수).

    Returns:
        ``read_document`` 도구.
    """

    @tool(parse_docstring=True)
    async def read_document(document_id: str, config: RunnableConfig, offset: int = 0) -> str:
        """Read the text of a document attached to this conversation or stored in its project.

        Use it when the user asks about an attached or project document. The document ids
        are listed in the user's message (attachments) or in the project section of your
        instructions. Long documents are returned in parts; the result tells you the offset
        to continue from. Read further parts when the answer may be in them.

        Args:
            document_id: The document_id from the attachment list or the project document list.
            config: Run configuration (injected, not visible to the model).
            offset: Character position to start from, for reading the next part of a long
                document (use the value given in the previous result). 0 for the beginning.
        """
        configurable = config.get("configurable") or {}
        conversation_id = str(configurable.get("thread_id", ""))
        project = configurable.get("project")
        found = await documents.read_chunk(
            document_id,
            conversation_id,
            max(offset, 0),
            settings.document_read_max_chars,
            project_id=project.id if project is not None else None,
        )
        if found is None:
            return (
                f"No readable document with id {document_id!r} in this conversation. "
                "Use a document_id from the attachment list or the project document list."
            )
        doc, chunk, total = found
        return reading.format_chunk(
            doc, chunk, max(offset, 0), total, settings.document_read_max_chars
        )

    return read_document


def make_search_project(settings: Settings) -> BaseTool:
    """프로젝트 문서 검색 도구를 만든다.

    대화가 속한 프로젝트(서버가 넘긴 ``configurable["project"]``)의 문서만 찾는다. 결과의 출처(문서 이름,
    쪽, 제목 경로, 그림)는 artifact 로 화면에 따로 준다.

    Args:
        settings: 앱 설정 (임베딩, 돌려줄 청크 수).

    Returns:
        ``search_project`` 도구.
    """

    @tool(parse_docstring=True, response_format="content_and_artifact")
    async def search_project(
        query: str, config: RunnableConfig, document_id: str = ""
    ) -> tuple[str, dict[str, Any]]:
        """Search the documents of this conversation's project for passages relevant to a query.

        Use it when the user asks about the project documents and their full text is not
        included in your instructions. It finds passages both by meaning and by exact words
        (names, codes, numbers), including tables and descriptions of images (charts,
        diagrams). Search again with different wording when
        the results do not answer the question, and use read_document with a result's
        document_id to read the surrounding text when a passage is cut off or you need more
        context. Pass a document_id only when the user names or clearly points to one
        document (for example "in the design document"); otherwise search all documents,
        even when you can guess which document is likely, because the answer may be in
        another document or in several.

        Args:
            query: What to look for, written as a short question or the key terms, in the
                language of the documents.
            config: Run configuration (injected, not visible to the model).
            document_id: Optional. The document_id of the one project document (from the
                project document list) that the user named or clearly pointed to. Leave
                empty to search all project documents.
        """
        project = (config.get("configurable") or {}).get("project")
        if project is None:
            return NOT_IN_PROJECT, {}
        target = None
        if document_id.strip():
            target = next((d for d in project.documents if d.id == document_id.strip()), None)
            if target is None:
                return (
                    f"There is no ready document with document_id {document_id!r} in this "
                    "project. Use a document_id from the project document list, or leave "
                    "document_id empty to search all project documents."
                ), {}
        async with SessionLocal() as session:
            hits = await search.search(
                session,
                settings,
                project.id,
                query,
                settings.project_search_top_k,
                uuid.UUID(target.id) if target else None,
            )
        text = search.format_hits(query, hits, target.filename if target else None)
        return text, {"sources": search.sources(project.id, hits)}

    return search_project
