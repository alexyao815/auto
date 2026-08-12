"""验证真实 Salt 验收包仍服从生产上传协议和预期场景。"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
import tarfile
from pathlib import Path

from automation_center.package_service import validate_bundle


def _load_builder():
    """直接加载仓库脚本，避免把开发工具并入生产 Python 包。"""

    script = Path(__file__).resolve().parents[2] / "scripts" / "build_real_salt_acceptance_packages.py"
    spec = importlib.util.spec_from_file_location("build_real_salt_acceptance_packages", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclass 会通过 sys.modules 解析延迟注解，动态执行前必须先注册模块。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_real_salt_acceptance_packages_match_upload_contract(settings, tmp_path):
    """四种场景均使用精确外层文件名、标准 SHA 和合法 Manifest。"""

    builder = _load_builder()
    outputs = builder.build_acceptance_packages(tmp_path / "packages", "target-lab")
    assert [path.name for path in outputs] == [
        "target-lab-success.bundle.tar.gz",
        "target-lab-retry-once.bundle.tar.gz",
        "target-lab-long-running.bundle.tar.gz",
        "target-lab-timeout.bundle.tar.gz",
    ]

    expected_steps = {
        "target-lab-success": ["shell", "python"],
        "target-lab-retry-once": ["shell"],
        "target-lab-long-running": ["shell"],
        "target-lab-timeout": ["shell"],
    }
    settings.ensure_directories()
    for output in outputs:
        with tarfile.open(output, "r:gz") as outer:
            assert [member.name for member in outer.getmembers()] == [
                "inner-package.tar.gz",
                "inner-package.sha256",
            ]
            inner = outer.extractfile("inner-package.tar.gz")
            checksum = outer.extractfile("inner-package.sha256")
            assert inner and checksum
            inner_bytes = inner.read()
            assert checksum.read().decode("ascii") == (
                f"{hashlib.sha256(inner_bytes).hexdigest()}  inner-package.tar.gz\n"
            )

        validated = validate_bundle(output, settings)
        try:
            name = validated.manifest["name"]
            assert validated.manifest["manifest_version"] == 1
            assert validated.manifest["target_roles"] == ["compute"]
            assert [step["executor_type"] for step in validated.steps] == expected_steps[name]
        finally:
            validated.cleanup()


def test_acceptance_scripts_encode_bounded_failure_scenarios(tmp_path):
    """确认 Retry、长任务和 Timeout 的关键控制语义未被误改。"""

    builder = _load_builder()
    outputs = builder.build_acceptance_packages(tmp_path, "bounded")
    scripts: dict[str, str] = {}
    for output in outputs:
        with tarfile.open(output, "r:gz") as outer:
            inner_member = outer.extractfile("inner-package.tar.gz")
            assert inner_member
            with tarfile.open(fileobj=inner_member, mode="r:gz") as inner:
                for member in inner.getmembers():
                    if member.name.startswith("scripts/"):
                        stream = inner.extractfile(member)
                        assert stream
                        scripts[member.name] = stream.read().decode("utf-8")

    assert "exit 42" in scripts["scripts/retry-once.sh"]
    assert "rm -f" in scripts["scripts/retry-once.sh"]
    assert "sleep 2" in scripts["scripts/long-running.sh"]
    assert "sleep 30" in scripts["scripts/timeout.sh"]
