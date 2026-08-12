from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automation_center.config import Settings
from automation_center.main import create_app
from automation_center.salt import FakeSaltAdapter


def build_bundle(
    tmp_path: Path,
    *,
    name: str = "demo-fix",
    script: str = "scripts/fix.sh",
    executor_type: str = "shell",
    failure_action: str = "stop",
    timeout: int = 30,
    checksum_ok: bool = True,
    manifest_version: int = 1,
) -> Path:
    inner = tmp_path / f"{name}-inner.tar.gz"
    manifest = (
        f"manifest_version: {manifest_version}\n"
        f"name: {name}\n"
        "description: demo package\n"
        "component: nova\n"
        "bug_id: BUG-1\n"
        "target_roles: [compute]\n"
        "applicable_versions: [Yoga]\n"
        "steps:\n"
        "  - name: fix\n"
        f"    type: {executor_type}\n"
        f"    script: {script}\n"
        f"    timeout: {timeout}\n"
        f"    failure_action: {failure_action}\n"
    ).encode()
    script_data = b"#!/bin/bash\necho ok\n"
    with tarfile.open(inner, "w:gz") as archive:
        info = tarfile.TarInfo("manifest.yaml")
        info.size = len(manifest)
        archive.addfile(info, io.BytesIO(manifest))
        info = tarfile.TarInfo(script)
        info.size = len(script_data)
        archive.addfile(info, io.BytesIO(script_data))
    digest = hashlib.sha256(inner.read_bytes()).hexdigest()
    if not checksum_ok:
        digest = "0" * 64
    checksum = f"{digest}  inner-package.tar.gz\n".encode()
    outer = tmp_path / f"{name}.bundle.tar.gz"
    with tarfile.open(outer, "w:gz") as archive:
        archive.add(inner, arcname="bundle/inner-package.tar.gz")
        info = tarfile.TarInfo("bundle/inner-package.sha256")
        info.size = len(checksum)
        archive.addfile(info, io.BytesIO(checksum))
    return outer


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    data = tmp_path / "data"
    return Settings(
        data_dir=data,
        database_url=f"sqlite:///{data / 'db' / 'automation.db'}",
        package_dir=data / "packages",
        temp_dir=data / "temp",
        log_dir=data / "logs",
        work_dir=data / "work",
        backup_dir=data / "backups",
        cookie_secure=False,
        initial_username="admin",
        initial_password="correct-password",
        app_secret="test-secret",
        startup_migrate=False,
        enable_scheduler=False,
        scheduler_interval_seconds=0.01,
    )


@pytest.fixture()
def salt() -> FakeSaltAdapter:
    return FakeSaltAdapter()


@pytest.fixture()
def client(settings: Settings, salt: FakeSaltAdapter):
    app = create_app(settings, salt)
    with TestClient(app, base_url="http://testserver") as test_client:
        yield test_client


@pytest.fixture()
def auth(client: TestClient) -> dict[str, str]:
    response = client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-password"})
    assert response.status_code == 200
    return {"X-CSRF-Token": response.json()["csrf_token"]}

