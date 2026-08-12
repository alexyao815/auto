from __future__ import annotations

import asyncio
import time
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from automation_center.models import Node, Package, Task, TaskAttempt, TaskNode, TaskStepResult, utcnow
from automation_center.scheduler import Scheduler

from .conftest import build_bundle


def upload(client, auth, bundle):
    with bundle.open("rb") as stream:
        return client.post("/api/v1/packages", files={"file": (bundle.name, stream, "application/gzip")}, headers=auth).json()


def create_waiting(client, auth, package_id, key):
    body = {"package_id": package_id, "node_ids": ["demo-node"], "role_names": [], "remark": "", "confirmed_warnings": []}
    return client.post("/api/v1/tasks", json=body, headers={**auth, "Idempotency-Key": key}).json()


def ensure_node(client, auth):
    client.post("/api/v1/nodes/refresh", headers=auth)


def test_node_refresh_releases_sqlite_connection_before_salt_calls(client, auth, salt, monkeypatch):
    engine = client.app.state.engine
    original_accepted_keys = salt.accepted_keys
    original_ping = salt.ping
    original_node_info = salt.node_info

    def assert_no_checked_out_connection():
        # Salt 回调期间连接池应为空；数据库读写只发生在探测前后的短阶段。
        assert engine.pool.checkedout() == 0

    def accepted_keys():
        assert_no_checked_out_connection()
        return original_accepted_keys()

    def ping(node_id):
        assert_no_checked_out_connection()
        return original_ping(node_id)

    def node_info(node_id):
        assert_no_checked_out_connection()
        return original_node_info(node_id)

    monkeypatch.setattr(salt, "accepted_keys", accepted_keys)
    monkeypatch.setattr(salt, "ping", ping)
    monkeypatch.setattr(salt, "node_info", node_info)

    assert client.post("/api/v1/nodes/refresh", headers=auth).status_code == 200
    client.app.state.scheduler._refresh_nodes_sync()


def test_failure_retry_fifo_and_ignore(client, auth, tmp_path):
    ensure_node(client, auth)
    failed_package = upload(client, auth, build_bundle(tmp_path, name="fail-package", script="scripts/fail.sh"))
    task = create_waiting(client, auth, failed_package["id"], "fail-task")
    scheduler = client.app.state.scheduler
    node_id = task["nodes"][0]["id"]
    assert scheduler._claim(node_id)
    scheduler._execute(node_id)
    detail = client.get(f"/api/v1/tasks/{task['id']}").json()
    assert detail["status"] == "FAILED"
    first_seq = detail["nodes"][0]["queue_seq"]
    ahead = create_waiting(client, auth, failed_package["id"], "ahead-task")
    retried = client.post(f"/api/v1/tasks/{task['id']}/nodes/{node_id}/retry", headers=auth)
    assert retried.status_code == 200
    assert retried.json()["queue_seq"] > first_seq
    assert retried.json()["queue_seq"] > ahead["nodes"][0]["queue_seq"]
    assert scheduler._claim(node_id) is False
    assert client.post(f"/api/v1/tasks/{task['id']}/nodes/{node_id}/cancel", headers=auth).status_code == 200
    ahead_node_id = ahead["nodes"][0]["id"]
    assert client.post(f"/api/v1/tasks/{ahead['id']}/nodes/{ahead_node_id}/cancel", headers=auth).status_code == 200

    ignored_package = upload(client, auth, build_bundle(tmp_path, name="ignore-package", script="scripts/fail-ignore.sh", failure_action="ignore"))
    ignored_task = create_waiting(client, auth, ignored_package["id"], "ignore-task")
    ignored_node = ignored_task["nodes"][0]["id"]
    assert scheduler._claim(ignored_node)
    scheduler._execute(ignored_node)
    result = client.get(f"/api/v1/tasks/{ignored_task['id']}").json()
    assert result["status"] == "SUCCESS"
    assert result["nodes"][0]["has_warning"] is True


def test_python_execution_running_cancel_guard_and_success_cleanup(client, auth, salt, tmp_path):
    ensure_node(client, auth)
    package = upload(client, auth, build_bundle(tmp_path, name="python-package", executor_type="python", script="scripts/fix.py"))
    task = create_waiting(client, auth, package["id"], "python-task")
    scheduler = client.app.state.scheduler
    node_id = task["nodes"][0]["id"]
    assert scheduler._claim(node_id)
    assert scheduler._claim(node_id) is False
    assert client.post(f"/api/v1/tasks/{task['id']}/cancel", headers=auth).status_code == 409
    scheduler._execute(node_id)
    detail = client.get(f"/api/v1/tasks/{task['id']}").json()
    assert detail["status"] == "SUCCESS"
    assert detail["nodes"][0]["attempts"][0]["steps"][0]["type"] == "python"
    assert ("demo-node", f"/var/lib/automation-center/tasks/{task['id']}/attempt-1") in salt.cleaned_workdirs


def test_timeout_lost_disabled_offline_and_revision_guards(client, auth, salt, tmp_path):
    ensure_node(client, auth)
    scheduler = client.app.state.scheduler
    timeout_package = upload(client, auth, build_bundle(tmp_path, name="timeout-package", script="scripts/timeout.sh", timeout=1))
    timeout_task = create_waiting(client, auth, timeout_package["id"], "timeout-task")
    timeout_node = timeout_task["nodes"][0]["id"]
    assert scheduler._claim(timeout_node)
    scheduler._execute(timeout_node)
    assert client.get(f"/api/v1/tasks/{timeout_task['id']}").json()["status"] == "FAILED"

    lost_package = upload(client, auth, build_bundle(tmp_path, name="lost-package", script="scripts/lost.sh"))
    lost_task = create_waiting(client, auth, lost_package["id"], "lost-task")
    lost_node = lost_task["nodes"][0]["id"]
    assert scheduler._claim(lost_node)
    scheduler._execute(lost_node)
    assert "EXECUTION_STATE_LOST" in client.get(f"/api/v1/tasks/{lost_task['id']}").json()["nodes"][0]["failure_reason"]

    guard_package = upload(client, auth, build_bundle(tmp_path, name="guard-package"))
    disabled_task = create_waiting(client, auth, guard_package["id"], "disabled-task")
    client.patch("/api/v1/nodes/demo-node", json={"enabled": False}, headers=auth)
    assert scheduler._claim(disabled_task["nodes"][0]["id"]) is False
    client.patch("/api/v1/nodes/demo-node", json={"enabled": True}, headers=auth)
    disabled_node_id = disabled_task["nodes"][0]["id"]
    assert client.post(f"/api/v1/tasks/{disabled_task['id']}/nodes/{disabled_node_id}/cancel", headers=auth).status_code == 200
    salt._accepted["demo-node"]["offline"] = True
    offline_task = create_waiting(client, auth, guard_package["id"], "offline-task")
    assert scheduler._claim(offline_task["nodes"][0]["id"]) is False
    assert client.get(f"/api/v1/tasks/{offline_task['id']}").json()["status"] == "FAILED"
    salt._accepted["demo-node"]["offline"] = False

    mismatch_task = create_waiting(client, auth, guard_package["id"], "mismatch-task")
    factory = client.app.state.session_factory
    with factory() as session:
        package = session.get(Package, guard_package["id"]); package.revision += 1; session.commit()
    assert scheduler._claim(mismatch_task["nodes"][0]["id"]) is False
    assert client.get(f"/api/v1/tasks/{mismatch_task['id']}").json()["nodes"][0]["failure_reason"] == "PACKAGE_REVISION_UNAVAILABLE"


def test_recovery_refresh_cleanup_and_run_loop(client, auth, salt, settings, tmp_path):
    scheduler: Scheduler = client.app.state.scheduler
    settings.node_status_check_interval = 0
    asyncio.run(scheduler.refresh_nodes())
    package = upload(client, auth, build_bundle(tmp_path, name="recover-package"))
    task = create_waiting(client, auth, package["id"], "recover-task")
    node_id = task["nodes"][0]["id"]
    assert scheduler._claim(node_id)
    # 人工构造“服务停止前已下发并持久化 JID”的数据库快照，恢复逻辑只能监控该 Job，不能重发。
    factory = client.app.state.session_factory
    with factory() as session:
        node = session.scalar(select(TaskNode).where(TaskNode.id == node_id).options(selectinload(TaskNode.attempts).selectinload(TaskAttempt.steps)))
        attempt = node.attempts[-1]
        step = attempt.steps[0]
        remote_root = f"/var/lib/automation-center/tasks/{task['id']}/attempt-{attempt.attempt_no}"
        jid = salt.start_step("demo-node", step.executor_type, step.script_snapshot, f"{remote_root}/work", f"{remote_root}/logs/step-01.stdout", f"{remote_root}/logs/step-01.stderr", f"{remote_root}/logs/step-01.exit")
        step.status = "RUNNING"; step.salt_jid = jid; step.started_at = utcnow(); session.commit()
    scheduler._recover_node(node_id)
    assert client.get(f"/api/v1/tasks/{task['id']}").json()["status"] == "SUCCESS"

    with factory() as session:
        old_task = session.get(Task, task["id"])
        old_task.finished_at = utcnow() - timedelta(days=30)
        old_node = session.get(TaskNode, node_id)
        old_node.status = "FAILED"
        old_node.finished_at = utcnow() - timedelta(days=30)
        session.commit()
    log_dir = settings.log_dir / task["id"]
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "old.log").write_text("old")
    scheduler._last_cleanup = 0
    cleanup_count = len(salt.cleaned_workdirs)
    asyncio.run(scheduler.cleanup_expired())
    assert not log_dir.exists()
    assert len(salt.cleaned_workdirs) > cleanup_count

    async def exercise_loop():
        loop_scheduler = Scheduler(factory, settings, salt)
        runner = asyncio.create_task(loop_scheduler.run())
        await asyncio.sleep(0.03)
        await loop_scheduler.stop()
        await runner
    asyncio.run(exercise_loop())
