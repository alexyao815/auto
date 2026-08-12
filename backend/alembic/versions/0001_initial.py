"""创建 Automation Center V1 初始数据库结构。"""

from alembic import op

from automation_center.database import Base
from automation_center import models  # noqa: F401


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """按当前模型元数据创建全部初始表和约束。"""
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    """删除 V1 全部表；仅供明确执行的迁移回退使用。"""
    Base.metadata.drop_all(bind=op.get_bind())
