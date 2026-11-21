"""메시지 피드백 (트레이스 ID, 👍/👎, 의견)

assistant 응답에 단 사용자 피드백을 저장하고, 응답의 Langfuse 트레이스에 점수로 남기기 위해 트레이스 ID 를 둔다.
기존 응답은 트레이스 ID 가 없어서 피드백은 DB 에만 저장된다.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-21 15:00:00.000000+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("messages", sa.Column("trace_id", sa.String(length=32), nullable=True))
    op.add_column("messages", sa.Column("feedback", sa.SmallInteger(), nullable=True))
    op.add_column("messages", sa.Column("feedback_comment", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("messages", "feedback_comment")
    op.drop_column("messages", "feedback")
    op.drop_column("messages", "trace_id")
