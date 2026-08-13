# 云平台自动化维护中心 V1 完整测试用例

## 1. 执行口径

- `A-已通过`：已纳入 pytest、Vitest 或 Playwright，并在当前工作区通过。
- `M-待验收`：必须在目标 CentOS 9、真实 salt-api/minion、TLS 或容量环境执行。
- `E-扩展自动化`：实现已具备，当前版本需要补充更细粒度自动化，不冒充已通过。
- 每个 API 时间字段校验 UTC RFC3339；前端展示统一校验 Asia/Shanghai。
- 写接口除登录外均带有效 Session 与 CSRF；负向用例分别遗漏 Cookie、遗漏 Token、Token 不匹配。

## 2. 认证、会话与通用安全

| ID | 场景与操作 | 预期结果 | 层级/状态 |
|---|---|---|---|
| AUTH-01 | 首次启动传入固定账号环境变量 | 仅首次初始化；密码为 Argon2id Hash | 集成/A-已通过 |
| AUTH-02 | 正确账号密码登录 | 创建服务端 Session，返回 CSRF，设置 HttpOnly/Secure/SameSite=Strict Cookie | API/A-已通过 |
| AUTH-03 | 错误账号或密码登录 | 返回 401，不泄露账号是否存在 | API/A-已通过 |
| AUTH-04 | 未登录访问受保护接口 | 返回 401 Problem 响应 | API/A-已通过 |
| AUTH-05 | 写请求不带或伪造 CSRF | 返回 403，业务数据不变 | API/A-已通过 |
| AUTH-06 | 空闲超过 8 小时 | Session 失效并要求重新登录 | 单元/A-已通过 |
| AUTH-07 | Session 总寿命超过 24 小时 | 即使持续访问也必须重新登录 | 单元/A-已通过 |
| AUTH-08 | 登出后重放原 Cookie | Session 已删除，返回 401 | API/A-已通过 |
| AUTH-09 | 容器 CLI 重置密码 | 旧 Session 清理，账号与新密码 Hash 写入 | CLI/A-已通过 |
| AUTH-10 | API 异常、校验错误与资源冲突 | 统一 Problem JSON，不返回堆栈或密钥明文 | API/E-扩展自动化 |

## 3. 节点、Key 与角色

| ID | 场景与操作 | 预期结果 | 层级/状态 |
|---|---|---|---|
| NODE-01 | 查询 Salt Pending Keys | 只返回待处理 Key | API/A-已通过 |
| NODE-02 | Accept Pending Key | Salt 接受成功并创建/更新 Node | 集成/A-已通过 |
| NODE-03 | Reject Pending Key | Salt 拒绝成功，不创建可调度 Node | Adapter/A-已通过；API E-扩展自动化 |
| NODE-04 | 刷新在线节点 | 一次批量 `test.ping` 更新 Online/Offline，不读取 grains/进程/服务 | 集成/A-已通过 |
| NODE-05 | Accept Key 采集基础资料 | 仅首次执行 `grains.item host ipv4`，保存 Hostname/IP | Adapter/A-已通过 |
| NODE-06 | 人工增加/删除角色 | 自动和人工标签取并集；保留未删除标签来源，新标签标记 manual | API/A-已通过 |
| NODE-07 | Disable 节点 | 节点不再进入 Preview/Create 目标集 | API/A-已通过 |
| NODE-08 | Enable 节点 | 节点重新允许被选择 | API/A-已通过 |
| NODE-09 | 删除无活动任务的 Offline 节点 | 页面仅对 Offline 节点显示删除入口；删除当前 Node 并写审计，历史任务保留 | API/UI/A-已通过 |
| NODE-10 | 删除 Online 节点或被 Waiting/Running 引用的 Offline 节点 | 返回 409，节点保留 | API/A-已通过 |
| NODE-11 | Salt 临时不可用时刷新 | 返回可识别错误，旧节点状态不被错误覆盖 | 集成/E-扩展自动化 |
| NODE-12 | 真实 Minion Key 接受与发现 | 页面与 API 能发现目标 Minion | 真实 Salt/M-待验收 |
| NODE-13 | 创建自动角色识别任务 | 快照所有节点和启用规则；ONLINE 含 Disabled 进入目标，Offline 记录跳过 | API/A-已通过 |
| NODE-14 | 重复创建识别任务 | 同时只允许一个 Waiting/Running 任务，返回 409 和活动任务 ID | API/A-已通过 |
| NODE-15 | 批量进程扫描 | 单次 list targeting `cmd.run_all`，15 秒超时，不调用 grains/service | Adapter/A-已通过；真实 Salt/M-待验收 |
| NODE-16 | 自动与人工标签合并 | 自动只补缺失标签、不重复、不删除；人工标签不被扫描覆盖 | Worker/A-已通过 |
| NODE-17 | 自动标签人工删除后重新扫描 | 进程仍匹配时重新添加 auto 标签 | Worker/A-已通过 |
| NODE-18 | 节点级部分失败 | 成功节点落库，失败节点记录原因，Job 为 PARTIAL_FAILED | Worker/A-已通过 |
| NODE-19 | 识别期间应用重启 | RUNNING Job 收敛为 FAILED/EXECUTION_STATE_LOST，不自动重复扫描 | 恢复/A-已通过 |
| NODE-20 | 角色规则管理 | 仅接受 process 规则；自由标签校验、重复规则和 service 规则被拒绝 | API/UI/A-已通过 |
| NODE-21 | 进程原文保密 | 原始命令行不写数据库、审计或日志，仅保存匹配和新增角色 | Worker/A-已通过 |

## 4. 维护包上传、校验与生命周期

| ID | 场景与操作 | 预期结果 | 层级/状态 |
|---|---|---|---|
| PKG-01 | 上传合法外层包 | 流式落盘、校验成功、Revision=1、解析 Steps | 集成/A-已通过 |
| PKG-02 | 外层不是恰好一个 `.tar`/`.tar.gz` 和一个 `*sha256` 文件 | 返回 422，临时文件清理 | API/A-已通过 |
| PKG-03 | SHA 行不是 64 位摘要、两个空格和实际包名 | 拒绝上传 | 单元/A-已通过 |
| PKG-04 | 摘要或 SHA 中记录的包名与实际内层包不一致 | 拒绝上传且不创建 Package | API/A-已通过 |
| PKG-04A | 任意内层包名和校验文件名 | 从 `*sha256` 内容读取包名，普通 tar/tar.gz 均通过 | API/A-已通过 |
| PKG-05 | manifest 缺失或超过 1 MiB | 拒绝上传 | 单元；大小上限 A-已通过，缺失 E-扩展自动化 |
| PKG-06 | manifest_version 缺失或不为 1 | 拒绝上传 | API/A-已通过 |
| PKG-07 | Executor 不是 Shell/Python | 拒绝上传 | API/A-已通过 |
| PKG-08 | Step 为空、字段非法或超过 100 | 拒绝上传 | 单元/A-已通过 |
| PKG-09 | Tar 含绝对路径或 `..` | 拒绝路径穿越 | API/A-已通过 |
| PKG-10 | Tar 含符号链接、硬链接或设备文件 | 拒绝成员 | 单元/A-已通过 |
| PKG-11 | Tar 含重复文件名 | 拒绝歧义覆盖 | 单元/A-已通过 |
| PKG-12 | 解压后超过 20 GiB 或文件超过 10 万 | 在解压前/过程中拒绝并清理 | 单元/E-扩展自动化 |
| PKG-13 | 上传超过设置值或 10 GiB 硬上限 | 流式中止，进程不整体载入内存 | API/A-已通过；10GiB/M-待验收 |
| PKG-14 | 同名同版本直接再次创建 | 返回冲突，要求使用 Update | API/A-已通过 |
| PKG-15 | Update 合法包且无活动引用 | 临时校验后原子切换，Revision 单调增加 | API/A-已通过 |
| PKG-16 | Waiting/Running 引用期间 Update | 返回 409，旧文件与 Revision 不变 | 并发/A-已通过 |
| PKG-17 | 删除当前 Package | 当前文件与业务对象删除，历史 Task 快照保留 | API；删除 A-已通过，历史快照 E-扩展自动化 |
| PKG-18 | 更新校验或切换失败 | 数据库和当前文件均保持旧版本 | 故障注入/E-扩展自动化 |

## 5. Preview、创建、幂等与快照

| ID | 场景与操作 | 预期结果 | 层级/状态 |
|---|---|---|---|
| TASK-01 | 仅按 Role 选择 | 命中启用节点并去重 | 单元/A-已通过 |
| TASK-02 | 仅直接选择 Node | 命中启用节点 | 单元/A-已通过 |
| TASK-03 | Role 与直接节点同时选择 | 取并集，按 Node ID 去重 | 单元/A-已通过 |
| TASK-04 | 目标含 Disabled 节点 | 自动排除，不创建 TaskNode | API/A-已通过 |
| TASK-05 | 目标含 Offline 或角色不匹配节点 | Preview 返回确认警告 | API/A-已通过 |
| TASK-06 | 未确认警告直接 Create | 返回 409/422，不创建任务 | API/A-已通过 |
| TASK-07 | Preview 后节点状态或 Revision 改变 | Create 重新校验并拒绝陈旧确认 | 并发/E-扩展自动化 |
| TASK-08 | 合法 Create | Task/TaskNode/Package/Node/Step 快照一次写入 | API；创建 A-已通过，全部快照字段 E-扩展自动化 |
| TASK-09 | 缺少 Idempotency-Key | 返回 422 | API/E-扩展自动化 |
| TASK-10 | 同 Key 同请求重放 | 返回原 Task，并标记 replay | API/A-已通过 |
| TASK-11 | 同 Key 不同请求 | 永久返回 409 | API/A-已通过 |
| TASK-12 | 两个并发请求使用同 Key | 最多创建一个 Task | 并发/A-已通过 |
| TASK-13 | 新建后所有 TaskNode Waiting | Task 保持 Waiting | 单元/A-已通过 |
| TASK-14 | Copy Template | 返回原包 Revision、选择与配置模板，不复制运行状态 | API/A-已通过 |

## 6. 调度、状态机、取消与 Retry

| ID | 场景与操作 | 预期结果 | 层级/状态 |
|---|---|---|---|
| SCH-01 | 同节点有多个 Waiting TaskNode | 按 queue_entered_at/queue_seq FIFO Claim | 调度/A-已通过 |
| SCH-02 | 不同节点同时有任务 | 节点间可并发，同一节点最多一个执行锁 | 调度/E-扩展自动化 |
| SCH-03 | 两个 Scheduler 同时 Claim | 唯一约束与 CAS 保证仅一个获得锁 | 并发/A-已通过 |
| SCH-04 | 获锁并准备执行 | 此时才创建 Attempt | 调度/A-已通过 |
| SCH-05 | Waiting 节点取消 | 变为 Cancelled，不创建空 Attempt | API/A-已通过 |
| SCH-06 | Task 级取消 | 只取消调用瞬间仍 Waiting 的节点 | API/A-已通过 |
| SCH-07 | Task 级取消遇到 Running | 不终止 Running 节点 | API/A-已通过 |
| SCH-08 | Shell Step 成功 | 持久化 JID、结果与日志，进入下一 Step | Fake Salt/A-已通过 |
| SCH-09 | Python Step 成功 | 与 Shell 一致串行收敛 | Fake Salt/A-已通过 |
| SCH-10 | Step 失败且策略 Stop | 当前 TaskNode Failed，后续 Step 不执行 | Fake Salt/A-已通过 |
| SCH-11 | Step 失败且策略 Ignore | 记录失败并继续，最终状态按规则聚合 | Fake Salt/A-已通过 |
| SCH-12 | Step 超时 | 立即 Timeout 失败，best-effort kill 仅记警告 | Fake Salt/A-已通过 |
| SCH-13 | Salt 通信失败 | Attempt/Step 收敛为明确失败原因，不持有长事务 | Fake Salt/E-扩展自动化 |
| SCH-14 | FAILED TaskNode Retry | 新 queue_seq 排在已有 Waiting 之后，从 Step1 新建 Attempt | 调度/A-已通过 |
| SCH-15 | 非 FAILED TaskNode Retry | 返回 409 | API/A-已通过 |
| SCH-16 | Package Update 与任务执行竞争 | 活动引用阻止 Update，执行使用固定 Revision | 并发/A-已通过 |
| SCH-17 | 30 节点调度 | 不丢任务、不破坏每节点串行/FIFO | 容量/M-待验收 |
| SCH-18 | 真实 Salt Shell/Python 多 Step、失败与 Retry | 状态、JID、日志与结果一致 | 真实 Salt/M-待验收 |
| SCH-19 | Scheduler Claim 与 Cancel 同时竞争 | 只能有一个决策生效；Running 有一个 Attempt，Cancelled 无 Attempt | 并发/A-已通过 |

## 7. 日志、SSE 与时间

| ID | 场景与操作 | 预期结果 | 层级/状态 |
|---|---|---|---|
| LOG-01 | 每个 Attempt/Step 输出 stdout/stderr | 写入独立文件且结构化结果保存路径/offset | 集成/A-已通过 |
| LOG-02 | 按 offset 查询日志 | 只返回增量与下一 offset | API；首次读取 A-已通过，非零 offset E-扩展自动化 |
| LOG-03 | SSE 首次连接 | 返回 text/event-stream 和数据事件 | API/A-已通过 |
| LOG-04 | 带 Last-Event-ID 重连 | 从已确认 offset 续传，不重复整段日志 | API/A-已通过 |
| LOG-05 | Nginx 代理 SSE | 禁止响应缓冲，长连接不被普通代理超时截断 | 容器/M-待验收 |
| LOG-06 | 日志访问其他任务的 step_id | 返回 404/403，不能越权读路径 | API/E-扩展自动化 |
| LOG-07 | API 时间字段 | UTC RFC3339，数据库保存 UTC | 单元/E-扩展自动化 |
| LOG-08 | 浏览器展示 | 按 Asia/Shanghai 转换，断线恢复后日志连续 | Playwright/E-扩展自动化 |

## 8. 恢复、清理、配置与审计

| ID | 场景与操作 | 预期结果 | 层级/状态 |
|---|---|---|---|
| REC-01 | 服务在 Salt Job 运行中重启 | 使用持久化 JID 恢复查询，不重复下发 | 调度/A-已通过 |
| REC-02 | 重启后 JID 成功/失败 | 正确收敛并继续或停止后续 Step | 调度；成功恢复 A-已通过，失败恢复 E-扩展自动化 |
| REC-03 | 重启后 JID 无法确认 | 标记 EXECUTION_STATE_LOST，禁止自动重发 | 调度/A-已通过 |
| REC-04 | 成功任务工作目录 | 完成后立即清理 | 清理/A-已通过 |
| REC-05 | 失败工作目录超过 7 天 | 后台清理且结构化历史保留 | 清理/A-已通过 |
| REC-06 | 日志超过配置保留期 | 删除日志文件，不删除 Task/Attempt/Result 历史 | 清理/A-已通过 |
| SET-01 | 查询 Settings | 返回生效值，Salt credential 始终掩码 | API/A-已通过 |
| SET-02 | 更新 Step Timeout 1–86400 | 边界内成功，越界 422 | API/A-已通过 |
| SET-03 | 更新 Salt timeout 1–300 | 边界内成功，越界 422 | API/A-已通过 |
| SET-04 | 更新探测 5–3600、日志 1–365、上传 1MiB–10GiB | 各边界准确校验并持久化 | API/E-扩展自动化 |
| SET-05 | 重启应用 | 持久化 Settings 重新加载并生效 | 集成/E-扩展自动化 |
| AUD-01 | 登录、节点、包、任务、设置写操作 | 记录操作者、动作、对象、时间与摘要 | API；基本写入 A-已通过，逐动作 E-扩展自动化 |
| AUD-02 | 查询审计日志与 limit 边界 | 倒序分页/限制正确，不泄露密钥 | API/A-已通过 |

## 9. 数据库、部署与真实环境验收

| ID | 场景与操作 | 预期结果 | 层级/状态 |
|---|---|---|---|
| DEP-01 | SQLite 启动 | WAL、foreign_keys、busy_timeout 生效 | 单元/E-扩展自动化 |
| DEP-02 | 空库启动 Alembic | 升级到 head 后应用 Ready | 集成/A-已通过 |
| DEP-03 | 已有库升级 | 先用 SQLite Backup API 备份，再迁移 | 集成/A-已通过 |
| DEP-04 | 迁移失败 | 应用拒绝启动，原库和备份可恢复 | 故障注入/E-扩展自动化 |
| DEP-05 | Nginx HTTP 访问 | 308/301 跳转 HTTPS | 容器/M-待验收 |
| DEP-06 | HTTPS Cookie 与安全头 | Secure Cookie 和配置头正确 | 容器/M-待验收 |
| DEP-07 | 容器重启 | `/var/lib/automation-center` 中 DB、包、日志、工作目录、备份均保留 | CentOS/M-待验收 |
| DEP-08 | 应用密钥缺失或仍为开发值 | 生产容器拒绝启动 | 容器/M-待验收 |
| DEP-09 | Salt Fileserver | Package 根目录只读可见，无需复制到 `/srv/salt` | 真实 Salt/M-待验收 |
| DEP-10 | Salt 最小权限 | 允许所需 local_async/jobs/key/test/cmd/cp/grains，其他能力拒绝 | 真实 Salt/M-待验收 |
| DEP-11 | salt-api 短暂不可用并恢复 | 已下发 JID 恢复监控，未下发任务保持 Waiting | 真实 Salt/M-待验收 |
| DEP-12 | 10 GiB 上传 | RSS 不随文件大小线性增长，超限立即停止 | CentOS 容量/M-待验收 |

## 10. 当前回归基线

- 后端：51 个 pytest 用例通过，行覆盖率 89.71%，门槛 85%。
- 前端：4 个 Vitest 用例通过（工具函数与登录组件）；Vite 生产构建通过。
- 浏览器：2 个 Playwright Chrome 用例通过，覆盖登录页冒烟，以及使用确定性 API Mock 的“登录→节点接入→上传包→创建任务→结果页”交互闭环。
- 未声称完成：真实 Salt、CentOS 容器构建/TLS、30 节点与 10 GiB 容量验收。
