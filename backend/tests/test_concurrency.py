from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi import HTTPException
from sqlalchemy import func, select

from automation_center.models import Task, TaskAttempt, TaskNode
from automation_center.scheduler import Scheduler
from automation_center.task_service import cancel_task_node, create_task

from .conftest import build_bundle


def upload(client, auth, bundle):
    with bundle.open("rb") as stream:
        response = client.post("/api/v1/packages", files={"file": (bundle.name, stream, "application/gzip")}, headers=auth)
    assert response.status_code == 201
    return response.json()


def create_waiting(client, auth, package_id, key):
    body = {"package_id": package_id, "node_ids": ["demo-node"], "role_names": [], "remark": "", "confirmed_warnings": []}
    response = client.post("/api/v1/tasks", json=body, headers={**auth, "Idempotency-Key": key})
    assert response.status_code == 201
    return response.json()


def test_concurrent_idempotent_create_returns_one_task(client, auth, tmp_path):
    client.post("/api/v1/nodes/refresh", headers=auth)
    package = upload(client, auth, build_bundle(tmp_path, name="concurrent-create"))
    factory = client.app.state.session_factory
    # Barrier 让两个事务越过各自准备阶段后同时写入，稳定触发幂等唯一键竞争。
    barrier = threading.Barrier(2)

    def worker():
        with factory() as session:
            barrier.wait()
            task, replayed = create_task(session, package["id"], [], ["demo-node"], "same", [], "same-key")
            return task.id, replayed

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in [pool.submit(worker), pool.submit(worker)]]

    assert len({task_id for task_id, _ in results}) == 1
    assert sorted(replayed for _, replayed in results) == [False, True]
    with factory() as session:
        assert session.scalar(select(func.count(Task.id)).where(Task.idempotency_key == "same-key")) == 1


def test_duplicate_scheduler_claim_creates_one_attempt(client, auth, salt, settings, tmp_path):
    client.post("/api/v1/nodes/refresh", headers=auth)
    package = upload(client, auth, build_bundle(tmp_path, name="concurrent-claim"))
    task = create_waiting(client, auth, package["id"], "claim-key")
    task_node_id = task["nodes"][0]["id"]
    factory = client.app.state.session_factory
    schedulers = [Scheduler(factory, settings, salt), Scheduler(factory, settings, salt)]
    # 两个独立 Scheduler 模拟重复 Worker，同时争用 TaskNode CAS 和节点唯一锁。
    barrier = threading.Barrier(2)

    def claim(scheduler):
        barrier.wait()
        return scheduler._claim(task_node_id)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [future.result() for future in [pool.submit(claim, scheduler) for scheduler in schedulers]]
        assert sorted(results) == [False, True]
        with factory() as session:
            assert session.scalar(select(func.count(TaskAttempt.id)).join(TaskNode).where(TaskNode.id == task_node_id)) == 1
        client.app.state.scheduler._execute(task_node_id)
        assert client.get(f"/api/v1/tasks/{task['id']}").json()["status"] == "SUCCESS"
    finally:
        for scheduler in schedulers:
            scheduler.executor.shutdown(wait=False, cancel_futures=True)


def test_scheduler_cancel_race_has_one_terminal_decision(client, auth, tmp_path):
    client.post("/api/v1/nodes/refresh", headers=auth)
    package = upload(client, auth, build_bundle(tmp_path, name="cancel-race"))
    task = create_waiting(client, auth, package["id"], "cancel-race-key")
    task_node_id = task["nodes"][0]["id"]
    scheduler = client.app.state.scheduler
    factory = client.app.state.session_factory
    # Claim 与 Cancel 从同一起点竞争，合法结果只能有一个状态转换成功。
    barrier = threading.Barrier(2)

    def claim():
        barrier.wait()
        return scheduler._claim(task_node_id)

    def cancel():
        with factory() as session:
            node = session.get(TaskNode, task_node_id)
            assert node is not None
            barrier.wait()
            try:
                cancel_task_node(session, node)
                return True
            except HTTPException as exc:
                assert exc.status_code == 409
                return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        claim_future = pool.submit(claim)
        cancel_future = pool.submit(cancel)
        claimed, cancelled = claim_future.result(), cancel_future.result()

    assert claimed is not cancelled
    detail = client.get(f"/api/v1/tasks/{task['id']}").json()
    if claimed:
        assert detail["nodes"][0]["status"] == "RUNNING"
        assert len(detail["nodes"][0]["attempts"]) == 1
        scheduler._execute(task_node_id)
    else:
        assert detail["nodes"][0]["status"] == "CANCELLED"
        assert detail["nodes"][0]["attempts"] == []
