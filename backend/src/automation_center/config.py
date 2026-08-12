"""集中定义运行配置、环境变量入口和持久化目录布局。"""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path


GIB = 1024**3
MIB = 1024**2


def _bool_env(name: str, default: bool) -> bool:
    """读取宽松布尔环境变量；未设置时保留显式默认值。"""

    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class Settings:
    """应用进程共享配置。

    环境变量负责首次启动值，部分业务设置会在启动后由数据库中的
    ``SystemSetting`` 覆盖。路径字段始终指向可持久化或可安全清理的目录。
    """

    data_dir: Path
    database_url: str
    package_dir: Path
    temp_dir: Path
    log_dir: Path
    work_dir: Path
    backup_dir: Path
    cookie_name: str = "automation_center_session"
    cookie_secure: bool = True
    session_idle_seconds: int = 8 * 3600
    session_absolute_seconds: int = 24 * 3600
    scheduler_interval_seconds: float = 1.0
    scheduler_max_workers: int = 30
    max_upload_size: int = 10 * GIB
    max_extracted_size: int = 20 * GIB
    max_archive_files: int = 100_000
    max_manifest_size: int = 1 * MIB
    max_steps: int = 100
    default_step_timeout: int = 1800
    execution_log_retention_days: int = 7
    failed_work_retention_days: int = 7
    node_status_check_interval: int = 60
    salt_request_timeout: int = 30
    salt_mode: str = "fake"
    salt_api_url: str = "http://127.0.0.1:8000"
    salt_api_username: str = "automation"
    salt_api_credential: str = ""
    salt_eauth: str = "file"
    initial_username: str = "admin"
    initial_password: str = "ChangeMe-Immediately!"
    app_secret: str = "development-only-change-me"
    startup_migrate: bool = True
    enable_scheduler: bool = True

    @property
    def encryption_key(self) -> bytes:
        """从应用密钥稳定派生 Fernet Key，不把原始密钥写入数据库。"""

        digest = hashlib.sha256(self.app_secret.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest)

    @classmethod
    def from_env(cls) -> "Settings":
        """从环境变量构造配置，并为单机部署建立统一的数据目录约定。"""

        data_dir = Path(os.getenv("AUTOMATION_CENTER_DATA_DIR", "/var/lib/automation-center"))
        database_path = data_dir / "db" / "automation-center.db"
        return cls(
            data_dir=data_dir,
            database_url=os.getenv("AUTOMATION_CENTER_DATABASE_URL", f"sqlite:///{database_path}"),
            package_dir=Path(os.getenv("AUTOMATION_CENTER_PACKAGE_DIR", str(data_dir / "packages"))),
            temp_dir=Path(os.getenv("AUTOMATION_CENTER_TEMP_DIR", str(data_dir / "temp"))),
            log_dir=Path(os.getenv("AUTOMATION_CENTER_LOG_DIR", str(data_dir / "logs"))),
            work_dir=Path(os.getenv("AUTOMATION_CENTER_WORK_DIR", str(data_dir / "work"))),
            backup_dir=Path(os.getenv("AUTOMATION_CENTER_BACKUP_DIR", str(data_dir / "backups"))),
            cookie_secure=_bool_env("AUTOMATION_CENTER_COOKIE_SECURE", True),
            salt_mode=os.getenv("AUTOMATION_CENTER_SALT_MODE", "fake"),
            salt_api_url=os.getenv("AUTOMATION_CENTER_SALT_API_URL", "http://127.0.0.1:8000"),
            salt_api_username=os.getenv("AUTOMATION_CENTER_SALT_API_USERNAME", "automation"),
            salt_api_credential=os.getenv("AUTOMATION_CENTER_SALT_API_CREDENTIAL", ""),
            salt_eauth=os.getenv("AUTOMATION_CENTER_SALT_EAUTH", "file"),
            initial_username=os.getenv("AUTOMATION_CENTER_INITIAL_USERNAME", "admin"),
            initial_password=os.getenv("AUTOMATION_CENTER_INITIAL_PASSWORD", "ChangeMe-Immediately!"),
            app_secret=os.getenv("AUTOMATION_CENTER_APP_SECRET", "development-only-change-me"),
            startup_migrate=_bool_env("AUTOMATION_CENTER_STARTUP_MIGRATE", True),
            enable_scheduler=_bool_env("AUTOMATION_CENTER_ENABLE_SCHEDULER", True),
        )

    def ensure_directories(self) -> None:
        """在数据库或上传开始前创建全部必需目录。"""

        for path in (
            self.data_dir,
            Path(self.database_url.removeprefix("sqlite:///")).parent,
            self.package_dir,
            self.temp_dir,
            self.log_dir,
            self.work_dir,
            self.backup_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
