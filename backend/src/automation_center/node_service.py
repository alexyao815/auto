"""Salt 节点探测与数据库节点视图更新之间的两阶段边界。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .models import Node, utcnow
from .salt import SaltAdapter


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class NodeSnapshot:
    """一次 Salt 探测结果；对象本身不持有数据库 Session。"""

    node_id: str
    online: bool
    hostname: str | None = None
    management_ip: str | None = None


def probe_node(salt: SaltAdapter, node_id: str) -> NodeSnapshot:
    """首次接入时探测单个 Minion，并只读取 hostname/IP 基础资料。"""

    try:
        online = salt.ping(node_id)
    except Exception:
        logger.exception("Salt 节点在线探测失败 node_id=%s", node_id)
        return NodeSnapshot(node_id=node_id, online=False)

    if not online:
        return NodeSnapshot(node_id=node_id, online=False)

    try:
        info = salt.node_info(node_id)
        return NodeSnapshot(
            node_id=node_id,
            online=True,
            hostname=info.get("hostname", node_id),
            management_ip=info.get("management_ip"),
        )
    except Exception:
        # 详情失败不能抹掉已经取得的在线状态和数据库中的旧属性/角色。
        logger.exception("Salt 节点详情探测失败 node_id=%s", node_id)
        return NodeSnapshot(node_id=node_id, online=online)


def collect_node_snapshots(salt: SaltAdapter, known_node_ids: Iterable[str]) -> list[NodeSnapshot]:
    """用一次批量 ``test.ping`` 生成在线快照，不采集 grains/进程/服务。"""

    accepted = set(salt.accepted_keys())
    accepted_ids = sorted(accepted)
    try:
        online_by_id = salt.ping_many(accepted_ids)
    except Exception:
        # 整批探测失败时保守标记 Offline；Scheduler 下个周期会重试。
        logger.exception("Salt 批量节点在线探测失败")
        online_by_id = {}
    return [
        NodeSnapshot(node_id=node_id, online=node_id in accepted and online_by_id.get(node_id) is True)
        for node_id in sorted(set(known_node_ids) | accepted)
    ]


def apply_node_snapshots(session: Session, snapshots: Iterable[NodeSnapshot]) -> list[Node]:
    """在单个短事务中更新节点资料和在线状态，不发起任何 Salt 调用。"""

    snapshot_list = list(snapshots)
    nodes = list(session.scalars(select(Node).options(selectinload(Node.roles))).unique())
    by_id = {node.id: node for node in nodes}
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
    return sorted(nodes, key=lambda node: node.hostname)
