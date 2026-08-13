# V1 需求—实现—测试追踪

| 需求域 | 主要实现 | 自动化验证 |
|---|---|---|
| 固定账号、Session、CSRF | `security.py`、认证 API、Nginx 安全头 | `test_auth_session_and_csrf` |
| Pending Key 与节点状态 | Salt Adapter、Nodes API、后台探测 | `test_node_package_task_scheduler_flow` |
| Role 人工设置 | NodeRole、Nodes API | 节点闭环测试、API 测试 |
| 维护包 SHA/Manifest | `package_service.py` | `test_package_security.py` |
| 上传与解压安全 | 流式写盘、成员/路径/大小/数量限制 | 坏摘要、路径穿越、版本和 Executor 测试 |
| Task 幂等与确认 | Task Preview/Create API、请求 Hash | 重放和 409 冲突测试 |
| 节点级 FIFO | `queue_seq`、NodeExecutionLock、CAS | 同节点防越序、重复 Claim、Scheduler/Cancel 竞争测试 |
| Shell/Python Step | Scheduler、Fake/HTTP Salt Adapter | Fake Salt 端到端、HTTP Adapter 契约；真实 Salt 列为目标环境验收 |
| 实时日志 | 本地日志文件、增量采集、SSE | 后端日志查询测试；SSE 断线续传列为扩展自动化 |
| Cancel/Retry/Copy | Task/TaskNode API | 取消与 Retry 状态守卫测试 |
| 重启恢复 | Salt JID 持久化、Scheduler Recover | 恢复专项测试 |
| Settings/Audit | SystemSetting、AuditLog API/UI | 设置范围、掩码和审计查询测试 |
| SQLite 升级 | Alembic、启动前 SQLite Backup | 迁移与备份专项测试 |

完整场景、预期结果、执行层级及当前验收状态见 [`test-cases.md`](test-cases.md)。其中“已通过”只表示本地可重复自动化结果；真实 Salt、CentOS 容器构建和 30 节点容量项不会由 Fake Salt 结果替代。

前端浏览器测试使用确定性 API Mock 覆盖登录、节点接入、维护包上传、任务创建和结果页；后端与 Fake Salt 的真实 API/数据库闭环由 pytest 独立覆盖。
