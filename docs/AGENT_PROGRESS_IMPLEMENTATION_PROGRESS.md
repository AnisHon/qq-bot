# Agent Progress V1 实施进度

工作区基线：HEAD 39e735b；启动时存在用户未提交的 Context V2、Web Research/Tavily 与 Agent 相关改动。本实施在其上增量修改，不覆盖这些变更。此文件记录本轮真实状态，不代表部署状态。

## 总览

| Task | 状态 | 主要交付 | 测试 | 阻塞 / 下一步 |
|---|---|---|---|---|
| AP-01 | COMPLETED | AgentEvent、TaskIdentity、request sink；UUID task_id 传入 WorkingState | 多轮 targeted unittest 通过 | 已接通下游事件生产 |
| AP-02 | COMPLETED | Registry 校验、Semaphore、Handler、裁剪后结果生命周期事件 | Registry targeted tests 通过 | 真实执行边界完成 |
| AP-03 | COMPLETED | Agent/WorkingState/Compaction 事件；移除旧前置提示 | Agent、Compaction、WorkingState 回归通过 | 无旧 on_tool_call 播报路径 |
| AP-04 | COMPLETED | Coordinator、主动唤醒、状态归约、有界 mailbox、虚拟时钟 | `tests.test_progress_coordinator` 通过 | 已进入 Reporter 集成 |
| AP-05 | COMPLETED | QQ Reporter、共享群发送屏障、Reset/Purge fence、终态分离 | Fake Adapter 锁竞争/失败/epoch 测试通过 | 真实 NapCat 远端顺序未验证 |
| AP-06 | COMPLETED | 群级 progress_mode、`/agent status`、权限过滤 | Command tests 通过 | 不需要数据库迁移 |
| AP-07 | COMPLETED | 静态安全阶段文案，balanced/verbose 差异；PublicPlan 保留扩展契约 | Presentation/Fake tests 通过 | V1 无额外模型调用 |
| AP-08 | COMPLETED | Fake Agent→Registry→Coordinator→Reporter 集成；准确模型阶段文案；完整回归 | 全量 483 tests 通过；进度核心定向套件 26 tests 通过 | 无代码阻塞；NapCat 实际远端送达顺序尚未现场验证 |

## 基线检查

- 读取 AP 设计、任务、命令和审计文档；源码现状以当前工作区为准。
- 已新增 app/progress 包；`on_tool_call` 提前提示及 CommandService Registry 前通用提示已移除。
- 已有 Dirty 工作区文件保持不变；实施修改必须落在必要文件的最小 diff。
- 规划修订 A-D 已补充到设计/任务文档：主动唤醒、唯一 delivery 锁序、执行/持久化/QQ 投递状态分离、PublicPlan 版本边界。

## 实施记录

### AP-01

- 新增 app/progress/events.py 与进度 contract 单测。
- BotService 为普通消息和 scheduled task 创建独立 UUID task_id；request_id 保持 OneBot/occurrence 关联。
- LLMHandler 将 identity 传入 ContextManager 与 Agent 调用；ContextManager 写入同一 WorkingState.task_id。
- 通过：timeout 60 .venv/bin/python -m unittest tests.test_progress_events tests.test_working_memory tests.test_context_manager tests.test_bot_service -q；51 tests passed。
- 未做数据库迁移；向后兼容未传 identity 的直接 ContextManager/LLM 调用。

### AP-02

- ToolSpec 增加固定 ToolCategory；既有工具按名称映射到静态类别。
- ToolRegistry 在拒绝、Semaphore 获取后、handler 结果裁剪后以及错误/取消/超时路径发结构化事件。
- 事件仅带类别、状态、执行标志、Evidence 数和 completeness，不带参数/异常正文。
- 通过：timeout 90 .venv/bin/python -m unittest tests.test_progress_events tests.test_tool_registry tests.test_agent tests.test_agent_integration tests.test_working_memory tests.test_context_manager tests.test_bot_service -q；82 tests passed。

### AP-03

- Agent 已输出模型请求、Compaction、checkpoint、Agent skip/reject 事件；LLMHandler 与 BotService 开始使用任务级身份/终态。
- 删除 Agent Loop 在 Registry 之前调用 on_tool_call 的播报行为；移除 CommandService 的提前“正在查询”及 main 的旧等待提示。
- 通过：timeout 60 .venv/bin/python -m unittest tests.test_agent tests.test_tool_registry tests.test_progress_events tests.test_bot_service -q（65 passed）；timeout 60 .venv/bin/python -m unittest tests.test_agent_compaction tests.test_agent_integration tests.test_working_memory tests.test_context_manager -q（27 passed）。
- 组合回归曾因 handler 事件导入缺失卡住并中断；补齐导入后 Agent/Compaction/WorkingState 目标套件通过。Command/Main/TaskRuntime 入口及三类状态分离已在 AP-05～AP-08 回归中复核。

### AP-04

- 新增请求级 reducer、task/tool 状态聚合、sequence 去重、quiet/balanced/verbose、固定 deadline scheduler、虚拟时钟与 32 项有界 mailbox。
- 首次进度由 scheduler 在 3 秒门槛主动唤醒；任务在门槛前结束不会补发。单 task 最多 3 条候选、事件最多 192 项。
- 通过：`timeout 60 .venv/bin/python -m unittest tests.test_progress_coordinator -q`；最终核心覆盖见 AP-08 的 26 项定向结果。

### AP-05

- 新增两个固定 Reporter worker；状态先经同群 8 秒和全局 30 次/60 秒限制，再通过 QQMessageDispatcher。
- Dispatcher 的普通 send、状态与 final 共用群级 barrier；final 先关闭状态门，Reporter 在 Adapter 前重查 task/sequence/epoch。
- Context reset/purge 在同一群 barrier 内建立短期 event fence，SQLite epoch 更新后失效旧 task；发送 HTTP action 被记录为 Adapter accepted，不声称客户端已展示。
- 执行终态、WorkingState checkpoint 状态和最终 QQ 投递状态分别保存到 Coordinator 安全运行态；状态失败不改写 Agent 终态。
- 通过：`tests.test_progress_reporter` 与 `tests.test_dispatcher` 的 Fake Adapter 竞态、失败、限流和 shutdown 测试。

### AP-06 / AP-07

- `/config show` 增加 progress_mode；`/config set progress_mode quiet|balanced|verbose` 管理员可用，存于现有 `group_settings`。
- `/agent status` 读取 Coordinator 实时状态；普通成员只看本人，管理员仅看本群聚合，不返回目标/query/用户身份。查询排除当前 status 命令本身。
- 实现静态安全文案。V1 不读普通模型 content、reasoning、工具参数或网页正文，不增加 LLM 请求。
- reset 确认通过 Dispatcher barrier 和 Coordinator epoch fence。无需 schema migration。
- 通过：定向 Command、Main、Dispatcher、Progress 测试均通过。

### AP-08

- 新增 `tests/test_agent_progress_integration.py`：Fake LLM 触发真实 Agent Loop 与 Tool Registry；Fake Clock 推进到 3 秒播报门槛；Fake QQ Adapter 验证阶段消息先于最终答案，并验证工具 Query / Tool 内容未进入进度文本。
- 最终审查发现并修正一个状态文案问题：只有模型请求仍活跃且没有已完成工具时，显示“正在生成回复”，不再声称工具处理已经完成；新增测试覆盖。
- 22 项验收场景由分层 Fake/Fixture 测试与既有回归覆盖；不是一次真实 NapCat 端到端演练。锁竞争、epoch fence、终态与投递状态拆分、限流、超时/取消、Fixture 数据缺失和全量回归均有测试证据。
- 定向回归：`timeout 60 .venv/bin/python -m unittest tests.test_progress_coordinator tests.test_progress_reporter tests.test_agent_progress_integration -q`，26 tests passed。
- 完整回归：`timeout 300 .venv/bin/python -m unittest discover -s tests -q`，483 tests passed（12.013s）。测试期间有预期故障注入日志、FastAPI deprecation warnings 和部分旧测试的 SQLite ResourceWarning；无失败。
- 静态检查：本轮涉及模块和测试 `compileall` 成功；`git diff --check` 成功。当前 `.venv` 未安装 Ruff，因此未运行 Ruff。
- 结果边界：Fake/本地测试通过；没有真实模型、Tavily、QQ/NapCat、生产数据库或部署验证。HTTP Adapter 接受不能证明 QQ 客户端显示。

## 恢复位置

当前：AP-01～AP-08 均完成并验收。所有本地可验证的代码工作已完成；没有部署。下一步由人工决定是否安排 NapCat 现场验证。
