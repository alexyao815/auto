"""生成可供本地 Fake Salt 上手流程上传的安全演示维护包。"""

from __future__ import annotations

import argparse
import hashlib
import io
import re
import tarfile
import tempfile
from pathlib import Path


DEFAULT_OUTPUT = Path("data/onboarding-demo.bundle.tar.gz")
DEFAULT_NAME = "onboarding-demo"
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _add_bytes(archive: tarfile.TarFile, name: str, data: bytes, mode: int = 0o644) -> None:
    """以固定权限写入内存文件，避免把开发机路径和元数据带入维护包。"""

    member = tarfile.TarInfo(name)
    member.size = len(data)
    member.mode = mode
    member.mtime = 0
    archive.addfile(member, io.BytesIO(data))


def build_demo_package(output: Path, package_name: str = DEFAULT_NAME) -> Path:
    """构造包含 Shell/Python 两个成功 Step 的标准双层维护包。"""

    if not SAFE_NAME_RE.fullmatch(package_name):
        raise ValueError("Package name 只能包含字母、数字、点、下划线和连字符，且不超过 128 字符")

    manifest = f"""manifest_version: 1
name: {package_name}
description: 后端新人上手使用的安全演示包
component: onboarding
bug_id: DEMO-001
target_roles: [compute]
applicable_versions: [development]
steps:
  - name: shell-hello
    type: shell
    script: scripts/hello.sh
    timeout: 60
    failure_action: stop
  - name: python-hello
    type: python
    script: scripts/hello.py
    timeout: 60
    failure_action: stop
""".encode("utf-8")
    shell_script = b"#!/bin/bash\nset -eu\nprintf 'onboarding shell step succeeded\\n'\n"
    python_script = b"#!/usr/bin/env python3\nprint('onboarding python step succeeded')\n"

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="automation-center-demo-") as temporary:
        inner_path = Path(temporary) / "inner-package.tar.gz"
        with tarfile.open(inner_path, "w:gz") as inner:
            _add_bytes(inner, "manifest.yaml", manifest)
            _add_bytes(inner, "scripts/hello.sh", shell_script, 0o755)
            _add_bytes(inner, "scripts/hello.py", python_script, 0o755)

        inner_bytes = inner_path.read_bytes()
        checksum = f"{hashlib.sha256(inner_bytes).hexdigest()}  inner-package.tar.gz\n".encode("ascii")
        with tarfile.open(output, "w:gz") as outer:
            # 外层名称是服务端协议的一部分，不能替换为开发机上的临时文件名。
            _add_bytes(outer, "inner-package.tar.gz", inner_bytes)
            _add_bytes(outer, "inner-package.sha256", checksum)
    return output


def main() -> None:
    """解析开发者 CLI 参数并输出生成文件的绝对路径。"""

    parser = argparse.ArgumentParser(description="生成 Automation Center V1 演示维护包")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="外层包输出路径")
    parser.add_argument("--name", default=DEFAULT_NAME, help="manifest 中的 Package name")
    arguments = parser.parse_args()
    try:
        path = build_demo_package(arguments.output, arguments.name)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"演示维护包已生成: {path}")


if __name__ == "__main__":
    main()
