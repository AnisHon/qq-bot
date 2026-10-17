# Context Management V2 实施进度

## 部署补记（2026-10-17）

- 按用户后续授权，已将 Context V2 部署至 `tx:/home/anishan/project/bot`。GitHub `main` 与远端 checkout 当前提交为 `a2a57d3`。
- 分组提交：`5415981`（Context/历史存储）、`7a5b5ae`（Agent/Evidence）、`7a3cca9`（命令/服务接线）、`a2a57d3`（文档/评估）。
- `qq-deepseek-bot.service` 已重启并 active；`/ready`、`/health` 均返回 200。生产 SQLite 增量 schema 已初始化；只读 `PRAGMA integrity_check` 为 `ok`，消息历史与 FTS 行数均为 70。未执行 purge 或 TTL 清理。
- 部署前没有创建额外生产数据库备份；本次只执行已测试的非破坏性增量 schema 初始化，没有清理或删除历史。后续 purge/TTL 等破坏性维护仍需单独授权和备份演练。
- 本机通过生产服务做了无副作用的 OneBot `meta_event` 签名探测：有效 HMAC 返回 200/ignored，错误签名返回 401；NapCat Docker 容器自部署后日志中 401 数为 0。没有真实 QQ 群消息，因此真实 NapCat 消息回调及 bot 回复仍未端到端验证。

## 最终验收补记（2026-10-15）

- CV2-01..12 均已完成本地实现与适用验收。最终命令 `timeout 300 .venv/bin/python -m unittest discover -s tests -q`：406 tests，OK（11.097 秒）；未调用真实模型、QQ 或生产数据库。
- 固定 1,000 行 Fixture SHA-256：`b3d909b39727a7d0ab1ec23cec60ce0d899ff7e7adb57532a6041b1b15e573ec`。V1 旧 recent-8 选择稀有事实 recall@8 为 0/10；V2 ContextManager + SQLite FTS recall@12 为 10/10，版本约束保留 10/10。估算输入大小均为 UTF-8-byte heuristic：V1 平均 477.7、V2 平均 9515.5；不是 tokenizer 数据。离线检索测评无 compaction 和额外模型调用，任务正确率未测。
- 人工 SQLite 10,000 行 FTS 基准 100 次查询：FTS optimize 前 p50/p95/max 1220.299/1238.076/1262.979 ms，后 175.406/179.367/182.521 ms。环境为 Python 3.13.11、SQLite 3.51.0、Linux x86_64、AMD Ryzen 5 7600；仅本地合成基准，不代表生产 SLA。定时 optimize 可能造成 SQLite 写锁窗口。
- 新增 Evidence Reader 经现有 ToolRegistry 约束，只能分页读取当前请求已缓存 Evidence；之前 Registry 裁剪的内容仍为 partial，不能伪称完整。定向 Registry/Agent 回归 29 与 24 tests 均通过。
- 全量测试有旧 Recipe repository/importer fixture 的未关闭 SQLite 连接 ResourceWarning、FastAPI `on_event` 弃用告警及预期错误路径日志；测试无失败。上下文历史与记忆定向测试在 ResourceWarning 检查下通过且无本模块泄漏警告。

开始基线：`63a9da1`（2026-10-15）。Context V2 实施验收时未部署；后续部署见上方补记。真实模型、GTNH 数据、真实 SFM Parser 和真实 QQ 群消息回归仍未验证。

## 总览

- 任务总数：12
- 状态：CV2-01..12 `COMPLETED`；没有阻塞本地实施的外部依赖。
- 当前执行位置：12项本地任务与服务器部署检查完成；真实模型、GTNH 数据、SFM Parser 和真实 QQ 消息回归未验证。
- 回滚/检查点：四个分组提交见部署补记。原有未跟踪的 `docs/CONTEXT_MANAGEMENT_AUDIT.md` 未纳入提交并予以保留。
- 测试：`timeout 300 .venv/bin/python -m unittest discover -s tests -q`：406 tests，OK；未调用真实模型或 QQ。

## 任务记录

| ID | 状态 | 依赖 | 主要修改 | 测试/结果 | 未完成/下一步 |
|---|---|---|---|---|---|
| CV2-01 | COMPLETED | — | `app/context/contracts.py`、`app/memory.py`、`app/bot_service.py`、`app/handlers/llm_handler.py`、`app/main.py`、`app/models.py` | `.venv/bin/python -m unittest tests.test_memory tests.test_qq_context tests.test_bot_service.SummaryTest tests.test_bot_service.StorageOffloadIntegrationTest tests.test_agent tests.test_sfm_knowledge tests.test_main.OneBotAdmissionTest.test_reply_from_old_context_epoch_is_not_sent -q`：29 tests，OK | 请求行 ID/epoch 边界、旧 schema additive migration、动态上下文不进 system role、epoch 失效时禁止写回和发送；外发请求已经开始后不可撤回 |
| CV2-02 | COMPLETED | — | `app/context/budget.py`、`app/config.py`、`app/llm.py`、`app/main.py`、`app/handlers/llm_handler.py`、`app/commands/service.py` | `.venv/bin/python -m unittest tests.test_context_budget tests.test_usage_budget tests.test_commands.CommandServiceTest.test_enforce_mode_rejects_model_switch_without_explicit_profile tests.test_main.OneBotAdmissionTest.test_enforce_missing_active_profile_degrades_readiness_without_startup_failure -q`：36 tests，OK；更广回归 `.venv/bin/python -m unittest tests.test_context_budget tests.test_usage_budget tests.test_agent tests.test_agent_integration tests.test_commands tests.test_main -q`：89 tests，OK | UTF-8 JSON heuristic 非 tokenizer 校准；当前 mode 默认 off，生产启用前需明确配置活动模型 Profile |
| CV2-03 | COMPLETED | 01 | `app/context/contracts.py`、`app/memory.py`、`app/handlers/llm_handler.py` | `.venv/bin/python -m unittest tests.test_memory tests.test_bot_service.SummaryTest tests.test_bot_service.GroupReplyCancellationTest tests.test_sfm_knowledge tests.test_main.OneBotAdmissionTest.test_reply_from_old_context_epoch_is_not_sent -q`：20 tests，OK；阶段回归 `.venv/bin/python -m unittest tests.test_bot_service tests.test_main tests.test_memory -q`：47 tests，OK | SQLite CAS 保跨实例写一致；single-flight 是当前单进程实例范围；摘要不删除原文；多实例下避免重复模型调用未验证 |
| CV2-04 | COMPLETED | 01,02,03 | ContextManager/contracts/budget/LLMHandler/Agent epoch guard | `.venv/bin/python -m unittest tests.test_context_manager tests.test_context_budget tests.test_agent tests.test_memory tests.test_bot_service tests.test_sfm_knowledge -q`：77 tests，OK；`git diff --check`：OK | 固定 snapshot 组装 recent/summary/group memory/quote/附件/@，当前原文只出现一次且在末尾；tools schema 纳入预算；低优先级背景先移除；epoch guard 在组装、Agent dispatch/tool 和摘要 dispatch 前拒绝 stale work。PreparedContext 含 WorkingState 请求种子且不携带权限。摘要请求经共享 LLM client 最终窗口校验；独立摘要输入分段留待 CV2-08 |
| CV2-05 | COMPLETED | 03 | `app/memory.py`、`app/config.py`、`app/main.py`；新增 `app/context/history.py`、`tests/test_history_retrieval.py` | `.venv/bin/python -m unittest tests.test_history_retrieval tests.test_context_manager tests.test_memory tests.test_bot_service tests.test_main tests.test_agent -q`：75 tests，OK；`git diff --check`：OK | SQLite临时库1000行中文/英文术语检索、FTS输入转义、旧Schema保留和可重启500行批量回填、更新/撤回/reset/purge/TTL删除的索引一致、容量健康降级和IO错误fail closed均通过；未触碰生产DB |
| CV2-06 | COMPLETED | 04,05 | `app/context/history.py`、`app/context/manager.py`、`app/memory.py`、`app/qq_context.py`、`tests/test_history_retrieval.py`、`tests/test_context_manager.py` | `.venv/bin/python -m unittest tests.test_history_retrieval tests.test_context_manager tests.test_qq_context -q`：23 tests，OK；`git diff --check`：OK | 近期120候选/24选择、回复链3层、FTS命中扩邻域±1并按RRF合并、来源去重和预算裁剪；历史检索超时降级到近期上下文。增加默认不接线的向量契约、显式注入本地encoder的Chroma适配器、SQLite outbox与hash/群/epoch回查，Fake embedding/向量故障可测。生产无向量开关、未加载/下载模型，真实Chroma集成未验证。 |
| CV2-07 | COMPLETED | 04,05 | `app/memory.py`、新增 `app/context/memories.py`、`app/context/manager.py`、`app/commands/service.py`、`app/main.py`；新增 `tests/test_structured_memory.py`，调整 context manager 测试 | `.venv/bin/python -m unittest tests.test_structured_memory tests.test_memory tests.test_context_manager tests.test_commands tests.test_main -q`：69 tests，OK；`git diff --check`：OK | `memories`原ID/内容保留；结构列、revision/source表及FTS追加迁移；legacy标`legacy_unknown`，旧user scope不进入群上下文；人工`/memory add`经现有admin gate写`admin_confirmed`；普通user assertion私有；typed成功ToolResult+Evidence才接受tool_observed；同key冲突保留并停用注入；删除逻辑撤回、revision/source保留；人工记忆检索最多2000估算token。FTS回填与历史回填合并同一受管后台任务，修复首次试验中队列容量不足导致单独记忆索引回填被跳过的问题。 |
| CV2-08 | COMPLETED | 03,07 | `app/memory.py`、`app/config.py`、`app/handlers/llm_handler.py`、`app/context/manager.py`；新增 `app/context/summaries.py`、`tests/test_context_summaries.py`；调整 `tests/test_bot_service.py` | `.venv/bin/python -m unittest tests.test_context_summaries tests.test_bot_service.SummaryTest tests.test_memory tests.test_context_manager tests.test_commands tests.test_main -q`：69 tests，OK；`git diff --check`：OK | 结构化摘要严格校验 schema/source row ID；按消息数或字节代理token触发，输入最多256KiB/100行；同事务CAS写summary+episode；每批覆盖非重叠新原文，不循环重写previous_summary；源行编辑/撤回使episode失效；原文保留。Fake LLM格式测试不等于真实摘要语义质量验证。 |
| CV2-09 | COMPLETED | 02,04 | `app/context/compaction.py`、`app/context/contracts.py`、`app/context/manager.py`、`app/tools/contracts.py`、`app/tools/registry.py`、`app/tools/agent.py`、`app/llm.py`、`app/handlers/llm_handler.py`；新增 `tests/test_agent_compaction.py` | `.venv/bin/python -m unittest tests.test_agent_compaction tests.test_agent tests.test_agent_integration tests.test_sfm_workflow tests.test_recipe_query tests.test_hybrid_retrieval -q`：64 tests，OK；`git diff --check`：OK | Agent 每轮模型调用和最终综合前都预算拟合；先按优先级丢弃可选 ContextItem，再按完整 assistant tool_calls + 所有 tool results 协议块归档；强制保留 system 与当前请求；校验 call/result ID 一对一；归档元数据标记工具结果状态并要求精确事实重新授权查询。EvidenceStore 请求/群/用户/epoch隔离，有字节/条数/单记录/TTL上限，显式 partial 与 hash/source/revision/version/query conditions；新增 `task_evidence_read` 通过 ToolRegistry 策略分页读取当前请求已缓存内容，不能恢复已被 Registry 裁掉的数据，仍标 partial；真实精确事实仍需重调已有领域工具；未在compaction中调用额外LLM。|
| CV2-10 | COMPLETED | 09 | `app/context/contracts.py`、新增 `app/context/working.py`、`app/memory.py`、`app/handlers/llm_handler.py`、`app/tools/agent.py`；新增 `tests/test_working_memory.py` | `.venv/bin/python -m unittest tests.test_working_memory tests.test_agent_compaction tests.test_agent tests.test_agent_integration tests.test_context_manager -q`：34 tests，OK；阶段回归 `.venv/bin/python -m unittest tests.test_commands tests.test_bot_service tests.test_memory tests.test_working_memory tests.test_context_summaries tests.test_main -q`：92 tests，OK；`git diff --check`：OK | startup running→interrupted，不自动续跑；reset/purge 原子删除工作状态但不碰原文/长期记忆/定时任务 |
| CV2-11 | COMPLETED | 06,08,10 | `app/commands/service.py`、`app/handlers/llm_handler.py`、`app/memory.py`；调整 `tests/test_commands.py`、`tests/test_bot_service.py` | `.venv/bin/python -m unittest tests.test_commands tests.test_bot_service tests.test_agent_compaction tests.test_working_memory tests.test_memory tests.test_context_summaries tests.test_main -q`：104 tests，OK；`git diff --check`：OK | 修改仅针对上下文子命令；旧show behavior保留，旧 summary/clear故意移除；其他工具/命令均保留 |
| CV2-12 | COMPLETED | 11（传递依赖全任务） | 新增 `tests/test_context_v2_evaluation.py`；覆盖测试分布于 context、memory、agent、commands 与 main 测试 | `timeout 300 .venv/bin/python -m unittest discover -s tests -q`：406 tests，OK；`git diff --check`：OK | Fixture A/B、临时 SQLite/Chroma fake-embedder 集成、18 类场景覆盖和离线评估完成。未运行真实模型、真实 GTNH/SFM Parser 或生产验证；结果见实施报告。 |

## 设计修正

已修订 `docs/CONTEXT_V2_DESIGN.md`、`docs/CONTEXT_V2_TASKS.md`、`docs/CONTEXT_V2_COMMANDS.md`：

1. Context mode 为 `off/shadow/enforce`；shadow不提供保护；非活动模型缺Profile不阻塞启动；运维声明标 `operator_declared`；生产enforce要求所有活动模型配置完整。
2. 容量降级与数据库/磁盘错误分离；非持久化请求不能伪造row/source；存储故障fail closed。
3. 命令采用 `/context reset` 与 `/context purge`，分别表示逻辑epoch切换和物理历史删除。
4. Evidence completeness显式区分complete/partial/reference_only/unavailable，域服务只读重取受原权限及预算约束。
5. 75%只作为压缩评估阈值，确定性压缩优先；语义checkpoint是有界可选调用。
6. CV2-12增加固定Fixture的V1/V2 A/B；不声称真实模型质量提升。

## 设计与阻塞日志

- 实际源码基线确认：`63a9da1`；原有V2三文档和Context Audit为未跟踪文件，均保留。
- 未读取或输出任何密钥/Token；未运行网络或付费请求。
- CV2-01 实际修改并运行 29 项定向测试，全通过；新增测试验证相同文本的不同消息 ID、OneBot 重投幂等、后到消息排除、群隔离、clear epoch 写入保护、旧 schema 数据保留、不可信 Quote/Summary/Memory 只处于 user role，以及旧 epoch 回复不会发送。
- 测试期间初次 stale-reply 测试尝试直接赋值 slots dataclass 方法失败；改为在测试中 patch 类方法后通过。FastAPI 现有 `on_event` deprecation 与 usage cap 配置 warning 非本任务失败。
- CV2-01 的迁移仅在临时 SQLite 执行，未访问或修改生产库；clear 与摘要并发 CAS 在 CV2-03 实施。
- CV2-02 在统一 `DeepSeekClient` 请求边界应用估算；profile 环境格式为 `LLM_CONTEXT_WINDOW_PROFILES` JSON，key 是精确的 `provider:model`，缺省状态 `operator_declared`；模式通过 `LLM_CONTEXT_BUDGET_MODE`，默认 `off`。enforce 的活动默认模型缺 profile 时 `/ready` 返回 503，但应用可启动；无 profile 的其他模型不会让启动失败，`/model use` 会拒绝选择，`/model list` 会标出受阻模型。
- CV2-02 估算方法为完整 JSON payload（含 tools schema）UTF-8 bytes × 0.40、消息开销与固定开销；预留 `max(512, 10% context window)`，Profile 可声明 max/preferred output，缺失时 enforce 添加默认 2048 并按可用空间收紧。该方法未用真实 tokenizer 校准，不是精确 token 账本或数学硬保证。
- CV2-02 对结构化 provider context-length 错误最多将 max_tokens 减半重试一次；流式只在首个内容块前重试，保留已有 usage 预约/结算语义。无 Profile 在 enforce 中于 HTTP 与 usage reservation 之前拒绝；没有真实 API 调用。
- CV2-02 定向 Fake/Mock 测试覆盖 profile 精确匹配、off/shadow/enforce、8k/32k 窗口、payload 上限、tool schema 计入、普通/Function Calling/streaming、模型切换、缺 profile readiness 与单次窗口错误恢复；无真实模型/tokenizer 验证。
- CV2-03 增加 `conversation_summaries.epoch/revision`、群 `summary_covered_id`；摘要输入使用事务快照，单群并发在同一 handler 进程内 single-flight，提交在 SQLite `BEGIN IMMEDIATE` 内 CAS epoch/revision/covered ID。摘要绝不删除原始消息；clear 会推进 epoch/revision 且删除摘要，迟到结果提交失败。请求 prompt 只读不晚于请求快照 revision 且覆盖行小于当前请求行的摘要。
- CV2-03 摘要空结果/超长结果、模型异常、取消、reset/clear 竞态和 revision 冲突均不推进覆盖位置；摘要 prompt 的历史数据明确作为 untrusted user data。
- CV2-03 旧 `message_history_limit` 不再是写入时硬删上限；存储增长与容量降级/原文生命周期治理留给 CV2-05。单进程 single-flight 不是跨进程 LLM 去重协议，跨实例依靠 CAS 保正确提交。
- CV2-04 将 prompt 拼装迁入 `ContextManager`，增加 `PreparedContext`、带来源/完整度/优先级的 `ContextItem` 和请求级 `WorkingState` seed。Agent 调度器保留，通过 guard 在新模型轮次、工具调用及 final synthesis 前检查 epoch；已经发往网络的调用不可撤回。预算按完整消息、tool schema 与输出 reserve 重算，当前请求和可信策略不压缩。50 轮人工 Fixture、snapshot scope/reset 竞态、非持久化来源与超窗测试通过。容量降级策略等待 CV2-05。
- CV2-05 给 `message_history` 增加可见性、内容hash、估算token和撤回元数据，建立群/epoch受限 FTS。所有新写入、编辑、撤回和物理删除与SQLite FTS在同一事务；旧历史通过可续跑的最多500行batch回填，启动时用受管后台任务续做。始终维护 unicode61 + CJK bi-gram技术词索引；运行时探测并额外使用SQLite trigram，只有明确的 tokenizer 不支持错误才降级，I/O错误仍启动失败。默认单群上限20000行/512MiB，触顶返回明确 `capacity_degraded` 无row ID的快照，Bot可继续本次回复并标记 `context_persisted=False`；健康统计/epoch读写成功是降级前提。数据库/磁盘异常不捕获为容量。新加 `reset_group_context` 仅切epoch、保留旧原文；物理purge先epoch失效并tombstone，再最多500行一批擦除；旧 `clear_group_context` 仍保持破坏性兼容，CV2-11接入新 reset/purge 命令。TTL批删API已提供但不自动执行，默认 `CONTEXT_HISTORY_AUTO_PRUNE=false`，生产留存策略未生效。
- CV2-06 把近期消息上限从8提高为24、近期候选至120；回复链最长3层；满足召回提示或近期消息不足时执行群/epoch/snapshot上界受限FTS，检索锚点扩±1邻域后按RRF融合并让预算决定最终保留。向量能力明确为可选注入：Chroma适配器必须收到调用者显式提供的collection与本地encoder，不会构造/下载模型；SQLite outbox按行排队，同步时从当前可见epoch重读，搜索再按群/epoch/hash/current-row回查。向量故障仅把vector_status标记unavailable、继续FTS。外部create_app当前没有配置或启用语义聊天索引，Fake契约通过不等于Chroma/Embedding已集成。
- CV2-07 为原`memories`新增结构元数据、稳定visibility/scope、revision和来源表，保留旧ID与内容并用lazy 500行batch补FTS。老group/user记录皆`legacy_unknown`；用户旧记录`legacy_unscoped`，避免跨群暴露；上下文仅注入有效的管理员确认/工具观察事实，词项匹配优先，否则至多回落两个群级偏好，总2000估算tokens。当前admin-only `/memory add`写入admin_confirmed与QQ命令来源；工具观测只接受成功的typed ToolResult及Evidence；模型推测没有提升入口。明确key冲突保存两条并标conflict；撤回保留源和历史修订。群历史purge把关联memory sources标`source_deleted`但保留人工事实。结构化记忆Fixture覆盖旧schema ID迁移、隔离、来源、重复、冲突、逻辑删除、source失效；未运行真实模型事实抽取。
- CV2-08 增加`episodic_memory`与`episode_fts`；模型输出严格JSON，决定/未决项引用本批允许的原文行ID；每批只摘要不重叠新原文，不再把previous summary反复压缩。触发条件为消息数或估算token；每次连续源区间输入最大256KiB/100行，CAS同时提交latest summary、episode、来源hash和FTS；提交前逐行校验可见和内容未变。源消息编辑/撤回使相关episode失效，purge删除episodes，reset靠epoch隔离；上下文按当前epoch最多取4条且标作untrusted derived summary。Fake JSON/引用/CAS/并发/清空/原文保留和限额测试通过；真实模型结构输出可靠性及摘要语义保真未知。
- CV2-09 在每次 Agent Function Calling 调用前（及 max-round final synthesis 前）运行 `AgentContextCompactor.fit`，预算包含当前 tools schema 与 max_tokens。先按 ContextMessagePlan 只丢弃 optional item，仍超限后整轮归档最老 ProtocolBlock；保留完整 tool_call/tool_result 协议，块验证要求每个ID恰好一对。归档只含轮次、工具名、call ID、result status、Evidence refs，并明确原输出不存在、精确事实需重查；不把工具文本提升为 system。EvidenceStore 有请求/群/用户/epoch scope、单记录/总字节/条数/TTL上限和读取长度限制，保存结果hash与source metadata。真实领域来源分段重读未注册为新模型工具，compaction后需重新调用已有Registry工具。测试先发现Evidence查询条件dict导致既有Evidence不可哈希，改成不可变元组后通过。Fake 8轮Agent调用、整块归档、协议ID配对、超大必需请求预派发拒绝和Evidence scope测试通过；定向回归64项通过，未调用模型API。
- CV2-10 增加`agent_tasks`与append-only `agent_checkpoints`，task scope包含request/group/user/epoch，所有checkpoint用SQLite BEGIN IMMEDIATE + revision CAS，状态和JSON限制32KiB、TTL默认一天且上限七天。Agent开始时存入goal/约束，只有工具真实返回后记录工具名+状态和Evidence refs；进度以不可信user JSON提供给后续轮次，不含内部scope ID或权限。正常结束标completed；取消尽力记录cancelled；失败记录failed。MemoryStore初始化把未终止running任务标成interrupted，不自动触发LLM/工具。reset/purge事务内删工作任务和checkpoint而保留普通历史/长期记忆/定时任务。Fake验证实际Agent tool→checkpoint→final，CAS冲突，跨用户/群隔离，重启interrupted，reset只清工作态以及completed不可重用；34个针对性测试通过。 |
- CV2-11 删除旧`/context summary`与`/context clear`，加入`show --summary`、`stats`、管理员`compact/reset/purge`及管理员`memory search [--kind fact|episode|all] [--limit]`，保留其它记忆命令且不增加`/task status`。reset/purge确认token目标包含群和epoch；reset保留旧原文且切epoch，purge先tombstone然后每次最多物理删除500条，重复命令可继续批次；memory search按管理员当前群和用户scope查询并返回verification/source refs。stats将真实请求payload token估算显式报UNKNOWN，历史token标作估算。compact复用原single-flight摘要服务；该命令仅在管理员调用时可能发起既有摘要模型请求，单测未调用外部模型。命令、handler、storage回归92项通过。 |
