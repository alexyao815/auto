# 192.168.200.11/12 真实 Salt 分层验收手册

本文在 [`deployment-192.168.200.11.md`](deployment-192.168.200.11.md) 全部通过后执行。它会在 `192.168.200.12` 上真实运行 Shell/Python，并在维护窗口内暂停服务。执行前必须确认该节点是可用于验收的测试 Minion。

所有状态初始均为 `NOT RUN`。只有实际执行、核对预期并保存证据后，才能改为 `PASS` 或 `FAIL`。

## 1. 验收信息

| 项目 | 实际值 |
|---|---|
| 执行人 | NOT RUN |
| 开始/结束时间（Asia/Shanghai） | NOT RUN |
| 管理节点 | 192.168.200.11 |
| 测试 Minion | 192.168.200.12 |
| 实际 Minion ID | NOT RUN |
| 下载源码包 SHA-256 | NOT RUN |
| 镜像 ID | NOT RUN |
| CentOS 版本 | NOT RUN |
| salt-master/salt-api 版本 | NOT RUN |
| salt-minion 版本 | NOT RUN |
| 证据目录 | `/root/automation-center-acceptance/<timestamp>/` |

### 1.1 创建证据目录并记录版本

**这一步做什么：**保存本次验收对应的下载源码包摘要、镜像、系统、Salt 和 Minion 版本。目标机没有 Git，因此不执行 `git rev-parse`。

在 `.11` 执行：

```bash
export MINION_ID='替换为 salt-key -L 的真实 ID'
export EVIDENCE_DIR="/root/automation-center-acceptance/$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 0700 "$EVIDENCE_DIR"
cd /opt/automation-center
cat /root/automation-center-source-url.txt | tee "$EVIDENCE_DIR/source-url.txt"
cat /root/automation-center-source.sha256 | tee "$EVIDENCE_DIR/source-sha256.txt"
docker inspect automation-center --format '{{.Image}}' | tee "$EVIDENCE_DIR/image-id.txt"
cat /etc/centos-release | tee "$EVIDENCE_DIR/os-version.txt"
salt-master --version | tee "$EVIDENCE_DIR/salt-master-version.txt"
salt-api --version | tee "$EVIDENCE_DIR/salt-api-version.txt"
salt "$MINION_ID" test.version | tee "$EVIDENCE_DIR/salt-minion-version.txt"
printf '%s\n' "$MINION_ID" | tee "$EVIDENCE_DIR/minion-id.txt"
```

停止条件：实际 Minion ID 无法证明对应 `.12`，或者证据目录不是 root 私有目录。

## 2. 生成并核对验收包

**这一步做什么：**使用一次性 Python 容器生成四个标准双层维护包；不会修改源码，也不需要目标机安装 Python 3.12。输出目录位于持久化数据区。

在 `.11` 解压后的源码根目录执行：

```bash
cd /opt/automation-center
docker run --rm \
  -v /opt/automation-center:/workspace:ro \
  -v /var/lib/automation-center/temp:/output \
  -w /workspace \
  python:3.12-slim-bookworm \
  python scripts/build_real_salt_acceptance_packages.py \
    --output-dir /output/real-salt-acceptance \
    --name-prefix real-salt-acceptance
find /var/lib/automation-center/temp/real-salt-acceptance \
  -maxdepth 1 -type f -name '*.bundle.tar.gz' -printf '%f %s bytes\n' | sort
```

预期生成：

- `real-salt-acceptance-success.bundle.tar.gz`
- `real-salt-acceptance-retry-once.bundle.tar.gz`
- `real-salt-acceptance-long-running.bundle.tar.gz`
- `real-salt-acceptance-timeout.bundle.tar.gz`

用途和影响：

| 包 | 预期行为 | 对 `.12` 的影响 |
|---|---|---|
| success | Shell、Python 均成功 | 只写 Attempt 工作目录，成功后清理 |
| retry-once | 首次退出 42，Retry 成功 | 暂存并最终删除 `/var/lib/automation-center/acceptance/retry-once.marker` |
| long-running | 每 2 秒输出，约 90 秒 | 用于 SSE、容器重启/JID 恢复 |
| timeout | 输出后 sleep 30，Step 超时为 5 秒 | 尽力终止，失败工作目录保留 7 天 |

禁止把这些包投放到未经确认的生产 Minion。若名称冲突，使用脚本的 `--name-prefix` 生成新逻辑名称，不要覆盖有活跃任务的 Package。

## 3. 必做：基础设施和最小权限

### A-01 Salt、端口和容器

```bash
cd /opt/automation-center
MINION_ID="$MINION_ID" EXPECTED_MINION_IP=192.168.200.12 REQUIRE_DOCKER=1 \
  sh deploy/scripts/preflight.sh | tee "$EVIDENCE_DIR/A-01-preflight.txt"
APP_IP=192.168.200.11 sh deploy/scripts/post-deploy-validate.sh \
  | tee "$EVIDENCE_DIR/A-01-post-deploy.txt"
MINION_ID="$MINION_ID" \
SALT_API_USERNAME=automation-center \
SALT_API_PASSWORD=automation-center \
sh deploy/salt/verify-salt-api.sh | tee "$EVIDENCE_DIR/A-01-salt-api.txt"
```

预期：三个脚本均退出 0；没有打印 Token 或密码。

停止条件：任一 `FAIL`、8000 对外暴露、Minion 不对应 `.12`，或容器不是 Healthy。

| 用例 | 状态 | 实际结果/证据 |
|---|---|---|
| A-01 | NOT RUN | NOT RUN |

## 4. 必做：登录与节点接入

### A-02 登录

1. 运维浏览器访问 `https://192.168.200.11:8443`。
2. 确认证书 SAN 为 `192.168.200.11`，再信任自签名证书。
3. 使用固定测试账号 `admin/admin` 登录。
4. 打开 Dashboard，确认页面没有 API 错误。

随后在 `.11` 执行自动性能和页面 API 回归；脚本不会打印密码、Token 或 Cookie：

```bash
cd /opt/automation-center
APP_URL=https://192.168.200.11:8443 \
APP_USERNAME=admin APP_PASSWORD=admin \
sh deploy/scripts/auth-ui-validate.sh \
  | tee "$EVIDENCE_DIR/A-02-auth-ui.txt"
docker logs --since 10m automation-center 2>&1 \
  | grep -E 'auth/login|auth/me|database is locked|SQLite 写锁|未处理异常' \
  | tee "$EVIDENCE_DIR/A-02-auth-log.txt"
```

预期：登录成功，浏览器只保存 Secure/HttpOnly Session Cookie；页面时间按 Asia/Shanghai 展示；登录小于 1 秒；20 个并发 `/auth/me` 全部 200 且最大耗时小于 2 秒；Dashboard、节点、维护包、任务、设置和审计 API 都小于 2 秒；日志无 500 和 SQLite 锁错误。

### A-03 节点发现

1. 进入“节点中心”。
2. 若实际 Minion ID 位于 `Pending Keys`，点击“接受”；若已接受，点击“立即探测”。
3. 核对 Hostname、管理 IP 为 `.12`、状态为 `ONLINE`。
4. 点击“角色”，人工设置 `compute`；不要依赖测试机恰好运行 nova 进程。

预期：节点 Enabled、ONLINE，角色包含 `compute`；Audit Log 记录节点操作。

停止条件：页面显示的 Minion ID、Hostname 或 IP 无法证明是 `.12`。不要接受未知 Key。

| 用例 | 状态 | 实际结果/证据 |
|---|---|---|
| A-02 | NOT RUN | NOT RUN |
| A-03 | NOT RUN | NOT RUN |

## 5. 必做：成功任务和实时日志

### A-04 上传成功包

1. 进入“维护包中心”。
2. 上传 `/var/lib/automation-center/temp/real-salt-acceptance/real-salt-acceptance-success.bundle.tar.gz`。浏览器在其他主机时，先通过安全文件传输复制该包，不要放宽服务器文件权限。
3. 核对包名、Revision v1、目标角色 `compute`、两个 Step 顺序为 Shell → Python。

预期：上传校验成功，无 SHA/Manifest 错误。

### A-05 创建成功任务

1. 进入“创建维护任务”，选择 success 包。
2. 只直接选择 `.12` 对应节点；不要仅靠角色批量选择。
3. 生成确认快照，再次核对实际节点只有 `.12`。
4. 填写 Remark：`REAL-SALT-ACCEPTANCE A-05`，创建任务并立即记录 Task ID。
5. 打开任务详情，观察 TaskNode、Attempt 1、两个 Step。

预期：Task 从 WAITING/RUNNING 收敛为 SUCCESS；Attempt 1 SUCCESS；Shell 和 Python Step 均 SUCCESS、exit code 0。

停止条件：确认快照出现其他节点，立即返回，不得创建任务。

### A-06 SSE 和日志

在任务运行时分别点击两个 Step 的“查看”，保存浏览器截图。预期日志包含：

```text
REAL_SALT_SHELL_SUCCESS
REAL_SALT_PYTHON_SUCCESS
```

服务端支持 `Last-Event-ID`，但当前前端在 EventSource 出错时主动关闭连接；断线后需要重新打开日志，不能把前端自动重连记为通过。

### A-07 成功目录清理和结构化历史

将 Task ID 写入变量，在 `.11` 执行：

```bash
export SUCCESS_TASK_ID='从页面复制 Task ID'
salt "$MINION_ID" cmd.retcode \
  "test ! -e /var/lib/automation-center/tasks/$SUCCESS_TASK_ID/attempt-1" \
  | tee "$EVIDENCE_DIR/A-07-remote-cleanup.txt"
find /var/lib/automation-center/logs -path "*$SUCCESS_TASK_ID*" -type f -printf '%s %p\n' \
  | tee "$EVIDENCE_DIR/A-07-local-logs.txt"
```

预期：远端 `cmd.retcode` 返回 0；管理节点仍保存 stdout/stderr；任务详情和 Audit Log 可查询。

| 用例 | 状态 | 实际结果/证据 |
|---|---|---|
| A-04 | NOT RUN | NOT RUN |
| A-05 | NOT RUN | NOT RUN |
| A-06 | NOT RUN | NOT RUN |
| A-07 | NOT RUN | NOT RUN |

## 6. 必做：备份与容器持久化

### A-08 手工备份

```bash
docker exec automation-center automation-center backup-db \
  | tee "$EVIDENCE_DIR/A-08-backup-path.txt"
backup_path="$(tail -n 1 "$EVIDENCE_DIR/A-08-backup-path.txt")"
test -s "$backup_path"
```

预期：输出文件位于 `/var/lib/automation-center/backups/` 且非空。

### A-09 容器重启

```bash
cd /opt/automation-center
docker restart automation-center
attempt=0
until [ "$(docker inspect -f '{{.State.Health.Status}}' automation-center 2>/dev/null)" = healthy ]; do
  attempt=$((attempt + 1)); [ "$attempt" -lt 30 ] || exit 1; sleep 2
done
APP_IP=192.168.200.11 sh deploy/scripts/post-deploy-validate.sh \
  | tee "$EVIDENCE_DIR/A-09-restart.txt"
```

重新登录并核对：节点、success Package、A-05 Task、日志、Settings 和 Audit Log 均存在。

停止条件：60 秒未 Healthy；立即保存日志，不要删除数据目录或重新初始化数据库。

| 用例 | 状态 | 实际结果/证据 |
|---|---|---|
| A-08 | NOT RUN | NOT RUN |
| A-09 | NOT RUN | NOT RUN |

## 7. 维护窗口：失败与 Retry

### D-01 首次失败

先确保旧标记不存在：

```bash
salt "$MINION_ID" cmd.run 'rm -f /var/lib/automation-center/acceptance/retry-once.marker'
```

上传 `retry-once` 包，只选择 `.12`，Remark 使用 `REAL-SALT-ACCEPTANCE D-01`。预期 Attempt 1 FAILED、Step exit code 42，stderr 包含：

```text
REAL_SALT_RETRY_FIRST_FAILURE
```

### D-02 Retry

在同一 TaskNode 点击 `Retry`。预期重新排在已有 Waiting 任务之后，从 Step 1 创建 Attempt 2；最终 Attempt 2 SUCCESS，stdout 包含：

```text
REAL_SALT_RETRY_SUCCESS
```

确认标记已清理：

```bash
salt "$MINION_ID" cmd.retcode \
  'test ! -e /var/lib/automation-center/acceptance/retry-once.marker' \
  | tee "$EVIDENCE_DIR/D-02-marker-cleanup.txt"
```

恢复命令：若用例中断，执行上面的 `rm -f`。最长等待 3 分钟。若目标选错、产生超过两个 Attempt 或 Retry 没有从 Step 1 开始，停止验收并保存证据。

| 用例 | 状态 | 实际结果/证据 |
|---|---|---|
| D-01 | NOT RUN | NOT RUN |
| D-02 | NOT RUN | NOT RUN |

## 8. 维护窗口：Timeout

### D-03 Step Timeout

上传 `timeout` 包，只选择 `.12`，Remark 使用 `REAL-SALT-ACCEPTANCE D-03`。预期约 5 秒后：

- Step、Attempt、TaskNode 和 Task 收敛为 FAILED。
- exit code 为 124，failure reason 包含 `TIMEOUT` 和尽力终止结果。
- 日志包含 `REAL_SALT_TIMEOUT_STARTED`，不应包含 `UNEXPECTED_COMPLETION`。
- 失败 Attempt 工作目录被保留，不立即清理。

在页面复制 Task ID 后验证：

```bash
export TIMEOUT_TASK_ID='从页面复制 Task ID'
salt "$MINION_ID" cmd.run \
  "find /var/lib/automation-center/tasks/$TIMEOUT_TASK_ID -maxdepth 3 -type f -printf '%p %s bytes\\n' 2>/dev/null || true" \
  | tee "$EVIDENCE_DIR/D-03-retained-workdir.txt"
```

最长等待 2 分钟。若 30 秒后仍 RUNNING、远端脚本继续运行，先执行：

```bash
salt "$MINION_ID" cmd.run "pkill -f '$TIMEOUT_TASK_ID' || true"
```

随后停止验收并记录 FAIL；不要手工把数据库状态改成成功。

| 用例 | 状态 | 实际结果/证据 |
|---|---|---|
| D-03 | NOT RUN | NOT RUN |

## 9. 维护窗口：Minion Offline 和风险确认

### D-04 Offline 探测

在 `.12` 执行：

```bash
systemctl stop salt-minion
```

在 `.11` 验证 `salt "$MINION_ID" test.ping` 无成功返回，再在页面点击“立即探测”。预期节点显示 OFFLINE。

进入创建任务页面，选择任意验收包和该节点，仅生成确认快照：预期出现 Offline 二次确认警告。**不要正式创建任务**。

恢复：

```bash
# 在 192.168.200.12
systemctl start salt-minion
systemctl is-active salt-minion

# 在 192.168.200.11
attempt=0
until salt "$MINION_ID" test.ping 2>/dev/null | grep -q True; do
  attempt=$((attempt + 1))
  [ "$attempt" -lt 60 ] || { echo 'Minion 未在 120 秒内恢复' >&2; exit 1; }
  sleep 2
done
```

随后页面重新探测，状态必须恢复 ONLINE。最长中断 2 分钟。若有非验收任务正在运行或 `.12` 不是专用测试节点，不执行本用例。

| 用例 | 状态 | 实际结果/证据 |
|---|---|---|
| D-04 | NOT RUN | NOT RUN |

## 10. 维护窗口：应用重启与 JID 恢复

### D-05 长任务日志

上传 `long-running` 包，只选择 `.12`，Remark 使用 `REAL-SALT-ACCEPTANCE D-05`。任务进入 RUNNING 后打开日志，确认连续出现 `tick=1`、`tick=2` 等。记录 Task ID。

### D-06 重启应用容器

确认 Step 已产生 Salt JID 后，在 `.11` 执行：

```bash
docker restart automation-center
```

浏览器连接会断开，这是当前前端边界。等待容器 Healthy 后重新登录并打开任务详情。

预期：

- 原 Attempt 继续，不创建 Attempt 2。
- 不出现第二次 `tick=1`，没有重复下发。
- Task 最终 SUCCESS，日志继续到 `tick=45`。
- Audit/Task 结构化历史仍存在。

恢复：容器未在 60 秒内 Healthy 时保存 `docker logs automation-center`，执行 `docker compose up -d`。最长等待任务完成 4 分钟。出现 `EXECUTION_STATE_LOST`、重复 Attempt 或重复日志起点即记 FAIL。

| 用例 | 状态 | 实际结果/证据 |
|---|---|---|
| D-05 | NOT RUN | NOT RUN |
| D-06 | NOT RUN | NOT RUN |

## 11. 维护窗口：salt-api 短暂中断

### D-07 已下发 JID 的监控恢复

再次创建一个 `long-running` 任务。必须等 Step 进入 RUNNING 且日志已有至少 3 个 tick 后执行：

```bash
systemctl stop salt-api
sleep 10
systemctl start salt-api
systemctl is-active salt-api
```

预期：Minion 上已提交的 Job 继续运行；应用在 API 恢复后继续查询同一 JID，最终 Task SUCCESS，不新建 Attempt。

恢复：`systemctl start salt-api`，然后重新执行最小权限验证脚本。salt-api 最长中断 15 秒，必须小于 Step Timeout。若同时影响已有非验收任务，不执行本用例；salt-api 无法恢复时立即停止验收。

| 用例 | 状态 | 实际结果/证据 |
|---|---|---|
| D-07 | NOT RUN | NOT RUN |

## 12. 收尾与一致性检查

```bash
cd /opt/automation-center
APP_IP=192.168.200.11 sh deploy/scripts/post-deploy-validate.sh \
  | tee "$EVIDENCE_DIR/final-post-deploy.txt"
salt "$MINION_ID" test.ping | tee "$EVIDENCE_DIR/final-minion-ping.txt"
salt "$MINION_ID" cmd.run \
  'rm -f /var/lib/automation-center/acceptance/retry-once.marker' \
  | tee "$EVIDENCE_DIR/final-marker-cleanup.txt"
docker compose ps | tee "$EVIDENCE_DIR/final-compose-ps.txt"
journalctl -u salt-master -u salt-api --since "1 hour ago" --no-pager \
  > "$EVIDENCE_DIR/final-salt-journal.txt"
docker logs --since 1h automation-center > "$EVIDENCE_DIR/final-container.log" 2>&1
```

不要删除失败任务工作目录；保留策略由 Scheduler 按 7 天执行。验收包可以保留用于复测，但不得更新或删除仍有 WAITING/RUNNING 引用的 Package。

## 13. 汇总记录

| 类别 | 用例 | 初始状态 | 最终状态 | 证据/备注 |
|---|---|---|---|---|
| 基础 | A-01 基础设施和最小权限 | NOT RUN | NOT RUN | NOT RUN |
| 认证 | A-02 登录 | NOT RUN | NOT RUN | NOT RUN |
| 节点 | A-03 节点发现 | NOT RUN | NOT RUN | NOT RUN |
| 包 | A-04 上传成功包 | NOT RUN | NOT RUN | NOT RUN |
| 任务 | A-05 成功任务 | NOT RUN | NOT RUN | NOT RUN |
| 日志 | A-06 SSE/日志 | NOT RUN | NOT RUN | NOT RUN |
| 清理 | A-07 成功目录清理 | NOT RUN | NOT RUN | NOT RUN |
| 备份 | A-08 SQLite 备份 | NOT RUN | NOT RUN | NOT RUN |
| 持久化 | A-09 容器重启 | NOT RUN | NOT RUN | NOT RUN |
| Retry | D-01/D-02 首次失败与 Retry | NOT RUN | NOT RUN | NOT RUN |
| Timeout | D-03 Timeout | NOT RUN | NOT RUN | NOT RUN |
| Offline | D-04 Minion Offline | NOT RUN | NOT RUN | NOT RUN |
| 恢复 | D-05/D-06 容器重启/JID | NOT RUN | NOT RUN | NOT RUN |
| 故障 | D-07 salt-api 中断 | NOT RUN | NOT RUN | NOT RUN |

最终结论只能填写以下之一：

- `PASS`：全部必做项通过，已执行的扰动项也全部通过。
- `PASS WITH LIMITATIONS`：全部必做项通过，明确列出的扰动项未执行。
- `FAIL`：任一必做项失败，或出现越权执行、重复下发、状态错误、数据丢失。

## 14. 明确未覆盖

当前只有一个真实 Minion，以下项目不能由本次结果替代：

- 30 节点并发调度与不同节点并行。
- 同一节点多个真实任务的长队列容量与公平性压力。
- 10 GiB 流式上传、20 GiB 解压容量和内存曲线。
- HA、多 Worker、多副本 Scheduler（V1 本身不支持）。

这些项目继续保持 `NOT RUN/待验收`，不得因为单 Minion 闭环成功而标记通过。
