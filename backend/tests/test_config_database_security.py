from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

from automation_center.cli import main as cli_main
from automation_center.config import Settings
from automation_center.database import Base, backup_sqlite, create_db_engine, create_session_factory, run_migrations
from automation_center.models import Credential, SessionRecord
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


def test_cli_backup_and_password_reset(monkeypatch, settings, capsys):
    monkeypatch.setenv("AUTOMATION_CENTER_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("AUTOMATION_CENTER_DATABASE_URL", settings.database_url)
    monkeypatch.setattr(sys, "argv", ["automation-center", "reset-password", "--username", "cli", "--password", "cli-password"])
    cli_main()
    assert "账号已重置" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["automation-center", "backup-db"])
    cli_main()
    assert ".db" in capsys.readouterr().out

