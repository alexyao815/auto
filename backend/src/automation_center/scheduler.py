"""节点级 FIFO 调度、Salt Step 执行、重启恢复和过期文件清理。"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, selectinload, sessionmaker

from .config import Settings
from .models import (
    Node,
    NodeExecutionLock,
    Package,
    PackageStep,
    SystemSetting,
    Task,
    TaskAttempt,
    TaskNode,
    TaskStepResult,
    utcnow,
)
from .node_service import apply_node_snapshots, collect_node_snapshots
from .salt import SaltAdapter
from .task_service import aggregate_task


logger = logging.getLogger(__name__)


class _SchedulerStopping(Exception):
    """应用关闭时停止本地监控，但保留已持久化 JID 供下次启动恢复。"""


class Scheduler:
    """在单应用进程内驱动持久化任务状态机。

    ``_running`` 只减少本进程的重复提交；真正的并发正确性来自 TaskNode 状态
    CAS 与 ``NodeExecutionLock`` 唯一约束。Salt 调用放在线程池执行，避免阻塞
    FastAPI 事件循环。
    """

    def __init__(self, factory: sessionmaker[Session], settings: Settings, salt: SaltAdapter) -> None:
        self.factory = factory
        self.settings = settings
        self.salt = salt
        self.executor = ThreadPoolExecutor(max_workers=settings.scheduler_max_workers, thread_name_prefix="task-node")
        self._stop = asyncio.Event()
        self._thread_stop = threading.Event()
        self._running: set[str] = set()
        self._guard = threading.Lock()

    async def run(self) -> None:
        """先恢复持久化 RUNNING 节点，再循环刷新、Claim 和清理。"""
        await self.recover()
        while not self._stop.is_set():
            try:
                await self.refresh_nodes()
                await self.tick()
                await self.cleanup_expired()
            except Exception:
                # 单个执行失败由 Worker 落库，不能让异常终止整个调度循环。
                logger.exception("Scheduler 主循环执行失败，将在下一个周期重试")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.settings.scheduler_interval_seconds)
            except TimeoutError:
                continue

    async def refresh_nodes(self) -> None:
        """按配置周期把阻塞式 Salt 节点探测移到工作线程。"""
        marker = getattr(self, "_last_node_refresh", 0.0)
        if time.monotonic() - marker < self.settings.node_status_check_interval:
            return
        try:
            await asyncio.to_thread(self._refresh_nodes_sync)
        finally:
            # 从一轮完成时重新计时；慢探测或异常不能触发无间隔连续重试。
            self._last_node_refresh = time.monotonic()

    def _refresh_nodes_sync(self) -> None:
        """先在事务外批量 ping Salt，再用单个短事务更新节点状态。"""

        with self.factory() as session:
            known_ids = set(session.scalars(select(Node.id)))
        snapshots = collect_node_snapshots(self.salt, known_ids)
        with self.factory() as session:
            apply_node_snapshots(session, snapshots)
            session.commit()

    async def stop(self) -> None:
        """停止调度并等待工作线程退出，已持久化 JID 留给重启恢复。

        Python 不能强制取消正在运行的线程。这里通过线程事件让监控循环主动退出，
        再在线程外执行 ``shutdown(wait=True)``；既不阻塞 FastAPI 事件循环，也不会
        把已 Claim 但尚未开始的任务留成无 JID 的 RUNNING 状态。
        """

        self._stop.set()
        self._thread_stop.set()
        await asyncio.to_thread(self.executor.shutdown, wait=True)

    async def tick(self) -> None:
        """按全局 queue_seq 扫描 Waiting 节点并尝试并发 Claim。"""

        if self._stop.is_set():
            return
        with self.factory() as session:
            waiting = list(session.scalars(
                select(TaskNode).where(TaskNode.status == "WAITING").order_by(TaskNode.queue_seq).limit(100)
            ))
        for task_node in waiting:
            if self._stop.is_set():
                break
            with self._guard:
                # 这是进程内去重优化，服务重启或竞争 Worker 仍依赖数据库约束。
                if task_node.id in self._running:
                    continue
            claimed = await asyncio.to_thread(self._claim, task_node.id)
            if claimed:
                if self._stop.is_set():
                    # stop 可能在 Claim 的数据库线程执行期间到达；尚未提交到执行池时
                    # 可以安全删除空 Attempt、释放节点锁并保持原 FIFO 位置。
                    await asyncio.to_thread(self._release_unstarted_claim, task_node.id)
                    break
                with self._guard:
                    self._running.add(task_node.id)
                asyncio.get_running_loop().run_in_executor(self.executor, self._execute_claimed, task_node.id)

    def _release_unstarted_claim(self, task_node_id: str) -> bool:
        """把尚未发生任何 Salt 下发的 Claim 原子退回 Waiting。"""

        with self.factory() as session:
            task_node = session.scalar(
                select(TaskNode).where(TaskNode.id == task_node_id).options(
                    selectinload(TaskNode.task).selectinload(Task.nodes),
                    selectinload(TaskNode.attempts).selectinload(TaskAttempt.steps),
                )
            )
            if task_node is None or task_node.status != "RUNNING" or not task_node.attempts:
                return False
            attempt = task_node.attempts[-1]
            if any(step.status != "WAITING" or step.salt_jid is not None for step in attempt.steps):
                return False
            session.execute(delete(NodeExecutionLock).where(NodeExecutionLock.task_node_id == task_node.id))
            session.delete(attempt)
            task_node.status = "WAITING"
            task_node.started_at = None
            task_node.finished_at = None
            task_node.failure_reason = None
            task = aggregate_task(session, task_node.task_id)
            if task.status == "WAITING" and not any(node.started_at for node in task.nodes):
                task.started_at = None
            session.commit()
            return True

    def _claim(self, task_node_id: str) -> bool:
        """以 CAS 和唯一节点锁 Claim 一个 Waiting 节点。

        返回 ``True`` 时，节点、Attempt 和 Step 快照已在同一事务提交；返回
        ``False`` 表示状态变化、FIFO 前置任务、节点锁冲突或执行前校验失败。
        Attempt 仅在确实取得执行权时创建，排队取消不会产生空 Attempt。
        """
        with self.factory() as session:
            task_node = session.get(TaskNode, task_node_id)
            if task_node is None or task_node.status != "WAITING" or task_node.node_id is None:
                return False
            node = session.get(Node, task_node.node_id)
            if node is None:
                self._fail_without_attempt(session, task_node, "NODE_MISSING")
                return False
            earlier_waiting = session.scalar(
                select(TaskNode.id).where(
                    TaskNode.node_id == task_node.node_id,
                    TaskNode.status == "WAITING",
                    TaskNode.queue_seq < task_node.queue_seq,
                ).limit(1)
            )
            if earlier_waiting is not None:
                # 同一节点不能绕过更早的 Waiting 项，即使更早项尚未被本轮扫描 Claim。
                return False
            if not node.enabled:
                return False
            # 在线探测发生在创建唯一锁之前，Salt 网络等待期间尚未持有 SQLite 写锁。
            if not self.salt.ping(node.id):
                self._fail_without_attempt(session, task_node, "OFFLINE")
                return False
            task = session.get(Task, task_node.task_id)
            package = session.get(Package, task.package_id) if task and task.package_id else None
            if task is None or package is None or package.revision != task.package_revision_snapshot:
                self._fail_without_attempt(session, task_node, "PACKAGE_REVISION_UNAVAILABLE")
                return False
            try:
                # node_id 主键提供跨线程唯一执行权；TaskNode 条件更新再防止 Cancel/Claim 竞态。
                session.add(NodeExecutionLock(node_id=node.id, task_node_id=task_node.id))
                session.flush()
                changed = session.execute(
                    update(TaskNode).where(TaskNode.id == task_node.id, TaskNode.status == "WAITING").values(status="RUNNING", started_at=utcnow())
                ).rowcount
                if changed != 1:
                    session.rollback()
                    return False
                attempt_no = int(session.scalar(select(func.count(TaskAttempt.id)).where(TaskAttempt.task_node_id == task_node.id)) or 0) + 1
                attempt = TaskAttempt(id=str(uuid.uuid4()), task_node_id=task_node.id, attempt_no=attempt_no, status="RUNNING")
                session.add(attempt)
                for step in package.steps:
                    session.add(TaskStepResult(
                        id=str(uuid.uuid4()),
                        attempt_id=attempt.id,
                        sequence=step.sequence,
                        name_snapshot=step.name,
                        executor_type=step.executor_type,
                        script_snapshot=step.script,
                        timeout_snapshot=step.timeout,
                        failure_action_snapshot=step.failure_action,
                        status="WAITING",
                    ))
                task.status = "RUNNING"
                task.started_at = task.started_at or utcnow()
                session.commit()
                return True
            except (IntegrityError, OperationalError):
                session.rollback()
                return False

    def _fail_without_attempt(self, session: Session, task_node: TaskNode, reason: str) -> None:
        """在尚未真实执行时把不可运行节点收敛为 FAILED，不制造 Attempt。"""
        task_node.status = "FAILED"
        task_node.failure_reason = reason
        task_node.finished_at = utcnow()
        task_id = task_node.task_id
        aggregate_task(session, task_id)
        session.commit()

    def _execute_claimed(self, task_node_id: str) -> None:
        """执行已 Claim 节点，并保证释放进程内去重标记。"""
        try:
            if self._thread_stop.is_set():
                self._release_unstarted_claim(task_node_id)
                return
            self._execute(task_node_id)
        except _SchedulerStopping:
            logger.info("Scheduler 关闭，TaskNode 留待下次启动恢复 task_node_id=%s", task_node_id)
        finally:
            with self._guard:
                self._running.discard(task_node_id)

    def _execute(self, task_node_id: str) -> None:
        """传输包并严格按 sequence 串行执行当前 Attempt 的 Step。"""
        with self.factory() as session:
            task_node = session.scalar(
                select(TaskNode).where(TaskNode.id == task_node_id).options(
                    selectinload(TaskNode.task),
                    selectinload(TaskNode.attempts).selectinload(TaskAttempt.steps),
                )
            )
            if task_node is None or task_node.status != "RUNNING" or task_node.node_id is None:
                return
            task = task_node.task
            package = session.get(Package, task.package_id) if task.package_id else None
            attempt = task_node.attempts[-1]
            if self._thread_stop.is_set() and self._release_unstarted_claim(task_node_id):
                return
            if package is None:
                self._finish_node(session, task_node, attempt, "FAILED", "PACKAGE_UNAVAILABLE")
                return
            node_id = task_node.node_id
            remote_root = f"/var/lib/automation-center/tasks/{task.id}/attempt-{attempt.attempt_no}"
            archive_name = Path(package.storage_path).name
            remote_archive = f"{remote_root}/{archive_name}"
            remote_work = f"{remote_root}/work"
            salt_source = f"salt://{package.id}/v{package.revision}/{archive_name}"
            # 先结束读取事务再调用 Salt；包传输和解压期间不占用 SQLite 事务。
            session.commit()
            try:
                self.salt.transfer_package(node_id, salt_source, remote_archive)
                self.salt.prepare_workdir(node_id, remote_archive, remote_work)
            except Exception as exc:
                self._finish_node(session, task_node, attempt, "FAILED", f"PACKAGE_TRANSFER_FAILED: {exc}")
                return

            warning_messages: list[str] = []
            stopped = False
            for step in attempt.steps:
                # 同一节点内 Step 串行；ignore 只把失败降为警告，stop 会跳过后续 Step。
                result = self._execute_step(session, task_node, attempt, step, remote_work)
                if result != "SUCCESS":
                    if step.failure_action_snapshot == "ignore":
                        warning_messages.append(f"{step.name_snapshot}: {step.failure_reason or result}")
                        continue
                    stopped = True
                    for later in attempt.steps:
                        if later.sequence > step.sequence and later.status == "WAITING":
                            later.status = "SKIPPED"
                            later.failure_reason = "PREVIOUS_STEP_FAILED"
                    session.commit()
                    break
            if stopped:
                self._finish_node(session, task_node, attempt, "FAILED", next((s.failure_reason for s in attempt.steps if s.status == "FAILED"), "STEP_FAILED"))
            else:
                try:
                    self.salt.cleanup_workdir(node_id, remote_root)
                except Exception as exc:
                    warning_messages.append(f"工作目录清理失败: {exc}")
                if warning_messages:
                    attempt.warning_message = "; ".join(warning_messages)
                    task_node.has_warning = True
                self._finish_node(session, task_node, attempt, "SUCCESS", None)

    def _execute_step(self, session: Session, task_node: TaskNode, attempt: TaskAttempt, step: TaskStepResult, remote_work: str) -> str:
        """启动一个异步 Salt Job，增量采集日志并收敛 Step 状态。

        JID 在下发成功后立即提交，用于服务重启恢复。轮询中的 Salt 调用之间只
        提交最终状态，不持有写事务；异常会持续重试到 Step Timeout。
        """
        node_id = task_node.node_id
        assert node_id is not None
        remote_log_root = f"/var/lib/automation-center/tasks/{task_node.task_id}/attempt-{attempt.attempt_no}/logs"
        stdout_remote = f"{remote_log_root}/step-{step.sequence:02d}.stdout"
        stderr_remote = f"{remote_log_root}/step-{step.sequence:02d}.stderr"
        exit_remote = f"{remote_log_root}/step-{step.sequence:02d}.exit"
        local_root = self.settings.log_dir / task_node.task_id / task_node.id / f"attempt-{attempt.attempt_no}"
        local_root.mkdir(parents=True, exist_ok=True)
        stdout_local = local_root / f"step-{step.sequence:02d}.stdout"
        stderr_local = local_root / f"step-{step.sequence:02d}.stderr"
        step.stdout_path = str(stdout_local)
        step.stderr_path = str(stderr_local)
        step.status = "RUNNING"
        step.started_at = utcnow()
        session.commit()
        try:
            jid = self.salt.start_step(node_id, step.executor_type, step.script_snapshot, remote_work, stdout_remote, stderr_remote, exit_remote)
            # JID 是恢复监控的唯一远端凭据，必须在第一次轮询前持久化。
            step.salt_jid = jid
            session.commit()
        except Exception as exc:
            step.status = "FAILED"
            step.failure_reason = f"SALT_SUBMIT_FAILED: {exc}"
            step.finished_at = utcnow()
            session.commit()
            return "FAILED"
        started = time.monotonic()
        stdout_offset = 0
        stderr_offset = 0
        while True:
            if self._thread_stop.is_set():
                # JID 已在上方提交；退出本地监控不会终止远端命令，重启后按 JID 恢复。
                raise _SchedulerStopping
            try:
                stdout_offset = self._collect_log(node_id, stdout_remote, stdout_local, stdout_offset)
                stderr_offset = self._collect_log(node_id, stderr_remote, stderr_local, stderr_offset)
                result = self.salt.job_result(jid, node_id)
            except Exception:
                if self._thread_stop.wait(1):
                    raise _SchedulerStopping
                if time.monotonic() - started <= step.timeout_snapshot:
                    continue
                result = None
            if result and result.state in {"SUCCESS", "FAILED", "LOST"}:
                step.status = "SUCCESS" if result.state == "SUCCESS" else "FAILED"
                step.exit_code = result.exit_code
                step.failure_reason = result.failure_reason or (None if result.state == "SUCCESS" else "EXIT_CODE_NONZERO")
                step.finished_at = utcnow()
                session.commit()
                return step.status
            if time.monotonic() - started > step.timeout_snapshot:
                # 终止是尽力而为；无论 Salt 是否确认终止，本地最终都收敛为 TIMEOUT。
                try:
                    warning = self.salt.terminate_job(node_id, jid)
                except Exception as exc:
                    warning = f"终止请求失败: {exc}"
                step.status = "FAILED"
                step.exit_code = 124
                step.failure_reason = f"TIMEOUT; {warning}"
                step.finished_at = utcnow()
                session.commit()
                return "FAILED"
            if self._thread_stop.wait(1):
                raise _SchedulerStopping

    def _collect_log(self, node_id: str, remote: str, local: Path, offset: int) -> int:
        """从远端 offset 追加新字节到本地日志，返回下次采集起点。"""
        data, new_offset = self.salt.read_file(node_id, remote, offset)
        if data:
            with local.open("ab") as output:
                output.write(data)
        return new_offset

    def _finish_node(self, session: Session, task_node: TaskNode, attempt: TaskAttempt, status: str, reason: str | None) -> None:
        """原子完成 Attempt/TaskNode、释放节点锁，再重新聚合 Task 状态。"""
        attempt.status = status
        attempt.finished_at = utcnow()
        task_node.status = status
        task_node.failure_reason = reason
        task_node.finished_at = utcnow()
        session.execute(delete(NodeExecutionLock).where(NodeExecutionLock.task_node_id == task_node.id))
        task_id = task_node.task_id
        aggregate_task(session, task_id)
        session.commit()

    async def recover(self) -> None:
        """为数据库中所有 RUNNING TaskNode 启动恢复监控线程。"""
        with self.factory() as session:
            running = list(session.scalars(select(TaskNode).where(TaskNode.status == "RUNNING")))
        for task_node in running:
            if self._stop.is_set():
                break
            with self._guard:
                self._running.add(task_node.id)
            asyncio.get_running_loop().run_in_executor(self.executor, self._recover_node, task_node.id)

    def _recover_node(self, task_node_id: str) -> None:
        """仅凭已持久化 JID 续接运行中 Step，绝不自动重复下发未知命令。"""
        try:
            with self.factory() as session:
                task_node = session.scalar(
                    select(TaskNode).where(TaskNode.id == task_node_id).options(
                        selectinload(TaskNode.task),
                        selectinload(TaskNode.attempts).selectinload(TaskAttempt.steps),
                    )
                )
                if task_node is None or not task_node.attempts:
                    return
                if self._thread_stop.is_set():
                    raise _SchedulerStopping
                attempt = task_node.attempts[-1]
                running_step = next((step for step in attempt.steps if step.status == "RUNNING"), None)
                if running_step is None or running_step.salt_jid is None or task_node.node_id is None:
                    # 缺少恢复所需事实时宁可失败，也不能冒险重复执行有副作用的 Step。
                    self._finish_node(session, task_node, attempt, "FAILED", "EXECUTION_STATE_LOST")
                    return
                recovered_status = self._monitor_recovered_step(session, task_node, attempt, running_step)
                if recovered_status != "SUCCESS" and running_step.failure_action_snapshot == "stop":
                    for later in attempt.steps:
                        if later.sequence > running_step.sequence and later.status == "WAITING":
                            later.status = "SKIPPED"
                            later.failure_reason = "PREVIOUS_STEP_FAILED"
                    session.commit()
                    self._finish_node(session, task_node, attempt, "FAILED", running_step.failure_reason or "STEP_FAILED")
                    return
                warning_messages = [] if recovered_status == "SUCCESS" else [f"{running_step.name_snapshot}: {running_step.failure_reason}"]
                remote_work = f"/var/lib/automation-center/tasks/{task_node.task_id}/attempt-{attempt.attempt_no}/work"
                for step in attempt.steps:
                    if step.status != "WAITING":
                        continue
                    status = self._execute_step(session, task_node, attempt, step, remote_work)
                    if status != "SUCCESS":
                        if step.failure_action_snapshot == "ignore":
                            warning_messages.append(f"{step.name_snapshot}: {step.failure_reason}")
                            continue
                        for later in attempt.steps:
                            if later.sequence > step.sequence and later.status == "WAITING":
                                later.status = "SKIPPED"
                                later.failure_reason = "PREVIOUS_STEP_FAILED"
                        session.commit()
                        self._finish_node(session, task_node, attempt, "FAILED", step.failure_reason or "STEP_FAILED")
                        return
                session.commit()
                remote_root = f"/var/lib/automation-center/tasks/{task_node.task_id}/attempt-{attempt.attempt_no}"
                try:
                    self.salt.cleanup_workdir(task_node.node_id, remote_root)
                except Exception as exc:
                    warning_messages.append(f"工作目录清理失败: {exc}")
                if warning_messages:
                    attempt.warning_message = "; ".join(warning_messages)
                    task_node.has_warning = True
                self._finish_node(session, task_node, attempt, "SUCCESS", None)
        except _SchedulerStopping:
            logger.info("Scheduler 关闭，恢复监控交回下次启动 task_node_id=%s", task_node_id)
        finally:
            with self._guard:
                self._running.discard(task_node_id)

    def _monitor_recovered_step(self, session: Session, task_node: TaskNode, attempt: TaskAttempt, step: TaskStepResult) -> str:
        """从本地日志末尾续采已提交 Job，并应用原 Step Timeout。"""
        assert task_node.node_id is not None and step.salt_jid is not None
        node_id = task_node.node_id
        remote_log_root = f"/var/lib/automation-center/tasks/{task_node.task_id}/attempt-{attempt.attempt_no}/logs"
        stdout_remote = f"{remote_log_root}/step-{step.sequence:02d}.stdout"
        stderr_remote = f"{remote_log_root}/step-{step.sequence:02d}.stderr"
        stdout_local = Path(step.stdout_path or self.settings.log_dir / task_node.task_id / task_node.id / f"attempt-{attempt.attempt_no}" / f"step-{step.sequence:02d}.stdout")
        stderr_local = Path(step.stderr_path or self.settings.log_dir / task_node.task_id / task_node.id / f"attempt-{attempt.attempt_no}" / f"step-{step.sequence:02d}.stderr")
        stdout_local.parent.mkdir(parents=True, exist_ok=True)
        stdout_offset = stdout_local.stat().st_size if stdout_local.exists() else 0
        stderr_offset = stderr_local.stat().st_size if stderr_local.exists() else 0
        elapsed = max(0.0, (utcnow() - (step.started_at or utcnow())).total_seconds())
        started = time.monotonic() - elapsed
        while True:
            if self._thread_stop.is_set():
                raise _SchedulerStopping
            try:
                stdout_offset = self._collect_log(node_id, stdout_remote, stdout_local, stdout_offset)
                stderr_offset = self._collect_log(node_id, stderr_remote, stderr_local, stderr_offset)
                result = self.salt.job_result(step.salt_jid, node_id)
            except Exception:
                result = None
            if result and result.state == "LOST":
                # Salt 既无返回又无 Job 记录时执行事实不可证明，禁止重放并明确标记丢失。
                step.status = "FAILED"
                step.failure_reason = "EXECUTION_STATE_LOST"
                step.finished_at = utcnow()
                session.commit()
                return "FAILED"
            if result and result.state in {"SUCCESS", "FAILED"}:
                step.status = result.state
                step.exit_code = result.exit_code
                step.failure_reason = result.failure_reason or (None if result.state == "SUCCESS" else "EXIT_CODE_NONZERO")
                step.finished_at = utcnow()
                session.commit()
                return step.status
            if time.monotonic() - started > step.timeout_snapshot:
                try:
                    warning = self.salt.terminate_job(node_id, step.salt_jid)
                except Exception as exc:
                    warning = f"终止请求失败: {exc}"
                step.status = "FAILED"
                step.exit_code = 124
                step.failure_reason = f"TIMEOUT; {warning}"
                step.finished_at = utcnow()
                session.commit()
                return "FAILED"
            if self._thread_stop.wait(1):
                raise _SchedulerStopping

    async def cleanup_expired(self) -> None:
        """每小时清理过期执行日志，并为失败 Attempt 回收远端工作目录。"""
        marker = getattr(self, "_last_cleanup", 0.0)
        if time.monotonic() - marker < 3600:
            return
        self._last_cleanup = time.monotonic()
        local_targets: list[Path] = []
        remote_targets: list[tuple[str, str]] = []
        with self.factory() as session:
            setting = session.get(SystemSetting, "execution_log_retention_days")
            days = int(setting.value) if setting else self.settings.execution_log_retention_days
            cutoff = utcnow() - timedelta(days=days)
            expired = list(session.scalars(select(Task).where(Task.finished_at.is_not(None), Task.finished_at < cutoff)))
            for task in expired:
                # 仅删除文件日志；Task、Attempt 和 StepResult 等结构化历史长期保留。
                local_targets.append(self.settings.log_dir / task.id)
            failed_cutoff = utcnow() - timedelta(days=self.settings.failed_work_retention_days)
            failed_nodes = session.scalars(
                select(TaskNode).where(
                    TaskNode.status == "FAILED",
                    TaskNode.finished_at.is_not(None),
                    TaskNode.finished_at < failed_cutoff,
                    TaskNode.node_id.is_not(None),
                ).options(selectinload(TaskNode.attempts))
            ).all()
            for node in failed_nodes:
                for attempt in node.attempts:
                    remote_targets.append((node.node_id, f"/var/lib/automation-center/tasks/{node.task_id}/attempt-{attempt.attempt_no}"))
        if local_targets:
            # 大日志目录可能包含大量文件，必须在线程中删除，不能阻塞 FastAPI 事件循环。
            await asyncio.to_thread(self._cleanup_log_dirs, local_targets)
        if remote_targets:
            await asyncio.to_thread(self._cleanup_remote_workdirs, remote_targets)

    @staticmethod
    def _cleanup_log_dirs(targets: list[Path]) -> None:
        """在工作线程逐个删除过期本地日志目录。"""

        for target in targets:
            shutil.rmtree(target, ignore_errors=True)

    def _cleanup_remote_workdirs(self, targets: list[tuple[str, str]]) -> None:
        """逐个尽力清理远端目录，离线节点留待下一轮保留期扫描。"""
        for node_id, workdir in targets:
            try:
                self.salt.cleanup_workdir(node_id, workdir)
            except Exception:
                # 离线节点会在下一轮保留期扫描中再次尝试。
                continue
