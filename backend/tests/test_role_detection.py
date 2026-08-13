from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from automation_center.models import Node, RoleDetectionJob, RoleRule
from automation_center.role_detection import RoleDetectionWorker


def refresh(client, auth):
    response = client.post("/api/v1/nodes/refresh", headers=auth)
    assert response.status_code == 200
    return response.json()


def start_and_run(client, auth):
    created = client.post("/api/v1/nodes/role-detection-jobs", headers=auth)
    assert created.status_code == 202
    job_id = created.json()["id"]
    assert client.app.state.role_detection_worker.run_once() == job_id
    detail = client.get(f"/api/v1/nodes/role-detection-jobs/{job_id}")
    assert detail.status_code == 200
    return detail.json()


def test_detection_adds_auto_roles_without_overwriting_manual_labels(client, auth, salt):
    refresh(client, auth)
    manual = client.patch(
        "/api/v1/nodes/demo-node",
        json={"roles": ["人工-标签"]},
        headers=auth,
    )
    assert manual.status_code == 200

    created = client.post("/api/v1/nodes/role-detection-jobs", headers=auth)
    assert created.status_code == 202
    duplicate = client.post("/api/v1/nodes/role-detection-jobs", headers=auth)
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["active_job_id"] == created.json()["id"]

    client.app.state.role_detection_worker.run_once()
    detail = client.get(f"/api/v1/nodes/role-detection-jobs/{created.json()['id']}").json()
    assert detail["status"] == "SUCCESS"
    assert detail["results"][0]["matched_roles"] == ["compute"]
    assert detail["results"][0]["added_roles"] == ["compute"]
    node = client.get("/api/v1/nodes").json()[0]
    assert node["roles"] == ["compute", "人工-标签"]
    assert node["role_details"] == [
        {"role": "compute", "sources": ["auto"]},
        {"role": "人工-标签", "sources": ["manual"]},
    ]

    # 人工删除 auto 标签后，只要进程仍匹配，下一次识别会再次补回。
    removed = client.patch(
        "/api/v1/nodes/demo-node",
        json={"roles": ["人工-标签"]},
        headers=auth,
    )
    assert removed.json()["roles"] == ["人工-标签"]
    rerun = start_and_run(client, auth)
    assert rerun["results"][0]["added_roles"] == ["compute"]
    repeated = start_and_run(client, auth)
    assert repeated["results"][0]["added_roles"] == []

    # 进程原文可能含敏感参数，但结构化历史只能保存匹配/新增角色。
    salt._accepted["demo-node"]["process_text"] = "nova-compute --password secret-process-value"
    safe = start_and_run(client, auth)
    assert "secret-process-value" not in json.dumps(safe, ensure_ascii=False)


def test_concurrent_detection_creation_keeps_one_active_job(client, auth):
    refresh(client, auth)
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(
            lambda _: client.post("/api/v1/nodes/role-detection-jobs", headers=auth),
            range(8),
        ))
    assert [response.status_code for response in responses].count(202) == 1
    assert [response.status_code for response in responses].count(409) == 7
    active_id = next(response.json()["id"] for response in responses if response.status_code == 202)
    assert all(
        response.json()["detail"]["active_job_id"] == active_id
        for response in responses if response.status_code == 409
    )


def test_detection_scans_disabled_online_nodes_and_reports_partial_failure(client, auth, salt):
    salt._accepted["broken"] = {
        "hostname": "broken",
        "management_ip": "192.0.2.99",
        "process_error": "PROCESS_READ_FAILED",
    }
    refresh(client, auth)
    disabled = client.patch("/api/v1/nodes/demo-node", json={"enabled": False}, headers=auth)
    assert disabled.status_code == 200
    detail = start_and_run(client, auth)
    assert detail["target_node_count"] == 2
    assert detail["status"] == "PARTIAL_FAILED"
    assert detail["success_count"] == 1 and detail["failed_count"] == 1
    by_node = {item["node_id_snapshot"]: item for item in detail["results"]}
    assert by_node["demo-node"]["status"] == "SUCCESS"
    assert by_node["broken"]["failure_reason"] == "PROCESS_READ_FAILED"


def test_detection_skips_offline_and_requires_an_online_target(client, auth, salt):
    salt._accepted["offline"] = {
        "hostname": "offline",
        "management_ip": "192.0.2.88",
        "processes": [],
        "offline": True,
    }
    refresh(client, auth)
    detail = start_and_run(client, auth)
    assert detail["skipped_count"] == 1
    skipped = next(item for item in detail["results"] if item["node_id_snapshot"] == "offline")
    assert skipped["status"] == "SKIPPED_OFFLINE"

    salt._accepted["demo-node"]["offline"] = True
    refresh(client, auth)
    blocked = client.post("/api/v1/nodes/role-detection-jobs", headers=auth)
    assert blocked.status_code == 422
    assert "ONLINE" in blocked.json()["detail"]


def test_detection_recovery_marks_running_job_lost(client, auth):
    refresh(client, auth)
    created = client.post("/api/v1/nodes/role-detection-jobs", headers=auth).json()
    factory = client.app.state.session_factory
    with factory() as session:
        job = session.get(RoleDetectionJob, created["id"])
        job.status = "RUNNING"
        session.commit()
    RoleDetectionWorker(factory, client.app.state.salt).recover_interrupted()
    detail = client.get(f"/api/v1/nodes/role-detection-jobs/{created['id']}").json()
    assert detail["status"] == "FAILED"
    assert detail["failure_reason"] == "EXECUTION_STATE_LOST"
    assert detail["results"][0]["failure_reason"] == "EXECUTION_STATE_LOST"


def test_detection_timeout_fails_without_holding_database_transaction(client, auth, salt, monkeypatch):
    refresh(client, auth)
    engine = client.app.state.engine

    def timeout(_node_ids):
        # Worker 在 Salt 网络阶段已经关闭前置 Session，连接池不应有签出连接。
        assert engine.pool.checkedout() == 0
        raise TimeoutError("sensitive process command must not be persisted")

    monkeypatch.setattr(salt, "process_snapshot_many", timeout)
    detail = start_and_run(client, auth)
    assert detail["status"] == "FAILED"
    assert detail["failure_reason"] == "ALL_TARGETS_FAILED"
    assert detail["results"][0]["failure_reason"] == "SALT_REQUEST_FAILED"
    assert "sensitive process command" not in json.dumps(detail, ensure_ascii=False)


def test_detection_internal_error_releases_active_slot(client, auth, monkeypatch):
    refresh(client, auth)
    created = client.post("/api/v1/nodes/role-detection-jobs", headers=auth).json()
    worker = client.app.state.role_detection_worker

    def fail_apply(*_args):
        raise RuntimeError("forced")

    monkeypatch.setattr(worker, "_apply_results", fail_apply)
    with pytest.raises(RuntimeError, match="forced"):
        worker.run_once()
    detail = client.get(f"/api/v1/nodes/role-detection-jobs/{created['id']}").json()
    assert detail["status"] == "FAILED"
    assert detail["failure_reason"] == "ROLE_DETECTION_INTERNAL_ERROR"
    retry = client.post("/api/v1/nodes/role-detection-jobs", headers=auth)
    assert retry.status_code == 202


def test_role_rule_and_label_validation(client, auth):
    valid_rules = [
        {"role": "计算-节点", "matcher_type": "process", "pattern": "nova-compute", "enabled": True}
    ]
    saved = client.patch("/api/v1/settings", json={"role_detection_rules": valid_rules}, headers=auth)
    assert saved.status_code == 200
    assert saved.json()["role_detection_rules"] == valid_rules
    duplicate = client.patch(
        "/api/v1/settings",
        json={"role_detection_rules": valid_rules + valid_rules},
        headers=auth,
    )
    assert duplicate.status_code == 422
    service = client.patch(
        "/api/v1/settings",
        json={"role_detection_rules": [{"role": "x", "matcher_type": "service", "pattern": "x"}]},
        headers=auth,
    )
    assert service.status_code == 422
    invalid_label = client.patch(
        "/api/v1/nodes/demo-node",
        json={"roles": ["bad,label"]},
        headers=auth,
    )
    assert invalid_label.status_code == 404  # 节点尚未刷新，先验证路由不创建隐式节点。
    refresh(client, auth)
    invalid_label = client.patch(
        "/api/v1/nodes/demo-node",
        json={"roles": ["bad,label"]},
        headers=auth,
    )
    assert invalid_label.status_code == 422
    invalid_preview = client.post(
        "/api/v1/tasks/preview",
        json={"package_id": "missing", "node_ids": [], "role_names": ["bad,label"]},
    )
    assert invalid_preview.status_code == 422
