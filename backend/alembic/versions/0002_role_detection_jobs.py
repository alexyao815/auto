"""增加持久化角色识别任务，并切换到自动/人工标签并集。"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op


revision = "0002_role_detection_jobs"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """幂等创建新表，并清理不再支持的 service 规则。

    ``0001_initial`` 使用当前 ORM metadata 执行 ``create_all``，所以全新数据库
    可能已经包含本迁移的新表；这里先检查表名以同时兼容新装和原地升级。
    """

    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "role_detection_jobs" not in tables:
        op.create_table(
            "role_detection_jobs",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("active_slot", sa.Integer(), nullable=True),
            sa.Column("rules_snapshot_json", sa.Text(), nullable=False),
            sa.Column("total_node_count", sa.Integer(), nullable=False),
            sa.Column("target_node_count", sa.Integer(), nullable=False),
            sa.Column("success_count", sa.Integer(), nullable=False),
            sa.Column("failed_count", sa.Integer(), nullable=False),
            sa.Column("skipped_count", sa.Integer(), nullable=False),
            sa.Column("failure_reason", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("active_slot", name="uq_role_detection_active_slot"),
        )
        op.create_index("ix_role_detection_jobs_status", "role_detection_jobs", ["status"])
        op.create_index("ix_role_detection_jobs_created_at", "role_detection_jobs", ["created_at"])
    if "role_detection_node_results" not in tables:
        op.create_table(
            "role_detection_node_results",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("job_id", sa.String(length=36), nullable=False),
            sa.Column("node_id", sa.String(length=128), nullable=True),
            sa.Column("node_id_snapshot", sa.String(length=128), nullable=False),
            sa.Column("hostname_snapshot", sa.String(length=255), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("matched_roles_json", sa.Text(), nullable=False),
            sa.Column("added_roles_json", sa.Text(), nullable=False),
            sa.Column("failure_reason", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["job_id"], ["role_detection_jobs.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["node_id"], ["nodes.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("job_id", "node_id_snapshot", name="uq_role_detection_job_node"),
        )
        op.create_index("ix_role_detection_node_results_job_id", "role_detection_node_results", ["job_id"])
        op.create_index("ix_role_detection_node_results_node_id", "role_detection_node_results", ["node_id"])
        op.create_index("ix_role_detection_node_results_status", "role_detection_node_results", ["status"])

    # 当前决策不再采用人工整体覆盖；历史 manual/auto 记录都由并集语义读取。
    bind.execute(sa.text("UPDATE nodes SET role_override = 0"))
    bind.execute(sa.text("DELETE FROM role_rules WHERE matcher_type <> 'process'"))
    rules = [
        {
            "role": row.role,
            "matcher_type": "process",
            "pattern": row.pattern,
            "enabled": bool(row.enabled),
        }
        for row in bind.execute(
            sa.text("SELECT role, pattern, enabled FROM role_rules ORDER BY role, pattern, id")
        )
    ]
    serialized = json.dumps(rules, ensure_ascii=False)
    updated = bind.execute(
        sa.text("UPDATE system_settings SET value = :value WHERE key = 'role_detection_rules'"),
        {"value": serialized},
    )
    if updated.rowcount == 0:
        bind.execute(
            sa.text(
                "INSERT INTO system_settings (key, value, sensitive, updated_at) "
                "VALUES ('role_detection_rules', :value, 0, CURRENT_TIMESTAMP)"
            ),
            {"value": serialized},
        )


def downgrade() -> None:
    """删除角色识别历史表；已清理的旧规则和 override 标记不自动恢复。"""

    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "role_detection_node_results" in tables:
        op.drop_table("role_detection_node_results")
    if "role_detection_jobs" in tables:
        op.drop_table("role_detection_jobs")
