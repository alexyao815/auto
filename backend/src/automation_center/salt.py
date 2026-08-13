"""Salt 执行契约，以及用于开发测试的 Fake 和生产 HTTP 两种实现。"""

from __future__ import annotations

import base64
import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .config import Settings


NODE_PROBE_TIMEOUT_SECONDS = 5
ROLE_DETECTION_TIMEOUT_SECONDS = 15
JOB_SUBMISSION_GRACE_SECONDS = 10
PROCESS_SNAPSHOT_COMMAND = "ps -eo comm=,args= --no-headers"


@dataclass(slots=True)
class SaltJobResult:
    """统一不同 Salt 返回形态后的异步 Job 状态。"""
    state: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    failure_reason: str | None = None


@dataclass(slots=True)
class SaltProcessSnapshot:
    """单个 Minion 的进程快照结果；原始文本只供内存匹配使用。"""

    state: str
    process_text: str = ""
    failure_reason: str | None = None


class SaltAdapter(Protocol):
    """Scheduler 依赖的最小 Salt 能力边界。

    Adapter 负责远端调用，不负责数据库事务；调用方必须安排事务边界，确保
    Salt 网络等待期间不长期占用 SQLite 写锁。
    """
    def pending_keys(self) -> list[str]: ...
    def accept_key(self, key_id: str) -> None: ...
    def reject_key(self, key_id: str) -> None: ...
    def accepted_keys(self) -> list[str]: ...
    def ping(self, node_id: str) -> bool: ...
    def ping_many(self, node_ids: list[str]) -> dict[str, bool]: ...
    def node_info(self, node_id: str) -> dict[str, Any]: ...
    def process_snapshot_many(self, node_ids: list[str]) -> dict[str, SaltProcessSnapshot]: ...
    def transfer_package(self, node_id: str, salt_source: str, target_path: str) -> None: ...
    def prepare_workdir(self, node_id: str, archive_path: str, workdir: str) -> None: ...
    def start_step(self, node_id: str, executor_type: str, script: str, workdir: str, stdout_path: str, stderr_path: str, exit_path: str) -> str: ...
    def job_result(self, jid: str, node_id: str) -> SaltJobResult: ...
    def read_file(self, node_id: str, path: str, offset: int) -> tuple[bytes, int]: ...
    def terminate_job(self, node_id: str, jid: str) -> str: ...
    def cleanup_workdir(self, node_id: str, workdir: str) -> None: ...


class FakeSaltAdapter:
    """供自动化测试和本地上手使用的确定性内存 Salt 实现。

    脚本名含 ``fail``、``timeout`` 或 ``lost`` 时分别模拟失败、超时和 JID
    丢失。它验证的是 Adapter 契约，不模拟真实 Salt 的认证、网络和权限问题。
    """

    def __init__(self) -> None:
        self._pending = {"demo-minion"}
        self._accepted: dict[str, dict[str, Any]] = {
            "demo-node": {"hostname": "demo-node", "management_ip": "192.0.2.10", "processes": ["nova-compute"]}
        }
        self._jobs: dict[str, dict[str, Any]] = {}
        self._files: dict[tuple[str, str], bytes] = {}
        self._lock = threading.Lock()
        self.cleaned_workdirs: list[tuple[str, str]] = []

    def add_pending(self, key_id: str) -> None:
        """向 Fake Key 列表加入一个等待接入的 Minion。"""
        self._pending.add(key_id)

    def pending_keys(self) -> list[str]:
        """返回排序后的 Fake pending Key。"""
        return sorted(self._pending)

    def accept_key(self, key_id: str) -> None:
        """把 Fake Key 从 pending 移入 accepted。"""
        if key_id not in self._pending:
            raise RuntimeError("Pending Key 不存在")
        self._pending.remove(key_id)
        self._accepted[key_id] = {"hostname": key_id, "management_ip": "192.0.2.20", "processes": []}

    def reject_key(self, key_id: str) -> None:
        """从 Fake pending 集合移除被拒绝的 Key。"""
        if key_id not in self._pending:
            raise RuntimeError("Pending Key 不存在")
        self._pending.remove(key_id)

    def accepted_keys(self) -> list[str]:
        """返回排序后的 Fake accepted Key。"""
        return sorted(self._accepted)

    def ping(self, node_id: str) -> bool:
        """根据 accepted 和 offline 标记模拟 test.ping。"""
        return node_id in self._accepted and not self._accepted[node_id].get("offline", False)

    def ping_many(self, node_ids: list[str]) -> dict[str, bool]:
        """一次返回多个 Fake Minion 的在线状态，模拟真实 Salt 批量 targeting。"""
        return {node_id: self.ping(node_id) for node_id in node_ids}

    def node_info(self, node_id: str) -> dict[str, Any]:
        """返回 Fake 节点属性的副本，避免调用方修改内部状态。"""
        if node_id not in self._accepted:
            raise RuntimeError("Minion 不存在")
        return dict(self._accepted[node_id])

    def process_snapshot_many(self, node_ids: list[str]) -> dict[str, SaltProcessSnapshot]:
        """用一次 Fake 调用返回多节点进程文本，并支持节点级失败测试。"""

        snapshots: dict[str, SaltProcessSnapshot] = {}
        for node_id in node_ids:
            node = self._accepted.get(node_id)
            if node is None or node.get("offline"):
                snapshots[node_id] = SaltProcessSnapshot(state="FAILED", failure_reason="NODE_NO_RESPONSE")
            elif node.get("process_error"):
                snapshots[node_id] = SaltProcessSnapshot(
                    state="FAILED",
                    failure_reason=str(node["process_error"]),
                )
            else:
                process_text = node.get("process_text")
                if process_text is None:
                    process_text = "\n".join(str(item) for item in node.get("processes", []))
                snapshots[node_id] = SaltProcessSnapshot(state="SUCCESS", process_text=str(process_text))
        return snapshots

    def transfer_package(self, node_id: str, salt_source: str, target_path: str) -> None:
        """模拟 Fileserver 下载，并记录目标路径供后续断言。"""
        if not self.ping(node_id):
            raise RuntimeError("PackageTransferFailed")
        self._files[(node_id, target_path)] = f"fake:{salt_source}".encode()

    def prepare_workdir(self, node_id: str, archive_path: str, workdir: str) -> None:
        """模拟远端解压前的在线检查。"""
        if not self.ping(node_id):
            raise RuntimeError("NodeOffline")

    def start_step(
        self,
        node_id: str,
        executor_type: str,
        script: str,
        workdir: str,
        stdout_path: str,
        stderr_path: str,
        exit_path: str,
    ) -> str:
        """提交一个可轮询的 Fake 异步任务并立即返回 JID。"""
        if not self.ping(node_id):
            raise RuntimeError("NodeOffline")
        jid = f"fake-{secrets.token_hex(8)}"
        fail = "fail" in script.lower()
        slow = "timeout" in script.lower()
        stdout = f"[{executor_type}] {script} on {node_id}\n"
        stderr = "simulated failure\n" if fail else ""
        self._files[(node_id, stdout_path)] = stdout.encode("utf-8")
        self._files[(node_id, stderr_path)] = stderr.encode("utf-8")
        with self._lock:
            self._jobs[jid] = {
                "ready_at": time.monotonic() + (3600 if slow else 0.05),
                "exit_code": 1 if fail else 0,
                "stdout": stdout,
                "stderr": stderr,
                "lost": "lost" in script.lower(),
            }
        return jid

    def job_result(self, jid: str, node_id: str) -> SaltJobResult:
        """按单调时钟收敛 Fake Job，未知或显式丢失的 JID 返回 LOST。"""
        job = self._jobs.get(jid)
        if job is None or job["lost"]:
            return SaltJobResult(state="LOST", failure_reason="EXECUTION_STATE_LOST")
        if time.monotonic() < job["ready_at"]:
            return SaltJobResult(state="RUNNING")
        code = int(job["exit_code"])
        return SaltJobResult(
            state="SUCCESS" if code == 0 else "FAILED",
            exit_code=code,
            stdout=job["stdout"],
            stderr=job["stderr"],
        )

    def read_file(self, node_id: str, path: str, offset: int) -> tuple[bytes, int]:
        """从指定字节偏移增量读取 Fake 远端文件。"""
        content = self._files.get((node_id, path), b"")
        return content[offset:], len(content)

    def terminate_job(self, node_id: str, jid: str) -> str:
        """模拟尽力终止；终止动作本身不改变调用方已落库的超时结论。"""
        job = self._jobs.get(jid)
        if job:
            job["ready_at"] = 0
            job["exit_code"] = 124
            job["stderr"] += "terminated after timeout\n"
        return "best-effort termination requested"

    def cleanup_workdir(self, node_id: str, workdir: str) -> None:
        """删除 Fake 远端工作目录下的文件并留下可测试的清理记录。"""
        if not self.ping(node_id):
            raise RuntimeError("NodeOffline")
        prefix = workdir.rstrip("/") + "/"
        for key in [key for key in self._files if key[0] == node_id and (key[1] == workdir or key[1].startswith(prefix))]:
            self._files.pop(key, None)
        self.cleaned_workdirs.append((node_id, workdir))


class HttpSaltAdapter:
    """通过 salt-api 实现 ``SaltAdapter`` 的同步 HTTP 客户端。

    执行 Step 使用 ``local_async`` 尽早取得并持久化 JID；日志通过受限远端
    Python 命令按 offset 拉取，避免每轮重复传输整个文件。
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._token = ""
        self._token_expire = 0.0
        self._submitted_at: dict[str, float] = {}
        self._submission_lock = threading.Lock()

    def _remember_submission(self, jid: str) -> None:
        """记录新 JID，并清除已超出 job-cache 宽限期的历史记录。

        该内存映射仅用于覆盖 Salt Job 刚提交但尚未进入 job cache 的短窗口，
        不是 Job 历史存储。每次提交都会淘汰超过十秒的条目，使占用量受近期
        提交速率约束，不会随服务运行天数增长。
        """

        now = time.monotonic()
        cutoff = now - JOB_SUBMISSION_GRACE_SECONDS
        with self._submission_lock:
            expired = [known_jid for known_jid, submitted_at in self._submitted_at.items() if submitted_at <= cutoff]
            for known_jid in expired:
                self._submitted_at.pop(known_jid, None)
            self._submitted_at[jid] = now

    def _submission_is_in_grace(self, jid: str) -> bool:
        """判断 JID 是否仍在短暂可见性宽限期，并顺手淘汰过期记录。"""

        now = time.monotonic()
        with self._submission_lock:
            submitted_at = self._submitted_at.get(jid)
            if submitted_at is None:
                return False
            if now - submitted_at < JOB_SUBMISSION_GRACE_SECONDS:
                return True
            self._submitted_at.pop(jid, None)
            return False

    def _forget_submission(self, jid: str) -> None:
        """Job 已进入终态或被终止后立即释放临时提交时间。"""

        with self._submission_lock:
            self._submitted_at.pop(jid, None)

    def _login(self, timeout_seconds: float | None = None) -> None:
        """使用外部认证登录并在真实到期时间前 30 秒刷新 Token。"""
        response = httpx.post(
            f"{self.settings.salt_api_url.rstrip('/')}/login",
            data={
                "username": self.settings.salt_api_username,
                "password": self.settings.salt_api_credential,
                "eauth": self.settings.salt_eauth,
            },
            headers={"Accept": "application/json"},
            timeout=timeout_seconds or self.settings.salt_request_timeout,
        )
        response.raise_for_status()
        payload = response.json()["return"][0]
        self._token = payload["token"]
        self._token_expire = float(payload.get("expire", time.time() + 600)) - 30

    def _request(
        self,
        data: dict[str, Any],
        retry_auth: bool = True,
        timeout_seconds: float | None = None,
        deadline: float | None = None,
    ) -> Any:
        """发送一个 Salt 请求；可选 deadline 覆盖登录、重认证和业务请求总时长。"""

        if deadline is None and timeout_seconds is not None:
            deadline = time.monotonic() + timeout_seconds

        def request_timeout() -> float:
            if deadline is None:
                return float(self.settings.salt_request_timeout)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise httpx.TimeoutException("Salt request deadline exceeded")
            return remaining

        if not self._token or time.time() >= self._token_expire:
            self._login(request_timeout())
        response = httpx.post(
            self.settings.salt_api_url,
            data=data,
            headers={"Accept": "application/json", "X-Auth-Token": self._token},
            timeout=request_timeout(),
        )
        if response.status_code == 401 and retry_auth:
            self._login(request_timeout())
            return self._request(
                data,
                retry_auth=False,
                timeout_seconds=timeout_seconds,
                deadline=deadline,
            )
        response.raise_for_status()
        return response.json().get("return", [None])[0]

    def _local(self, node_id: str, fun: str, arg: list[str] | None = None, kwarg: dict[str, Any] | None = None) -> Any:
        """执行同步 local client 调用并提取当前 Minion 的返回值。"""
        data: dict[str, Any] = {"client": "local", "tgt": node_id, "fun": fun}
        if arg:
            data["arg"] = arg
        if kwarg:
            data["kwarg"] = json.dumps(kwarg)
        result = self._request(data)
        return result.get(node_id) if isinstance(result, dict) else result

    def _wheel(self, fun: str, **kwargs: Any) -> Any:
        """执行仅用于 Key 管理的 wheel client 调用。"""
        return self._request({"client": "wheel", "fun": fun, **kwargs})

    def pending_keys(self) -> list[str]:
        """通过 wheel 查询尚未接受的 Minion Key。"""
        result = self._wheel("key.list_all") or {}
        data = result.get("data", {}).get("return", result.get("return", result))
        return sorted(data.get("minions_pre", []))

    def accept_key(self, key_id: str) -> None:
        """通过受限 wheel 权限接受一个 Minion Key。"""
        self._wheel("key.accept", match=key_id)

    def reject_key(self, key_id: str) -> None:
        """通过受限 wheel 权限拒绝一个 Minion Key。"""
        self._wheel("key.reject", match=key_id)

    def accepted_keys(self) -> list[str]:
        """通过 wheel 查询已接受的 Minion Key。"""
        result = self._wheel("key.list_all") or {}
        data = result.get("data", {}).get("return", result.get("return", result))
        return sorted(data.get("minions", []))

    def ping(self, node_id: str) -> bool:
        """复用批量探测契约，单节点接入同样受五秒截止时间保护。"""
        return self.ping_many([node_id]).get(node_id, False)

    def ping_many(self, node_ids: list[str]) -> dict[str, bool]:
        """用一次 list targeting 探测多个 Minion，并严格过滤错误字符串。

        Salt 对失联 Key 会返回 ``Minion did not return`` 字符串。该字符串非空，
        不能使用 ``bool(value)`` 判断，否则会把 Offline 节点误报为 ONLINE。
        五秒 publish timeout 是在线探测的独立硬上限，不影响任务执行超时。
        """

        if not node_ids:
            return {}
        result = self._request({
            "client": "local",
            "tgt": ",".join(node_ids),
            "tgt_type": "list",
            "fun": "test.ping",
            "timeout": NODE_PROBE_TIMEOUT_SECONDS,
        })
        values = result if isinstance(result, dict) else {}
        return {node_id: values.get(node_id) is True for node_id in node_ids}

    def node_info(self, node_id: str) -> dict[str, Any]:
        """首次接入时低频读取 hostname 和 IPv4，不扫描进程或服务。"""

        grains = self._local(node_id, "grains.item", ["host", "ipv4"])
        if not isinstance(grains, dict):
            raise RuntimeError("InvalidGrainsResponse")
        ips = grains.get("ipv4") or []
        management_ip = next(
            (str(ip) for ip in ips if isinstance(ip, str) and not ip.startswith("127.")),
            None,
        )
        return {
            "hostname": grains.get("host", node_id),
            "management_ip": management_ip,
        }

    def process_snapshot_many(self, node_ids: list[str]) -> dict[str, SaltProcessSnapshot]:
        """用一次 list targeting 读取进程快照，且不执行 grains 或服务查询。

        命令是应用内固定常量，RoleRule pattern 只在本地 Python 中匹配，绝不会
        拼接进远端 Shell。登录、重认证、业务 HTTP 请求和 Salt publish 共享 15 秒
        deadline，避免坏节点让识别请求等待数分钟。
        """

        if not node_ids:
            return {}
        result = self._request(
            {
                "client": "local",
                "tgt": ",".join(node_ids),
                "tgt_type": "list",
                "fun": "cmd.run_all",
                "arg": PROCESS_SNAPSHOT_COMMAND,
                "timeout": ROLE_DETECTION_TIMEOUT_SECONDS,
            },
            timeout_seconds=ROLE_DETECTION_TIMEOUT_SECONDS,
        )
        values = result if isinstance(result, dict) else {}
        snapshots: dict[str, SaltProcessSnapshot] = {}
        for node_id in node_ids:
            value = values.get(node_id)
            if isinstance(value, dict) and int(value.get("retcode", 1)) == 0:
                snapshots[node_id] = SaltProcessSnapshot(
                    state="SUCCESS",
                    process_text=str(value.get("stdout", "")),
                )
            elif isinstance(value, dict):
                snapshots[node_id] = SaltProcessSnapshot(
                    state="FAILED",
                    failure_reason=f"PROCESS_SNAPSHOT_EXIT_{int(value.get('retcode', 1))}",
                )
            else:
                snapshots[node_id] = SaltProcessSnapshot(state="FAILED", failure_reason="NODE_NO_RESPONSE")
        return snapshots

    def transfer_package(self, node_id: str, salt_source: str, target_path: str) -> None:
        """让 Minion 从只读 Fileserver 下载已校验的内层包。"""
        result = self._local(node_id, "cp.get_file", [salt_source, target_path], {"makedirs": True})
        if not result:
            raise RuntimeError("PackageTransferFailed")

    def prepare_workdir(self, node_id: str, archive_path: str, workdir: str) -> None:
        """在远端创建隔离目录；GNU tar 自动识别普通 tar 和 gzip tar。"""
        import shlex
        # 所有来自任务快照的路径都经 shell quoting，不能直接拼接成命令参数。
        command = f"mkdir -p {shlex.quote(workdir)} && tar -xf {shlex.quote(archive_path)} -C {shlex.quote(workdir)}"
        result = self._local(node_id, "cmd.run_all", [command]) or {}
        if int(result.get("retcode", 1)) != 0:
            raise RuntimeError(f"PackageExtractFailed: {result.get('stderr', '')}")

    def start_step(self, node_id: str, executor_type: str, script: str, workdir: str, stdout_path: str, stderr_path: str, exit_path: str) -> str:
        """异步启动 Shell/Python Step，把输出和退出码写到独立远端文件。"""
        import posixpath
        import shlex
        executable = "/bin/bash" if executor_type == "shell" else "/usr/bin/python3"
        log_dir = posixpath.dirname(stdout_path)
        command = (
            f"mkdir -p {shlex.quote(log_dir)} && cd {shlex.quote(workdir)} && {executable} {shlex.quote(script)} "
            f">{shlex.quote(stdout_path)} 2>{shlex.quote(stderr_path)}; "
            f"rc=$?; printf '%s' \"$rc\" > {shlex.quote(exit_path)}; exit $rc"
        )
        # local_async 的返回只用于取得 JID；Scheduler 会先落库 JID，再开始轮询。
        result = self._request({"client": "local_async", "tgt": node_id, "fun": "cmd.run_all", "arg": command})
        jid = result.get("jid") if isinstance(result, dict) else None
        if not jid:
            raise RuntimeError("SaltJobSubmitFailed")
        jid = str(jid)
        self._remember_submission(jid)
        return jid

    def job_result(self, jid: str, node_id: str) -> SaltJobResult:
        """查询 Job 并区分运行中、完成和无法恢复的执行状态丢失。"""
        result = self._request({"client": "runner", "fun": "jobs.lookup_jid", "jid": jid}) or {}
        if not result:
            jobs = self._request({"client": "runner", "fun": "jobs.list_jobs"}) or {}
            # 刚提交的 Job 可能尚未出现在 job cache，10 秒宽限可避免误判；重启后没有
            # 内存提交时间，只能依据持久化 JID 和 Salt job 列表保守判断。
            in_submission_grace = self._submission_is_in_grace(jid)
            if jid in jobs or in_submission_grace:
                return SaltJobResult(state="RUNNING")
            self._forget_submission(jid)
            return SaltJobResult(state="LOST", failure_reason="EXECUTION_STATE_LOST")
        node_result = result.get(node_id)
        if node_result is None:
            self._forget_submission(jid)
            return SaltJobResult(state="LOST", failure_reason="EXECUTION_STATE_LOST")
        self._forget_submission(jid)
        if isinstance(node_result, dict):
            code = int(node_result.get("retcode", 1))
            return SaltJobResult(state="SUCCESS" if code == 0 else "FAILED", exit_code=code, stdout=str(node_result.get("stdout", "")), stderr=str(node_result.get("stderr", "")))
        return SaltJobResult(state="SUCCESS", exit_code=0, stdout=str(node_result))

    def read_file(self, node_id: str, path: str, offset: int) -> tuple[bytes, int]:
        """从远端日志文件按字节偏移读取，并用 base64 安全穿过文本协议。"""
        import shlex
        code = "import base64,sys;p=open(sys.argv[1],'rb');p.seek(int(sys.argv[2]));d=p.read();print(base64.b64encode(d).decode())"
        command = f"python3 -c {shlex.quote(code)} {shlex.quote(path)} {offset}"
        encoded = self._local(node_id, "cmd.run", [command]) or ""
        data = base64.b64decode(encoded) if encoded else b""
        return data, offset + len(data)

    def terminate_job(self, node_id: str, jid: str) -> str:
        """请求 Minion 尽力终止超时 Job，返回文本只作为警告证据。"""
        try:
            result = self._local(node_id, "saltutil.kill_job", [jid])
            return str(result)
        finally:
            # Scheduler 在超时后不再轮询该 JID，因此无论终止请求是否成功都要清理。
            self._forget_submission(jid)

    def cleanup_workdir(self, node_id: str, workdir: str) -> None:
        """删除远端 Attempt 工作目录；失败由 Scheduler 记录并稍后重试。"""
        import shlex
        result = self._local(node_id, "cmd.run_all", [f"rm -rf -- {shlex.quote(workdir)}"]) or {}
        if int(result.get("retcode", 1)) != 0:
            raise RuntimeError(f"WorkdirCleanupFailed: {result.get('stderr', '')}")


def create_salt_adapter(settings: Settings) -> SaltAdapter:
    """根据配置选择 Fake 或真实 HTTP Adapter。"""
    return FakeSaltAdapter() if settings.salt_mode == "fake" else HttpSaltAdapter(settings)
