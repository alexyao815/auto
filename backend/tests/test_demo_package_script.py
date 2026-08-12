"""验证新人演示包生成器与生产 Package Validator 使用同一上传契约。"""

from __future__ import annotations

import hashlib
import importlib.util
import tarfile
from pathlib import Path

from automation_center.package_service import validate_bundle


def _load_builder():
    """从仓库脚本目录加载开发工具，避免把 scripts 变成生产 Python 包。"""

    script = Path(__file__).resolve().parents[2] / "scripts" / "build_demo_package.py"
    spec = importlib.util.spec_from_file_location("build_demo_package", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_package_matches_upload_contract(settings, tmp_path):
    """同时检查外层协议、摘要格式、Manifest 和两类 Executor。"""

    builder = _load_builder()
    output = builder.build_demo_package(tmp_path / "demo.bundle.tar.gz", "newcomer-demo")

    with tarfile.open(output, "r:gz") as outer:
        assert {member.name for member in outer.getmembers()} == {
            "inner-package.tar.gz",
            "inner-package.sha256",
        }
        inner = outer.extractfile("inner-package.tar.gz")
        checksum_file = outer.extractfile("inner-package.sha256")
        assert inner and checksum_file
        inner_bytes = inner.read()
        assert checksum_file.read().decode("ascii") == (
            f"{hashlib.sha256(inner_bytes).hexdigest()}  inner-package.tar.gz\n"
        )

    settings.ensure_directories()
    validated = validate_bundle(output, settings)
    try:
        assert validated.manifest["manifest_version"] == 1
        assert validated.manifest["name"] == "newcomer-demo"
        assert [step["executor_type"] for step in validated.steps] == ["shell", "python"]
    finally:
        validated.cleanup()
