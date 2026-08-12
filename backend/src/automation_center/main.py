"""FastAPI 应用工厂，以及数据库迁移、默认配置和调度器的生命周期编排。"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from . import __version__
from .api import create_api_router
from .config import Settings
from .database import Base, backup_sqlite, create_db_engine, create_session_factory, is_sqlite_locked, run_migrations, session_dependency
from .models import RoleRule, SystemSetting
from .salt import SaltAdapter, create_salt_adapter
from .scheduler import Scheduler
from .security import build_auth_dependencies, decrypt_secret, encrypt_secret, ensure_initial_credential


DEFAULT_ROLE_RULES = [
    {"role": "compute", "matcher_type": "process", "pattern": "nova-compute"},
    {"role": "ceph", "matcher_type": "process", "pattern": "ceph-osd"},
    {"role": "network", "matcher_type": "process", "pattern": "ovn-controller"},
    {"role": "network", "matcher_type": "process", "pattern": "ovs-vswitchd"},
    {"role": "controller", "matcher_type": "process", "pattern": "nova-api"},
]
logger = logging.getLogger(__name__)


def seed_defaults(factory, settings: Settings) -> None:
    """补齐首次启动所需的账号、系统配置和角色规则。

    只写入尚不存在的键，避免服务重启时覆盖管理员已保存的设置；Salt
    credential 在进入数据库前加密。函数自行提交一个短事务。
    """
    with factory() as session:
        ensure_initial_credential(session, settings)
        defaults = {
            "salt_api_url": settings.salt_api_url,
            "salt_api_username": settings.salt_api_username,
            "salt_request_timeout": str(settings.salt_request_timeout),
            "package_storage_path": str(settings.package_dir),
            "temp_path": str(settings.temp_dir),
            "max_upload_size": str(settings.max_upload_size),
            "default_step_timeout": str(settings.default_step_timeout),
            "execution_log_retention_days": str(settings.execution_log_retention_days),
            "node_status_check_interval": str(settings.node_status_check_interval),
            "role_detection_rules": json.dumps(DEFAULT_ROLE_RULES, ensure_ascii=False),
        }
        if settings.salt_api_credential:
            defaults["salt_api_credential"] = encrypt_secret(settings, settings.salt_api_credential)
        for key, value in defaults.items():
            if session.get(SystemSetting, key) is None:
                session.add(SystemSetting(key=key, value=value, sensitive=key == "salt_api_credential"))
        if not session.scalar(select(RoleRule).limit(1)):
            for rule in DEFAULT_ROLE_RULES:
                session.add(RoleRule(**rule, enabled=True))
        session.commit()


def load_persisted_settings(factory, settings: Settings) -> None:
    """把数据库中的运行时设置覆盖到环境变量构造出的 ``Settings``。

    数据库是管理员设置的持久化来源，敏感项在读取后解密到进程内存；路径变化后
    会确保目标目录存在。这里不执行写事务。
    """
    with factory() as session:
        values = {item.key: item for item in session.scalars(select(SystemSetting))}
    string_fields = {
        "salt_api_url": "salt_api_url",
        "salt_api_username": "salt_api_username",
    }
    integer_fields = {
        "salt_request_timeout": "salt_request_timeout",
        "max_upload_size": "max_upload_size",
        "default_step_timeout": "default_step_timeout",
        "execution_log_retention_days": "execution_log_retention_days",
        "node_status_check_interval": "node_status_check_interval",
    }
    for key, attribute in string_fields.items():
        if key in values:
            setattr(settings, attribute, values[key].value)
    for key, attribute in integer_fields.items():
        if key in values:
            setattr(settings, attribute, int(values[key].value))
    if values.get("salt_api_credential"):
        settings.salt_api_credential = decrypt_secret(settings, values["salt_api_credential"].value)
    if values.get("package_storage_path"):
        settings.package_dir = Path(values["package_storage_path"].value)
    if values.get("temp_path"):
        settings.temp_dir = Path(values["temp_path"].value)
    settings.ensure_directories()


def create_app(settings: Settings | None = None, salt_adapter: SaltAdapter | None = None) -> FastAPI:
    """创建可注入配置和 Salt Adapter 的应用实例。

    注入参数让测试能够使用隔离数据目录和 Fake Salt。生产启动时则从环境变量
    构建配置，并在同一个应用生命周期内运行单实例 Scheduler。
    """
    settings = settings or Settings.from_env()
    settings.ensure_directories()
    engine = create_db_engine(settings)
    factory = create_session_factory(engine)
    salt = salt_adapter or create_salt_adapter(settings)
    scheduler = Scheduler(factory, settings, salt)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """按“备份 → 迁移 → 初始化 → 调度”的顺序管理服务生命周期。"""
        if settings.startup_migrate:
            # 迁移失败会阻止应用进入可服务状态；迁移前的 Backup API 快照用于人工回滚。
            backup_sqlite(settings)
            run_migrations(settings)
        else:
            Base.metadata.create_all(engine)
        seed_defaults(factory, settings)
        load_persisted_settings(factory, settings)
        # 单进程部署让内置 Scheduler 只有一个实例；多 Worker 会破坏这个不变量。
        scheduler_task = asyncio.create_task(scheduler.run()) if settings.enable_scheduler else None
        try:
            yield
        finally:
            if scheduler_task:
                await scheduler.stop()
                await scheduler_task
            engine.dispose()

    app = FastAPI(title="云平台自动化维护中心", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = factory
    app.state.salt = salt
    app.state.scheduler = scheduler
    get_session = session_dependency(factory)
    require_session, require_csrf = build_auth_dependencies(settings, get_session)
    app.include_router(create_api_router(settings, get_session, salt, require_session, require_csrf))

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):
        """贯穿代理、应用日志和响应返回同一个无敏感信息的请求标识。"""

        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        """把业务 HTTP 异常统一转换为可机器解析的错误结构。"""
        return JSONResponse(
            status_code=exc.status_code,
            content={"type": "about:blank", "title": "请求失败", "status": exc.status_code, "detail": exc.detail, "instance": str(request.url.path)},
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        """统一 FastAPI 参数校验错误，避免各端点返回格式不一致。"""
        return JSONResponse(
            status_code=422,
            content={"type": "about:blank", "title": "参数校验失败", "status": 422, "detail": exc.errors(), "instance": str(request.url.path)},
        )

    @app.exception_handler(OperationalError)
    async def database_operational_error(request: Request, exc: OperationalError):
        """把 SQLite 锁竞争快速转换为可重试响应，其余数据库错误保留异常栈。"""

        request_id = getattr(request.state, "request_id", "")
        if is_sqlite_locked(exc):
            logger.warning("SQLite 写锁超时 request_id=%s path=%s", request_id, request.url.path)
            return JSONResponse(
                status_code=503,
                content={
                    "type": "about:blank",
                    "title": "服务暂时繁忙",
                    "status": 503,
                    "detail": "数据库正在处理其他写操作，请稍后重试",
                    "instance": str(request.url.path),
                },
                headers={"Retry-After": "1", "X-Request-ID": request_id},
            )
        logger.error(
            "数据库操作异常 request_id=%s path=%s",
            request_id,
            request.url.path,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return JSONResponse(
            status_code=500,
            content={"type": "about:blank", "title": "服务器错误", "status": 500, "detail": "数据库操作失败", "instance": str(request.url.path)},
            headers={"X-Request-ID": request_id},
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        """记录未预期异常栈，同时避免把内部路径或凭据暴露给浏览器。"""

        request_id = getattr(request.state, "request_id", "")
        logger.error(
            "未处理异常 request_id=%s path=%s",
            request_id,
            request.url.path,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return JSONResponse(
            status_code=500,
            content={"type": "about:blank", "title": "服务器错误", "status": 500, "detail": "服务器内部错误", "instance": str(request.url.path)},
            headers={"X-Request-ID": request_id},
        )

    return app
