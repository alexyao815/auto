"""Automation Center 的持久化模型和任务执行快照。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    """返回适合 SQLite 存储的无时区 UTC 时间；API 序列化时补回 Z。"""

    return datetime.now(UTC).replace(tzinfo=None)


class Credential(Base):
    """V1 唯一共享账号；数据库只保存 Argon2id 密码 Hash。"""

    __tablename__ = "credentials"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    username: Mapped[str] = mapped_column(String(128), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class SessionRecord(Base):
    """服务端登录会话，保存 Token/CSRF Hash 和双重过期时间。"""

    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    source_ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    idle_expires_at: Mapped[datetime]
    absolute_expires_at: Mapped[datetime]


class Node(Base):
    """Salt Minion 的当前视图；历史任务依赖 TaskNode 快照而非本记录。"""

    __tablename__ = "nodes"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    hostname: Mapped[str] = mapped_column(String(255))
    management_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    online_status: Mapped[str] = mapped_column(String(16), default="OFFLINE", index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    role_override: Mapped[bool] = mapped_column(Boolean, default=False)
    last_check_time: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    roles: Mapped[list[NodeRole]] = relationship(back_populates="node", cascade="all, delete-orphan")


class NodeRole(Base):
    """节点角色及来源；auto 与 manual 共同组成节点当前有效角色。"""

    __tablename__ = "node_roles"
    __table_args__ = (UniqueConstraint("node_id", "role", "source", name="uq_node_role_source"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(16), default="auto")
    node: Mapped[Node] = relationship(back_populates="roles")


class RoleRule(Base):
    """根据进程命令行推导业务角色的可配置字面匹配规则。"""

    __tablename__ = "role_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    role: Mapped[str] = mapped_column(String(64), index=True)
    matcher_type: Mapped[str] = mapped_column(String(32))
    pattern: Mapped[str] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class RoleDetectionJob(Base):
    """一次用户触发的角色识别任务，独立于维护任务状态机。

    ``active_slot`` 在 WAITING/RUNNING 时固定为 1，终态清空。数据库唯一约束
    保证即使两个创建请求并发到达，也只会存在一个活动识别任务。
    """

    __tablename__ = "role_detection_jobs"
    __table_args__ = (UniqueConstraint("active_slot", name="uq_role_detection_active_slot"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), default="WAITING", index=True)
    active_slot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rules_snapshot_json: Mapped[str] = mapped_column(Text, default="[]")
    total_node_count: Mapped[int] = mapped_column(default=0)
    target_node_count: Mapped[int] = mapped_column(default=0)
    success_count: Mapped[int] = mapped_column(default=0)
    failed_count: Mapped[int] = mapped_column(default=0)
    skipped_count: Mapped[int] = mapped_column(default=0)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    results: Mapped[list[RoleDetectionNodeResult]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="RoleDetectionNodeResult.node_id_snapshot",
    )


class RoleDetectionNodeResult(Base):
    """角色识别任务的节点快照和结果；节点删除后历史仍可解释。"""

    __tablename__ = "role_detection_node_results"
    __table_args__ = (UniqueConstraint("job_id", "node_id_snapshot", name="uq_role_detection_job_node"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("role_detection_jobs.id", ondelete="CASCADE"), index=True)
    node_id: Mapped[str | None] = mapped_column(ForeignKey("nodes.id", ondelete="SET NULL"), nullable=True, index=True)
    node_id_snapshot: Mapped[str] = mapped_column(String(128))
    hostname_snapshot: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="WAITING", index=True)
    matched_roles_json: Mapped[str] = mapped_column(Text, default="[]")
    added_roles_json: Mapped[str] = mapped_column(Text, default="[]")
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    job: Mapped[RoleDetectionJob] = relationship(back_populates="results")


class Package(Base):
    """维护包当前 Revision；删除后历史 Task 仍保留包名和 Revision 快照。"""

    __tablename__ = "packages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    description: Mapped[str] = mapped_column(Text, default="")
    component: Mapped[str] = mapped_column(String(128), default="")
    bug_id: Mapped[str] = mapped_column(String(128), default="")
    target_roles_json: Mapped[str] = mapped_column(Text, default="[]")
    applicable_versions_json: Mapped[str] = mapped_column(Text, default="[]")
    storage_path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    manifest_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    steps: Mapped[list[PackageStep]] = relationship(back_populates="package", cascade="all, delete-orphan", order_by="PackageStep.sequence")


class PackageStep(Base):
    """当前 Package Revision 的有序 Step 定义。"""

    __tablename__ = "package_steps"
    __table_args__ = (UniqueConstraint("package_id", "name", name="uq_package_step_name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    package_id: Mapped[str] = mapped_column(ForeignKey("packages.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int]
    name: Mapped[str] = mapped_column(String(255))
    executor_type: Mapped[str] = mapped_column(String(16))
    script: Mapped[str] = mapped_column(Text)
    timeout: Mapped[int]
    failure_action: Mapped[str] = mapped_column(String(16))
    package: Mapped[Package] = relationship(back_populates="steps")


class QueueCounter(Base):
    """持久化全局队列计数器，为 TaskNode 和 Retry 分配单调 queue_seq。"""

    __tablename__ = "queue_counters"
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[int] = mapped_column(Integer, default=0)


class Task(Base):
    """一次用户任务及其聚合状态、Package 快照和永久幂等键。"""

    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    package_id: Mapped[str | None] = mapped_column(ForeignKey("packages.id", ondelete="SET NULL"), nullable=True, index=True)
    package_name_snapshot: Mapped[str] = mapped_column(String(255))
    package_revision_snapshot: Mapped[int]
    package_description_snapshot: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="WAITING", index=True)
    target_node_count: Mapped[int] = mapped_column(default=0)
    success_count: Mapped[int] = mapped_column(default=0)
    failed_count: Mapped[int] = mapped_column(default=0)
    cancelled_count: Mapped[int] = mapped_column(default=0)
    remark: Mapped[str] = mapped_column(Text, default="")
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    nodes: Mapped[list[TaskNode]] = relationship(back_populates="task", cascade="all, delete-orphan")


class TaskNode(Base):
    """Task 在单个目标节点上的队列项和节点快照，是 Scheduler 调度粒度。"""

    __tablename__ = "task_nodes"
    __table_args__ = (
        UniqueConstraint("task_id", "node_id", name="uq_task_node"),
        Index("ix_task_node_queue", "node_id", "status", "queue_seq"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    node_id: Mapped[str | None] = mapped_column(ForeignKey("nodes.id", ondelete="SET NULL"), nullable=True, index=True)
    hostname_snapshot: Mapped[str] = mapped_column(String(255))
    management_ip_snapshot: Mapped[str | None] = mapped_column(String(64), nullable=True)
    roles_snapshot_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(32), default="WAITING", index=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    has_warning: Mapped[bool] = mapped_column(Boolean, default=False)
    queue_entered_at: Mapped[datetime] = mapped_column(default=utcnow)
    queue_seq: Mapped[int] = mapped_column(index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    task: Mapped[Task] = relationship(back_populates="nodes")
    attempts: Mapped[list[TaskAttempt]] = relationship(back_populates="task_node", cascade="all, delete-orphan", order_by="TaskAttempt.attempt_no")


class TaskAttempt(Base):
    """TaskNode 的一次真实执行；Retry 会追加 Attempt 而不覆盖历史。"""

    __tablename__ = "task_attempts"
    __table_args__ = (UniqueConstraint("task_node_id", "attempt_no", name="uq_task_attempt_no"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_node_id: Mapped[str] = mapped_column(ForeignKey("task_nodes.id", ondelete="CASCADE"), index=True)
    attempt_no: Mapped[int]
    status: Mapped[str] = mapped_column(String(32), default="RUNNING")
    warning_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    task_node: Mapped[TaskNode] = relationship(back_populates="attempts")
    steps: Mapped[list[TaskStepResult]] = relationship(back_populates="attempt", cascade="all, delete-orphan", order_by="TaskStepResult.sequence")


class TaskStepResult(Base):
    """Attempt 内单个 Step 的定义快照、Salt JID、日志位置和最终结果。"""

    __tablename__ = "task_step_results"
    __table_args__ = (UniqueConstraint("attempt_id", "sequence", name="uq_attempt_step_sequence"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("task_attempts.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int]
    name_snapshot: Mapped[str] = mapped_column(String(255))
    executor_type: Mapped[str] = mapped_column(String(16))
    script_snapshot: Mapped[str] = mapped_column(Text)
    timeout_snapshot: Mapped[int]
    failure_action_snapshot: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32), default="WAITING")
    salt_jid: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    exit_code: Mapped[int | None] = mapped_column(nullable=True)
    stdout_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    stderr_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    attempt: Mapped[TaskAttempt] = relationship(back_populates="steps")


class NodeExecutionLock(Base):
    """数据库级节点锁；以 node_id 为主键保证每节点最多一个执行者。"""

    __tablename__ = "node_execution_locks"
    node_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    task_node_id: Mapped[str] = mapped_column(String(36), unique=True)
    acquired_at: Mapped[datetime] = mapped_column(default=utcnow)


class SystemSetting(Base):
    """可由管理页面修改并在重启后恢复的运行设置。"""

    __tablename__ = "system_settings"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class AuditLog(Base):
    """状态修改操作的不可变审计摘要，不记录密码或 Salt 明文凭据。"""

    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_ip: Mapped[str] = mapped_column(String(64), default="")
    operation: Mapped[str] = mapped_column(String(64), index=True)
    object_type: Mapped[str] = mapped_column(String(64))
    object_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
