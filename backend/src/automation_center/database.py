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


SQLITE_BUSY_TIMEOUT_MS = 5_000


class Base(DeclarativeBase):
    """所有 ORM 模型共享的声明式基类。"""

    pass


def create_db_engine(settings: Settings) -> Engine:
    """创建数据库引擎，并为 SQLite 设置并发与完整性约束。

    ``journal_mode`` 是数据库级持久设置，只能在应用启动、尚无并发请求时设置
    一次。若在每条新连接上重复切换 WAL，并发建立连接时该 PRAGMA 自身会争用
    数据库锁，最终让登录等短请求等待几十秒。
    """

    connect_args = {
        "check_same_thread": False,
        "timeout": SQLITE_BUSY_TIMEOUT_MS / 1000,
    } if settings.database_url.startswith("sqlite") else {}
    engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True)

    if settings.database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection: sqlite3.Connection, _: object) -> None:
            """为连接池中新建的每条连接设置连接级 PRAGMA。"""

            # foreign_keys 和 busy_timeout 都是连接级设置，必须逐连接应用。
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
            cursor.close()

        # 此时 Scheduler 和 HTTP Server 尚未启动，可以安全地一次性启用 WAL。
        with engine.connect() as connection:
            journal_mode = connection.exec_driver_sql("PRAGMA journal_mode=WAL").scalar_one()
            if str(journal_mode).lower() != "wal":
                raise RuntimeError(f"SQLite WAL 启用失败，当前模式为 {journal_mode}")

    return engine


def is_sqlite_locked(error: BaseException) -> bool:
    """判断 SQLAlchemy/SQLite 异常是否属于可重试的写锁竞争。"""

    return "database is locked" in str(error).lower()


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
