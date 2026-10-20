# Agent Execution Events 与 QQ Progress V1 设计

日期：2026-10-20
源码基线：当前工作区，HEAD `39e735b`；Context V2、Web Research/Tavily 与 Agent 相关文件有未提交改动。
状态：设计稿；本文件不表示功能已实现或已部署。

## 1. 目标与事实边界

V1 为现有 Bot 增加少量、真实、可节流的 Agent 执行反馈。Agent 继续负责模型与工具编排，Tool Registry 继续执行工具，QQMessageDispatcher 继续发送消息。新事件层只观测执行事实并将安全摘要交给进度协调器；它不能改变模型结果、授权或工具执行。

源码审计报告 `docs/AGENT_PROGRESS_AUDIT.md` 针对当前工作区，记录了入站异步队列、工具提示、Context V2 状态和 Dispatcher 的实际边界。`docs/CONTEXT_V2_PROGRESS.md` 与 `docs/CONTEXT_V2_IMPLEMENTATION_REPORT.md` 记录了较早版本的本地验收和 2026-10-17 部署；这不证明当前未提交工作区与服务器一致。本文仅按可见源码设计，不读取凭据、不调用模型或 QQ，也不将规划能力当作已交付。

### 1.1 当前可复用能力

- `app/main.py` 的 `/onebot/event` 在 `MessageTaskRuntime.try_submit()` 成功后返回 HTTP 200；这是入队确认。`app/task_runtime.py` 使用有界队列，默认 4 个 worker、64 个待处理事件，关机 drain 默认 15 秒（`app/config.py:180-184`）。
- 普通群消息由 `BotService.handle_group_message()` 分流命令/聊天、记录 snapshot，并以群为粒度限制并发 LLM 回复（`app/bot_service.py:75-150,185-203`）。不同群可并发；命令路径不经过普通 LLM busy gate。
- Agent 使用原生 Function Calling；默认总 deadline 110 秒、最多 8 轮、每轮 10 个 tool call、总计 36 个 call（`app/config.py:218-223`）。同轮工具并发，模型上下文按原始 call 顺序收集结果（`app/tools/agent.py:684-769`）。
- `ToolRegistry.dispatch()` 已统一处理查找、策略、参数验证、deadline/timeout、每工具 semaphore、handler、结果校验和裁剪（`app/tools/registry.py:222-329`）。`ToolResult` 有结构化 `ToolStatus`、Evidence、ToolUsage 和 completeness（`app/tools/contracts.py:16-27,43-58,158-166`）。
- `run_agent_loop()` 当前有 `on_tool_call` 与 `on_context_compaction` 回调（`app/tools/agent.py:494-518`）。前者在 Registry 校验和执行前触发；它表示模型提出了调用意图，不表示工具已开始或成功。
- Context V2 已有 request-scoped WorkingState、checkpoint、EvidenceStore 和 Compaction。`WorkingState.task_id` 当前复用 snapshot request ID；Agent 记录工具结果和 Evidence 引用，但没有通用可订阅的运行事件（`app/context/contracts.py:72-94`、`app/context/manager.py:502-523`、`app/tools/agent.py:852-866`）。
- `QQMessageDispatcher` 已有同群锁、分片发送、状态消息 2 秒抑制、同文本 8 秒去重和布尔发送结果（`app/qq_dispatcher.py:44-88`）。`NatcapQQAdapter.send_group_message()` 不返回 outbound message ID（`app/natcapqq_adapter.py:261-277`），源码没有编辑消息实现。
- 当前 `/task` 是 scheduled task 命令；`/status` 是 Bot/模型/知识服务状态；`/context stats` 是持久上下文统计；`/config set` 仅允许 `model_provider`、`default_pack`（`app/commands/service.py:51,101-220,334-369,744-766`）。

### 1.2 当前限制

现有 `_process_group_message()` 在 tool callback 内直接发送状态消息，callback 由 Agent 在 dispatch 之前调用；它没有 request task ID 或 epoch fence。`reply_notice_delay=3s` 只延迟通用等待消息，不阻止 2 秒内完成的工具任务先发状态（`app/main.py:398-437`、`app/tools/agent.py:653-672`）。命令的 `_dispatch_tool()` 也在 Registry 之前发通用提示（`app/commands/service.py:836-867`）。V1 应以统一事件与协调器替代这些提前播报入口。

## 2. 目标架构

~~~mermaid
flowchart LR
  E[OneBot / scheduled request] --> RT[MessageTaskRuntime]
  RT --> BS[BotService / CommandService]
  BS --> LH[LLMHandler / ContextManager]
  LH --> AG[现有 Agent Loop]
  AG -->|模型与轮次事件| ES[请求级 AgentEventSink]
  AG --> TR[现有 ToolRegistry]
  TR -->|真实工具生命周期事件| ES
  TR --> DOM[现有领域服务与 WebResearch]
  AG --> WM[WorkingMemoryService / EvidenceStore]
  WM -->|持久 checkpoint 与来源事实| AG
  ES --> PC[ProgressCoordinator：状态归约、合并、策略]
  PC --> MB[有界 Outbound Mailbox]
  MB --> QR[QQ Progress Reporter]
  QR --> DISP[QQMessageDispatcher]
  DISP --> AD[NatcapQQAdapter]
  AD --> NC[NapCat]
  AG -->|最终文本| PC
  PC -->|关闭 task gate 后| DISP
~~~

逻辑职责如下：

| 组件 | 职责 | 明确不负责 |
|---|---|---|
| Agent Loop | 发出任务、模型请求、轮次、Compaction、终态事件；协调工具并发；继续使用现有预算与 deadline | 不发送 QQ，不将 tool-call 参数展示给群，不把进度反馈注入模型 |
| Tool Registry | 在权限/参数/准入通过并真正进入 handler 前后报告工具生命周期；状态来自 `ToolResult` 或受控异常映射 | 不发送 QQ，不解释网页文本，不猜测执行成功 |
| AgentEventSink | 校验请求身份、分配 sequence、调用协调器 reducer；同步、进程内、无 I/O | 不保存无限事件，不等待 QQ HTTP，不承载授权 |
| ProgressCoordinator | 聚合事件、生成可公开的状态投影、限速/合并、epoch/task fencing、关闭任务 | 不执行工具、不改变 WorkingState、不解析原始参数生成消息 |
| QQ Reporter / Dispatcher | 有界发送、群内串行、发送失败隔离、最终结果优先 | 不参与 Agent 推理，不承诺 NapCat 编辑/撤回能力 |
| WorkingState / EvidenceStore | 保留现有持久化任务状态、checkpoint 和请求内证据 | 不作为实时事件队列，不把每个事件写入 SQLite |

## 3. AgentEvent 契约

### 3.1 身份、顺序与版本

V1 定义不可变 `AgentEvent` 与请求级 `TaskIdentity`。任务 ID 使用 Bot 生成的 UUID，不能复用 OneBot `message_id`；`request_id` 继续保留现有 OneBot ID 供预算、历史和诊断关联。message_id 缺失时 `request_id` 使用独立随机值，不能留下空 ID。事件 identity 至少包含：

~~~python
@dataclass(frozen=True, slots=True)
class TaskIdentity:
    task_id: str           # UUID；一次 Agent/直接工具任务唯一
    request_id: str        # 入站消息或 scheduled occurrence 的关联键
    group_id: str
    user_id: str
    epoch: int             # 从 ContextSnapshot 捕获；非持久请求也保留其 epoch
    origin: str            # user_message | slash_command | scheduled_task

@dataclass(frozen=True, slots=True)
class AgentEvent:
    schema_version: int
    identity: TaskIdentity
    sequence: int          # 每个 task 单调递增，从 1 开始
    occurred_at: datetime  # UTC aware
    kind: AgentEventKind
    round_index: int | None
    tool_call_id: str | None  # 内部关联值，不显示给用户
    payload: AgentEventPayload
~~~

`sequence` 由一个请求级 sink 在同一 asyncio 事件循环内同步分配。当前工具 handler 均通过 async Registry 在事件循环中执行；若未来允许线程回调，必须通过 `loop.call_soon_threadsafe()` 回到该事件循环后再分配 sequence。消费者按 identity 和 sequence 防重、拒绝重复或倒退事件；它不能假设同轮工具按调用顺序完成。

WorkingState 应使用同一个 UUID `task_id`，保留原 `request_id`。这只是现有字段值语义的调整；`agent_tasks` 已有 task_id/request_id/group/user/epoch 列，不需要为 UUID 增加数据库迁移。旧 task 行继续可按原 ID 读取，V1 不对历史任务做重写。ToolContext 增加非授权用途的 task identity/event sink 引用，`request_id`、BudgetScope 和 Evidence scope 语义保持不变。

### 3.2 事件种类和语义

建议稳定的 V1 kind：

| Event | 产生位置 | 语义 |
|---|---|---|
| `TASK_STARTED` | Agent 入口或直接 Registry 命令任务入口 | 执行已获准开始；不等于一定会调用模型 |
| `MODEL_REQUEST_STARTED` / `MODEL_REQUEST_COMPLETED` | 每次 `chat_with_tools` 或最终 `chat` 的 await 前后 | 只表示一次模型 API 请求的状态，不含 prompt/content/reasoning |
| `TOOL_REJECTED` | Agent 调用预算/去重/白名单 skip，或 Registry policy/schema/deadline 预检拒绝 | handler 尚未运行；禁止映射成“正在执行” |
| `TOOL_STARTED` | Registry 参数与策略通过、获得 semaphore 后、即将 await handler 时 | 这是唯一真实“工具开始”信号；等待 semaphore 不算 started |
| `TOOL_COMPLETED` | handler 返回有效 `ToolResult` 且 Registry 完成结果裁剪后 | 代表 handler 有结构化结果，不等于成功；附真实 `ToolStatus` |
| `TOOL_FAILED` | handler exception、无效返回、工具 timeout 或受控执行失败 | 附安全状态/代码，不附原始异常与正文 |
| `TOOL_CANCELLED` | Registry 执行因取消中断 | 附 `execution_started`，区分等待 semaphore 与已进入 handler |
| `COMPACTION_STARTED` / `COMPACTION_COMPLETED` | Agent 每次窗口拟合/协议块归档周边 | 只带安全计数与耗时；不展示被归档文本 |
| `CHECKPOINT_SAVED` | WorkingMemory CAS checkpoint 成功后 | 带 revision 和有限步骤/证据计数；失败不冒充已保存 |
| `WORKING_STATE_NOT_CONFIGURED` / `WORKING_STATE_SAVE_FAILED` | Agent 无持久化工作状态，或 checkpoint 写入失败 | 单独记录 WorkingState 状态；不能改写 Agent 执行终态 |
| `TASK_COMPLETED` / `TASK_FAILED` / `TASK_CANCELLED` / `TASK_TIMED_OUT` / `TASK_STALE` | BotService 按实际返回、异常、取消及 epoch 校验映射 | 互斥的 Agent 执行终态，不表示 QQ 已送达最终回复 |

WorkingState 最近一次 checkpoint 状态（`unknown / not_configured / saved / failed`）与 Agent execution terminal 分开；最终 QQ 投递另记 `accepted_by_adapter / suppressed / stale_epoch / failed / cancelled`。`accepted_by_adapter` 仅表示 Dispatcher 的 Adapter 请求返回成功。
| `TASK_STALE` | epoch fence 使事件/任务失效 | 后续用户可见进度一律丢弃 |

`TOOL_COMPLETED` 的 result status 维持完整枚举：`ok`、`partial`、`no_results`、`data_unavailable`、`not_indexed`、`version_mismatch` 等。ProgressCoordinator 单独聚合成功、部分结果、无结果、不可用、拒绝、超时和失败；不能把所有返回都计为成功。

### 3.3 Payload 安全投影

Event payload 是固定 typed DTO，不是任意 dictionary dump。允许字段包括：工具静态分类/用户可见 label、`ToolStatus`、handler 是否开始、Evidence 实际返回条数、Registry 截断/partial 标记、同轮已结束数/总数、Compaction 计数、模型调用结果类别、耗时 bucket 和安全错误 code。Evidence 条数只表明 Registry 最终保留的 Evidence 数；若结果为 `PARTIAL`，文案必须写“部分结果/已返回引用”，不能声称完整搜索总数。

绝不将原始 `ToolArguments`、搜索 query、URL 原文/查询凭据、异常正文、工具 content、网页文本、仓库绝对路径、QQ 私有历史、`reasoning_content` 或 CoT 放进用户可见投影。工具显示名称由 `ToolSpec` 静态进度分类或明确白名单映射产生；未知工具不能直接回显模型提供的名称。Payload 上限为 4 KiB UTF-8；超限事件被拒绝并只累计内部诊断计数。

外部网页/工具结果是不可信数据。即使内容命令“继续执行/已成功/通知群”，事件状态也只能由 Registry 的控制流和结构化 `ToolResult.status` 决定。ToolPolicy 拒绝、Agent 层 skip、timeout 和取消都不能由自由文本改写。

## 4. 事件投递机制选择

| 方案 | 优点 | 代价/风险 |
|---|---|---|
| Awaited async callback | API 直观；异常可在调用点捕捉 | callback 若做网络/SQLite I/O 会占用 Agent deadline、拖慢并发工具，正是当前旧通知可能触发的问题 |
| 每请求 bounded asyncio.Queue | 事件有顺序缓冲，消费者可异步处理 | 需要管理每请求 consumer 生命周期、队列满时终态优先级、取消 drain；若每事件创建 task 会形成无界任务集合 |
| 同步 `AgentEventSink.publish()` + reducer + 有界输出 mailbox | 事件状态同步归约，不等 QQ；只为用户可见的合并消息排队；最少新生命周期状态 | reducer 必须严格保持 O(1)/有界，不允许同步数据库访问；记录完整事件历史需另行设计 |

**推荐最后一种。** Agent/Registry 调用同步 `publish(event)`；sink 只分配 sequence、校验固定身份并调用进程内 reducer，不 await、不访问网络、不写数据库。ProgressCoordinator 更新内存状态并向一个有界 Outbound Mailbox 投递“当前最新的合并通知”。Reporter 是 FastAPI lifespan 管理的固定 consumer（建议 1 个），不是每个事件创建 task；关闭由 `app/main.py` 明确 stop/drain。此处是 in-process event contract，不承诺跨进程或重启恢复事件。

有界邮箱按 task 只保留一个待发送候选；新状态替换旧候选。全局容量满时先淘汰已终止/过期候选，其次丢弃最旧的可合并候选并计数；不阻塞 Agent。task 终态通过 Coordinator 的同步 close 操作立即更新活跃表，即使没有终态可见消息，也不会被队列满吞掉。事件总量和 reducer work 均有硬上限；满额后丢弃非终态细节，终态仍直达请求级 coordinator close 状态。

### 4.1 首次进度的主动唤醒

3 秒首次播报门槛不能只在事件抵达时检查，否则一个从 t=1 秒运行到 t=20 秒才结束的工具不会在 t=3 秒播报。Coordinator 对仍活跃且进度模式非 quiet 的任务登记一个有界 deadline；由固定 reporter/scheduler consumer 使用单调时钟等待最早 deadline，到期时重新读取当前聚合快照，只有任务仍有效且存在真实的活动阶段才发布一条状态。工具完成、任务终态、取消、epoch invalidate 与 shutdown 都会移除 deadline。不能每次 publish 都创建 asyncio task/timer。门槛前结束的任务永久关闭，绝不补发。

时间戳可用 UTC 记录审计时间，但门槛、间隔、候选 TTL 必须使用 loop.time()/单调时钟。虚拟时钟测试应固定 t=0 注册、t=1 工具开始、推进到 t=3 并触发一条进度、t=20 完成；另测 t=2.9 完成后推进时钟不产生迟到播报。

### 4.2 QQ Delivery Barrier 与死锁协议

Dispatcher 需提供统一的群级 delivery critical section；进度、final 和 reset/purge fence 使用同一个按群锁。唯一允许的锁顺序是“先取得 group delivery barrier，再在 barrier 内读取 Coordinator 的同步内存 fence 并决定发送”；绝不在持锁期间 await 等待 Agent、队列消费者或另一个依赖此锁的操作。Coordinator 的 close/invalidate 是同步、无 await 的操作。reset/purge 在 barrier 内同步关闭旧 epoch gate，再 await 既有存储操作推进 epoch，释放 barrier 后才发送 reset/purge 确认。所有发消息路径不得先持有 Coordinator/Agent 锁再等 group lock。

Reporter 从 mailbox 取候选后，等待 group barrier 期间候选仍可被 final/reset 标记失效；取得锁后必须重新检查 task gate、epoch 与 sequence，然后才调用 Adapter。final 先同步 close task，清除待发候选，再取得同一 barrier；因此等待中的旧 progress 不会越过 final。Adapter 调用可能已在远端接受但本地取消/超时，无法撤回，也无法证明 QQ 客户端显示顺序。测试应以可控锁和 Fake Adapter barrier 确定性交错 progress-wait、final、reset、取消，并用有界 timeout 检测死锁。

## 5. Agent 与 Tool Registry 的最小挂点

### 5.1 Agent Loop

在 `run_agent_loop()` 加可选 `event_sink`/`TaskIdentity` 入参，保留现有模型和工具调度逻辑。入口启动 WorkingState 后发 `TASK_STARTED`；每次 `_safe_chat_with_tools()` 和 final `_safe_chat()` 周围发模型事件；每轮 compactor fit 前后只在实际压缩/归档时发 Compaction 事件；Agent 返回文本或捕获 timeout/cancel/error 时发唯一终态事件。事件发出不能替代 `context_guard`、Usage Budget、Context Window Budget 或 Tool Policy。

当前 `on_tool_call` 是 Registry 前通知（`app/tools/agent.py:655-672`），应删除其“真实执行状态”语义并由事件代替。`LLMHandler` 中 fallback 分支（如以后启用）应沿用同一 task_id、不同 model attempt 序号；不可将 fallback 重试误算成第二个用户任务。Final Agent task event 表示模型结果已生成，QQ 发送成功与否由 delivery 层单独记账。

### 5.2 Tool Registry

仅在 `app/tools/registry.py:222-292` 的真实统一 dispatch 入口发工具生命周期事件：

1. 查 ToolSpec、Policy 和 Schema 之前/失败路径只能发 `TOOL_REJECTED`，不发 started。
2. deadline 已过、timeout 配置无效、semaphore 仍在等待时，不提前发 started；若等待中取消，发 `TOOL_CANCELLED(execution_started=false)`。
3. Semaphore 已获得、handler 即将调用时发 `TOOL_STARTED`。
4. handler 返回合法 ToolResult 后，先按当前限制完成 `_limit_result()`，再基于最终 status/completeness 发 `TOOL_COMPLETED`；异常/超时分别映射 `TOOL_FAILED`，取消映射 `TOOL_CANCELLED(execution_started=true)`。
5. 事件通知异常只记脱敏内部诊断并隔离，不得把成功 handler 变失败或跳过结果返回。

`ToolContext` 增可选 `task_id` 与 `event_sink`，二者不参与 Policy 决策；Registry 仍按原策略和预算执行。对直接命令 `_dispatch_tool()`，移除当前 Registry 前的通用 callback，按同一 task identity/event sink 调用 Registry。Agent 层的未知名、重复签名、每轮和总调用上限 skip 不经过 Registry，需由 Agent 产生 `TOOL_REJECTED`，且 `execution_started=false`。

同轮工具仍并发；事件 `sequence` 表示真实观察顺序，tool_call 序号单独作为调用序号，不能让 UI 依据 sequence 推断调用顺序。最终模型上下文保持现有原始 Tool Calls 顺序及一对一 tool_call_id 结果配对。

## 6. WorkingState 与任务身份

WorkingState 是持久任务事实，AgentEvent 是进程内实时生命周期，Coordinator 是用户可见状态投影。三者关系：

- 一个 request task 只有一份 WorkingState；让其 `task_id` 与 UUID TaskIdentity 一致，`request_id/group_id/user_id/epoch` 与 snapshot 一致。
- Registry 发出的每个最终 ToolResult status 也由 Agent 用来更新同轮 WorkingState。Agent 在 checkpoint CAS 成功后发 `CHECKPOINT_SAVED`；Coordinator 将此 revision 标记为 durable watermark。此前完成计数是运行中观测，不能说“已保存”。
- Agent 最终状态先通过 WorkingMemory checkpoint 完成，随后 `TASK_COMPLETED` 关闭事件门。若 storage 不可用或 WorkingState checkpoint 失败，终态必须体现失败/未持久化，不能 UI 宣称恢复点已保存。
- 当前 WorkingState 的 `pending_steps` 并没有 Agent 实时更新入口；V1 不让 Coordinator 伪造或自动写入 pending steps。事件态的正在运行工具仅存在于 Coordinator 内存。
- 事件不替代 EvidenceStore。用户只可看到 Registry 最终 Evidence 的计数及 completeness；不序列化 Evidence 正文，也不把事件来源计为已验证事实。

ProgressCoordinator 与 WorkingState 终态一致性的验收比较 task_id、epoch、工具 outcome 与 checkpoint revision。没有 checkpoint（非持久化 snapshot）的请求应标记 `persistence=unavailable/not_persisted`；不能用内存事件冒充数据库恢复能力。

## 7. ProgressCoordinator 状态机与聚合

请求状态：

~~~text
CREATED -> RUNNING -> COMPLETED | FAILED | CANCELLED | TIMED_OUT | STALE
                    \-> WAITING_MODEL / WAITING_TOOL  (只作阶段投影，不是终态)
~~~

Reducer 只接受匹配 task_id/request_id/group_id/user_id/epoch 且 sequence 大于已见值的事件。task 结束、reset、身份冲突、TTL 到期后关闭 gate；终态后的迟到事件只增加 `late_event_dropped` 计数。一个群的新任务不能接收旧任务事件。运行中阶段由真实事件派生，不从模型文本或工具 output 推断。

并发工具在每轮建立调用集合及其当前 outcome 计数。Registry started/terminal 事件按 tool_call_id 聚合，重复事件不重复计数。UI 展示“5 项处理中、已完成 2 项；1 项无结果、1 项不可用、其余进行中”一类状态；这里的完成只表示该调用已有结构化终态，不表示成功。没有公开 label 或没有可安全展示的变化时仅更新内部计数。最终 round 结果通过 Agent 同一批 ToolResult checkpoint 与状态核对；若缺失结果按 `unknown/unavailable` 处理，不当成成功。

Progress 模式：

| 模式 | 用户可见行为 |
|---|---|
| `quiet` | 仅最终答案；协调器仍可维护内存诊断状态 |
| `balanced` | 默认；任务达到延迟门槛后，仅在有实质阶段变化时发送短状态 |
| `verbose` | 可显示安全工具类别与合并后的计数；不显示参数/正文，仍受同样硬上限 |

模式不是模型策略或 ToolPolicy。群设置在任务开始时读取并固定；任务中途更改对下一任务生效。

## 8. QQ Reporter、顺序和 Context Reset

### 8.1 发送与最终回复顺序

Reporter 的候选消息绑定 `(group_id, task_id, epoch, last_event_sequence)`。用户可见状态走同一 `QQMessageDispatcher` 的群锁。最终答案开始发送前，Coordinator 同步关闭该 task gate、撤掉其所有 pending candidate，再以同一群锁发送最终答案；发送进度前在锁内再次验证 active task 与 epoch。这样可保证：

- 已经完成且正在持有群锁的进度 HTTP 请求先结束，最终答案随后发送。
- 仍在 mailbox/等待群锁的进度，在最终 close 以后会被丢弃。
- 不同任务之间只发送 active-task 的投影，不能靠队列 FIFO 将旧 task 状态越过新任务。
- 最终答案不排在普通可丢弃进度队列后，也不等待全局进度限速额度。

跨网络的绝对送达顺序无法用本地锁证明：NapCat 可能已经接受消息但 HTTP 客户端在收到回执前超时。V1 提供“本进程发送调用的锁序”保证；不提供 QQ 端到端投递、撤回或编辑保证，也不自动重试结果未知的发送。

### 8.2 Reset/Purge epoch fence

仅在事件入队时检查 epoch 不够。`/context reset`、`/context purge` 确认执行时，应取得 Dispatcher 对应群的同一 delivery barrier；在 barrier 内先标记旧 epoch task 为 stale/关闭 reporter gate，再执行现有 SQLite epoch reset/purge。Reporter 取得 barrier 后，在实际 adapter send 前重新读取当前 epoch/Coordinator fence。若 progress 已先拿到 barrier，则 reset 等它完成后才推进 epoch；若 reset 先拿到 barrier，旧 progress 随后被拒绝。Reset/Purge 成功确认由 barrier 释放后发送，因此不会有旧 epoch 的新发送调用排在确认之后。

不能撤回 reset barrier 取得前已经到达 QQ 的消息；也不能把 SQLite 操作与 NapCat网络发送组成分布式原子事务。目标保证是同一进程内调用顺序和 fence，已在途的远端送达状态仍为 best effort/未知。

### 8.3 Suppress、异常与消息渲染

- Dispatcher 的 `False`（状态频率抑制/重复）是明确的 `SUPPRESSED`；Coordinator 清理或合并候选并递增 suppressed 计数，不视为已发送。
- Progress 发送异常、超时、adapter 非成功返回不改变 Agent/ToolResult，也不取消正常执行；记录脱敏错误类别和失败计数，最终答案仍可发送。
- 进度限定为单条短消息，不走最终长答案分片；进度投影目标上限 240 个 Unicode 字符（小于当前 850 字符分片上限）。
- 仅输出系统模板、受控 label、枚举和数字；对 renderer/adapter 应复用统一 CQ 字面文本安全处理。当前 Agent 普通文本未统一 `[CQ:` 转义，NapCat 对字面字符串的解释不能从 mock 测试推断，V1 实施需增加 mock 安全测试和现场验证。
- 进度不引用用户消息、不写入普通聊天历史、不作为 assistant/tool prompt 内容。独立消息加简短 task 阶段标识；不依赖 outbound message_id、edit 或 revoke。

### 8.4 执行状态与投递状态分离

执行状态、持久化状态和 QQ 投递状态是三个独立维度，不能折叠成一个 completed/failed：

| 维度 | 示例值 | 唯一事实来源 |
|---|---|---|
| Agent execution | running / completed / failed / cancelled / timed_out / stale | Agent 生命周期事件 |
| WorkingState persistence | saved(revision) / not_persisted / failed | WorkingMemory 的 start/checkpoint 返回值 |
| QQ delivery | not_attempted / attempted / accepted_by_adapter / suppressed / failed / unknown | Dispatcher 与 Adapter 的本地返回 |

Adapter 的 HTTP 请求成功最多记为 accepted_by_adapter，不能声称 QQ 客户端已展示。发送失败不回写 Agent execution 为 failed；Agent 完成而 final 发送失败时，可诊断为 Agent=completed、WorkingState=saved、QQ=failed。进度候选合并/丢弃、Dispatcher 抑制、progress send 失败、final send 失败分别计数，不能共用一个失败计数。

## 9. Level 1 与 Level 2 公共叙述

### Level 1：V1 必须实现的确定性状态

根据 AgentEvent 与固定 tool progress category 映射阶段，例如“正在检索资料”“正在读取已选来源”“正在查询本地配方数据”“正在检查 SFM 程序”。完成/失败文案只从 Registry status/Evidence projection 生成。不要把未知工具名、query、路径或 URL 直接拼入文案。task 超过约 3 秒且仍运行时才允许发第一条有意义状态；快速请求沉默直到最终答案。

目前 WebResearch 已统一出 `web_search`、`web_read`、`news_search`，通过 Tool Registry 与 EvidenceStore/Compaction；工具内部 HTTP/cache 细节不发成群消息。Fixture/API 搜索成功不代表真实 Tavily 配置或领域数据已验证。

### Level 2：模型生成 PublicPlan（V1 不启用）

当前 `chat_with_tools()` 是非流式，它返回普通 content 与完整 tool_calls；`chat_stream()` 是独立文本 SSE 路径，不组装 tool-call delta；Agent 不消费 stream（`app/llm.py:353-405,407-490`，`app/tools/agent.py:610-620`）。因此当前没有可靠、独立的公开计划字段。

不推荐把含 tool_calls 的任意 content 原样转发：协议没有保证它是经过审核的 plan，可能是草稿或不合适内容。也不建议把 `reasoning_content`、CoT 或网页工具正文解释为计划。额外 preflight LLM call 会增加费用、deadline 和预算占用，且 V1 不需要。

将来若有真实产品需要，可采用明确 `PublicPlan(summary, steps)` JSON 契约，经固定长度、步骤数、字符控制与安全渲染；计划明确标记为“准备执行”，不能作为结果或权限来源。可比较：provider 可靠支持的独立 structured output，或 Agent 内部专用 `set_public_plan` 受限 control call。两者都必须共享 Usage/Context Budget/deadline，允许失败降级为 Level 1，且不得走普通 Tool Registry 执行授权。当前优先不实现、不额外请求 LLM。

PublicPlan 版本扩展契约预留为 schema_version、summary（最多 160 字符）、steps（最多 4 项，每项最多 80 字符）、status=planned。只允许显式受约束字段；拒绝额外字段、工具输出、网页正文或 reasoning_content。计划步骤不自动映射为成功/完成事件，也不授权工具。V1 不发布模型普通 content，不增加 preflight 请求。

## 10. 资源界限与默认候选

这些是 V1 规划值，不是当前配置或实测值；部署前由 AP-08 fixture/压力测试确认。建议尽量集中在代码常量或一个 `ProgressPolicy`，不需要为每项新增环境变量。

| 资源 | V1 候选上限/策略 |
|---|---|
| 每 task AgentEvent 数 | 192；达到上限时丢弃非终态细节，仍记录一个终态状态 |
| 单事件 payload | 4 KiB UTF-8；不含任何原始 tool 参数/正文 |
| 待发送邮箱 | 全局最多 32 个 task 候选；每 task 一个最新候选，可替换/合并 |
| 活跃 reporter 消费者 | 2 个 lifespan 管理的固定进程内 worker；不按事件 create_task |
| 每 task 中间状态 | 最多 3 条；不含最终答案 |
| 快速任务门槛 | 任务启动后约 3 秒；早于阈值结束时不发送任何进度 |
| 两条进度最短间隔 | task 级至少 8 秒；同群共享至少 8 秒 |
| 全局进度 API 额度 | 最多 30 次尝试/60 秒；最终回复不消耗此额度 |
| 状态候选可等待 | 在邮箱中最多 2 秒；过期候选删除，不在最终回复之后补发 |
| 状态文本 | 240 字符以内、单片、固定模板 |
| 运行/终态内存摘要 | 最多 256 个 task；关闭后最多 15 分钟，只保留安全计数/终态，不存原事件正文 |
| Shutdown drain | reporter 最多 2 秒；超时关闭 gate、丢弃待发状态再关闭 adapter |
| 活跃任务数 | Coordinator 最多跟踪 32 个活动任务；进度 Reporter 为固定 2 个 worker |

Dispatcher 现有 `status_window=2s` 保留为底层防洪 backstop；Coordinator 管理 task 级 8 秒窗口，Reporter 管理跨 task 的同群 8 秒与全局 30 次/60 秒额度。dispatcher duplicate window 是相同文本幂等保护，不与业务节流混用。最终回复不占进度额度。若 AP-08 显示限速或延迟不合适，调整进度策略，而不是再叠多个相反的 throttle。

## 11. 持久化、迁移、失败降级

V1 不持久化每个 AgentEvent，不新增事件表或数据库迁移。现有 `agent_tasks`/`agent_checkpoints` 继续保存 WorkingState；现有 `group_settings(group_id, setting_key, setting_value)` 可保存经白名单验证的 `progress_mode`，不需要新 SQLite 表。task UUID 使用现有 task_id 字段，不回写历史行。

Reporter 运行态和最近状态只在进程内；重启后不能声称旧任务仍运行。现有 MemoryStore 初始化会将 `agent_tasks.status='running'` 改为 `interrupted'`（`app/memory.py:381-384`），`/agent status` 应只依赖 Coordinator 当前 active map，不把 SQLite 中未终结记录当作活跃协程。

若事件回调失败，Agent 主链路仍运行；若 Outbound Mailbox 满、限流、dispatcher suppress 或 NapCat send 失败，最多丢弃/合并进度，不改变最终回复、工具状态或模型 Budget。Agent execution terminal、WorkingState checkpoint status 和 QQ final delivery 分开保留；Adapter 接受 HTTP action 只记为 `accepted_by_adapter`，不能据此声称客户端已展示。

## 12. 兼容影响、回滚与不采用方案

- 保留 FastAPI、NapCat/OneBot、BotService、LLMHandler、`run_agent_loop()`、Tool Registry、领域服务、WorkingState、EvidenceStore、Dispatcher 和原群聊行为。
- 删除旧 `on_tool_call` 的提前播报语义以及 `CommandService._dispatch_tool()` 的 dispatch 前“正在查询”callback；所有 tool progress 改为 Registry 真实生命周期。若保留参数兼容，仅可作为 deprecated no-op 过渡，不得继续对外播报模型意图。
- 原 `/task` 仍是定时任务；如为 scheduled occurrence 发进度，origin 必须标记且复用同一 event/Coordinator/reporting policy，不把 Scheduler ID 当作用户 Agent task ID。
- 禁止整体重写 Agent Loop、重写 Registry、让 Registry 直接访问 NapCat、每个 tool call 发一条群消息、每条消息额外 LLM 开场白、展示隐藏 CoT、依赖 NapCat edit、引入 Redis/Kafka/LangGraph、把所有事件落 SQLite、把 Scheduled Task 与 Agent Task 合并。
- 回滚路径：将群 `progress_mode` 设为 `quiet` 可立即关闭中间消息；保留 final response 与 Agent/Tool Registry。代码级回滚只撤销 Reporter/Event wiring；不删除历史 WorkingState/checkpoint 数据，也不更改 ToolPolicy。

## 13. 主要风险

1. **真实 QQ 排序边界**：本地锁只能保证本进程调用顺序，不能证明 NapCat/QQ 端到端到达序；无 message_id 及 edit/revoke 能力时不能撤回迟到发送。
2. **Context Reset 竞态**：需让 reset/purge 与 status/final send 共用群 delivery barrier，并在发送临界区检查 epoch；只在队列入队时判断不够。
3. **事件映射错误**：把 handler 返回视作成功会误报 NO_RESULTS/partial/unavailable，必须用 ToolStatus 的显式投影。
4. **无意泄露查询内容**：工具参数和网页可能含用户隐私、密钥 URL、路径或注入内容；V1 只输出静态分类和结构化计数。
5. **多任务/命令竞争**：命令绕过普通 Agent group busy marker；Coordinator 必须以 task_id 和 epoch 防串扰，不能只用 group_id。
6. **Fake 与现场行为差异**：NapCat 对文字 CQ 内容、群级限速、HTTP 回执和请求取消后的远端处理需现场验证；mock adapter 不能证明。
