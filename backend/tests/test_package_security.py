from __future__ import annotations

import io
import tarfile

from .conftest import build_bundle


def test_rejects_bad_checksum(client, auth, tmp_path):
    bundle = build_bundle(tmp_path, name="bad-sha", checksum_ok=False)
    with bundle.open("rb") as stream:
        response = client.post("/api/v1/packages", files={"file": (bundle.name, stream, "application/gzip")}, headers=auth)
    assert response.status_code == 422
    assert "SHA256" in response.json()["detail"]


def test_requires_manifest_version_one(client, auth, tmp_path):
    bundle = build_bundle(tmp_path, name="bad-version", manifest_version=2)
    with bundle.open("rb") as stream:
        response = client.post("/api/v1/packages", files={"file": (bundle.name, stream, "application/gzip")}, headers=auth)
    assert response.status_code == 422
    assert "manifest_version" in response.json()["detail"]


def test_rejects_archive_traversal(client, auth, tmp_path):
    outer = tmp_path / "evil.tar.gz"
    with tarfile.open(outer, "w:gz") as archive:
        # 直接写 TarInfo 才能构造正常打包工具会规避的父目录逃逸成员。
        info = tarfile.TarInfo("../inner-package.tar.gz")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
        checksum = b"0" * 64 + b"  inner-package.tar.gz\n"
        info = tarfile.TarInfo("inner-package.sha256")
        info.size = len(checksum)
        archive.addfile(info, io.BytesIO(checksum))
    with outer.open("rb") as stream:
        response = client.post("/api/v1/packages", files={"file": (outer.name, stream, "application/gzip")}, headers=auth)
    assert response.status_code == 422
    assert "非法路径" in response.json()["detail"]


def test_rejects_unsupported_executor(client, auth, tmp_path):
    bundle = build_bundle(tmp_path, name="ansible-package", executor_type="ansible")
    with bundle.open("rb") as stream:
        response = client.post("/api/v1/packages", files={"file": (bundle.name, stream, "application/gzip")}, headers=auth)
    assert response.status_code == 422
    assert "Executor" in response.json()["detail"]
