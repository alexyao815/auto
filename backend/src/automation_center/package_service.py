"""维护包流式接收、安全校验、Revision 更新和文件生命周期。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
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


CHECKSUM_RE = re.compile(r"^([0-9a-fA-F]{64})  ([^\r\n]+)\r?\n?$")
SUPPORTED_INNER_SUFFIXES = (".tar", ".tar.gz")


@dataclass(slots=True)
class ValidatedBundle:
    """校验成功但尚未提交数据库的临时维护包。"""

    temp_root: Path
    inner_path: Path
    archive_name: str
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


def _identify_outer_files(members: list[tarfile.TarInfo]) -> tuple[tarfile.TarInfo, tarfile.TarInfo]:
    """从外层恰好两个普通文件中找出 SHA 文本和被校验归档。

    目录项不计入“两个文件”；文件名不固定，但 SHA 文本必须以 ``sha256``
    结尾，另一个文件必须是 ``.tar`` 或 ``.tar.gz``。实际对应关系稍后以
    SHA 文本中记录的 basename 为准。
    """

    files = [member for member in members if member.isfile()]
    if len(files) != 2:
        raise HTTPException(status_code=422, detail="外层包必须且只能包含两个普通文件：一个内层 Tar 包和一个 SHA256 文件")
    # 与部署机进入解压目录执行 ``ls *sha256`` 的识别规则一致，不固定前缀。
    checksum_files = [member for member in files if PurePosixPath(member.name).name.endswith("sha256")]
    if len(checksum_files) != 1:
        raise HTTPException(status_code=422, detail="外层包必须且只能包含一个文件名以 sha256 结尾的校验文件")
    checksum_member = checksum_files[0]
    archive_member = next(member for member in files if member is not checksum_member)
    archive_name = PurePosixPath(archive_member.name).name
    if not archive_name.lower().endswith(SUPPORTED_INNER_SUFFIXES):
        raise HTTPException(status_code=422, detail="SHA256 对应的内层包格式只支持 .tar 或 .tar.gz")
    return archive_member, checksum_member


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
    """按 SHA 文本动态识别内层 Tar 包并校验 Manifest；失败不留临时文件。"""

    # 所有不可信内容先进入独立临时目录，校验成功前不会触碰当前 Revision。
    temp_root = Path(tempfile.mkdtemp(prefix="bundle-", dir=settings.temp_dir))
    inner_path: Path | None = None
    checksum_path: Path | None = None
    try:
        with tarfile.open(path, "r:*") as outer:
            outer_members = _validate_members(outer, settings, settings.max_upload_size)
            inner_member, checksum_member = _identify_outer_files(outer_members)
            if checksum_member.size > 4096:
                raise HTTPException(status_code=422, detail="SHA256 文件过大")
            checksum_name = PurePosixPath(checksum_member.name).name
            checksum_path = temp_root / checksum_name
            _copy_member(outer, checksum_member, checksum_path)
            archive_name = PurePosixPath(inner_member.name).name
            inner_path = temp_root / archive_name
            _copy_member(outer, inner_member, inner_path)
        try:
            checksum_text = checksum_path.read_text(encoding="ascii")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=422, detail="SHA256 文件必须是 ASCII 文本") from exc
        match = CHECKSUM_RE.fullmatch(checksum_text)
        if not match:
            raise HTTPException(status_code=422, detail="SHA256 文件格式非法，必须为：64位摘要、两个空格、内层包文件名")
        recorded_name = match.group(2)
        if PurePosixPath(recorded_name).name != recorded_name or recorded_name != archive_name:
            raise HTTPException(status_code=422, detail=f"SHA256 文件记录的包名与实际内层包不一致: {recorded_name} != {archive_name}")
        # 等价于进入临时目录执行 ``sha256sum -c <实际找到的 *sha256 文件>``。
        # shell=False 防止上传文件名被解释成命令；``--`` 阻止以连字符开头的名字
        # 被 sha256sum 当成选项。Windows 开发环境没有 sha256sum 时用同算法回退。
        sha256sum = shutil.which("sha256sum")
        if sha256sum:
            checked = subprocess.run(
                [sha256sum, "-c", "--", checksum_path.name],
                cwd=temp_root,
                capture_output=True,
                text=True,
                check=False,
            )
            checksum_ok = checked.returncode == 0
        else:
            digest = hashlib.sha256()
            with inner_path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            checksum_ok = digest.hexdigest().lower() == match.group(1).lower()
        if not checksum_ok:
            raise HTTPException(status_code=422, detail="内层包 SHA256 校验失败")
        actual_sha = match.group(1).lower()
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
        return ValidatedBundle(
            temp_root=temp_root,
            inner_path=inner_path,
            archive_name=archive_name,
            sha256=actual_sha,
            manifest=manifest,
            steps=steps,
        )
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
        final_path = settings.package_dir / package_id / "v1" / validated.archive_name
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
        new_path = settings.package_dir / package.id / f"v{new_revision}" / validated.archive_name
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
