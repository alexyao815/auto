"""REST API：输入校验、认证保护、业务服务编排、序列化和 SSE 日志输出。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Annotated, Any, Iterator, Literal

from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .config import GIB, MIB, Settings
from .models import (
    AuditLog,
    Credential,
    Node,
    NodeRole,
    Package,
    RoleDetectionJob,
    RoleDetectionNodeResult,
    RoleRule,
    SessionRecord,
    SystemSetting,
    Task,
    TaskAttempt,
    TaskNode,
    TaskStepResult,
    utcnow,
)
from .node_service import apply_node_snapshots, collect_node_snapshots, probe_node
from .package_service import create_package, delete_package, update_package
from .role_detection import (
    RoleValidationError,
    create_role_detection_job,
    normalize_role_label,
    normalize_role_labels,
    normalize_rule_pattern,
)
from .salt import SaltAdapter
from .security import create_login_session, decrypt_secret, encrypt_secret, sha256_text, verify_password
from .task_service import aggregate_task, cancel_task_node, create_task, effective_roles, retry_task_node, task_preview


class LoginRequest(BaseModel):
    """固定共享账号的登录请求。"""
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class TaskPreviewRequest(BaseModel):
    """任务预览目标；角色和直接节点会在 Service 中取并集。"""
    package_id: str
    role_names: list[str] = Field(default_factory=list, max_length=200)
    node_ids: list[str] = Field(default_factory=list)

    @field_validator("role_names")
    @classmethod
    def validate_role_names(cls, value: list[str]) -> list[str]:
        try:
            return normalize_role_labels(value)
        except RoleValidationError as exc:
            raise ValueError(str(exc)) from None


class TaskCreateRequest(TaskPreviewRequest):
    """正式创建请求，包含预览阶段要求确认的警告。"""
    remark: str = Field(default="", max_length=2000)
    confirmed_warnings: list[str] = Field(default_factory=list)


class NodeUpdateRequest(BaseModel):
    """节点启停和人工角色请求；roles 表示编辑后的完整有效标签集合。"""
    enabled: bool | None = None
    roles: list[str] | None = Field(default=None, max_length=200)
    restore_auto_roles: bool = False


class RoleRuleRequest(BaseModel):
    """System Settings 中仅支持的进程字面匹配规则。"""

    role: str
    matcher_type: Literal["process"] = "process"
    pattern: str
    enabled: bool = True

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        try:
            return normalize_role_label(value)
        except RoleValidationError as exc:
            raise ValueError(str(exc)) from None

    @field_validator("pattern")
    @classmethod
    def validate_pattern(cls, value: str) -> str:
        try:
            return normalize_rule_pattern(value)
        except RoleValidationError as exc:
            raise ValueError(str(exc)) from None


class SettingsUpdateRequest(BaseModel):
    """允许运行时修改的系统设置及其边界。"""
    salt_api_url: str | None = Field(default=None, max_length=2048)
    salt_api_username: str | None = Field(default=None, max_length=128)
    salt_api_credential: str | None = Field(default=None, max_length=4096)
    salt_request_timeout: int | None = Field(default=None, ge=1, le=300)
    package_storage_path: str | None = Field(default=None, max_length=4096)
    temp_path: str | None = Field(default=None, max_length=4096)
    max_upload_size: int | None = Field(default=None, ge=MIB, le=10 * GIB)
    default_step_timeout: int | None = Field(default=None, ge=1, le=86400)
    execution_log_retention_days: int | None = Field(default=None, ge=1, le=365)
    node_status_check_interval: int | None = Field(default=None, ge=5, le=3600)
    role_detection_rules: list[RoleRuleRequest] | None = Field(default=None, max_length=200)


def _iso(value):
    """把数据库 UTC 时间序列化为带 Z 的 RFC3339 文本。"""
    return f"{value.isoformat()}Z" if value else None


def _source_ip(request: Request) -> str:
    """取得当前连接来源地址，供安全审计记录使用。"""
    return request.client.host if request.client else ""


def audit(session: Session, request: Request, operation: str, object_type: str, object_id: str | None, detail: dict[str, Any] | None = None) -> None:
    """把审计记录加入调用方事务，使业务变更和审计同成同败。"""
    session.add(AuditLog(
        source_ip=_source_ip(request),
        operation=operation,
        object_type=object_type,
        object_id=object_id,
        detail_json=json.dumps(detail or {}, ensure_ascii=False),
    ))


def serialize_node(node: Node) -> dict[str, Any]:
    """序列化节点，并同时返回合并标签和可解释的来源列表。"""
    sources_by_role: dict[str, set[str]] = {}
    for role in node.roles:
        sources_by_role.setdefault(role.role, set()).add(role.source)
    return {
        "id": node.id,
        "hostname": node.hostname,
        "management_ip": node.management_ip,
        "online_status": node.online_status,
        "enabled": node.enabled,
        "roles": effective_roles(node),
        "role_details": [
            {"role": role, "sources": sorted(sources)}
            for role, sources in sorted(sources_by_role.items())
        ],
        "role_override": node.role_override,
        "last_check_time": _iso(node.last_check_time),
        "created_at": _iso(node.created_at),
        "updated_at": _iso(node.updated_at),
    }


def serialize_role_detection_result(result: RoleDetectionNodeResult) -> dict[str, Any]:
    """返回节点识别快照和结构化结果，不包含原始进程命令行。"""

    return {
        "id": result.id,
        "node_id": result.node_id,
        "node_id_snapshot": result.node_id_snapshot,
        "hostname_snapshot": result.hostname_snapshot,
        "status": result.status,
        "matched_roles": json.loads(result.matched_roles_json),
        "added_roles": json.loads(result.added_roles_json),
        "failure_reason": result.failure_reason,
        "finished_at": _iso(result.finished_at),
    }


def serialize_role_detection_job(job: RoleDetectionJob, include_results: bool = False) -> dict[str, Any]:
    """返回角色识别任务摘要；详情接口额外附带规则和逐节点结果。"""

    payload: dict[str, Any] = {
        "id": job.id,
        "status": job.status,
        "total_node_count": job.total_node_count,
        "target_node_count": job.target_node_count,
        "success_count": job.success_count,
        "failed_count": job.failed_count,
        "skipped_count": job.skipped_count,
        "failure_reason": job.failure_reason,
        "created_at": _iso(job.created_at),
        "started_at": _iso(job.started_at),
        "finished_at": _iso(job.finished_at),
    }
    if include_results:
        payload["rules"] = json.loads(job.rules_snapshot_json)
        payload["results"] = [serialize_role_detection_result(result) for result in job.results]
    return payload


def serialize_package(package: Package, include_steps: bool = False) -> dict[str, Any]:
    """序列化当前 Package Revision，可选包含 Step 定义。"""
    result = {
        "id": package.id,
        "name": package.name,
        "revision": package.revision,
        "description": package.description,
        "component": package.component,
        "bug_id": package.bug_id,
        "target_roles": json.loads(package.target_roles_json),
        "applicable_versions": json.loads(package.applicable_versions_json),
        "sha256": package.sha256,
        "created_at": _iso(package.created_at),
        "updated_at": _iso(package.updated_at),
    }
    if include_steps:
        result["steps"] = [
            {
                "sequence": step.sequence,
                "name": step.name,
                "type": step.executor_type,
                "script": step.script,
                "timeout": step.timeout,
                "failure_action": step.failure_action,
            }
            for step in package.steps
        ]
    return result


def serialize_step(step: TaskStepResult) -> dict[str, Any]:
    """序列化执行时固化的 Step 快照与结果。"""
    return {
        "id": step.id,
        "sequence": step.sequence,
        "name": step.name_snapshot,
        "type": step.executor_type,
        "script": step.script_snapshot,
        "timeout": step.timeout_snapshot,
        "failure_action": step.failure_action_snapshot,
        "status": step.status,
        "salt_jid": step.salt_jid,
        "exit_code": step.exit_code,
        "failure_reason": step.failure_reason,
        "started_at": _iso(step.started_at),
        "finished_at": _iso(step.finished_at),
    }


def serialize_attempt(attempt: TaskAttempt) -> dict[str, Any]:
    """序列化一次真实执行及其 Step 结果。"""
    return {
        "id": attempt.id,
        "attempt_no": attempt.attempt_no,
        "status": attempt.status,
        "warning_message": attempt.warning_message,
        "started_at": _iso(attempt.started_at),
        "finished_at": _iso(attempt.finished_at),
        "steps": [serialize_step(step) for step in attempt.steps],
    }


def serialize_task_node(node: TaskNode, include_attempts: bool = False) -> dict[str, Any]:
    """序列化单节点状态、排队事实和可选执行历史。"""
    result = {
        "id": node.id,
        "node_id": node.node_id,
        "hostname": node.hostname_snapshot,
        "management_ip": node.management_ip_snapshot,
        "roles": json.loads(node.roles_snapshot_json),
        "status": node.status,
        "failure_reason": node.failure_reason,
        "has_warning": node.has_warning,
        "queue_seq": node.queue_seq,
        "queue_entered_at": _iso(node.queue_entered_at),
        "started_at": _iso(node.started_at),
        "finished_at": _iso(node.finished_at),
    }
    if include_attempts:
        result["attempts"] = [serialize_attempt(attempt) for attempt in node.attempts]
    return result


def serialize_task(task: Task, include_nodes: bool = False) -> dict[str, Any]:
    """序列化 Task 聚合状态及不可变 Package 快照。"""
    result = {
        "id": task.id,
        "package_id": task.package_id,
        "package_name": task.package_name_snapshot,
        "package_revision": task.package_revision_snapshot,
        "package_description": task.package_description_snapshot,
        "status": task.status,
        "target_node_count": task.target_node_count,
        "success_count": task.success_count,
        "failed_count": task.failed_count,
        "cancelled_count": task.cancelled_count,
        "remark": task.remark,
        "created_at": _iso(task.created_at),
        "started_at": _iso(task.started_at),
        "finished_at": _iso(task.finished_at),
    }
    if include_nodes:
        result["nodes"] = [serialize_task_node(node, include_attempts=True) for node in task.nodes]
    return result


def create_api_router(settings: Settings, get_session, salt: SaltAdapter, require_session, require_csrf) -> APIRouter:
    """组装 ``/api/v1`` 路由，并统一注入 Session、CSRF 和 Salt 依赖。"""
    api = APIRouter(prefix="/api/v1")
    protected = APIRouter(dependencies=[Depends(require_session)])

    # 认证：登录是唯一无需既有 Session 的写接口，后续写请求必须同时通过 CSRF。
    @api.post("/auth/login", summary="登录")
    def login(payload: LoginRequest, request: Request, response: Response, session: Session = Depends(get_session)):
        """校验共享账号，创建服务端 Session，并仅通过 Cookie 返回原始 Token。"""
        credential = session.scalar(select(Credential).where(Credential.username == payload.username))
        if credential is None or not verify_password(credential.password_hash, payload.password):
            audit(session, request, "LOGIN_FAILED", "SESSION", None, {"username": payload.username})
            session.commit()
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        token, csrf, record = create_login_session(session, settings, _source_ip(request))
        audit(session, request, "LOGIN", "SESSION", record.id)
        session.commit()
        # Token/CSRF 在数据库中只保存 Hash；Cookie 禁止脚本读取并限制跨站携带。
        response.set_cookie(
            settings.cookie_name,
            token,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="strict",
            max_age=settings.session_absolute_seconds,
            path="/",
        )
        return {"username": credential.username, "csrf_token": csrf, "idle_expires_at": _iso(record.idle_expires_at), "absolute_expires_at": _iso(record.absolute_expires_at)}

    @api.post("/auth/logout", dependencies=[Depends(require_csrf)], summary="退出登录")
    def logout(request: Request, response: Response, session: Session = Depends(get_session)):
        """删除当前服务端 Session 和浏览器 Cookie。"""
        record = request.state.session_record
        audit(session, request, "LOGOUT", "SESSION", record.id)
        session.delete(record)
        session.commit()
        response.delete_cookie(settings.cookie_name, path="/")
        return {"ok": True}

    @protected.get("/auth/me", summary="查询当前账号")
    def me(session: Session = Depends(get_session)):
        """返回当前固定账号名称，用于前端恢复登录态。"""
        credential = session.scalar(select(Credential).limit(1))
        return {"username": credential.username if credential else ""}

    # 运维探针：live 仅证明进程响应；ready 额外证明数据库可查询。
    @api.get("/health/live", summary="存活探针")
    def live():
        """返回进程存活状态。"""
        return {"status": "ok"}

    @api.get("/health/ready", summary="就绪探针")
    def ready(session: Session = Depends(get_session)):
        """验证数据库连接并报告当前 Salt 模式。"""
        session.execute(select(1))
        return {"status": "ready", "database": "ok", "salt_mode": settings.salt_mode}

    # Dashboard：只读聚合，不触发节点探测或任务状态变化。
    @protected.get("/dashboard/summary", summary="查询概览统计")
    def dashboard_summary(session: Session = Depends(get_session)):
        """返回节点、Package 和各 Task 状态的计数。"""
        nodes = list(session.scalars(select(Node)))
        return {
            "nodes": {
                "total": len(nodes),
                "online": sum(node.online_status == "ONLINE" for node in nodes),
                "offline": sum(node.online_status == "OFFLINE" for node in nodes),
                "disabled": sum(not node.enabled for node in nodes),
            },
            "packages": int(session.scalar(select(func.count(Package.id))) or 0),
            "tasks": {
                status: int(session.scalar(select(func.count(Task.id)).where(Task.status == status)) or 0)
                for status in ["WAITING", "RUNNING", "SUCCESS", "PARTIAL_SUCCESS", "FAILED", "CANCELLED"]
            },
        }

    @protected.get("/dashboard/recent-tasks", summary="查询最近任务")
    def recent_tasks(session: Session = Depends(get_session)):
        """返回最近创建的十个 Task。"""
        tasks = session.scalars(select(Task).order_by(Task.created_at.desc()).limit(10)).all()
        return [serialize_task(task) for task in tasks]

    # 节点：Key 管理由 Salt 完成，节点事实和角色由应用数据库持久化。
    @protected.get("/nodes/pending", summary="查询待接入节点")
    def pending_nodes():
        """列出 Salt 中处于 pending 状态的 Minion Key。"""
        return [{"id": key_id} for key_id in salt.pending_keys()]

    @protected.post("/nodes/pending/{key_id}/accept", dependencies=[Depends(require_csrf)], summary="接受节点 Key")
    def accept_node(key_id: str, request: Request, session: Session = Depends(get_session)):
        """接受 Minion Key，并低频读取一次 hostname 与 management IP。"""
        salt.accept_key(key_id)
        snapshot = probe_node(salt, key_id)
        node = next(node for node in apply_node_snapshots(session, [snapshot]) if node.id == key_id)
        audit(session, request, "ACCEPT_NODE", "NODE", key_id)
        session.commit()
        return serialize_node(node)

    @protected.post("/nodes/pending/{key_id}/reject", dependencies=[Depends(require_csrf)], summary="拒绝节点 Key")
    def reject_node(key_id: str, request: Request, session: Session = Depends(get_session)):
        """拒绝一个 pending Minion Key 并记录审计。"""
        salt.reject_key(key_id)
        audit(session, request, "REJECT_NODE", "NODE", key_id)
        session.commit()
        return {"ok": True}

    @protected.get("/nodes", summary="查询节点列表")
    def list_nodes(session: Session = Depends(get_session)):
        """返回全部已发现节点及其有效角色。"""
        nodes = session.scalars(select(Node).options(selectinload(Node.roles)).order_by(Node.hostname)).all()
        return [serialize_node(node) for node in nodes]

    @protected.post("/nodes/refresh", dependencies=[Depends(require_csrf)], summary="立即刷新节点")
    def refresh_nodes(request: Request, session: Session = Depends(get_session)):
        """先结束认证事务并探测 Salt，再以短事务更新节点事实。"""
        known_ids = set(session.scalars(select(Node.id)))
        session.rollback()
        snapshots = collect_node_snapshots(salt, known_ids)
        nodes = apply_node_snapshots(session, snapshots)
        audit(session, request, "REFRESH_NODES", "NODE", None, {"count": len(nodes)})
        session.commit()
        return [serialize_node(node) for node in nodes]

    @protected.post(
        "/nodes/role-detection-jobs",
        dependencies=[Depends(require_csrf)],
        status_code=202,
        summary="创建自动角色识别任务",
    )
    def start_role_detection(request: Request, session: Session = Depends(get_session)):
        """快照在线节点和进程规则；后台 Worker 稍后执行唯一活动任务。"""

        active = session.scalar(
            select(RoleDetectionJob)
            .where(RoleDetectionJob.status.in_(["WAITING", "RUNNING"]))
            .order_by(RoleDetectionJob.created_at)
            .limit(1)
        )
        if active is not None:
            raise HTTPException(
                status_code=409,
                detail={"code": "ROLE_DETECTION_ALREADY_RUNNING", "active_job_id": active.id},
            )
        try:
            job = create_role_detection_job(session)
        except RoleValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        audit(
            session,
            request,
            "CREATE_ROLE_DETECTION_JOB",
            "ROLE_DETECTION_JOB",
            job.id,
            {"target_node_count": job.target_node_count, "skipped_count": job.skipped_count},
        )
        try:
            session.commit()
        except IntegrityError as exc:
            # active_slot 唯一约束是并发创建的最终防线。
            session.rollback()
            active_id = session.scalar(
                select(RoleDetectionJob.id)
                .where(RoleDetectionJob.status.in_(["WAITING", "RUNNING"]))
                .order_by(RoleDetectionJob.created_at)
                .limit(1)
            )
            raise HTTPException(
                status_code=409,
                detail={"code": "ROLE_DETECTION_ALREADY_RUNNING", "active_job_id": active_id},
            ) from exc
        return serialize_role_detection_job(job, include_results=True)

    @protected.get("/nodes/role-detection-jobs", summary="查询自动角色识别任务")
    def list_role_detection_jobs(
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        session: Session = Depends(get_session),
    ):
        """按创建时间倒序返回有限数量的识别任务摘要。"""

        jobs = session.scalars(
            select(RoleDetectionJob).order_by(RoleDetectionJob.created_at.desc()).limit(limit)
        ).all()
        return [serialize_role_detection_job(job) for job in jobs]

    @protected.get("/nodes/role-detection-jobs/{job_id}", summary="查询自动角色识别任务详情")
    def get_role_detection_job(job_id: str, session: Session = Depends(get_session)):
        """返回规则快照和所有节点结果，用于页面轮询及历史解释。"""

        job = session.scalar(
            select(RoleDetectionJob)
            .where(RoleDetectionJob.id == job_id)
            .options(selectinload(RoleDetectionJob.results))
        )
        if job is None:
            raise HTTPException(status_code=404, detail="角色识别任务不存在")
        return serialize_role_detection_job(job, include_results=True)

    @protected.patch("/nodes/{node_id}", dependencies=[Depends(require_csrf)], summary="更新节点")
    def update_node(node_id: str, payload: NodeUpdateRequest, request: Request, session: Session = Depends(get_session)):
        """启停节点或编辑合并标签；人工操作不再整体覆盖自动识别结果。"""
        node = session.scalar(select(Node).where(Node.id == node_id).options(selectinload(Node.roles)))
        if node is None:
            raise HTTPException(status_code=404, detail="Node 不存在")
        if payload.restore_auto_roles and payload.roles is not None:
            raise HTTPException(status_code=422, detail="roles 与 restore_auto_roles 不能同时提交")
        if payload.enabled is not None:
            node.enabled = payload.enabled
        if payload.restore_auto_roles:
            node.roles[:] = [role for role in node.roles if role.source != "manual"]
        elif payload.roles is not None:
            try:
                requested_roles = set(normalize_role_labels(payload.roles))
            except RoleValidationError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            # 删除用户从最终集合中移除的标签；保留其余标签的 auto/manual 来源。
            node.roles[:] = [role for role in node.roles if role.role in requested_roles]
            existing_roles = {role.role for role in node.roles}
            for role in sorted(requested_roles - existing_roles):
                node.roles.append(NodeRole(role=role, source="manual"))
        node.role_override = False
        audit(session, request, "UPDATE_NODE", "NODE", node_id, payload.model_dump(exclude_none=True))
        session.commit()
        return serialize_node(node)

    @protected.delete("/nodes/{node_id}", dependencies=[Depends(require_csrf)], status_code=204, summary="删除节点")
    def remove_node(node_id: str, request: Request, session: Session = Depends(get_session)):
        """删除无 Waiting/Running 引用的离线节点对象，不删除 Salt Minion Key。"""
        node = session.get(Node, node_id)
        if node is None:
            raise HTTPException(status_code=404, detail="Node 不存在")
        # 页面只向 Offline 节点展示删除入口，API 仍需重复校验，防止绕过
        # 前端或在状态变化竞争中误删仍在线的节点。
        if node.online_status != "OFFLINE":
            raise HTTPException(status_code=409, detail="仅允许删除 Offline 节点")
        active = session.scalar(
            select(func.count()).select_from(TaskNode).where(
                TaskNode.node_id == node_id,
                TaskNode.status.in_(["WAITING", "RUNNING"]),
            )
        )
        # 历史 TaskNode 保留节点快照；只有活跃引用会阻止删除当前 Node。
        if active:
            raise HTTPException(status_code=409, detail="Node 存在 Waiting/Running 任务，禁止删除")
        audit(session, request, "DELETE_NODE", "NODE", node_id)
        session.delete(node)
        session.commit()

    # 维护包：Service 负责流式落盘、安全校验和 Revision 原子切换，API 只编排事务。
    @protected.get("/packages", summary="查询维护包列表")
    def list_packages(session: Session = Depends(get_session)):
        """返回当前可用 Package Revision 列表。"""
        packages = session.scalars(select(Package).order_by(Package.updated_at.desc())).all()
        return [serialize_package(package) for package in packages]

    @protected.get("/packages/{package_id}", summary="查询维护包详情")
    def package_detail(package_id: str, session: Session = Depends(get_session)):
        """返回 Package 当前 Revision 及其已校验 Step。"""
        package = session.scalar(select(Package).where(Package.id == package_id).options(selectinload(Package.steps)))
        if package is None:
            raise HTTPException(status_code=404, detail="Package 不存在")
        return serialize_package(package, include_steps=True)

    @protected.post("/packages", dependencies=[Depends(require_csrf)], status_code=201, summary="上传维护包")
    def upload_package(request: Request, file: Annotated[UploadFile, File()], session: Session = Depends(get_session)):
        """流式接收、校验并保存一个新的维护包。"""
        package = create_package(session, settings, file)
        audit(session, request, "UPLOAD_PACKAGE", "PACKAGE", package.id, {"name": package.name, "revision": package.revision})
        session.commit()
        return serialize_package(package, include_steps=True)

    @protected.put("/packages/{package_id}/bundle", dependencies=[Depends(require_csrf)], summary="更新维护包")
    def replace_package(package_id: str, request: Request, file: Annotated[UploadFile, File()], session: Session = Depends(get_session)):
        """在没有 Waiting/Running 引用时原子发布下一 Revision。"""
        package = session.get(Package, package_id)
        if package is None:
            raise HTTPException(status_code=404, detail="Package 不存在")
        package = update_package(session, settings, package, file)
        audit(session, request, "UPDATE_PACKAGE", "PACKAGE", package.id, {"revision": package.revision})
        session.commit()
        return serialize_package(package, include_steps=True)

    @protected.delete("/packages/{package_id}", dependencies=[Depends(require_csrf)], status_code=204, summary="删除维护包")
    def remove_package(package_id: str, request: Request, session: Session = Depends(get_session)):
        """删除当前 Package 文件和业务对象，但保留 Task 中的历史快照。"""
        package = session.get(Package, package_id)
        if package is None:
            raise HTTPException(status_code=404, detail="Package 不存在")
        audit(session, request, "DELETE_PACKAGE", "PACKAGE", package.id, {"name": package.name, "revision": package.revision})
        session.flush()
        delete_package(session, package)

    # 任务：Preview 与 Create 都进入同一 Service 重校验当前包版本和节点事实。
    @protected.post("/tasks/preview", summary="预览任务")
    def preview_task(payload: TaskPreviewRequest, session: Session = Depends(get_session)):
        """解析目标并返回排除节点及必须二次确认的警告。"""
        return task_preview(session, payload.package_id, payload.role_names, payload.node_ids)

    @protected.post("/tasks", dependencies=[Depends(require_csrf)], status_code=201, summary="创建任务")
    def submit_task(
        payload: TaskCreateRequest,
        request: Request,
        response: Response,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
        session: Session = Depends(get_session),
    ):
        """重新校验预览条件，并用永久 Idempotency-Key 创建或重放 Task。"""
        try:
            task, replayed = create_task(session, payload.package_id, payload.role_names, payload.node_ids, payload.remark, payload.confirmed_warnings, idempotency_key)
            if replayed:
                # 同 Key 同请求返回原 Task；不同请求由 Service 以 409 拒绝。
                response.status_code = 200
                response.headers["Idempotent-Replayed"] = "true"
            else:
                # Service 仅 flush；Task、TaskNode、队列号和审计在这里一次提交。
                audit(session, request, "CREATE_TASK", "TASK", task.id, {"package_id": payload.package_id, "target_node_count": task.target_node_count})
                session.commit()
        except Exception:
            session.rollback()
            raise
        return serialize_task(task, include_nodes=True)

    @protected.get("/tasks", summary="查询任务列表")
    def list_tasks(session: Session = Depends(get_session)):
        """按创建时间倒序返回 Task 摘要。"""
        tasks = session.scalars(select(Task).order_by(Task.created_at.desc())).all()
        return [serialize_task(task) for task in tasks]

    @protected.get("/tasks/{task_id}", summary="查询任务详情")
    def task_detail(task_id: str, session: Session = Depends(get_session)):
        """返回 Task、节点、Attempt 和 Step 的完整层级。"""
        task = session.scalar(select(Task).where(Task.id == task_id).options(selectinload(Task.nodes).selectinload(TaskNode.attempts).selectinload(TaskAttempt.steps)))
        if task is None:
            raise HTTPException(status_code=404, detail="Task 不存在")
        return serialize_task(task, include_nodes=True)

    @protected.post("/tasks/{task_id}/cancel", dependencies=[Depends(require_csrf)], summary="取消任务排队节点")
    def cancel_task(task_id: str, request: Request, session: Session = Depends(get_session)):
        """批量取消调用瞬间仍为 Waiting 的节点，不终止 Running 节点。"""
        task = session.scalar(select(Task).where(Task.id == task_id).options(selectinload(Task.nodes)))
        if task is None:
            raise HTTPException(status_code=404, detail="Task 不存在")
        waiting_ids = [node.id for node in task.nodes if node.status == "WAITING"]
        # 结束列表读取事务，随后每行通过 status=WAITING 条件更新与 Scheduler Claim 竞争。
        session.rollback()
        cancelled = 0
        for node_id in waiting_ids:
            cancelled += session.query(TaskNode).filter(
                TaskNode.id == node_id,
                TaskNode.status == "WAITING",
            ).update({TaskNode.status: "CANCELLED", TaskNode.finished_at: utcnow()})
        if cancelled == 0:
            session.rollback()
            raise HTTPException(status_code=409, detail="Task 没有可取消的 Waiting 节点")
        try:
            # CAS 结果、Task 聚合字段和审计必须在同一事务中成功或一起回滚。
            task = aggregate_task(session, task_id)
            audit(session, request, "CANCEL_TASK", "TASK", task_id, {"cancelled_nodes": cancelled})
            session.commit()
        except Exception:
            session.rollback()
            raise
        return serialize_task(task, include_nodes=True)

    @protected.post("/tasks/{task_id}/nodes/{task_node_id}/cancel", dependencies=[Depends(require_csrf)], summary="取消单个排队节点")
    def cancel_node(task_id: str, task_node_id: str, request: Request, session: Session = Depends(get_session)):
        """只允许把指定 Waiting TaskNode 原子更新为 Cancelled。"""
        task_node = session.get(TaskNode, task_node_id)
        if task_node is None or task_node.task_id != task_id:
            raise HTTPException(status_code=404, detail="TaskNode 不存在")
        task_node = cancel_task_node(session, task_node)
        audit(session, request, "CANCEL_TASK_NODE", "TASK_NODE", task_node_id)
        session.commit()
        return serialize_task_node(task_node)

    @protected.post("/tasks/{task_id}/nodes/{task_node_id}/retry", dependencies=[Depends(require_csrf)], summary="重试失败节点")
    def retry_node(task_id: str, task_node_id: str, request: Request, session: Session = Depends(get_session)):
        """把 Failed TaskNode 重新排到当前队尾，下次执行从 Step1 创建新 Attempt。"""
        task_node = session.scalar(select(TaskNode).where(TaskNode.id == task_node_id).options(selectinload(TaskNode.task).selectinload(Task.nodes)))
        if task_node is None or task_node.task_id != task_id:
            raise HTTPException(status_code=404, detail="TaskNode 不存在")
        task_node = retry_task_node(session, task_node)
        audit(session, request, "RETRY_TASK_NODE", "TASK_NODE", task_node_id, {"queue_seq": task_node.queue_seq})
        session.commit()
        return serialize_task_node(task_node)

    @protected.get("/tasks/{task_id}/copy-template", summary="复制任务模板")
    def copy_task_template(task_id: str, session: Session = Depends(get_session)):
        """从历史任务提取当前仍可用的包、节点和备注。"""
        task = session.scalar(select(Task).where(Task.id == task_id).options(selectinload(Task.nodes)))
        if task is None:
            raise HTTPException(status_code=404, detail="Task 不存在")
        if task.package_id is None or session.get(Package, task.package_id) is None:
            raise HTTPException(status_code=409, detail="当前 Package 已不存在，不能复制")
        return {"package_id": task.package_id, "node_ids": [node.node_id for node in task.nodes if node.node_id], "role_names": [], "remark": task.remark}

    # 日志：先校验完整父子链，防止只知道 step_id 就跨 Task 读取日志。
    @protected.get("/tasks/{task_id}/nodes/{task_node_id}/attempts/{attempt_id}/steps/{step_id}/logs", summary="分页读取 Step 日志")
    def read_logs(task_id: str, task_node_id: str, attempt_id: str, step_id: str, stream: str = "stdout", offset: int = 0, session: Session = Depends(get_session)):
        """按字节 offset 返回最多 1 MiB stdout 或 stderr。"""
        step = session.get(TaskStepResult, step_id)
        attempt = session.get(TaskAttempt, attempt_id)
        node = session.get(TaskNode, task_node_id)
        if not step or not attempt or not node or step.attempt_id != attempt_id or attempt.task_node_id != task_node_id or node.task_id != task_id:
            raise HTTPException(status_code=404, detail="日志对象不存在")
        path = Path(step.stdout_path if stream == "stdout" else step.stderr_path or "")
        if not path.exists():
            return {"data": "", "offset": offset, "available": False}
        with path.open("rb") as file:
            file.seek(max(0, offset))
            data = file.read(1024 * 1024)
            new_offset = file.tell()
        return {"data": data.decode("utf-8", errors="replace"), "offset": new_offset, "available": True}

    @protected.get("/tasks/{task_id}/nodes/{task_node_id}/attempts/{attempt_id}/steps/{step_id}/logs/stream", summary="订阅 Step 实时日志")
    def stream_logs(task_id: str, task_node_id: str, attempt_id: str, step_id: str, request: Request, stream: str = "stdout", session: Session = Depends(get_session)):
        """以 SSE 推送增量日志，支持用 Last-Event-ID 从字节 offset 续传。"""
        step = session.get(TaskStepResult, step_id)
        attempt = session.get(TaskAttempt, attempt_id)
        node = session.get(TaskNode, task_node_id)
        if not step or not attempt or not node or step.attempt_id != attempt_id or attempt.task_node_id != task_node_id or node.task_id != task_id:
            raise HTTPException(status_code=404, detail="日志对象不存在")
        path = Path(step.stdout_path if stream == "stdout" else step.stderr_path or "")
        try:
            # SSE id 就是文件字节 offset；浏览器重连时可通过 Last-Event-ID 无重复续读。
            start_offset = int(request.headers.get("Last-Event-ID", "0"))
        except ValueError:
            start_offset = 0

        def events() -> Iterator[str]:
            """逐段读取日志，完成时发送 end 事件，空闲时发送心跳。"""
            offset = max(0, start_offset)
            idle = 0
            while idle < 1800:
                if path.exists():
                    with path.open("rb") as file:
                        file.seek(offset)
                        data = file.read(64 * 1024)
                        offset = file.tell()
                    if data:
                        idle = 0
                        payload = json.dumps({"stream": stream, "data": data.decode("utf-8", errors="replace")}, ensure_ascii=False)
                        yield f"id: {offset}\nevent: log\ndata: {payload}\n\n"
                        continue
                current_status = session.execute(
                    select(TaskStepResult.status).where(TaskStepResult.id == step_id)
                ).scalar_one_or_none()
                # 每轮结束隐式只读事务，不能让最长 30 分钟的 SSE 持续占用连接快照。
                session.rollback()
                if current_status in {"SUCCESS", "FAILED", "SKIPPED"}:
                    yield f"id: {offset}\nevent: end\ndata: {json.dumps({'status': current_status})}\n\n"
                    break
                idle += 1
                if idle % 15 == 0:
                    yield ": heartbeat\n\n"
                time.sleep(1)

        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # 配置与审计：敏感值永不回显，写配置与审计记录在同一事务提交。
    @protected.get("/settings", summary="查询系统设置")
    def get_settings(session: Session = Depends(get_session)):
        """返回可编辑配置；Salt credential 仅以固定掩码表示是否已设置。"""
        values = {item.key: item.value for item in session.scalars(select(SystemSetting))}
        # API 永不解密回显 credential，避免明文进入浏览器、日志或网络调试记录。
        values["salt_api_credential"] = "********" if values.get("salt_api_credential") else ""
        # RoleRule 表是识别执行的唯一事实来源，避免重复 JSON 与实际规则漂移。
        values["role_detection_rules"] = [
            {
                "role": rule.role,
                "matcher_type": "process",
                "pattern": rule.pattern,
                "enabled": rule.enabled,
            }
            for rule in session.scalars(
                select(RoleRule)
                .where(RoleRule.matcher_type == "process")
                .order_by(RoleRule.role, RoleRule.pattern, RoleRule.id)
            )
        ]
        for key in ["salt_request_timeout", "max_upload_size", "default_step_timeout", "execution_log_retention_days", "node_status_check_interval"]:
            if key in values:
                values[key] = int(values[key])
        return values

    @protected.patch("/settings", dependencies=[Depends(require_csrf)], summary="更新系统设置")
    def patch_settings(payload: SettingsUpdateRequest, request: Request, session: Session = Depends(get_session)):
        """校验并持久化运行时设置，同时更新当前进程内配置。"""
        updates = payload.model_dump(exclude_none=True)
        if "role_detection_rules" in updates:
            rules = updates.pop("role_detection_rules")
            identities = {(item["role"], item["pattern"]) for item in rules}
            if len(identities) != len(rules):
                raise HTTPException(status_code=422, detail="角色识别规则不得重复")
            session.query(RoleRule).delete()
            for item in rules:
                session.add(RoleRule(
                    role=item["role"],
                    matcher_type="process",
                    pattern=item["pattern"],
                    enabled=item["enabled"],
                ))
            updates["role_detection_rules"] = json.dumps(rules, ensure_ascii=False)
        for key, value in updates.items():
            sensitive = key == "salt_api_credential"
            # credential 只以应用密钥加密后的密文落库，明文仅存在于当前请求内存。
            stored = encrypt_secret(settings, str(value)) if sensitive else str(value)
            record = session.get(SystemSetting, key)
            if record is None:
                session.add(SystemSetting(key=key, value=stored, sensitive=sensitive))
            else:
                record.value = stored
                record.sensitive = sensitive
            if key == "max_upload_size":
                settings.max_upload_size = int(value)
            elif key == "default_step_timeout":
                settings.default_step_timeout = int(value)
            elif key == "execution_log_retention_days":
                settings.execution_log_retention_days = int(value)
            elif key == "node_status_check_interval":
                settings.node_status_check_interval = int(value)
            elif key == "salt_request_timeout":
                settings.salt_request_timeout = int(value)
            elif key == "salt_api_url":
                settings.salt_api_url = str(value)
            elif key == "salt_api_username":
                settings.salt_api_username = str(value)
            elif key == "salt_api_credential":
                settings.salt_api_credential = str(value)
            elif key == "package_storage_path":
                settings.package_dir = Path(str(value))
                settings.package_dir.mkdir(parents=True, exist_ok=True)
            elif key == "temp_path":
                settings.temp_dir = Path(str(value))
                settings.temp_dir.mkdir(parents=True, exist_ok=True)
        audit(session, request, "UPDATE_SETTINGS", "SYSTEM_SETTING", None, {"keys": sorted(updates)})
        session.commit()
        return get_settings(session)

    @protected.get("/audit-logs", summary="查询审计日志")
    def list_audit_logs(limit: int = 100, session: Session = Depends(get_session)):
        """按时间倒序返回至多 1000 条审计记录。"""
        limit = min(max(limit, 1), 1000)
        logs = session.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)).all()
        return [
            {
                "id": item.id,
                "source_ip": item.source_ip,
                "operation": item.operation,
                "object_type": item.object_type,
                "object_id": item.object_id,
                "detail": json.loads(item.detail_json),
                "created_at": _iso(item.created_at),
            }
            for item in logs
        ]

    api.include_router(protected)
    return api
