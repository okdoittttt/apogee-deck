"""채팅 이미지 원본을 DB 에서 첨부 저장소로 옮김 (data → storage_key)

이미지 원본은 이제 첨부 저장소(MinIO)에 두고 DB 에는 객체 키만 둔다. 원본을 저장소로 옮기는 일은
마이그레이션에서 하지 않는다 (마이그레이션이 저장소에 접근하지 않게 하기 위해). 그래서 이미지가 이미
저장되어 있으면 멈추고 안내한다. 이 변경 시점에는 저장된 이미지가 없었다.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-20 08:00:00+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_no_images() -> None:
    count = op.get_bind().scalar(sa.text("SELECT count(*) FROM message_images"))
    if count:
        raise RuntimeError(
            f"message_images 에 이미지가 {count}개 있어서 자동으로 옮길 수 없습니다. "
            "원본을 저장소로 옮기는 스크립트를 먼저 실행하거나, 해당 대화를 지운 뒤 다시 실행해 주세요."
        )


def upgrade() -> None:
    """Upgrade schema."""
    _require_no_images()
    op.add_column("message_images", sa.Column("storage_key", sa.String(length=200), nullable=False))
    op.drop_column("message_images", "data")


def downgrade() -> None:
    """Downgrade schema."""
    _require_no_images()
    op.add_column("message_images", sa.Column("data", sa.LargeBinary(), nullable=False))
    op.drop_column("message_images", "storage_key")
