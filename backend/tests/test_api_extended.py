from __future__ import annotations

import sqlite3
import time

from automation_center.models import Node, TaskNode

from .conftest import build_bundle


def upload(client, auth, bundle):
    with bundle.open("rb") as stream:
        return client.post("/api/v1/packages", files={"file": (bundle.name, stream, "application/gzip")}, headers=auth)


def test_sqlite_write_lock_returns_retryable_503(client, settings):
    database_path = settings.database_url.removeprefix("sqlite:///")
    with sqlite3.connect(database_path, timeout=0) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        started = time.perf_counter()
        response = client.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong"})
        elapsed = time.perf_counter() - started

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.headers["X-Request-ID"]
    assert response.json()["detail"] == "数据库正在处理其他写操作，请稍后重试"
    assert 4.5 <= elapsed < 7


def test_node_manual_roles_restore_disable_and_delete(client, auth, salt):
    client.post("/api/v1/nodes/refresh", headers=auth)
    response = client.patch("/api/v1/nodes/demo-node", json={"roles": ["network", "ceph"]}, headers=auth)
    assert response.json()["role_override"] is False
    assert response.json()["roles"] == ["ceph", "network"]
    assert response.json()["role_details"] == [
        {"role": "ceph", "sources": ["manual"]},
        {"role": "network", "sources": ["manual"]},
    ]
    restored = client.patch("/api/v1/nodes/demo-node", json={"restore_auto_roles": True}, headers=auth)
    assert restored.json()["role_override"] is False
    assert restored.json()["roles"] == []
    disabled = client.patch("/api/v1/nodes/demo-node", json={"enabled": False}, headers=auth)
    assert disabled.json()["enabled"] is False
    assert client.patch("/api/v1/nodes/missing", json={"enabled": True}, headers=auth).status_code == 404
    assert client.delete("/api/v1/nodes/missing", headers=auth).status_code == 404
    online_delete = client.delete("/api/v1/nodes/demo-node", headers=auth)
    assert online_delete.status_code == 409
    assert online_delete.json()["detail"] == "仅允许删除 Offline 节点"
    salt._accepted["demo-node"]["offline"] = True
    refreshed = client.post("/api/v1/nodes/refresh", headers=auth)
    assert next(node for node in refreshed.json() if node["id"] == "demo-node")["online_status"] == "OFFLINE"
    assert client.delete("/api/v1/nodes/demo-node", headers=auth).status_code == 204
    assert all(node["id"] != "demo-node" for node in client.get("/api/v1/nodes").json())
    audits = client.get("/api/v1/audit-logs").json()
    assert any(item["operation"] == "DELETE_NODE" and item["object_id"] == "demo-node" for item in audits)


def test_package_update_active_lock_and_delete(client, auth, tmp_path, salt):
    client.post("/api/v1/nodes/refresh", headers=auth)
    client.patch("/api/v1/nodes/demo-node", json={"roles": ["compute"]}, headers=auth)
    first = upload(client, auth, build_bundle(tmp_path, name="lifecycle"))
    assert first.status_code == 201
    package = first.json()
    assert client.get(f"/api/v1/packages/{package['id']}").json()["steps"][0]["name"] == "fix"
    duplicate = upload(client, auth, build_bundle(tmp_path, name="lifecycle"))
    assert duplicate.status_code == 409
    body = {"package_id": package["id"], "node_ids": ["demo-node"], "role_names": [], "remark": "", "confirmed_warnings": []}
    task = client.post("/api/v1/tasks", json=body, headers={**auth, "Idempotency-Key": "active-package"})
    assert task.status_code == 201
    salt._accepted["demo-node"]["offline"] = True
    client.post("/api/v1/nodes/refresh", headers=auth)
    assert client.delete("/api/v1/nodes/demo-node", headers=auth).status_code == 409
    replacement = build_bundle(tmp_path, name="lifecycle", executor_type="python", script="scripts/fix.py")
    with replacement.open("rb") as stream:
        locked = client.put(f"/api/v1/packages/{package['id']}/bundle", files={"file": (replacement.name, stream, "application/gzip")}, headers=auth)
    assert locked.status_code == 409
    client.post(f"/api/v1/tasks/{task.json()['id']}/cancel", headers=auth)
    with replacement.open("rb") as stream:
        updated = client.put(f"/api/v1/packages/{package['id']}/bundle", files={"file": (replacement.name, stream, "application/gzip")}, headers=auth)
    assert updated.status_code == 200
    assert updated.json()["revision"] == 2
    wrong_name = build_bundle(tmp_path, name="other-name")
    with wrong_name.open("rb") as stream:
        wrong = client.put(f"/api/v1/packages/{package['id']}/bundle", files={"file": (wrong_name.name, stream, "application/gzip")}, headers=auth)
    assert wrong.status_code == 422
    assert client.delete("/api/v1/packages/missing", headers=auth).status_code == 404
    assert client.delete(f"/api/v1/packages/{package['id']}", headers=auth).status_code == 204
    assert client.get(f"/api/v1/packages/{package['id']}").status_code == 404


def test_task_cancel_node_copy_and_missing_routes(client, auth, tmp_path):
    client.post("/api/v1/nodes/refresh", headers=auth)
    client.patch("/api/v1/nodes/demo-node", json={"roles": ["compute"]}, headers=auth)
    package = upload(client, auth, build_bundle(tmp_path, name="task-actions")).json()
    body = {"package_id": package["id"], "node_ids": ["demo-node"], "role_names": [], "remark": "copy me", "confirmed_warnings": []}
    task = client.post("/api/v1/tasks", json=body, headers={**auth, "Idempotency-Key": "actions"}).json()
    node = task["nodes"][0]
    copied = client.get(f"/api/v1/tasks/{task['id']}/copy-template")
    assert copied.status_code == 200
    assert copied.json()["remark"] == "copy me"
    cancelled = client.post(f"/api/v1/tasks/{task['id']}/nodes/{node['id']}/cancel", headers=auth)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"
    assert client.post(f"/api/v1/tasks/{task['id']}/nodes/{node['id']}/cancel", headers=auth).status_code == 409
    assert client.post("/api/v1/tasks/missing/cancel", headers=auth).status_code == 404
    assert client.get("/api/v1/tasks/missing").status_code == 404
    assert client.get("/api/v1/tasks/missing/copy-template").status_code == 404


def test_periodic_probe_keeps_roles_manual_and_settings_audit_limit(client, auth, salt):
    assert client.get("/api/v1/health/ready").status_code == 200
    rules = [{"role": "storage", "matcher_type": "process", "pattern": "storage-daemon", "enabled": True}]
    changed = client.patch("/api/v1/settings", json={"role_detection_rules": rules, "node_status_check_interval": 5}, headers=auth)
    assert changed.status_code == 200
    assert changed.json()["role_detection_rules"] == rules
    salt._accepted["demo-node"]["processes"] = ["storage-daemon"]
    refreshed = client.post("/api/v1/nodes/refresh", headers=auth)
    assert refreshed.status_code == 200
    assert next(node for node in refreshed.json() if node["id"] == "demo-node")["roles"] == []
    manual = client.patch("/api/v1/nodes/demo-node", json={"roles": ["storage"]}, headers=auth)
    assert manual.json()["roles"] == ["storage"]
    refreshed = client.post("/api/v1/nodes/refresh", headers=auth)
    assert next(node for node in refreshed.json() if node["id"] == "demo-node")["roles"] == ["storage"]
    bad = client.patch("/api/v1/settings", json={"role_detection_rules": [{"role": "x", "matcher_type": "service", "pattern": "p"}]}, headers=auth)
    assert bad.status_code == 422
    assert len(client.get("/api/v1/audit-logs?limit=99999").json()) >= 1
