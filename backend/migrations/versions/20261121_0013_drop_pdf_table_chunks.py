"""PDF 문서의 표 청크 지우기 (데이터만)

PDF 는 표 내용이 본문(텍스트 레이어) 청크에 이미 들어 있어서, 표 청크가 있으면 같은 내용이 검색 결과
두 자리를 차지한다. 이제 청크를 만들 때 PDF 표 요소를 빼므로(``elements.tables_in_body``), 이미 만든
표 청크도 지운다. 본문 청크와 임베딩은 그대로라 다시 처리할 필요가 없다.

되돌리기는 하지 않는다. 표 청크가 필요하면 문서를 다시 처리하면 된다 (지금 규칙으로는 만들지 않는다).

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-21 12:00:00.000000+00:00

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        """
        DELETE FROM document_chunks
        WHERE kind = 'table'
          AND document_id IN (SELECT id FROM documents WHERE kind = 'pdf')
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
