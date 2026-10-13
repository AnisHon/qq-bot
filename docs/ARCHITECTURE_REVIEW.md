# Bot 项目架构分析与 Minecraft AI Agent 演进建议

分析日期：2026-10-12

## 范围与结论摘要

本报告以当前仓库源码、依赖清单、测试和部署文件为依据。现有说明文档用于交叉核对；当文档与实现不一致时，以实现为准。没有读取本机环境文件内容，没有运行服务或测试。本次只新增本报告。

当前项目是一个 Python 3.12、FastAPI、NapCat / OneBot 11 单体 Bot。它已经实现 OpenAI 兼容的模型客户端、原生 Function Calling、有界 Agent 循环、工具执行、QQ 群消息适配、群级 SQLite 对话记忆、SQLite FTS5 和 Chroma 语义检索。它不是任意代码执行 Agent，也没有通用插件发现机制。

从现有架构演进到 Minecraft Agent 不需要重写消息接入或 Agent 主循环。建议保留 FastAPI、OneBot 适配器、消息分发器、SQLite 状态库和工具循环，先补并发背压、会话键、工具注册边界和安全约束；再逐项增加结构化配方服务、本地资料与源码索引、SFM 验证工具。配方查询应由版本化结构化数据回答，向量检索只承担文档和源码语义搜索。

## 1. 当前架构概览

### 1.1 语言、框架、运行环境和依赖

| 项目 | 当前实现 |
|---|---|
| 语言 / 运行时 | Python，要求版本为 3.12 或更高；依赖由 uv 管理，清单为 pyproject.toml，解析锁定信息在 uv.lock。 |
| HTTP 服务 | FastAPI + Uvicorn。入口在 app/main.py，暴露 /health 和 OneBot 回调路由。 |
| QQ 接入 | NapCat 的 OneBot 11 HTTP client 将事件推送至 Bot；Bot 使用 HTTP API 调用 NapCat 发送群消息、读取回复消息和群成员名片。 |
| 模型 | app/llm.py 中的 DeepSeekClient 通过 HTTPX 调用 OpenAI 兼容的 /chat/completions。main.py 根据环境配置注册 DeepSeek、NVIDIA、Qwen、New API、CCTQ 等客户端和模型别名。类名虽为 DeepSeekClient，实际使用的协议和 base URL 可配置。 |
| 状态存储 | Python sqlite3。app/memory.py 存聊天历史、摘要、长期记忆、群配置和定时任务；app/rag.py 在同一 SQLite 文件内建 FTS5 文档索引。 |
| 向量检索 | app/vector_rag.py 使用 ChromaDB 持久化向量，并在首次使用时加载 sentence-transformers 的多语言 MiniLM 嵌入模型。向量目录与 SQLite 路径分开。 |
| 网络和内容解析 | HTTPX、BeautifulSoup、feedparser、tenacity；PDF 文本抽取使用 pypdf。 |
| 部署 | scripts/run.sh 加载项目环境文件并运行 Uvicorn；deploy/ 下有 systemd 用户级和系统级 unit。仓库文件清单中没有 Dockerfile 或 Compose 文件。 |

直接依赖为 beautifulsoup4、chromadb、fastapi、feedparser、httpx[socks]、pypdf、sentence-transformers、tenacity、uvicorn；pyproject.toml 给出最低版本范围，uv.lock 固定解析结果。chromadb 与 sentence-transformers 当前列为必需依赖；语义模型本身延迟加载。工具缓存为进程内 TTL 缓存，不是 Redis 或 SQLite 缓存。

### 1.2 目录结构与模块职责

| 路径 | 职责与关键实现 |
|---|---|
| app/main.py | 应用组装、模型客户端注册、FastAPI 路由、OneBot 事件去重、后台消息任务、定时任务循环、生命周期关闭。 |
| app/natcapqq_adapter.py | NapCat / OneBot 消息格式适配：解析数组段或 CQ 字符串、@、附件、reply；调用 /get_msg、/send_group_msg 等 API。 |
| app/models.py | 传输和业务间共用的数据类：GroupMessageEvent、QuotedMessage、MessageAttachment、SearchResult、ChatReply、BotCommand。 |
| app/qq_dispatcher.py、app/text_utils.py | 群内发送锁、重复消息和状态节流、长文本切段、仅首段引用原消息。EventDeduplicator 保存短期消息 ID。 |
| app/bot_service.py | 主业务编排：唤醒规则、命令解析、写入历史、群级繁忙标记、选择命令或 LLM 流程、处理定时任务入口。 |
| app/handlers/command_handler.py | 管理命令、长期记忆、模型切换、任务 CRUD、上下文和摘要操作。 |
| app/handlers/task_handler.py | 定时任务时间表达式解析与任务文本格式化。 |
| app/handlers/llm_handler.py | 组装 system / summary / memory / quote / mention / recent context；选择模型；调用 Agent；接入本地 SFM 和 Minecraft 在线搜索；生成自动摘要。 |
| app/llm.py | OpenAI 兼容模型客户端、聊天、Function Calling、SSE 流式接口和响应数据类。 |
| app/tools/agent.py | 工具定义、Agent 循环、并发执行和调用预算、去重、结果格式化、证据校验及来源拼接。 |
| app/tools/external.py | Bing 搜索、新闻 RSS、网页正文、天气、GitHub 仓库和文件检索；GitHub API 不可用时支持浅克隆到本地缓存。 |
| app/tools/minecraft_wiki.py、app/tools/mcbaike.py | Minecraft Wiki 与 MC 百科搜索结果获取。 |
| app/tools/cache.py | 工具结果的进程内 TTL/LRU 风格缓存。 |
| app/rag.py、app/vector_rag.py | SQLite FTS5 和 Chroma 语义索引的存取与检索。 |
| app/config.py | Settings 数据类从环境变量读取服务、模型、OneBot、数据库、上下文、RAG、Agent 和调度参数。 |
| app/logging_ctx.py | ContextVar 请求上下文，日志前缀可带 request / group 标识。 |
| scripts/index_sfm_pdf.py | 读取 PDF，按页切分，写 SQLite FTS5 并尝试更新 Chroma 向量索引。 |
| tests/ | unittest 测试，覆盖 Adapter、Dispatcher、BotService、Context、Memory、RAG 和 Agent。 |
| deploy/、scripts/ | systemd 部署单元和启动 / SFM PDF 索引脚本。 |
| README.md、ARCHITECTURE.md、HANDOFF.md | 用户行为、运行方式和架构交接说明；部分配置和部署描述需与实现及部署环境重新核对。 |

仓库没有发现独立的 domain / infrastructure 包、迁移目录、插件目录、Docker 文件或 CI workflow。模块化主要通过 app 下的 Python 模块和 handler 类实现。

### 1.3 消息处理架构

~~~mermaid
flowchart LR
  NC[NapCat OneBot 11] -->|HTTP POST event| API[FastAPI /onebot/event]
  API --> AD[Event 校验与 NatcapQQAdapter 解析]
  AD --> DEDUPE[EventDeduplicator]
  DEDUPE --> HYDRATE[引用消息与 Bot 群名片补全]
  HYDRATE --> BG[asyncio 后台消息任务]
  BG --> BS[BotService]
  BS --> CMD[命令处理]
  BS --> CTX[群历史 / 摘要 / 记忆上下文]
  CTX --> LLMH[LLMHandler]
  LLMH --> MODEL[OpenAI 兼容模型]
  MODEL -->|Function Calling| LOOP[app/tools/agent.py 有界循环]
  LOOP --> TOOLS[知识 / Web / 新闻 / 天气 / GitHub 工具]
  TOOLS --> LOOP
  LOOP --> SYNTH[证据整理与最终回答]
  CMD --> DISPATCH[QQMessageDispatcher]
  SYNTH --> DISPATCH
  DISPATCH -->|send_group_msg| NC
  SYNTH --> STORE[(SQLite MemoryStore)]
  INDEX[PDF 索引脚本] --> FTS[(SQLite FTS5)]
  INDEX --> VEC[(ChromaDB)]
  LOOP --> FTS
  LOOP --> VEC
~~~

### 1.4 关键消息处理调用链

1. NapCat 将 OneBot 事件发送到 POST /onebot/event。app/main.py 的 onebot_event 先验证回调 Token，再读取 JSON；app/natcapqq_adapter.py 的 parse_group_event 只接受群消息，并把数组消息段或 CQ 字符串归一化为 GroupMessageEvent。
2. EventDeduplicator.accept 按 message_id 做进程内短期去重；之后 hydrate_quote 调 /get_msg 补全文本引用，_resolve_bot_names 读取 Bot 群名片并加入默认唤醒名。
3. 路由用 asyncio.create_task 启动 _process_group_message，然后立即返回 accepted。路由 ACK 与业务处理不是事务性的：接受后进程崩溃时，内存任务没有持久队列可恢复。
4. _process_group_message 计算 expects_reply，启动 BotService.handle_group_message；超出 reply_notice_delay 时发送等待状态。实际发送由 QQMessageDispatcher.send 负责，最后通过 adapter 调 NapCat /send_group_msg。
5. BotService 先识别 slash 命令。管理命令交 CommandHandler；/weather、/news、/web 转成 forced tool。普通消息先写 message_history，再应用 @、前缀、昵称和回复 Bot 的触发规则。
6. 对需要回答的普通问题，BotService._try_begin_group_reply 对群 ID 加单进程内锁；LLMHandler._reply_with_llm 根据群设置选择模型，组装当前请求、引用、@对象、近期上下文、群摘要、长期记忆和附件描述。
7. 开启 Agent 后，run_agent_loop 将 TOOL_DEFINITIONS 交给模型。模型若发出工具调用，_execute_native_tool_call 依名称分派到知识检索、Web、RSS 新闻、天气或 GitHub 实现；同一轮的不同调用通过 asyncio.gather 并发执行。
8. 工具观察结果再传回模型，最终答案经文本清理后写回群历史，dispatcher 分段发送并只在首段引用触发消息。发送完成后另起任务尝试更新群摘要。

不带 @ / 前缀 / 昵称唤醒的消息仍会被写入短期历史，但不会触发模型回答。命令识别和特殊路由在 app/bot_service.py；具体管理命令实现在 app/handlers/command_handler.py。

### 1.5 模型、上下文与隔离方式

- 模型请求由 DeepSeekClient 发送到兼容 /chat/completions 的端点。chat_with_tools 使用 OpenAI 风格 tools 与 tool_choice=auto；若模型在文本中返回特定 DSML 形式，Agent 另有兼容解析。模型注册表和 provider/model 别名在 app/main.py 中手工组装。
- 群设置表 group_settings 保存每群所选模型。CommandHandler 和 LLMHandler 各有一份可用模型和当前 provider 选择逻辑；当前模型请求失败不会自动切换到备用模型，_fallback_llm 返回 None。
- 短期历史按 group_id 保存，而非按用户会话保存。LLMHandler._recent_context_rows 调 select_recent_context，优先当前发言用户和 Bot 回复，最多保留两条其他群成员消息作为背景。群摘要也按群保存；管理命令增加的长期记忆按群保存。LLM 请求额外读取该 QQ 用户的 user scope 记忆，但当前管理命令没有创建 user scope 记忆的入口。
- 引用消息被单独标为当前请求的主要指代；@对象、附件、近期群消息均作为结构化说明加入模型上下文。附件目前仅传类型、名称和 URL 字符串，不读取图片像素、音视频或文件正文。
- 这提供了群级历史隔离和近期发言选择，不等同于严格的多用户会话隔离：摘要与历史本来就是群共享。群内其他成员仍可能成为最多两条背景上下文。

### 1.6 工具调用、命令系统和扩展接口

已实现的 Agent 能力在 app/tools/agent.py：静态 TOOL_DEFINITIONS 描述 JSON Schema；TOOL_NAMES 和 _execute_native_tool_call 组成白名单和 if / elif 分发器；run_agent_loop 管理轮次、总调用数、每轮并发数、重复签名和结果长度；LLMHandler 通过 local_knowledge_search 回调注入本地资料查询。还有 forced_tool_call 路径处理显式 /weather、/news、/web 命令。

当前模型可调用的功能包含本地领域知识入口、普通 Web 搜索、新闻、最多六条并行搜索、公开 GitHub 仓库查找 / 文件检索 / 文件读取、URL 正文读取、单城市和批量天气。Minecraft Wiki 与 MC 百科由本地知识入口按关键词调用。GitHub 读取的是公开远程仓库，必要时浅克隆到 data/github_repos；它不提供当前工作区仓库的受限文件或符号索引。

这套实现属于已落地的 Tool Calling，不是可自动发现的插件系统。新增工具需要同步维护模型 schema、工具白名单 / 分发分支，可能还要更新进度状态、强制命令映射、来源处理和测试。现有内部工具协议可以复用，但扩展边界仍集中在 agent.py 和 llm_handler.py。

命令系统由 BotService._parse_command 解析，CommandHandler.handle_command 执行。/help、记忆管理、任务、上下文、摘要和模型切换属于已有命令；/weather、/news、/web 被路由成可供普通用户使用的工具命令。没有通用斜杠命令注册表。

### 1.7 数据、配置和可观测性

- MemoryStore 每次操作新建 sqlite3 连接；使用线程锁保护本进程内写操作。schema 由 _initialize 中的 CREATE TABLE 和少量 ALTER TABLE 就地维护，没有独立版本化 migration 管理。
- message_history 的保留上限按群修剪；conversation_summaries、memories、group_settings、scheduled_tasks 与历史共用 SQLite 文件。RAG 文档和 chunks / FTS5 表也由同一数据库文件维护。ChromaDB 是另一个持久化目录。
- RAG 文档由 scripts/index_sfm_pdf.py 写入。仓库 .gitignore 忽略 sfm.pdf 和运行数据，因此不能从这份仓库确认实际文档内容或部署索引是否存在。
- Settings.from_env 读取环境变量；本报告未读取环境文件内容，也不记录任何密钥值。README 说明真实环境文件、数据库、日志和仓库缓存不入 Git。
- 工具结果缓存是 10 分钟、最多 400 项的进程内字典；GitHub tree 另有进程内缓存。多进程服务不会共享这些缓存或 BotService 的繁忙状态。
- app/main.py 配置控制台和轮转文件日志，日志文件位于 logs/bot.log。logging_ctx.py 使用 ContextVar 添加请求上下文。/health 仅返回静态 ok，没有外部依赖健康检查、就绪状态或队列指标。

### 1.8 异步任务、错误恢复、部署和测试

- HTTPX、FastAPI 和 Agent 网络工具使用 async 接口。工具单轮并发通过 asyncio.gather；普通消息、摘要更新和定时任务由 asyncio.create_task 创建。SQLite 和 Chroma 查询为同步调用；向量 embedding 也是同步编码。
- 每群同时只有一个 BotService LLM 回复；其他群没有全局并发上限或成本闸门。被拒绝的同群问题已写入历史，但不会排队等待回答。命令分支不经过该群 LLM 繁忙锁。
- Agent 设置最大轮次、每轮调用数、总调用数、工具超时及总时长。网络重试只用于部分 HTTP 获取。模型客户端对空回答可能增加一次 token 上限重试；外部工具一般把错误转成 SearchResult。工具阶段没有统一按剩余 Agent deadline 包住整轮 gather，因此 total_timeout 不是完整的端到端硬截止时间。
- 定时任务通过单个循环轮询并原子 claim；同一循环内逐个执行。任务失败会停用且不自动付费重试；进程重启时发现 running_since 的任务也会停用。状态保存在 SQLite，但循环没有独立任务 worker 池。
- systemd unit 配合 scripts/run.sh 是目前可见部署方式。两个 unit 中存在写死的工作目录，与本次 checkout 目录不同；README / HANDOFF 还描述本机部署和端口，不能仅凭仓库确认线上实际部署路径。仓库文件中没有 Docker 或容器编排配置。
- tests 使用标准库 unittest，覆盖 adapter 事件解析、dispatcher、上下文选择、命令 / BotService、SQLite 记忆、FTS RAG 和 Agent 工具循环。未发现 API 集成、消息洪峰、真实 provider、外网工具、任务中断恢复或 Agent 安全策略测试。本次没有运行测试。

## 2. 当前可复用能力清单

| 能力 | 可直接保留 / 复用 | 适用范围和边界 |
|---|---|---|
| OneBot Adapter + Dispatcher | 是 | NapCat 接入、消息段归一化、reply / at / 附件元数据、分段、群发送串行和状态节流。Agent 目标域变化无需重写这一层。 |
| 规范化事件与结果模型 | 是 | GroupMessageEvent 和 SearchResult 已能作为 transport 与工具层的轻量契约；未来可新增 session / provenance 字段。 |
| Function Calling 数据协议和循环 | 是 | 原生协议兼容、多工具并发、去重、结果预算和证据整理；扩展时应把 if / elif 提升为注册表，逐步迁移而不是重写。 |
| 外部 Web / GitHub 工具 | 部分 | 可复用网络访问和结果模型；需为主动联网继续做 SSRF、URL 重定向和结果大小限制。GitHub 工具目前偏公开仓库和模组资料，不等同于本地工作区检索。 |
| SQLite 历史、摘要、记忆、群设置、调度 | 是，单进程阶段 | 适合先承载会话、工具审计和小规模结构化配方数据；多实例或写入压力提高后再拆分数据库。 |
| FTS5 + Chroma | 是，文档搜索起点 | 适合混合检索文档和源码片段；需加统一排名融合、过滤、稳定引用和索引原子替换。不要用它作为配方事实的唯一数据源。 |
| 任务与群内状态消息 | 部分 | 可保留命令、atomic claim、失败停用和状态节流；长任务需从请求处理移到受控队列并提供任务 ID、状态、取消和成本统计。 |
| 测试夹具 | 是 | 现有 unittest 可扩展到新工具 schema、配方数据、会话隔离和异步 worker。 |

## 3. 架构评估：证据、风险和建议

### 3.1 已确认的实现与缺口

1. **已具备 Tool Calling。** app/llm.py 的 chat_with_tools 调用 OpenAI 兼容工具协议，app/tools/agent.py 的 run_agent_loop 执行工具并将 tool 结果回传模型。已有调用轮数、每轮并发量、总调用量、重复调用和结果长度限制。
2. **工具扩展仍需改核心代码。** schema、名称白名单和 executor 分支在 agent.py 集中维护；本地知识回调接在 LLMHandler。没有 plugin loader、entry point、统一 ToolSpec 或工具权限元数据。工具数量较少时直观；随着配方、文档、源码、SFM 验证增加，单个 executor 会变成扩展瓶颈。
3. **消息传输与业务已有一定分层。** NapCat 协议解析和发送分别位于 NatcapQQAdapter / QQMessageDispatcher；BotService 接收 GroupMessageEvent。但 BotService 与 LLMHandler仍直接按群 ID、QQ 用户、触发规则组织上下文，LLMHandler 知道 SFM / Minecraft 检索细节，因而只解耦了传输，不是完整的 channel-agnostic 业务层。
4. **当前没有明显的循环导入证据。** 主要依赖方向为 main -> BotService / Adapter / Store，BotService -> handlers，LLMHandler -> Agent 和数据源；没有发现反向导入 BotService 的路径。仍存在跨模块使用下划线私有函数，例如 BotService 使用 LLMHandler 的内部 helper，CommandHandler 使用 task_handler 的私有解析工具，属于边界偏松，不应误报成循环依赖。
5. **存在重复实现。** CommandHandler 与 LLMHandler 分别计算可用模型和当前群模型；provider 注册表则在 main.py 中重复构造模型客户端。这些代码当前能工作，但切换 provider 的新增修改点多，模型能力（是否支持工具调用、上下文大小、价格）没有集中描述。
6. **没有结构化配方检索。** 现有 Minecraft 检索返回 Minecraft Wiki / MC 百科搜索结果，GitHub 工具检索公开仓库文本或 KubeJS / FTB 文件片段。代码中没有按整合包版本、物品 ID、机器、输入 / 输出、流体、功率和持续时间建模的配方 schema 或查询服务。
7. **目标中的 GTNH 本地 RAG 尚未实现。** 当前本地索引脚本只接收一个 PDF，按页切片；没有 Markdown / Wiki / 任务文档同步器、版本过滤、仓库符号索引或引用解析管线。Minecraft 网络搜索不是 GTNH 文档 RAG。
8. **SFM 现状是检索，不是编程 Agent。** 可见能力是 SFM 关键词触发、PDF chunk 检索和一般模型回答；仓库没有 SFM parser、程序生成工作流、语法验证、语义验证或测试语料。
9. **部分说明已滞后或不完整。** README 对存储与 SFM RAG 的描述集中在 SQLite FTS；代码还会初始化 Chroma 并合并语义结果。部署 unit 的工作目录与当前 checkout 路径不一致。应维护一处架构事实源，避免读者从文档误认为 Docker / 数据集 / 本地源码检索已存在。

### 3.2 已确认的风险

| 优先级 | 证据与现象 | 对后续 Agent 的影响 |
|---|---|---|
| 高：回调认证默认放行 | app/natcapqq_adapter.py 的 NatcapQQAdapter.verify_callback_token 在 callback_token 为空时返回 True；app/config.py 的 Settings.from_env 默认该值为空；app/main.py 的 onebot_event 只依赖这个校验。部署未配置时，OneBot 事件路由没有应用层 Token 验证。 | Agent 能联网、读资料并产生付费调用；上线应 fail closed，并限制 Bot 服务网络入口。 |
| 高：URL 抓取缺少内网地址约束 | app/tools/external.py 的 looks_like_url 只检查 scheme / netloc；fetch_web_page 创建 follow_redirects=True 的 client，没有限制 loopback、私网、link-local、DNS 重绑定或特殊 IP。app/tools/agent.py 的 fetch_url 分支会到达这个路径。 | 这是服务器端请求伪造风险；主动 Web 工具接入更多后，应在统一网络出口校验目标和每次重定向。 |
| 高：无全局准入 / 付费上限 | app/main.py 的 onebot_event 每条回调都 create_task；app/bot_service.py 的 _try_begin_group_reply 只以进程内群 ID 集合限制同群回复。不同群可以同时发起模型和工具请求；Settings 与响应解析没有全局 semaphore、每用户速率、每日 token / 费用额度或 usage 账本。 | 多群长任务会争用事件循环、连接池和 provider 额度。run_agent_loop 的限次只约束单次 Agent 调用数，不限制所有并发任务的总成本。 |
| 高：ACK 后后台任务不可恢复 | app/main.py 的 onebot_event 在创建 _process_group_message 后即返回 accepted；app/qq_dispatcher.py 的 EventDeduplicator 只保存在进程内。没有持久 inbox 或 durable job queue。进程退出可能丢失已接受的消息；去重器重启后无法识别旧事件。 | 长任务应进入受控、可恢复队列；至少先做任务追踪、优雅停机、排队上限和明确满载策略。 |
| 中：Agent 总超时不完全覆盖工具阶段 | app/tools/agent.py 的 run_agent_loop 对模型调用设 remaining deadline；工具 asyncio.gather 本身不受剩余总 deadline 的 wait_for 包裹。app/tools/external.py 的 _clone_github_repository 还可能使用长于普通网络工具超时的时间。 | 配置的 agent_total_timeout 不能严格保证一个问题在该时间内结束；群锁可能保持较久。 |
| 中：同步操作运行在异步请求路径 | app/memory.py 的 MemoryStore 和 app/rag.py 的 RagStore 使用同步 sqlite3；app/vector_rag.py 的 VectorRagStore.search 同步执行 Chroma 和 SentenceTransformer.encode。app/handlers/llm_handler.py 的 _maybe_search_sfm 在 async 工具路径直接调用这些方法。 | 首次加载嵌入模型或较慢磁盘检索可能堵住同一事件循环，拖慢其他群和状态消息。 |
| 中：向量替换可能保留旧片段 | scripts/index_sfm_pdf.py 的 index_pdf 调 VectorRagStore.upsert_chunks；app/vector_rag.py 的 upsert_chunks 只删除本次传入的 chunk IDs，再 add 新内容，没有清理该文档中已消失的旧 IDs。app/rag.py 的 replace_document 则先删除整份 FTS 文档。 | 更新后 Chroma 可能返回旧版本 PDF 的尾部 chunk；需要按 document_id 原子重建 collection 或明确全量删除。 |
| 中：来源数据没有统一可引用标识 | app/rag.py 的 RagChunk 有页码和 chunk_id；app/handlers/llm_handler.py 的 _search_local_knowledge 将本地 chunk 转成空 URL 的 SearchResult。app/tools/agent.py 的 _append_references 仅为带 URL 的结果附参考链接。 | 外部链接会显示来源，但本地页 / 文件引用没有统一、可校验的引用格式；未来 RAG 需要文件路径、页码或行范围和版本 ID。 |
| 中：多用户上下文是群共享 | app/memory.py 的 message_history 和 conversation_summaries 均以 group_id 为键；app/handlers/llm_handler.py 的 _recent_context_rows 与 app/qq_context.py 的 select_recent_context 会选入最多两条其他用户消息。 | 若需求是个人私聊式会话，需区分私有会话与公共群上下文，否则摘要和跨用户背景会混入。 |
| 中：部署与运行状态缺少观测 | app/main.py 的 health 只返回静态 ok；没有后台 task 数量、队列长度、provider 错误率、工具耗时或 token 成本指标。deploy/*.service 的工作目录写死且与当前 checkout 不同。 | 发生拥塞和成本异常时难定位；多 worker / 多实例也会放大当前内存锁和缓存不共享的问题。 |
| 低：索引与数据迁移分散 | app/memory.py 的 MemoryStore._initialize 与 app/rag.py 的 RagStore._initialize 各自创建 / 修改 schema，没有统一 schema 版本或迁移脚本；FTS 和向量分别维护。 | 新增结构化 recipe、source/version 元数据时需有可回滚迁移和一致性检查。 |
| 低：应用组装带导入期副作用 | app/main.py 文件末尾直接执行 app = create_app()；create_app 会读取环境配置、创建日志目录、初始化 SQLite 和 Store。 | 导入 app.main 的命令行工具或测试会隐式访问运行配置并可能写运行数据；后续可通过 lifespan / app factory 明确创建时机。 |

### 3.3 适合渐进重构吗？

适合。消息协议、发送行为、业务编排、模型请求、工具循环、数据 Store 已经有独立模块，测试也能在不启动 NapCat 的情况下使用假 Adapter / 假 LLM。建议通过兼容现有接口的新组件逐步替换内部实现。暂时没有证据证明需要拆成微服务或重写入口。

值得保留：NatcapQQAdapter、QQMessageDispatcher、GroupMessageEvent、现有模型客户端的兼容协议、Agent 预算和证据整理思路、SQLite MemoryStore 与 FTS 初始实现、unittest 结构。

适合重构：模型 registry / provider 元数据、ToolSpec 与执行器、会话键和准入队列、数据迁移、文档来源与混合排序、配方数据管线、后台任务生命周期和观测性。

## 4. Minecraft Agent 的目标架构

建议将 Agent 运行时视为 BotService 后面的一个应用能力；保留 FastAPI 和 OneBot 边界。内部引入稳定的 Session、ToolRegistry、证据结果和资源预算接口，而不是先换框架。

~~~mermaid
flowchart TB
  QQ[NapCat / OneBot] --> ING[OneBot Adapter + 鉴权]
  ING --> QUEUE[有界准入队列 / Worker 池]
  QUEUE --> SESSION[Session Router 与权限 / 成本策略]
  SESSION --> APP[Bot Application / 命令服务]
  APP --> AGENT[Agent Runtime：规划、调用、预算、取消、引用]
  AGENT --> LLM[LLM Provider Registry]
  LLM --> AGENT
  AGENT --> REG[Tool Registry：schema + handler + policy]
  REG --> RECIPE[配方查询：结构化精确查询]
  REG --> DOCS[GTNH 文档：FTS + 向量混合检索]
  REG --> REPO[仓库：文本检索 + 符号 / 文件读取]
  REG --> WEB[Web 搜索与安全抓取]
  REG --> SFM[SFM 生成 + Parser / 基础验证]
  RECIPE --> EVIDENCE[统一 ToolResult / 来源元数据]
  DOCS --> EVIDENCE
  REPO --> EVIDENCE
  WEB --> EVIDENCE
  SFM --> EVIDENCE
  EVIDENCE --> AGENT
  AGENT --> RESP[答案与引用格式化]
  RESP --> OUT[QQ Dispatcher]
  OUT --> QQ
  MSGDB[(SQLite：会话 / 历史 / 任务 / 配方)] --- SESSION
  FTS[(SQLite FTS5)] --- DOCS
  VEC[(现有 Chroma；按需保留)] --- DOCS
  SOURCES[Markdown / Wiki / Git 仓库] --> INGEST[可重复运行的索引导入器]
  EXPORT[配方导出器 / 版本数据] --> RECIPE
  INGEST --> FTS
  INGEST --> VEC
  EXPORT --> MSGDB
  SRC[SFM Parser 或独立 Validator] --- SFM
~~~

### 4.1 目标边界

- **消息入口与出口：** 继续只做 OneBot 校验、事件规范化、确认接收和回复发送；回调尽量快速入有界队列。若队列满，应有可观察的拒绝 / 重试策略。
- **Session Router：** 把 group_id、user_id、引用线程 / 会话策略映射为稳定 session_id。建议把公共群上下文作为显式可选输入，默认不把其他用户私聊式历史并入个人会话。群配置、用户预算和总并发闸门由此层执行。
- **Agent Runtime：** 保留当前 OpenAI Tool Calling 协议循环，但增加总 deadline 覆盖模型和工具阶段、每工具并发上限、取消、资源预算、回合审计和标准化证据对象。
- **Tool Registry：** 每个工具声明名称、JSON Schema、权限、超时、并发类别、成本权重和 handler；从一个 registry 导出模型 schema。所有工具仍使用白名单执行，禁止模型直接传 shell 命令。
- **数据域：** 配方用确定性的 SQL 查询服务；文档、源码使用关键词 + 向量检索；SFM Validator 接收程序文本并返回结构化错误。每个工具结果包含来源、版本、定位符、检索方法和置信度。
- **离线索引管线：** 对文档、源码和配方导出建立可重复、可校验、可报告来源版本的导入过程。导入失败不应先删除正在使用的索引。

## 5. 技术选型评估

| 选项 | 当前项目中的价值 | 建议 |
|---|---|---|
| 保留当前架构 | 现有 async webhook、消息 adapter、SQLite 记忆和 Function Calling 已运行在同一服务中；新功能与现有工具循环兼容。 | 作为近期主线。先抽清楚会话、工具和检索接口，继续单体部署。 |
| 引入 LangGraph | 提供显式状态图、分支、检查点、人工确认和恢复能力；适合长时间、多阶段、可暂停任务。官方文档将 checkpointer 用于 thread 状态和恢复，将 interrupt 用于暂停后等待外部输入；也要求生产流程配置持久化 checkpointer。[Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)、[Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)。 | 目前没有必须依赖图状态的用例。先不引入；当 SFM 生成流程需要可恢复步骤、人工审查、重试分支，或任务需跨进程恢复时，再以一个独立工作流做小范围评估。 |
| 自己维护 Tool Calling | 项目已有约 10 个 schema、并发执行、有界循环、去重和证据整理；控制成本和工具白名单直接。 | 保留循环，先把静态 schema / if-elif 分发改成 ToolRegistry。维护边界清晰时，自有实现通常比框架替换成本低。 |
| 成熟 Agent 框架 | 可能提供状态持久化、重试、trace、人工审批和组件生态。 | 只有在这些能力成为真实需求且自有实现成本变高时再选。不要因为项目名为 Agent 就换框架。 |
| LlamaIndex | 提供文档 reader、transformations、chunking、缓存和向量存储连接；适合来源种类迅速扩张、需要快速接入常见知识源的情况。官方 ingestion 文档包含可缓存的 transformation pipeline、document management 和 async API。[Ingestion Pipeline](https://developers.llamaindex.ai/python/framework/module_guides/loading/ingestion_pipeline/)。 | 当前单 PDF + FTS / Chroma 规模小，先用轻量 pipeline；把 document loader、chunker、retriever 设为接口。若 GTNH 来源格式多且同步成本高，再试用 LlamaIndex 的 ingestion / connector 子集，不必把 Agent 主循环交给它。 |
| 轻量自建 RAG | SQLite FTS5 和 Chroma 已在项目中；可以直接加 Markdown、版本 metadata、RRF 排名融合和 citation。SQLite 官方将 FTS5 定义为 SQLite 内的全文检索模块，并提供相关性排序等能力。[FTS5](https://www.sqlite.org/fts5.html)。 | 近期推荐。FTS5 负责精确词项，向量负责语义召回；RRF 或统一打分融合。为源文件 hash、版本、路径、页码 / 行号建立引用契约。 |
| SQLite | 当前状态和单进程写入规模适配；事务、FTS5、备份都简单。结构化配方表可以落在现有 SQLite 文件。 | 初期继续使用；增加 schema migration、备份和 WAL / 锁策略评估。WAL 允许同一主机上的读写并行，但 SQLite 同一时刻仍只有一个写入者，且 WAL 不适合网络文件系统；应根据实际并发决定是否保留 SQLite。[WAL 并发说明](https://www.sqlite.org/wal.html)。配方查询用 SQL 索引和唯一键，不用向量数据库。 |
| PostgreSQL | 多 worker / 多实例共享状态、并发写入、并发任务 claim、远程备份和访问控制更成熟。PostgreSQL 的 MVCC 允许事务读取一致快照，减少读写彼此阻塞。[MVCC](https://www.postgresql.org/docs/current/mvcc-intro.html)。 | 等需要多实例、共享队列或 SQLite 锁成为瓶颈时再迁移。当前单机单进程无需提前迁移。 |
| Qdrant | 专用向量服务，支持向量查询时按 payload 做版本 / 文档过滤；这类过滤可在检索过程中执行。增加一个需要部署、备份、监控的服务。[Filtering](https://qdrant.tech/documentation/search/filtering/)。 | 当前代码使用 Chroma PersistentClient 嵌入式本地持久化；Chroma 文档将 PersistentClient 定位为本地开发 / 测试用途，生产建议 server-backed。若部署规模增长，先比较把现有 Chroma 改为 server-backed 与迁移 Qdrant 的成本，再按多版本过滤、数据量和并发需求选型。[Chroma Clients](https://docs.trychroma.com/reference/python)。 |
| MCP | 对独立进程、外部团队工具或多个 Agent 客户端共享同一工具接口有帮助。MCP 提供工具发现、JSON Schema 输入、工具调用和结构化结果协议；权限、限流、输入验证仍须由 server 实现。[MCP Tools Spec](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)。 | 不作为 Bot 内部工具注册的先决条件。内部 Python handler 走 ToolRegistry 更简单；确需将 SFM Validator、配方服务独立部署或供多个客户端复用时，再给该服务加 MCP server / client 适配器。 |
| 外部 SFM Parser / 配方导出器 | 可提供权威语法、语义或版本数据，降低自行猜测格式的风险。 | 放在 adapter 边界后，通过固定参数和结构化 JSON 输入 / 输出调用。版本固定、校验退出状态、超时和资源限制；解析用户 / 模型文本时在受限子进程或容器中运行，不拼接 shell 命令，不执行生成程序。配方导出结果先做 schema、来源版本和数量校验再导入。 |

### 5.1 配方数据模型方向

建议先定义稳定数据契约，再接导出器。至少需保存 modpack_id、modpack_version、Minecraft / loader 版本、recipe_id、machine_id、输入物品与数量、输入流体与数量、输出物品与数量、输出流体与数量、功率 / 电压、能耗、持续时间、来源文件和来源版本。物品标识需保留 namespace 与原始 ID。

查询先要求或推断整合包与版本；对歧义明确追问或列出候选。精确配方、机器和能耗由 SQL 查询返回；生产链可在结构化 recipe graph 上做有界遍历。文本向量只帮助解析别名或定位资料，不能当成精确配方事实。

## 6. 渐进式重构方案

每阶段都可以独立合并并回滚；先保留当前的用户行为与 OneBot 接口。以下是未来实施建议，本次只提供分析，没有运行这些测试。

| 阶段 / 优先级 | 模块和工作 | 风险与控制 | 建议验证 |
|---|---|---|---|
| P0：安全与运行边界 | natcapqq_adapter.py：回调 Token 缺失时 fail closed；tools/external.py：拒绝 loopback / 私网 / link-local 目标、校验每次重定向和 DNS 解析、响应大小上限。config.py：明确部署必填项。main.py：登记后台任务，设全局并发 semaphore、入队上限和优雅关闭；记录模型 usage / 工具耗时并加每群成本预算。 | 鉴权策略收紧可能导致未配环境无法收消息；内网访问白名单可能误拦合法文档。先在测试配置验证，再部署。 | 回调无 / 错 Token 均拒绝；URL 私网、重定向和 DNS 变化用 fake resolver 覆盖；洪峰时队列上限有效；并发成本预算能停止后续工具；关机时任务按策略取消或持久化。 |
| P1：会话与长任务隔离 | app/models.py 增加 SessionKey / request id；BotService 的群级繁忙 set 改为按会话锁 + 全局有界队列；命令路径定义是否也需串行；长任务提供任务 ID、状态、取消和持久化。把 summary 更新和 scheduler 放入受控后台工作器。 | 用户可见行为会变化，尤其群历史是否共享；SQLite schema 与旧数据迁移需兼容。先采用双读或明确默认策略，并保留公共群 context 开关。 | 两用户同群会话相互隔离；同会话重复任务处理策略明确；跨群并发不超过上限；重启后 queued / running 状态满足约定；命令和回答不会造成上下文误删。 |
| P2：ToolRegistry | app/tools/agent.py 把 schema 与 dispatch 抽为 ToolSpec / ToolRegistry；为现有工具逐个适配，保持原名称和 SearchResult 行为；统一参数校验、权限、超时、并发级别、结果长度、错误、来源和取消。LLMHandler 只拿到 registry / knowledge service。 | 迁移时工具漏注册、参数兼容改变或权限遗漏。先并行生成旧、新工具定义比对，再逐工具切换。 | 所有 schema 均能序列化；未知工具、非法 JSON、重复调用、预算耗尽、单工具超时和并发失败都有确定 tool result；现有 Agent 测试行为保留；普通用户不能调用管理员工具。 |
| P3：结构化配方查询 | 新增 recipe schema、SQLite migration、recipe query service 和数据导入命令；接入经确认的 GTNH / modpack recipe exporter，按版本导入并保留源路径。新增 recipe_lookup 和有界 recipe_chain 工具。 | 数据源可能缺版本、机器或流体信息；错误导出会比低召回文本更隐蔽。导入先 staging，校验后事务切换；答案明确显示版本和未提供字段。 | 同名跨版本配方隔离；精确物品 ID、输入输出方向、数量、流体和能耗核对 fixture；错误数据拒收；生产链环路、无解和深度上限得到覆盖。 |
| P4：GTNH 文档、源码与引用 | 为 Markdown / Wiki / quest 和本地 Git 仓库增加索引器；来源 metadata 包含仓库 commit / modpack 版本、文件路径、页 / 行范围、hash。SQLite FTS5 + Chroma hybrid 加统一去重和 RRF；新增限根目录的 workspace search / read / symbol 工具。 | 大文件、二进制、生成目录、忽略文件、私密配置和索引过期风险。明确允许的根路径、排除名单、最大文件 / 结果尺寸；原子索引切换。 | 关键词与语义问题分别评估；要求引用可以打开且定位到原文件；旧索引不会混入；权限外路径和凭据文件不会返回；变更 commit 能触发可重复增量更新。 |
| P5：SFM 编程和验证 | 注册 sfm_docs_search、sfm_validate 等工具；先检索对应 SFM 版本文档和示例，再生成程序；对结果调用版本固定的 parser。若外部工具可给 AST / diagnostics，整理成稳定 JSON；基础语义校验用 allowlist 规则和本地 fixture。 | Parser 不同版本语法不同；通过解析不代表游戏内行为正确；执行生成代码会扩大风险。只解析 / 静态验证，不执行生成程序，答案标明检查等级。 | 官方有效样例通过、常见语法错误准确定位；版本差异 fixture；未闭合语句和未知 API 返回明确诊断；模型生成物不触发宿主 shell / Minecraft 实际执行。 |
| P6：按需选择框架和存储 | 评估是否把长任务改成 LangGraph；测量后决定 PostgreSQL / Qdrant；若外部工具需跨应用复用，再增加 MCP 边界。补充 readiness、队列和 usage 指标。 | 引入多服务会增加运维面，迁移会涉及任务状态和备份。每项以明确容量或恢复需求作为启动条件，保持可回退。 | 故障注入、并发读写、备份恢复、任务检查点恢复、工具协议兼容和运行成本对比。 |

## 7. 仍需进一步确认的问题

1. 多用户“隔离”预期是每位用户完全独立、群共享，还是个人主会话加少量显式公共群上下文？引用回复要不要形成单独 thread？
2. 首批支持的 GTNH / 其他整合包版本有哪些？文档来源、版本许可、更新频率，以及配方导出器的项目、格式、字段完整度分别是什么？
3. 部署会继续单进程单机吗？预期同时在线群数、峰值消息速率、单日模型预算、任务最长运行时间和数据保留时间是多少？
4. SFM Parser 的来源、许可证、支持版本、是否能作为库调用、输入 / 输出格式和可否返回 AST / 诊断信息是什么？
5. 群内公开仓库检索是否还需要搜索本 Bot 所在的本地工作区？如需要，允许索引哪些路径、是否排除 .env / Git ignored 文件、谁有权访问？
6. AI 主动 Web Search 的允许目标范围和引用展示要求是什么？是否允许访问用户明确提供的内网地址？
7. /task 与未来 Agent 长任务要不要允许跨重启续跑、取消、人工审批和费用封顶？这些会影响是否采用 LangGraph 或单独任务 worker。
8. 当前 systemd unit 的绝对工作目录是有意部署到另一 checkout，还是历史残留？需要以真实服务配置确认，不能从仓库推断线上路径。

## 8. 结论

当前架构已有 Agent 的核心基础：模型工具协议、受限工具循环、上下文装配、分群状态和异步消息边界。主要差距集中在 Tool Registry、全局任务治理、严格多用户 session、可追溯知识来源，以及三类新领域能力的数据面：GTNH 文档索引、版本化配方结构数据和 SFM 验证器。

最小代价路径是先为现有单体补好安全和并发边界，再把现有工具循环的数据结构化；优先做确定性配方查询，然后扩展文档与仓库混合检索和来源引用，最后增加 SFM 生成 / 验证工作流。LangGraph、PostgreSQL、Qdrant、LlamaIndex 和 MCP 都应由明确的恢复、容量、数据规模或复用需求触发，而不是作为重写前提。
