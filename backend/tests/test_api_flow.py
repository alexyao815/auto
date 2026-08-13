from __future__ import annotations

import time

from fastapi.testclient import TestClient

from .conftest import build_bundle


def test_auth_session_and_csrf(client: TestClient):
    assert client.get("/api/v1/health/live").json() == {"status": "ok"}
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "bad"}).status_code == 401
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-password"})
    assert login.status_code == 200
    assert login.cookies.get("automation_center_session")
    assert client.get("/api/v1/auth/me").json()["username"] == "admin"
    assert client.post("/api/v1/nodes/refresh").status_code == 403
    headers = {"X-CSRF-Token": login.json()["csrf_token"]}
    assert client.post("/api/v1/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401


def test_node_package_task_scheduler_flow(client, auth, salt, tmp_path):
    pending = client.get("/api/v1/nodes/pending").json()
    assert pending == [{"id": "demo-minion"}]
    accepted = client.post("/api/v1/nodes/pending/demo-minion/accept", headers=auth)
    assert accepted.status_code == 200
    assert accepted.json()["online_status"] == "ONLINE"
    assert client.post("/api/v1/nodes/refresh", headers=auth).status_code == 200
    nodes = client.get("/api/v1/nodes").json()
    demo_node = next(node for node in nodes if node["id"] == "demo-node")
    assert demo_node["roles"] == []
    update = client.patch("/api/v1/nodes/demo-node", json={"roles": ["compute"]}, headers=auth)
    assert update.status_code == 200
    bundle = build_bundle(tmp_path)
    with bundle.open("rb") as stream:
        uploaded = client.post("/api/v1/packages", files={"file": (bundle.name, stream, "application/gzip")}, headers=auth)
    assert uploaded.status_code == 201, uploaded.text
    package = uploaded.json()
    assert package["revision"] == 1
    preview = client.post("/api/v1/tasks/preview", json={"package_id": package["id"], "node_ids": ["demo-node"], "role_names": []})
    assert preview.status_code == 200
    request = {"package_id": package["id"], "node_ids": ["demo-node"], "role_names": [], "remark": "e2e", "confirmed_warnings": []}
    create_headers = {**auth, "Idempotency-Key": "task-key-1"}
    created = client.post("/api/v1/tasks", json=request, headers=create_headers)
    assert created.status_code == 201, created.text
    task = created.json()
    replay = client.post("/api/v1/tasks", json=request, headers=create_headers)
    assert replay.status_code == 200
    assert replay.headers["Idempotent-Replayed"] == "true"
    conflict = client.post("/api/v1/tasks", json={**request, "remark": "different"}, headers=create_headers)
    assert conflict.status_code == 409
    scheduler = client.app.state.scheduler
    task_node_id = task["nodes"][0]["id"]
    assert scheduler._claim(task_node_id)
    scheduler._execute(task_node_id)
    detail = client.get(f"/api/v1/tasks/{task['id']}").json()
    assert detail["status"] == "SUCCESS"
    step = detail["nodes"][0]["attempts"][0]["steps"][0]
    logs = client.get(f"/api/v1/tasks/{task['id']}/nodes/{task_node_id}/attempts/{detail['nodes'][0]['attempts'][0]['id']}/steps/{step['id']}/logs").json()
    assert "fix.sh" in logs["data"]
    stream_url = f"/api/v1/tasks/{task['id']}/nodes/{task_node_id}/attempts/{detail['nodes'][0]['attempts'][0]['id']}/steps/{step['id']}/logs/stream"
    stream = client.get(stream_url)
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert "event: log" in stream.text and "event: end" in stream.text
    resumed = client.get(stream_url, headers={"Last-Event-ID": str(logs["offset"])})
    assert "event: log" not in resumed.text and "event: end" in resumed.text
    dashboard = client.get("/api/v1/dashboard/summary").json()
    assert dashboard["tasks"]["SUCCESS"] == 1
    assert client.get("/api/v1/audit-logs").json()


def test_offline_warning_cancel_and_retry_guard(client, auth, salt, tmp_path):
    client.post("/api/v1/nodes/refresh", headers=auth)
    client.patch("/api/v1/nodes/demo-node", json={"roles": ["compute"]}, headers=auth)
    salt._accepted["demo-node"]["offline"] = True
    assert client.post("/api/v1/nodes/refresh", headers=auth).status_code == 200
    bundle = build_bundle(tmp_path, name="offline-fix")
    with bundle.open("rb") as stream:
        package = client.post("/api/v1/packages", files={"file": (bundle.name, stream, "application/gzip")}, headers=auth).json()
    preview = client.post("/api/v1/tasks/preview", json={"package_id": package["id"], "node_ids": ["demo-node"], "role_names": []})
    assert {item["type"] for item in preview.json()["warnings"]} == {"OFFLINE"}
    body = {"package_id": package["id"], "node_ids": ["demo-node"], "role_names": [], "remark": "", "confirmed_warnings": ["OFFLINE"]}
    assert client.post("/api/v1/tasks", json={**body, "confirmed_warnings": []}, headers={**auth, "Idempotency-Key": "offline-unconfirmed"}).status_code == 409
    created = client.post("/api/v1/tasks", json=body, headers={**auth, "Idempotency-Key": "offline-1"})
    assert created.status_code == 201
    task = created.json()
    cancelled = client.post(f"/api/v1/tasks/{task['id']}/cancel", headers=auth)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"
    node = task["nodes"][0]
    assert client.post(f"/api/v1/tasks/{task['id']}/nodes/{node['id']}/retry", headers=auth).status_code == 409


def test_settings_ranges_and_masking(client, auth):
    assert client.patch("/api/v1/settings", json={"default_step_timeout": 0}, headers=auth).status_code == 422
    response = client.patch("/api/v1/settings", json={"default_step_timeout": 120, "salt_api_credential": "secret"}, headers=auth)
    assert response.status_code == 200
    assert response.json()["default_step_timeout"] == 120
    assert response.json()["salt_api_credential"] == "********"


def test_session_expiry_cookie_attributes_and_forged_csrf(client, settings):
    from datetime import timedelta
    from automation_center.models import SessionRecord, utcnow

    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-password"})
    cookie = login.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.post("/api/v1/nodes/refresh", headers={"X-CSRF-Token": "forged"}).status_code == 403
    factory = client.app.state.session_factory
    with factory() as session:
        for record in session.query(SessionRecord):
            record.idle_expires_at = utcnow() - timedelta(seconds=1)
        session.commit()
    assert client.get("/api/v1/auth/me").status_code == 401

    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-password"})
    with factory() as session:
        record = session.query(SessionRecord).filter(SessionRecord.token_hash.is_not(None)).one()
        record.absolute_expires_at = utcnow() - timedelta(seconds=1)
        session.commit()
    assert client.get("/api/v1/auth/me").status_code == 401

    settings.cookie_secure = True
    secure = client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-password"})
    assert "secure" in secure.headers["set-cookie"].lower()
