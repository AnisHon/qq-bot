# QQ Bot 演进为 Minecraft AI Agent：实施规划

规划基线：2026-10-12
状态：设计稿，未执行代码改造。本文以当前仓库源码和《ARCHITECTURE_REVIEW.md》为基线；仓库事实以源码为准。

## 1. 目标与约束

目标是在保留现有 FastAPI、NapCat / OneBot 11、OpenAI 兼容模型客户端和自研 Agent Loop 的前提下，逐步增加安全的工具注册机制、版本化知识检索、结构化配方服务、本地 Git 检索和 SFM 编程工作流。

本计划遵守以下边界：

- 本回合只创建规划文档，没有修改业务代码、依赖、配置或数据库，没有运行测试、Bot 或模型 API。
- 当前仓库有 SFM PDF 检索和 SQLite FTS5 + Chroma 的基础设施；没有 GTNH Wiki、任务书、配方数据库、配方导出文件或已验证的配方 Schema。
- 不假设 gtnh-toolkit 可用。GTNH 真实数据接入单独放在 P6。
- Fake Validator、人工 Markdown / Recipe Fixture 只能证明接口和通用逻辑，不证明真实 GTNH 数据或 SFM 语法有效。
- 每个任务必须独立回归、能够逐任务回滚；数据库变更使用可重复的增量迁移和备份，不用自动降级破坏用户数据。
- 新的领域服务在数据集缺失或索引为空时仍能初始化；明确返回 DataUnavailable、NotIndexed、NoResults、VersionMismatch 或 ToolExecutionError。
- 不允许自动跨整合包版本借用数据，也不允许模型用猜测补齐缺失配方。

## 2. 当前架构事实

仓库要求 Python 3.12+，使用 uv 管理依赖；直接依赖见 pyproject.toml，包括 FastAPI、Uvicorn、HTTPX、ChromaDB、sentence-transformers、BeautifulSoup、feedparser、pypdf 和 tenacity。应用入口为 app/main.py，OneBot 回调交给 NatcapQQAdapter 解析，BotService 分派命令和普通消息，LLMHandler 组装群上下文，DeepSeekClient 调 OpenAI 兼容的 chat/completions，app/tools/agent.py 执行原生 Function Calling。

已有模块和可复用能力：

| 模块 | 当前职责 / 可复用点 | 证据 |
|---|---|---|
| OneBot 接入和发送 | NapCat API 适配、事件归一化、引用补全、消息分段和发送串行 | app/main.py 的 onebot_event、_process_group_message；app/natcapqq_adapter.py；app/qq_dispatcher.py |
| Bot 编排 | 唤醒规则、命令路由、写入历史、同群忙碌保护 | app/bot_service.py 的 handle_group_message、_parse_command、_try_begin_group_reply |
| 模型层 | OpenAI 兼容请求、Function Calling、SSE | app/llm.py 的 DeepSeekClient.chat、chat_with_tools、chat_stream |
| Agent Loop | 多轮规划、重复调用去重、每轮和总工具次数上限、并发执行、结果裁剪、引用整理 | app/tools/agent.py 的 run_agent_loop、_execute_native_tool_call |
| 对话状态 | 群历史、摘要、长期记忆、群设置、定时任务 | app/memory.py 的 MemoryStore；app/handlers/command_handler.py |
| 文档检索 | SQLite FTS5 和 Chroma 语义检索 | app/rag.py 的 RagStore；app/vector_rag.py 的 VectorRagStore；scripts/index_sfm_pdf.py |
| 外部工具 | Web / 新闻 / 天气 / GitHub 搜索与读取 | app/tools/external.py |
| 测试 | 标准库 unittest，已有 adapter、agent、BotService、dispatcher、memory、上下文和 RAG 测试 | tests/test_*.py |

现有工具实现不是插件系统：app/tools/agent.py 静态维护 TOOL_DEFINITIONS、TOOL_NAMES 和按名称分支的 _execute_native_tool_call；forced tool 命令在 app/bot_service.py 与 app/handlers/llm_handler.py 另有映射。计划保留当前调用协议，增加统一注册入口并用兼容适配器迁移。

关键已确认风险：

1. app/natcapqq_adapter.py 的 NatcapQQAdapter.verify_callback_token 在 callback_token 为空时放行；app/config.py 默认值为空，app/main.py 的事件路由只检查该结果。
2. app/tools/external.py 的 looks_like_url 只判断 URL 格式；fetch_web_page 使用 follow_redirects=True。当前没有每跳 DNS/IP 限制、内网地址拒绝和响应体总大小限制的统一出口。
3. app/main.py 收到事件后 create_task 并立即返回 accepted；没有全局有界准入队列。app/bot_service.py 的活动保护按群 ID 且仅在业务协程开始后生效。
4. app/tools/agent.py 的总 deadline 覆盖模型请求，但工具 gather 没有整体的剩余时间约束；单工具超时不能替代整次任务 deadline。
5. MemoryStore、RagStore、VectorRagStore 和 embedding 执行同步 SQLite / Chroma / 模型编码；其中一部分从 async 请求路径直接调用。
6. scripts/index_sfm_pdf.py 的向量写入仅移除本次输入 chunk ID；PDF 更新时消失的旧 chunk 可能留在 Chroma。
7. 模型客户端解析模型文本和 tool_calls，但没有在统一账本记录 usage / token 或按群、用户限额。
8. 事件、摘要和调度任务有 asyncio.create_task 路径；定时主循环会在 shutdown 取消，但请求处理和摘要任务没有统一跟踪与 drain。
9. 现有 GitHub 工具面向公开远程仓库，并可浅克隆到缓存；还没有受目录白名单约束的本地工作区检索。
10. 仓库内没有配方领域模型或查询服务；SFM PDF 检索也不等于 SFM Parser / Compiler / Validator。

以上对应的源码证据和其他现状见《ARCHITECTURE_REVIEW.md》“关键消息处理调用链”“风险”和“架构评估”各节。本计划把确定事实、尚未验证项和建议分开，不把目标功能描述为当前能力。

## 3. 目标架构

采用模块化单体。QQ 协议边界继续留在 Adapter / Dispatcher；BotService 负责用户行为和会话路由；自研 Agent Loop 负责模型驱动的多工具流程；ToolRegistry 负责 Schema、参数校验、权限和资源约束；知识和外部访问由有明确边界的领域服务承担。

~~~mermaid
flowchart LR
  N[NapCat / OneBot 11] --> A[FastAPI + OneBot Adapter]
  A --> Q[有界准入队列 / 生命周期受管 Worker]
  Q --> B[BotService：命令与 Session 路由]
  B --> S[上下文 / 会话隔离 / 配额]
  S --> R[保留的 Agent Loop]
  R <--> L[OpenAI 兼容模型客户端]
  R --> T[ToolRegistry：schema / policy / budget / timeout]
  T --> D[DocumentKnowledgeService]
  T --> C[RecipeQueryService]
  T --> G[RepositorySearchService]
  T --> W[WebSearchService]
  T --> F[SFMKnowledge / SFMValidation]
  D --> E[ToolResult + Evidence]
  C --> E
  G --> E
  W --> E
  F --> E
  E --> R
  R --> B
  B --> O[QQMessageDispatcher]
  O --> N
  DB[(SQLite：对话 / 配额 / 配方 / FTS5)] --- S
  DB --- D
  DB --- C
  V[(现有 Chroma：语义索引)] --- D
  SRC[未来文档源 / 人工或真实数据] --> I[独立、可重复运行的 Importer]
  I --> DB
  I --> V
~~~

图中接口是职责边界，不意味着需要为每个方框单独建类。先把当前 app/tools/agent.py 和 app/handlers/llm_handler.py 中重复的工具路由逐步迁入注册机制；只有存在多实现、生命周期或测试替身需求时才新增服务对象。

### 3.1 持久化与版本隔离

- 保留现有 SQLite 文件继续承载对话、群设置和 FTS5；新增表通过 schema 版本迁移管理。Recipe 是精确结构化查询，使用 SQLite 表和索引，不用向量相似度回答配方事实。
- 文档表记录 corpus / source / pack_id / pack_version / source_revision / content_hash / parser_version / indexed_at；chunk 记录稳定 chunk_id、标题、正文、页码或行范围、章节路径和文档版本。
- Chroma 继续作为可选语义索引。FTS5 是关键词检索来源，向量为空或不可用时服务仍可运行。混合排序用 Reciprocal Rank Fusion (RRF)，应用层用同一 revision 去重和过滤。
- SQLite 与 Chroma 不能作为一个原子事务提交。Indexer 先写新 generation，再校验两侧，最后切换 active revision；旧 generation 在新版本可查询后清理。启动和健康检查报告索引状态但不因领域数据缺失而失败。
- 文档删除 / 更新必须按 document_id 清理 SQLite、FTS 和 Chroma 的全部旧 generation；清理失败记为可重试的索引任务，查询端只读取 active generation。
- 现有 SFM PDF 数据通过兼容导入器保留原 document_name 与引用方式；迁移时先 shadow read / 对比，再切换，回滚可切回旧查询路径。
- 现有对话按 group_id 保存，群共享是当前用户可见行为，必须保持默认不变。用户级预算计量不依赖 SessionKey；P1-04 只保留为可选、延后启用的隔离扩展，不阻塞 P2–P5，也不要求提前重构 MemoryStore。未来隔离模式不得把群历史复制到个人会话。
- 配方数据按精确 PackVersion key 查询。未知版本一律 VersionMismatch / DataUnavailable，不回退到其他版本。

### 3.2 异步任务与资源管理

P0 先增加有界内存队列和固定 worker 数、全局模型并发 semaphore、每群 / 用户并发策略、优雅 drain 和排队满载响应。第一步不引入 Redis、Celery 或新服务；未处理事件在进程崩溃时仍可能丢失，应将这个语义显式记录。若后来需要多副本或跨重启保证，再单独评估持久队列。

每次 Agent 请求持有统一 deadline / cancel token。模型请求、工具 semaphore 排队、工具运行和最终回答都消耗同一剩余时长；超时取消子任务并通过 finally 释放群锁 / 资源。同步 SQLite 和 Chroma / embedding 通过有界线程池或专属执行器运行，线程数受限，避免为每次请求无上限创建线程。

### 3.3 安全、预算和运行观测

- OneBot 回调鉴权默认 fail closed，增加启动期配置校验、401 行为测试和部署迁移说明。生产环境切换前需确认 NapCat header 配置，防止配置遗漏导致回调全拒绝。
- WebSearchService 不能只在请求前解析并检查 DNS：普通 HTTPX 请求会由其 transport 另行解析，前置检查与实际 TCP 连接之间存在 TOCTOU / DNS rebinding 窗口。安全出口必须将经策略批准的 IP 与实际 socket dial 绑定，并在保留原始 hostname 的情况下完成 HTTPS SNI、Host header 和证书主机名验证；不允许调用方覆盖 Host。每次 redirect 由应用手动接收、重新规范化 / 解析 / 验证 / 固定连接；关闭 HTTPX 自动跟随，并确保连接池不会跨 host / port / approved-IP 策略错误复用。覆盖 IPv4、IPv6、IPv4-mapped IPv6、IDNA、编码 authority 和特殊 IP 表达式；限制重定向、连接 / 读总时长、原始与解压后响应字节。安全抓取 client 使用 trust_env=False；HTTP_PROXY / HTTPS_PROXY / NETWORK_PROXY_URL 不得隐式绕过策略。当前 _fetch_via_jina 会把目标 URL 交给远端 Reader，因此不允许将用户 URL 直接送入该 fallback；P0-02 安全路径应在本地失败关闭，除非未来代理本身被证明实施同等的目标绑定与逐跳验证。
- HTTPX 边界：MockTransport 只验证请求策略和状态处理，不证明真实 socket 目标。优先把安全逻辑放在专用 SafeFetchTransport / transport adapter 边界，底层须由受控连接器按本次解析结果连接固定 IP，同时保留原 host 的 TLS 校验。HTTPX 的公开自定义 transport 接口并未直接提供“每请求指定 DNS 结果并固定 dial”的高层参数；若实现不得不依赖 httpcore 的内部网络后端接口，必须针对锁定版本写受控连接器集成测试并审查升级风险。不能证明目标 IP 固定时，安全抓取保持不可用并失败关闭；可选替代是专用、可信的出站抓取代理 / 隔离服务，但必须在代理侧实施同一策略，Bot 不能把未验证 URL 直接交给它。
- 该边界应以 HTTPX 的 [自定义 transport 文档](https://www.python-httpx.org/advanced/transports/)、[TLS 校验文档](https://www.python-httpx.org/advanced/ssl/) 和锁定版本的 [httpcore AsyncNetworkBackend 接口](https://github.com/encode/httpcore/blob/master/httpcore/_backends/base.py) 为依据；公开扩展接口不等于已有可直接配置的 per-request DNS pinning。
- 工具执行先应用 ToolPolicy：工具 allowlist、群 / 用户权限、来源目录 / 网络目的地、只读或受限动作类别、全局 / 分类 semaphore、单次与累计结果上限。
- DeepSeekClient 只把 provider usage 作为调用完成后的观测值。每次模型 API 调用前由 SQLite 预算控制器事务性预留请求次数、估算 / 上限 token 和可选费用额度；完成后 settle，确认尚未发送时 release。多请求并发使用 BEGIN IMMEDIATE 等写事务保证预留准入不超发；每个工具循环模型请求、重试和流式 API 请求都独立记账。没有 usage 时记 usage_unknown，不能伪造精确 token；采用保守的已预留额度占用策略。费用只有在可信费率和可靠 usage 同时存在时才计算，否则只报告未知 / 估算；请求次数上限是硬约束，token reservation 是预算准入估算，只有 provider-enforced output limit 等约束能限制单次生成上界。若结算发现真实 usage 超过预留，记实际值并拒绝后续调用；不能倒称这次请求在发送前已被精确限制。预算拒绝必须发生在 HTTP 请求之前。保留现有模型客户端公开调用签名，由调用侧 facade / invoker 注入预算控制。
- 增加任务 ID、队列长度、活跃工作数、工具名、耗时、状态、模型 usage 以及错误分类指标；日志保持 request / group context，但不得记录 API Key、Token、密码或未脱敏的敏感正文。

## 4. 技术选型

| 选项 | 决策 | 依据 |
|---|---|---|
| 保留当前架构或引入 LangGraph | 保留当前自研 Agent Loop。现有代码已处理工具循环、并行调用、次数预算和引用。LangGraph 的 checkpoint / interrupt 对跨请求的可恢复长流程有价值，但当前目标先是受限工具、短任务与同步群聊；尚无需要图状态持久化或人工中断的具体流程。若后续引入多步长任务和人工审批，再用单一工作流做小型验证，不先替换运行时。参考 [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence) 和 [interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)。 |
| 自己实现 Tool Calling 或用框架 | 继续使用 OpenAI 兼容原生 Function Calling；实现 ToolRegistry / typed result / policy 层，不自行重做模型协议或 tokenization。当前瓶颈是本地 schema/dispatch/policy 集中，不是缺少 Agent 框架。 |
| LlamaIndex 或轻量 RAG | P2 先扩展现有 RagStore / VectorRagStore，做版本元数据、RRF、引用和删除一致性。LlamaIndex ingestion pipeline 适合大量 parser / transformation / vector store 集成；当前来源类型有限且已有 FTS5 + Chroma，先引入会增加依赖和双套抽象。只有需要大量外部 connector 或复杂 ingestion DAG 时再做 spike。参考 [LlamaIndex ingestion pipeline](https://developers.llamaindex.ai/python/framework/module_guides/loading/ingestion_pipeline/)。 |
| SQLite、PostgreSQL、Qdrant | 先 SQLite + FTS5 + Chroma。当前单体、小规模群 Bot 且已有 SQLite；Recipe、会话、配额、文档元数据适合关系表与事务。SQLite FTS5 提供本地全文索引。PostgreSQL 仅在多实例写入竞争、远程共享数据或治理需求已经测量出来后评估；Qdrant 仅在向量数据规模、过滤性能或并发压测证明 Chroma 不够时评估。参考 [SQLite FTS5](https://www.sqlite.org/fts5.html)、[SQLite WAL](https://www.sqlite.org/wal.html)、[PostgreSQL MVCC](https://www.postgresql.org/docs/current/mvcc-intro.html)、[Chroma Python API](https://docs.trychroma.com/reference/python) 与 [Qdrant filtering](https://qdrant.tech/documentation/search/filtering/)。 |
| MCP | 当前不引入。Bot 内部由 ToolRegistry 统一执行即可。MCP 适合独立进程或第三方工具提供方要跨宿主复用、明确服务边界时；会新增协议、进程生命周期和权限映射。在确有多宿主复用需求后，可将现有 ToolSpec 适配到 MCP server，而不让 MCP 成为唯一内部接口。参考 [MCP tools specification](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)。 |
| SFM Parser / 配方导出器 | 通过版本化、只读子进程或本地 importer 接口集成。限制 stdin / 文件大小 / wall-clock / CPU / 内存；禁止 Bot 执行生成的 SFM 程序或连接游戏。外部源码或导出工具须在目标版本和许可证审核后锁定版本，不假设当前已安装。 |
| Docker / 微服务 / 新缓存 | 本计划不新增。仓库当前可见部署为 systemd + scripts/run.sh；没有 Docker / Compose。只有运维目标变化或实测负载证明进程内方案不满足后另立提案。 |

## 5. 阶段与验收门

| 阶段 | 结果 | 前置 | 离开阶段的验收门 |
|---|---|---|---|
| P0 安全与运行稳定性 | 回调 fail closed、SSRF 防护、背压、请求取消、同步 I/O 隔离、预留式预算、现有 SFM PDF stale chunk 最小修复 | 无 | SSRF 除 Mock / Fake Resolver 外，还需受控 connector 实测 peer IP 与 SNI / Host / 证书；并发 reservation、失败与取消策略通过单测；满载、超时、shutdown 无任务泄漏；旧消息行为兼容 |
| P1 Agent Tool Registry | 单一 Schema / Handler / Policy / 结果注册与兼容迁移；P1-04 用户隔离是可选延期扩展 | P0-01 至 P0-06 | 现有工具名、参数、进度、来源与命令行为一致；未知工具、参数无效和权限拒绝有结构化结果；默认 legacy 群共享行为不变。P1-04 不作为 P2–P5 或 P7-01 的前置门 |
| P2 通用 RAG | 多来源文档、版本过滤、FTS + 向量 + RRF、可定位引用、增量更新 / 删除 | P1-03、P0；P1-04 不作依赖 | 只用人工 Markdown Fixture 完成用例；空库与向量不可用仍启动；不宣称 GTNH RAG 可用 |
| P3 本地 Git 检索 | 白名单仓库路径、固定 commit 检索、文件范围读取与来源引用 | P0-02、P0-03、P1-03 | 临时 Git Fixture 覆盖越界、符号链接、敏感文件、大小限制与引用；无外网依赖 |
| P4 配方基础设施 | Contract-first Recipe schema / importer / SQLite repository / query service / Agent 工具 | P1-03；可与 P2 并行；P1-04 不作依赖 | 八类用户要求 Fixture 和复杂 Ingredient / quarantine Fixture 通过；精确与条件匹配分开；未导入 pack/version 返回明确状态；明示未以 GTNH 真实样本校准 |
| P5 SFM 编程工作流 | SFM 文档来源元数据、Validator 接口、生成—诊断—有限修复 | P1-03、P2；P1-04 不作依赖 | Fake Validator 证明工作流协议和上限；界面明确标注 fake / 未验证；不宣称真实语法通过 |
| P6 真实数据接入 | GTNH 文档、真实配方导出、真实 SFM Parser 与真实语料验证 | P2、P4、P5 及数据许可 / 版本核对 | 真实来源、版本、字段覆盖、导入质量、引用、查询和语法样例形成可审计验收记录 |
| P7 集成与回归 | 原有 Bot 兼容、并发、资源、缺数据和阶段能力报告 | P0-P5；真实集成报告依 P6；P1-04 若未实施不阻塞 | 逐任务运行对应测试、逐阶段运行相关回归；P7-01 是最终完整兼容验收。报告分别列出 Mock、人工 Fixture、本地组件集成、真实数据集验证和未验证外部依赖 |

P6 不阻塞 P0-P5 的接口、Fixture 和单元测试。P4-01 Schema 初稿完成后，可选安排一次真实样本可行性检查；只能检查维护者已提供且获准读取的样本，不自动下载、不假定 gtnh-toolkit。没有样本时标记“待验证”，不阻塞 P4 fixture。P6-02 负责承接该 checkpoint 并在真实样本后完成 exporter 评估。P6 没完成前，面向用户的配方或 SFM 验证工具必须返回数据未就绪或验证器不可用，不可用生成内容冒充检索事实。

### 5.1 各阶段涉及模块、风险与验证

| 阶段 | 主要模块 | 主要风险 | 验证与回滚 |
|---|---|---|---|
| P0 | app/main.py、app/natcapqq_adapter.py、app/tools/external.py、app/tools/agent.py、app/llm.py、app/memory.py、app/rag.py、app/vector_rag.py、app/config.py | fail closed 可能因部署遗漏 secret 而拒收回调；队列满载语义可能遇到 NapCat 重试差异；同步线程任务取消后仍可能运行到函数返回；底层固定 IP connector 与 HTTPX 版本兼容需验证；usage 缺失时无法精确知道 token / 费用 | 分别用 header 单测、MockTransport、Fake Resolver、受控 connector 验证 peer IP / SNI / Host / 证书、满载 / drain / cancel、预算并发与故障注入。生产 rollout 前检查配置；可按任务 facade 回滚，保留数据 |
| P1 | app/tools/agent.py、app/llm.py、app/handlers/llm_handler.py、app/bot_service.py、新 app/tools/registry.py；可选 app/session.py | 不同兼容模型对 JSON Schema 子集和 tool-call 回复格式支持不同；只有启用用户隔离时才有历史串扰迁移风险；切换可能改变旧工具权限 | 锁定现有 schema 与命令快照，Fake LLM 合约测试；P1-04 单独验证私有隔离和 legacy 行为，按 scope flag 回滚；缺省继续 legacy group scope |
| P2 | app/rag.py、app/vector_rag.py、scripts/index_sfm_pdf.py、新 app/knowledge/ | SQLite 与 Chroma 无共同事务；embedding 冷启动 / 删除失败可能造成部分索引；人工 Fixture 无法证明真实语料质量 | Fake Chroma 故障注入、临时 DB 的 generation / 引用 / 更新删除测试；active pointer 回滚旧版，Fixture 报告不宣称 GTNH 可用 |
| P3 | 新 app/repositories/local_git.py、app/config.py、app/tools/agent.py | 路径穿越、symlink / TOCTOU、敏感文件误读、工作区在两次工具调用间变化 | 临时仓库测试越界 / symlink / denylist / pinned commit；先关闭 Registry 工具即可回滚，不改现有 GitHub 工具 |
| P4 | 新 app/recipes/、SQLite schema migration、app/tools/agent.py | 未见真实 exporter 字段；Fixture 过拟合；单位、tag、Ore Dictionary、predicate、NBT / component、模组条件和随机输出语义无法仅靠猜测定稿 | 八类最低用例 + 复杂 Ingredient synthetic Fixture；unknown critical quarantine、unknown field 原样保留、exact / conditional 区分；dataset active pointer 回滚。Schema 未用真实样本校准前仅为 contract |
| P5 | 新 app/sfm/、app/knowledge/、app/tools/agent.py、app/handlers/llm_handler.py | Fake Validator 被误标为真实；只解析 grammar 无法证明 AST 或 registry 语义；生成修复可能耗尽预算 | Fake LLM / Validator 测试状态标签、两轮上限和 cancel；关闭 workflow tool 回滚；没有真实 parser 时状态只能 UNAVAILABLE |
| P6 | app/knowledge/、app/recipes/、scripts/、外部数据 manifest | 许可证、版本或字段不匹配；数据可能有缺项 / 重复；上游 Parser 的独立构建和诊断格式未知 | dry-run、hash / coverage 报告、抽样人工核对；先 staging，再切 active；回滚旧 dataset / generation。数据许可未清楚时不发布 |
| P7 | tests/、Mock / Fixture 适配层、能力状态文案 | 只测成功路径会漏掉并发竞态；Fixture 全通过也可能让真实能力状态被夸大 | 将回归结果分为 Mock / Fixture、本地组件、真实数据集、外部依赖未验证；失败时按单任务 revert，真实数据仅设 inactive |

各阶段涉及的文件列表、接口变更、测试命令范围和逐任务回滚方式以《EXECUTION_TASKS.md》为准。本回合仅制定测试方案，没有运行这些测试。

## 6. 渐进重构与迁移策略

1. **先增加边界，再迁移调用点。** P0 尽量保持现有公开函数签名。P1 保留 TOOL_DEFINITIONS 生成结果中的旧工具名和 JSON 参数；旧 handler 先包装为 ToolSpec，新服务逐个注册。命令 /weather、/news、/web 仍调用同一工具注册表，输出文案和 QQ 回复方式保持兼容。
2. **并行对照再切流。** 文档检索先让旧 SFM PDF 查询路径和新 KnowledgeService 对相同 Fixture 查询，比较 chunk / 引用；通过后按配置开关切换。配方和本地 Git 是新增工具，不影响旧工具默认行为。
3. **SQLite 使用版本化增量迁移。** 增加 migration ledger；迁移前检查并记录 DB 文件备份路径和 schema version。添加表 / 列 / 索引优先只增不删；新读路径通过 feature flag 打开；回滚用旧读路径，不自动反向删表。数据量变大后再设计显式压缩 / 清理操作。
4. **FTS 与向量一致性分两步。** P0-07 仅修复现有 SFM PDF 的单文档 Chroma 替换 / 删除：复用 document-name collection，先完成 embedding，再清除该文档 collection 旧 IDs 并写入新 IDs；正常成功完成后不能命中旧尾部 chunk。该最小实现不提供 SQLite 与 Chroma 的跨库原子回滚，写入中断可能出现向量暂缺，查询由 FTS 降级；不得声称旧 generation 原子保留。P2-03 才为通用来源引入 generation、active revision、staging、tombstone、恢复和 pending cleanup。
5. **会话兼容。** 在默认 legacy 模式下仍按 group_id 提供既有群上下文。用户隔离模式使用新 session key 且默认不复制过去群记录；切换前可从数据库导出审阅，不自动对每个用户扩散群级摘要。支持按群灰度和回滚。
6. **外部工具版本可审计。** SFM Parser 和配方导出器采用锁定版本 / commit、记录许可证和输入数据 hash；不下载大仓库或真实领域数据作为 P0-P5 的隐式前置。

## 7. 测试和验收报告等级

每个后续实施任务完成后先运行该任务对应单元 / 集成测试；每个阶段结束时运行该阶段相关回归集。P7-01 额外执行最终完整兼容验收，不是唯一回归入口。测试代码不得调用真实 provider、真实搜索服务或 Minecraft 服务器；使用 Mock HTTP、Fake Resolver / 受控 connector、Fake LLM / Validator、临时 SQLite 与临时 Git 仓库。

报告严格区分：

1. **Mock 测试通过**：证明隔离边界、调用序列和错误映射；不能证明真实网络连接目标。
2. **人工 Fixture 测试通过**：证明合成文档 / Recipe / SFM 样例符合契约，不证明任何 GTNH 真实能力。
3. **本地组件集成测试通过**：证明服务与本地 SQLite / Chroma / 临时 Git / 受控 connector 协作；不表示领域数据完整。
4. **真实数据集验证通过**：只适用于具备来源、版本、完整性说明和许可记录的真实语料 / 配方数据，并报告抽样与覆盖率。
5. **外部依赖未验证**：外部导出器 / Parser 未锁定、未运行或不支持指定版本；状态不能由 Fake 测试提升。

## 8. 仍需确认

- 生产部署中的 OneBot 回调 Authorization header 是否已配置，以及修改默认 fail closed 的上线窗口。
- 运行部署是否单进程、是否有多个 Uvicorn worker，以及 systemd 实际工作目录 / 环境变量来源。
- 当前 provider 在非流式、流式和工具调用响应中分别是否返回可信 usage；费用费率表如何维护；无 usage 时采用何种预留上限与未知费用降级策略。
- SSRF 安全抓取的固定 IP transport 是以锁定版本的 httpcore backend 实现，还是由经审计的可信 egress proxy 提供；在证实之前不能启用任意 URL 抓取。
- 群聊未来采用“每用户私有会话 + 独立共享群上下文”，还是继续共享同一群线程；旧摘要能否作为群公共背景保留。
- 目标 GTNH 整合包版本、游戏版本、模组版本、配方事实来源与许可证尚未提供。
- GTNH Wiki / 任务书数据是否允许本地索引及对群成员展示原文摘录 / 引用。
- Recipe 导出来源与覆盖字段未知；流体单位、条件 DSL、随机输出及源条目身份键需由真实样本校准。
- SFM 实际目标版本未知。公开的 1.19.2 上游源码可看到 ANTLR grammar、ProgramBuilder、ASTBuilder 和错误元数据，但单独运行 Parser、构建是否必须启动 Minecraft、结果类型可否稳定序列化为 JSON、目标版本支持矩阵均待验证。源码结构不能证明某个 GTNH 版本已兼容。
- 单体 Bot 的队列是否需要跨进程 / 重启可靠投递；P0 先做有界进程内队列，后续由消息丢失容忍度和负载数据决定是否升级。

## 9. 参考

- 当前仓库证据：《ARCHITECTURE_REVIEW.md》及本计划第 2 节列出的实现文件。
- SFM 上游：[仓库与 MPL-2.0 许可说明](https://github.com/TeamDman/SuperFactoryManager)、[1.19.2 ProgramBuilder](https://github.com/TeamDman/SuperFactoryManager/blob/1.19.2/platform/minecraft/src/main/java/ca/teamdman/sfml/program_builder/ProgramBuilder.java)、[1.19.2 ASTBuilder](https://github.com/TeamDman/SuperFactoryManager/blob/1.19.2/platform/minecraft/src/main/java/ca/teamdman/sfml/ast/ASTBuilder.java)、[1.19.2 SFM grammar](https://github.com/TeamDman/SuperFactoryManager/blob/1.19.2/platform/minecraft/src/main/antlr/sfml/SFML.g4)。
- 技术文档引用详见第 4 节。
