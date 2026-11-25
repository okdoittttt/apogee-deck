"""문서 수정본: documents 의 source_document_id, revision, created_by

에이전트가 고친 수정본을 documents 의 새 행으로 둔다 (원본은 바꾸지 않는다). source_document_id 는
수정본을 만든 직전 문서를 가리키고, 직전 문서가 지워지면 비운다 (SET NULL). 기존 문서는 모두 사용자가
올린 원본이라 revision 1, created_by 'user' 로 채운다.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-24 01:17:53.748512+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("documents", sa.Column("source_document_id", sa.Uuid(), nullable=True))
    op.add_column(
        "documents", sa.Column("revision", sa.Integer(), server_default="1", nullable=False)
    )
    op.add_column(
        "documents",
        sa.Column("created_by", sa.String(length=20), server_default="user", nullable=False),
    )
    op.create_index(
        op.f("ix_documents_source_document_id"), "documents", ["source_document_id"], unique=False
    )
    op.create_foreign_key(
        "fk_documents_source_document_id_documents",
        "documents",
        "documents",
        ["source_document_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("fk_documents_source_document_id_documents", "documents", type_="foreignkey")
    op.drop_index(op.f("ix_documents_source_document_id"), table_name="documents")
    op.drop_column("documents", "created_by")
    op.drop_column("documents", "revision")
    op.drop_column("documents", "source_document_id")
