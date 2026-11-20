"""청크 키워드 검색 열 (tsvector + GIN 인덱스)

하이브리드 검색(벡터 + 키워드)에 쓴다. 토큰은 앱이 만든다 (``app.documents.keywords``, 한글은 두 글자 조각).
기존 청크는 비어 있고, 그 프로젝트를 처음 검색할 때 채운다.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-20 13:04:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "document_chunks", sa.Column("search_vector", postgresql.TSVECTOR(), nullable=True)
    )
    op.create_index(
        "ix_document_chunks_search_vector",
        "document_chunks",
        ["search_vector"],
        unique=False,
        postgresql_using="gin",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_document_chunks_search_vector", table_name="document_chunks")
    op.drop_column("document_chunks", "search_vector")
