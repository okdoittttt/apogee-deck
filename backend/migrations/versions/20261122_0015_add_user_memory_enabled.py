"""사용자별 장기 기억 켜기/끄기

장기 기억(프로필과 기억)은 LangGraph Store 테이블에 저장한다 (LangGraph 가 자체 마이그레이션으로 관리).
여기서는 사용자가 기억 기능을 끌 수 있게 설정 열만 추가한다. 기존 사용자는 켜진 상태로 시작한다.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-22 12:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "users",
        sa.Column("memory_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("users", "memory_enabled")
