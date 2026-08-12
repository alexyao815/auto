"""生成只应投放到确认测试 Minion 的真实 Salt 分层验收维护包。"""

from __future__ import annotations

import argparse
import hashlib
import io
import re
import tarfile
from dataclasses import dataclass
from pathlib import Path


DEFAULT_OUTPUT_DIR = Path("data/real-salt-acceptance")
DEFAULT_PREFIX = "real-salt-acceptance"
SAFE_PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


@dataclass(frozen=True, slots=True)
class PackageDefinition:
    """描述一个验收包的固定场景、Step 和脚本文件。"""

    suffix: str
    description: str
    steps: tuple[tuple[str, str, str, int], ...]
    files: dict[str, bytes]


DEFINITIONS = (
    PackageDefinition(
        suffix="success",
        description="真实 Salt Shell/Python 成功闭环",
        steps=(
            ("shell-success", "shell", "scripts/success.sh", 60),
            ("python-success", "python", "scripts/success.py", 60),
        ),
        files={
            "scripts/success.sh": b"#!/bin/bash\nset -eu\nprintf 'REAL_SALT_SHELL_SUCCESS\\n'\n",
            "scripts/success.py": b"#!/usr/bin/env python3\nprint('REAL_SALT_PYTHON_SUCCESS')\n",
        },
    ),
    PackageDefinition(
        suffix="retry-once",
        description="首次退出 42，Retry 后成功并清理专用标记",
        steps=(("retry-once", "shell", "scripts/retry-once.sh", 60),),
        files={
            "scripts/retry-once.sh": (
                b"#!/bin/bash\nset -eu\n"
                b"marker=/var/lib/automation-center/acceptance/retry-once.marker\n"
                b"mkdir -p \"$(dirname \"$marker\")\"\n"
                b"if [ ! -e \"$marker\" ]; then\n"
                b"  date -u +%FT%TZ > \"$marker\"\n"
                b"  printf 'REAL_SALT_RETRY_FIRST_FAILURE\\n' >&2\n"
                b"  exit 42\n"
                b"fi\n"
                b"rm -f \"$marker\"\n"
                b"printf 'REAL_SALT_RETRY_SUCCESS\\n'\n"
            )
        },
    ),
    PackageDefinition(
        suffix="long-running",
        description="持续输出 90 秒，用于 SSE 和 JID 恢复",
        steps=(("long-running", "shell", "scripts/long-running.sh", 180),),
        files={
            "scripts/long-running.sh": (
                b"#!/bin/bash\nset -eu\n"
                b"i=1\nwhile [ \"$i\" -le 45 ]; do\n"
                b"  printf 'REAL_SALT_LONG_RUNNING tick=%s\\n' \"$i\"\n"
                b"  i=$((i + 1))\n  sleep 2\ndone\n"
            )
        },
    ),
    PackageDefinition(
        suffix="timeout",
        description="超过 5 秒 Step Timeout 的受控超时场景",
        steps=(("timeout", "shell", "scripts/timeout.sh", 5),),
        files={
            "scripts/timeout.sh": (
                b"#!/bin/bash\nset -eu\n"
                b"printf 'REAL_SALT_TIMEOUT_STARTED\\n'\n"
                b"sleep 30\n"
                b"printf 'REAL_SALT_TIMEOUT_UNEXPECTED_COMPLETION\\n'\n"
            )
        },
    ),
)


def _add_bytes(archive: tarfile.TarFile, name: str, data: bytes, mode: int = 0o644) -> None:
    """使用固定权限与时间写入成员，避免泄露构建机元数据。"""

    member = tarfile.TarInfo(name)
    member.size = len(data)
    member.mode = mode
    member.mtime = 0
    archive.addfile(member, io.BytesIO(data))


def _manifest(package_name: str, definition: PackageDefinition) -> bytes:
    """生成 Validator 可接受的 Manifest V1。"""

    lines = [
        "manifest_version: 1",
        f"name: {package_name}",
        f"description: {definition.description}",
        "component: acceptance",
        f"bug_id: ACCEPTANCE-{definition.suffix.upper()}",
        "target_roles: [compute]",
        "applicable_versions: [real-salt]",
        "steps:",
    ]
    for name, executor, script, timeout in definition.steps:
        lines.extend(
            (
                f"  - name: {name}",
                f"    type: {executor}",
                f"    script: {script}",
                f"    timeout: {timeout}",
                "    failure_action: stop",
            )
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def build_acceptance_packages(output_dir: Path, name_prefix: str = DEFAULT_PREFIX) -> list[Path]:
    """生成四个标准双层验收包并返回其绝对路径。"""

    if not SAFE_PREFIX_RE.fullmatch(name_prefix):
        raise ValueError("name-prefix 只能包含字母、数字、点、下划线和连字符，且不超过 96 字符")
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    for definition in DEFINITIONS:
        package_name = f"{name_prefix}-{definition.suffix}"
        inner_buffer = io.BytesIO()
        with tarfile.open(fileobj=inner_buffer, mode="w:gz") as inner:
            _add_bytes(inner, "manifest.yaml", _manifest(package_name, definition))
            for name, content in definition.files.items():
                _add_bytes(inner, name, content, 0o755)
        inner_bytes = inner_buffer.getvalue()
        checksum = f"{hashlib.sha256(inner_bytes).hexdigest()}  inner-package.tar.gz\n".encode("ascii")
        output = output_dir / f"{package_name}.bundle.tar.gz"
        with tarfile.open(output, "w:gz") as outer:
            _add_bytes(outer, "inner-package.tar.gz", inner_bytes)
            _add_bytes(outer, "inner-package.sha256", checksum)
        outputs.append(output)
    return outputs


def main() -> None:
    """解析命令行参数并打印生成路径，不读取或连接任何 Salt 环境。"""

    parser = argparse.ArgumentParser(description="生成 Automation Center 真实 Salt 验收包")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--name-prefix", default=DEFAULT_PREFIX)
    arguments = parser.parse_args()
    try:
        outputs = build_acceptance_packages(arguments.output_dir, arguments.name_prefix)
    except ValueError as exc:
        parser.error(str(exc))
    for output in outputs:
        print(output)


if __name__ == "__main__":
    main()
