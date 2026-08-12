下面这份可直接作为编码前的详细设计输入。

# 云平台自动化维护中心 V1——Task / Node / Step 状态机与调度器详细设计

**文档版本：** V1.0
**适用系统：** Automation Center V1
**执行底座：** SaltStack
**数据库：** SQLite
**目标规模：** ≤ 30 台受管节点
**核心调度原则：** 节点间并发、节点内串行、FIFO 排队

------

# 1. 文档目的

本文档定义 Automation Center V1 中：

- Task 状态模型；
- TaskNode 状态模型；
- Attempt 状态模型；
- Step 状态模型；
- 状态转换规则；
- Task 总体状态聚合规则；
- 节点级 FIFO 调度规则；
- Node Execution Lock；
- Retry；
- Waiting Cancel；
- Salt Job 关联；
- Automation Center 重启恢复；
- 异常处理；
- SQLite 并发控制。

本文档重点解决：

> 一个任务选择多台节点以后，系统如何可靠判断谁应该立即执行、谁应该排队、什么时候进入下一 Step，以及各种失败和重启情况下状态如何保持一致。

------

# 2. 核心对象关系

系统执行模型：

```text
Task
│
├── TaskNode: compute01
│   │
│   ├── Attempt 1
│   │   ├── Step 1
│   │   ├── Step 2
│   │   └── Step 3
│   │
│   └── Attempt 2
│       ├── Step 1
│       ├── Step 2
│       └── Step 3
│
├── TaskNode: compute02
│   └── Attempt 1
│
└── TaskNode: compute03
    └── Attempt 1
```

含义：

- **Task**：用户创建的一次维护任务；
- **TaskNode**：该 Task 在某一目标节点上的执行实例；
- **Attempt**：某个节点一次完整执行尝试；
- **Step**：Attempt 内具体 Shell/Python 执行步骤。

------

# 3. 设计原则

## 3.1 Task 不直接执行

Task 只是总体业务对象。

真正参与调度的是：

```text
TaskNode
```

例如：

```text
Task-001
├── compute01
├── compute02
└── compute03
```

调度器实际上处理：

```text
TaskNode-001
TaskNode-002
TaskNode-003
```

------

## 3.2 调度粒度是 Node

同一 Node：

```text
同一时刻只允许一个 TaskNode Running
```

不同 Node：

```text
允许同时 Running
```

因此：

```text
compute01 → Task-A Running
compute02 → Task-A Running
compute03 → Task-A Running
```

可以同时发生。

但：

```text
compute01 → Task-A Running
compute01 → Task-B Running
```

禁止发生。

------

## 3.3 数据库是状态事实来源

Automation Center 内存中的：

- Thread；
- Future；
- Worker；
- Coroutine；

都不能作为任务真实状态来源。

真实状态必须落入 SQLite。

原则：

```text
DB State = Source of Truth
```

这样服务重启后才能恢复。

------

# 4. Task 状态定义

Task 顶层状态定义：

```text
WAITING
RUNNING
SUCCESS
PARTIAL_SUCCESS
FAILED
CANCELLED
```

Web 展示：

```text
Waiting
Running
Success
Partial Success
Failed
Cancelled
```

------

# 5. WAITING

定义：

> Task 已创建，但当前没有任何目标节点进入实际执行状态。

常见场景：

```text
Task-001
├── compute01 Waiting
├── compute02 Waiting
└── compute03 Waiting
```

Task：

```text
WAITING
```

------

# 6. RUNNING

只要 Task 尚未进入终态，并存在至少一个：

```text
TaskNode = RUNNING
```

或者存在：

```text
TaskNode = WAITING
```

但已有其他节点执行完成/正在执行，则整个 Task 视为活动状态：

```text
RUNNING
```

例如：

```text
compute01 Success
compute02 Running
compute03 Waiting
```

Task：

```text
RUNNING
```

------

# 7. SUCCESS

定义：

> 所有有效目标节点最终均执行成功。

例如：

```text
compute01 Success
compute02 Success
compute03 Success
```

则：

```text
Task = SUCCESS
```

如果某个节点仅存在：

```text
failure_action = ignore
```

产生的可忽略 Step 错误，则该 Node 可以最终：

```text
SUCCESS + has_warning=true
```

仍然参与 Task SUCCESS 判定。

------

# 8. PARTIAL_SUCCESS

定义：

> 至少一个节点 Success，同时至少一个节点 Failed/Offline。

例如：

```text
compute01 Success
compute02 Success
compute03 Failed
```

则：

```text
PARTIAL_SUCCESS
```

或者：

```text
compute01 Success
compute02 Offline
```

也是：

```text
PARTIAL_SUCCESS
```

------

# 9. FAILED

定义：

> 所有实际目标节点最终都失败，没有任何成功节点。

例如：

```text
compute01 Failed
compute02 Failed
compute03 Offline
```

则：

```text
FAILED
```

Offline 在总体状态聚合时视为失败。

------

# 10. CANCELLED

主要用于：

```text
尚未执行的 Waiting TaskNode 被用户取消
```

如果一个 Task 的所有 TaskNode：

```text
都未真正执行
且
全部被 Cancelled
```

则：

```text
Task = CANCELLED
```

如果：

```text
compute01 Success
compute02 Cancelled
```

建议 Task：

```text
PARTIAL_SUCCESS
```

因为该 Task 并没有完整覆盖全部原目标节点。

------

# 11. TaskNode 状态

TaskNode 定义以下状态：

```text
WAITING
RUNNING
SUCCESS
FAILED
CANCELLED
```

Offline 不建议单独作为内部主状态。

推荐：

```text
status = FAILED
failure_reason = NODE_OFFLINE
```

这样整体状态模型更统一。

Web 可以显示：

```text
Offline
```

但数据库仍保存：

```text
FAILED + NODE_OFFLINE
```

------

# 12. TaskNode WAITING

Task 创建后，正常节点首先进入：

```text
WAITING
```

表示：

> 当前 TaskNode 尚未取得节点执行锁。

原因可能是：

```text
节点当前空闲，但 Scheduler 尚未调度

或者

节点已经有其他 TaskNode Running
```

------

# 13. TaskNode RUNNING

条件：

```text
Scheduler 成功获取 Node Execution Lock
```

并开始当前 Attempt 后：

```text
TaskNode = RUNNING
```

------

# 14. TaskNode SUCCESS

当前最新 Attempt：

```text
SUCCESS
```

则：

```text
TaskNode = SUCCESS
```

允许：

```text
has_warning = true
```

表示其中存在 `failure_action=ignore` 的失败 Step。

------

# 15. TaskNode FAILED

以下任意场景均可导致：

```text
TaskNode = FAILED
```

例如：

```text
NODE_OFFLINE
PACKAGE_TRANSFER_FAILED
PACKAGE_EXTRACT_FAILED
STEP_TIMEOUT
STEP_EXIT_NONZERO
INTERPRETER_NOT_FOUND
SALT_COMMUNICATION_FAILED
EXECUTION_STATE_LOST
```

------

# 16. TaskNode CANCELLED

只有：

```text
TaskNode = WAITING
```

允许用户取消。

转换：

```text
WAITING
   ↓ cancel
CANCELLED
```

禁止：

```text
RUNNING
   ↓
CANCELLED
```

Running TaskNode V1 不允许人为停止。

------

# 17. TaskNode 状态机

```text
                  创建
                   │
                   ▼
                WAITING
                   │
          ┌────────┼────────┐
          │                 │
       cancel           获得节点锁
          │                 │
          ▼                 ▼
      CANCELLED          RUNNING
                            │
                     ┌──────┴──────┐
                     │             │
                  SUCCESS        FAILED
                                    │
                                    │ Retry
                                    ▼
                                  WAITING
```

Retry 后重新进入：

```text
WAITING
```

等待节点执行锁。

------

# 18. Attempt 状态

Attempt 是某个 TaskNode 的一次完整执行尝试。

状态：

```text
WAITING
RUNNING
SUCCESS
FAILED
```

通常 Attempt 创建时：

```text
WAITING
```

获取节点锁后：

```text
RUNNING
```

最终：

```text
SUCCESS
或
FAILED
```

------

# 19. Attempt 与 Retry

第一次执行：

```text
Attempt 1
```

Retry：

```text
Attempt 2
```

再次 Retry：

```text
Attempt 3
```

依次递增。

历史 Attempt 永不覆盖。

------

# 20. Retry 状态流程

例如：

```text
TaskNode
└── Attempt 1 Failed
```

用户点击 Retry：

```text
检查 Package
检查 Revision
检查 Node
      ↓
创建 Attempt 2
      ↓
TaskNode → WAITING
      ↓
Scheduler
      ↓
Attempt 2 → RUNNING
```

------

# 21. Retry 前置检查

Retry 必须满足：

```text
Package 仍存在
AND
当前 Package Revision == Task Package Revision Snapshot
AND
Node Enabled
```

否则禁止 Retry。

------

# 22. Offline 节点 Retry

原任务：

```text
compute03
FAILED
failure_reason = NODE_OFFLINE
```

节点恢复：

```text
Online
```

用户允许点击 Retry。

Retry：

```text
从 Step1 开始执行
```

------

# 23. Step 状态

Step 定义：

```text
WAITING
RUNNING
SUCCESS
FAILED
SKIPPED
```

------

# 24. Step WAITING

Attempt 创建时，根据 PackageStep 快照创建：

```text
Step1 Waiting
Step2 Waiting
Step3 Waiting
```

------

# 25. Step RUNNING

Execution Engine 开始真正调用该 Step：

```text
WAITING
   ↓
RUNNING
```

同时记录：

```text
started_at
salt_jid
stdout_path
stderr_path
```

------

# 26. Step SUCCESS

脚本：

```text
exit_code = 0
```

且未 Timeout，则：

```text
SUCCESS
```

------

# 27. Step FAILED

典型条件：

```text
exit_code != 0
Timeout
Interpreter Not Found
Salt Command Failure
```

转换：

```text
RUNNING
   ↓
FAILED
```

------

# 28. Step SKIPPED

仅在：

```text
前一个 Step FAILED
且
failure_action = stop
```

时出现。

例如：

```text
Step1 Success
Step2 Failed (stop)
Step3 Skipped
Step4 Skipped
```

------

# 29. Step 状态机

```text
WAITING
   │
   ▼
RUNNING
   │
 ┌─┴───────┐
 │         │
 ▼         ▼
SUCCESS   FAILED
             │
             │ failure_action=stop
             ▼
        后续 Step
          SKIPPED
```

SKIPPED 本身不执行。

------

# 30. failure_action=stop

例如：

```yaml
failure_action: stop
```

Step Failed：

```text
当前 Step = FAILED
后续 Step = SKIPPED
Attempt = FAILED
TaskNode = FAILED
```

------

# 31. failure_action=ignore

例如：

```yaml
failure_action: ignore
```

当前 Step：

```text
FAILED
ignored = true
```

随后：

```text
继续执行下一 Step
```

如果最终不存在不可忽略失败：

```text
Attempt = SUCCESS
TaskNode = SUCCESS
has_warning = true
```

------

# 32. ignored 字段

建议 Step Result 单独保存：

```text
ignored = true / false
```

避免出现：

```text
FAILED 到底算不算整个任务失败？
```

这种歧义。

例如：

```text
status = FAILED
ignored = true
```

表示：

> 这个 Step 确实执行失败，但失败被流程规则允许忽略。

------

# 33. Attempt 最终状态算法

执行全部 Step 后：

如果存在：

```text
FAILED AND ignored=false
```

则：

```text
Attempt = FAILED
```

否则：

```text
Attempt = SUCCESS
```

如果存在：

```text
FAILED AND ignored=true
```

同时无不可忽略失败：

```text
Attempt = SUCCESS
has_warning = true
```

------

# 34. TaskNode 最终状态算法

TaskNode 当前状态以：

```text
最新 Attempt
```

为准。

例如：

```text
Attempt 1 Failed
Attempt 2 Success
```

最终：

```text
TaskNode = SUCCESS
```

历史 Attempt 1 仍保留。

------

# 35. Task 状态实时聚合

Task 状态不建议由用户操作直接修改。

每次 TaskNode 状态发生变化后执行：

```text
aggregate_task_status(task_id)
```

------

# 36. Task 聚合算法

设：

```text
W = Waiting 数量
R = Running 数量
S = Success 数量
F = Failed 数量
C = Cancelled 数量
T = 总 TaskNode 数量
```

规则优先级如下。

## 规则 1：仍存在 Running

如果：

```text
R > 0
```

则：

```text
Task = RUNNING
```

------

## 规则 2：没有 Running，但同时存在 Waiting 和已执行结果

例如：

```text
W > 0
AND
S + F > 0
```

则：

```text
Task = RUNNING
```

因为整个 Task 尚未结束。

------

## 规则 3：全部 Waiting

如果：

```text
W = T
```

则：

```text
Task = WAITING
```

------

## 规则 4：全部 Cancelled

如果：

```text
C = T
```

则：

```text
Task = CANCELLED
```

------

## 规则 5：全部成功

如果：

```text
S = T
```

则：

```text
Task = SUCCESS
```

------

## 规则 6：至少一个成功，并存在失败或取消

如果：

```text
S > 0
AND
(F > 0 OR C > 0)
```

则：

```text
Task = PARTIAL_SUCCESS
```

------

## 规则 7：没有成功且存在失败

如果：

```text
S = 0
AND
F > 0
AND
W = 0
AND
R = 0
```

则：

```text
Task = FAILED
```

------

# 37. Node Execution Lock

V1 调度最关键的约束：

```text
一个 Node
同一时间
最多一个 Running TaskNode
```

建议数据库表：

```text
node_execution_locks
├── node_id             UNIQUE
├── task_node_id
├── acquired_at
└── heartbeat_at
```

核心：

```text
UNIQUE(node_id)
```

由数据库约束避免两个 Worker 同时拿到同一 Node。

------

# 38. 获取 Node Lock

Scheduler 启动 TaskNode 前：

```text
BEGIN TRANSACTION

INSERT node_execution_locks(
    node_id,
    task_node_id
)

如果 UNIQUE 冲突
→ 获取失败

如果成功
→ TaskNode 可执行

COMMIT
```

即使未来 Scheduler 存在并发执行线程，也不能绕过数据库唯一约束。

------

# 39. 释放 Node Lock

以下场景必须释放：

```text
Attempt Success
Attempt Failed
Preparation Failed
Execution State Lost
```

释放顺序推荐：

```text
更新 Attempt/TaskNode 最终状态
   ↓
COMMIT
   ↓
删除 Node Lock
```

然后 Scheduler 下一周期才能取下一条。

------

# 40. 为什么不直接使用内存 Lock

禁止仅使用：

```text
threading.Lock
asyncio.Lock
```

作为节点锁。

原因：

```text
Automation Center Restart
→ 内存 Lock 全部消失
```

数据库 Lock 才能进行状态恢复。

------

# 41. FIFO 队列模型

不单独建设 Queue 表。

`task_nodes` 本身就是队列。

候选条件：

```text
status = WAITING
```

排序：

```text
created_at ASC
id ASC
```

同一 Node：

```text
第一条 Waiting TaskNode
```

拥有最高执行资格。

------

# 42. FIFO 示例

数据库：

```text
compute01

TaskNode-101  10:01 Waiting
TaskNode-205  10:05 Waiting
TaskNode-309  10:10 Waiting
```

执行顺序：

```text
101
 ↓
205
 ↓
309
```

------

# 43. Scheduler 总体结构

Scheduler 是 Automation Center 后台长期运行组件。

建议：

```text
Scheduler Loop
每 1 秒运行一次
```

30 节点规模下足够。

------

# 44. Scheduler 主流程

```text
┌─────────────────────┐
│ Scheduler Tick      │
└─────────┬───────────┘
          │
          ▼
读取所有 WAITING TaskNode
          │
          ▼
按 node_id 分组
          │
          ▼
每个 Node 取 FIFO 第一条
          │
          ▼
检查 Node 状态
          │
     ┌────┼─────┐
     │          │
 Offline      Online
     │          │
     ▼          ▼
  Failed     获取 Node Lock
                │
          ┌─────┴─────┐
          │           │
        Failed      Success
          │           │
        保持         创建/启动
       Waiting        Attempt
```

------

# 45. Scheduler 伪逻辑

逻辑示意：

```text
for each node having waiting_task_nodes:

    task_node = earliest_waiting_task_node(node)

    if node.disabled:
        continue

    if node.offline:
        mark_failed(task_node, NODE_OFFLINE)
        continue

    if node_lock_exists(node):
        continue

    if acquire_node_lock(node, task_node):
        start_task_node(task_node)
```

------

# 46. 为什么 Offline 直接失败

此前需求已经确定：

```text
Offline Node
允许进入任务
但执行时直接 Failed
```

因此 Scheduler 不应该：

```text
一直 Waiting 等节点上线
```

否则就变成了条件调度系统。

V1 规则：

```text
调度时发现 Offline
→ FAILED / NODE_OFFLINE
```

后续人工 Retry。

------

# 47. Disabled 处理

正常情况下 Disabled Node 创建 Task 时已经不能选择。

如果 Task 创建后节点被人工 Disabled，而该 TaskNode 尚在 Waiting：

建议：

```text
保持 WAITING
不自动执行
```

Web 显示：

```text
Waiting - Node Disabled
```

用户可以：

```text
重新 Enable
```

后继续调度，或者：

```text
Cancel Waiting
```

不应自动标记 Failed。

------

# 48. 节点在 Running 期间被 Disabled

如果 TaskNode 已：

```text
RUNNING
```

此时用户 Disable Node：

```text
不影响当前运行任务
```

Disabled 只阻止：

```text
后续新的 TaskNode
```

V1 不停止已运行脚本。

------

# 49. Execution Engine 启动

Scheduler 获得节点锁后：

```text
TaskNode WAITING
     ↓
创建 Attempt
     ↓
Attempt RUNNING
     ↓
TaskNode RUNNING
     ↓
启动 Execution Engine
```

------

# 50. Execution Engine 阶段

一个 Attempt 建议内部拆成：

```text
PREPARING
EXECUTING
FINALIZING
```

这三个可以作为：

```text
runtime_phase
```

内部字段，不需要作为用户主状态。

------

# 51. PREPARING

包括：

```text
检查 Package
创建远端工作目录
下载 payload.tar.gz
解压 payload
检查解释器
准备日志目录
```

任何 Preparation 失败：

```text
Attempt = FAILED
TaskNode = FAILED
```

failure_reason 保存具体原因。

------

# 52. EXECUTING

按 PackageStep 顺序：

```text
for step in steps:
    execute(step)
```

不允许并行执行同一节点的多个 Step。

------

# 53. FINALIZING

负责：

```text
记录最终状态
保存 exit_code
完成日志采集
成功时清理工作目录
失败时保留工作目录
释放 Node Lock
重新聚合 Task
```

------

# 54. Step 启动流程

```text
Step WAITING
      ↓
生成 stdout/stderr 路径
      ↓
调用 Salt Async Job
      ↓
获得 salt_jid
      ↓
Step RUNNING
      ↓
持续跟踪
```

必须做到：

```text
salt_jid 在执行早期持久化
```

这样服务重启才能恢复。

------

# 55. Salt JID

每个实际远程 Step 保存：

```text
salt_jid
```

关系：

```text
TaskStepResult
     │
     └── salt_jid
             │
             ▼
         Salt Job
```

禁止仅把 JID 放在进程内存里。

------

# 56. Step 执行完成

检测到 Salt Job 完成：

```text
获取 exit_code
获取最终 stdout
获取最终 stderr
```

然后：

```text
exit_code == 0
→ SUCCESS

exit_code != 0
→ FAILED
```

------

# 57. Timeout 处理

每个 Step：

```text
deadline =
started_at + effective_timeout
```

Execution Engine 周期检查：

```text
now > deadline
```

则：

```text
Step = FAILED
failure_reason = STEP_TIMEOUT
```

然后根据 `failure_action`：

```text
stop
或
ignore
```

V1 不要求复杂远程进程强杀语义。

但实现时应尽量终止对应远程命令，避免平台已判超时、节点进程仍永久运行。

------

# 58. Salt 通信短暂失败

运行期间如果 Salt API 查询暂时失败：

```text
不能立即把 Step 判定 Failed
```

建议引入：

```text
salt_query_retry_count
```

例如连续若干次查询失败后再进入异常处理。

短暂失败期间：

```text
Step 仍保持 RUNNING
```

同时日志记录：

```text
SALT_COMMUNICATION_WARNING
```

------

# 59. Salt 通信长期失败

超过系统允许的查询失败窗口后：

```text
failure_reason = SALT_COMMUNICATION_FAILED
```

但在决定是否 Failed 前应尽量检查：

```text
远端 exit marker
Salt Job Cache
```

避免重复执行。

------

# 60. 运行日志

Step 运行时：

```text
stdout
stderr
```

持续写入远端运行目录。

例如：

```text
runtime/attempt-1/
├── step-01.stdout
├── step-01.stderr
└── step-01.exit
```

Automation Center 增量获取日志。

------

# 61. 实时日志状态与任务状态解耦

实时日志读取失败：

```text
不等于任务执行失败
```

例如：

```text
日志 API 暂时失败
Salt Job 仍正常运行
```

则：

```text
Step = RUNNING
```

Web 只提示：

```text
实时日志暂时不可用
```

不能因此重跑任务。

------

# 62. Waiting Cancel

允许：

```text
TaskNode WAITING
→ CANCELLED
```

取消操作必须事务化：

```text
UPDATE task_nodes
SET status = CANCELLED
WHERE id = ?
AND status = WAITING
```

只有实际影响一行时才算取消成功。

这样可避免：

```text
用户点击 Cancel
同时 Scheduler 正好开始执行
```

造成状态冲突。

------

# 63. Scheduler 与 Cancel 竞争

典型竞争：

```text
T1: Scheduler 准备启动
T2: User 点击 Cancel
```

必须通过数据库条件更新解决。

Scheduler 获取执行资格时：

```text
UPDATE task_nodes
SET status = RUNNING
WHERE id = ?
AND status = WAITING
```

如果更新行数为 0：

```text
说明已经被取消
→ 不执行
```

------

# 64. Retry 与并发控制

Retry 时：

```text
TaskNode 当前必须为 FAILED
```

禁止：

```text
RUNNING → Retry
WAITING → Retry
SUCCESS → Retry
```

Retry API 使用事务：

```text
验证状态
验证 Package Revision
创建 Attempt N+1
TaskNode → WAITING
```

------

# 65. Retry 与节点队列

Retry 不获得特殊优先级。

例如：

```text
compute01:

Task-B Waiting
Task-C Waiting
```

此时 Task-A Retry：

```text
创建时间晚于 B/C
```

因此按 FIFO：

```text
B
C
A Retry
```

V1 不允许 Retry 插队。

------

# 66. Copy Task

Copy 与 Retry 不同。

Copy：

```text
创建新的 Task
```

新的 TaskNode：

```text
全部重新进入正常 FIFO
```

不继承旧 Task 的 Attempt。

------

# 67. Package Update 锁

若存在某 Package 的：

```text
TaskNode WAITING
或
TaskNode RUNNING
```

则禁止 Package Update。

判断必须基于：

```text
Package ID + Revision Snapshot
```

------

# 68. Package Delete 锁

同理：

```text
存在 WAITING / RUNNING Task
→ Delete Forbidden
```

终态：

```text
SUCCESS
PARTIAL_SUCCESS
FAILED
CANCELLED
```

不阻止 Package 删除。

------

# 69. 服务重启恢复

Automation Center 启动时第一阶段：

```text
进入 RECOVERY MODE
```

此时：

```text
Scheduler 暂不调度新的 Waiting TaskNode
```

先恢复旧 Running 状态。

------

# 70. Recovery 流程

```text
Application Start
      ↓
打开 SQLite
      ↓
读取所有 RUNNING TaskNode
      ↓
读取 Running Step
      ↓
检查 salt_jid
      ↓
查询 Salt Job / 远端 marker
      ↓
恢复状态
      ↓
重建 Node Lock
      ↓
完成 Recovery
      ↓
启动 Scheduler
```

------

# 71. Node Lock 恢复

如果数据库中：

```text
TaskNode = RUNNING
```

但：

```text
node_execution_locks
```

不存在，Recovery 必须重新创建锁。

原则：

```text
RUNNING TaskNode
→ 必须对应 Node Lock
```

------

# 72. 孤儿 Node Lock

如果发现：

```text
Node Lock 存在
但关联 TaskNode 已 SUCCESS/FAILED/CANCELLED
```

说明是异常残留。

Recovery：

```text
删除该 Lock
```

------

# 73. Running Step 恢复场景 1

Salt Job：

```text
仍在运行
```

则：

```text
Step 保持 RUNNING
重新启动状态监控
重新启动实时日志读取
```

不得重新下发。

------

# 74. Running Step 恢复场景 2

Salt Job：

```text
已完成
```

则：

```text
读取 Job Result
更新 Step
继续 Attempt 后续流程
```

如果 Step Success：

```text
继续下一 Step
```

------

# 75. Running Step 恢复场景 3

Salt Job Cache 不存在。

此时检查目标节点：

```text
step.exit
stdout
stderr
```

如果有明确 exit marker：

```text
根据 marker 恢复最终结果
```

------

# 76. Running Step 恢复场景 4

Salt Job：

```text
不存在
```

远端：

```text
也不存在执行结果
```

则：

```text
Step = FAILED
failure_reason = EXECUTION_STATE_LOST
```

禁止自动重新下发当前 Step。

原因：

> 无法确认旧命令是否已经执行过，自动重复执行可能产生破坏性副作用。

------

# 77. Recovery 后 failure_action

如果恢复得到：

```text
Step FAILED
```

继续按照原：

```text
failure_action
```

处理。

例如：

```text
ignore
→ 继续下一 Step

stop
→ Attempt Failed
```

------

# 78. Automation Center 崩溃期间 Step 已完成

典型场景：

```text
Automation Center Down
    ↓
Minion Step 执行结束
    ↓
Automation Center Start
```

Recovery 应通过：

```text
Salt Job Result
或
remote marker
```

取得真实结果。

------

# 79. SQLite 模式

建议启动时：

```text
PRAGMA journal_mode=WAL;
```

目的：

```text
提高读写并发能力
```

当前规模下不需要数据库连接池做复杂调优。

------

# 80. SQLite 写入原则

必须遵循：

```text
事务短
状态更新小
日志正文不入 SQLite
```

避免：

```text
一个 5 分钟事务持有数据库写锁
```

执行远程 Salt Job 时绝对不能一直保持 SQLite transaction。

------

# 81. 状态转换事务原则

推荐：

```text
BEGIN
更新状态
写关键关联信息
COMMIT

然后执行外部 Salt API
```

对于外部调用返回后再开启新事务更新结果。

不要：

```text
BEGIN
调用 Salt 10 分钟
COMMIT
```

------

# 82. 状态 CAS

所有关键状态切换建议采用：

```text
Compare-And-Set
```

例如：

```text
WAITING → RUNNING
```

SQL 语义：

```text
UPDATE ...
SET status='RUNNING'
WHERE id=?
AND status='WAITING'
```

避免并发 Worker 重复启动。

------

# 83. Scheduler 单实例

V1 Automation Center：

```text
单 Docker 实例
```

因此 Scheduler 也只运行一份。

即使如此仍应依赖数据库锁和 CAS，而不是假设永远不会出现并发请求。

------

# 84. Scheduler Tick

建议默认：

```text
1 秒
```

每次只扫描：

```text
WAITING TaskNode
```

目标规模 30 台时性能压力很低。

------

# 85. Scheduler 不执行长任务

Scheduler 只负责：

```text
发现候选
获取 Lock
提交执行
```

不得：

```text
Scheduler 主循环自己同步等待脚本执行完成
```

否则某个 30 分钟 Step 会阻塞全部调度。

------

# 86. Execution Worker

每个 Running TaskNode 由独立 Execution Worker 管理。

逻辑：

```text
Scheduler
   ↓
submit(TaskNode)
   ↓
Worker
```

Worker 数量无需用户配置。

因为 Node Lock 已保证：

```text
最大有效并发 ≈ 在线 Node 数量
```

当前最多约 30。

------

# 87. Worker 上限

虽然最多 30 节点，仍建议内部设置：

```text
max_execution_workers
```

默认：

```text
32
```

该参数可以内部固定，V1 不必暴露 Web 设置。

------

# 88. Node Offline 检查时机

Task 创建确认页：

```text
展示最近 Online/Offline 状态
```

但真正执行前 Scheduler 必须：

```text
再次确认当前 Node 状态
```

防止：

```text
创建任务时 Online
5 秒后节点宕机
```

------

# 89. Salt Ping 与真正执行

即使：

```text
test.ping = True
```

也不能保证下一次命令一定成功。

因此：

```text
Online 只是调度参考
```

实际 Salt 执行失败仍需要独立：

```text
SALT_COMMUNICATION_FAILED
```

处理。

------

# 90. Task 计数字段

Task 中：

```text
target_node_count
success_count
failed_count
```

不建议由业务代码到处手工加减。

每次聚合时重新计算：

```text
COUNT(TaskNode WHERE status=...)
```

当前只有 ≤30 节点，成本极低。

这样不容易因为异常重试导致计数漂移。

------

# 91. TaskNode has_warning

建议增加：

```text
has_warning BOOLEAN
```

场景：

```text
Step2 Failed
failure_action=ignore
Step3 Success
```

最终：

```text
TaskNode:
status = SUCCESS
has_warning = true
```

Web：

```text
Success ⚠
```

------

# 92. Task Warning

Task 顶层不新增：

```text
SUCCESS_WITH_WARNING
```

避免状态过多。

可以动态统计：

```text
warning_node_count
```

展示：

```text
Success
2 nodes with warnings
```

------

# 93. failure_reason

平台 failure_reason 推荐枚举：

```text
NODE_OFFLINE
NODE_DISABLED

PACKAGE_NOT_FOUND
PACKAGE_REVISION_CHANGED
PACKAGE_TRANSFER_FAILED
PACKAGE_EXTRACT_FAILED

SHELL_NOT_FOUND
PYTHON_NOT_FOUND

STEP_TIMEOUT
STEP_EXIT_NONZERO

SALT_API_UNAVAILABLE
SALT_COMMUNICATION_FAILED

EXECUTION_STATE_LOST

INTERNAL_ERROR
```

------

# 94. Step failure_reason

Step 失败至少区分：

```text
STEP_TIMEOUT
STEP_EXIT_NONZERO
SALT_COMMUNICATION_FAILED
INTERPRETER_NOT_FOUND
EXECUTION_STATE_LOST
```

而具体业务错误：

```text
由 stderr/stdout 体现
```

Automation Center 不解析业务错误文本。

------

# 95. Task 终态不可逆原则

除 Retry 外：

```text
TaskNode SUCCESS
TaskNode FAILED
TaskNode CANCELLED
```

不得直接修改回 Running。

Retry 通过：

```text
新建 Attempt
```

使 TaskNode：

```text
FAILED → WAITING → RUNNING
```

保留完整历史。

------

# 96. Step 终态不可修改

历史 Attempt 中：

```text
Step SUCCESS
Step FAILED
Step SKIPPED
```

一旦完成：

```text
永不覆盖
```

Retry 创建新的 Attempt/StepResult。

------

# 97. Task 状态可能因 Retry 改变

例如：

```text
Task = PARTIAL_SUCCESS

compute01 Success
compute02 Failed
```

Retry compute02：

```text
compute02 → WAITING
```

Task：

```text
RUNNING
```

Retry 成功后：

```text
compute02 → Success
```

Task：

```text
SUCCESS
```

因此：

```text
Task 顶层状态不是永久终态
```

只要用户发起合法 Retry，就可能重新进入 Running。

------

# 98. Retry 后状态变化

完整：

```text
PARTIAL_SUCCESS
      │
      │ Retry Failed Node
      ▼
    RUNNING
      │
   ┌──┴───┐
   │      │
Success  Failed
   │      │
   ▼      ▼
SUCCESS PARTIAL_SUCCESS
```

------

# 99. Failed Task Retry

如果 Task：

```text
FAILED
```

其中某些节点 Retry：

```text
Task → RUNNING
```

最后如果：

```text
部分成功
```

则：

```text
PARTIAL_SUCCESS
```

如果全部成功：

```text
SUCCESS
```

------

# 100. Waiting Cancel 后聚合

例如：

```text
compute01 Running
compute02 Waiting
```

取消 compute02：

```text
compute02 Cancelled
```

Task 仍：

```text
RUNNING
```

compute01 Success 后：

```text
Success + Cancelled
```

Task：

```text
PARTIAL_SUCCESS
```

------

# 101. 核心状态图

```text
                           TASK
                            │
                ┌───────────┼────────────┐
                │           │            │
              WAITING    RUNNING      TERMINAL*
                            │
                ┌───────────┼───────────────┐
                ▼           ▼               ▼
             SUCCESS  PARTIAL_SUCCESS     FAILED
                                          CANCELLED

* Retry 可以使 FAILED/PARTIAL_SUCCESS 再次进入 RUNNING
```

TaskNode：

```text
WAITING
   │
   ├──────── cancel ───────► CANCELLED
   │
   ▼
RUNNING
   │
   ├───────────────────────► SUCCESS
   │
   └───────────────────────► FAILED
                                │
                                │ Retry
                                ▼
                              WAITING
```

Step：

```text
WAITING
   ▼
RUNNING
   ├────────────► SUCCESS
   │
   └────────────► FAILED
                    │
                    ├─ ignore → 下一 Step
                    │
                    └─ stop   → 后续 SKIPPED
```

------

# 102. 调度器核心不变量

实现中必须保证以下不变量始终成立。

### 不变量 1

```text
每个 node_id
最多一个有效 Node Execution Lock
```

### 不变量 2

```text
RUNNING TaskNode
必须存在对应 Node Lock
```

### 不变量 3

```text
同一 TaskNode
最多一个 RUNNING Attempt
```

### 不变量 4

```text
同一 Attempt
最多一个 RUNNING Step
```

### 不变量 5

```text
Retry 永远创建新 Attempt
不修改历史 Attempt
```

### 不变量 6

```text
Task 状态由 TaskNode 聚合
```

### 不变量 7

```text
Step 执行必须保存 Salt JID
```

### 不变量 8

```text
Automation Center 重启不得重新执行状态不明确的 Step
```

------

# 103. 最终执行示例

Task：

```text
fix-nova-v2
```

目标：

```text
compute01
compute02
compute03
```

初始：

```text
Task WAITING

compute01 WAITING
compute02 WAITING
compute03 WAITING
```

Scheduler：

```text
compute01 Lock Success
compute02 当前有其他任务 → Waiting
compute03 Lock Success
```

变成：

```text
Task RUNNING

compute01 RUNNING
compute02 WAITING
compute03 RUNNING
```

compute01：

```text
precheck Success
fix Success
verify Success

→ SUCCESS
```

compute03：

```text
precheck Success
fix Failed(stop)
verify Skipped

→ FAILED
```

随后 compute02 获得锁：

```text
RUNNING
```

最终成功：

```text
compute01 Success
compute02 Success
compute03 Failed
```

Task：

```text
PARTIAL_SUCCESS
```

用户 Retry compute03：

```text
Attempt 2
从 Step1 开始
```

Task：

```text
RUNNING
```

Retry 成功：

```text
compute03 Success
```

最终：

```text
Task SUCCESS
```

完整历史仍保留：

```text
compute03
├── Attempt 1 Failed
└── Attempt 2 Success
```

------

# 104. 设计结论

Automation Center V1 调度器本质不是复杂 Workflow Engine。

核心只需要实现：

```text
SQLite 状态机
+
Node FIFO Queue
+
Node Execution Lock
+
Execution Worker
+
Salt JID Tracking
+
Recovery
```

完整核心链路：

```text
Task
  ↓
TaskNode
  ↓
Node FIFO
  ↓
Node Lock
  ↓
Attempt
  ↓
Step
  ↓
Salt Async Job
  ↓
Result
  ↓
State Aggregation
```

这一设计可以满足当前 ≤30 节点规模，同时保持足够简单，并为后续增加 Ansible Executor、更多角色及更大规模节点留下扩展空间。