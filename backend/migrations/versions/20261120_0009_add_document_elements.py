"""프로젝트 문서의 요소(본문·표·그림) 테이블과 문서의 그림 수

프로젝트 문서를 구조화해서 적재한다. 요소는 문서와 함께 지워진다 (CASCADE). 그림 원본은 첨부 저장소에
두고 객체 키만 둔다. 기존 프로젝트 문서는 요소가 없다 (다시 올리면 만들어진다).

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-20 11:42:34.519630+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "document_elements",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("heading", sa.String(length=500), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("image_key", sa.String(length=300), nullable=True),
        sa.Column("image_mime", sa.String(length=50), nullable=True),
        sa.Column("image_status", sa.String(length=10), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_document_elements_document_id"), "document_elements", ["document_id"], unique=False
    )
    op.add_column("documents", sa.Column("image_count", sa.Integer(), nullable=True))
    op.add_column("documents", sa.Column("image_fallback_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("documents", "image_fallback_count")
    op.drop_column("documents", "image_count")
    op.drop_index(op.f("ix_document_elements_document_id"), table_name="document_elements")
    op.drop_table("document_elements")
