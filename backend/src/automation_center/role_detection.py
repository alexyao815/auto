"""角色标签校验、识别任务创建，以及独立后台 Worker。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid

from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload, sessionmaker

from .models import (
    Node,
    NodeRole,
    RoleDetectionJob,
    RoleDetectionNodeResult,
    RoleRule,
    utcnow,
)
from .salt import SaltAdapter, SaltProcessSnapshot


logger = logging.getLogger(__name__)
ROLE_LABEL_PATTERN = re.compile(r"^[\w.-]{1,64}$", re.UNICODE)


class RoleValidationError(ValueError):
    """角色标签或识别规则不符合公开输入约束。"""


def normalize_role_label(value: str) -> str:
    """校验并返回规范角色标签，保留大小写和中英文原文。"""

    normalized = value.strip()
    if not ROLE_LABEL_PATTERN.fullmatch(normalized):
        raise RoleValidationError("角色标签必须为 1–64 位中英文、数字、点、下划线或短横线")
    return normalized


def normalize_role_labels(values: list[str]) -> list[str]:
    """规范、去重并排序一个节点的完整人工编辑结果。"""

    if len(values) > 200:
        raise RoleValidationError("单节点角色标签不得超过 200 个")
    return sorted({normalize_role_label(value) for value in values})


def normalize_rule_pattern(value: str) -> str:
    """校验字面进程匹配文本；控制字符可能破坏页面或审计输出，必须拒绝。"""

    normalized = value.strip()
    if not normalized or len(normalized) > 255 or any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise RoleValidationError("进程匹配文本必须为 1–255 位且不能包含控制字符")
    return normalized


def create_role_detection_job(session: Session) -> RoleDetectionJob:
    """在调用方事务中快照规则和全部节点，不执行 Salt 调用。"""

    rules = list(session.scalars(
        select(RoleRule)
        .where(RoleRule.enabled.is_(True), RoleRule.matcher_type == "process")
        .order_by(RoleRule.role, RoleRule.pattern, RoleRule.id)
    ))
    if not rules:
        raise RoleValidationError("至少需要一条已启用的 process 角色规则")
    nodes = list(session.scalars(
        select(Node).options(selectinload(Node.roles)).order_by(Node.id)
    ).unique())
    online_nodes = [node for node in nodes if node.online_status == "ONLINE"]
    if not online_nodes:
        raise RoleValidationError("当前没有 ONLINE 节点可执行角色识别")

    job = RoleDetectionJob(
        id=str(uuid.uuid4()),
        status="WAITING",
        active_slot=1,
        rules_snapshot_json=json.dumps(
            [
                {
                    "role": rule.role,
                    "matcher_type": "process",
                    "pattern": rule.pattern,
                    "enabled": True,
                }
                for rule in rules
            ],
            ensure_ascii=False,
        ),
        total_node_count=len(nodes),
        target_node_count=len(online_nodes),
        skipped_count=len(nodes) - len(online_nodes),
    )
    for node in nodes:
        skipped = node.online_status != "ONLINE"
        job.results.append(RoleDetectionNodeResult(
            id=str(uuid.uuid4()),
            node_id=node.id,
            node_id_snapshot=node.id,
            hostname_snapshot=node.hostname,
            status="SKIPPED_OFFLINE" if skipped else "WAITING",
            finished_at=utcnow() if skipped else None,
        ))
    session.add(job)
    return job


class RoleDetectionWorker:
    """Claim 并执行持久化角色识别任务，不复用维护任务调度状态机。"""

    def __init__(self, factory: sessionmaker[Session], salt: SaltAdapter, interval_seconds: float = 1.0) -> None:
        self.factory = factory
        self.salt = salt
        self.interval_seconds = interval_seconds
        self._stop = asyncio.Event()

    async def run(self) -> None:
        """恢复中断任务，然后在独立线程中串行执行最多一个活动任务。"""

        await asyncio.to_thread(self.recover_interrupted)
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self.run_once)
            except Exception:
                logger.exception("角色识别 Worker 执行失败，将在下一周期继续检查")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                continue

    async def stop(self) -> None:
        """通知 Worker 停止；正在进行的 Salt 请求最多受 15 秒超时约束。"""

        self._stop.set()

    def recover_interrupted(self) -> None:
        """把无持久化 JID 的 RUNNING 任务收敛为失败，禁止自动重复扫描。"""

        with self.factory() as session:
            jobs = list(session.scalars(
                select(RoleDetectionJob)
                .where(RoleDetectionJob.status == "RUNNING")
                .options(selectinload(RoleDetectionJob.results))
            ).unique())
            now = utcnow()
            for job in jobs:
                for result in job.results:
                    if result.status == "WAITING":
                        result.status = "FAILED"
                        result.failure_reason = "EXECUTION_STATE_LOST"
                        result.finished_at = now
                job.status = "FAILED"
                job.failed_count = sum(result.status == "FAILED" for result in job.results)
                job.success_count = sum(result.status == "SUCCESS" for result in job.results)
                job.failure_reason = "EXECUTION_STATE_LOST"
                job.finished_at = now
                job.active_slot = None
            if jobs:
                session.commit()

    def run_once(self) -> str | None:
        """CAS Claim 最早的 WAITING 任务并执行；无任务时返回 None。"""

        with self.factory() as session:
            job_id = session.scalar(
                select(RoleDetectionJob.id)
                .where(RoleDetectionJob.status == "WAITING")
                .order_by(RoleDetectionJob.created_at, RoleDetectionJob.id)
                .limit(1)
            )
            if job_id is None:
                return None
            claimed = session.execute(
                update(RoleDetectionJob)
                .where(RoleDetectionJob.id == job_id, RoleDetectionJob.status == "WAITING")
                .values(status="RUNNING", started_at=utcnow())
            ).rowcount
            session.commit()
        if claimed != 1:
            return None
        try:
            self._execute(job_id)
        except Exception:
            self._fail_claimed_job(job_id)
            raise
        return job_id

    def _fail_claimed_job(self, job_id: str) -> None:
        """内部异常时尽力释放活动槽，避免任务在当前进程永久卡为 RUNNING。"""

        with self.factory() as session:
            job = session.scalar(
                select(RoleDetectionJob)
                .where(RoleDetectionJob.id == job_id, RoleDetectionJob.status == "RUNNING")
                .options(selectinload(RoleDetectionJob.results))
            )
            if job is None:
                return
            now = utcnow()
            for result in job.results:
                if result.status == "WAITING":
                    result.status = "FAILED"
                    result.failure_reason = "ROLE_DETECTION_INTERNAL_ERROR"
                    result.finished_at = now
            job.status = "FAILED"
            job.success_count = sum(result.status == "SUCCESS" for result in job.results)
            job.failed_count = sum(result.status == "FAILED" for result in job.results)
            job.failure_reason = "ROLE_DETECTION_INTERNAL_ERROR"
            job.finished_at = now
            job.active_slot = None
            session.commit()

    def _execute(self, job_id: str) -> None:
        """在数据库事务外读取 Salt 进程快照，再用短事务合并标签。"""

        with self.factory() as session:
            job = session.scalar(
                select(RoleDetectionJob)
                .where(RoleDetectionJob.id == job_id)
                .options(selectinload(RoleDetectionJob.results))
            )
            if job is None:
                return
            rules = json.loads(job.rules_snapshot_json)
            target_ids = [
                result.node_id_snapshot for result in job.results if result.status == "WAITING"
            ]

        try:
            snapshots = self.salt.process_snapshot_many(target_ids)
        except Exception as exc:
            logger.warning("角色识别 Salt 批量调用失败 job_id=%s error_type=%s", job_id, type(exc).__name__)
            snapshots = {
                node_id: SaltProcessSnapshot(state="FAILED", failure_reason="SALT_REQUEST_FAILED")
                for node_id in target_ids
            }
        self._apply_results(job_id, rules, snapshots)

    def _apply_results(
        self,
        job_id: str,
        rules: list[dict[str, object]],
        snapshots: dict[str, SaltProcessSnapshot],
    ) -> None:
        """重新读取当前标签后只补缺失 auto 标签，保护并发人工修改。"""

        with self.factory() as session:
            job = session.scalar(
                select(RoleDetectionJob)
                .where(RoleDetectionJob.id == job_id, RoleDetectionJob.status == "RUNNING")
                .options(selectinload(RoleDetectionJob.results))
            )
            if job is None:
                return
            target_ids = [result.node_id_snapshot for result in job.results if result.status == "WAITING"]
            nodes = list(session.scalars(
                select(Node).where(Node.id.in_(target_ids)).options(selectinload(Node.roles))
            ).unique())
            nodes_by_id = {node.id: node for node in nodes}
            now = utcnow()
            for result in job.results:
                if result.status != "WAITING":
                    continue
                snapshot = snapshots.get(result.node_id_snapshot)
                node = nodes_by_id.get(result.node_id_snapshot)
                if node is None:
                    result.status = "FAILED"
                    result.failure_reason = "NODE_NOT_FOUND"
                elif snapshot is None or snapshot.state != "SUCCESS":
                    result.status = "FAILED"
                    result.failure_reason = (snapshot.failure_reason if snapshot else "NODE_NO_RESPONSE") or "PROCESS_SNAPSHOT_FAILED"
                else:
                    # 进程原文仅在此处参与字面匹配，绝不写入模型或日志。
                    matched = sorted({
                        str(rule["role"])
                        for rule in rules
                        if rule.get("enabled", True) and str(rule["pattern"]) in snapshot.process_text
                    })
                    existing = {role.role for role in node.roles}
                    added = [role for role in matched if role not in existing]
                    for role in added:
                        node.roles.append(NodeRole(role=role, source="auto"))
                    node.role_override = False
                    result.status = "SUCCESS"
                    result.matched_roles_json = json.dumps(matched, ensure_ascii=False)
                    result.added_roles_json = json.dumps(added, ensure_ascii=False)
                result.finished_at = now

            job.success_count = sum(result.status == "SUCCESS" for result in job.results)
            job.failed_count = sum(result.status == "FAILED" for result in job.results)
            job.skipped_count = sum(result.status == "SKIPPED_OFFLINE" for result in job.results)
            if job.failed_count == 0:
                job.status = "SUCCESS"
            elif job.success_count:
                job.status = "PARTIAL_FAILED"
            else:
                job.status = "FAILED"
                job.failure_reason = "ALL_TARGETS_FAILED"
            job.finished_at = now
            job.active_slot = None
            session.commit()
