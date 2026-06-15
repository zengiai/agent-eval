"""add_eval_run_id_to_eval_scores

Revision ID: d30fca81b164
Revises: 0f024b84c17b
Create Date: 2026-06-15 10:48:51.533405
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'd30fca81b164'
down_revision: Union[str, None] = '0f024b84c17b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. 添加 eval_run_id 列（先可空，便于迁移已有数据）
    op.add_column('eval_scores',
        sa.Column('eval_run_id', postgresql.UUID(as_uuid=True), nullable=True)
    )

    # 2. 创建索引
    op.create_index('idx_eval_scores_run', 'eval_scores', ['eval_run_id'])

    # 3. 添加外键约束
    op.create_foreign_key(
        'fk_eval_scores_eval_run_id',
        'eval_scores', 'eval_runs',
        ['eval_run_id'], ['id'],
        ondelete='CASCADE',
    )


def downgrade() -> None:
    op.drop_constraint('fk_eval_scores_eval_run_id', 'eval_scores', type_='foreignkey')
    op.drop_index('idx_eval_scores_run', table_name='eval_scores')
    op.drop_column('eval_scores', 'eval_run_id')
