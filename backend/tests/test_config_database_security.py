from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

from sqlalchemy import inspect, text

from automation_center.cli import main as cli_main
from automation_center.config import Settings
from automation_center.database import Base, backup_sqlite, create_db_engine, create_session_factory, run_migrations
from automation_center.models import (
    Credential,
    Node,
    RoleDetectionJob,
    RoleDetectionNodeResult,
    RoleRule,
    SessionRecord,
    SystemSetting,
)
from automation_center.security import decrypt_secret, encrypt_secret, hash_password, reset_credential, verify_password


def test_settings_from_env_and_encryption(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOMATION_CENTER_DATA_DIR", str(tmp_path / "env-data"))
    monkeypatch.setenv("AUTOMATION_CENTER_COOKIE_SECURE", "no")
    monkeypatch.setenv("AUTOMATION_CENTER_ENABLE_SCHEDULER", "0")
    monkeypatch.setenv("AUTOMATION_CENTER_APP_SECRET", "env-secret")
    loaded = Settings.from_env()
    loaded.ensure_directories()
    assert loaded.cookie_secure is False and loaded.enable_scheduler is False
    encrypted = encrypt_secret(loaded, "salt-password")
    assert encrypted != "salt-password"
    assert decrypt_secret(loaded, encrypted) == "salt-password"
    digest = hash_password("password")
    assert verify_password(digest, "password") and not verify_password(digest, "wrong")


def test_backup_migrations_and_reset(settings):
    settings.ensure_directories()
    run_migrations(settings)
    engine = create_db_engine(settings); factory = create_session_factory(engine)
    assert {"role_detection_jobs", "role_detection_node_results"}.issubset(inspect(engine).get_table_names())
    with factory() as session:
        session.add(Credential(username="old", password_hash=hash_password("old-pass")))
        session.add(SessionRecord(id="s", token_hash="t", csrf_hash="c", idle_expires_at=__import__('datetime').datetime.max, absolute_expires_at=__import__('datetime').datetime.max))
        session.commit()
        reset_credential(session, "new", "new-pass")
        record = session.get(Credential, 1)
        assert record.username == "new" and verify_password(record.password_hash, "new-pass")
        assert session.get(SessionRecord, "s") is None
    backup = backup_sqlite(settings)
    assert backup and backup.exists()
    with sqlite3.connect(backup) as connection:
        assert connection.execute("select count(*) from credentials").fetchone()[0] == 1
    engine.dispose()


def test_role_detection_migration_cleans_legacy_service_rules(settings):
    settings.ensure_directories()
    engine = create_db_engine(settings)
    Base.metadata.create_all(engine)
    # 构造真正的 0001 既有数据库：没有新任务表，Alembic 已记录旧 revision。
    RoleDetectionNodeResult.__table__.drop(engine)
    RoleDetectionJob.__table__.drop(engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version (version_num) VALUES ('0001_initial')"))
    factory = create_session_factory(engine)
    legacy_rules = [
        {"role": "storage", "matcher_type": "service", "pattern": "ceph-osd", "enabled": True},
        {"role": "compute", "matcher_type": "process", "pattern": "nova-compute", "enabled": True},
    ]
    with factory() as session:
        session.add(Node(id="legacy", hostname="legacy", role_override=True))
        session.add_all([
            RoleRule(role="storage", matcher_type="service", pattern="ceph-osd", enabled=True),
            RoleRule(role="compute", matcher_type="process", pattern="nova-compute", enabled=True),
        ])
        session.add(SystemSetting(key="role_detection_rules", value=json.dumps(legacy_rules), sensitive=False))
        session.commit()
    engine.dispose()

    # 0002 必须从旧 revision 创建表，并同步清理 service 规则与 override 语义。
    run_migrations(settings)
    engine = create_db_engine(settings)
    factory = create_session_factory(engine)
    assert {"role_detection_jobs", "role_detection_node_results"}.issubset(inspect(engine).get_table_names())
    with factory() as session:
        assert session.get(Node, "legacy").role_override is False
        rules = list(session.query(RoleRule).all())
        assert [(rule.matcher_type, rule.role) for rule in rules] == [("process", "compute")]
        stored = json.loads(session.get(SystemSetting, "role_detection_rules").value)
        assert stored == [
            {"role": "compute", "matcher_type": "process", "pattern": "nova-compute", "enabled": True}
        ]
    engine.dispose()


def test_cli_backup_and_password_reset(monkeypatch, settings, capsys):
    monkeypatch.setenv("AUTOMATION_CENTER_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("AUTOMATION_CENTER_DATABASE_URL", settings.database_url)
    monkeypatch.setattr(sys, "argv", ["automation-center", "reset-password", "--username", "cli", "--password", "cli-password"])
    cli_main()
    assert "账号已重置" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["automation-center", "backup-db"])
    cli_main()
    assert ".db" in capsys.readouterr().out
