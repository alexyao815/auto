from __future__ import annotations

import hashlib
import io
import tarfile

import pytest


def outer_with_inner(tmp_path, name, inner_bytes, checksum_name="inner-package.tar.gz", checksum=None):
    digest = checksum or hashlib.sha256(inner_bytes).hexdigest()
    outer = tmp_path / f"{name}.tar.gz"
    line = f"{digest}  {checksum_name}\n".encode()
    with tarfile.open(outer, "w:gz") as archive:
        info = tarfile.TarInfo("inner-package.tar.gz"); info.size=len(inner_bytes); archive.addfile(info, io.BytesIO(inner_bytes))
        info = tarfile.TarInfo("inner-package.sha256"); info.size=len(line); archive.addfile(info, io.BytesIO(line))
    return outer


def inner_archive(tmp_path, name, manifest: bytes, files=None, special=None):
    """按需注入符号链接成员，专门覆盖解压前的 Tar 类型拒绝分支。"""

    path = tmp_path / f"{name}-inner.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        info=tarfile.TarInfo("manifest.yaml"); info.size=len(manifest); archive.addfile(info, io.BytesIO(manifest))
        for filename,data in (files or {}).items():
            info=tarfile.TarInfo(filename);info.size=len(data);archive.addfile(info,io.BytesIO(data))
        if special:
            info=tarfile.TarInfo(special);info.type=tarfile.SYMTYPE;info.linkname="/etc/passwd";archive.addfile(info)
    return path.read_bytes()


@pytest.mark.parametrize(
    "manifest, expected",
    [
        (b"- not-an-object\n", "顶层"),
        (b"manifest_version: 1\nname: ''\nsteps: []\n", "name"),
        (b"manifest_version: 1\nname: x\nsteps: []\n", "steps"),
        (b"manifest_version: 1\nname: x\nsteps:\n - name: a\n   type: shell\n   script: missing.sh\n", "不存在"),
        (b"manifest_version: 1\nname: x\nsteps:\n - name: a\n   type: shell\n   script: a.sh\n   timeout: 0\n", "timeout"),
        (b"manifest_version: 1\nname: x\nsteps:\n - name: a\n   type: shell\n   script: a.sh\n   failure_action: bad\n", "failure_action"),
        (b"manifest_version: 1\nname: x\ntarget_roles: bad\nsteps:\n - name: a\n   type: shell\n   script: a.sh\n", "target_roles"),
    ],
)
def test_manifest_validation_errors(client, auth, tmp_path, manifest, expected):
    inner = inner_archive(tmp_path, expected.replace('/', '_'), manifest, {"a.sh": b"echo ok"})
    outer = outer_with_inner(tmp_path, expected.replace('/', '_'), inner)
    with outer.open("rb") as stream:
        response = client.post("/api/v1/packages", files={"file": (outer.name, stream, "application/gzip")}, headers=auth)
    assert response.status_code == 422
    assert expected in response.json()["detail"]


def test_rejects_symlink_duplicate_members_and_bad_checksum_format(client, auth, tmp_path):
    manifest=b"manifest_version: 1\nname: symlink\nsteps:\n - name: a\n   type: shell\n   script: a.sh\n"
    inner=inner_archive(tmp_path,"symlink",manifest,{"a.sh":b"x"},special="link")
    outer=outer_with_inner(tmp_path,"symlink",inner)
    with outer.open("rb") as stream: response=client.post("/api/v1/packages",files={"file":(outer.name,stream,"application/gzip")},headers=auth)
    assert response.status_code==422 and "链接" in response.json()["detail"]

    bad=outer_with_inner(tmp_path,"bad-format",inner,checksum_name="wrong.tar.gz")
    with bad.open("rb") as stream: response=client.post("/api/v1/packages",files={"file":(bad.name,stream,"application/gzip")},headers=auth)
    assert response.status_code==422 and "格式" in response.json()["detail"]


def test_upload_empty_and_size_limits(client, auth, settings, tmp_path):
    response=client.post("/api/v1/packages",files={"file":("empty.tar.gz",io.BytesIO(b""),"application/gzip")},headers=auth)
    assert response.status_code==422
    settings.max_upload_size=4
    response=client.post("/api/v1/packages",files={"file":("large.tar.gz",io.BytesIO(b"12345"),"application/gzip")},headers=auth)
    assert response.status_code==413


@pytest.mark.parametrize("member_type", [tarfile.LNKTYPE, tarfile.CHRTYPE])
def test_rejects_hardlinks_and_device_members(client, auth, tmp_path, member_type):
    manifest = b"manifest_version: 1\nname: special\nsteps:\n - name: a\n   type: shell\n   script: a.sh\n"
    path = tmp_path / f"special-{member_type!r}-inner.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo("manifest.yaml"); info.size = len(manifest); archive.addfile(info, io.BytesIO(manifest))
        info = tarfile.TarInfo("a.sh"); info.size = 1; archive.addfile(info, io.BytesIO(b"x"))
        # TarInfo 允许无须在文件系统创建真实设备节点即可测试硬链接/设备类型。
        info = tarfile.TarInfo("special")
        info.type = member_type
        if member_type == tarfile.LNKTYPE:
            info.linkname = "a.sh"
        archive.addfile(info)
    outer = outer_with_inner(tmp_path, f"special-{int(member_type[0])}", path.read_bytes())
    with outer.open("rb") as stream:
        response = client.post("/api/v1/packages", files={"file": (outer.name, stream, "application/gzip")}, headers=auth)
    assert response.status_code == 422
    assert "链接或设备" in response.json()["detail"]


def test_rejects_duplicate_paths_and_all_archive_limits(client, auth, settings, tmp_path):
    manifest = b"manifest_version: 1\nname: duplicate\nsteps:\n - name: a\n   type: shell\n   script: a.sh\n"
    duplicate = tmp_path / "duplicate-inner.tar.gz"
    with tarfile.open(duplicate, "w:gz") as archive:
        for filename, data in [("manifest.yaml", manifest), ("a.sh", b"first"), ("a.sh", b"second")]:
            info = tarfile.TarInfo(filename); info.size = len(data); archive.addfile(info, io.BytesIO(data))
    outer = outer_with_inner(tmp_path, "duplicate", duplicate.read_bytes())
    with outer.open("rb") as stream:
        response = client.post("/api/v1/packages", files={"file": (outer.name, stream, "application/gzip")}, headers=auth)
    assert response.status_code == 422 and "重复路径" in response.json()["detail"]

    settings.max_manifest_size = 8
    normal_inner = inner_archive(tmp_path, "manifest-limit", manifest, {"a.sh": b"x"})
    normal_outer = outer_with_inner(tmp_path, "manifest-limit", normal_inner)
    with normal_outer.open("rb") as stream:
        response = client.post("/api/v1/packages", files={"file": (normal_outer.name, stream, "application/gzip")}, headers=auth)
    assert response.status_code == 422 and "manifest.yaml" in response.json()["detail"]


def test_rejects_more_than_configured_steps_and_members(client, auth, settings, tmp_path):
    settings.max_steps = 2
    steps = "".join(f" - name: s{i}\n   type: shell\n   script: s{i}.sh\n" for i in range(3))
    manifest = f"manifest_version: 1\nname: too-many-steps\nsteps:\n{steps}".encode()
    files = {f"s{i}.sh": b"x" for i in range(3)}
    inner = inner_archive(tmp_path, "too-many-steps", manifest, files)
    outer = outer_with_inner(tmp_path, "too-many-steps", inner)
    with outer.open("rb") as stream:
        response = client.post("/api/v1/packages", files={"file": (outer.name, stream, "application/gzip")}, headers=auth)
    assert response.status_code == 422 and "steps 数量" in response.json()["detail"]

    settings.max_steps = 100
    settings.max_archive_files = 1
    with outer.open("rb") as stream:
        response = client.post("/api/v1/packages", files={"file": (outer.name, stream, "application/gzip")}, headers=auth)
    assert response.status_code == 422 and "文件数量" in response.json()["detail"]
