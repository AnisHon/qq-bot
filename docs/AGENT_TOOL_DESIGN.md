# Agent Tool Registry 设计

状态：接口设计稿；当前实现仍是 app/tools/agent.py 中的静态 schema、名称集合和 if / elif 执行器。

当前没有 GTNH Wiki / 任务书数据、配方数据集或验证过的 Recipe Schema；gtnh-toolkit 可用性未核实。P4 的合成 Recipe Fixture 只验证合同。当前也没有 SFM Parser / Compiler，Fake Validator 不代表真实语法验证。

## 1. 设计目标

在保留 app/llm.py 的 OpenAI 兼容 Function Calling 和 app/tools/agent.py 的自研 Agent Loop 前提下，建立单一工具注册与执行边界。新增工具只需注册一次，不再同时改 schema 列表、白名单、分发器、工具进度和结果来源逻辑。

兼容条件：

- 现有工具名称与参数结构保持不变：knowledge_search(query)、web_search(query)、news_search(query)、multi_search(queries)、github_repo_search(query)、github_repo_inspect(repository, query)、github_read_file(repository, path)、fetch_url(url)、weather(location)、weather_batch(locations)。
- 保持现有 run_agent_loop 入口和群内 /weather、/news、/web 命令的用户行为；内部先通过适配器过渡。
- Function Calling 请求仍输出当前 OpenAI 风格 tools JSON Schema；未知工具仍给模型一个完成该 tool_call_id 的工具观察值。
- 新领域功能通过同一执行器注册，但每个领域服务继续负责本领域规则和存储。

## 2. 接口模型

以下为职责草图，不是要求逐字采用的 Python API。优先使用标准库数据类 / Enum 和现有类型，不为抽象本身增加依赖。

~~~python
ToolSpec(
    name: str,
    description: str,
    parameters: dict,                 # JSON Schema，供模型和测试读取
    handler: AsyncToolHandler,
    argument_validator: Callable,     # 运行时严格校验和规范化
    policy: ToolPolicy,
    timeout_seconds: float,
    max_concurrency: int,
    max_result_chars: int,
    output_kind: str,                 # text / evidence / structured
    data_requirements: tuple[str, ...] = (),
)

ToolContext(
    request_id: str,
    session_id: str | None,          # optional; legacy conversation scope is group
    group_id: str,
    user_id: str,
    deadline_monotonic: float,
    cancel_scope: CancelScope,
    policy_snapshot: PolicySnapshot,
    budget_scope: BudgetScope,       # independent of session_id
    budget_controller: BudgetController,
    evidence_limit: int,
)

ToolResult(
    status: ToolStatus,
    content: str | dict,
    evidence: tuple[Evidence, ...] = (),
    usage: ToolUsage = ToolUsage(),
    error: ToolError | None = None,
)

ToolUsage(
    tool_call_count: int = 0,
    model_request_count: int = 0,
    input_tokens_reported: int | None = None,
    output_tokens_reported: int | None = None,
    tokens_reserved_estimate: int | None = None,
    estimate_method: str | None = None,
    cost_minor_units: int | None = None,
    cost_source: str = "unknown",   # provider | rate_card | unknown
    cost_status: str = "unknown",   # exact | estimated | unknown
    usage_status: str = "unknown",  # reported | estimated | unknown
)

BudgetReservation(
    reservation_id: str,
    request_id: str,
    scope_keys: tuple[str, ...],  # global + group + optional user; independent of session
    reserved_model_requests: int,
    reserved_tokens_estimate: int | None,
    reserved_cost_minor_units: int | None,
    status: str,                  # reserved | settled | released | unknown_settled
    lease_expires_at: int,
)

Evidence(
    kind: str,                        # web / document / recipe / repository / sfm
    source_id: str,
    title: str,
    locator: str,                     # URL、路径#行范围、文档章节 / 页码或 recipe ID
    revision: str | None,
    pack_id: str | None,
    pack_version: str | None,
    confidence: str | None,
)
~~~

建议的 ToolStatus：OK、PARTIAL、NO_RESULTS、DATA_UNAVAILABLE、NOT_INDEXED、VERSION_MISMATCH、INVALID_ARGUMENT、DENIED、TIMED_OUT、CANCELLED、ERROR。它们进入模型的文本表示时应附可读但稳定的状态，不通过空列表或“未找到”掩盖数据缺失。

ToolError 是可安全展示的结构化错误，至少含 code、retryable、message 和可选 field。异常细节写日志时删去凭证、Authorization、URL 中的 userinfo / query secret 和用户文件正文。不要把 Python traceback 原样发给模型或群。

ToolPolicy 用声明式数据描述权限和资源限制：

- allowed_roles / allowed_groups / allowed_users：默认拒绝未允许的敏感工具。
- access: READ_ONLY、NETWORK_READ、LOCAL_READ、VALIDATION_PROCESS；当前不设游戏执行或任意写权限。
- network: scheme、host / port allowlist、redirect limit 和公网目的地策略。目标 URL 必须经过 SafeFetchTransport；仅解析 DNS 并检查后再让标准 HTTPX transport 自行解析不构成防护。
- filesystem: 固定 root allowlist、相对路径、拒绝符号链接逃逸、文件类型与大小上限。
- rate: 每请求调用上限、全局 / 分类 semaphore、用户 / 群时间窗预算。
- result: 单结果 / 累计字节限制、引用数、截断标记。
- data: 所需 pack/version / index state；缺失时返回明确 DataUnavailable / NotIndexed。

### 2.1 预算预留与结算

预算挂在实际 HTTP dispatch 边界；ToolContext 提供预算身份和 controller。用户级预算可用 user_id 单独计量，不要求先启用用户级 SessionKey。保留 DeepSeekClient.chat / chat_with_tools / chat_stream 的既有公开调用签名，由 BudgetedModelInvoker 设置 request-scoped budget context，并让 DeepSeekClient 内部统一的 POST / stream dispatch helper 在每一次实际网络派发前 reserve、完成后 settle。不能只在外部方法入口预留一次：当前 chat() 对空回复存在内部重试，每次 POST 都必须独立 reservation。

建议 controller 契约：

~~~python
reserve_budget(scope, request_id, model, input_estimate, max_output_tokens,
               request_limit=1, cost_limit_minor_units=None) -> BudgetReservation
settle_budget(reservation, usage: ToolUsage, outcome) -> None
release_budget(reservation, reason) -> None
recover_expired_reservations(now) -> RecoveryReport
~~~

规则：

- 每次真实 provider HTTP 调用前 reserve。SQLite 用 BEGIN IMMEDIATE（或等价串行写事务）同时校验并写入 global、group、user 各 scope 的 reservation / 计数，避免并发请求同时获准而超额。请求次数、token 和费用是独立预算维度。
- 请求次数上限是本地可强制的硬准入限制；每次网络派发都计一次，即使 provider 后续报错，因为对端可能已接收或计费。Token reservation 根据可知 prompt 大小、配置的估算器和 max completion 做预留；除非服务方提供可强制的输入上限，它只是保守准入估算，不保证精确 token 用量。Agent 的工具调用次数上限不等同模型 HTTP 请求次数上限。
- 有 provider-enforced max_tokens 时同时设置每次 completion 上限；本地可确定的 prompt token 也纳入 reservation。若 provider 不支持可执行的 token hard cap，预留 token 是估算；结算若发现实际 usage 超过预留，应记实际 usage 并阻止后续调用，而不能声称此前请求绝未越额。货币费用同理，无法在发送前准确计价时不能称为硬费用上限。
- 成功响应含可信 usage 时按 provider 的 input / output usage settle，并释放多余预留；费用只在可信费率表可用时结算。usage 缺失则 input / output 保持 null，usage_status=unknown；不得虚构精确 token。为避免绕过限制，保守地将本次预留量作为预算占用，标记为估算 / 未知，而不是实际消耗。
- 预算拒绝、请求构造失败，或有 transport 证据确认网络请求尚未派发时，可 release。派发后超时、连接中断、HTTP 错误或取消时，provider 是否已处理未知，不得全额释放；保留本次 reservation 的保守额度并标记 usage / outcome unknown。
- Streaming 在打开 stream 前预留；若最后的 SSE usage 可用则 settle，否则取消、超时或缺少 usage 均按派发后未知策略。Agent 工具循环中的每次模型请求、最终回答和重试都各自预留、结算，不能把整轮当成一次请求。
- 每个 reservation 有唯一 ID、状态和 lease，settle / release 幂等。进程异常后恢复扫描发现过期 reserved 记录时，不直接删除或释放；转成 unknown_settled 并暂时保留完整预留占额。只有取得 provider 对账或可审计的未派发证据时，才修正并释放差额。
- lease 应长于请求 hard deadline 和 shutdown grace；长 stream 仅在仍活跃时续租，恢复过程不得把正常慢请求误判为进程遗留。
- 费用控制只有在 rate card 来源、版本、生效区间和 usage 单位可审计时才启用；否则 cost 保持 null / unknown。若调用方要求严格货币预算而费率不可用，应拒绝该预算模式或明确要求维护者选择“仅请求 / token 配额、费用未知”的策略，不得静默当作已实施金额上限。估算 token 或费用可作为保守准入和告警，不能宣称实际账单精度或精确费用硬上限。

当前 app/llm.py 中 ChatWithToolsResponse 没有 usage 字段，chat() 返回纯文本，chat_stream() 只 yield 文本 delta。P0-06 可增加带 optional metadata 的内部响应适配器和 POST / stream dispatch helper，但必须保留公开旧签名；chat() 内部每次 retry 也要重复走预算 gate。在 provider 流 usage 协议未验证前，不能声称 streaming token 结算准确。

注册表负责 schema、handler、校验器和策略，不把业务查询塞进 Registry：

~~~python
registry.register(spec)
registry.openai_schemas(policy_snapshot) -> list[dict]
registry.dispatch(name, raw_arguments, context) -> ToolResult
registry.names(policy_snapshot) -> frozenset[str]
~~~

Registry 在应用启动时冻结或校验重复工具名，避免处理请求时热改 schema。测试构造独立 Registry，不触碰全局状态。

## 3. 执行顺序

1. 模型返回 tool_calls 后，按已注册 Schema 检查工具名。
2. 解析 JSON 参数，拒绝顶层类型错误、缺失必填、未知字段（除明确兼容字段）、超长字符串和超限数组；不默默转空 dict。
3. 依据 ToolContext 与 Policy 快照做权限 / 版本 / 准入检查。
4. 先取得该工具分类 semaphore，再根据当前 monotonic deadline 计算剩余时间；入队等待也消耗 deadline。
5. 在剩余时间内运行 handler。外部 I/O 用 async；同步 SQLite / Chroma 使用有界 executor；本地进程仅可执行预先固定的验证入口，参数数组传递，不经 shell。
6. 捕获 ToolError、超时、取消和未预期异常，返回对应 ToolResult。取消要传播给子任务并释放 semaphore。
7. 对内容和 Evidence 做权限审查、去敏、来源校验和输出裁剪。记录工具名、耗时、状态、usage，不记录 API Token。
8. Agent 将每个 ToolResult 适配为 provider 所需的 tool message。Evidence 被收集并由统一 citation renderer 格式化；模型生成最终文本不能伪造或替换 Evidence locator。

Agent 请求级控制 max_rounds、max_calls_per_round、max_total_calls、deadline 与累计结果字符数；模型 API 的 request-count / token reservation / optional cost reservation 由 BudgetController 单独管理。ToolSpec timeout 是单项上限，实际采用 min(spec timeout, request deadline remaining)。全局、群、用户预算同时适用；user_id 预算无需改变默认群共享会话。

## 4. 兼容迁移

当前 app/tools/agent.py 可作为暂时 facade：

1. 把现有静态 TOOL_DEFINITIONS 转换成一批 ToolSpec，由 Registry 生成等价 schema。快照测试确保工具名、必需字段、描述性语义、数组边界未意外改变。
2. 将现有 _execute_native_tool_call 的每个分支拆成 handler adapter。保留 SearchResult 作为外部工具内部 DTO，并写 SearchResult -> ToolResult/Evidence 转换；不要求一次改完外部函数。
3. run_agent_loop 改为读取 registry schemas 与 dispatch，保持函数签名或保留 wrapper。未知工具、重复调用、并行上限和每 tool_call 对应一个回复仍保留。
4. LLMHandler._search_local_knowledge 先适配成 knowledge_search handler；后续按文档、配方、Git、SFM 域拆分 handler，而非向 LLMHandler 填更多 if / elif。
5. /weather、/news、/web 通过同一 registry 执行，但命令解析和 QQ 回复格式保持现有行为。命令是否允许匿名群成员继续沿用既有权限，工具策略不可隐式收紧或放宽。
6. 每次迁移一个工具族：旧路径保留一版可切换 feature flag；新旧 Fixture / Mock 的结果对比通过后再移除兼容分支。

兼容风险主要在 OpenAI 兼容供应商对 schema 支持有差异。先保留现有 schema 子集，避免未经 provider Mock / contract test 验证而使用复杂 JSON Schema 条件。

## 5. 新工具分类

| 工具族 | 例子 | 默认策略 | 结果 Evidence |
|---|---|---|---|
| 本地文档 | document_search | 只读；精确 pack/version 过滤；索引空时返回 NotIndexed | 来源文件、章节、页码 / 行范围、文档 revision |
| 配方 | recipe_lookup、machine_recipes | 只读 SQLite 查询；必须指定 pack/version 或唯一默认版本 | dataset/version、recipe ID、来源文件与导入记录 |
| 仓库 | repository_search、repository_read | 固定本地根目录；只读；固定 commit；过滤敏感文件 | repo ID、commit、相对路径、行范围 |
| Web | web_search、fetch_url | 公开网络只读；SSRF 防护、host / redirect / byte limit | 原始最终 URL、title、fetched_at、摘要 |
| SFM | sfm_examples、sfm_validate | 检索只读；validator 输入受限；无真实 Parser 时状态为 unavailable | SFM release / Minecraft version、样例或诊断位置 |

生产环境的注册表必须在创建 Agent 前检查策略和服务状态；空资料不意味着禁用全 Bot。一个资料工具不可用时，其他现有能力仍可工作。

## 6. 测试契约

- 注册：重复名启动失败、schema 一致、工具策略过滤和 provider schema 兼容快照。
- 参数：错误 JSON、缺字段、错类型、多余字段、超长输入、最大数组边界。
- 权限：未授权用户、群策略拒绝、域外网络、路径越界和敏感文件拒绝。
- 预算：并发 semaphore、排队即耗 deadline、工具超时、累计结果裁剪；每个模型 HTTP 请求在发送前 reserve，测试并发原子准入、成功 usage settle、请求前 release、发送后失败 / 取消保留占额、无 usage、过期 reservation 恢复和流式未知 usage。
- 取消：请求取消向工具 handler 传播并释放锁；连续失败不产生后台遗留协程。
- 兼容：旧工具名称 / 参数、强制命令映射、每个 tool_call_id 返回结果、引用顺序和格式一致。
- 错误：NoResults、NotIndexed、DataUnavailable、VersionMismatch、Denied、TimedOut 和 ERROR 不相互混淆。
- Web 抓取测试使用 MockTransport 验证 redirect / body 策略，Fake Resolver 验证地址归一化和拒绝，受控连接器集成测试验证实际 peer IP 是批准的 IP 且 HTTPS Host、SNI、证书主机名仍为原始 hostname。MockTransport 本身不证明实际 TCP 连接目标。测试覆盖 IPv4 / IPv6 / IPv4-mapped IPv6、IDNA、编码 authority、十进制 / 八进制 / 十六进制等特殊 IP 写法、mixed A/AAAA、DNS rebinding、重定向重新验证、代理绕过、原始及解压后 body cap。
- HTTPX client 使用 trust_env=False 且 follow_redirects=False；默认拒绝用户配置的通用 HTTP proxy。若使用代理，必须是由管理员显式配置、并在代理端验证 DNS 到 socket 目标绑定和逐跳 redirect 的受信任 egress proxy。不得把未经安全验证的 URL 交给 Jina Reader 等第三方抓取服务。若 pinned connector 只能通过不受支持的内部 API 实现，则对 httpcore 版本锁定并设置安全升级门；无法证明 pin 时失败关闭。
- 所有真实领域资料用人工 Fixture；Fixture 不替代真实 GTNH 数据验证。

## 7. 当前不引入 MCP 的原因

目前工具都由同一 Python 进程拥有，且需共享 Bot 会话、用户 / 群配额、deadline、引用和权限策略。ToolRegistry 是更小的内部边界。MCP 可作为将来外部工具进程的协议适配层，但不能取代本项目内部的数据授权和执行限制。只有出现独立工具供应方 / 多宿主复用 / 进程隔离的实际需求，再单独验证 MCP；参考 [MCP Tools 规范](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)。

## 8. 代码证据

- Schema 与分发：app/tools/agent.py 的 TOOL_DEFINITIONS、TOOL_NAMES、_execute_native_tool_call、run_agent_loop。
- 模型协议：app/llm.py 的 ToolCallRequest、ChatWithToolsResponse、DeepSeekClient.chat_with_tools。
- 知识回调和命令强制调用：app/handlers/llm_handler.py 的 _search_local_knowledge、_make_forced_call / forced tool 分支；app/bot_service.py 的 _parse_command。
- 现有次数 / 时间限制：app/config.py 的 agent_tool_max_rounds、agent_tool_max_calls_per_round、agent_tool_max_total_calls、agent_total_timeout、network_tool_timeout。
- 现有错误转文本：app/tools/agent.py 的工具结果循环和 _format_tool_result；需要迁为状态明确的 ToolResult。
