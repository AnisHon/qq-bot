# Agent Execution Events 与 QQ Progress V1 实施任务

日期：2026-10-20
任务数：8，分三个迭代。
实现契约：[设计](AGENT_PROGRESS_DESIGN.md)；命令边界：[命令设计](AGENT_PROGRESS_COMMANDS.md)。

## 1. 依赖图与推进规则

~~~mermaid
flowchart LR
  AP01[AP-01 Event Contract / Task Identity] --> AP02[AP-02 Registry Lifecycle Events]
  AP01 --> AP03[AP-03 Agent / WorkingState Events]
  AP02 --> AP03
  AP02 --> AP04[AP-04 ProgressCoordinator]
  AP03 --> AP04
  AP04 --> AP05[AP-05 QQ Reporter / Fencing]
  AP05 --> AP06[AP-06 Commands / Config]
  AP05 --> AP07[AP-07 Deterministic Narratives]
  AP04 --> AP07
  AP06 --> AP08[AP-08 Integrated Acceptance]
  AP07 --> AP08
~~~

AP-02 与 AP-03 都需 AP-01 契约，且会编辑共同的 ToolContext/Agent 接线文件，默认串行。AP-04 等 Registry 与 Agent 两种事件源接通后实现。AP-06 与 AP-07 可在 AP-05 后并行，前提是分别只改命令服务/测试与进度文案/投影/测试。AP-08 汇合回归。没有文件所有权隔离时串行。

每项任务完成后运行针对测试；每个迭代完成后运行相关回归。AP-08 才运行完整 unittest discovery，但不是首次回归入口。以下是后续代码修改范围，本次并未实施。

## 2. Iteration 1：真实执行事件

### AP-01：AgentEvent Contract 与稳定任务身份

- **目标**：建立有版本、有界、按群/用户/epoch 隔离的进程内事件契约；task_id 不再复用 OneBot message_id。
- **修改范围**：新增 **app/progress/contracts.py**；修改 **app/main.py**、**app/bot_service.py**、**app/handlers/llm_handler.py**、**app/context/manager.py**、**app/context/contracts.py**、**app/tools/contracts.py**、**app/commands/contracts.py**。若 scheduled path 无法复用身份，再最小调整 **app/task_runtime.py** 回调签名。
- **依赖**：无。
- **核心接口**：TaskIdentity(task_id, request_id, group_id, user_id, epoch, origin)、版本化 AgentEvent、AgentEventKind、typed payload union、同步 AgentEventSink.publish(event) -> None。ToolContext 增可选 identity/event sink；它们不参与权限判断。
- **实施边界**：每个 Agent/直接工具命令/scheduled occurrence 使用 UUID task_id；保留 request_id 和 BudgetScope 语义；ContextManager 接受 task_id 并写入新 WorkingState；不增加事件表，不改写历史 task 行。
- **验收标准**：OneBot ID 缺失仍生成非空 request_id/task_id；task_id 不复用 message_id；身份 scope 一致且 sequence 单调；错误版本/超限 payload 被拒；无 sink 调用可通过 no-op/default adapter 运行。
- **必要测试**：新增 **tests/test_agent_event_contracts.py**；扩展 **tests/test_main.py**、**tests/test_bot_service.py**、**tests/test_working_memory.py**。覆盖 UUID、缺失 ID、epoch、群隔离、sequence、版本/大小上限、非持久化 snapshot、scheduled identity。
- **完成定义**：所有调用链能携带 identity 至 ToolContext/WorkingState；无需 DB migration；事件中没有原始工具参数。
- **风险**：旧测试可能依赖 task_id=request_id；只改变新 task 创建值，不批量迁移历史行。
- **回滚/降级**：event sink 可置空/no-op；停事件接线不改 request_id 与数据库。新 task ID 不回退为 message_id。
- **可并行**：不可与 AP-02/AP-03 同时编辑共享契约文件；接口定型后可并行准备独立测试 fixture。

### AP-02：Tool Registry 生命周期事件

- **目标**：让工具进度表示经过校验并真实执行的调用，状态来自最终结构化 ToolResult。
- **修改范围**：修改 **app/tools/contracts.py**、**app/tools/registry.py**、ToolSpec 注册位置（按当前源码核实 **app/tools/agent.py** / **app/tools/legacy_tools.py**）；扩展 **tests/test_tool_registry.py**。
- **依赖**：AP-01。
- **核心接口**：ToolContext 携带 task identity/sink；ToolSpec 增固定 progress category/label 或使用安全白名单映射；Registry 统一产生 TOOL_REJECTED、TOOL_STARTED、TOOL_COMPLETED、TOOL_FAILED、TOOL_CANCELLED。
- **实施边界**：Policy/JSON/Schema/Agent budget 拒绝不得发 started；获得 semaphore 后、调用 handler 前才发 started；有效 ToolResult 经现有 _limit_result 后发 completed；timeout/cancel 说明 handler 是否真正开始。事件异常不能影响 handler 或结果；Registry 不依赖 QQ。
- **验收标准**：handler 调用最多一条 started 和一条 terminal；拒绝无 started；NO_RESULTS、PARTIAL、DATA_UNAVAILABLE、DENIED、TIMED_OUT、ERROR、CANCELLED 区分；Evidence 数来自裁剪后结果并保留 completeness；事件不含 args、异常正文或工具内容。
- **必要测试**：Policy 拒绝、schema 错误、未知工具、semaphore 等待/取消、handler success/partial/no results、异常、timeout、无效结果、Evidence 裁剪；运行 Registry/Agent 兼容回归。
- **完成定义**：Agent 与直接命令工具经同一 dispatch 边界；显示 label 不由参数控制；权限、预算、timeout 与并发保持原约束。
- **风险**：started/terminal 挂错在 semaphore 或结果裁剪边界会误报；保留 semaphore finally 释放。
- **回滚/降级**：event_sink=None 时维持原 dispatch；可停事件投影，不动 ToolPolicy/ToolResult。
- **可并行**：AP-01 后单独实现 Registry；不可与 AP-03 同时编辑 **app/tools/contracts.py**、**app/tools/agent.py**。

### AP-03：Agent Loop、Compaction 与 WorkingState 事件接线

- **目标**：通过现有 Agent Loop 观测模型轮次、Compaction、checkpoint 和终态；移除旧的提前 tool-call 播报语义。
- **修改范围**：修改 **app/tools/agent.py**、**app/handlers/llm_handler.py**、**app/context/manager.py**、按需 **app/context/working.py**、**app/bot_service.py**、**app/main.py**；扩展 Agent、Compaction、WorkingState 测试。
- **依赖**：AP-01、AP-02。
- **核心接口**：run_agent_loop(..., task_identity, event_sink)；Agent 外层唯一终态发射器；模型 attempt/round、Compaction 与 WorkingState CAS 使用同一请求身份。
- **实施边界**：普通模型请求和 final synthesis 周围发 start/completed；BotService 对 timeout/cancel/error/正常返回执行互斥终态映射；checkpoint CAS 成功后才发 CHECKPOINT_SAVED，未配置和写入失败使用独立 WorkingState 状态事件；不保存模型文本、reasoning 或工具参数。保留现有 Agent 并发、协议 compactor、Context Window/Usage Budget 和 deadline。
- **旧入口处理**：移除 on_tool_call 的用户进度语义；替换 **app/main.py** 提前 tool callback 与 **app/commands/service.py** dispatch 前“正在查询”。直接工具命令把 sink 传至 Registry。scheduled task 若播报，也复用同一事件流并标 origin，不维护第二条通知路径。
- **验收标准**：成功 Agent 恰有一个 TASK_COMPLETED；失败/deadline/cancel/stale epoch 有对应互斥终态；WorkingState outcome 与事件可对账；checkpoint 失败不声称已保存；tool_call_id 仍一对一返回。
- **必要测试**：Fake LLM 多轮、模型 timeout/cancel、8 轮工具、兄弟工具取消、Compaction、WorkingState checkpoint 成败、fallback attempt、reset guard；断言隐藏文本和参数不进入事件。
- **完成定义**：事件不触发 QQ I/O；普通 Agent 所有退出路径有确定终态；原生 Function Calling 兼容。
- **风险**：Agent 对某些错误会返回友好字符串而不是抛异常；必须按真实分支设置终态，不能把 fallback error text 归为成功。
- **回滚/降级**：移除 sink 接线或注入 no-op；Agent 调度器与协议保持不变，不恢复“模型意图=执行状态”的通知。
- **可并行**：依赖 AP-01/02；共享文件修改串行。契约冻结后可独立审查取消和终态路径。

**Iteration 1 验收门**：Agent 与 Registry 事件身份一致、状态真实、不含敏感原文；事件投递不阻塞模型/工具；Agent、Registry、WorkingState 相关测试通过。

## 3. Iteration 2：QQ 进度体验

### AP-04：请求级 ProgressCoordinator

- **目标**：将并发事件归约为少量、正确、隔离的进度状态。
- **修改范围**：新增 **app/progress/coordinator.py**；在 **app/main.py** 组装；新增 **tests/test_progress_coordinator.py**。
- **依赖**：AP-01、AP-02、AP-03。
- **核心接口**：ProgressCoordinator.register(identity, mode) -> AgentEventSink、publish(event)、snapshot(group_id, user_id, is_admin)、close(task_id, terminal)、invalidate_group_epoch(group_id, epoch)、有界 notice port。
- **实施边界**：同步无 I/O reducer；按 sequence 去重；按 tool_call_id 聚合；按 task/group/epoch fencing；并发结果区分 ok/partial/no_results/unavailable/denied/timeout/error/cancel；每 task 只保留最新候选，不写 SQLite。首次播报必须由固定有界调度器按单调时钟主动唤醒；不得为每个事件创建 timer/task。任务关闭、取消、reset、shutdown 取消对应 deadline。
- **验收标准**：1 秒任务无候选；t=1 工具开始时任务持续运行，到 t=3 会主动生成一条真实阶段状态；门槛前完成的任务永不补发；重复事件不重复计数；乱序完成不改变 outcome；终态/reset 后迟到事件被拒；quiet/balanced/verbose 不突破硬上限。
- **必要测试**：单调虚拟时钟；t=1 started/t=3 wake/t=20 completed；t=2.9 completed 后不得播报；单工具、5 工具乱序并发、部分失败、无结果、拒绝、重复/迟到事件、task close/reset、mailbox 满和合并、取消 deadline 与限速计数。
- **完成定义**：仅依赖 event contract 与 reporter protocol，不 import NapCat、工具 handler 或模型客户端。
- **风险**：把“已有终态”误报为成功，或将旧 task 合进新 task，是阻断级问题。
- **回滚/降级**：全群 quiet 或停止产生通知；事件可 no-op，不影响 Bot。
- **可并行**：接口定型后可并行写独立测试；coordinator/mailbox 由单一 owner 修改。

### AP-05：QQ Reporter、顺序与 epoch fence

- **目标**：在现有 Dispatcher 上有界发布状态，确保 final/reset 不被等待中的旧进度打乱。
- **修改范围**：新增 **app/progress/reporter.py**；修改 **app/qq_dispatcher.py**、**app/main.py**、**app/commands/service.py** reset/purge 接线；必要时调整 **app/natcapqq_adapter.py**，但不假设 edit/revoke；新增 delivery 测试并扩展 Dispatcher/Main 测试。
- **依赖**：AP-04。
- **核心接口**：固定数量 lifespan reporter consumer；有界 mailbox offer(candidate)；close_task(task_id)；send_final(..., task_id, epoch)；reset/purge 使用 with_group_delivery_barrier(group_id)。
- **实施边界**：所有 send/reset 采用唯一锁序：取得 group barrier 后检查同步内存 task/epoch fence，再调用 Adapter；持锁时不得等待 Agent、mailbox consumer 或其他依赖该锁的工作。Agent final 前同步关闭 task gate 并清理 pending 状态，随后走同一 barrier；Reporter 在等待群锁期间候选可失效，取得锁后必须再次检查。Reset/Purge 在 barrier 内先 fence old epoch 再更新 SQLite epoch，释放后发确认。Progress timeout/error 只丢状态，Agent/final 继续；final 不等待普通 progress queue/global progress quota。
- **验收标准**：pending progress 不越过 final；尚未发送的旧状态在 final/reset 后被拒；不同 task 不串扰；reset 后旧 epoch 被拒；Dispatcher SUPPRESSED 记独立诊断；NapCat 错误不改变 Agent terminal；Agent execution、WorkingState persistence、QQ delivery 三种状态分开；shutdown 有界关闭固定数量 consumer 并清 mailbox。跨 task 同群最短间隔 8 秒，全局进度发送上限 30 次/60 秒；final 不占额度。
- **必要测试**：Fake Adapter asyncio barrier 覆盖 progress 等锁、final 抢先、reset 抢先、锁内取消、send timeout、final priority、多群与 shutdown；用有界 wait_for 检测死锁；断言 Agent=completed/WorkingState=saved/QQ=failed 等状态可分别表达；final 引用原消息行为保持。
- **完成定义**：实现本进程 task/epoch fence；明确远端 delivery/HTTP 取消是 best effort；不依赖 outbound message ID。
- **风险**：SQLite epoch check 与 HTTP send 不是分布式原子事务；发送前临界区需复核 fence。
- **回滚/降级**：Reporter 关闭后 final 仍经现有 Dispatcher；reset/purge 保留 SQLite 语义；无需数据回滚。
- **可并行**：不可与 AP-06 同时编辑 CommandService reset 接线；可独立预审 Dispatcher API。

### AP-06：Command System V2 配置与运行状态查询

- **目标**：提供安全群级进度模式配置，并允许按权限查看当前运行态。
- **修改范围**：修改 **app/commands/service.py**、必要时 **app/commands/contracts.py**、**app/main.py** 服务注入；扩展命令和 Main 测试。
- **依赖**：AP-04、AP-05。
- **核心接口**：现有 /config allowlist 增加 progress_mode；Coordinator snapshot 只读服务；新增 **/agent status**。不新建 Command Registry。
- **实施边界**：写入现有 group_settings，默认 balanced。**/config show** 展示模式；**/config set progress_mode quiet|balanced|verbose** 管理员-only且限定 enum。普通成员只查本人任务；管理员只看群级安全聚合，不看目标/query/正文。状态只来自 Coordinator 当前内存态；SQLite running 记录不能证明 coroutine 活跃。
- **验收标准**：危险 key/非法值拒绝；跨用户/群隔离；无活动任务有清晰响应；重启后 interrupted task 不显示为活动；/task list/cancel 语义不变。
- **必要测试**：权限矩阵、mode/default/非法值、跨群 scope、任务终态、重启状态、命令不触发 Agent，以及现有 Command Registry 测试。
- **完成定义**：经现有 Parser/Policy/Renderer；无 schema migration；不直接序列化 WorkingState.goal。
- **风险**：公开目标/query 会泄露群成员隐私；只返回安全阶段、时间和聚合计数。
- **回滚/降级**：移除 progress_mode allowlist 或设 quiet；可隐藏 /agent status；不影响 Agent 或 /task。
- **可并行**：AP-05 reset 接线后，可与 AP-07 并行；限制为命令文件和测试。

**Iteration 2 验收门**：快速请求无状态；并发工具状态合并；每任务最多 3 条中间状态；发送失败不影响 Agent；final/reset/shutdown 本地顺序通过 Fake Adapter 测试。

## 4. Iteration 3：叙述与集成验收

### AP-07：确定性阶段文案与 PublicPlan 边界

- **目标**：实现 Level 1 静态文案，并为 Level 2 留清晰边界但不启用额外模型调用。
- **修改范围**：新增/修改 **app/progress/presentation.py**（或在 AP-04 projection 中由单一 owner 实现）；修改静态 ToolSpec progress category；新增 presentation 测试。
- **依赖**：AP-04、AP-05。
- **核心接口**：render_progress(snapshot, mode) -> str | None；输入只能是 Coordinator 安全 DTO。
- **实施边界**：覆盖 web search/read、local knowledge/recipe、repository、SFM validation、other 静态类别；多工具文案显示真实 started/resolved/status count。balanced 默认；verbose 仅增加安全类别/计数，不提高消息上限。V1 不生成 PublicPlan。
- **验收标准**：CQ-looking 文本、URL secret、恶意网页、路径、长参数不会进入文案；PARTIAL/NO_RESULTS/DENIED/FAILED 表达不同；计划不写成结果；不增加模型调用。
- **必要测试**：模板金样例、Unicode/长度、恶意参数/网页内容、ToolStatus 矩阵、模式上限、普通无工具消息。
- **完成定义**：所有进度由静态模板和 typed event 决定；不读取 reasoning/CoT。PublicPlan 是后续版本候选。
- **风险**：Evidence 被裁剪时不得声称找到完整 N 个来源；使用“已返回 N 项引用/部分结果”。
- **回滚/降级**：转为短通用模板或 quiet；不改 Agent/ToolResult。
- **可并行**：AP-06 后可并行，隔离 presentation 文件/测试。

### AP-08：端到端测试与体验评估

- **目标**：验证事件真实性、身份、消息顺序、隐私和资源边界，清理旧进度路径回归。
- **修改范围**：新增 **tests/test_agent_progress_integration.py** 与 Fake/Fixture；仅修复本功能引入的缺陷。
- **依赖**：AP-01 至 AP-07。
- **核心接口**：Fake LLM、Fake Tool、Mock QQ Adapter、临时 SQLite；不连接真实模型、NapCat、Tavily、QQ 或生产库。
- **验收标准**：下方 22 场景及本任务追加的主动唤醒、barrier 死锁、三类状态分离、过载场景均有断言；final 不被 pending progress 越过；终态/reset/跨群无过期状态；ToolStatus 映射正确；Bot、Command、Context V2、WebResearch、Agent 无非预期回归；完整测试通过或如实记录独立既存失败。
- **必要测试**：所有针对测试；最终完整命令如下，若环境工具链改变则使用已安装的等价命令，不下载依赖。
- **完成定义**：报告区分 Mock/Fixture/本地集成，注明未做真实 NapCat/模型送达验证；指标来自实际测试，不预填改善值。
- **风险**：Fake Adapter 不能证明 NapCat CQ 字面文本、HTTP 回执和取消期间远端处理。
- **回滚/降级**：保留测试；设 progress_mode=quiet 关闭进度；无需 DB rollback。
- **可并行**：只并行无共享状态 fixtures；集成与全量测试串行。

## 5. 必须验证的场景

| # | 场景 | 主要断言 |
|---|---|---|
| 1 | 1 秒普通任务 | 零中间消息，只有 final |
| 2 | 2 秒工具任务 | 不刷屏，即使已发生真实 tool event |
| 3 | 10 秒单工具任务 | 最多一条状态；started 来自 Registry 真执行边界 |
| 4 | 30 秒多阶段 WebResearch | search/read/count 来自 Registry/Evidence，且节流 |
| 5 | 同轮 5 工具并发 | 聚合，不是一工具一消息 |
| 6 | 工具乱序完成 | sequence 是观察顺序，统计不依赖 call 顺序 |
| 7 | 权限/schema/allowlist 拒绝 | 无 started、无“正在执行” |
| 8 | partial/no results/unavailable | outcome 区分，不统一算成功 |
| 9 | Tool timeout | timed_out 状态与执行阶段一致 |
| 10 | Agent deadline | 唯一 TASK_TIMED_OUT，迟到候选丢弃 |
| 11 | 外部取消 | TASK_CANCELLED，清理候选，不影响其他群 |
| 12 | Reset 与状态发送交错 | group barrier/epoch fence 阻止旧状态 |
| 13 | Final 前有 pending 状态 | close gate 后丢 pending，final 不排其后 |
| 14 | 多群并发 | identity/epoch 独立，全局额度有效 |
| 15 | 同群任务期间命令 | 命令不串入 Agent task，权限仍有效 |
| 16 | 状态发送失败、final 成功 | Agent 继续，final 仍能发送 |
| 17 | Dispatcher 2 秒 suppress | suppressed 可观察，不算 sent |
| 18 | 恶意 query/URL secret/CQ | 参数、凭据、正文不进入状态 |
| 19 | WorkingState 与 event terminal | task/epoch/outcome/revision 对账；失败不冒充 durable |
| 20 | Runtime shutdown | bounded drain，consumer 无泄漏 |
| 21 | Tavily/GTNH 数据缺失 | 明确 unavailable，不虚构成功 |
| 22 | 完整 unittest | Bot、工具、命令、Context、WebResearch 回归 |

评价指标：快速任务中间消息数、长任务首次有效进度延迟、每 task 状态数、重复/无效候选比例、final 顺序错误、旧 epoch 泄漏、工具状态误报、Reporter 丢弃/发送失败、额外模型请求数（V1 必须为 0）、reducer 延迟。只报告实际值，不预写改善结论。

## 6. 迭代测试命令建议

- **Iteration 1**：Agent event contracts、Tool Registry、Agent、Agent Compaction、Agent integration、Working Memory、BotService。
- **Iteration 2**：新增 coordinator/delivery suites；加跑 Dispatcher、Main、MessageTaskRuntime、Commands。
- **Iteration 3**：新增 presentation/integration suites；回归 WebResearch、Context V2、OneBot、Agent、commands、dispatcher；最后运行：

~~~sh
timeout 300 .venv/bin/python -m unittest discover -s tests -q
~~~

所有自动测试使用 Fake/Mock/临时库。NapCat 实机能力、真实模型体验、Tavily API 和生产设置属于后续现场验证；它们不阻塞本地 contract，也不能由 Fixture 通过替代。
