"""Salt 节点探测与数据库节点视图更新之间的两阶段边界。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .models import Node, NodeRole, RoleRule, utcnow
from .salt import SaltAdapter


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class NodeSnapshot:
    """一次 Salt 探测结果；对象本身不持有数据库 Session。"""

    node_id: str
    online: bool
    hostname: str | None = None
    management_ip: str | None = None
    processes: tuple[str, ...] = ()
    services: tuple[str, ...] = ()
    details_available: bool = False


def probe_node(salt: SaltAdapter, node_id: str) -> NodeSnapshot:
    """在数据库事务外探测单个已接受 Minion，并保守处理详情查询失败。"""

    try:
        online = salt.ping(node_id)
    except Exception:
        logger.exception("Salt 节点在线探测失败 node_id=%s", node_id)
        return NodeSnapshot(node_id=node_id, online=False)

    try:
        # 即使 ping 为 False 也尝试读取详情：Fake Salt 和部分短暂抖动场景仍可
        # 返回最近信息，可保留旧实现中新发现节点的角色识别语义。
        info = salt.node_info(node_id)
        return NodeSnapshot(
            node_id=node_id,
            online=online,
            hostname=info.get("hostname", node_id),
            management_ip=info.get("management_ip"),
            processes=tuple(info.get("processes", [])),
            services=tuple(info.get("services", [])),
            details_available=True,
        )
    except Exception:
        # 详情失败不能抹掉已经取得的在线状态和数据库中的旧属性/角色。
        logger.exception("Salt 节点详情探测失败 node_id=%s", node_id)
        return NodeSnapshot(node_id=node_id, online=online)


def collect_node_snapshots(salt: SaltAdapter, known_node_ids: Iterable[str]) -> list[NodeSnapshot]:
    """取得 Salt 接受列表，并为已知节点和新节点生成完整状态快照。"""

    accepted = set(salt.accepted_keys())
    snapshots: list[NodeSnapshot] = []
    for node_id in sorted(set(known_node_ids) | accepted):
        snapshots.append(probe_node(salt, node_id) if node_id in accepted else NodeSnapshot(node_id=node_id, online=False))
    return snapshots


def apply_node_snapshots(session: Session, snapshots: Iterable[NodeSnapshot]) -> list[Node]:
    """在单个短事务中更新节点事实和自动角色，不发起任何 Salt 调用。"""

    snapshot_list = list(snapshots)
    nodes = list(session.scalars(select(Node).options(selectinload(Node.roles))).unique())
    by_id = {node.id: node for node in nodes}
    rules = list(session.scalars(select(RoleRule).where(RoleRule.enabled.is_(True))))
    for snapshot in snapshot_list:
        node = by_id.get(snapshot.node_id)
        if node is None:
            node = Node(id=snapshot.node_id, hostname=snapshot.hostname or snapshot.node_id)
            session.add(node)
            by_id[node.id] = node
            nodes.append(node)
        if snapshot.hostname:
            node.hostname = snapshot.hostname
        if snapshot.management_ip:
            node.management_ip = snapshot.management_ip
        node.online_status = "ONLINE" if snapshot.online else "OFFLINE"
        node.last_check_time = utcnow()
        if snapshot.details_available and not node.role_override:
            process_text = "\n".join(snapshot.processes).lower()
            service_text = "\n".join(snapshot.services).lower()
            detected = sorted({
                rule.role
                for rule in rules
                if rule.pattern.lower() in (service_text if rule.matcher_type == "service" else process_text)
            })
            existing_auto = [role for role in node.roles if role.source == "auto"]
            if {role.role for role in existing_auto} != set(detected):
                # SQLite 的唯一约束是 (node_id, role, source)。先明确落地删除，
                # 再插入新集合，避免 ORM 在同一 flush 中先 INSERT 后 DELETE。
                for role in existing_auto:
                    node.roles.remove(role)
                if existing_auto:
                    session.flush()
                for role in detected:
                    node.roles.append(NodeRole(role=role, source="auto"))
    return sorted(nodes, key=lambda node: node.hostname)
