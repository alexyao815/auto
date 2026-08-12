"""维护包流式接收、安全校验、Revision 更新和文件生命周期。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tarfile
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

import yaml
from fastapi import HTTPException, UploadFile, status
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from .config import Settings
from .models import Package, PackageStep, Task, TaskNode, utcnow


CHECKSUM_RE = re.compile(r"^([0-9a-fA-F]{64})  inner-package\.tar\.gz\n?$")


@dataclass(slots=True)
class ValidatedBundle:
    """校验成功但尚未提交数据库的临时维护包。"""

    temp_root: Path
    inner_path: Path
    sha256: str
    manifest: dict
    steps: list[dict]

    def cleanup(self) -> None:
        """幂等删除本次校验产生的临时目录。"""

        shutil.rmtree(self.temp_root, ignore_errors=True)


def _safe_member_path(name: str) -> PurePosixPath:
    """把 Tar 成员限制在包内 POSIX 相对路径，拒绝目录逃逸。"""

    path = PurePosixPath(name.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise HTTPException(status_code=422, detail=f"压缩包包含非法路径: {name}")
    return path


def _validate_members(archive: tarfile.TarFile, settings: Settings, max_size: int | None = None) -> list[tarfile.TarInfo]:
    """在解压前检查成员数量、累计大小、类型和重复路径。"""

    members = archive.getmembers()
    if len(members) > settings.max_archive_files:
        raise HTTPException(status_code=422, detail="压缩包文件数量超过限制")
    total = 0
    seen_paths: set[str] = set()
    for member in members:
        normalized = str(_safe_member_path(member.name))
        if normalized in seen_paths:
            raise HTTPException(status_code=422, detail=f"压缩包包含重复路径: {member.name}")
        seen_paths.add(normalized)
        if member.issym() or member.islnk() or member.isdev() or member.isfifo():
            raise HTTPException(status_code=422, detail=f"压缩包包含不允许的链接或设备文件: {member.name}")
        if not (member.isfile() or member.isdir()):
            raise HTTPException(status_code=422, detail=f"压缩包包含不支持的成员: {member.name}")
        total += member.size
        if total > (max_size or settings.max_extracted_size):
            raise HTTPException(status_code=422, detail="压缩包解压后总大小超过限制")
    return members


def _find_one(members: list[tarfile.TarInfo], basename: str) -> tarfile.TarInfo:
    """按 basename 查找唯一协议文件，允许外层增加无语义目录前缀。"""

    matches = [member for member in members if member.isfile() and PurePosixPath(member.name).name == basename]
    if len(matches) != 1:
        raise HTTPException(status_code=422, detail=f"外层包必须且只能包含一个 {basename}")
    return matches[0]


def _copy_member(archive: tarfile.TarFile, member: tarfile.TarInfo, target: Path) -> None:
    """以固定缓冲区复制单个 Tar 成员，避免把整个成员读入内存。"""

    source = archive.extractfile(member)
    if source is None:
        raise HTTPException(status_code=422, detail=f"无法读取 {member.name}")
    with source, target.open("wb") as output:
        shutil.copyfileobj(source, output, length=1024 * 1024)


def _validate_manifest(manifest: object, inner_members: list[tarfile.TarInfo], settings: Settings) -> tuple[dict, list[dict]]:
    """验证 Manifest V1 和 Step，并返回可直接持久化的规范化结果。"""

    if not isinstance(manifest, dict):
        raise HTTPException(status_code=422, detail="manifest.yaml 顶层必须是对象")
    if manifest.get("manifest_version") != 1:
        raise HTTPException(status_code=422, detail="manifest_version 必须为 1")
    name = manifest.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 255:
        raise HTTPException(status_code=422, detail="manifest.name 必须是非空字符串且不超过 255 字符")
    raw_steps = manifest.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps or len(raw_steps) > settings.max_steps:
        raise HTTPException(status_code=422, detail=f"steps 数量必须为 1 到 {settings.max_steps}")
    file_names = {str(PurePosixPath(member.name)) for member in inner_members if member.isfile()}
    base_prefixes = {str(PurePosixPath(member.name).parent) for member in inner_members if PurePosixPath(member.name).name == "manifest.yaml"}
    manifest_prefix = next(iter(base_prefixes), ".")
    seen: set[str] = set()
    steps: list[dict] = []
    for index, raw in enumerate(raw_steps, start=1):
        if not isinstance(raw, dict):
            raise HTTPException(status_code=422, detail=f"Step {index} 必须是对象")
        step_name = raw.get("name")
        if not isinstance(step_name, str) or not step_name.strip() or step_name in seen:
            raise HTTPException(status_code=422, detail="Step name 必须非空且唯一")
        seen.add(step_name)
        executor_type = raw.get("type")
        if executor_type not in {"shell", "python"}:
            raise HTTPException(status_code=422, detail=f"不支持的 Executor 类型: {executor_type}")
        script = raw.get("script")
        if not isinstance(script, str):
            raise HTTPException(status_code=422, detail=f"Step {step_name} 缺少 script")
        script_path = _safe_member_path(script)
        full_script = str(script_path if manifest_prefix == "." else PurePosixPath(manifest_prefix) / script_path)
        if full_script not in file_names:
            raise HTTPException(status_code=422, detail=f"脚本文件不存在: {script}")
        timeout_value = raw.get("timeout", settings.default_step_timeout)
        if not isinstance(timeout_value, int) or isinstance(timeout_value, bool) or not 1 <= timeout_value <= 86400:
            raise HTTPException(status_code=422, detail=f"Step {step_name} timeout 必须为 1-86400 秒")
        failure_action = raw.get("failure_action", "stop")
        if failure_action not in {"stop", "ignore"}:
            raise HTTPException(status_code=422, detail=f"Step {step_name} failure_action 非法")
        steps.append({
            "sequence": index,
            "name": step_name,
            "executor_type": executor_type,
            "script": script,
            "timeout": timeout_value,
            "failure_action": failure_action,
        })
    for field in ("target_roles", "applicable_versions"):
        value = manifest.get(field, [])
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise HTTPException(status_code=422, detail=f"{field} 必须为字符串数组")
    return manifest, steps


def validate_bundle(path: Path, settings: Settings) -> ValidatedBundle:
    """校验双层包、SHA 和 Manifest；失败时不留下临时文件。"""

    # 所有不可信内容先进入独立临时目录，校验成功前不会触碰当前 Revision。
    temp_root = Path(tempfile.mkdtemp(prefix="bundle-", dir=settings.temp_dir))
    inner_path = temp_root / "inner-package.tar.gz"
    checksum_path = temp_root / "inner-package.sha256"
    try:
        with tarfile.open(path, "r:*") as outer:
            outer_members = _validate_members(outer, settings, settings.max_upload_size)
            inner_member = _find_one(outer_members, "inner-package.tar.gz")
            checksum_member = _find_one(outer_members, "inner-package.sha256")
            if checksum_member.size > 4096:
                raise HTTPException(status_code=422, detail="SHA256 文件过大")
            _copy_member(outer, inner_member, inner_path)
            _copy_member(outer, checksum_member, checksum_path)
        checksum_text = checksum_path.read_text(encoding="ascii")
        match = CHECKSUM_RE.fullmatch(checksum_text)
        if not match:
            raise HTTPException(status_code=422, detail="inner-package.sha256 格式非法")
        digest = hashlib.sha256()
        with inner_path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        actual_sha = digest.hexdigest()
        if actual_sha.lower() != match.group(1).lower():
            raise HTTPException(status_code=422, detail="内层包 SHA256 校验失败")
        with tarfile.open(inner_path, "r:*") as inner:
            inner_members = _validate_members(inner, settings)
            manifests = [member for member in inner_members if member.isfile() and PurePosixPath(member.name).name == "manifest.yaml"]
            if len(manifests) != 1:
                raise HTTPException(status_code=422, detail="内层包必须且只能包含一个 manifest.yaml")
            manifest_member = manifests[0]
            if manifest_member.size > settings.max_manifest_size:
                raise HTTPException(status_code=422, detail="manifest.yaml 超过大小限制")
            stream = inner.extractfile(manifest_member)
            if stream is None:
                raise HTTPException(status_code=422, detail="manifest.yaml 无法读取")
            raw_manifest = stream.read(settings.max_manifest_size + 1)
            try:
                parsed = yaml.safe_load(raw_manifest.decode("utf-8"))
            except (UnicodeDecodeError, yaml.YAMLError) as exc:
                raise HTTPException(status_code=422, detail=f"manifest.yaml 解析失败: {exc}") from exc
            manifest, steps = _validate_manifest(parsed, inner_members, settings)
        return ValidatedBundle(temp_root=temp_root, inner_path=inner_path, sha256=actual_sha, manifest=manifest, steps=steps)
    except Exception:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise


def stream_upload(upload: UploadFile, settings: Settings) -> Path:
    """按 1 MiB 块把 UploadFile 写盘，并在越限或异常时删除半成品。"""

    fd, name = tempfile.mkstemp(prefix="upload-", suffix=".bundle.tar.gz", dir=settings.temp_dir)
    os.close(fd)
    target = Path(name)
    total = 0
    try:
        with target.open("wb") as output:
            while chunk := upload.file.read(1024 * 1024):
                total += len(chunk)
                if total > settings.max_upload_size:
                    raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="上传文件超过 10 GiB 上限")
                output.write(chunk)
        if total == 0:
            raise HTTPException(status_code=422, detail="上传文件为空")
        return target
    except Exception:
        target.unlink(missing_ok=True)
        raise


def _apply_metadata(package: Package, validated: ValidatedBundle) -> None:
    """把 Manifest 顶层元数据复制到当前 Package 记录。"""

    manifest = validated.manifest
    package.description = str(manifest.get("description", ""))
    package.component = str(manifest.get("component", ""))
    package.bug_id = str(manifest.get("bug_id", ""))
    package.target_roles_json = json.dumps(manifest.get("target_roles", []), ensure_ascii=False)
    package.applicable_versions_json = json.dumps(manifest.get("applicable_versions", []), ensure_ascii=False)
    package.sha256 = validated.sha256
    package.manifest_json = json.dumps(manifest, ensure_ascii=False)


def _replace_steps(package: Package, validated: ValidatedBundle, session: Session | None = None) -> None:
    """用校验后的 Step 集合替换当前 Revision 定义。"""

    package.steps.clear()
    if session is not None:
        session.flush()
    for step in validated.steps:
        package.steps.append(PackageStep(**step))


def create_package(session: Session, settings: Settings, upload: UploadFile) -> Package:
    """创建 Package v1；文件和数据库任一失败都会回滚新文件。"""

    upload_path = stream_upload(upload, settings)
    validated: ValidatedBundle | None = None
    final_path: Path | None = None
    try:
        validated = validate_bundle(upload_path, settings)
        name = validated.manifest["name"].strip()
        if session.scalar(select(Package).where(Package.name == name)):
            raise HTTPException(status_code=409, detail="同名 Package 已存在，请使用 Update")
        package_id = str(uuid.uuid4())
        final_path = settings.package_dir / package_id / "v1" / "inner-package.tar.gz"
        final_path.parent.mkdir(parents=True, exist_ok=True)
        # 同一文件系统内原子移动，数据库提交前不会暴露部分写入文件。
        os.replace(validated.inner_path, final_path)
        package = Package(id=package_id, name=name, revision=1, storage_path=str(final_path), sha256="", manifest_json="{}")
        _apply_metadata(package, validated)
        _replace_steps(package, validated)
        session.add(package)
        session.commit()
        session.refresh(package)
        return package
    except Exception:
        session.rollback()
        if final_path:
            final_path.unlink(missing_ok=True)
        raise
    finally:
        upload_path.unlink(missing_ok=True)
        if validated:
            validated.cleanup()


def _has_active_tasks(session: Session, package: Package) -> bool:
    """判断当前 Revision 是否仍被 Waiting/Running TaskNode 使用。"""

    statement = (
        select(exists().where(
            Task.id == TaskNode.task_id,
            Task.package_id == package.id,
            Task.package_revision_snapshot == package.revision,
            TaskNode.status.in_(["WAITING", "RUNNING"]),
        ))
    )
    return bool(session.scalar(statement))


def update_package(session: Session, settings: Settings, package: Package, upload: UploadFile) -> Package:
    """校验新包后切换 Revision；活动引用存在时保持旧版本不变。"""

    if _has_active_tasks(session, package):
        raise HTTPException(status_code=409, detail="Package 存在 Waiting/Running 任务，禁止更新")
    upload_path = stream_upload(upload, settings)
    validated: ValidatedBundle | None = None
    new_path: Path | None = None
    old_path = Path(package.storage_path)
    try:
        validated = validate_bundle(upload_path, settings)
        if validated.manifest["name"].strip() != package.name:
            raise HTTPException(status_code=422, detail="Update 包的逻辑名称必须与原 Package 一致")
        new_revision = package.revision + 1
        new_path = settings.package_dir / package.id / f"v{new_revision}" / "inner-package.tar.gz"
        new_path.parent.mkdir(parents=True, exist_ok=True)
        # 新 Revision 使用独立目录；提交失败时删除新文件，旧 Revision 仍可用。
        os.replace(validated.inner_path, new_path)
        package.revision = new_revision
        package.storage_path = str(new_path)
        package.updated_at = utcnow()
        _apply_metadata(package, validated)
        _replace_steps(package, validated, session)
        session.commit()
        old_path.unlink(missing_ok=True)
        try:
            old_path.parent.rmdir()
        except OSError:
            pass
        return package
    except Exception:
        session.rollback()
        if new_path:
            new_path.unlink(missing_ok=True)
        raise
    finally:
        upload_path.unlink(missing_ok=True)
        if validated:
            validated.cleanup()


def delete_package(session: Session, package: Package) -> None:
    """删除当前 Package 文件和对象，依靠 Task 快照保留历史可读性。"""

    if _has_active_tasks(session, package):
        raise HTTPException(status_code=409, detail="Package 存在 Waiting/Running 任务，禁止删除")
    storage = Path(package.storage_path)
    session.delete(package)
    session.commit()
    storage.unlink(missing_ok=True)
    try:
        shutil.rmtree(storage.parents[1])
    except OSError:
        pass
