# 云平台自动化维护中心 V1——维护包 manifest.yaml 与出包规范

**文档版本：** V1.0
**适用对象：** 维护包研发平台、研发人员、Automation Center
**执行平台：** Automation Center V1
**支持执行器：** Shell、Python
**预留执行器：** Ansible

------

# 1. 文档目的

本文档定义研发出包平台与 Automation Center 之间的维护包接口规范。

维护包必须满足本规范后才能被 Automation Center 正确识别和执行。

职责边界：

```text
研发出包平台
负责：
维护逻辑开发
脚本测试
文件准备
Manifest 生成
业务校验
包结构校验
SHA256 生成

Automation Center
负责：
上传
SHA256 验证
Manifest 读取
保存
节点选择
任务下发
执行
状态记录
实时日志
Retry
```

------

# 2. 包结构总览

维护包采用两层结构：

```text
Outer Bundle
    │
    ├── payload.tar.gz
    └── payload.sha256
             │
             ▼
       payload.tar.gz
             │
             ├── manifest.yaml
             ├── scripts/
             └── files/
```

------

# 3. 外层包

建议外层包统一使用：

```text
.tar.gz
```

示例：

```text
nova-compute-fix-001.bundle.tar.gz
```

解压后必须包含：

```text
payload.tar.gz
payload.sha256
```

V1 建议固定这两个名称，减少 Automation Center 的包识别逻辑。

------

# 4. 外层包示例

```text
nova-compute-fix-001.bundle.tar.gz
└── nova-compute-fix-001.bundle/
    ├── payload.tar.gz
    └── payload.sha256
```

外层包本身：

```text
不要求 SHA256 校验
```

------

# 5. payload.sha256

`payload.sha256` 用于校验：

```text
payload.tar.gz
```

推荐文件内容采用标准 SHA256 格式：

```text
<64位SHA256>  payload.tar.gz
```

例如：

```text
8a9f0e...c91d  payload.tar.gz
```

Automation Center 执行：

```text
SHA256(payload.tar.gz)
```

并与文件中记录值比较。

不一致：

```text
Reject Package
```

------

# 6. 内层包

真正下发目标节点的是：

```text
payload.tar.gz
```

典型结构：

```text
payload.tar.gz
├── manifest.yaml
├── scripts/
│   ├── precheck.sh
│   ├── fix.py
│   └── verify.sh
└── files/
    ├── nova.conf
    ├── nova-compute.rpm
    ├── xxx.py
    └── patch.diff
```

------

# 7. manifest.yaml

`manifest.yaml` 是维护包的唯一执行描述文件。

位置必须固定：

```text
payload 根目录/manifest.yaml
```

不得：

```text
config/manifest.yaml
scripts/manifest.yaml
```

------

# 8. Manifest Schema Version

建议 V1 增加：

```yaml
manifest_version: "1.0"
```

该字段描述的是：

> Manifest 格式版本。

不是维护包 Revision。

维护包 Revision：

```text
v1 / v2 / v3
```

由 Automation Center 自己维护。

研发出包平台不得通过 Manifest 控制 Automation Center Revision。

------

# 9. 完整 Manifest 示例

```yaml
manifest_version: "1.0"

name: nova-compute-fix-001

description: >
  修复 nova-compute 在特定场景下出现的已知问题。

component: nova

bug_id: BUG-12345

target_roles:
  - compute

applicable_versions:
  - OpenStack Yoga

steps:
  - name: precheck
    type: shell
    script: scripts/precheck.sh
    timeout: 300
    failure_action: stop

  - name: fix
    type: python
    script: scripts/fix.py
    timeout: 1800
    failure_action: stop

  - name: verify
    type: shell
    script: scripts/verify.sh
    timeout: 300
    failure_action: ignore
```

------

# 10. 顶层字段

## manifest_version

```yaml
manifest_version: "1.0"
```

用途：

```text
Manifest Schema Version
```

V1 固定：

```text
1.0
```

------

## name

```yaml
name: nova-compute-fix-001
```

维护包逻辑名称。

要求：

- 必填；
- 同一个 Automation Center 中应唯一；
- Update 操作时必须与被更新 Package Name 一致。

建议只使用：

```text
字母
数字
-
_
.
```

不建议使用中文作为内部 name。

中文说明放：

```text
description
```

------

## description

```yaml
description: 修复 nova-compute XXX 问题
```

用于 Web 展示。

可以包含中文。

------

## component

```yaml
component: nova
```

用于描述该维护包主要关联组件。

示例：

```text
nova
neutron
ovs
ovn
ceph
system
kernel
libvirt
```

Automation Center 不根据该字段执行特殊逻辑。

仅用于：

```text
展示
搜索
分类
```

------

## bug_id

```yaml
bug_id: BUG-12345
```

用于关联：

```text
Bug
Defect
Issue
内部缺陷编号
```

如果维护动作不是 Bug 修复，可以填写：

```yaml
bug_id: CONFIG-2026-001
```

或者允许为空。

------

## target_roles

示例：

```yaml
target_roles:
  - compute
```

或：

```yaml
target_roles:
  - controller
  - network
```

V1 核心角色：

```text
compute
network
controller
ceph
```

该字段属于：

```text
建议执行范围
```

不是强制限制。

用户可以选择不匹配节点，但 Automation Center 必须：

```text
警告
+
二次确认
```

------

## applicable_versions

例如：

```yaml
applicable_versions:
  - OpenStack Yoga
```

或者：

```yaml
applicable_versions:
  - ECP 6.1
  - ECP 6.2
```

Automation Center：

```text
只展示
不自动识别节点版本
不进行强制匹配
```

因此该字段本质属于：

```text
人工执行参考信息
```

------

# 11. steps

```yaml
steps:
```

定义维护包执行流程。

例如：

```yaml
steps:
  - name: check
    ...

  - name: fix
    ...

  - name: verify
    ...
```

执行顺序严格按照 YAML 数组顺序。

不支持：

```yaml
order: 10
```

等额外排序逻辑。

------

# 12. Step Name

```yaml
name: precheck
```

要求：

```text
同一 Manifest 内唯一
```

建议使用简单英文标识：

```text
precheck
backup
fix
restart
verify
cleanup
```

该字段同时用于：

```text
Web 展示
日志目录
执行结果
故障定位
```

------

# 13. Step Type

V1 支持：

```yaml
type: shell
```

和：

```yaml
type: python
```

未来预留：

```yaml
type: ansible
```

但 V1 如果读取到：

```yaml
type: ansible
```

应提示：

```text
Executor Unsupported
```

不得执行。

------

# 14. Shell Step

示例：

```yaml
- name: restart
  type: shell
  script: scripts/restart.sh
  failure_action: stop
```

执行解释器：

```text
/bin/bash
```

脚本必须位于 payload 内。

------

# 15. Python Step

示例：

```yaml
- name: fix
  type: python
  script: scripts/fix.py
```

执行解释器：

```text
python3
```

目标节点必须提前存在可使用的 Python3。

维护包不应假设 Automation Center 的 Python Runtime 与目标节点 Runtime 相同。

------

# 16. script

例如：

```yaml
script: scripts/fix.py
```

必须使用：

```text
相对于 payload 根目录的相对路径
```

允许：

```text
scripts/fix.py
scripts/check.sh
```

禁止：

```text
/etc/test.sh
/root/fix.py
../fix.py
../../etc/passwd
```

------

# 17. Working Directory

所有 Step 执行时：

```text
Current Working Directory = payload 解压根目录
```

例如目标节点：

```text
/var/lib/automation-center/tasks/task-001/work/
```

包含：

```text
manifest.yaml
scripts/
files/
```

执行：

```text
scripts/fix.py
```

时当前目录就是：

```text
work/
```

因此脚本访问资源应使用相对路径：

```text
files/nova.conf
files/xxx.rpm
```

不应依赖 Automation Center 随机临时目录。

------

# 18. timeout

例如：

```yaml
timeout: 300
```

单位：

```text
秒
```

如果未填写：

```text
使用 Automation Center System Settings
default_step_timeout
```

如果系统也未配置：

```text
1800 秒
```

------

# 19. timeout 行为

超过 timeout：

```text
Step = Failed
failure_reason = Timeout
```

随后根据：

```text
failure_action
```

决定是否执行下一 Step。

------

# 20. failure_action

支持两个值：

```text
stop
ignore
```

默认：

```text
stop
```

------

# 21. failure_action=stop

例如：

```yaml
failure_action: stop
```

行为：

```text
Step1 Success
      ↓
Step2 Failed
      ↓
后续 Step 全部 Skipped
      ↓
Node Failed
```

适用于：

```text
前置检查失败
关键修复失败
必要文件不存在
升级失败
```

------

# 22. failure_action=ignore

例如：

```yaml
failure_action: ignore
```

行为：

```text
Step Failed
      ↓
记录失败详情
      ↓
标记 ignored
      ↓
继续下一 Step
```

推荐用于：

```text
非关键清理
辅助检查
允许失败的探测动作
```

V1 建议：

> 仅有 ignored Step 失败时，不将整个 Node 判为 Failed。

页面应保留告警信息。

------

# 23. Exit Code

脚本执行结果统一采用 Linux Exit Code。

```text
0
→ Success

非 0
→ Failed
```

研发侧必须确保脚本正确返回 exit code。

禁止出现：

```text
执行失败
但最终 exit 0
```

否则 Automation Center 无法正确判断执行结果。

------

# 24. stdout

脚本普通进度信息输出：

```text
stdout
```

例如 Shell：

```text
echo "checking nova service..."
```

Python：

```text
print("checking nova service...")
```

Automation Center 会将 stdout：

```text
实时展示
+
持久化
```

------

# 25. stderr

异常、警告等信息应合理输出：

```text
stderr
```

Automation Center 独立记录：

```text
stdout
stderr
```

研发侧不要将所有内容无差别重定向到 stdout。

------

# 26. 推荐日志格式

为了方便人阅读，建议脚本输出：

```text
[INFO] Checking current version
[INFO] Backup /etc/nova/nova.conf
[INFO] Replacing file
[INFO] Restart nova-compute
[INFO] Verify service
```

失败：

```text
[ERROR] nova-compute failed to restart
```

V1 不要求 JSON Log。

------

# 27. 动态参数

V1 不允许 Automation Center 给维护包传入业务参数。

禁止依赖 Web 用户输入：

```text
service_name
config_value
file_path
timeout argument
```

维护包必须在出包阶段确定完整行为。

------

# 28. 环境变量

维护脚本不应依赖 Automation Center 注入业务变量。

平台可以提供少量只读运行上下文，例如未来：

```text
AUTOMATION_TASK_ID
AUTOMATION_NODE_ID
AUTOMATION_ATTEMPT
```

但 V1 维护逻辑不得强依赖这些变量。

------

# 29. files 目录

`files/` 可包含任意普通维护资源，例如：

```text
RPM
配置文件
Python 文件
Patch
二进制文件
证书
模板
压缩文件
```

Automation Center：

```text
不理解
不修改
不检查业务内容
```

全部作为 payload 的一部分下发。

------

# 30. 文件操作责任

例如：

```text
files/nova.conf
```

Automation Center 不会自动将它复制到：

```text
/etc/nova/nova.conf
```

必须由 Step 明确执行。

例如逻辑上：

```text
backup
copy
chmod
chown
restart
verify
```

全部属于维护脚本职责。

------

# 31. Root 权限

所有维护 Step：

```text
以 root 权限执行
```

因此维护脚本可以执行：

```text
systemctl
rpm
dnf
ovs-vsctl
ceph
修改 /etc
替换程序文件
```

研发侧需要对脚本风险负责。

------

# 32. 幂等性建议

虽然 Automation Center 不强制检查脚本幂等，但维护包应尽可能支持重复执行。

原因：

```text
Failed Node Retry
→ 从 Step1 重新执行
```

因此例如：

```text
mkdir
copy
rpm install
修改配置
```

都应考虑第二次执行时不会产生不可控结果。

------

# 33. Precheck 建议

重要维护包建议第一步设置：

```text
precheck
```

用于判断：

```text
环境是否适用
文件是否存在
软件版本是否适用
服务状态是否合理
问题是否已经修复
```

例如：

```yaml
- name: precheck
  type: shell
  script: scripts/precheck.sh
  failure_action: stop
```

------

# 34. Verify 建议

重要维护包建议最后设置：

```text
verify
```

检查：

```text
服务是否正常
配置是否生效
进程是否存在
修改是否成功
```

Automation Center 本身不理解业务成功条件，因此验证逻辑必须由维护包提供。

------

# 35. Backup

V1 Automation Center 不提供统一 rollback。

如果维护动作需要备份：

```text
必须由维护包脚本自己完成
```

例如：

```text
cp nova.conf nova.conf.bak
```

Automation Center 不负责恢复。

------

# 36. Retry 与幂等

如果某节点：

```text
Step1 Success
Step2 Failed
```

Retry：

```text
从 Step1 开始
```

因此维护包必须考虑：

```text
Step1 第二次运行
```

是否安全。

------

# 37. Package Revision

Revision 不写在 Manifest。

第一次上传：

```text
Automation Center
→ v1
```

Update：

```text
→ v2
```

再次 Update：

```text
→ v3
```

如果开发者需要在 payload 内标识自身构建版本，可以额外通过 description 或其他研发侧信息表示，但不能替代 Automation Center Revision。

------

# 38. Package Update

更新已有包时：

```text
manifest.name
```

必须与目标 Package Name 一致。

例如：

```text
现有：
name = nova-compute-fix-001

Update Bundle：
name = nova-compute-fix-001
```

允许。

如果：

```text
name = nova-compute-fix-002
```

应作为新的维护包上传，而不是 Update。

------

# 39. Package Name

推荐命名：

```text
<component>-<purpose>-<id>
```

例如：

```text
nova-compute-fix-001
ovs-lacp-fix-001
ceph-config-fix-002
kernel-net-fix-001
```

------

# 40. 文件名规范

推荐：

```text
scripts/
  01_precheck.sh
  02_fix.py
  03_verify.sh
```

虽然实际执行顺序由 Manifest 决定，但编号有助于研发调试和人工阅读。

------

# 41. 禁止使用绝对脚本路径

禁止 Manifest：

```yaml
script: /root/fix.sh
```

维护包必须自包含。

正确：

```yaml
script: scripts/fix.sh
```

------

# 42. 禁止访问包外相对路径

禁止：

```yaml
script: ../../tmp/fix.sh
```

同时 payload 内的 symlink 也不得被用于绕过工作目录边界。

出包平台应执行该检查。

Automation Center 解包时仍需要做基础路径安全保护。

------

# 43. Shell Shebang

Shell 文件建议：

```bash
#!/bin/bash
```

但 Automation Center V1 按：

```text
/bin/bash script
```

方式运行，不依赖 executable bit 和 shebang。

------

# 44. Python Shebang

Python 可包含：

```python
#!/usr/bin/env python3
```

但 Automation Center V1 明确使用：

```text
python3 script.py
```

------

# 45. Python 依赖

V1 不负责自动创建 venv 或安装 Python dependency。

如果脚本需要：

```text
requests
yaml
其他第三方 Python Package
```

研发侧必须：

1. 确认目标节点已有；
2. 或在维护包中自行携带并处理；
3. 或使用纯标准库实现。

不要假设目标节点可以连接公网 pip。

------

# 46. Shell 依赖

同样，脚本如果依赖：

```text
jq
curl
rpm
ovs-vsctl
ceph
```

需要在 precheck 中检查。

Automation Center 不负责自动安装。

------

# 47. Service 名称差异

研发维护包应处理产品实际环境差异。

例如：

```text
systemctl restart nova-compute
```

如果真实环境为容器化服务，则维护包应根据环境执行正确动作。

Automation Center 不负责判断服务部署方式。

------

# 48. Applicable Versions

建议尽可能填写：

```yaml
applicable_versions:
```

例如：

```yaml
applicable_versions:
  - OpenStack Yoga
  - Product 6.2.1
```

但它只作为人工判断依据。

真正严格版本校验应该放在：

```text
precheck
```

中。

------

# 49. Target Roles

同理：

```yaml
target_roles:
```

只是建议范围。

如果必须严格禁止在错误角色执行，则维护包应在：

```text
precheck
```

检查自身环境。

不要完全依赖 Automation Center Web 警告。

------

# 50. 建议维护包结构

推荐标准模板：

```text
payload/
├── manifest.yaml
│
├── scripts/
│   ├── 01_precheck.sh
│   ├── 02_backup.sh
│   ├── 03_fix.py
│   ├── 04_restart.sh
│   └── 05_verify.sh
│
└── files/
    └── ...
```

不是所有包都需要 5 个步骤。

简单任务可以：

```text
manifest.yaml
scripts/fix.sh
```

------

# 51. 单 Step 示例

```yaml
manifest_version: "1.0"

name: ovs-max-idle-fix

description: 修改 OVS max-idle 参数

component: ovs

bug_id: CONFIG-001

target_roles:
  - network

applicable_versions: []

steps:
  - name: fix
    type: shell
    script: scripts/fix.sh
    timeout: 300
    failure_action: stop
```

------

# 52. 多 Step 示例

```yaml
manifest_version: "1.0"

name: nova-compute-hotfix-001

description: Nova Compute 文件热修复

component: nova

bug_id: BUG-2026-001

target_roles:
  - compute

applicable_versions:
  - OpenStack Yoga

steps:
  - name: precheck
    type: shell
    script: scripts/01_precheck.sh
    timeout: 120
    failure_action: stop

  - name: backup
    type: shell
    script: scripts/02_backup.sh
    timeout: 120
    failure_action: stop

  - name: fix
    type: python
    script: scripts/03_fix.py
    timeout: 600
    failure_action: stop

  - name: restart
    type: shell
    script: scripts/04_restart.sh
    timeout: 300
    failure_action: stop

  - name: verify
    type: shell
    script: scripts/05_verify.sh
    timeout: 300
    failure_action: stop
```

------

# 53. Ignore 示例

```yaml
- name: collect_debug_info
  type: shell
  script: scripts/debug.sh
  timeout: 120
  failure_action: ignore
```

如果 debug 信息采集失败：

```text
记录错误
继续下一 Step
```

------

# 54. 出包平台校验职责

研发出包平台至少应检查：

```text
Manifest 可解析
Manifest Version 合法
name 存在
steps 非空
Step name 唯一
Step type 合法
script 文件存在
timeout 为正整数
failure_action 合法
路径不存在 ..
不存在绝对路径
不存在危险 symlink
payload 可以正常打包
```

Automation Center 不重复进行完整业务校验。

------

# 55. Automation Center 最低校验

Automation Center V1 主要负责：

```text
外层包可以解压
payload.tar.gz 存在
payload.sha256 存在
SHA256 一致
manifest.yaml 可以读取
执行所需基础 Metadata 可以解析
```

其余完整规范由出包平台保证。

------

# 56. SHA256 生成流程

研发平台：

```text
构建 payload/
      ↓
tar.gz
      ↓
payload.tar.gz
      ↓
sha256sum
      ↓
payload.sha256
      ↓
组装 Outer Bundle
```

------

# 57. 出包流程

完整流程：

```text
研发完成脚本
      ↓
本地测试
      ↓
创建 manifest.yaml
      ↓
出包平台结构校验
      ↓
构建 payload.tar.gz
      ↓
生成 payload.sha256
      ↓
构建 outer bundle
      ↓
交付 Automation Center
```

------

# 58. Automation Center 处理流程

```text
收到 Bundle
      ↓
解压 Bundle
      ↓
验证 payload SHA256
      ↓
读取 Manifest
      ↓
保存 Metadata
      ↓
保存 payload.tar.gz
```

任务执行：

```text
payload.tar.gz
      ↓
Salt Fileserver
      ↓
Minion
      ↓
Task Workspace
      ↓
解压
      ↓
按照 steps 顺序执行
```

------

# 59. 错误码语义建议

为了方便未来统一处理，可以定义逻辑错误原因：

```text
PACKAGE_CHECKSUM_FAILED
PACKAGE_MANIFEST_INVALID
PACKAGE_TRANSFER_FAILED
PACKAGE_EXTRACT_FAILED

INTERPRETER_NOT_FOUND
STEP_TIMEOUT
STEP_EXIT_NONZERO

NODE_OFFLINE
SALT_COMMUNICATION_FAILED
EXECUTION_STATE_LOST
```

这些不是脚本 exit code，而是 Automation Center 平台级 failure_reason。

------

# 60. 研发侧验收标准

一个维护包提交 Automation Center 前至少满足：

1. Bundle 可正常解压；
2. payload.sha256 与 payload.tar.gz 一致；
3. manifest.yaml 可正常解析；
4. 所有 script 路径均存在；
5. Shell/Python 在目标操作系统可运行；
6. Exit Code 正确；
7. precheck 能识别不适用环境；
8. Retry 从 Step1 重新执行不会产生不可接受副作用；
9. 必要业务验证已经包含在 Step 中；
10. 不依赖 Automation Center 提供业务参数。

------

# 61. 核心原则

维护包必须：

> 自包含、自检查、自执行、自验证。

Automation Center 不理解维护动作内部语义。

标准边界：

```text
Manifest
负责：
描述“执行什么”

Scripts
负责：
实现“如何执行”

Files
负责：
提供“修复资源”

Automation Center
负责：
“把上述内容送到正确节点并跟踪结果”
```