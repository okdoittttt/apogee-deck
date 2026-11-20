"""첨부 문서 읽기 도구(``read_document``)."""

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool

from app.core.config import Settings
from app.documents import reading
from app.documents import service as documents


def make_read_document(settings: Settings) -> BaseTool:
    """첨부 문서 읽기 도구를 만든다.

    지금 대화(``thread_id``)에 붙은 문서만 읽는다. 모델이 다른 대화의 문서 ID 를 넣어도 찾을 수
    없다고 답한다.

    Args:
        settings: 앱 설정 (한 번에 보여줄 글자 수).

    Returns:
        ``read_document`` 도구.
    """

    @tool(parse_docstring=True)
    async def read_document(document_id: str, config: RunnableConfig, offset: int = 0) -> str:
        """Read the text of a document the user attached to this conversation.

        Use it when the user asks about an attached document. The document ids are listed
        in the user's message. Long documents are returned in parts; the result tells you
        the offset to continue from. Read further parts when the answer may be in them.

        Args:
            document_id: The document_id from the attachment list in the user's message.
            config: Run configuration (injected, not visible to the model).
            offset: Character position to start from, for reading the next part of a long
                document (use the value given in the previous result). 0 for the beginning.
        """
        conversation_id = str((config.get("configurable") or {}).get("thread_id", ""))
        found = await documents.read_chunk(
            document_id, conversation_id, max(offset, 0), settings.document_read_max_chars
        )
        if found is None:
            return (
                f"No readable attached document with id {document_id!r} in this conversation. "
                "Use a document_id from the attachment list."
            )
        doc, chunk, total = found
        return reading.format_chunk(
            doc, chunk, max(offset, 0), total, settings.document_read_max_chars
        )

    return read_document
