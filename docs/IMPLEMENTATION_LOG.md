# Minecraft AI Agent 实施日志

本日志只记录实施过程中的决策、测试事实和风险，不存放密钥、Token、密码或用户消息正文。

## 2026-10-12：启动与恢复

- 完整读取 `docs/ARCHITECTURE_REVIEW.md`、`docs/IMPLEMENTATION_PLAN.md`、`docs/AGENT_TOOL_DESIGN.md`、`docs/MINECRAFT_KNOWLEDGE_DESIGN.md`、`docs/EXECUTION_TASKS.md`；执行基线为 33 项任务及其依赖。
- 工作区最初 `git status --short` 显示整个 `docs/` 为未跟踪目录；里面包含本次任务前已经存在的规划文件。本轮为保护这些文件，不自动 `git add` 或 commit；每次用文件级 diff / 进度记录作为恢复点。
- 已确认源码与规划一致：P0-01 的 `NatcapQQAdapter.verify_callback_token()` 对空 callback token 放行；设置默认空字符串；`/onebot/event` 仅依赖该校验。生产 NapCat header 未核验，仅做本地代码与假请求测试。
- P1-04 作为可选延期处理；P6-01..04 因真实数据 / 外部 Parser 缺失保持外部阻塞；不将 Fixture 状态映射为真实能力。
- P0-01 的实现改动位于 `app/config.py`、`app/natcapqq_adapter.py`、`app/main.py` 和 `tests/test_adapter.py`：缺失 / 全空白 token fail closed；配置环境值 trim；Bearer scheme 与 Authorization header name 大小写兼容；secret 用 constant-time bytes comparison；`/health` 保持原存活语义，新增 `/ready` 503 配置未就绪状态，启动日志只报告配置缺失而不包含 secret。
- 独立复核 P0-01：配置值在 `Settings.from_env()` 归一化，认证器对 raw / Bearer header 兼容并使用 `hmac.compare_digest`；`Settings` / Adapter dataclass repr 隐藏 key、token 和可能内嵌凭证的 endpoint / proxy URL。`/onebot/event` 仍先验 token 再解析 payload，既有 HTTP 401 分支保持不变；`/health` 未改，`/ready` 仅返回状态码与固定原因。确认通过 P0-01 代码验收；仍未在完整 FastAPI runtime 测 `/ready`。
- 测试环境事实：普通系统 Python 缺少 `httpx`，原生 `python3 -m unittest discover -s tests -p 'test_adapter.py'` 在 import 阶段失败。按授权尝试的 `uv run python -m unittest tests.test_adapter` 自动建 `.venv` 并开始同步锁文件依赖，其中触发了大型机器学习 / CUDA wheel 下载；进程结束前没有捕获完整测试结果。未修改依赖清单或锁文件。为避免重复启动重量级下载，随后以临时 `httpx.AsyncClient` 构造器 Stub 运行 `tests/test_adapter.py`，13 项通过；这是 Mock / adapter 逻辑测试，不是完整运行时集成测试。4 个改动 Python 文件 AST parse 通过，`git diff --check` 通过。
- 更正测试数字：增加 repr 保密测试后再次以临时 HTTPX constructor Stub 执行 `tests/test_adapter.py`，最终为 15 tests OK。原生系统 Python 和完整 FastAPI 路由集成测试仍未通过环境验证。
- 检查点策略：因启动时 `docs/` 已整体未跟踪，不创建 commit；使用每任务文件级 diff 与 `IMPLEMENTATION_PROGRESS.md` 状态恢复。
- 下一步：开始 P0-02，只读确认实际 HTTPX / httpcore 锁定接口，再实现连接目标 pinning；不能用 DNS 预检查替代。

## 2026-10-12：P0-02 SSRF 防护

- 源码核对：`app/tools/agent.py` 的 `fetch_url` 分发到 `app/tools/external.py::fetch_web_page`；原实现跟随 redirect，并会将原始 URL 交给 `_fetch_via_jina`。本任务将 arbitrary URL 抓取统一到新增 `app/tools/url_policy.py::SafeHTTPFetcher`，保留 search provider API 自身 client，不改其固定 provider 请求路径。
- 实现选择：HTTPX 0.28.1 的 `AsyncHTTPTransport` 公开配置不提供 per-request pinned DNS dial 参数，因此通过锁定的 httpcore 1.0.9 `AsyncConnectionPool._network_backend` 内部挂接 `PinnedAsyncNetworkBackend`。backend 只接受原 URL host/port，但连接到批准的数值 IP，检查 socket peer；httpcore 仍以原 host 建 TLS，保留 SNI / Host / hostname 证书验证。HTTPX/httpcore 任何版本变化都会拒绝创建安全 transport，直到维护者重新审核。
- 安全边界：禁用 HTTPX 自动 redirect、环境代理和默认 proxy；每跳重新 normalise/resolve/过滤；拒绝特殊地址 / 特殊数字 IP 表达、userinfo、异常端口和编码 authority；response 仅接受 identity 编码并限制 raw body 字节、redirect 与整个请求总时限。Jina compatibility shim 不会发出网络请求或转交 URL。
- 测试证据：FakeResolver + MockTransport 覆盖地址分类、IDNA、DNS 混合答案、rebinding、redirect 私网 / loop / limit、body、timeout、proxy 环境和 Reader 不调用；受控 localhost TCP connector 测量实际 peer；临时自签证书 TLS server 检查 SNI、Host 和成功 / 失败 hostname 校验。最终 `test_web_security.py` 17 项、`test_adapter.py` 15 项、`test_agent.py` 6 项通过；测试全本地/mock，无外部 HTTP 请求。`git diff --check` 通过。
- 安全复核：未把 DNS 预检查当作 SSRF 防护；IP dial + 实际 peer 检查直接覆盖 TCP 连接目标。内部 httpcore seam 是明确维护成本，精确版本门禁为 fail-closed；若未来需要跨版本安全支持，选择经过审计的定制连接器/transport，而非退化到普通 DNS 预解析。普通公网目标和真实企业代理没有在线集成测试。
- 失败恢复：首次 Mock 响应测试因流式响应构造不当失败，调整 Fixture 让测试使用原始异步字节流；随后安全用例通过。首次新增 transport 版本测试断言错误放置，诊断后移动到 redirect 测试并通过。未降低安全条件。
- P0-02 检查点：`app/tools/url_policy.py`、`app/tools/external.py`、`tests/test_web_security.py`；无 Git commit（原 docs 未跟踪且 `.git` 只读）。

## 2026-10-12：P0-03 有界 OneBot 消息运行时

- 实施内容：新增 `app/task_runtime.py::MessageTaskRuntime` 固定 worker + 有界 queue；callback 改为 `try_submit`，队列满返回 503 + `Retry-After: 1`。只有接受入队才登记 `EventDeduplicator`，所以 caller 获得 overload response 后同一 message_id 可再试。把 quote hydration / group bot name resolution 移到 worker；回复后的 group summary 作为受管后台任务，数量限制为 worker 数，达到上限时仅跳过非关键 summary 并记录。
- 生命周期：FastAPI startup 启动固定 worker；shutdown 先停止 scheduler，再在设置的 drain deadline 内处理队列与 summary。超时时取消 workers/background、清理尚未开始的内存队列项并记录丢弃数；常规 handler exception 被观察并记录，不杀死 worker。worker 被 cancel 时 `_process_group_message` 同步取消并回收其 service 子 task。
- 测试证据：新增 `tests/test_task_runtime.py` 5 项，覆盖队列/worker 上限、队列饱和、异常后存活、drain、cancel、后台任务上限与异常收集；新增 `tests/test_main.py` 3 项，ASGI lifespan 验证容量满的 503、不提前写 dedupe、重试成功、重复事件仍返回 duplicate、未配置 callback key 时 readiness 503 / callback 401。`test_bot_service.py` 22 项通过。P0-01 `test_adapter.py` 15 项、P0-02 `test_web_security.py` 17 项、Agent `test_agent.py` 6 项均通过；`git diff --check` 通过。
- 独立审查与风险：HTTP handler 数受 worker 数约束；queue 长度受单独容量约束；summary background 再受 worker 数约束。每个 Agent 内仍有受配置限制的并发工具 fan-out，scheduler 另有一个串行执行位，因此这里不是“所有 HTTP request 共用一个 semaphore”。进程 crash 时 accepted 的内存队列项会丢；shutdown 超时也会丢未开始项。NapCat 是否重试 503 尚未在生产配置确认，生产启用前需要维护者核实。
- 运行测试会触发项目既有 FastAPI `on_event` 弃用警告；没有为此引入 lifespan 重构。测试 app 使用临时 SQLite、关闭 RAG、不运行真实模型 / 外部网络。
- P0-03 检查点：`app/config.py`、`app/main.py`、`app/qq_dispatcher.py`、`app/task_runtime.py`、`tests/test_main.py`、`tests/test_task_runtime.py`；无 Git commit（避免纳入既有未跟踪 docs）。
- 下一步：P0-04 Agent deadline 与取消传播。

## 2026-10-12：P0-04 Agent 总 deadline / cancel

- 源码核对：`run_agent_loop` 之前只对每轮 model call / final call 使用 wait_for；并发工具 `asyncio.gather` 只依赖各个 handler 的 per-tool timeout，聚合耗时可能超过 Agent 总预算。final timeout 使用 `max(3.0, min(15.0, remaining))`，剩余时间不足 3 秒时能超过总 deadline。`LLMHandler` 的 forced tool、非 Agent chat 与 fallback 路径也没有共用绝对截止时间。`BotService.handle_group_message` 已有 `finally: _end_group_reply()`，取消时可释放 group busy marker。
- 实现决策：`run_agent_loop` 新增可选绝对 `deadline`（基于 `asyncio.get_running_loop().time()`）；缺省仍使用 `total_timeout` 推导，旧调用兼容。`LLMHandler._reply_with_llm` 为一次响应生成单一 deadline，并传递给 Agent/fallback；forced tool、plain chat、final chat 通过 `_await_before_deadline` 调用侧 wait_for 约束，不修改 `DeepSeekClient` 方法签名，也不修改 `app/llm.py`。per-tool timeout = `min(configured_tool_timeout, remaining)`。
- 并行工具取消：把每次有效 tool call 保存为有名 child task，用绝对 deadline 限制 `asyncio.wait` 聚合；deadline / 外部 cancellation 时 cancel 所有未完成子任务并 `gather(..., return_exceptions=True)` 清理。工具主动 cancellation 也作为 BaseException 路径处理，避免格式化代码把它误判为成功结果。进度 callback 和 final generation 一并限制；移除 final timeout 的 3 秒 floor。
- 测试证据：`tests/test_agent.py` 11 项覆盖已有 tool-call 协议，以及总 deadline 限制单工具预算、模型调用取消、并发工具 timeout 与主动取消、notice callback 超时、截止点已过则不发 final request。`tests/test_bot_service.py` 23 项新增真实 BotService fixture 的取消测试，断言 group busy marker 在外部取消后释放。ASGI `test_main.py` 3 项回归通过；涉及 Python 文件 AST parse、`git diff --check` 通过；全程 Fake LLM/tool，无模型 API 请求。
- 独立审查：`asyncio.CancelledError` 不被普通 `except Exception` 吞掉；LLMHandler 的 deadline 适用于现有 chat 方法和 DeepSeekClient retry 包装。协作式 async I/O 的测试均在短 deadline 内完成清理。Python 无法抢占同步事件循环阻塞或不遵守 cancellation 的第三方 awaitable；P0-05 解决已识别阻塞 storage/embedding 出口，并保留为明确限制。
- P0-04 检查点：`app/tools/agent.py`、`app/handlers/llm_handler.py`、`tests/test_agent.py`、`tests/test_bot_service.py`；`app/llm.py` 和 `app/bot_service.py` public interface 保持不变；无 Git commit（既有 untracked docs 不可安全纳入）。
- 下一步：P0-05 同步 SQLite / Chroma I/O offload。

## 2026-10-12：P0-05 有界 storage offload

- 源码核对与范围调整：`MemoryStore` 自身已在 `_connect()` 的 `finally` 关闭 SQLite connection；`RagStore._connect()` 原先返回裸 connection，而 `with conn` 只提交 / 回滚，并不关闭。除此之外，BotService 在 async `handle_group_message()` / `run_scheduled_task()` 里同步调用消息存储、CommandHandler 的 SQLite 管理命令也直接执行；scheduler 在 async loop 内直接 claim / update task。基于这些真实调用点，把 `app/bot_service.py` 纳入 P0-05 修改范围，并保留 Store 的同步 API 供 CLI / 测试继续调用。
- 实现：新增 `StorageExecutor`，分别管理 SQLite 与 vector/embedding 的 `ThreadPoolExecutor`；semaphore 限制已提交 work 为 workers + queue capacity，配置夹紧到 SQLite 1–4 workers、vector 1 worker、queue 1–128。vector lane 固定串行，直到本项目有证据验证 Chroma client 与 sentence-transformers 共享实例可并发。`create_app()` 负责注入及关闭；BotService 命令/消息/定时任务和 main scheduler 的 DB 出口交给 SQLite lane；LLMHandler 的 memory / FTS / summary 和向量检索分别 offload，sentence-transformers 编码随 `VectorRagStore.search()` 一同在线程内运行。
- Future 与取消：同步函数运行期间，取消 async 调用方不会撤销 executor future 或提前释放槽位；实际完成回调才释放。每 50ms 为 future completion 设置一次有界 timeout 检查作为线程完成通知的 fallback；被取消的调用方不再等待底层工作，但其槽位仍占用至操作完成。shutdown 在 message runtime / scheduler 关停后停止新提交、限时 drain 已提交工作；Python 不可抢占正在运行的同步函数，超过 grace 时记录日志并非阻塞关闭。
- RAG Store 连接：`RagStore._connect()` 改为 contextmanager，成功 commit、异常 rollback、finally close；增测试验证两种路径都已关闭连接且 rollback 内容不可检索。MemoryStore 的 `_connect()` 已有 finally close，因此没有重写其同步存储接口。
- 测试证据：最终命令 `.venv/bin/python -m unittest tests.test_storage_executor tests.test_bot_service tests.test_rag tests.test_memory tests.test_main -v`：40 tests OK。覆盖两个 lane 的 heartbeat offload、vector blocking fixture、workers+queue 背压、异常传递、caller cancellation 后占槽、shutdown drain / reject new work、hard caps、20 个并发 SQLite 写入无丢失、BotService 实际 async storage 注入、RagStore connection close / rollback、现有 memory CRUD 与 ASGI 入队回归。10 个改动 Python 文件 AST parse OK，`git diff --check` 通过；Fake / 临时 DB 测试，没有真实 Chroma 模型加载、真实向量库并发压测或生产流量。
- 调试记录：早期短超时 fixture 显示快速同步 future 完成时测试立即 await 会遇到本环境线程通知时序延迟；等待增加有界 50ms fallback 后，完整 BotService 以及 storage test suite 均通过。取消测试曾把已排队的第二项误当作未提交；依据 worker+queue 定义修正该断言到两个 pending futures，槽位上限未放宽。
- 独立审查：源码扫描确认所有现有 async MemoryStore/RagStore/vector 搜索调用均有执行器路径，CommandHandler 数据库命令也在 SQLite lane；`VectorRagStore` 的 lazy model / Chroma init 与 `.encode()` 都发生于 `search()` 内，故不会在请求 loop 中冷启动。剩余风险是任意同步第三方函数超过 shutdown grace 后仍可能占用线程；vector lane 缺乏真实 Chroma/embedding 初始化与性能集成证据。P0-05 不引入依赖或真实领域数据。
- 文件检查点：新增 `app/storage_executor.py`、`tests/test_storage_executor.py`；修改 `app/config.py`、`app/main.py`、`app/bot_service.py`、`app/handlers/llm_handler.py`、`app/rag.py`、`tests/test_bot_service.py`、`tests/test_main.py`、`tests/test_rag.py`、`docs/IMPLEMENTATION_PROGRESS.md`、`docs/IMPLEMENTATION_LOG.md`。无 commit：此前工作区已有未跟踪 `docs/`，且 `.git` 对本会话只读。
- 下一步：P0-06 在每个真实 provider dispatch 前实现原子 budget reservation / settlement / release / expired recovery；保持现有模型公开方法签名。

## 2026-10-12：P0-06 模型预算与全局 dispatch gate

- 源码核对：`DeepSeekClient.chat()` 有空回复 retry；`chat_with_tools()` 每轮一次 POST；`chat_stream()` 是独立 HTTP stream；`LLMHandler` 摘要、BotService 消息与 scheduler 均可能发起请求。通过 `create_app()` 构造的所有模型客户端现在共享同一个 `BudgetController`，群 / 用户 / message scope 由 BotService 和摘要路径的 contextvar 注入，不改变现有公开 `chat` / `chat_with_tools` / `chat_stream` 签名或 group-shared memory 行为。
- 并发审查补充：此前 P0-03 限定了入站 worker，但定时调用与受管 summary 可和消息并发；因此在真实 dispatch 边界补上共享 `LLM_MAX_CONCURRENT_REQUESTS` 信号量（默认 4，环境值夹紧 1–32）。JSON 请求在网络完成 / 结算后释放；stream 在关闭前持有槽位。预算被拒、等待 gate 时取消、请求构造失败均不会发送 HTTP。
- Reservation / settlement：`UsageBudgetStore` 以 SQLite `BEGIN IMMEDIATE` 在一次事务中检查 global、group、user 的 request / token budget 并插入 reservation。每次 POST 都独立记账，包括 `chat()` 空回复 retry、工具轮次、final call 与 stream。通过 P0-05 SQLite executor 执行账本操作；异步 reserve 被取消但后台同步 insert 已提交时，controller 取回 reservation 并释放，避免请求未发却错误占额。账本不保存 prompt 或凭据，并保留估算方法、scope keys、请求 ID、状态和 lease。
- 用量语义：provider 返回合法 input/output usage 后按实际数结算；缺失 / 无法解析则 token 字段为 NULL，unknown reservation 保留完整预留额。实际 usage 高于估算额时记实际用量并使后续准入拒绝。预算 token 估算覆盖完整 JSON request payload（包括 tool schema），仍是 UTF-8 字节近似，不是 tokenizer 精确值；配置 token quota 时每次请求用 `max_tokens` 限制单次 completion。Request count 是在配置限额后的硬准入；usage/cost 在请求前不可知，不能声称模型费用硬限额。没有 auditable rate card 时费用保持 NULL / unknown，显式 `cost_limit_minor_units` 被拒绝。
- 错误 / 恢复：仅 ConnectError / ConnectTimeout / PoolTimeout / InvalidURL / UnsupportedProtocol 视为有证据确认未派发并 release；read timeout、派发后取消、HTTP error、无效 response usage 都保留 unknown 占额。连接 / status 错误字符串不回显 provider URL 或 response body。启动期过期 reservation 转为 `unknown_settled`，不释放旧额；长 stream 有独立 heartbeat 续租；lease 下限覆盖配置的最长模型 timeout、agent deadline 与 shutdown grace。
- 配置行为：新增 global/group/user 日请求、token 配额、completion cap、lease 与模型并发环境配置。当前配置若没有请求 / token 限额，账本仍统计请求，但不会拒绝超限请求；startup 产生无 secret 的 warning。显式无效的 request/token 配额启动失败，避免误将负数或格式错误静默解释为“无预算”。没有可信费率时不提供费用配额。
- 测试证据：`tests.test_usage_budget` 21 项覆盖原子并发准入、group/user scope、UTC 跨日、cancel-before-dispatch release、连接失败与读超时差异、retry / 工具轮次独立记账、stream exact/unknown usage 与 paused-consumer lease renewal、expired recovery、unknown 保守占额、实际 usage 超预留、工具 Schema 估算、费用限额 fail closed、请求 gate cancel cleanup 和错误去敏；`tests.test_main` 5 项覆盖配置 lease、无效预算配置和既有 ASGI 路由。最终 `.venv/bin/python -m unittest discover -s tests -v` 为 117 tests OK，含 P0-02 localhost TCP / TLS connector tests；MockTransport 与本地测试，无模型 API、公网或外部服务调用。最初 sandbox 全量回归仅因禁止本地 socket 有两项失败；获得本地测试命令授权后重跑全部通过。`py_compile` 与 `git diff --check` 通过。
- 独立复核与限制：公开模型调用签名保持不变；每个真实 dispatch 都在 HTTPX send 前 reserve，模型 semaphore 在 reserve 前取得；stream 取消会 stop heartbeat、标 unknown 并 close response。当前每日 request / token 配额没有运维值，须维护者配置才实际拒绝调用；token estimate 无法取代 provider tokenizer；费用未知。模型 concurrency semaphore 是单进程范围，若部署多 worker / 多副本，实际 aggregate 上限是每副本上限之和；SQLite reservation budget 仍以同一 DB 文件事务跨进程原子。
- 文件检查点：新增 `app/usage_budget.py`、`tests/test_usage_budget.py`；更新 `app/llm.py`、`app/config.py`、`app/main.py`、`app/bot_service.py`、`app/handlers/llm_handler.py`、`tests/test_main.py` 以及本进度 / 日志文件。未提交 commit：现有 docs 整体未跟踪且 `.git` 只读；保留文件级检查点。
- P0-06 完成。下一项 READY：P0-07，只处理现有 SFM PDF 索引更新 / 删除残留，不提前开发 P2-03 generation 框架。

## 2026-10-12：P0-07 启动

- 源码核对：`scripts/index_sfm_pdf.py::index_pdf()` 先调用 `RagStore.replace_document()` 替换 FTS/SQLite，再调用 `VectorRagStore.upsert_chunks()`；`upsert_chunks()` 目前只删新输入中的同名 chunk IDs，因此 PDF 缩短时旧尾部向量残留。`VectorRagStore` 按 document name 使用独立 Chroma collection；本任务保留该边界，不引入 generation / active revision。
- 实施范围：将 Chroma 新文本与 embeddings 先在内存准备完毕，再枚举并删除目标 collection 的全部旧 IDs，添加新完整集合；空 chunk 列表表示清空该文档。新增 SQLite / FTS 与 Chroma 的单文档幂等删除接口，由 PDF 脚本协调两端并在任一端失败时明确报告 partial/error。没有共同事务；不承诺跨库原子回滚，也不保留旧 Chroma snapshot。
- 测试计划：Fake Chroma collection / embedder 测 3→1 替换、重复替换、空替换、文档隔离、embedding 失败时旧向量保留；临时 SQLite 验证删除 FTS/chunks/document 记录；脚本故障注入覆盖一端失败与重试。测试不会加载真实 sentence-transformers 模型或访问真实索引。

## 2026-10-12：P0-07 验收与 P1-01 完成

- P0-07 复核：向量替换先准备全部新 embeddings，再列出并清掉该文档 collection 的全部旧 IDs；缩短、重复及空替换会清除旧尾部。SQLite / FTS 事务内删除文档行；CLI 删除协调两端、分别尝试并以 partial error 表示部分失败，重复重试可收敛。没有跨库事务、旧向量快照或 P2 generation 能力。Fake Chroma / Fake embedder + 临时 SQLite 的定向用例 9 项通过；全量 unittest 123 项通过；py_compile、`git diff --check` 通过。没有真实 PDF、Chroma 持久库或 embedding model 集成验证。
- P1-01 源码核对：旧 Agent 有 10 个静态 OpenAI Schema、`TOOL_NAMES` 独立集合、`run_agent_loop` 白名单检查与 `_execute_native_tool_call` if/elif 分支；公开 LLM Function Calling DTO 在 `app/llm.py`，`SearchResult` 在 `app/models.py`。预算已有 `BudgetScope` / `ToolUsage` / `BudgetController` 位于 `app/usage_budget.py`，因此复用而未重复定义或改公开模型 API。
- P1-01 实施：新增 `app/tools/contracts.py` 与 `app/tools/registry.py`。`ToolSpec` 同时承载现有 schema 和兼容 handler；冻结注册表检查名称重复、生成 OpenAI Schema、过滤策略、验证 JSON object / schema 子集、使用有界参数字节 / 字符 / 数组数、并发 semaphore、deadline / timeout、结果及 Evidence 上限和结构化错误。Agent 已通过 Registry 取 Schema 与 dispatch。此项仍调用 `_execute_native_tool_call` 兼容 adapter；按计划留待 P1-02 迁移各工具执行器。SearchResult 双向适配，Evidence 从合格来源 locator 构建。将当前 LLM budget ContextVar scope、controller 注入每次 ToolContext，但不启用用户级 SessionKey。
- 兼容依据：迁移前 10 个 OpenAI tool schema 的 canonical JSON SHA-256 固定在测试 `bcd6ed210d8615e1f0e91323c4eaefbb58085d3588d1ea2afd2465c1d47676ea`，迁移后完全一致。保留旧 weather `query` 别名与 batch / multi-search 标量到数组的规范化。Agent 继续每个 tool_call_id 输出一个 tool message；P0-04 request deadline 仍取消并 await sibling 工具。强 schema 校验拒绝其他未知字段和类型错误；这属于既定参数验证边界。
- P1-01 测试：新增 `tests/test_tool_registry.py` 11 项测试注册重复 / freeze、Schema 快照、SearchResult / Evidence adapter、budget 注入、schema 验证、旧参数归一、权限过滤 / 拒绝、错误去敏、timeout、取消、per-tool 并发与输出裁剪。定向 `tests.test_tool_registry tests.test_agent` 共 23 项通过；全量 `.venv/bin/python -m unittest discover -s tests`：135 项通过；`py_compile` 与 `git diff --check` 通过。首轮定向发现 registry timeout 与 Agent 总 deadline 的交接差异；修正为 deadline 到期继续走 Agent sibling cancellation 路径，而单工具 timeout 返回 `TIMED_OUT`；后续全量通过。
- P1-01 独立复核：没有改 `app/llm.py` 或 `app/models.py`，因为当前模型 DTO / SearchResult 足够，budget usage contract 已存在；保留 `TOOL_DEFINITIONS` / `TOOL_NAMES` 兼容 facade，但两者现由 registry 生成。工具执行的历史 if/elif 和 forced `/weather` `/news` `/web` 路径仍保留；下一项 P1-02 / P1-03 的范围不提前吸收。没有 API key、prompt、异常正文进入 Registry 日志；只记录 tool 名和异常类型。
- 检查点：工作区原本含 P0 代码和未跟踪的规划 docs，`.git` 只读；未 stage / commit。P0-07 修改 `app/vector_rag.py`、`app/rag.py`、`scripts/index_sfm_pdf.py`、`tests/test_rag.py`；P1-01 修改 `app/tools/agent.py`，新增 `app/tools/contracts.py`、`app/tools/registry.py`、`tests/test_tool_registry.py`，并更新进度文件。已留下可审查文件级 checkpoint。
- 当前下一项：P1-02（依赖 P1-01）。P1-04 保持 DEFERRED；P6 的真实 GTNH 数据、Recipe exporter 与 SFM Parser 仍为外部缺失材料，不阻塞 P0–P5。生产未部署，NapCat 生产 header / token 需要部署者确认。

## 2026-10-12：P1-03 命令兼容 / Policy 验收与 P2-01 启动

- P1-03 实施：forced `/weather`、`/news`、`/web` 仍由 `forced_tool_call()` 映射旧名称和参数，但 `execute_tool_call()` 现在作为兼容 facade 统一 dispatch `TOOL_REGISTRY`。`LLMHandler` 从经过 OneBot 回调认证的群事件构造不可变 `PolicySnapshot`，传入普通 Agent 与 forced path；`ToolPolicy` 支持角色约束，并要求每个 `ToolSpec` 显式声明至少一种 audience / 权限边界。既有网络工具明确允许 `group_member`，`bot_admin` 是可加角色，不改变原命令开放范围。forced path 显式关闭缓存并以 `ToolContext` 传递调用身份、预算 scope、deadline 和限制；兼容 adapter 保留 SearchResult observation。
- P1-03 测试与审查：增加实际 `BotService` 事件 fixture，证明非管理员 `/weather` 仍运行 provider 且模型回复保持原行为；Registry / compatibility tests 覆盖无关角色被拒绝且 provider 未执行、管理员 additive role、权限拒绝与缺数据状态分离、较长网页结果限制。目标模块 `py_compile` 通过；`.venv/bin/python -m unittest tests.test_tool_compat tests.test_tool_registry tests.test_agent tests.test_bot_service -q`：59 项通过。全量 unittest 在受限沙箱首次遇到两项本地 TCP / TLS connector 测试无法 bind；按环境权限流程获得授权后，`.venv/bin/python -m unittest discover -s tests`：147 项通过。`git diff --check` 通过。独立复核确认 registry offered schemas 和 dispatch name allowlist 共用同一 snapshot；无外网或模型请求。曾将大网页兼容 summary 的精确断言从 423 调整为 `<=423` 并同时断言省略号，因为既有 `rstrip()` 会移除被截断前的尾随空白，实测输出 422；实际兼容上限未放宽。
- 限制：强制命令的认证前提来自 P0-01 OneBot callback fail-closed；生产 NapCat Authorization 配置仍待部署者核对。工具当前公开范围保持全体群成员；新受限工具必须声明专门角色、权限或 group / user allowlist。全量测试的 TCP/TLS loopback fixture 只证明本地受控连接器行为，不代表生产网络环境验证。
- 文件检查点：修改 `app/tools/contracts.py`、`app/tools/legacy_tools.py`、`app/tools/policies.py`、`app/tools/agent.py`、`app/handlers/llm_handler.py`、`tests/test_tool_registry.py`、`tests/test_tool_compat.py`、`tests/test_bot_service.py`；更新实施进度文件。无 Git commit：`.git` 元数据对本会话只读且工作区原有 docs 未跟踪，未暂存或覆盖用户文件。
- P1-03 验收后，P2-01 成为唯一执行中的任务；P1-04 继续 DEFERRED，不能阻塞 P2–P5。P2-01 将只建立通用 Markdown 来源 / 行定位与索引状态 contract，测试使用人工 fixture，不读取或假设 GTNH 领域语料存在。

## 2026-10-12：P2-02 版本过滤 / 混合检索 / Evidence 验收与 P2-03 启动

- 实施边界：复用 P2-01 的 `DocumentSource` / `DocumentFilter` / locator contract、SQLite FTS5、当前 Chroma 和 P0-05 `StorageExecutor`。新增 `DocumentKnowledgeService` 并行调两路 top-k，以 RRF 合并，不混算 BM25 与 cosine 分值。SQLite 与 Chroma 都按来源、语料、pack、pack version、game version、source revision 精确过滤；不会在版本不匹配时回退其他版本。保留原 `RagStore.search` 与 `VectorRagStore.search(query, document_name, limit)` 调用形态，vector 新查询接口为附加能力。
- 状态与覆盖：Service 区分未配置来源、来源未索引、版本不匹配、有效查询无结果、仅 FTS 可用和 Chroma 后端故障。共享 corpus 上 `indexed_corpus_documents()` 通过 document-id `$in` 加相同 metadata/version 条件检查目标版本覆盖；每次最多核验 128 个目标文档，更大范围标记截断 / PARTIAL，避免按任意一个 collection 存在就谎报完整。Chroma collection 的真实 `NotFoundError` 与 fake client `KeyError` 表示无 collection；其它 lookup failure 在 Knowledge strict path 传播为 backend error；既有旧 API 仍默认返回兼容空结果。
- 引用及 Agent 兼容：FTS / provenance-aware Chroma chunk 返回 document、章节与原始行 / 页 locator，Evidence 带来源 revision 和 pack version。旧 Chroma 向量缺 `source_uri` 时原实现生成的 `source="vector"` 只作为 legacy DTO 占位，不再输出伪 `source_uri` / locator / Evidence。Agent observation 在 PARTIAL 且带 error 时保留可用 FTS 片段，避免原来优先显示 error message 导致 FTS evidence 被吞。`LLMHandler._search_local_knowledge()` 通过新 Service 搜索既有 SFM document，并继续结合旧 Minecraft Wiki / MC百科来源，不更换 Bot 工具名或模型 schema。
- 新增人工 fixture tests：精确版本双后端隔离与引用；未索引 / 未知 corpus / 版本错配各自状态；空 document；FTS-only partial；共享 corpus 有 / 无向量混合覆盖；Chroma 仅旧版本向量时不误计当前版本覆盖且不混入过期内容；Chroma backend failure 不当成缺索引；旧 vector-only 无 source URI 不产生 sentinel citation；Agent registry / loop 保留 ToolResult、partial content 和引用；RRF 依据名次、稳定打破同分。所有 fixture 是 synthetic，不是 GTNH 样本。
- 测试结果：`.venv/bin/python -m unittest tests.test_hybrid_retrieval tests.test_rag -q`：25 tests OK；最终改动后 `.venv/bin/python -m unittest discover -s tests`：164 tests OK。全量首次在默认沙箱中有 2 项已有 loopback TCP/TLS SSRF connector tests 因监听权限失败；在授权的本地全量重跑中通过。`py_compile` 与 `git diff --check` 通过。未调用模型 API、公网搜索或真实 Chroma / sentence-transformers。
- 独立审查：第一轮发现 Agent observation 丢弃 PARTIAL 片段、legacy `vector` sentinel 伪引用、共享索引不完整仍可能 OK、collection backend failure 误作无 collection；均修复并测试。第二轮发现 coverage 需同时应用精确版本 filter，且结果结构不应暴露 `source_uri="vector"`；均已修复并新增 stale-version-only vector fixture。Reviewer 最终结论 APPROVED。`indexed_corpus_documents()` 使用 Chroma `$in` 元数据过滤目前由 Fake Chroma 验证，尚未用真实 Chroma persistent collection 做组件集成。
- 文件检查点：更新 `app/knowledge/documents.py`、新增 `app/knowledge/retrieval.py`，更新 `app/rag.py`、`app/vector_rag.py`、`app/handlers/llm_handler.py`、`app/tools/agent.py` 与 `app/tools/legacy_tools.py`，新增 `tests/test_hybrid_retrieval.py`，更新进度。本环境 `.git` 对当前会话只读，未 stage / commit；通过可审查文件 diff 作为 checkpoint。无数据库实际迁移、真实内容索引或部署动作。
- 下一批显式 READY：P3-01、P4-01、P7-01；按域阶段顺序继续 P2-03。P7-01 显式依赖虽然满足，暂留作 P0–P5 后的完整 Bot 兼容验收；此前每项 / 每阶段仍执行回归。P1-04 仍延期，P6 真实资料 / 导出器 / Parser 仍外部阻塞，不阻塞 Fixture 能力。

## 2026-10-12：P2-01 通用文档 Contract / Markdown 摄取验收与 P2-02 启动

- 源码复核：原 `RagChunk` 只保留 `chunk_id/source/page/title/content/score`；`rag_documents`、`rag_chunks` 与 FTS5 表服务 SFM PDF，`replace_document()` 原先只接受 `(chunk_id, page, title, content)` 元组；PDF CLI 调用这一同步 API。为避免破坏旧索引，采用新增 `rag_document_sources`、`rag_chunk_locators` 表，不重写原表或旧 SFM PDF API。没有把解析、版本过滤、混合检索或 generation 切换塞进 RagStore 单一抽象。
- 实施：新增 `app/knowledge/documents.py` 中的 `DocumentSource`、独立 `document_id`、`DocumentLocator`（line/page/anchor）、`DocumentChunk`、`ParsedDocument` 与 index status DTO；新增 `app/knowledge/ingestion.py` 的 strict UTF-8 Markdown parser、ATX heading hierarchy / 行区间 chunker、确定性 chunk ID 和明确文件摄取入口。版本、来源 revision、license 和 pack/game key 由调用者显式传入，不解析未知 front matter。`RagChunk` 以可选字段保留 provenance / locator / parser 元数据；旧 tuple 接口保持；`RagStore.index_status()` 明确返回 NOT_INDEXED / EMPTY / INDEXED。replace / delete 在同一 SQLite transaction 中清理 locator metadata。此任务未实现 Chroma 写入、版本过滤、RRF、active generation 或领域数据。
- 测试与审查：新建 `tests/fixtures/documents/markdown_fixture.md`，全部数据为人工示例。14 项 `tests.test_rag` 包括旧 schema migration fixture 搜索、版本 source 保存、不同 source_id / document_id、heading path 与精确行范围、代码 fence 中 heading 排除、重复 ingestion ID/hash 行为、坏 UTF-8、空与缺索引状态、极长单行和高 overlap 的有界推进，以及既有 PDF tuple / deletion / Chroma 最小行为。定向 Rag / BotService / Agent 回归 `.venv/bin/python -m unittest tests.test_rag tests.test_bot_service tests.test_agent -q`：50 项通过。全量 `.venv/bin/python -m unittest discover -s tests`：152 项通过；包含 localhost TCP/TLS 测试并在获准 loopback 绑定后运行，无外部网络 / provider API。`py_compile` 与 `git diff --check` 通过。
- 独立复核：原 legacy DB 仅新增两张 metadata 表，不执行迁移回填，不要求清除用户数据；旧记录仍可 FTS 检索，但其 provenance 字段自然保持缺省。Markdown 新记录的 `source_id` 与 `document_id` 不强行等同；行 locator 是原始文件行，不是检索结果的临时序号。chunk ID 包含 source/document identity、section、字符偏移、行和内容 hash，重复导入稳定；section 独立切块，不跨 heading 混合。注意这是轻量 Markdown parser，不声称实现完整 CommonMark。
- 已知限制：fixture 是合成知识，未提供任何真实 GTNH 覆盖证据。Markdown 摄取目前只建立 SQLite / FTS 侧内容；向量写入及一致版本过滤将在 P2-02 / P2-03。历史 PDF 不自动补 source metadata；重新运行既有 importer 会以兼容 legacy source 标记新的来源记录。当前 status 粒度为单 document，corpus / version 查询状态由后续 Knowledge Service 负责。
- 文件检查点：新增 `app/knowledge/__init__.py`、`documents.py`、`ingestion.py` 和 `tests/fixtures/documents/markdown_fixture.md`；更新 `app/rag.py`、`tests/test_rag.py`、进度文件。未修改 `app/config.py` 或 `scripts/index_sfm_pdf.py`，因为默认参数已足够且保留原 PDF CLI 即满足兼容验收。无 Git commit：工作区 docs 原未跟踪且 `.git` 只读，避免对已有用户文件做 stage。
- 下一项 P2-02 依赖已经满足：P2-01、P0-05、P0-07。将把现有 FTS 和 Chroma query 两路扩展到共享 version/source filter、RRF 与文档 Evidence；仍只用上述人工 fixture 与 fake embedding，真实 GTNH 数据继续为未准备状态。

## 2026-10-12：P1-02 验收与 P1-03 启动

- P1-02 源码核对：Agent 原有 `_execute_native_tool_call()` 内有 10 个工具分支和工具结果缓存；forced `/weather`、`/news`、`/web` 在 `app/handlers/llm_handler.py` 另走 `forced_tool_call()` / `execute_tool_call()`。本任务只迁移原生 Agent Tool Calling，明确留 forced commands 给 P1-03。`app/tools/external.py` 现有 provider / SafeHTTPFetcher leaf APIs 可复用；`LLMHandler._search_local_knowledge` 可直接作为 handler context callback，因此没有为实现迁移重写这些接口。
- P1-02 实施：新增 `app/tools/legacy_tools.py`，按旧名称映射独立具名 async handler；Cacheable / non-cacheable 行为保留，天气批量失败信息只包含异常类型、不把异常正文发送模型。把 legacy argument alias normalizer 留在 spec 参数入口。`app/tools/agent.py` 的 Agent 现在只用 `TOOL_REGISTRY.dispatch`，删除 `_execute_native_tool_call` 名称分支及 per-name whitelist；schema compatibility facade 由 Registry 生成。公共 `run_agent_loop`、OpenAI Function Calling JSON、forced command 和外部搜索函数签名未变。`ToolContext.result_max_chars` 与 spec 上限保留历史输出 cap；SearchResult 展示 formatter 收敛到 `app/tools/contracts.py`，Evidence 同步进入引用列表。
- P1-02 测试：`tests/test_tool_compat.py` 的 Mock 覆盖八个网络 handler、knowledge callback、weather batch、缓存命中与绕过、weather query alias、multi_search / weather_batch scalar、safe exception mapping；`test_agent.py` 改为替换注册 spec handler，仍验证一 tool_call_id 一回复、parallel cancel、deadline 和 progress。Registry + Agent + compat 定向测试 28 项通过；全量 `.venv/bin/python -m unittest discover -s tests`：140 项通过；四个变更 Python / test 模块 py_compile 通过。无搜索服务、provider API 或公网访问。
- P1-02 独立复核：OpenAI schema canonical digest 仍为旧静态定义 SHA-256；没有新的按名称执行分支，未知 tool 被拒绝；默认群上下文和 tool-call 格式保持。工具 per-spec timeout 及 max-concurrency 在 Registry 中执行，实际超时、deadline 与 cancellation 路径覆盖；外部抓取不会绕过 P0-02。回滚通过恢复 P1-02 文件级 checkpoint / revert 本次文件集；未额外留下第二套分支或配置 feature switch，以免双份行为分叉。当前 Git 元数据只读且初始 docs 整体未跟踪，所以没有 commit。
- P1-03 启动证据：`BotService._parse_command()` 将 `/weather`、`/news`、`/web` 识别为 `BotCommand("tool",...)`；`handle_group_message` 把 forced command 送入 `_reply_with_llm`，群 member 不经过 admin-only `CommandHandler`。`LLMHandler` 调 `_make_forced_call`，再用 `_agent_execute` 和 `format_tool_observations` 把结果加进 model system message；命令本身不改变当前群 memory / bot 回复策略。新的 Policy 必须显式表示经 OneBot 事件验证的 `group_member` 角色并允许这 3 个既有只读网络 command，不能意外改成 admin-only 或提高权限。接下来还要核对 `Settings.is_admin`、`GroupMessageEvent` 源和 forced observation 格式后实施。
## 2026-10-12：P2-03 generation 生命周期完成与 P2-04 启动

- 生命周期范围：在既有 RagStore / VectorRagStore 边界上新增 additive generation tables；SQLite / FTS staging 不影响 active 数据，Chroma 按 document generation / vector revision 隔离。切换前校验 SQLite、FTS 和 Chroma 的精确 chunk ID；active generation 与 active vector revision 都由 SQLite pointer 决定。既有 `rag_chunks` / SFM PDF API 保留，P0-07 的原 collection 最小替换 / 删除职责未扩成此 generation 框架。
- 状态与恢复：关键 compare-and-swap 事务先 `BEGIN IMMEDIATE`，避免 deferred read 后多个进程依据陈旧 head 同时激活；重复删除创建 tombstone，禁止查询，并 durable claim `cleaning` 后才物理删除。cleanup claim 后 rollback / restage 不得复活；重复 tombstone 与迟到 failure 不会将 `cleaning` 降级。generation 与 vector revision cleanup、未完成 staging / shadow rebuild 可幂等恢复。shadow vector rebuild 成功后才更新 SQLite revision pointer；late worker error 不会把 complete/active revision排入删除。检索将 active SQLite chunk ID 集合与 Chroma 实际 ID 精确比较，same-count wrong-ID collection 也不返回 stale vector。
- CLI 与兼容：`scripts/index_sfm_pdf.py --dry-run` 支持导入解析预览和删除预览；均不初始化 RagStore / Chroma、不做索引写入。旧的无 generation SFM 路径仍保留。generation lifecycle 暴露替换、删除、重建、恢复、rollback、status；dry-run 对替换、删除、重建均不写状态。
- 测试 / 审查：新增 fake Chroma + Fake Embedder + 临时 SQLite 生命周期测试，覆盖替换、active-only、rollback、shadow 写失败保留指针、building/ready 中断恢复、tombstone / cleanup 重试 / reimport、generation/vector rebuild 幂等、双 RagStore stale activation / cleanup claim / 重复 tombstone / late failure、legacy source type 路由、same-count stale IDs、缺失非默认 vector revision fail-closed；CLI fixture 验证 preview / delete dry-run 没有 store 初始化。定向 `.venv/bin/python -m unittest tests.test_index_lifecycle tests.test_sfm_index_script tests.test_hybrid_retrieval tests.test_rag -q`：47 tests OK；全量 `.venv/bin/python -m unittest discover -s tests -q`：185 tests OK。全量需在授权的本地环境运行以绑定 loopback TCP/TLS SSRF fixtures；未访问公网、模型服务、真实 Chroma 或生产数据。Python compile 与 `git diff --check` 通过。
- 独立复核：reviewer 曾指出并协助定位 CAS 竞争、向量修建集合测试期望错误、rollback 默认 collection 写错、cleanup 与 rollback / re-stage / 重删竞争、重复删除后 stale activation、cleaning 状态降级、以及 vector rebuild 完成后的迟到失败误删风险；逐项增加状态保护和双 store / lifecycle fixtures。最终独立结论 APPROVED。
- 已知限制：SQLite pointer 与 Chroma 不在单一事务，cleanup 失败保留 pending 状态而非伪称跨存储原子；如果需要回滚的旧 generation Chroma revision 已丢失，当前 rollback fail closed，不错误写入默认 collection，需先通过受控重建恢复。现无 GTNH Wiki / 任务书 / recipe 样本；这些 Fixture 不证明 GTNH 检索 / 配方功能已可用。
- 文件检查点：改动 `app/rag.py`、`app/vector_rag.py`、`app/knowledge/documents.py`、`app/knowledge/retrieval.py`、新增 `app/knowledge/index_jobs.py`，更新 `scripts/index_sfm_pdf.py`，新增 `tests/test_index_lifecycle.py` 与 `tests/test_sfm_index_script.py`。无 commit（`.git` 元数据只读且不触碰其他已有工作区改动）。
- 下一任务：P2-04，只补充/收束人工文档 Fixture 验收，并运行 P2 阶段回归。P3-01 与 P4-01 仍是其后 READY；P1-04 deferred、P6 外部数据依赖不阻塞。

## 2026-10-12：P2-04 文档 Fixture 验收完成与 P3-01 启动

- P2-04 补充第二个人工 Markdown 文件 `tests/fixtures/documents/second_pack_fixture.md`，明确注明为 synthetic fixture。测试使用两套固定 `DocumentSource`（不同 pack、pack version、game version、revision），经真实 Markdown parser/chunker 和 `DocumentIndexLifecycle` 写入临时 SQLite / FTS 与 Fake Chroma；共享查询分别按 alpha、beta 精确过滤，检查章节、来源引用、FTS / vector ranks、RRF 稳定性和版本隔离。进一步覆盖 VersionMismatch、空 corpus DataUnavailable、NotIndexed、generation 更新后旧 FTS 文本不再命中及 tombstone 后不可查询。
- 首轮测试发现两处不可靠假设：一个 Markdown 文件可产生多 chunk，因此不能断言结果只有一行；向量检索会对当前 generation 返回相似候选，即使 FTS 没有词项命中，因此旧词验证改为确认旧文本不再返回且 FTS rank 缺失。独立 reviewer 又指出仅用 alpha 独有 query 不足以证明两个 pack 交叉隔离；改用两份 fixture 共用的 `synthetic EU tick` 分别查询两套 pack/version，reviewer 最终 APPROVED。
- 验证：`.venv/bin/python -m unittest tests.test_hybrid_retrieval -q`：15 项通过；P2 阶段回归 `.venv/bin/python -m unittest tests.test_rag tests.test_hybrid_retrieval tests.test_index_lifecycle tests.test_sfm_index_script -q`：50 项通过；目标测试 `py_compile` 与 `git diff --check` 通过。测试仅使用人工 Markdown、临时 SQLite、Fake Embedder / Chroma；没有真实 GTNH 文档，也不表示领域检索能力已验证。测试中可见的 ResourceWarning 来自既有 sqlite 测试资源生命周期，不影响断言结果。
- 本任务只改测试和人工 Fixture，无业务代码变化。文件级检查点；不 stage / commit，以保留既有工作区改动，且当前 `.git` 元数据只读。
- 下一任务 P3-01：在静态 repo_id -> 管理员配置 root 白名单上建立只读本地 Git 边界与 snapshot contract，所有测试使用临时仓库；不调用远程 GitHub、不执行仓库脚本。

## 2026-10-12：P3-02 本地仓库搜索与读取工具验收、P3-03 启动

- 实施：在既有 `LocalGitRepositoryService` 上增加 bounded path/text search、分页、固定 commit 的指定行读取和 `git://repo/commit/path#Lx-Ly` Evidence；递归 tree 输出、文件数、检查字节、单文件字节、行数、结果字节和总 deadline 均有上限。Git 调用经 `StorageExecutor` 有界 vector lane 执行。Registry 增加 `repository_search` / `repository_read`；既有 10 个 OpenAI Function Calling schema 的兼容指纹测试保持不变，最终 Registry 为 12 个工具。LLMHandler 只从静态配置创建 repo allowlist，回传引用。
- 审查修复：独立审查发现 Git 超时、启动失败或非零退出码原先会落为 `DATA_UNAVAILABLE`。新增 `RepositoryTimedOut` / `RepositoryExecutionError`；超时映射 `TIMED_OUT`，命令执行错误映射 `ERROR`，只有未配置仓库或无法解析提交的缺数据条件继续映射 `DATA_UNAVAILABLE`。限定 `rev-parse --verify --quiet` rc=1 才作为找不到 commit；其它失败保持执行错误，不以 stderr 内容做状态猜测。
- 测试：最新定向命令 `.venv/bin/python -m unittest tests.test_local_git tests.test_tool_registry tests.test_tool_compat tests.test_agent tests.test_hybrid_retrieval -q`：70 tests OK；覆盖本地临时 Git、固定 commit、路径和敏感文件策略、symlink / binary / 大文件、分页与部分扫描、引用定位、超时和命令失败映射、兼容 schema。目标模块 `py_compile` 和 `git diff --check` 通过。无 GitHub 网络、模型 API 或真实领域数据访问。
- 独立审查：第一轮 CHANGES_REQUESTED 仅针对超时 / 执行异常被误报为缺数据；补充分类接口和 Registry 行为测试后最终 APPROVED。非阻塞限制：Git 与 Chroma 当前共用单 worker vector lane，可能排队；根据实际并发 / 延迟观察后再评估独立 bounded lane。
- 文件检查点：新增 / 更新 `app/repositories/local_git.py`、`app/tools/agent.py`、`app/tools/legacy_tools.py`、`app/handlers/llm_handler.py`、`tests/test_local_git.py`、`tests/test_tool_registry.py`、`tests/test_tool_compat.py`；无 Git commit（既有未提交工作区保留且 `.git` 只读）。回滚可移除新增 repo tools/context extension 并恢复本地 Git 服务与测试文件；不会触碰用户仓库。
- 下一步：P3-03 增加 submodule / 超时退出与取消跟踪 / tree listing 截断直接测试，并执行 P3 阶段回归；仅在测试证实实现问题时做最小服务修复。

## 2026-10-12：P3-03 本地 Git 安全 Fixture 验收

- 测试扩展全部使用 `TemporaryDirectory` 创建的本地 Git repository；增加带空格 / Unicode / 方括号 / 前导短横线的路径检索和读取，gitlink submodule 不得作为可读树目录或普通文件暴露，递归 tree 输出 cap 通过 Registry 传播为 `PARTIAL`。
- 超时测试仅对 `_list_tree` 操作施加 0.1 秒期限，并以受控 `sys.executable` 子进程模拟挂起，确认 service deadline 后 kill + wait 并且 `poll()` 已收割；另验证 Registry 请求取消后同步 Git future 留在 StorageExecutor pending 集合中，释放 fixture Event 后完成并归零。测试 finally 会释放 Event、cancel / await 尚未完成请求，async teardown drain executor。
- 验证：`.venv/bin/python -m unittest tests.test_local_git -q`：27 tests OK；阶段回归 `.venv/bin/python -m unittest tests.test_local_git tests.test_tool_registry tests.test_tool_compat tests.test_agent tests.test_hybrid_retrieval tests.test_main tests.test_adapter -q`：独立 reviewer 复跑 96 tests OK。`py_compile` 与 `git diff --check` 通过。无网络、GitHub、模型服务或外部数据访问。
- 独立审查 APPROVED。第一轮指出 timeout fixture 初始化仓库也用过短 command timeout、取消等待失败路径可能遗留 asyncio task，以及可补 Registry 的 PARTIAL 断言；三项已按要求修正后最终通过。
- 文件检查点：仅更新 `tests/test_local_git.py`；不改现有业务服务，不接触管理员配置的真实仓库，不 stage / commit。P3 阶段任务全部验收，转入已就绪的 P4-01。

## 2026-10-12：P4-01 Recipe Schema / Repository 初稿与样本可行性检查

- 代码初稿：新增 `app/recipes/models.py` 的 `PackVersionKey`、dataset/source metadata、source locator、原始 byte payload + SHA-256、unknown / critical unknown fields、Ingredient/Output 和 Recipe status。Ingredient 将 item/fluid exact、tag、Ore Dictionary、predicate、source extension 分开；item/fluid exact 才接受 canonical namespaced ID。constraints 保存 NBT / component / predicate 表达，但只要约束、consumption 或解释不足就不能标 exact；未知 unit、unsupported 条件或 critical unknown field 会把整条 Recipe quarantine。
- Repository：新增 recipe-own additive SQLite tables、schema version marker、exact pack/version active pointer、源记录 / hash / locator、machine 多对多行、输入 / 输出多行与 exact 查找索引；Decimal 以 TEXT round-trip。dataset 切换使用 `BEGIN IMMEDIATE`，只允许明确版本 key 查询；未知 pack 返回 DATA_UNAVAILABLE，已知同 pack 其他版本返回 VERSION_MISMATCH，active empty dataset 返回 EMPTY。Bot 现有 MemoryStore / DB 公共接口未改，repository 尚未接入运行路径。
- Fixture 测试：人工 synthetic payload（字节正文明确标记不是 GTNH 数据）覆盖 item/fluid、tag、Ore Dictionary、predicate、NBT/component、tool/catalyst `consumed=false`、source extension 未知字段、critical condition quarantine、未知 unit、安全 exact ID、foreign keys、Decimal/probability 精确保存、source bytes/hash/JSON pointer 回读、pack/version 隔离、空库和已有旧表 additive 启动、quarantined dataset 禁止激活。`.venv/bin/python -m unittest tests.test_recipe_repository tests.test_memory tests.test_rag -q`：29 tests OK；目标模块 `py_compile` 和 `git diff --check` 通过。全部数据库都在临时目录。
- 可选真实样本可行性检查：只在当前仓库的文件路径中查找 GTNH / Minecraft recipe / JSON/CSV/TSV 候选，结果只有本任务新建 Recipe 模块和测试；没有维护者提供的真实样本。根据范围没有访问网络、下载数据、运行 gtnh-toolkit 或导出器。故 Schema 尚未获得 GTNH 真实样本验证，检查结果 BLOCKED_EXTERNAL_DATA，但不阻塞 P4-02 至 P4-04。
- 当前状态：实现完成待独立审查；P4-01 还不能在人工 Fixture 通过之外声称真实 GTNH Recipe Schema 已验证。Schema / unit 字段约定需要后续维护者审阅；待审查员复核后进入 P4-02。

## 2026-10-12：P4-01 独立复核批准，进入 P4-02

- 首轮复核提出的三个问题均已在 repository/model 边界修复：raw source record 的 `critical_unknown_fields` 在写入时合并进 Recipe 并强制 quarantine；`find_consuming_exact()` 仅匹配 `consumed = 1`；JSON 字段采用深拷贝，且 `add_recipe()` 在持久化前重建并重新校验子模型和 Recipe 状态。
- 新增回归覆盖：遗漏传递的 critical unknown 不会进入精确查询；non-consumable TOOL / CATALYST 不会被报作消耗输入；构造后添加 conditions 会重新推导为 CONDITIONAL，添加精确输入 constraints 会被拒绝。
- 独立复核 APPROVED。reviewer 独立运行 `.venv/bin/python -m unittest tests.test_recipe_repository tests.test_memory tests.test_rag -q`：32 tests OK；目标 `py_compile` 与 `git diff --check` 通过。所有 Fixture 仍明确为 synthetic / not GTNH data。
- P4-01 状态转为 COMPLETED；P4-02 进入 IN_PROGRESS。未发现真实 GTNH 样本，也未假定 / 运行 gtnh-toolkit 或 exporter；该项真实 Schema 验证继续待外部样本。

## 2026-10-12：P4-03 Recipe 查询与工具验收

- 实施：增加 `RecipeQueryService`、`recipe_lookup` 与 `recipe_machine_lookup`，要求调用者提供 pack、pack version、game version；按 active dataset 精确查询 item/fluid producer/consumer 与 machine。查询结果保持 recipe 状态、输入输出、流体、概率、非消耗性工具、EU/t、duration、conditions 和来源 Evidence。未知 pack / 版本不做 fallback；缺少已索引数据与查询无结果分别返回不同状态。Agent schema 保留旧工具 schema 指纹。
- 安全与并发复核：首次独立审查指出结果读取没有 SQL 分页上限、序列化会对每条来源 locator 做同步 SQLite 读取，且 active pointer 查询可能与后续 recipe/metadata 读取跨 revision。修复后 `RecipeRepository.query_active_exact()` / `query_active_machine()` 限制 limit 1–20、offset 0–100000，并在同一只读 SQLite 事务快照读取 active key、dataset、quarantine count、recipes 和 locators；locators 批量取回并随 query DTO 返回。工具序列化不再逐条访问 repository。Repository 扩展是避免无界结果、事件循环阻塞及跨 revision 混合结果所必需的 P4-03 范围调整。
- 测试：`.venv/bin/python -m unittest tests.test_recipe_query tests.test_recipe_repository tests.test_recipe_importer tests.test_tool_registry tests.test_tool_compat tests.test_agent -q`：69 tests OK。覆盖 exact / conditional、quarantine 排除、复杂 Ingredient 不冒充精确匹配、版本隔离、缺数据、SQL page 上限、active snapshot 与 Evidence。`py_compile`、`git diff --check` 通过。测试输出包含 tool failure fixture 的预期日志以及若干 SQLite `ResourceWarning`；审查未确认这些警告由本任务引入，需在后续测试资源生命周期检查时留意。
- 独立复核：最终 APPROVED；reviewer 独立复跑上述 69 项，通过 `py_compile` 与 `git diff --check`。人工 Recipe 数据均为 synthetic fixture，不代表真实 GTNH 数据或真实配方能力已验证。
- 检查点：更新 `app/recipes/query.py`、`app/recipes/repository.py`、`app/tools/agent.py`、`app/tools/legacy_tools.py`、`app/handlers/llm_handler.py`、`tests/test_recipe_query.py`、`tests/test_tool_registry.py`、`tests/test_tool_compat.py`。不创建 commit，以保留初始未提交工作区并遵守 `.git` 只读限制。
- 状态与下一步：P4-03 COMPLETED；P4-04 READY。P4-04 只补充人工 Fixture 和测试，重点验证 synthetic raw records 从 import 到 query 的边界；真实 GTNH 数据仍独立缺失，不阻塞此任务。

## 2026-10-12：P4-04 合成 Recipe Fixture 验收

- 新增 `tests/fixtures/recipes/synthetic_recipe_set.json`，其顶层声明明确说明这是人工合成契约 Fixture，不是 GTNH、Minecraft 整合包或 exporter 数据。测试专用 `_SyntheticJsonRecipeSource` 将其转成现有 `RecipeSource` contract，经 `RecipeImporter` 导入临时 SQLite；没有新增生产 parser 或 exporter。
- 端到端验证覆盖同一物品的多生产配方、多 item 输入、item + fluid 混合输入、概率 item/fluid 输出、非消耗 tool、v1/v2 不同产量与机器、精确 / 条件匹配、tag / Ore Dictionary / predicate / NBT / component 输入、source extension quarantine、单独的 critical unknown quarantine、non-critical unknown 字段保留、原始 JSON record 与 locator、missing item、missing pack、version mismatch。查询均断言状态、key / dataset、匹配质量和来源位置；quarantine recipe 不进入正常 producer/machine query。
- 初测因测试读取 quarantine 行时没有传 `include_quarantined=True` 而失败；检查 repository contract 后只修正测试读取方式，没有放宽业务过滤。最终 `.venv/bin/python -m unittest tests.test_recipe_query tests.test_recipe_importer -q`：21 tests OK；P4 阶段回归 `.venv/bin/python -m unittest tests.test_recipe_repository tests.test_recipe_importer tests.test_recipe_query tests.test_tool_registry tests.test_tool_compat tests.test_agent tests.test_memory tests.test_rag -q`：87 tests OK。JSON parse、`py_compile`、`git diff --check` 通过。
- 独立复核 APPROVED：reviewer 独立运行 21 项定向测试，确认没有将 synthetic Fixture 测试表述为真实 GTNH 配方验证，未发现阻断缺口。
- 检查点：只新增人工 JSON Fixture 并更新 `tests/test_recipe_query.py`；数据库均为 `TemporaryDirectory` 下的 SQLite。无生产业务改动、数据导入、外网访问或 commit。
- 状态与下一步：P4-04 COMPLETED，P4 阶段验收通过，仅限 contract / Fixture 能力。P5-01 READY；当前未导入真实 SFM 上游文档或示例，Parser 仍未验证。

## 2026-10-13：P5-01 SFM 知识检索实施中

- 源码核对：现有 `LLMHandler._search_local_knowledge()` 仅查询配置的旧 SFM PDF document ID；通用 `DocumentKnowledgeService` 已支持对 `source_type`、`corpus_id`、`pack_id`、`pack_version` 和 `game_version` 做 FTS / Chroma 同一精确过滤。Tool Registry 要求每个公开 schema 在 `legacy_tools._HANDLERS` 有匹配 handler，因此新增 `sfm_examples` 必须改 `app/tools/legacy_tools.py`，而非只追加 `app/tools/agent.py` schema。计划内允许文件漏列该必要 adapter，属于为 Registry 可执行闭环所需的任务范围扩展；同步补充 Registry schema / compatibility tests。
- 当前改动：新增 `SFMSampleSourceManifest` 和 `SFMKnowledgeService`，将 SFM release 明确映射到通用 `pack_version`，Minecraft release 映射到 `game_version`；查询固定 `source_type=sfm`、`corpus_id=sfm-examples` 与这两个精确版本，不伪造 Minecraft pack ID，也不允许跨版本 fallback。新增只读、受限的 `sfm_examples` OpenAI Function Calling tool，并由 LLMHandler 的标准 Agent ToolContext 注入。无 manifest 数据时，在已配置的 RagStore 下返回 NOT_INDEXED；RAG 未配置时返回 DATA_UNAVAILABLE。
- 新增 synthetic Markdown Fixture 和 SQLite FTS + DocumentKnowledgeService / Tool Registry 测试，覆盖版本过滤、Evidence locator、未索引 / 空索引、版本不匹配、缺版本与有界结果。Fixture 明确是人工占位文字，不引用或宣称官方 SFM grammar，且提示未通过真实 Parser。首轮定向 37 tests 通过；仍需验证 LLMHandler 实际 tool context 注入，并进行回归和独立审查后验收。

### P5-01 运行路径测试与回归

- 增加 `LLMHandler._reply_with_llm()` Fake Agent Loop 测试，核对 LLMHandler 创建的 `SFMKnowledgeService` 与同一个 `DocumentKnowledgeService` 关联，并实际注入 native Agent `tool_extensions`。该用例只使用临时 SQLite 和 Fake LLM，不执行模型 API。
- 最新 P5-01 回归：`.venv/bin/python -m unittest tests.test_sfm_knowledge tests.test_hybrid_retrieval tests.test_tool_registry tests.test_tool_compat tests.test_agent tests.test_main -q`：58 tests OK；独立 SFM 模块 4 tests OK；相关模块 `py_compile` 与 `git diff --check` 通过。输出中的 RuntimeError 日志来自测试注入的错误路径，FastAPI 产生既有 `on_event` deprecation warnings；均未导致失败。
- 独立审查已委派，任务状态保持 IN_PROGRESS，等待结论后再验收；当前没有真实 SFM 内容、官方样例、上游代码、Parser 或语法验证。
- 审查前的语义精简：移除 manifest 的 `pack_id="sfm"`，因为 SFM 本身不是 Minecraft 整合包；新增断言确保检索结果的 `pack_id` 不会误报为 `sfm`。更新后 `.venv/bin/python -m unittest tests.test_sfm_knowledge -q`：4 tests OK，目标模块 `py_compile` 与 `git diff --check` 通过。

## 2026-10-13：P5-01 独立审查通过；P5-02 开始

- P5-01 独立复核 APPROVED；reviewer 对最新代码独立运行 `.venv/bin/python -m unittest tests.test_sfm_knowledge tests.test_hybrid_retrieval tests.test_tool_registry tests.test_tool_compat tests.test_agent tests.test_main -q`：58 tests OK。无需修改。
- P5-01 转为 COMPLETED。审查边界仅为人工 SFM 检索 Fixture 与应用接线；未获取官方 SFM 文档 / 源码，没有 SFM Parser，也未声称任何真实 SFM 语法验证。
- 当前任务 P5-02：实现版本化 Validator contract、结构化诊断、安全默认 UNAVAILABLE 与显式标记的 Fake Validator。Fake 仅供单测注入，其“模拟有效”结果不得表示真实 VALID。计划不添加或假设真实 Parser，也不添加 SFM 执行能力。
- P5-02 依赖 P1-03 与 P5-01，均已完成。单测预计使用 Fake Validator、asyncio 受控等待器；没有外网、模型 API 或真实领域数据依赖。

## 2026-10-13：P5-02 Validator 合约与 Fake 验收

- 新增 `SFMTargetVersion`、结构化 `ValidatorDiagnostic`、`ValidationResult` 和 `ValidationStatus`，版本对始终随请求及结果绑定。真实状态 `VALID` / `INVALID` 与 Fake 专用的 `SIMULATED_*` 状态分开；结果序列化提供 `is_simulated` / `is_real_validation`，`validator_id="fake"` 不可构造出真实 VALID / INVALID。
- 新增异步 `SFMValidator` Protocol 与 `SFMValidationService`。无注入 Parser 时使用 `UnavailableSFMValidator`，每次都返回 UNAVAILABLE；源码上限 64 KiB UTF-8、timeout 上限 30 秒、默认 5 秒。timeout 返回 TIMEOUT；请求取消继续传播；适配器异常 / 错误版本或 ID 响应变为通用 ERROR，不将异常正文和源码回显。Diagnostics 限制 256 条并只接纳 tuple/list，先检查长度再冻结，避免无限 iterable 消耗。
- 新增可显式配置样本响应的 `FakeSFMValidator`，仅命中人工固定样本或固定 unsupported target；不解析 SFM grammar，也不导入生产请求路径。
- 定向测试 `.venv/bin/python -m unittest tests.test_sfm_validator -v`：8 tests OK。覆盖默认 UNAVAILABLE、模拟有效 / 错误 / 版本不支持标签、诊断行列序列化、真实状态拒绝、源码 / timeout / surrogate 边界、bounded diagnostics、timeout 引发适配器取消、caller cancellation、adapter mismatch 与 exception 信息脱敏。
- P5 阶段回归 `.venv/bin/python -m unittest tests.test_sfm_validator tests.test_sfm_knowledge tests.test_hybrid_retrieval tests.test_tool_registry tests.test_tool_compat tests.test_agent tests.test_main -q`：66 tests OK；`py_compile` 与 `git diff --check` 通过。日志中的 RuntimeError 是错误路径 fixture 的预期输出，FastAPI `on_event` deprecation warnings 为既有行为。
- 独立复核 APPROVED；reviewer 独立运行定向测试 8 tests OK。复核建议将诊断输入改成先检查长度的 tuple/list；已实现并由测试覆盖后再次获批准。
- 风险限制：`asyncio.timeout` 不能强制终止忽略取消的协程，更不能让事件循环上的同步 CPU 解析及时中止。未来真实 Parser adapter 必须放在可取消 async I/O、受限子进程或 worker；其具体实现待 Parser 版本与执行边界验证。当前无真实 Parser、无语法验证。
- 状态：P5-02 COMPLETED；P5-03 READY 并转 IN_PROGRESS。无依赖变更、外网、数据库改动或 commit。

## 2026-10-13：P5-03 有界生成 / 验证 / 修复验收

- 新增 `SFMGenerationWorkflow`，沿用现有 Agent Loop 内的模型客户端与版本过滤知识服务。默认无真实 Parser 时在检索 / 模型调用前返回 UNAVAILABLE；生成源码不执行，完整源码超出返回预算时显式省略，不发送被截断程序给 Validator。
- 生成和最多两轮修复共享 `ToolContext` 的绝对 deadline；每个逻辑模型调用请求 768 completion tokens、总计最多 3 次。DeepSeekClient 可能对空回复追加一次更大 `max_tokens` 请求，因此代码记录逻辑 `chat()` 尝试次数，不谎报 provider 实际调用 / token usage；P0-06 在每个物理 HTTP dispatch 前执行预算预留。
- 注册 `sfm_generate_validate` 原生 Function Calling tool，适配现有 Tool Registry 与 LLMHandler。该工具独占一个 Agent tool round，避免其它并发结果挤掉完整源码；被延后工具保留各自的 tool_call_id 响应，且可在下一轮重试。并发上限设为 2，兼容注册机制的全局限流。
- 结构化结果保留最多 8 个有界 Evidence。Agent 最终引用优先附上 SFM 来源，既有其它工具的引用不会挤掉该次检索来源。增加多来源 DTO 和既有引用占位测试。
- 定向 `.venv/bin/python -m unittest tests.test_sfm_workflow tests.test_sfm_validator tests.test_sfm_knowledge tests.test_tool_registry tests.test_tool_compat tests.test_agent -q`：60 tests OK。P5 阶段回归 `.venv/bin/python -m unittest tests.test_sfm_validator tests.test_sfm_workflow tests.test_sfm_knowledge tests.test_hybrid_retrieval tests.test_tool_registry tests.test_tool_compat tests.test_agent tests.test_main -q`：80 tests OK；`py_compile` 与 `git diff --check` 通过。日志 RuntimeError 来自预期错误路径 fixture；FastAPI `on_event` deprecation warning 为既有。
- 独立复核 APPROVED，reviewer 独立运行同组回归 80 tests OK。审查要求补齐多来源证据序列化及引用优先级；已补代码与测试后通过。
- 验收边界：仅 Fake LLM / Fake Validator / 人工文档 Fixture；没有真实 SFM Parser、官方 SFM 数据或真实语法验证。对忽略取消的未来同步 Parser，async timeout 无法强杀；需要隔离执行器或子进程。
- 检查点：`app/sfm/workflow.py`、`app/sfm/validator.py`、`app/sfm/__init__.py`、`app/tools/agent.py`、`app/tools/legacy_tools.py`、`app/handlers/llm_handler.py`、`tests/test_sfm_workflow.py`、`tests/test_sfm_knowledge.py`、`tests/test_tool_registry.py`、`tests/test_tool_compat.py`。无 commit（工作区已有改动，`.git` 只读）。P5-03 COMPLETED；开始 P5-04 合成 `.sfm` 流程 transcript 验收。

## 2026-10-13：P5-04 Fake SFM 流程 Fixture 验收

- 新增 `synthetic_program.sfm` 和 `synthetic_invalid_program.sfm` 两份人工文本，文件正文明确说明不是 SFM 程序、官方示例或可执行源码。Fake Validator 只按完整字符串精确匹配这两份样本，不解析任何 grammar。
- `tests.test_sfm_workflow` 新增 transcript 检查：无效 synthetic sample 的固定 Fake 诊断触发一次有界修复；结构化状态为 `simulated_valid`，`is_real_validation=false`。另测 `max_repairs=0` 时返回 `simulated_invalid` 和结构化诊断，不冒充 Parser 结果。
- 定向 `.venv/bin/python -m unittest tests.test_sfm_workflow -v`：16 tests OK。P5 阶段回归 `.venv/bin/python -m unittest tests.test_sfm_validator tests.test_sfm_workflow tests.test_sfm_knowledge tests.test_hybrid_retrieval tests.test_tool_registry tests.test_tool_compat tests.test_agent tests.test_main -q`：82 tests OK；`py_compile` 与 `git diff --check` 通过。
- 独立审查 APPROVED，reviewer 独立运行 82 tests。P5 阶段只完成 Fake / Fixture 工作流与版本约束；真实 SFM Parser、官方数据和语法能力均未验证。
- 状态：P5-04 COMPLETED；P7-01 最终旧 Bot 兼容回归开始，P7-02 已就绪。无依赖、业务代码、数据库或运行环境修改；无外网或模型 API 请求；无 commit。

## 2026-10-13：P7-01 最终兼容回归开始

- P0–P5 的每任务与阶段回归已按进度记录完成。现在执行 P7-01 全量 unittest / Mock 兼容 sweep，覆盖 OneBot adapter、命令 / memory、旧工具名称与 schema、LLM 和消息处理。没有生产 NapCat / 模型请求。
- 全量 `.venv/bin/python -m unittest discover -s tests -q` 首次运行在 sandbox 中为 277/279；仅 P0-02 两项受控 localhost TCP/TLS connector 无法 bind `127.0.0.1`（`PermissionError`），不是断言 / 实现失败。得到本地 socket 执行权限后，同一完整命令 279 tests OK。期间出现现有/预期的 error-path logger、FastAPI / regex DeprecationWarning 与 SQLite ResourceWarning；无失败用例。
- 当前交由独立 reviewer 检查 P7-01 覆盖和结果；该任务没有修改应用代码。P7-02 按其显式依赖 P0–P5 已就绪，可与 P7-01 审查独立推进。

## 2026-10-13：P7-02 跨域 Fixture 集成测试实施中

- 新增 `tests/test_agent_integration.py`，使用临时文件数据库、已有 synthetic SFM Markdown / Recipe JSON 与 Fake LLM / Fake Validator，经自研 Agent Loop / Tool Registry 调用 SFM 文档和 Recipe 工具；同时请求正确与不匹配版本，检查 Citation Evidence、Recipe 数据集版本、模拟验证状态和逐个 `tool_call_id` 的响应闭环。
- 补充空文档 corpus 与空配方库时经 Agent Tool Registry 返回显式 `NOT_INDEXED` / `DATA_UNAVAILABLE`；并发 scenario 使用临时 SQLite + StorageExecutor + MessageTaskRuntime，验证多个 group 消息可执行本地知识与 Recipe 查询且并发不超过 worker 数。
- 首轮 3 项集成测试出现两个测试编排错误：断言期望的旧 locator scheme 与 fixture 中的 URI 不符，且将 3 个消息提交到总准入只有 2 的 runtime（2 workers + queue capacity 2），按实际契约修正为 fixture URI 和 queue capacity 3。当前定向 3 tests 通过；还需 P7-02 回归、审查和完整集成覆盖检查。无业务代码 / 生产 DB 修改，无外部网络或模型调用。
- P7-02 定向回归 `.venv/bin/python -m unittest tests.test_agent_integration tests.test_sfm_knowledge tests.test_hybrid_retrieval tests.test_recipe_query tests.test_recipe_importer tests.test_recipe_repository tests.test_sfm_workflow tests.test_task_runtime tests.test_tool_registry tests.test_tool_compat -q`：102 tests OK；测试构造的 `RuntimeError` 仅来自预期失败路径 Fixture；SQLite `ResourceWarning` 来自现有测试 teardown / traceback 引用，测试结果仍通过，未因本次集成测试新增持久连接。

## 2026-10-13：P7-02 独立复核通过；P7-03 能力声明核对

- 独立复核最初指出版本结果与空数据状态的聚合断言可能互相掩盖。测试改为按 `tool_call_id` 分别核对 SFM 正确版本 / 错配、Recipe v1 / v2 专属配方 ID 与 EU/t、Recipe 缺失版本，以及 SFM / Recipe 空数据的不同状态。无业务代码变更。
- 修订后 P7-02 定向 `.venv/bin/python -m unittest tests.test_agent_integration tests.test_sfm_knowledge tests.test_hybrid_retrieval tests.test_recipe_query tests.test_recipe_importer tests.test_recipe_repository tests.test_sfm_workflow tests.test_task_runtime tests.test_tool_registry tests.test_tool_compat -q`：102 tests OK；全量 `.venv/bin/python -m unittest discover -s tests -q`：282 tests OK；`py_compile` 与 `git diff --check` 通过。全量测试的受控 localhost TCP/TLS 用例在 sandbox 中不能 bind loopback；在获准的本地测试权限下同命令通过。
- P7-02 最终独立复核 APPROVED，reviewer 独立复跑 102 tests 并确认按工具 ID 的版本与降级断言、临时 SQLite / StorageExecutor 清理及合成数据声明。P7-02 完成，进入 P7-03。
- P7-03 核查了仓库内的数据 Fixture、服务降级状态与 Fake Validator 输出，并生成 Fixture SHA-256 清单。P7-02 为 Fake / Fixture / 本地组件集成；当前未发现 / 提供 GTNH Wiki、任务书、真实配方导出样本或真实 SFM Parser / 样例。未验证 gtnh-toolkit；没有调用外部网络、模型 API 或 Parser。
- P7-03 仅在 `docs/DATA_VALIDATION_REPORT.md` 记录当前能力级别、证据、精确用户可见降级状态、Fixture 哈希与 P6 缺口。完成它不表示 P6 真实数据能力已交付；后续需在获得有许可的具体数据 / Parser 后重新执行真实验收。
- 报告独立审查指出 SFM 无 Parser 状态有外层 Agent Tool 与内层 workflow 两层：适配器映射到 `ToolResult.status=DATA_UNAVAILABLE`，`content.status=unavailable`。报告已澄清，并新增 Registry 级测试验证这两个状态、无模型调用 / 检索和 `is_real_validation=false`。
- 加强 P7-02 错配断言：SFM / Recipe 两个 `VERSION_MISMATCH` 结果现在也分别核对工具专属消息。`tests.test_sfm_workflow`：17 tests OK；P7 集成 / 状态定向回归：103 tests OK；全量 `.venv/bin/python -m unittest discover -s tests -q`：283 tests OK；`py_compile` 与 `git diff --check` 通过。最新报告和测试正在最终独立复核。
- 最终独立复核 APPROVED：reviewer 独立复跑 103 项定向回归；确认 report 记录 103 / 283 的最新计数，SFM Tool 外层与 workflow 内层状态、Recipe/SFM 错配消息、Fixture hash 和能力边界均符合源码；没有把人工样本或 Fake 结果称为真实验证。
- P7-03 COMPLETED。33 项总计：28 COMPLETED、4 BLOCKED_EXTERNAL_DATA（P6-01 至 P6-04）、1 DEFERRED（可选 P1-04），0 FAILED / PENDING / IN_PROGRESS / REVIEW。当前所有不依赖缺失真实数据或人工选择的任务均已完成。生产部署、真实 NapCat、真实模型 Provider、GTNH 真实数据、配方 exporter 和 SFM Parser 均未验证或执行。
