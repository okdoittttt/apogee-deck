"""청크가 어느 요소에서 왔는지 (element_ids)

문서 보기 패널에서 검색 결과의 근거 위치(요소)를 강조할 때 쓴다. 이 열을 추가하기 전에 만든 청크는 비어 있고,
문서를 다시 처리하면 채워진다. 비어 있으면 패널은 청크 본문으로 위치를 찾는다.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-21 00:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "document_chunks",
        sa.Column("element_ids", postgresql.ARRAY(sa.Uuid()), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("document_chunks", "element_ids")
