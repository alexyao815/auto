"""任务目标解析、永久幂等、FIFO 入队、状态聚合、Retry 与 Cancel。"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session, selectinload

from .models import Node, Package, QueueCounter, Task, TaskNode, utcnow


TERMINAL_NODE_STATUSES = {"SUCCESS", "FAILED", "CANCELLED"}


def effective_roles(node: Node) -> list[str]:
    """返回自动识别与人工维护角色的去重并集。"""

    return sorted({role.role for role in node.roles})


def resolve_nodes(session: Session, role_names: list[str], node_ids: list[str]) -> tuple[list[Node], list[dict]]:
    """合并角色和直接节点选择、去重并排除 Disabled 节点。"""

    nodes = list(session.scalars(select(Node).options(selectinload(Node.roles))).all())
    # 以 Node ID 为键天然实现“角色选择 + 直接选择”的并集去重。
    selected: dict[str, Node] = {node.id: node for node in nodes if node.id in node_ids}
    requested_roles = set(role_names)
    if requested_roles:
        for node in nodes:
            if requested_roles.intersection(effective_roles(node)):
                selected[node.id] = node
    missing = sorted(set(node_ids) - {node.id for node in nodes})
    if missing:
        raise HTTPException(status_code=422, detail=f"节点不存在: {', '.join(missing)}")
    # Disabled 是调度禁用开关：混合目标中直接排除，而不是产生确认警告。
    selected = {node_id: node for node_id, node in selected.items() if node.enabled}
    if not selected:
        raise HTTPException(status_code=422, detail="至少选择一个有效目标节点")
    warnings: list[dict] = []
    for node in selected.values():
        if node.online_status != "ONLINE":
            warnings.append({"node_id": node.id, "type": "OFFLINE", "message": "节点当前离线，执行时将失败"})
    return sorted(selected.values(), key=lambda item: item.id), warnings


def task_preview(session: Session, package_id: str, role_names: list[str], node_ids: list[str]) -> dict:
    """返回创建任务前的实际节点、Package Revision 和需二次确认的风险。"""

    package = session.get(Package, package_id)
    if package is None:
        raise HTTPException(status_code=404, detail="Package 不存在")
    nodes, warnings = resolve_nodes(session, role_names, node_ids)
    target_roles = set(json.loads(package.target_roles_json))
    if target_roles:
        for node in nodes:
            roles = set(effective_roles(node))
            if not roles.intersection(target_roles):
                warnings.append({"node_id": node.id, "type": "ROLE_MISMATCH", "message": "节点角色与 Package target_roles 不匹配"})
    return {
        "package": {"id": package.id, "name": package.name, "revision": package.revision, "description": package.description},
        "nodes": [
            {
                "id": node.id,
                "hostname": node.hostname,
                "management_ip": node.management_ip,
                "online_status": node.online_status,
                "roles": effective_roles(node),
            }
            for node in nodes
        ],
        "warnings": warnings,
    }


def canonical_request_hash(package_id: str, role_names: list[str], node_ids: list[str], remark: str, confirmed_warnings: list[str]) -> str:
    """规范化集合型字段后计算请求 Hash，使顺序差异不破坏幂等。"""

    payload = {
        "package_id": package_id,
        "role_names": sorted(set(role_names)),
        "node_ids": sorted(set(node_ids)),
        "remark": remark,
        "confirmed_warnings": sorted(set(confirmed_warnings)),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _next_queue_values(session: Session, count: int) -> list[int]:
    """在当前写事务中分配连续、单调的全局 queue_seq。"""

    counter = session.get(QueueCounter, "task_node")
    if counter is None:
        counter = QueueCounter(name="task_node", value=0)
        session.add(counter)
        session.flush()
    start = counter.value + 1
    counter.value += count
    session.flush()
    return list(range(start, start + count))


def create_task(
    session: Session,
    package_id: str,
    role_names: list[str],
    node_ids: list[str],
    remark: str,
    confirmed_warnings: list[str],
    idempotency_key: str,
) -> tuple[Task, bool]:
    """重新校验并创建任务快照，但把最终提交留给 API 编排层。

    为取得 SQLite 写锁，本函数会先提交无业务写入的 Preview 读事务。新 Task
    只执行 ``flush``，调用方必须在同一事务中写入审计后提交；异常时回滚会同时
    撤销 Task、TaskNode 和队列计数器变更。
    """

    if not idempotency_key or len(idempotency_key) > 128:
        raise HTTPException(status_code=422, detail="Idempotency-Key 必须存在且不超过 128 字符")
    request_hash = canonical_request_hash(package_id, role_names, node_ids, remark, confirmed_warnings)
    # 快速路径处理绝大多数重放；写锁内还会再次检查并发首次创建。
    existing = session.scalar(select(Task).where(Task.idempotency_key == idempotency_key))
    if existing:
        if existing.request_hash != request_hash:
            raise HTTPException(status_code=409, detail="Idempotency-Key 已被不同请求使用")
        return existing, True
    preview = task_preview(session, package_id, role_names, node_ids)
    required_warning_types = {warning["type"] for warning in preview["warnings"]}
    if not required_warning_types.issubset(set(confirmed_warnings)):
        missing = sorted(required_warning_types - set(confirmed_warnings))
        raise HTTPException(status_code=409, detail=f"以下警告尚未确认: {', '.join(missing)}")
    package = session.get(Package, package_id)
    assert package is not None
    nodes = [session.get(Node, item["id"]) for item in preview["nodes"]]
    nodes = [node for node in nodes if node is not None]
    try:
        # 结束 Preview 产生的读事务，再获取 SQLite 写锁，避免读事务升级死锁。
        session.commit()
        if session.bind and session.bind.dialect.name == "sqlite":
            session.execute(text("BEGIN IMMEDIATE"))
        # 两个请求可能同时在快速路径读到不存在，必须在写锁内重新确认 Key。
        existing = session.scalar(select(Task).where(Task.idempotency_key == idempotency_key))
        if existing:
            if existing.request_hash != request_hash:
                raise HTTPException(status_code=409, detail="Idempotency-Key 已被不同请求使用")
            # 该分支没有业务写入；释放 BEGIN IMMEDIATE，避免重放请求占用写锁。
            session.rollback()
            return existing, True
        queue_values = _next_queue_values(session, len(nodes))
        task = Task(
            id=str(uuid.uuid4()),
            package_id=package.id,
            package_name_snapshot=package.name,
            package_revision_snapshot=package.revision,
            package_description_snapshot=package.description,
            status="WAITING",
            target_node_count=len(nodes),
            remark=remark,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        session.add(task)
        for node, queue_seq in zip(nodes, queue_values, strict=True):
            warning = any(item["node_id"] == node.id for item in preview["warnings"])
            task.nodes.append(TaskNode(
                id=str(uuid.uuid4()),
                node_id=node.id,
                hostname_snapshot=node.hostname,
                management_ip_snapshot=node.management_ip,
                roles_snapshot_json=json.dumps(effective_roles(node), ensure_ascii=False),
                status="WAITING",
                has_warning=warning,
                queue_entered_at=utcnow(),
                queue_seq=queue_seq,
            ))
        # 审计必须与 Task 创建原子提交，Service 不得提前 commit。
        session.flush()
        return task, False
    except Exception:
        session.rollback()
        raise


def aggregate_task(session: Session, task_id: str) -> Task:
    """依据全部 TaskNode 状态重算聚合字段，不提交调用方事务。"""

    # autoflush=False，先落下调用方在同一事务内修改的 TaskNode 状态再聚合。
    session.flush()
    task = session.scalar(select(Task).where(Task.id == task_id).options(selectinload(Task.nodes)))
    if task is None:
        raise HTTPException(status_code=404, detail="Task 不存在")
    counts = Counter(node.status for node in task.nodes)
    task.success_count = counts["SUCCESS"]
    task.failed_count = counts["FAILED"]
    task.cancelled_count = counts["CANCELLED"]
    total = len(task.nodes)
    if counts["RUNNING"]:
        status = "RUNNING"
    elif counts["WAITING"] == total:
        status = "WAITING"
    elif counts["WAITING"]:
        status = "RUNNING"
    elif counts["SUCCESS"] == total:
        status = "SUCCESS"
    elif counts["CANCELLED"] == total:
        status = "CANCELLED"
    elif counts["FAILED"] == total:
        status = "FAILED"
    elif counts["SUCCESS"] and (counts["FAILED"] or counts["CANCELLED"]):
        status = "PARTIAL_SUCCESS"
    elif counts["FAILED"]:
        status = "FAILED"
    else:
        status = "CANCELLED"
    task.status = status
    if status == "RUNNING" and task.started_at is None:
        task.started_at = utcnow()
    if status in {"SUCCESS", "PARTIAL_SUCCESS", "FAILED", "CANCELLED"}:
        task.finished_at = task.finished_at or utcnow()
    else:
        task.finished_at = None
    session.flush()
    return task


def retry_task_node(session: Session, task_node: TaskNode) -> TaskNode:
    """把 FAILED TaskNode 重新排到队尾；Attempt 在后续 Claim 时创建。"""

    if task_node.status != "FAILED":
        raise HTTPException(status_code=409, detail="只有 FAILED TaskNode 允许 Retry")
    task = session.get(Task, task_node.task_id)
    if task is None or task.package_id is None:
        raise HTTPException(status_code=409, detail="原 Package 已删除，禁止 Retry")
    package = session.get(Package, task.package_id)
    if package is None or package.revision != task.package_revision_snapshot:
        raise HTTPException(status_code=409, detail="原 Package Revision 已不存在，禁止 Retry")
    if task_node.node_id is None:
        raise HTTPException(status_code=409, detail="原节点已删除，禁止 Retry")
    node = session.get(Node, task_node.node_id)
    if node is None or not node.enabled:
        raise HTTPException(status_code=409, detail="节点不存在或已 Disabled，禁止 Retry")
    try:
        # Retry 与新建任务共用持久化计数器，不能插入已有 Waiting 项之前。
        session.commit()
        if session.bind and session.bind.dialect.name == "sqlite":
            session.execute(text("BEGIN IMMEDIATE"))
        task_node.queue_seq = _next_queue_values(session, 1)[0]
        task_node.queue_entered_at = utcnow()
        task_node.status = "WAITING"
        task_node.failure_reason = None
        task_node.finished_at = None
        task.status = "RUNNING" if any(item.status in {"SUCCESS", "RUNNING"} for item in task.nodes) else "WAITING"
        task.finished_at = None
        session.commit()
        return task_node
    except Exception:
        session.rollback()
        raise


def cancel_task_node(session: Session, task_node: TaskNode) -> TaskNode:
    """使用状态 CAS 取消 Waiting 节点；与 Scheduler Claim 只能有一方成功。"""

    if task_node.status != "WAITING":
        raise HTTPException(status_code=409, detail="只有 WAITING TaskNode 允许取消")
    task_node_id = task_node.id
    task_id = task_node.task_id
    # 丢弃加载 TaskNode 时建立的读快照，让 UPDATE 看到最新 Claim 结果。
    session.rollback()
    changed = session.query(TaskNode).filter(TaskNode.id == task_node_id, TaskNode.status == "WAITING").update({
        TaskNode.status: "CANCELLED",
        TaskNode.finished_at: utcnow(),
    })
    if changed != 1:
        session.rollback()
        raise HTTPException(status_code=409, detail="TaskNode 已被调度或取消")
    aggregate_task(session, task_id)
    session.commit()
    return session.get(TaskNode, task_node_id)
