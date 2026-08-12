"""容器内运维 CLI：共享账号重置和 SQLite 即时备份。"""

from __future__ import annotations

import argparse
import sys

from .config import Settings
from .database import Base, backup_sqlite, create_db_engine, create_session_factory
from .security import reset_credential


def main() -> None:
    """解析子命令，并复用与服务进程相同的配置和数据库模型。"""

    parser = argparse.ArgumentParser(prog="automation-center")
    subparsers = parser.add_subparsers(dest="command", required=True)
    reset = subparsers.add_parser("reset-password", help="重置固定共享账号和密码")
    reset.add_argument("--username", required=True)
    reset.add_argument("--password", required=True)
    subparsers.add_parser("backup-db", help="立即备份 SQLite")
    args = parser.parse_args()
    settings = Settings.from_env()
    settings.ensure_directories()
    if args.command == "backup-db":
        path = backup_sqlite(settings)
        print(path or "数据库尚未创建")
        return
    engine = create_db_engine(settings)
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        reset_credential(session, args.username, args.password)
    print("账号已重置，现有会话已全部失效")


if __name__ == "__main__":
    main()
