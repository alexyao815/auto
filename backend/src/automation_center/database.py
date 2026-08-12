"""SQLAlchemy 引擎、会话、SQLite 备份和 Alembic 启动入口。"""

from __future__ import annotations

import shutil
import sqlite3
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Generator

from sqlalchemy import Engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy import create_engine

from .config import Settings


class Base(DeclarativeBase):
    """所有 ORM 模型共享的声明式基类。"""

    pass


def create_db_engine(settings: Settings) -> Engine:
    """创建数据库引擎，并为 SQLite 连接设置并发与完整性约束。"""

    connect_args = {"check_same_thread": False, "timeout": 30} if settings.database_url.startswith("sqlite") else {}
    engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True)

    if settings.database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection: sqlite3.Connection, _: object) -> None:
            """对连接池中新建的每条 SQLite 连接重复设置连接级 PRAGMA。"""

            # WAL 允许读写并行；foreign_keys 让快照/删除语义真正由数据库约束。
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """创建请求和 Worker 共用的短生命周期 Session 工厂。"""

    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def session_dependency(factory: sessionmaker[Session]):
    """把 Session 工厂适配为 FastAPI 依赖，并确保请求结束时关闭连接。"""

    def _get_session() -> Generator[Session, None, None]:
        """为单次请求提供 Session；事务提交与回滚由业务函数显式控制。"""

        session = factory()
        try:
            yield session
        finally:
            session.close()

    return _get_session


def backup_sqlite(settings: Settings) -> Path | None:
    """使用 SQLite Backup API 创建一致性备份；空库或非 SQLite 返回 None。"""

    if not settings.database_url.startswith("sqlite:///"):
        return None
    source = Path(settings.database_url.removeprefix("sqlite:///"))
    if not source.exists() or source.stat().st_size == 0:
        return None
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = settings.backup_dir / f"automation-center-{stamp}.db"
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    shutil.copystat(source, target)
    return target


def run_migrations(settings: Settings) -> None:
    """定位受控 Alembic 配置并升级到 head，定位失败时拒绝启动。"""

    from alembic import command
    from alembic.config import Config

    candidates = [
        # 同时兼容本地仓库、可编辑安装和容器中的固定后端目录。
        Path(os.getenv("AUTOMATION_CENTER_BACKEND_ROOT", "")),
        Path.cwd(),
        Path(__file__).resolve().parents[2],
        Path("/app/backend"),
    ]
    backend_root = next((candidate for candidate in candidates if candidate and (candidate / "alembic.ini").exists()), None)
    if backend_root is None:
        raise RuntimeError("找不到 alembic.ini，拒绝在未知 Schema 下启动")
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
    command.upgrade(config, "head")
