# Codex 执行任务清单

本清单是后续逐项实施用的规划，不表示本回合执行了任何重构。每项任务完成后先运行该任务指定测试与对应现有 unittest；每个阶段完成后运行该阶段相关回归。P7-01 是最终完整兼容验收，不是唯一回归入口。测试报告区分 Mock、人工 Fixture、本地组件集成、真实数据集验证和外部依赖未验证。提交每个任务时，记录改动文件、测试命令、结果、数据状态与回滚点。当前允许执行范围仅为用户指定的规划文档。

## 任务依赖图

~~~mermaid
flowchart LR
  P001[P0-01 鉴权] --> P101[P1-01 Registry]
  P002[P0-02 SSRF] --> P101
  P003[P0-03 队列] --> P004[P0-04 deadline / cancel]
  P003 --> P101
  P004 --> P101
  P006[P0-06 预算] --> P101
  P005[P0-05 阻塞 I/O] --> P202[P2-02 混合检索]
  P007[P0-07 PDF Chroma 最小修复] --> P202
  P101 --> P102[P1-02 迁移现有工具]
  P102 --> P103[P1-03 命令兼容 / policy]
  P103 -. 可选 / 延后 .-> P104[P1-04 用户 SessionKey 隔离]
  P103 --> P201[P2-01 文档 Contract / ingest]
  P201 --> P202
  P202 --> P203[P2-03 generation 更新删除]
  P203 --> P204[P2-04 文档 Fixture]
  P103 --> P301[P3-01 本地仓库边界]
  P301 --> P302[P3-02 搜索 / 行读取]
  P302 --> P303[P3-03 Git Fixture]
  P103 --> P401[P4-01 Recipe 模型 / 存储]
  P401 --> P402[P4-02 Importer]
  P402 --> P403[P4-03 Query Tool]
  P403 --> P404[P4-04 Recipe Fixture]
  P204 --> P501[P5-01 SFM 来源接入]
  P501 --> P502[P5-02 Validator / Fake]
  P502 --> P503[P5-03 bounded repair]
  P004 --> P503
  P006 --> P503
  P503 --> P504[P5-04 SFM Fixture]
  P204 --> P601[P6-01 真实文档数据]
  P401 -. 可选样本 checkpoint .-> P602[P6-02 配方导出评估]
  P402 --> P602
  P602 --> P603[P6-03 真实配方集成]
  P504 --> P604[P6-04 真实 SFM Parser]
  P001 --> P701[P7-01 完整兼容回归]
  P002 --> P701
  P003 --> P701
  P004 --> P701
  P005 --> P701
  P006 --> P701
  P007 --> P701
  P101 --> P701
  P102 --> P701
  P103 --> P701
  P001 --> P702[P7-02 集成 / 并发回归]
  P002 --> P702
  P003 --> P702
  P004 --> P702
  P005 --> P702
  P006 --> P702
  P007 --> P702
  P103 --> P702
  P204 --> P702
  P303 --> P702
  P404 --> P702
  P504 --> P702
  P702 --> P703[P7-03 数据能力验收]
  P601 --> P703
  P603 --> P703
  P604 --> P703
~~~

P0-01、P0-02、P0-03、P0-05、P0-06、P0-07 可并行启动；P0-04 在 P0-03 后完成；其余依赖按图执行。P1-04 可在 P1-03 后独立排期，但不阻塞 P2–P5 或 P7-01 / P7-02。P6 的真实数据和外部 Parser 不阻塞 P0-P5 的单测、阶段回归或 Fixture 验收。

## P0：安全与运行稳定性

### P0-01 OneBot 回调 fail closed

- **目标：** callback token 缺失时拒绝事件并给出清晰启动 / readiness 状态；保持已正确配置时现有 header 兼容。
- **前置依赖：** 无。
- **允许修改文件：** app/config.py、app/natcapqq_adapter.py、app/main.py、tests/test_adapter.py、tests/test_bot_service.py；仅在需要时新增 tests/test_main.py。
- **新增文件：** 可选 tests/test_main.py。
- **接口变更：** verify_callback_token 的空 token 语义改为拒绝；启动配置校验不输出 token 值。
- **实施步骤：** 保留 Bearer / raw header 兼容；拒绝空配置与错误 token；health / readiness 区分配置错误，不回显敏感值；更新配置说明。
- **测试方法：** unittest 覆盖缺失、错误、正确 Bearer、正确 raw token、大小写 header、配置检查。
- **验收标准：** 空 secret 不能接收回调；正确配置仍能解析事件；任何日志不包含 token。
- **回滚方式：** 单任务 revert；紧急回滚前须由部署者确认风险，不能以默认放行作为无提示回退。
- **依赖真实数据：** 否。
- **是否需要外部网络：** 否。
- **是否需要人工确认：** 生产启用前确认 NapCat callback header 配置和切换窗口；单元实现不依赖确认。

### P0-02 SSRF 与 redirect 校验

- **目标：** 所有由用户 / 模型提供 URL 的抓取，都保证实际 socket 连接目标是本次经过策略批准的公网 IP，且逐跳校验 redirect；仅 DNS 预解析 / 检查不算完成 SSRF 防护。
- **前置依赖：** 无。
- **允许修改文件：** app/tools/external.py、app/config.py、tests/test_agent.py；可新建 tests/test_web_security.py。
- **新增文件：** 可选 app/tools/url_policy.py、tests/test_web_security.py。
- **接口变更：** fetch_web_page 经 SafeFetchTransport / URL policy；搜索结果 shape 保持兼容。安全 client 使用 trust_env=False、follow_redirects=False；不继承环境 proxy。
- **实施步骤：** URL policy 先拒绝非 http(s)、userinfo、异常端口、控制字符及编码 authority 歧义；主机 IDNA 规范化，解析 IPv4 / IPv6 全量结果并拒绝 private、loopback、link-local、multicast、unspecified、reserved、IPv4-mapped IPv6 指向的非公网地址及特殊 IP 表达。把通过验证的解析 IP 固定为实际连接器的 dial target，原 URL hostname 不改，确保 HTTPS SNI、Host 和证书 hostname verification 使用原主机；拒绝调用者自定义 Host，并避免跨 host / port / approved-IP 错误复用连接池。自动 redirect 禁止；读取 Location 后每跳完整重复 URL / DNS / IP / 连接策略。限制跳数、连接和总超时、响应头 / MIME、原始压缩字节和解压后字节。HTTPX 公开 transport 抽象不直接提供 per-request 固定 DNS dial 参数；若需 httpcore network backend，版本锁定并视为需集成验证的低层边界，不能退化成 DNS 预检查。Jina Reader fallback 不能接收用户原始 URL，P0-02 安全路径本地失败关闭；只有代理自身能保证同等 pinning / redirect 策略时才可使用可信 egress proxy 替代。
- **测试方法：** MockTransport 覆盖请求策略 / redirect / body；Fake Resolver 覆盖解析和归一化；受控 connector + 本地测试服务器验证实际 peer IP 是批准地址，HTTPS 场景验证原 hostname 的 SNI、Host 和证书检查。覆盖 IPv4、IPv6、IPv4-mapped IPv6、IDNA、百分号编码、十进制 / 八进制 / 十六进制 / 短整数 IP、混合 A/AAAA、DNS rebinding、redirect 每跳转私网、循环、代理环境变量、Jina 不接收 URL、超时、超大 raw / 解压响应。
- **验收标准：** 只要 connector 不能证明实际 TCP peer 与批准 IP 一致就拒绝运行 / fail closed；未经批准 host 不触发网络请求；正常公开站点在受控 TLS 校验下兼容；Mock 单测不单独作为连接 pinning 通过证据。任何企业代理例外需由可信出口侧证明完整执行同策略。
- **回滚方式：** 从 Registry 关闭安全抓取工具或回滚 adapter，但不得恢复无界旧 fetch / Jina 原 URL fallback 对任意输入开放；若没有安全 connector，fetch_url 返回不可用，Web Search 仍可独立走已限定的固定 provider API。
- **依赖真实数据：** 否。
- **是否需要外部网络：** 否。
- **是否需要人工确认：** 生产网络策略、代理例外和支持的 HTTPX / httpcore 锁定版本需维护者确认；未确认时不开启任意 URL 抓取。

### P0-03 有界消息准入与 Worker 生命周期

- **目标：** 替代每条回调无限 create_task；限制队列容量、worker 数和全局并发，应用关闭时停止接收并 drain / 取消工作。
- **前置依赖：** 无；发布时同时启用须先完成 P0-01。
- **允许修改文件：** app/main.py、app/config.py、app/bot_service.py、app/qq_dispatcher.py、tests/test_bot_service.py；可新建 tests/test_main.py。
- **新增文件：** 可选 app/task_runtime.py、tests/test_main.py。
- **接口变更：** 新增 bounded submit / job status 边界；超满时定义 HTTP 503/429 和 OneBot 重试语义。
- **实施步骤：** lifespan 创建固定 worker 与有界 asyncio.Queue；admission 在创建任务前完成；追踪消息 / 摘要任务；shutdown 停止入队、限时 drain、再 cancel；保留 EventDeduplicator 并声明其进程内语义。
- **测试方法：** fake handler 验证最大队列、满载响应、并发峰值、重复消息、异常 worker 存活、shutdown drain 和 cancel。
- **验收标准：** 活跃处理数与队列长度不超过配置；过载不静默 accepted；worker 任务无未收集异常。
- **回滚方式：** feature flag 切回旧 dispatch；保留旧 handler 入口；无需 DB 回滚。
- **依赖真实数据：** 否。
- **是否需要外部网络：** 否。
- **是否需要人工确认：** 满载时 Bot 回调的返回码和 NapCat 重试行为需部署前确认。

### P0-04 Agent 总 deadline 与取消传播

- **目标：** 一个单调时钟 deadline 覆盖模型调用、工具并发等待、工具运行、最终回答与重试；超时后取消所有子任务。
- **前置依赖：** P0-03 的受管任务生命周期。
- **允许修改文件：** app/tools/agent.py、app/llm.py、app/handlers/llm_handler.py、app/bot_service.py、tests/test_agent.py。
- **新增文件：** 无。
- **接口变更：** Agent 请求接收 deadline / cancel scope；保持 run_agent_loop 旧参数可由兼容 wrapper 推导。
- **实施步骤：** timeout=min(per-tool limit, remaining deadline)；对 gather 整体限时；捕获 CancelledError 后取消并 await 子任务；finally 清理群忙标记与 progress task。
- **测试方法：** Fake LLM / tool 用 Event 控制卡住、部分完成、超时、主动取消和重复调用；断言所有子任务结束。
- **验收标准：** 任务耗时不超过 deadline 加清理宽限；取消时没有后台工具继续运行或群锁泄漏。
- **回滚方式：** 恢复旧 run_agent_loop wrapper；新增 deadline 类型保持可选。
- **依赖真实数据：** 否。
- **是否需要外部网络：** 否。
- **是否需要人工确认：** 否。

### P0-05 同步 SQLite / Chroma 出口受控

- **目标：** 避免 SQLite、Chroma 和 sentence-transformers 编码在 async 事件循环内阻塞，同时限制后台线程并发。
- **前置依赖：** 无。
- **允许修改文件：** app/memory.py、app/rag.py、app/vector_rag.py、app/handlers/llm_handler.py、app/main.py、tests/test_memory.py、tests/test_rag.py。
- **新增文件：** 可选 app/storage_executor.py、tests/test_storage_executor.py。
- **接口变更：** 异步调用边界通过有界 executor 或服务 facade；同步 Store API 可暂时保留供脚本调用。
- **实施步骤：** 明确各 Store 连接 / 锁语义；异步路径统一 offload；限制线程数与排队数；异常和取消时关闭连接；embedding 冷启动不阻塞 loop。
- **测试方法：** Fake blocking store 与 heartbeat 验证 loop 可响应；Store unittest、并发写和取消清理。
- **验收标准：** 慢检索不阻塞其他 async coroutine；连接和 executor 在线程压力后正常关闭。
- **回滚方式：** 每个 Store 通过可切换 facade 保留同步调用；逐一恢复 async caller。
- **依赖真实数据：** 否。
- **是否需要外部网络：** 否。
- **是否需要人工确认：** 否。

### P0-06 全局 / 群 / 用户模型预算与 usage 账本

- **目标：** 每次 provider HTTP 调用前进行并发安全的预算预留，完成后按可信 usage 结算；失败、取消、usage 缺失时不错误释放或伪造消耗。
- **前置依赖：** 无；P0-03 的全局 semaphore 可并行。
- **允许修改文件：** app/llm.py、app/config.py、app/memory.py、app/main.py、app/handlers/llm_handler.py、tests/test_agent.py、tests/test_memory.py；可新增 usage tests。
- **新增文件：** 可选 app/usage_budget.py、tests/test_usage_budget.py。
- **接口变更：** 增加 BudgetController 的 reserve_budget / settle_budget / release_budget / recover_expired_reservations；模型响应 metadata 保留 optional provider usage，并保持现有 chat / chat_with_tools / chat_stream 调用签名。预算 scope 独立包含 global、group 和可选 user，不依赖 P1-04 SessionKey。
- **实施步骤：** SQLite 以 BEGIN IMMEDIATE 同事务检查已结算用量 + 未决 reservation 并插入预留；在 DeepSeekClient 内部统一 POST / stream dispatch helper 设置准入钩子，保留 chat / chat_with_tools / chat_stream 公开签名。每个真实模型 HTTP 派发独立 reservation，包括 chat() 内部空回复重试、Agent 工具循环每轮、最终回答和 stream。请求次数是硬准入计数；token 预留按可见输入 / 估算器 / provider-enforced max completion 计算，是保守估计而非实际消耗保证；provider 支持时设置 max_tokens，chat_stream 也必须采用有界 completion 配置。实际 usage 高于预留时记实际值并禁止后续调用，不声称本次没越过估算额度。成功且 usage 可信时 settle 并释放未用预留；没有 usage 时精确 token 字段为 NULL、标 unknown，并保守保留完整 reservation 占额。请求构造失败或可证明未派发时 release；派发后 timeout、取消、HTTP 错误按 provider 可能已处理保守占额。reservation lease 长于总请求 deadline + shutdown grace，活跃 stream 可续租；异常退出后的过期 reservation 标记 unknown_settled，不直接删额；幂等对账后可修正。只有可信费率和 usage 同时存在才算实际费用，否则费用未知 / 估算；若配置了货币硬限额但没有可信费率，必须拒绝该“货币硬限额”模式或要求维护者显式降级为费用未知，不能假称已限费。
- **测试方法：** Fake / Mock LLM 覆盖 usage 结算、缺 usage、错误阶段和取消；两个并发请求竞争最后额度时只准一个；检查预算拒绝不发送 HTTP；请求发送前 release 与发送后 unknown 的差别；实际 usage 超预留后记账并禁止下一次请求；重试 / 多工具轮次分别扣 request count；stream 有 / 无 usage、completion 上限、活跃 lease 续期与进程恢复过期 reservation、幂等 settle / release、跨日 scope、无费率时货币硬限额拒绝 / 显式降级。
- **验收标准：** provider API 调用前预算拒绝；并发请求次数预算无超额准入；所有发送尝试至少有一次请求记账；usage 缺失不虚构 token；已派发但结果未知不全额释放；错误 / 取消释放策略有状态证据；账本无 API key / prompt；无可信费率时不声称精确费用控制。
- **回滚方式：** 关闭预算策略开关并由兼容 invoker 恢复旧调用入口；保留 usage / reservation 表；异常遗留项进入对账而非清库。
- **依赖真实数据：** 否。
- **是否需要外部网络：** 否。
- **是否需要人工确认：** 各 scope 的请求数 / token 预留限额、reservation lease、可信费率来源及无 usage 的保守策略需维护者确认；用户级 budget 不要求启用用户私有 SessionKey。

### P0-07 Chroma 文档完整替换与过期片段清理

- **目标：** 以最小改动修复当前 SFM PDF 索引路径中完整替换 / 删除后旧 chunk 仍可能残留的问题；不实现通用 generation 生命周期。
- **前置依赖：** 无；P2 generation 可进一步完善。
- **允许修改文件：** app/vector_rag.py、app/rag.py、scripts/index_sfm_pdf.py、tests/test_rag.py。
- **新增文件：** 可选 tests/test_vector_rag.py。
- **接口变更：** 尽量保留 VectorRagStore.upsert_chunks(document_name, chunks)；新增 RagStore.delete_document(name) 与 VectorRagStore.delete_document(document_name) 的单文档最小删除 API；空 chunks 作为完整替换为空文档，并清除全部向量。
- **实施步骤：** 沿用 app/vector_rag.py 现有按 document_name 独立 collection 的做法；先准备新文本和 embeddings；随后清除该 collection 的所有旧 vector IDs，再添加新完整集合。显式删除分别清 FTS / SQLite 元数据与对应 Chroma collection，并由脚本协调；仅在两端成功时报告删除完成，失败返回 partial/error，人工或重试调用幂等删除。此任务不创建 generation、active pointer、pending cleanup 或跨库 job。
- **测试方法：** Fake Chroma collection：3 chunks 更新为 1 后旧尾部无命中；重复替换幂等；空 chunks 与显式删除后无向量命中；确认只清理目标 document，不碰其他文档；SQLite 删除后 FTS / chunks / document 元数据均无记录；embedding 失败发生在删除前时保留旧向量；清理两端任一失败时显示 partial/error，重试能完成，不要求原子回滚。
- **验收标准：** 正常完整更新结束后旧 chunk 不可命中；完整删除成功后 SQLite / FTS / Chroma 均无该 document；失败不伪称成功并可重复删除。明确记录限制：SQLite FTS 与 Chroma 没有共同事务；删除旧向量后新 add 失败会导致 vector 暂缺，任一 store 部分删除也可能产生短暂不一致；P0-07 不保证旧 Chroma snapshot、自动恢复或跨库原子性。
- **回滚方式：** revert 最小 API / 调用变更；如 collection 已清空，使用 SQLite rag_chunks / 原 PDF 重建向量，而非声称能回滚到自动保留的旧 generation。P2-03 另行提供通用 generation 策略。
- **依赖真实数据：** 否。
- **是否需要外部网络：** 否。
- **是否需要人工确认：** 否。

## P1：Agent Tool Registry

### P1-01 注册表与结果契约

- **目标：** 增加 ToolSpec、ToolRegistry、ToolContext、ToolResult、Evidence、ToolError、ToolPolicy 与参数验证入口。
- **前置依赖：** P0-01、P0-02、P0-03、P0-04、P0-06。
- **允许修改文件：** app/tools/agent.py、app/llm.py、app/models.py、tests/test_agent.py。
- **新增文件：** app/tools/registry.py、app/tools/contracts.py；相应 tests。
- **接口变更：** Agent 可从 Registry 生成 schema 并 dispatch；老调用由 facade 保持。
- **实施步骤：** 建状态枚举与 JSON Schema 基础验证；策略默认显式；启动时检测重复名称；增加 SearchResult 兼容适配器。
- **测试方法：** Registry 独立单测；重复名、坏 JSON、schema 输出、拒绝权限、错误映射和预算注入。
- **验收标准：** 一个 ToolSpec 同时驱动模型 schema 和执行；旧定义快照无差异。
- **回滚方式：** 将 run_agent_loop 切回静态常量；新增 contracts 文件不被旧路径导入。
- **依赖真实数据：** 否。
- **是否需要外部网络：** 否。
- **是否需要人工确认：** 否。

### P1-02 迁移现有工具执行器

- **目标：** 把现有 Web / weather / GitHub / knowledge handlers 包装成 ToolSpec，删除 schema whitelist 分叉。
- **前置依赖：** P1-01。
- **允许修改文件：** app/tools/agent.py、app/tools/external.py、app/handlers/llm_handler.py、tests/test_agent.py。
- **新增文件：** 可选 app/tools/legacy_tools.py、tests/test_tool_compat.py。
- **接口变更：** 内部执行改用 registry.dispatch；外部函数与 SearchResult 先保留。
- **实施步骤：** 每个旧工具注册原名称 / 参数；将超时、并发、字节和权限策略搬到 ToolSpec；保留每个 tool_call_id 一个 observation；同步采集 Evidence。
- **测试方法：** 假客户端工具循环；旧参数快照；未知、重复、并发上限、异常与引用回归。
- **验收标准：** 所有旧工具行为等价，添加新工具无需再改 if / elif。
- **回滚方式：** feature flag 逐项切回 _execute_native_tool_call 旧分支。
- **依赖真实数据：** 否。
- **是否需要外部网络：** 否，测试全部 Mock。
- **是否需要人工确认：** 否。

### P1-03 命令兼容和默认权限策略

- **目标：** 将 forced tool 命令也送入 Registry，同时明确群成员可用能力和工具权限。
- **前置依赖：** P1-02。
- **允许修改文件：** app/bot_service.py、app/handlers/llm_handler.py、app/tools/agent.py、app/config.py、tests/test_bot_service.py、tests/test_agent.py。
- **新增文件：** 可选 app/tools/policies.py。
- **接口变更：** /weather、/news、/web 用 ToolContext 执行；命令显示保持不变。
- **实施步骤：** 抽取现有 forced-call 映射；基于调用者建立 policy snapshot；当前只读 / 外网能力使用相同 ToolPolicy；新工具必须声明权限。
- **测试方法：** 原命令解析和输出测试；授权 / 拒绝 / 数据服务不可用与权限无关的回归。
- **验收标准：** 旧斜杠命令输出兼容；所有新工具有显式 permission、deadline、result cap。
- **回滚方式：** 单独回滚命令 adapter；保留 Registry dispatch 供普通 Agent 使用。
- **依赖真实数据：** 否。
- **是否需要外部网络：** 否。
- **是否需要人工确认：** 若更改用户可用命令权限，发布前需审阅权限表。

### P1-04 群聊用户 SessionKey 与历史隔离

- **状态：** 可选 / 延后；属于未来用户隔离能力，不是引入 Agent 工具或知识服务的前置。
- **目标：** 保留未来显式 group_id + user_id 私有上下文扩展点；当前默认群共享上下文行为不变，不提前全面重构 MemoryStore。
- **前置依赖：** P1-01；如果实施，应先确认产品需要与 P1-03 命令权限兼容。P0-06 仅提供 user_id 预算身份，不是本任务的 session 依赖。
- **允许修改文件：** 仅在批准该可选任务时修改 app/memory.py、app/qq_context.py、app/bot_service.py、app/handlers/llm_handler.py、app/handlers/command_handler.py、app/config.py、tests/test_memory.py、tests/test_qq_context.py、tests/test_bot_service.py。
- **新增文件：** 可选 app/session.py、tests/test_session_isolation.py。
- **接口变更：** 可增加 SessionKey 和 scoped API；旧 MemoryStore 群级 API 与默认行为保留。ToolContext 的预算 scope 独立于 SessionKey。
- **实施步骤：** 若实际需要，先限定最小只读 history / summary 边界；scope 默认 group，未来 private 模式按平台 + group_id + user_id 定位；不 fan-out 旧群历史；切换使用按群 flag。无明确需求时此任务保持 deferred，不实施存储迁移。
- **测试方法：** 如实施，测试同群双用户隔离、legacy 结果兼容、历史 clear / summary 与额度 scope；否则由本阶段报告标记 deferred，确认预算按 user_id 可独立计量。
- **验收标准：** 无论本任务 deferred 与否，默认群共享上下文不变，P2–P5 可独立完成，P7-01 不依赖本任务。若实施，才要求私有 scope 独立且无历史复制。
- **回滚方式：** 如实施后回滚，切回 legacy group scope；保留 additive 表，不自动删除 / 合并数据。
- **依赖真实数据：** 否。
- **是否需要外部网络：** 否。
- **是否需要人工确认：** 是；只有产品 / 维护者确认需要用户私有会话后才排期；同时确认是否可附加独立、限量的共享群背景，默认不附加其他用户历史。预算按 user_id 计量不需等待本确认。

## P2：通用文档 RAG

### P2-01 Document / Source / Locator 契约和 Markdown 摄取

- **目标：** 增加通用来源、文档、generation、chunk、locator 元数据和 Markdown parser / chunker。
- **前置依赖：** P1-03；不依赖 GTNH 数据。
- **允许修改文件：** app/rag.py、app/config.py、tests/test_rag.py、scripts/index_sfm_pdf.py。
- **新增文件：** app/knowledge/documents.py、app/knowledge/ingestion.py、app/knowledge/__init__.py；人工 Fixture 可新建 tests/fixtures/documents/。
- **接口变更：** 现有 RagChunk 增加可选 provenance 元数据；旧参数兼容。
- **实施步骤：** schema additive migration；实现 Parser 输出 ParsedDocument；Markdown 切片保留 heading 与行范围；稳定生成 chunk ID；index 状态可查询。
- **测试方法：** 人工 Markdown Fixture；行定位、章节路径、hash 幂等、无效编码、空文件、版本元数据。
- **验收标准：** 空数据库启动；无语料返回 NotIndexed；旧 SFM PDF 导入入口可继续工作。
- **回滚方式：** feature flag 回旧 RagStore；只增表保留，migration ledger 标记，不删数据。
- **依赖真实数据：** 否。
- **是否需要外部网络：** 否。
- **是否需要人工确认：** 否。

### P2-02 版本过滤、混合检索与引用

- **目标：** 在 FTS5 + 现有 Chroma 查询上增加一致过滤、RRF、Document Evidence 和明确降级状态。
- **前置依赖：** P2-01、P0-05、P0-07。
- **允许修改文件：** app/rag.py、app/vector_rag.py、app/handlers/llm_handler.py、app/tools/agent.py、tests/test_rag.py。
- **新增文件：** app/knowledge/retrieval.py、tests/test_hybrid_retrieval.py。
- **接口变更：** DocumentKnowledgeService.search(filter, query) 返回 ToolResult / Evidence；保留 RagStore.search wrapper。
- **实施步骤：** 两路 top-k；统一过滤；按 revision / chunk 去重；RRF 合并；vector unavailable 时返回 FTS 部分结果。
- **测试方法：** Fixture + Fake Embedder；各路排名融合、版本隔离、空索引、只 vector / 只 FTS、稳定引用。
- **验收标准：** 同一数据源定位到文档 + 章节 + 行 / 页；不混旧版本 chunk；RRF 排序可重复。
- **回滚方式：** 配置切回旧 FTS / 拼接路径。
- **依赖真实数据：** 否；仅 Fixture。
- **是否需要外部网络：** 否。
- **是否需要人工确认：** 否。

### P2-03 安全 generation 更新、删除与重建

- **目标：** 为通用多来源索引实现 SQLite / FTS / Chroma generation 生命周期、跨存储一致性和中断恢复；不重复实现 P0-07 的 SFM PDF 单文档旧 chunk 最小修复。
- **前置依赖：** P2-01、P2-02。
- **允许修改文件：** app/rag.py、app/vector_rag.py、scripts/index_sfm_pdf.py、tests/test_rag.py。
- **新增文件：** 可选 app/knowledge/index_jobs.py、tests/test_index_lifecycle.py。
- **接口变更：** 新增 replace_generation、delete_document、rebuild_index 和 index_status。
- **实施步骤：** staged generation；校验 SQLite / FTS / Chroma ID；维护 active revision；增量替换；删除使用 tombstone；中断恢复；旧 generation cleanup 失败进入 pending cleanup；重建幂等。跨存储不能用单一事务假装原子：先保证查询只读 SQLite 中确认 active 的 generation，未切换时保留旧 active；active 切换后 cleanup 可重试。脚本支持 dry-run。
- **测试方法：** Fake Chroma 注入写失败 / 删除失败；Fixture 内容缩短、删除、重新导入、重复运行与恢复。
- **验收标准：** active 查询只返回 active generation；构建或校验失败时不切换 active；删除 tombstone 不再被查询，FTS / Chroma 清理可幂等重试；pending cleanup 可观察并恢复。此为通用生命周期保证，与 P0-07 的“正常完成替换后清除旧 SFM PDF chunk、但不保证跨库原子回滚”不同。
- **回滚方式：** active pointer 退回旧 generation；旧数据在审阅通过前保留。
- **依赖真实数据：** 否。
- **是否需要外部网络：** 否。
- **是否需要人工确认：** 真数据目录执行 delete / rebuild 前需 dry-run 和人工确认。

### P2-04 文档 Fixture 全量用例

- **目标：** 独立验证文档通用设施和空知识库行为。
- **前置依赖：** P2-01 至 P2-03。
- **允许修改文件：** tests/test_rag.py、tests/test_hybrid_retrieval.py、tests/fixtures/documents/。
- **新增文件：** 需要时新增人工 Markdown Fixture 与专用测试模块。
- **接口变更：** 无。
- **实施步骤：** 固定两个人工 pack/version；覆盖章节、关键词、向量、RRF、引用、更新、删除、NotIndexed 和 VersionMismatch。
- **测试方法：** unittest、TemporaryDirectory / 临时 SQLite、Fake Embedder / Fake Chroma。
- **验收标准：** 用例报告明确标注“人工 Fixture”；不使用“GTNH 检索已验证”措辞。
- **回滚方式：** 仅回滚新增 fixture / 测试；不改变 Bot 代码行为。
- **依赖真实数据：** 否。
- **是否需要外部网络：** 否。
- **是否需要人工确认：** 否。

## P3：本地 Git 检索

### P3-01 仓库根目录与文件安全服务

- **目标：** 配置不可由模型修改的 repo_id -> root 白名单，建立 commit snapshot 与敏感路径过滤。
- **前置依赖：** P0-02、P0-03、P1-03。
- **允许修改文件：** app/config.py、app/tools/external.py；可新建 app/repositories/local_git.py。
- **新增文件：** app/repositories/__init__.py、app/repositories/local_git.py。
- **接口变更：** 引入 repo_id、SnapshotRef、safe_relative_path；不接受任意绝对路径。
- **实施步骤：** 校验 root / realpath；拒绝越界 / symlink；排除凭证路径、.git、二进制和过大文件；定义只读工作树或 commit object 访问。
- **测试方法：** 临时 root 测路径遍历、软链接、denylist、只读权限、缺失仓库和非法配置。
- **验收标准：** 未配置仓库时 Bot 正常启动并返回 DataUnavailable；路径安全失败不读取文件。
- **回滚方式：** 新服务保持未注册状态；移除配置开关不影响 GitHub 工具。
- **依赖真实数据：** 否。
- **是否需要外部网络：** 否。
- **是否需要人工确认：** 生产需管理员配置允许索引的本地目录。

### P3-02 搜索、行读取和 Agent 工具

- **目标：** 提供本地文件名 / 路径 / 文本搜索、commit 固定文件读取与行范围引用。
- **前置依赖：** P3-01、P1-03。
- **允许修改文件：** app/tools/agent.py、app/handlers/llm_handler.py、app/config.py；扩展 app/repositories/local_git.py。
- **新增文件：** 可选 app/repositories/symbols.py（只有简单语言符号扫描经验证需要时）。
- **接口变更：** 注册 repository_search 和 repository_read，Schema 含 repo_id、snapshot / commit、相对 path、行范围与 limit。
- **实施步骤：** 所有操作固定 SnapshotRef；严格分页 / 输出 bytes；返回 Evidence；先不引入 AST parser。
- **测试方法：** 临时 Git repo Mock 检索、指定行读取、变更工作树时固定 commit、输出裁剪。
- **验收标准：** 两次调用使用同一 commit；模型不能读白名单外内容；引用可定位到 commit 和行。
- **回滚方式：** 从 Registry 移除新工具，保留本地搜索代码与 GitHub 功能。
- **依赖真实数据：** 否。
- **是否需要外部网络：** 否。
- **是否需要人工确认：** 管理员必须明确配置每个可读仓库 root。

### P3-03 临时仓库安全测试

- **目标：** 不依赖外部 GitHub 网络测试本地仓库服务。
- **前置依赖：** P3-01、P3-02。
- **允许修改文件：** tests/；必要时只修复 app/repositories/local_git.py 对应失败行为。
- **新增文件：** tests/test_local_git.py。
- **接口变更：** 无。
- **实施步骤：** TemporaryDirectory 建仓并初始化多 commit；包含敏感文件、超大文件、symlink 和特殊文件名。
- **测试方法：** unittest subprocess 创建本地 repo；不 clone / fetch 网络仓库。
- **验收标准：** 路径、安全、commit、行号、大小、denylist 用例全覆盖。
- **回滚方式：** 回滚本任务新增测试与对应最小修复；不删除真实仓库数据。
- **依赖真实数据：** 否。
- **是否需要外部网络：** 否。
- **是否需要人工确认：** 否。

## P4：配方服务基础设施

### P4-01 Recipe Schema 与结构化 Repository

- **目标：** Contract-first 定义 pack/version/source metadata、Recipe、可判定复杂 Ingredient、原始来源记录和 SQLite 查询索引；不依赖或假定真实 GTNH exporter。
- **前置依赖：** P1-03。
- **允许修改文件：** app/memory.py（共享 DB 初始化若需要）、app/config.py；新增领域模块。
- **新增文件：** app/recipes/models.py、app/recipes/repository.py、app/recipes/__init__.py、tests/test_recipe_repository.py。
- **接口变更：** 新建 PackVersionKey / RecipeRepository / Ingredient / RawRecipeRecord；不修改现有 MemoryStore 用户接口。Ingredient.kind 区分 item_exact、fluid_exact、tag、ore_dictionary、predicate、source_extension，另含 optional NBT / component constraints、role、consumed、interpretation、source locator。
- **实施步骤：** 字段语义与 schema version 先审阅；原始来源 payload / hash / source-native ID 与行 / JSON pointer 保存；unknown fields 不丢失；关键条件未知、单位未知或无法解释时 quarantine。exact ID 与条件匹配分开，不把复杂输入降为精确物品；只做最小共同模型，其他模组扩展靠保留 raw source record 而非预建大量字段。Schema 初稿完成后安排一次可选真实样本可行性检查：只检查维护者已提供、已获准读取的样本，不自动下载、不假设 gtnh-toolkit；没有样本则标记待验证并继续人工 Fixture，不阻塞 P4-02 至 P4-04。
- **测试方法：** 人工构造临时 SQLite 检查外键、索引、Decimal roundtrip、旧 DB 启动、精确 ID 与 Tag / Ore Dictionary / predicate / NBT / component 区分、unknown field 原文保留及未知关键条件 quarantine。所有人工 Fixture 标记 synthetic。
- **验收标准：** 空库可启动；缺失数据不自动构造；多个 pack version 精确隔离；无法解释的条件不能作为精确结果；Schema 报告明确“未用真实 GTNH 样本验证”，除非可选检查确实有真实样本并记录来源 / 版本。
- **回滚方式：** 停用新 repository；只增表保留；不自动 DROP recipe 表。
- **依赖真实数据：** 否。
- **是否需要外部网络：** 否。
- **是否需要人工确认：** Recipe schema 定稿时人工审阅字段 / 单位策略，不能以人工 Fixture 假定真实 exporter schema。

### P4-02 RecipeSource / Importer Contract

- **目标：** 实现导入器协议、校验报告、staging 导入和错误记录，不绑定特定外部项目。
- **前置依赖：** P4-01。
- **允许修改文件：** app/recipes/repository.py、app/config.py；新增 importer 文件。
- **新增文件：** app/recipes/importer.py、app/recipes/source.py、tests/test_recipe_importer.py。
- **接口变更：** RecipeSource manifest / records；RecipeImporter validate / import_dataset。
- **实施步骤：** 检查 schema version、pack key、unit、ID、probability、machine、重复 ID / hash；错误记录带 locator；校验通过后原子切换 active dataset。
- **测试方法：** Fake RecipeSource 和损坏的人工 JSON Fixture；注入事务中断后重试。
- **验收标准：** 无效数据不会覆盖上一 active dataset；报告含接受 / 拒绝数量和原因。
- **回滚方式：** 切回 previous active dataset；保留 staging 与报告供检查。
- **依赖真实数据：** 否；Fixture 仅测导入合同。
- **是否需要外部网络：** 否。
- **是否需要人工确认：** 数据版本激活前人工检查 ImportReport。

### P4-03 RecipeQueryService 与 Tool

- **目标：** 精确查 item producer / consumer、machine recipe、item/fluid 输入输出与能耗字段。
- **前置依赖：** P4-01、P4-02、P1-03。
- **允许修改文件：** app/tools/agent.py、app/config.py；新增 app/recipes/query.py。
- **新增文件：** app/recipes/query.py、tests/test_recipe_query.py。
- **接口变更：** 注册 recipe_lookup / recipe_machine_lookup；required pack_id、pack_version、game_version。
- **实施步骤：** 解析用户查询并校验必需版本；SQL 按精确 DatasetKey 查询；结果标明 exact / conditional / unsupported match quality；仅已知且版本化 resolver 可做条件匹配，unknown / null 字段不猜测；Evidence 关联原始来源定位。
- **测试方法：** 临时 SQLite 人工 Recipe Fixture，ToolRegistry 注入 Fake context；分别断言精确 ID、条件候选、quarantine 不进入正常查询。
- **验收标准：** 未给版本且无唯一默认值时要求澄清；未知版本无跨版本 fallback；producer lookup 默认只按精确输出 ID 匹配；未实现的 tag / predicate 不得伪装为精确命中。
- **回滚方式：** 关闭新工具注册，不影响对话历史与 Web 查询。
- **依赖真实数据：** 否。
- **是否需要外部网络：** 否。
- **是否需要人工确认：** 默认 pack/version 配置需人工设置；未配置则强制显式参数。

### P4-04 规定的 Recipe Fixture 集

- **目标：** 完成用户列出的八种人工数据查询场景，并确保报告边界正确。
- **前置依赖：** P4-01 至 P4-03。
- **允许修改文件：** tests/test_recipe_query.py、tests/test_recipe_importer.py、tests/fixtures/recipes/。
- **新增文件：** tests/fixtures/recipes/*.json 与必要测试模块。
- **接口变更：** 无。
- **实施步骤：** 在既有八种 Fixture 外，增加 exact item / fluid、tag、Ore Dictionary、predicate、NBT / component constraint、catalyst / non-consumable tool、unknown non-critical field 保存、unknown critical field quarantine、raw record locator、exact 与 conditional query 区别。
- **测试方法：** unittest；每例断言状态、match quality、结果、dataset/source locator 与版本。均为人工合成数据。
- **验收标准：** 八种用例通过；报告只写“Recipe Fixture 测试通过”。
- **回滚方式：** 可单独撤销测试与人工 Fixture；不影响数据服务接口。
- **依赖真实数据：** 否。
- **是否需要外部网络：** 否。
- **是否需要人工确认：** 否。

## P5：SFM 编程工作流

### P5-01 SFM 文档 / 示例来源接入

- **目标：** 使用通用文档接口检索版本化 SFM 语法说明、示例、grammar 和源码引用。
- **前置依赖：** P2-01、P2-02。
- **允许修改文件：** app/knowledge/documents.py、app/handlers/llm_handler.py、app/tools/agent.py；新 source manifest。
- **新增文件：** app/sfm/knowledge.py、app/sfm/__init__.py、人工审核的样例 Fixture。
- **接口变更：** sfm_examples 使用 SFM version / Minecraft version filter。
- **实施步骤：** 设计文档 source manifest；只用人工小样例 Fixture 联通；真实 upstream 获取与许可核对另列 P6。
- **测试方法：** 人工 SFM grammar / 示例 Fixture 查询和引用；版本过滤。
- **验收标准：** 只可声称“测试 Fixture 检索”；空 SFM corpus 返回 NotIndexed。
- **回滚方式：** 从 Registry 移除 SFM tool；不影响通用 DocumentKnowledgeService。
- **依赖真实数据：** 否，首版只用人工样例。
- **是否需要外部网络：** 否。
- **是否需要人工确认：** 上游源码 / 文档实质导入前需许可与版本审阅，归 P6。

### P5-02 SFMValidator Contract 与 Fake Validator

- **目标：** 定义 validator、target version、structured diagnostics 和错误状态。
- **前置依赖：** P1-03、P5-01。
- **允许修改文件：** app/tools/agent.py；新增 app/sfm 模块与 tests。
- **新增文件：** app/sfm/validator.py、app/sfm/contracts.py、app/sfm/fake_validator.py、tests/test_sfm_validator.py。
- **接口变更：** Validator 输入限长 source / target version / timeout；输出 VALID / INVALID / UNAVAILABLE / VERSION_UNSUPPORTED / TIMEOUT / ERROR 与行列诊断。
- **实施步骤：** Fake Validator 与生产 adapter 分开命名；安全默认 UNAVAILABLE；fake 状态不能映射为真实 VALID。
- **测试方法：** Fake Validator 固定合法 / 错误示例，验证 diagnostics / 行列序列化、超时与取消。
- **验收标准：** 空真实 Parser 时服务启动，调用明确返回 UNAVAILABLE；fake 结果带 validator_id=fake。
- **回滚方式：** 移除注册的 SFM validator tool；保留纯接口定义供后续适配。
- **依赖真实数据：** 否。
- **是否需要外部网络：** 否。
- **是否需要人工确认：** 否。

### P5-03 有界生成、验证与修复

- **目标：** 在自研 Agent Loop 内完成源码生成 -> Validator -> 最多两轮修复。
- **前置依赖：** P5-02、P0-04、P0-06。
- **允许修改文件：** app/handlers/llm_handler.py、app/tools/agent.py、app/sfm/validator.py。
- **新增文件：** app/sfm/workflow.py、tests/test_sfm_workflow.py。
- **接口变更：** sfm_generate_validate 入参含目标版本 / 描述；出参含 source、validator status、diagnostics 和 evidence。
- **实施步骤：** 固定 deadline 与 token budget；验证器不可用时停止；INVALID 最多修复 2 次；每轮重验；不执行程序。
- **测试方法：** Fake LLM / Validator 覆盖一次成功、多轮失败、timeout、budget、unsupported 和 cancel。
- **验收标准：** 无无限循环；最终状态准确；Fake Validator 的通过不能展示成真实 SFM 编译通过。
- **回滚方式：** 关闭 SFM workflow tool 并恢复文档问答路径。
- **依赖真实数据：** 否，测试使用 Fake Validator。
- **是否需要外部网络：** 否。
- **是否需要人工确认：** 否。

### P5-04 Fake SFM 流程验收

- **目标：** 验证 Agent 工作流上限、诊断格式和真实能力标签。
- **前置依赖：** P5-01 至 P5-03。
- **允许修改文件：** tests/test_sfm_workflow.py、tests/fixtures/sfm/。
- **新增文件：** 少量人工 .sfm Fixture。
- **接口变更：** 无。
- **实施步骤：** 固定样例、fake parser response 和 transcript；检查 fake / real 标签不可覆盖。
- **测试方法：** unittest only。
- **验收标准：** 用例报告写“Fake Validator 工作流通过；SFM Parser 未验证”。
- **回滚方式：** 仅回滚 Fixture / 测试。
- **依赖真实数据：** 否。
- **是否需要外部网络：** 否。
- **是否需要人工确认：** 否。

## P6：真实数据与外部依赖（不阻塞 P0-P5）

### P6-01 GTNH 文档数据准备与导入

- **目标：** 在来源许可、版本和更新时间核对后接入真实 GTNH Wiki / 任务书语料。
- **前置依赖：** P2-01 至 P2-04；数据来源明确。
- **允许修改文件：** app/knowledge 导入器适配、scripts 专用导入入口、数据 manifest / 文档说明；真实语料目录按维护者指定位置配置。
- **新增文件：** 来源适配器、manifest、数据校验报告；大数据通常不提交 Git。
- **接口变更：** 复用 DocumentSource / DocumentKnowledgeService，不改变通用查询 API。
- **实施步骤：** 获取来源许可 / attribution；记录来源版本与 hash；字段映射；dry-run；校验文档 / chunk / 引用数量；再导入 active generation。
- **测试方法：** 真实来源采样和完整导入报告 + 原有 Fixture 回归。
- **验收标准：** 文档目录、revision、hash、引用定位和错误率可审计；否则保持 NotIndexed。
- **回滚方式：** active generation 切回前版或 tombstone 新导入；保留备份与 manifest。
- **依赖真实数据：** 是，GTNH 文档和 / 或任务书文本。
- **是否需要外部网络：** 是，获取数据时需要。
- **是否需要人工确认：** 是，来源许可、内容展示和最终导入版本须确认。

### P6-02 Recipe Exporter 评估和版本化数据映射

- **目标：** 对真实配方导出候选进行可用性、完整字段和许可验证；不能预设 gtnh-toolkit。
- **前置依赖：** Schema 可行性检查可在 P4-01 初稿后立即启动；完整 exporter adapter / 导入合同需 P4-01、P4-02 完成；目标 GTNH / Minecraft / modpack 版本与来源待提供。
- **允许修改文件：** app/recipes/importer.py、专用 adapter、scripts recipe importer、manifest / 评估报告。
- **新增文件：** 被审阅的 exporter adapter、真实源映射、字段覆盖矩阵、fixture snapshot。
- **接口变更：** 仅实现 RecipeSource / Importer adapter，不改变 RecipeQueryService。
- **实施步骤：** 在 P4-01 Schema 初稿后先做一次可选真实样本可行性 checkpoint：只检查维护者已提供并获准读取的少量样本，确认字段形态、目标版本和潜在来源；不自动下载、不克隆大仓库、不假设工具安装。若仍无真实样本，记录“待验证 / 未执行”，不阻塞 P4 Fixture。之后在真实样本可获得时评估 exporter，记录 item/fluid、tag / predicate、NBT / components、单位、多输出、概率、非消耗条件和来源定位覆盖；dry validation 发现关键未知条件就 quarantine 并停止激活。
- **测试方法：** exporter 输出样本 + schema coverage 测试 + 错误记录测试。
- **验收标准：** 来源、版本、许可证、字段覆盖和数据 hash 明确；没有合格来源则结论为不导入。
- **回滚方式：** 移除特定 adapter / active dataset；保留通用 Contract 与历史报告。
- **依赖真实数据：** 是，需真实导出样本；不需要 P0-P5 使用。
- **是否需要外部网络：** 可行性 checkpoint 不需要；完整评估如需获取候选 exporter 或数据需另行明确授权与安排，不隐式联网。
- **是否需要人工确认：** 是，外部工具、许可证、数据字段映射与 importer 激活须审阅。

### P6-03 真实配方导入与一致性验证

- **目标：** 导入通过 P6-02 的真实数据版本，并核验查询输出。
- **前置依赖：** P6-02、P4-04。
- **允许修改文件：** 数据 manifest、专用 importer adapter、导入 / 校验脚本、真实集成测试。
- **新增文件：** real dataset manifest、coverage / consistency report；真实数据是否存仓库由许可决定。
- **接口变更：** 无；调用既有 RecipeImporter / QueryService。
- **实施步骤：** 备份目标 DB；dry-run；导入 staging；核验数量、字段空值、ID、version 和来源 hash；人工对照抽样；最后切 active。
- **测试方法：** 本地真实数据集集成测试；与 Fixture tests 分开报告；不调用模型。
- **验收标准：** 报告明确 pack/version、来源、recipe 计数、拒绝数和人工抽样结果；覆盖不足则标记部分可用。
- **回滚方式：** previous active dataset 恢复；保留导入报告和备份，禁止破坏式清库。
- **依赖真实数据：** 是。
- **是否需要外部网络：** 否，除非从源获取和导入同任务安排。
- **是否需要人工确认：** 是，切换真实 active dataset 前审核。

### P6-04 SFM Parser / Compiler 版本与独立性验证

- **目标：** 验证真实指定 SFM 版本能否在受限子进程运行、能否产出可结构化诊断。
- **前置依赖：** P5-02、P5-04；确定目标 SFM / Minecraft 版本。
- **允许修改文件：** app/sfm 外部 adapter、版本 manifest、scripts 验证入口、集成测试和证据报告。
- **新增文件：** 锁定的 Validator adapter / dependency manifest / real sample report；vendor 源码前先做许可证审查。
- **接口变更：** 实现 SFMValidator，或保留 UNAVAILABLE 并记录不能独立接入的原因。
- **实施步骤：** 只读检查上游对应 release / commit；依许可证决定 source / binary 使用；验证 grammar generation、build、启动依赖、无 Minecraft runtime 时 parse、diagnostic JSON、版本矩阵；受限环境运行正式样例。
- **测试方法：** 真实 SFM 正例 / 反例与格式化诊断集成测试；保留 Fake Validator 单测。
- **验收标准：** 只有真实 parser 对指定版本真实样例通过后才标记该版本 VALID；否则服务保持 UNAVAILABLE / VERSION_UNSUPPORTED。
- **回滚方式：** 关闭真实 adapter，退回 Fake / UNAVAILABLE；不改变 Agent Loop 和文档检索。
- **依赖真实数据：** 需要目标版本官方 grammar / parser 与真实样例，不依赖 GTNH recipe 数据。
- **是否需要外部网络：** 是，获取上游 release / source 时。
- **是否需要人工确认：** 是，版本选择、许可证、二进制分发与执行隔离须审阅。

## P7：集成与回归

### P7-01 原有 Bot 功能兼容回归

- **目标：** 验证 Adapter、命令、记忆、模型选择、普通对话、消息分段、Agent 旧工具兼容。
- **前置依赖：** P0-01 至 P0-07、P1-01 至 P1-03。P1-04 是可选延期任务，不阻塞最终兼容验收；若未实施，回归验证默认群共享行为保持兼容。
- **允许修改文件：** tests/；发现缺陷时只改被测 app 模块的最小修复文件。
- **新增文件：** 可选 tests/test_regression_contracts.py。
- **接口变更：** 无。
- **实施步骤：** 作为 P7 最终完整兼容 sweep，固定旧 schema / command / result snapshots；Fake NapCat / Fake LLM；核对默认配置、空数据、Tool Registry 兼容和所有阶段已启用能力。阶段中仍须逐任务 / 逐阶段回归，不等到此处才测试。
- **测试方法：** 现有 unittest 全集 + mock 回归；不访问真实模型和 NapCat。
- **验收标准：** 旧用户命令和消息行为通过；行为变化在 release note / 迁移文档列明。
- **回滚方式：** 对应单任务 commit revert；数据库 additive migration 可保留。
- **依赖真实数据：** 否。
- **是否需要外部网络：** 否。
- **是否需要人工确认：** 否。

### P7-02 端到端 Fixture / 并发 / 安全集成

- **目标：** 覆盖多工具、群聊并发、超时取消、空知识库、版本隔离、引用、配方、SFM fake 和安全策略。
- **前置依赖：** P0-01 至 P0-07、P1-01 至 P1-03、P2-01 至 P2-04、P3-01 至 P3-03、P4-01 至 P4-04、P5-01 至 P5-04；可选 P1-04 不阻塞。
- **允许修改文件：** tests/ 与测试 Fixture；必要时对对应服务做最小修复。
- **新增文件：** tests/test_agent_integration.py、临时资源 Fixture。
- **接口变更：** 无。
- **实施步骤：** Fake OneBot / LLM / HTTP / Resolver / Chroma / Validator；并发 scenario 使用临时 DB；输出阶段能力清单。
- **测试方法：** unittest 集成测试；无 provider key、无搜索 API、无外部网络。
- **验收标准：** 资源上限、取消、权限、空数据状态和 Fixture / Fake 边界符合设计；清理所有测试资源。
- **回滚方式：** 回滚新增集成测试或针对失败用例的单任务修复。
- **依赖真实数据：** 否；全用 Fixture。
- **是否需要外部网络：** 否。
- **是否需要人工确认：** 否。

### P7-03 真实数据能力声明验收

- **目标：** 将每个功能分别标记为 Mock、人工 Fixture、本地组件集成、真实数据集验证或外部依赖未验证，并验证用户可见状态准确。
- **前置依赖：** P7-02；声明 GTNH / SFM 真实能力还必须完成 P6-01、P6-03、P6-04。
- **允许修改文件：** docs/ 能力说明、用户提示模板、真实验证报告和对应测试。
- **新增文件：** docs/DATA_VALIDATION_REPORT.md（未来实施任务再创建）和样本清单。
- **接口变更：** 无。
- **实施步骤：** 审查来源、版本、hash、验证器、覆盖率与失效降级；把测试等级传到能力状态。
- **测试方法：** 真实 corpus / recipe set / SFM parser integration；Mock / Fixture 报告独立保留。
- **验收标准：** 真实能力只在 P6 对应真实样本 / 数据集 / Parser 验收通过时发布；Mock 和人工 Fixture 的通过单独记载，绝不升级为真实领域能力；否则显示未导入 / 未验证，不猜数据。
- **回滚方式：** 能力状态切回 Fixture / unavailable；真实 dataset 保留但设为 inactive。
- **依赖真实数据：** 是，只有声明真实领域能力时。
- **是否需要外部网络：** 视数据获取流程而定。
- **是否需要人工确认：** 是，发布真实能力和来源引用前审核。

## 可立即开始的任务与数据门

在外部数据仍缺失时可立即开始：P0 全部、P1-01 至 P1-03、P2 的 P2-01 至 P2-04、P3 全部（临时 Git Fixture）、P4 的 P4-01 至 P4-04（人工 Recipe Fixture）、P5 的 P5-01 至 P5-04（Fake Validator）。P1-04 可延后；P4-01 初稿后的真实样本可行性检查是可选 checkpoint，没有样本时明确留待验证。

必须等待或准备真实外部材料的任务只有 P6：GTNH 文档导入需要真实文档与许可；真实 Recipe adapter / 导入需要导出源和真实样本；SFM 真实 Validator 需要目标版本源码 / 依赖 / 许可和真实样例。它们不阻塞通用服务与模拟测试，也不能被 Fixture 测试替代。

**建议首先执行：P0-01。** 这是已确认的高风险回调鉴权问题，测试边界清楚；上线切换前先确认 NapCat 的 callback header 配置。随后按优先顺序执行 P0-02、P0-03、P0-04 和 P0-06，建立可安全承载后续工具的网络与资源边界。
