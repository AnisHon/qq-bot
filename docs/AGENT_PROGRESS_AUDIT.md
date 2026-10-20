# Agent 异步执行与 QQ 多阶段进度播报专项审计

审计日期：2026-10-20
审计对象：`/home/anishan/project/qq-bot` 当前工作区源码
HEAD：`39e735b`
审计方式：只读源码审计与本地 unittest；未调用真实模型、NapCat/QQ API 或生产数据库。

> **工作区状态说明**：审计时 HEAD 与工作区不一致。Agent、Context V2、Web Research 等相关代码含未提交修改；Tavily/Web Research 源码、实现说明和部分测试还是 untracked 文件。本报告结论针对审计时可见的工作区，不代表 HEAD 已提交内容或服务器部署状态。本文没有读取 `.env` 内容。

## 1. Executive Summary

当前系统已经具有异步接收、有限队列、工作线程、工具执行、少量群内状态消息和最终回复能力。OneBot 回调在事件入队后即返回 `200 accepted`，而处理由 `MessageTaskRuntime` 的 worker 执行；默认 4 个 worker、64 个排队容量。Agent 有最多 110 秒总 deadline，单轮并发执行工具，并通过 `on_tool_call` 发出按工具类别生成的简短提示。QQ Dispatcher 可将一次回复拆为多条消息，并对同群发送串行化、去重和抑制过快状态消息。

但现状不是完整的任务进度事件系统。Agent 的提示发生在 Tool Registry 验权、参数校验和实际 handler 执行之前；工具成功、失败、部分结果、证据数量、模型轮次、最终完成、超时和取消没有统一事件流。查询阶段文案有一处会在网页读取前宣称“已找到候选页面”，搜索查询文本也可能被转发到群里。`reply_notice_delay=3s` 仅控制通用等待提示，不延迟工具提示，因此 2 秒内完成但调用工具的请求仍可能先播报。最终答案在同一请求中晚于已完成的同步状态发送，但并发命令、其他任务、Context Reset 与状态消息之间没有 task-aware 顺序或 epoch 守卫。

Context V2 的 WorkingState、Checkpoint、EvidenceStore 和 Compaction 在当前源码中已实现一部分真实状态持久化；它们不是用户可订阅的实时事件流。`/task` 目前管理的是定时任务，不是 Agent 运行任务。基础设施足以用较小的 Agent/Registry 回调扩展实现可靠工具状态，但自然语言计划、任务取消/查询、消息编辑和跨任务消息排序仍需额外能力或 NapCat 环境验证。

## 2. Actual Execution Chain

```mermaid
sequenceDiagram
    participant NC as NapCat / OneBot
    participant API as FastAPI /onebot/event
    participant RT as MessageTaskRuntime
    participant BP as BotService
    participant LH as LLMHandler / ContextManager
    participant AG as run_agent_loop
    participant TR as ToolRegistry
    participant LLM as OpenAI-compatible LLM
    participant QQ as QQMessageDispatcher / Adapter

    NC->>API: POST event + callback auth
    API->>RT: try_submit(event)
    API-->>NC: 200 accepted (queued; not completed)
    RT->>BP: _process_group_message(event)
    BP->>LH: _reply_with_llm(event, on_tool_call, snapshot)
    LH->>AG: run_agent_loop(messages, deadline, policy, hooks)
    AG->>LLM: chat_with_tools (non-streaming)
    LLM-->>AG: text and/or tool_calls
    AG->>QQ: on_tool_call(stage text), before registry validation/dispatch
    QQ->>NC: send_group_msg(status)
    AG->>TR: dispatch(name, args, ToolContext)
    TR-->>AG: ToolResult / error / timeout
    AG->>LLM: next round with paired tool results
    LLM-->>AG: final text or next tool_calls
    AG-->>LH: final string
    LH-->>BP: ChatReply(text, epoch)
    BP-->>RT: reply
    RT->>QQ: send final reply, replying to original message
    RT->>RT: spawn bounded summary background task
```

### 2.1 OneBot 接入与 HTTP ACK

- `create_app()` 创建 `MessageTaskRuntime`，默认 worker 数和队列容量分别来自 `QQ_MESSAGE_WORKERS=4`、`QQ_MESSAGE_QUEUE_CAPACITY=64`；startup 才启动 worker，shutdown 最多 drain 15 秒。[`app/main.py:533-538`] [`app/main.py:280-314`] [`app/config.py:180-184`]
- `/onebot/event` 先验证回调鉴权；无效为 401，非字典 payload 为 400，不是群消息返回 `200 ignored`，重复消息返回 `200 duplicate`。通过 `try_submit` 入队才标记去重并返回默认 `200 accepted`；队列未启动或满时返回 `503 busy` 和 `Retry-After: 1`。[`app/main.py:358-381`]
- 所以 HTTP 200 是**接收/去重/入队 ACK**，不是 Agent 完成 ACK。测试用 ASGI transport 验证了 `accepted`、容量满时 `503 busy`、重试和 duplicate。[`tests/test_main.py:195-229`]
- Runtime 是进程内 asyncio 队列，不是持久化任务队列。进程退出或 drain 超时可以取消 worker 并丢弃尚未执行的排队任务；日志记录被放弃的数量。[`app/task_runtime.py:91-123`]

### 2.2 从 worker 到 Agent 与请求关联

`_process_group_message()` hydrate quote、取 Bot 名称、建立日志上下文后，创建 `service.handle_group_message()` task。它先等最多 `reply_notice_delay`（默认 3 秒）；若到时仍无返回且尚未尝试工具提示，则发送“正在处理，稍等。”，之后一直等 Agent task；这不是 Agent 总超时。[`app/main.py:398-428`] [`app/config.py:199-200`]

普通消息由 `BotService.handle_group_message()` 生成 `request_id=str(event.message_id)`，持久化当前消息快照，再判断是否回复；只有进入 LLM 回复路径才以 group 为 key 获取 active marker。BotService 用该 request ID 设置 `BudgetScope`，调用 `LLMHandler._reply_with_llm()`，结束时释放群 marker。[`app/bot_service.py:75-150`] 请求 ID 主要复用 OneBot message ID；普通消息没有单独生成的 UUID。`ContextManager` 将 `snapshot.request_id` 同时用作 `WorkingState.task_id` 和 `request_id`。[`app/context/manager.py:502-509`]

各次模型调用及工具执行可通过当前 budget scope 的 request ID 关联；每个模型 tool call 另有 provider `tool_call_id`，ToolContext 使用同一 request ID。[`app/tools/agent.py:539-545`] [`app/tools/agent.py:920-954`] 但没有独立的生命周期事件/trace 表、模型调用序号或统一 span ID；当前“request_id 可传递”不等于“有可查询的实时任务追踪”。普通请求 ID 依赖 OneBot `message_id`，代码没有为缺失值生成 request UUID；WorkingState 又要求非空 task/request ID。message ID 的全局唯一性依赖上游实现，代码本身未验证其跨群唯一性。[`app/natcapqq_adapter.py:126`] [`app/context/contracts.py:90-94`]

### 2.3 并发、错误、取消与最终回复

- 默认最多 4 个消息 worker 同时处理不同事件，队列可再容纳 64 个。不同群可以并发；SQLite/向量工作也走有界 `StorageExecutor`。[`app/task_runtime.py:19-34`] [`app/main.py:95-99`]
- 对普通 Agent 回复，BotService 的 `_active_groups` 使一个群同一时间只允许一个 LLM 回复；但新消息先持久化，然后才竞争 busy marker。忙群触发的另一条消息得到“我正在回答上一条问题”回复。slash command 在此 group marker 之前分流，因此命令并不统一受此门控。[`app/bot_service.py:83-111`] [`app/bot_service.py:185-199`]
- Agent 总 deadline 默认 110 秒，Agent 参数默认最多 8 轮、每轮最多 10 个工具调用、总计最多 36 个；传给 Registry 的工具 timeout 默认 10 秒。[`app/config.py:200-223`] [`app/handlers/llm_handler.py:456-463`] [`app/handlers/llm_handler.py:531-555`]
- 同轮工具以 `asyncio.create_task()` 并发运行，之后 `gather()` 收集；结果按原始 `tool_calls` 顺序被写入协议块，不按完成先后排序。超时/取消会取消未完成的兄弟工具并等待清理。[`app/tools/agent.py:684-769`] 每个原始 `tool_call_id` 都得到一条 Tool result，包括未知、重复、预算跳过和有效调用；Compactor 再校验一对一配对。[`app/tools/agent.py:827-849`] [`app/context/compaction.py:347-367`]
- 正常最终答案由 `_process_group_message()` 在 Agent 返回后发送，并引用原用户消息。发送后才尝试启动群摘要后台任务。[`app/main.py:430-443`]
- 外部取消时 `_process_group_message()` 会取消内部 service task 并 gather；Runtime shutdown 也会取消 worker。工具等待期间 Agent 会将 WorkingState 记为 cancelled；Agent 模型调用期间取消没有同样的局部状态提交，遗留 `running` 状态会在新 `MemoryStore` 初始化时标成 `interrupted`。[`app/main.py:444-447`] [`app/tools/agent.py:757-768`] [`app/memory.py:381-384`]

## 3. Agent Event Capabilities

`run_agent_loop()` 目前有两个可注入回调：`on_tool_call` 和 `on_context_compaction`，没有通用事件总线、async iterator 或 callback protocol。[`app/tools/agent.py:494-518`]

| 生命周期点 | 当前实际能力 | 证据/边界 |
|---|---|---|
| 请求开始/完成 | 没有 Agent event callback；HTTP 接入日志和最终返回分散在 API / service | [`app/main.py:398-452`] |
| 模型调用开始/结束 | 无事件；只是在 loop 中直接 await `chat_with_tools` / `chat` | [`app/tools/agent.py:588-620`] [`app/tools/agent.py:871-906`] |
| 工具阶段提示 | 有 `on_tool_call`；按 tool name 映射固定中文阶段，阶段变化时最多 4 次 | [`app/tools/agent.py:653-672`] [`app/tools/agent.py:1047-1078`] |
| 真实工具开始/完成 | 没有前后 Hook；上述提示发出时尚未完成白名单、重复调用、数量预算检查，也未进入 Registry。CommandService 的工具型命令也会在 Registry dispatch 前发送通用提示 | [`app/tools/agent.py:653-699`] [`app/commands/service.py:836-857`] |
| 工具成功/失败 | `ToolResult.status` 结构化；Agent 得到结果后记录状态，但不播报 | [`app/tools/contracts.py:16-27`] [`app/tools/agent.py:771-825`] |
| 多工具并发状态 | 实际并发执行；现有进度把一轮的调用合为一个 stage，不逐工具报告 | [`app/tools/agent.py:684-752`] |
| Agent round | loop 有 `_round` 计数，日志在请求工具时记录 round 和工具名；没有外部 round 事件 | [`app/tools/agent.py:653`] |
| Checkpoint | 有 SQLite WorkingState checkpoint；非事件流 | [`app/context/working.py:47-68`] |
| Compaction | `on_context_compaction` 仅写聚合计数；不是用户可见事件 | [`app/tools/agent.py:552-566`] [`app/handlers/llm_handler.py:502-507`] |
| 最终答案 | 函数返回字符串，主 handler 发送；无 Agent final event | [`app/tools/agent.py:638-648`] [`app/main.py:430-443`] |
| 失败/取消 | 异常、结构化 ToolStatus、WorkingState 状态与日志各自存在；无统一终态事件 | [`app/tools/agent.py:757-768`] [`app/tools/registry.py:273-285`] |

当前进度消息并非完全虚构：它由实际模型返回的 tool call 名称触发。但它只表示“模型提出了调用意图”，不是“工具已启动”或“工具成功”。例如 `web_read` 映射文案“已找到一些候选页面，正在读取正文”发生在实际读取调用之前，且可由模型直接请求某 URL 触发；该文案不能证明此前搜索成功。命令型工具调用也在 Registry dispatch 前通知，因此同样不是执行确认。[`app/tools/agent.py:655-672`] [`app/tools/agent.py:1059-1060`] [`app/commands/service.py:836-857`]

## 4. Tool Registry Instrumentation

当前 Agent 工具调用统一经过 `_dispatch_registered_tool_call()`，它构建 `ToolContext`（request/group/user、epoch、deadline、policy、budget、Evidence 限额、结果字数限制）后调用 `TOOL_REGISTRY.dispatch()`。[`app/tools/agent.py:920-954`]

Registry 已具备适合事件挂点的中央执行边界：先查 ToolSpec 和权限、解码/校验参数，再计算 deadline/timeout、获得 per-tool semaphore、执行 handler，最后验证 ToolResult 并裁剪内容和 evidence。[`app/tools/registry.py:222-292`] 成功/部分成功/无结果/数据缺失/拒绝/超时/取消/错误均有 `ToolStatus` 枚举；handler 异常会被转换为不暴露内部 traceback 的通用错误，取消会继续向上传播。[`app/tools/contracts.py:16-27`] [`app/tools/registry.py:273-304`]

可用于结果摘要的数据包括 status、`content`、evidence 数量、每条 Evidence 的来源标识/标题/locator/completeness，以及 ToolUsage；ToolResult 没有独立 `started_at`、耗时、cache hit、provider attempt 或被 Registry 拒绝的原因明细字段（用户面向错误只得到安全错误对象）。[`app/tools/contracts.py:43-58`] [`app/tools/contracts.py:158-166`] Registry 目前没有 before/after hooks。WebResearchService 内有全局缓存统计、每请求搜索/提取预算以及限速/并发，但结果本身没有表明本次是缓存命中还是新请求。[`app/handlers/llm_handler.py:409-423`] [`app/tools/web_research.py:431-508`] [`app/tools/web_research.py:530-585`]

`_tool_progress()` 对搜索工具从模型生成的参数抽取最多 2 个 query，并截到 90 字符后直接拼入群状态消息。[`app/tools/agent.py:1063-1077`] Tool arguments 因而可能包含用户的私密描述或被上下文诱导出的文本；它没有基于隐私/敏感项的摘要过滤。普通 Web 搜索的 Agent 执行使用 request-scoped WebResearchBudget，且并行调用共享同一 budget 对象，但这不形成进度事件。[`app/handlers/llm_handler.py:409-423`] [`app/tools/agent.py:568-571`] [`app/tools/agent.py:948-952`]

**边界判断**：工具真实开始、结束、拒绝、timeout、partial、cache 状态适合由 Registry 在对应执行边界发出；Agent Loop 适合发出 request/round/model/compaction/checkpoint/final/failed/cancelled 等编排级事件。当前 tool-call 级进度应视为 Agent 意图提示，而不是 Registry 执行事件。

## 5. LLM Streaming and Progress Text

- `DeepSeekClient.chat()` 和 `chat_with_tools()` 都构造普通 JSON Chat Completions 请求；`chat_with_tools()` 一次性解析完整的 `message.content` 和 `message.tool_calls`，不是流式增量解析。[`app/llm.py:310-405`]
- `chat_stream()` 是独立 SSE 路径，只 yield `delta.content` 字符串；它不解析或组装流式增量 tool call。[`app/llm.py:407-490`]
- `run_agent_loop()` 调用 `_safe_chat_with_tools()`，无工具的最终补答调用 `_safe_chat()`；没有调用 `chat_stream()`。[`app/tools/agent.py:610-620`] [`app/tools/agent.py:891-900`]
- Provider 可以返回同时带文本和 tool_calls 的消息；Agent 会把该文本放入 `last_text` 和 assistant tool-call protocol block，但不会将其实时发给 QQ。若该轮最后需要工具，用户可见的是确定性 `on_tool_call` 消息，之后是最终答案。[`app/llm.py:382-405`] [`app/tools/agent.py:630-648`] [`app/tools/agent.py:674-682`]
- 当前路径不读取 `reasoning_content` 作为状态，也不把其送到 QQ。`chat()` 对空 `content` 有 reasoning 型模型的受限重试注释，但只返回 `content`；`chat_stream()` 也只 yield `delta.content`。[`app/llm.py:337-350`] [`app/llm.py:487-490`]

**结论**：不改模型协议即可利用已有工具调用路径提供有限的、确定性阶段提示；但要让提示代表真实执行状态，应把触发点移到 Registry 的校验/执行边界，并从已完成的 ToolResult 汇总结果。类似“我先查 X，再核对 Y”的自然语言计划当前没有单独展示通道。模型 tool-call 响应里的自然语言不会被当作计划安全地展示。若要生成用户可见计划，需要显式、受 schema 约束的 commentary 输出或单独规划步骤，并且把它与隐藏 reasoning/CoT 分开；不能解读或展示隐藏推理。

## 6. QQ Delivery Capabilities

等级定义：`VERIFIED_IN_CODE` 指当前代码明确实现；`SUPPORTED_BY_PROTOCOL_BUT_NOT_IMPLEMENTED` 指协议层有相应通用动作概念但代码没有实现；`UNKNOWN` 指 NapCat 当前版本/部署行为需现场验证。

| 能力 | 结论 | 证据/说明 |
|---|---|---|
| 同任务多次发送群消息 | VERIFIED_IN_CODE | 工具提示和最终消息都是独立 `dispatcher.send()`；定时任务也有工具提示和最终输出。[`app/main.py:406-437`] [`app/main.py:499-513`] |
| 发送队列 | 部分 VERIFIED_IN_CODE | 接收事件有容量 64 的工作队列；QQ 发送本身没有独立 bounded send queue。[`app/task_runtime.py:31`] [`app/qq_dispatcher.py:55-88`] |
| 群内发送串行化 | VERIFIED_IN_CODE | 同一个 dispatcher group lock 覆盖分片发送。[`app/qq_dispatcher.py:51-52`] [`app/qq_dispatcher.py:78-87`] |
| 全局/群级速率限制 | 部分 VERIFIED_IN_CODE | 有群级状态 2 秒抑制、相同文本 8 秒去重、分片间 0.45 秒等待；没有通用全局 QQ API limiter。[`app/qq_dispatcher.py:48-53`] [`app/qq_dispatcher.py:67-87`] |
| 同任务状态先于最终答案 | VERIFIED_IN_CODE | Agent 同步 await 状态 callback 后才调用工具，最终答案在 Agent 返回后发送；无 task 消息重排队列。[`app/tools/agent.py:655-672`] [`app/main.py:415-437`] |
| 不同任务/命令的群内全序 | 未实现 | 同群 lock 只防并发写入，不记录 task sequence；命令绕过 BotService 的 LLM busy gate。 |
| 引用原用户消息 | VERIFIED_IN_CODE | 适配器在有 `reply_to` 时构造 `reply` segment；最终回复传入原 message ID，进度不带 reply segment。[`app/natcapqq_adapter.py:261-272`] [`app/main.py:411`] [`app/main.py:437`] |
| 获取发送后 message_id | 未实现于当前 adapter | `/send_group_msg` 响应只检查 HTTP 状态和 `status`，没有提取 `data.message_id`；方法返回 `None`。[`app/natcapqq_adapter.py:261-277`] |
| 编辑已发送消息 | UNKNOWN | 当前代码无 edit 调用，也无 outbound message ID；NapCat 部署是否有扩展 action 未验证。 |
| 撤回/删除已发送消息 | SUPPORTED_BY_PROTOCOL_BUT_NOT_IMPLEMENTED / 环境 UNKNOWN | 当前适配器无 delete/revoke action；NapCat 当前插件和权限是否支持需实机确认。 |
| 消息长度 | VERIFIED_IN_CODE（应用层） | 默认每片最多 850 字符，按换行/标点/字符切分；只首片引用原消息。长度按 Python 字符而不是协议字节计。[`app/config.py:180-183`] [`app/text_utils.py:6-54`] [`app/qq_dispatcher.py:78-87`] |
| CQ Code 转义 | 局部 VERIFIED_IN_CODE，普通 Agent 状态未统一 | Command Renderer 把 `[CQ:` 替换成全角冒号；普通 LLM `_sanitize_qq_text()` 没有此替换，进度 query 也不经过 Renderer。Adapter 将文本作为字符串发送；NapCat 对字面 CQ 文本的实际解释需环境验证。[`app/commands/renderer.py:22-29`] [`app/handlers/llm_handler.py:71-83`] [`app/natcapqq_adapter.py:261-272`] |
| 发送失败重试 | 未实现 | 进度发送异常被捕获并记日志，最终发送异常由外层捕获并记录；没有重试/投递确认。[`app/main.py:411-413`] [`app/main.py:449-450`] |

## 7. Context V2 Integration

### 7.1 已实现并被本地测试覆盖

- `ContextManager` 会根据请求快照建立 `WorkingState(task_id=request_id)`；在持久化可用时 Agent 调用 `WorkingMemoryService.start()` 创建任务记录。[`app/context/manager.py:502-523`] [`app/tools/agent.py:573-586`]
- WorkingState SQLite 表保存 `running/completed/failed/cancelled/interrupted` 状态和 revision checkpoint；每轮工具执行完成后，Agent 会把工具名/结果状态写入 `completed_steps`，异常状态写入 `open_questions`，并记录 Evidence refs。[`app/memory.py:359-374`] [`app/tools/agent.py:852-866`]
- 任务读取要求 task/group/user/epoch 匹配；重启后 `running` 会变成 `interrupted`，不会续跑模型或工具。[`app/memory.py:381-384`] [`app/memory.py:1352-1372`]
- EvidenceStore 是单请求内存对象，默认最多 64 条、512 KiB、每条最多 16 KiB、TTL 900 秒，保留来源、hash、版本、query 条件、时间及 completeness；`read_segment` 做 scope 校验。[`app/context/compaction.py:36-90`] [`app/context/compaction.py:93-176`] [`app/context/compaction.py:178-213`]
- Compaction 有完整 protocol block 校验；归档时保留 tool call ID、结果状态和 Evidence metadata，但明确提示精确输出已归档，需要重新执行授权只读工具才能做精确断言。[`app/context/compaction.py:347-395`]
- Agent 每轮可记录 Compaction 统计，但这是组/epoch 聚合指标，不是可订阅的实时事件。[`app/tools/agent.py:552-566`] [`app/handlers/llm_handler.py:502-507`]

### 7.2 有数据结构但仍有限的实时状态

- WorkingState 有 `pending_steps` 字段，但 Agent 当前只更新工具 `completed_steps`、`open_questions` 和 `evidence_refs`；没有模型调用状态、正在执行工具状态、每个工具耗时/缓存、用户可见 stage 或进度百分比。[`app/context/contracts.py:72-88`] [`app/tools/agent.py:852-866`]
- `WorkingMemoryService` 有 `load()` 和 MemoryStore 有 `list_agent_tasks()`，但没有 Command 调用这些 API 来展示 Agent 运行状态；完成后数据库记录可在 TTL 内查，但没有面向用户的任务查询路由。[`app/context/working.py:70-95`] [`app/memory.py:1352-1372`]
- `task_id` 复用请求 message ID，稳定性依赖 OneBot message_id；它并非新的任务 UUID，也未被作为发送 callback 的 epoch token 使用。

### 7.3 Epoch 行为及其进度边界

Agent 在每次模型派发、工具执行、最终合成前调用 `context_guard`；主处理器在发送最终答复前再次确认 epoch。[`app/tools/agent.py:592-594`] [`app/tools/agent.py:650-652`] [`app/tools/agent.py:873-875`] [`app/main.py:430-437`]

但 `on_tool_call` callback 闭包只捕获 group 和 dispatcher，不检查当前 epoch；管理员 reset 会递增 epoch、删除摘要与 Agent task，旧 Agent 后续阶段会被 guard 拦住，最终答案也会被丢弃，但已开始的状态发送没有 epoch fencing。[`app/main.py:406-415`] [`app/memory.py:1140-1163`]

**判断**：WorkingState 适合作为持久化任务状态的事实来源，但不应直接承担 QQ 播报通道。播报需要轻量、只读、task/epoch 绑定的执行事件；否则把持久化 checkpoint 的粒度、存储延迟和面向用户输出耦合在一起。WorkingState 可以为事件提供已提交步骤/Evidence 的安全摘要，但当前其状态比实际 Agent 生命周期更粗。

## 8. Command System

- Command V2 由 `CommandService._register_commands()` 注册，Registry 负责 parse、权限、超时和 handler 调用；BotService 识别 slash command 后不进入 LLM 工具 Agent。[`app/commands/service.py:81-100`] [`app/commands/registry.py:19-64`] [`app/bot_service.py:83-86`]
- `/status` 展示 Bot/model/知识源/配方/SFM/Web provider 的配置状态，不显示 Agent 活动任务。[`app/commands/service.py:227-264`]
- `/context show` 和 `/context stats` 读取历史摘要、epoch、索引、估算 token、摘要和 compaction 汇总；不是实时 Agent 状态。[`app/commands/service.py:316-354`]
- `/context compact` 触发群摘要更新；`/context reset` 和 `/context purge` 走管理员确认机制。[`app/commands/service.py:356-369`] [`app/commands/service.py:768-829`]
- `/task list/add/cancel` 仅对 `scheduled_tasks` 表操作。它不能查询或取消 `agent_tasks` 中的当前 Agent 任务。[`app/commands/service.py:141-148`] [`app/commands/service.py:457-510`]
- `/config show` 只显示群模型 provider 与默认 pack；`/config set` 只允许 `SAFE_GROUP_CONFIG_KEYS` 白名单，不包含进度播报设置。[`app/commands/service.py:208-213`] [`app/commands/service.py:744-766`]

当前没有 `/progress` 命令或群级播报开关。现有命令架构可承载状态查询/配置，但必须调用任务状态服务而不是把 scheduler `/task` 混用；是否需要新命令不在本次审计确定。

## 9. Concurrency and Lifecycle Risks (A–H)

| 场景 | 当前行为 | 审计风险/接口缺口 |
|---|---|---|
| A. 2 秒内快速回复 | 通用 wait notice 在 3 秒后才出现；但模型只要在 3 秒内返回 tool call，Agent 会立即 await `on_tool_call` 并发送状态 | 快速工具任务也会先发状态；没有“短任务静默”阈值。[`app/main.py:406-428`] [`app/tools/agent.py:655-672`] |
| B. 30 秒多阶段研究 | 每个 request 最多 4 个不同 stage；工具阶段改变才 callback；同阶段重复搜索不提示 | 无开始/完成、候选数、提取成功数、持续运行心跳；Dispatcher 2 秒状态窗口还可能抑制阶段转换。[`app/tools/agent.py:655-660`] [`app/qq_dispatcher.py:48-76`] |
| C. 同轮 5 工具 | 并发执行，结果按调用序归一；触发前只有一条聚合提示 | 用户看不到单项进度，失败/部分成功也无阶段消息；不能从播报区分具体工具完成顺序。[`app/tools/agent.py:684-752`] |
| D. timeout/cancel | Agent deadline/Tool timeout 可中断模型或工具；工具等待时取消 sibling；BotService finally 释放 group busy | 已发状态后可能只有友好失败回复或无结果；没有统一“已超时/取消”事件。Agent 模型请求被取消时工作状态可能暂留 running，重启后才变 interrupted。[`app/tools/agent.py:757-768`] [`app/memory.py:381-384`] |
| E. 同群消息竞争 | 普通 LLM 回复以 group active marker 单飞；消息先入历史；忙时回复稍后再问。不同群由多个 worker 并发 | slash commands 在 busy marker 前处理；多 worker 消费 FIFO 不保证同群任务完成/发送顺序；无每群任务 FIFO 执行锁。[`app/bot_service.py:83-111`] [`app/bot_service.py:185-199`] |
| F. 进度发送失败 | callback 捕获异常后继续 Agent；Dispatcher 返回 `False` 表示被抑制但调用方忽略；`network_notice_sent` 在 send 前设为真 | 状态失败不会阻断工具，但 3 秒后不再补通用 notice，用户可能全程沉默。最终回复发送失败只记录日志，无重试。[`app/main.py:406-428`] [`app/main.py:449-450`] |
| G. Context Reset | context guard 在 LLM/工具/最终合成前以及最终发送前检查 epoch；reset 递增 epoch 并删除 WorkingState | 最终答案防旧 epoch；progress callback 没有 epoch 检查，已发起的旧状态消息可与 reset/新消息交错，也可能在 reset 结果之后到达。[`app/tools/agent.py:592-652`] [`app/main.py:406-415`] [`app/main.py:430-437`] |
| H. 进程关闭 | Runtime 先停止接收并 drain，默认 15 秒；超时后 cancel workers/background、丢弃剩余队列；随后 storage 与 adapter close | 没有耐久消息投递队列或重启续跑；取消期间没有可靠 QQ 最终状态。若 task 抵抗取消，最多等待 1 秒后记录仍未停止，再继续关闭依赖。[`app/task_runtime.py:91-140`] [`app/main.py:262-278`] |

### 9.1 顺序结论

在**单条 Agent 请求内部**，状态 callback 是同步 await，工具执行随后开始，最终回复只有在 Agent task 返回后才发送，所以正常路径下该请求自身的进度不会排在自己的最终答案之后。没有异步 detached progress job，也没有进度发送队列。[`app/tools/agent.py:655-672`] [`app/main.py:415-437`]

但 Dispatcher 只提供每群锁，不提供 request/task 顺序号。其他并发命令或调度任务可以获得同一群锁并夹在状态与最终答复之间；跨任务“旧状态晚到/新答案先到”无法被 task-aware 地阻止。Context reset 也只守卫最终答复，不守卫状态 callback。

## 10. Resources and Safety

### 已有的主要资源上限

| 项目 | 当前默认值 | 证据 |
|---|---:|---|
| OneBot worker / pending queue | 4 / 64 | [`app/config.py:180-183`] |
| Agent 总 deadline | 110 秒 | [`app/config.py:219-223`] |
| 工具 timeout | 10 秒（上下文传入；再与 ToolSpec timeout 取较小值） | [`app/config.py:199-200`] [`app/tools/registry.py:248-267`] |
| Agent 轮次/单轮调用/总调用 | 8 / 10 / 36 | [`app/config.py:219-223`] |
| Agent 工具结果 | 配置 dataclass 默认 6000；`from_env` 默认 2500 字符 | [`app/config.py:221-223`] [`app/config.py:410-415`] |
| QQ 每片长度 | 850 字符 | [`app/config.py:180-183`] |
| Dispatcher 状态抑制/相同文本去重/分片延时 | 2 秒 / 8 秒 / 0.45 秒 | [`app/qq_dispatcher.py:48-53`] |
| WebResearch 每请求 search/extract | 各 3 次 | [`app/config.py:206-208`] [`app/tools/web_research.py:530-573`] |
| WebResearch 全局请求速率/并发 | 60 次/分钟 / 4 并发 | [`app/config.py:215-217`] [`app/tools/web_research.py:896-906`] |
| EvidenceStore | 64 records / 512 KiB / 16 KiB each / 900 秒 | [`app/context/compaction.py:73-90`] |

Usage budget 默认请求数/token 上限可为未配置（均为 `None`）；Bot 会提示“cost remains unknown”。Model request slot 默认全局 4 并发。[`app/config.py:187-195`] [`app/main.py:100-114`] 进度播报本身不调用模型，不直接增加模型 token 或费用，但会增加 NapCat API 请求、发送锁等待和公开群消息数。已有 stage callback 上限为每任务 4；当前没有 progress 专属总队列容量/群级策略。

### 信息安全

- Tool name 为注册名称，通常可安全显示；tool arguments 可能包含网页 query、URL、仓库路径或从群上下文得到的内容。当前搜索阶段提示直接显示最多 90 字符的 query，没有脱敏。[`app/tools/agent.py:1063-1077`]
- Tavily/Web 页面正文不会直接生成进度文字，但 Agent 看到外部不可信内容后仍可能提出新的 tool call；当前阶段提示依据 tool call 类型和 query，而非工具返回成功，因此外部内容间接影响后续用户可见 stage。状态生成应仅依据服务端事件字段，不能相信工具输出文本中自称“成功”的描述。
- `_tool_progress` 不调用 LLM；固定模板能减少额外成本且不暴露模型思维链。但当前返回阶段更接近工具意图而非已验证结果。
- `QQCommandRenderer` 转义 CQ-looking 内容，普通 Agent 最终文本和 progress query 没有使用同一个转义步骤。NapCat 对 string payload 中 CQ 片段的解析行为未经本地测试，列为 UNKNOWN/需验证。[`app/commands/renderer.py:22-29`] [`app/handlers/llm_handler.py:71-83`] [`app/natcapqq_adapter.py:261-272`]
- WebResearch cache、全局请求速率和并发已有独立限制，但 QQ Dispatcher 没有全局发送限流；群锁、状态抑制与接收 worker 数提供的是部分负载界限，而非发送 API 的速率保证。[`app/qq_dispatcher.py:48-87`]

## 11. Tests and Evidence

### 实际运行

1. 专项测试：

   ```text
   timeout 180 .venv/bin/python -m unittest tests.test_agent tests.test_agent_integration tests.test_agent_compaction tests.test_tool_registry tests.test_dispatcher tests.test_adapter tests.test_main tests.test_task_runtime tests.test_bot_service tests.test_working_memory tests.test_commands tests.test_web_research -q
   Ran 181 tests in 2.403s — OK
   ```

2. 全量测试：

   ```text
   timeout 300 .venv/bin/python -m unittest discover -s tests -q
   Ran 444 tests in 11.844s — OK
   ```

执行环境使用 Python 3.13.11；未调用真实模型、真实 QQ、NapCat API 或联网搜索服务。测试输出含测试故意触发的错误日志、FastAPI `on_event` 弃用告警以及若干 SQLite ResourceWarning，但两组命令均退出成功，无失败或卡住。

### 与进度相关的已有覆盖

- Agent callback 在 request deadline 内 await；超时会取消 callback，且工具尚未执行。[`tests/test_agent.py:187-218`]
- Dispatcher 覆盖相同状态去重、短时间状态 suppress 和最终消息仍发送。[`tests/test_dispatcher.py:32-41`]
- Main 覆盖 OneBot 队列 ACK/满队列/重试、slash command 不触发 Agent、旧 epoch 最终回复不发送。[`tests/test_main.py:195-229`] [`tests/test_main.py:231-291`] [`tests/test_main.py:293-340`]
- Runtime 覆盖队列容量、worker 并发和 shutdown 行为。[`tests/test_task_runtime.py`]；WorkingMemory 覆盖 request scope、CAS 与重启后 interrupted。[`tests/test_working_memory.py:75-109`]
- Compaction tests 覆盖协议配对、Evidence 引用和归档，但未验证 QQ 用户看到的多阶段消息顺序。[`tests/test_agent_compaction.py`]

### 未覆盖/未测试

- 未有测试证明 progress callback 一定在 Registry policy、参数校验、handler 调用之后；源码表明现在不是如此。
- 未测试工具完成/partial/failure/cached 的 QQ 状态消息，因为当前不存在这些消息事件。
- 未测试 `/context reset` 并发中状态消息被抑制；测试只验证旧 epoch 最终回复不发送。
- 未测试同群多个并发 command/Agent/status 的 task-aware 全序；也没有 message edit API 测试。
- Mock Adapter 只验证调用 `send_group_message`，不验证 NapCat/Docker 版本对回复段、CQ 字符串、发送回执或 edit/revoke 的实际行为。

## 12. Feasibility Matrix

| 能力 | 状态 | 当前依据 |
|---|---|---|
| 工具开始/完成播报 | NEEDS_MODERATE_CHANGE | 有模型 tool-call 前的意图提示；Registry 无前后 hook，完成态不存在。中央 dispatch 是自然的最小边界。 |
| 多工具并发播报 | NEEDS_SMALL_CHANGE | 同轮并发和 ordered results 已有；当前一轮仅聚合一个阶段消息。若只显示聚合结果，接入结果摘要即可；逐工具实时完成需 Registry 回调。 |
| 自然语言开场说明 | NEEDS_MODERATE_CHANGE | 非流式 tool response 的自然语言不会展示；需要显式用户可见的计划字段或单独调用，不可复用隐藏 reasoning。 |
| 长时间运行状态更新 | NEEDS_SMALL_CHANGE | 工具阶段提示可触发；没有 timer/heartbeat 或真实阶段结束事件。 |
| 进度合并和节流 | NEEDS_SMALL_CHANGE | 当前有每群 2 秒抑制、相同文本去重 8 秒；没有每 task 合并、任务结束清理或用户偏好。 |
| QQ 多条消息 | READY | 状态与最终答复通过 dispatcher 多次发送；长消息可分片。真实 NapCat delivery 仍需现场验证。 |
| QQ 消息编辑 | UNKNOWN | 代码不支持，且发送回执 ID 未暴露；NapCat 实际部署 API 未验证。 |
| 最终答案顺序保障 | NEEDS_SMALL_CHANGE | 单 request 的 callback 是同步 await，故自身 status 先于 final；跨 task/command 无全序。 |
| 取消与 epoch 隔离 | NEEDS_SMALL_CHANGE | Agent/model/tool 取消和最终 epoch 检查存在；progress callback 无 task token/epoch 校验，状态终态也未播报。 |
| 命令配置进度模式 | NEEDS_SMALL_CHANGE | Registry 与安全 `/config set` 可复用，但目前 whitelist 无 progress 项。 |
| 进度状态查询 | NEEDS_SMALL_CHANGE | Agent task 表、load/list API 已存在；无命令 API、活动状态接口或事件历史。 |

## 13. Recommended Architecture Boundaries

本报告不制定完整重构计划；仅标出建议保留的边界：

1. **保留 `MessageTaskRuntime`** 作为入站异步 admission、worker 与 shutdown 生命周期边界；它当前的 bounded queue 不应同时承担用户可见 progress event storage。
2. **保留现有 `run_agent_loop()`**。在已有 `on_tool_call` 与 `on_context_compaction` 的基础上，可评估增加统一只读事件 callback/iterator；事件类型应包含 `request_id/task_id`、group、epoch、round、事件时间、受控摘要、状态和可选 ToolResult/Evidence 摘要。Agent 负责编排事件，Registry 负责真实 tool start/finish。
3. **保留 `ToolRegistry.dispatch()` 为工具生命周期唯一入口**。事件不得绕过 policy/argument validation，且应在确实被接受执行后才产生 started；完成状态来自 `ToolResult.status` 或受控异常映射，不能从 tool 文本推断。
4. **保留 `QQMessageDispatcher` / `NatcapQQAdapter`** 作为传输层。增加 progress behavior 时需要 task-aware ordering/epoch check 和发送失败语义，而不是让 Registry/LLM handler 直接调用 NapCat。
5. **WorkingState 继续负责持久化的任务事实**；只读进度摘要可由其已提交状态投影，但应与临时执行事件区分。EvidenceStore 提供的来源、数量和 completeness 可用于安全摘要，完整页面/工具文本不应进入群进度消息。
6. **自然语言计划与操作事件分开**。若引入用户可见的计划文本，应标注为模型计划/预期步骤，不冒充已完成操作；不公开 reasoning_content/CoT。

## 14. Open Questions

1. 当前部署的 NapCat 版本和 OneBot HTTP Client 能否返回稳定 outbound `message_id`？是否有受支持的 edit API？需要基于实际 Docker 镜像/配置验证；代码当前没有实现。
2. 群聊中允许播报多少工具细节？默认公开展示搜索 query 可能暴露用户输入，应确定是否仅显示工具类型和计数。
3. 是否需要让群成员看到同群其他用户的 Agent 进度？现有群级回复语义与 WorkingState 的用户隔离并非同一个权限模型。
4. 用户取消一个 Agent 的权限边界是什么？当前 `/task cancel` 是取消定时任务，不是取消正在运行的 request。
5. 同群并发 slash command（尤其 `/context reset`、`/context purge` 确认）是否需要等待或取消当前 Agent？当前代码允许命令在 LLM group busy marker 外执行。
6. 工作区中 Agent、Context V2 和 Tavily/WebResearch 有未提交改动；要把哪些行为视为准备上线的版本，需要通过目标部署提交与实际 NapCat 配置再次验证。

## 15. Evidence Index

| 源码位置 | 支持的结论 |
|---|---|
| `app/main.py:358-381` | 鉴权、事件解析、bounded admission 与 HTTP ACK 语义 |
| `app/main.py:398-452` | worker 处理、wait notice、工具提示 callback、最终发送、epoch 检查、异常/取消处理 |
| `app/task_runtime.py:16-89` | worker 数、队列容量、background task admission |
| `app/task_runtime.py:91-198` | shutdown drain、cancel、队列丢弃与 worker 异常回收 |
| `app/bot_service.py:75-150` | command/聊天分流、message snapshot、群 busy gate、request budget scope、最终 ChatReply |
| `app/bot_service.py:185-203` | 群级 LLM reply 单飞与 epoch 查询 |
| `app/handlers/llm_handler.py:456-586` | deadline、ContextManager、Agent 调用、回调透传及最终 LLM 路径 |
| `app/tools/agent.py:494-586` | Agent 入参、回调接口、EvidenceStore、WorkingState 初始化 |
| `app/tools/agent.py:588-672` | 非流式模型轮次、tool-call progress 触发时序 |
| `app/tools/agent.py:674-849` | 同轮并发工具、结果顺序、状态/Evidence、协议块配对 |
| `app/tools/agent.py:852-917` | WorkingState 更新、final synthesis、失败路径 |
| `app/tools/agent.py:920-954` | ToolContext 构建与统一 Registry dispatch |
| `app/tools/agent.py:1047-1078` | 固定 progress 文案映射及搜索 query 公开行为 |
| `app/tools/registry.py:222-329` | Registry 权限、参数校验、timeout、并发、结构化状态、裁剪 |
| `app/tools/contracts.py:16-27,43-58,101-166` | ToolStatus、Evidence、ToolContext、ToolResult 合约 |
| `app/llm.py:310-405` | chat 与 Function Calling 非流式解析 |
| `app/llm.py:407-510` | 独立 stream 路径只 yield 内容 delta，不组装工具调用 delta |
| `app/qq_dispatcher.py:44-88` | 同群锁、status window、重复抑制、分片发送 |
| `app/natcapqq_adapter.py:261-277` | NapCat 群消息发送格式、回复段、忽略 outbound ID |
| `app/context/contracts.py:72-110` | WorkingState/PreparedContext 的字段及状态模型 |
| `app/context/working.py:15-113` | WorkingState 创建、CAS checkpoint、scope load 和限制 |
| `app/context/compaction.py:15-90,93-213,347-395` | ProtocolBlock、EvidenceStore 限额/引用读取、归档元数据 |
| `app/memory.py:359-384,1290-1372` | Agent task/checkpoint Schema、restart interrupted、读写列表 API |
| `app/commands/service.py:115-220,227-264,316-369,457-510,744-766` | 当前上下文、task、status 与安全群配置命令 |
| `tests/test_main.py:195-229,293-340` | 入站 ACK/队列与 stale final reply 测试 |
| `tests/test_agent.py:187-218` | tool notification deadline/cancellation 测试 |
| `tests/test_dispatcher.py:24-41` | 分片、引用首片、重复与 status 抑制测试 |
| `tests/test_working_memory.py:75-109` | scope、重启 interrupted、checkpoint CAS 测试 |
