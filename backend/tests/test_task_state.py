from __future__ import annotations

import json
import uuid

import pytest
from fastapi import HTTPException

from automation_center.models import Node, NodeRole, Package, Task, TaskNode
from automation_center.task_service import aggregate_task, effective_roles, resolve_nodes


def make_task(session, statuses):
    package = Package(id=str(uuid.uuid4()), name=f"pkg-{uuid.uuid4()}", revision=1, storage_path="x", sha256="0" * 64, manifest_json="{}")
    task = Task(id=str(uuid.uuid4()), package_id=package.id, package_name_snapshot=package.name, package_revision_snapshot=1, status="WAITING", target_node_count=len(statuses), idempotency_key=str(uuid.uuid4()), request_hash="x")
    session.add_all([package, task])
    for index, status in enumerate(statuses):
        task.nodes.append(TaskNode(id=str(uuid.uuid4()), node_id=None, hostname_snapshot=f"n{index}", roles_snapshot_json="[]", status=status, queue_seq=index + 1))
    session.commit()
    return task


@pytest.mark.parametrize(
    "statuses, expected",
    [
        (["WAITING", "WAITING"], "WAITING"),
        (["RUNNING", "WAITING"], "RUNNING"),
        (["SUCCESS", "WAITING"], "RUNNING"),
        (["SUCCESS", "SUCCESS"], "SUCCESS"),
        (["FAILED", "FAILED"], "FAILED"),
        (["CANCELLED", "CANCELLED"], "CANCELLED"),
        (["SUCCESS", "FAILED"], "PARTIAL_SUCCESS"),
        (["SUCCESS", "CANCELLED"], "PARTIAL_SUCCESS"),
        (["FAILED", "CANCELLED"], "FAILED"),
    ],
)
def test_task_aggregation(client, statuses, expected):
    factory = client.app.state.session_factory
    with factory() as session:
        task = make_task(session, statuses)
        assert aggregate_task(session, task.id).status == expected


def test_effective_roles_and_node_resolution(client):
    factory = client.app.state.session_factory
    with factory() as session:
        enabled = Node(id="enabled", hostname="enabled", online_status="ONLINE", enabled=True)
        enabled.roles.extend([NodeRole(role="compute", source="auto"), NodeRole(role="network", source="manual")])
        disabled = Node(id="disabled", hostname="disabled", online_status="ONLINE", enabled=False)
        session.add_all([enabled, disabled]); session.commit()
        assert effective_roles(enabled) == ["compute"]
        enabled.role_override = True
        assert effective_roles(enabled) == ["network"]
        selected, warnings = resolve_nodes(session, ["network"], ["enabled"])
        assert [node.id for node in selected] == ["enabled"] and warnings == []
        selected, warnings = resolve_nodes(session, ["network"], ["enabled", "disabled"])
        assert [node.id for node in selected] == ["enabled"] and warnings == []
        with pytest.raises(HTTPException): resolve_nodes(session, [], ["missing"])
        with pytest.raises(HTTPException): resolve_nodes(session, [], ["disabled"])
        with pytest.raises(HTTPException): resolve_nodes(session, [], [])
