# Context V2：Command System V2 定向调整

日期：2026-10-15；源码基线 `63a9da1`。仅设计，未修改命令代码。[整体设计](CONTEXT_V2_DESIGN.md)、[CV2-11任务](CONTEXT_V2_TASKS.md)。本方案复用现有 Registry/Parser/Policy/Renderer，不重写命令系统。

## 1. 当前真实命令清单

以 `app/commands/service.py:98` `_register_commands()` 为准。表中“公开”指无需配置管理员资格，仍受工具服务Policy限制；管理员由 `Settings.is_admin()` 的QQ ID白名单判断，不从文本或群成员昵称推断。根 model/context/memory/task/pack/sfm/repo/config 是子命令容器，单独输入显示用法，不执行领域操作。

| 当前格式 | 权限 / 实际能力 |
|---|---|
| `/help [command ...]`、`/<command> --help` | 按注册Policy生成帮助，Parser处理help选项 |
| `/status` | 公开；模型、文档、配方、工具、SFM真实可用性 |
| `/usage` | 普通成员自身范围；管理员本群范围；无可信费用/usage则说明未知 |
| `/model show`、`/model list` | 公开 |
| `/model use <model>` | 管理员；现有群设置模型选择 |
| `/context show` | 公开；聊天行数和是否有自动摘要 |
| `/context summary` | 公开；读取已有摘要，不触发新摘要 |
| `/context clear` | 管理员；发起确认，执行时删除群历史与自动摘要，不删长期记忆 |
| `/memory list`、`/memory add <content>`、`/memory delete <id>`、`/memory clear` | 全部管理员；add允许多行；clear须确认 |
| `/task list`、`/task cancel <id>` | 管理员；定时任务列表/取消 |
| `/task add <description> --in 10m` | 管理员；也可择一 `--at "YYYY-MM-DD HH:MM"` / `--every 1h` / `--daily HH:MM`，不等于任意自然语言时间解析 |
| `/pack show`、`/pack list` | 公开；列出实际索引文档或活动配方支持的精确版本 |
| `/pack use <pack> <version> [--game-version <version>]` | 管理员；没有精确可用数据不切换、不回退版本 |
| `/wiki <query>` | 公开；本地文档服务，所选pack版本约束 |
| `/recipe <item-id> [--direction produces\|consumes] [--kind item\|fluid]` | 公开；结构化查询，非LLM生成配方 |
| `/machine <machine-id>` | 公开；结构化机器配方查询 |
| `/sfm help` | 公开；能力与版本要求 |
| `/sfm search <query> --sfm-version <version> --mc-version <version>` | 公开；版本化文档检索 |
| `/sfm validate <code> --sfm-version <version> --mc-version <version>` | 公开但受Validator安全状态限制；多行代码，不可用/Fake不得报告真实验证通过 |
| `/repo search <query> --repo <id> [--mode path\|text] [--commit <sha>]` | 公开入口+服务安全Policy；allowlist、固定提交查询 |
| `/repo read <path> --repo <id> --commit <sha> --start <line> --end <line>` | 同上；路径/敏感文件/范围限制 |
| `/weather <location>`、`/news [topic]`、`/web <query-or-url>` | 公开入口，经现有ToolRegistry，不经过LLM才执行 |
| `/config show` | 公开安全字段 |
| `/config set <key> <value> [--version <pack-version>] [--game-version <version>]` | 管理员；当前白名单仅model_provider/default_pack，非通用环境编辑器 |
| `/confirm <token>` | 管理员；绑定用户/群/操作/目标，300秒、单次使用、内存最多128项，重启失效 |

相关调用：BotService识别斜杠→CommandService构造可信Context→CommandRegistry解析、Policy、timeout→handler→领域服务→QQCommandRenderer（最多6000字符，CQ文本转义）。Registry位于 `app/commands/registry.py`；确认与 `_context_show/_context_summary` 位于 service `:278` 起，`_confirm/_request_confirmation` 在同文件。未知命令返回错误，不进入Agent。现有 `tests/test_commands.py` 已验证这些权限、确认、群隔离和非命令路由。

旧 `/summary` 根命令不在当前注册表，不能把删除它当成本次新增工作。没有当前 `/task status`、`/context stats`、`/context compact` 或 `/memory search` 实现。

## 2. 最终调整决定

| 操作 | 命令 | 理由 |
|---|---|---|
| 保留并增强 | `/context show [--summary]` | 配置与状态；可选展示已有安全群摘要 |
| 新增 | `/context stats` | 来源数量、索引和估算窗口诊断；与usage费用账本不同 |
| 删除 | `/context clear` | 拆为语义明确的reset/purge；不保留旧入口 |
| 新增 | `/context compact` | 管理员故障排查/长讨论手动生成安全摘要；立即返回状态，不等待LLM |
| 删除并合并 | `/context summary` | 并入show --summary，不保留第二套同义入口；未知旧命令可提示新用法 |
| 新增 | `/memory search <query> [--kind fact\|episode\|all] [--limit <n>]` | 检索长期事实与讨论事件，复用MemoryService |
| 保留 | `/memory list/add/delete/clear` | 维持管理员人工记忆入口；补provenance/status显示 |
| 不增加 | `/task status` | 现有task是定时任务，list已显示状态；不让用户误以为这是Agent实时状态 |

当前Agent任务只通过context stats展示当前请求的安全运行概况，不公开另一个用户的私有目标/约束；不提供后台自主继续/任意resume指令。model/pack/wiki/recipe/sfm/repo/tools/config/help/status/usage保持原有职责。新增stats、compact、reset、purge、memory search五个handler；删除context summary与context clear，show增加summary选项；根context/memory保留。

## 3. 格式、权限和输出

所有示例是预期输出，数字是示意而非生产测量。普通成员能看到本群公开历史派生状态，不显示user私有事实、完整prompt、原始ToolResult或绝对路径。command权限与底层actor_scope双重检查，不能仅依赖隐藏帮助。

### `/context show [--summary]`

- 普通成员及管理员可用。无参数只显示本群模式、近期上限、留存政策、历史召回开关、模型窗口配置状态、epoch/revision和最近摘要状态。
- 示例：`/context show --summary`。
- 输出示意：`本群上下文：近期最多24条；历史FTS可用；语义检索关闭。模型窗口：管理员声明，尚未验证。摘要revision 4，覆盖至消息123，当前状态空闲。` 指定summary才显示群级概览；缺少摘要返回“尚无摘要”，不自动触发模型。
- 复用 `ContextManager.stats()` + SummaryService读取安全群概览，不能直接读私有episode拼接展示。legacy摘要标注来源不完整。

### `/context stats`

- 公开基础统计：本群当前epoch历史条数/FTS覆盖量、摘要覆盖与未覆盖量、向量关闭/降级状态、pending cleanup数、最近一次组装的来源计数、估算token/配置窗口/输出预留/安全余量、压缩次数/耗时。
- 示例：`/context stats`；输出可为 `最近组装输入约6200 tokens（byte_envelope_v1估算，非Provider计数）；窗口配置32768；输出预留2048；安全余量3277。FTS已索引1000/1000行。`
- 最近一次不是当前调用的精确prompt；没有观测则UNKNOWN。普通成员不见其他用户单次任务正文/记忆、query文本或用量账单。管理员可见本群聚合失败/排队计数，也不额外公开user私有文本。
- 复用ContextManager安全诊断与WorkingState状态摘要。`/usage`仍独立显示usage reservation/settlement；这里不能把估值写成实际消耗或费用。

### `/context reset`

- 管理员，二次确认；令牌绑定 `(actor, group, reset, expected_epoch)`，沿用5分钟TTL和单次使用。epoch变化后旧令牌无效。
- `/context reset` → `将开启新的上下文epoch，清除本群摘要和活动任务；保留历史原文，但旧历史不再参与召回。5分钟内 /confirm <token>。`
- 确认时原子epoch+1、失效旧摘要/工作状态/Evidence；不删除聊天原文、不清长期事实。与摘要CAS并发时仅一个结果生效。
- 复用 `ContextManager.reset()`→MemoryStore事务+runtime取消句柄，不由命令handler自行删SQL。已经发送的QQ消息不能撤回。

### `/context purge`

- 管理员，二次确认；令牌绑定 `(actor, group, purge, expected_epoch)`，purge令牌不能执行reset。
- `/context purge` → `将删除本群保留聊天原文及派生索引、摘要、事件记忆、Evidence和任务状态；长期人工事实保留并标记失效来源。数据不可恢复。5分钟内 /confirm <token>。`
- 复用 `ContextManager.purge()`。小数据SQLite事务清理；大数据epoch失效/tombstone后分批物理删除，状态显示“逻辑删除完成，物理清理待处理”。FTS与SQLite同事务，Chroma通过outbox清理；pending期间新请求不能看到旧索引。
- 磁盘/SQLite错误不返回成功，不降级执行部分清理。purge保留长期人工事实，但无来源事实展示source_deleted。

### `/context compact`

- 管理员。只是生成/更新群讨论摘要，不压缩另一用户正在运行的Agent轨迹，不清原文，不人为扩大后台容量。
- 示例：`/context compact` → `本群摘要任务已排队（覆盖至消息123）。可用 /context show 查看状态。`
- 没有可压区间：`近期完整消息仍需保留，暂无可压缩历史。`；相同群有任务：`摘要正在处理，无需重复提交。`；全局后台满：`后台任务已满，本次未排队，请稍后重试。`
- 复用ContextManager.request_compaction→SummaryService single-flight→MessageTaskRuntime；手动可跳过自动最小间隔/消息触发门，但不能跳过保留近期区间、窗口、usage预算、deadline、CAS和权限；命令dispatch不等待LLM，默认命令timeout仍可满足。
- 后台失败只反映在状态/脱敏日志，未经授权不主动向群额外发消息。用户等待状态不构成额外模型重试授权。

### `/memory search <query> [--kind fact|episode|all] [--limit <n>]`

- 管理员，保持现有memory命令组权限；默认kind=all、limit=5，限制1–20，query<=512字符；不接受group/user/path覆盖参数。
- 示例：`/memory search "高炉供电" --kind episode --limit 5`；`/memory search 铜线 --kind fact`。
- 输出：`fact:12 rev2 [admin_confirmed] ……；来源：群消息42（有效）` 或 `episode:7 [derived] 讨论主题/决定；覆盖2026-10-15，来源消息60–81`。明确标注来源过期/被删除、legacy、candidate/conflict等状态；来源编号不是服务器路径。
- 当前Scope为本群公开事实与episode，不因为管理员身份展示其他用户private事实或其他群内容。空结果与存储失败不同，不能SQLite异常时返回“无记忆”。
- 复用MemoryService.search，不走LLM，不将检索命中升级verified。

### 现有memory子命令的小调整

`/memory list`继续默认列长期事实，附ID/来源状态；`/memory add`记录管理员原文、执行者和本次命令来源，存为 `admin_confirmed` 的人工陈述，不声称外部工具已验证。模型抽取的普通陈述用 `user_asserted`，推断用 `inferred_candidate`；权限和来源只由程序设置。

`/memory delete <id>`保留纯数字fact ID；为删除搜索到的事件可接受 `episode:<id>`，fact可接受 `fact:<id>`，这只是命名空间解析，不混淆两个表ID。单条撤回立即不可检索，范围约束严格；`/memory clear`确认后清本群可管理fact与episode并失效相关概览，不删原始聊天。模型长期事实候选默认不自动确认；本轮不增加候选转正命令，管理员可通过 `/memory add` 显式写入自己确认的独立陈述，不自动提升旧候选。

## 4. 权限矩阵与失败语义

| 操作 | 普通成员 | 配置管理员 | 资源上限 |
|---|---|---|---|
| show/--summary | 本群公开状态/摘要 | 同上 | Renderer6000字符，长摘要节选标记 |
| stats | 本群安全聚合 | 加本群运维聚合，仍无私有文本 | 有界计数，不实时扫描全库正文 |
| reset/purge | 拒绝 | 各自确认后允许 | token不可互换；绑定epoch；purge有tombstone与批次状态 |
| compact | 拒绝 | 可排队 | 同群single-flight+全局后台容量 |
| memory list/add/delete/clear/search | 拒绝 | 本群授权记录 | search最多20项、查询512字符 |
| task list/add/cancel | 拒绝 | 原定时任务权限 | 不变 |

purge若量大：先事务epoch失效和tombstone，让查询立即不可见，再后台有界分批物理清理；状态说明“逻辑删除完成，物理清理待处理”。SQLite/FTS同批事务、Chroma outbox补偿，不宣称跨存储原子删除。旧原文未清完也不能被新epoch检索。

复用现有CommandErrorKind：权限拒绝PERMISSION_DENIED、参数INVALID_ARGUMENT、空搜索NOT_FOUND、模型窗口未配置NOT_CONFIGURED、知识/验证器缺失DATA_UNAVAILABLE、排队拒绝采用简明状态文案（不新增泛用错误体系）、超时TIMEOUT、存储失败INTERNAL_ERROR。用户输出不附trace、URL凭证或本机路径。

## 5. 实施与验收边界

只在 CV2-11 修改注册元数据和小handler，ContextManager/MemoryService接口由前置任务交付。保持 Parser的大小写/中文/引号/选项规则与现有QQ Renderer，确认结构扩展scope/epoch/revision字段，不保留V1/V2并行分发器。

覆盖动态help、旧summary提示、所有新选项、普通/管理员权限、跨群及旧epoch令牌、单次/过期确认、两compact请求、后台满载、清理状态、索引异常与日志脱敏；同时跑原命令服务/工具/非命令路由回归。Fixture只证明命令调用正确服务，不能冒充真实模型摘要效果、GTNH配方或真实SFM Parser可用。测试不发真实QQ、不调用付费API。
