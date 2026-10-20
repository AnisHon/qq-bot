# Agent Progress V1：命令与权限建议

日期：2026-10-20
审计基线：当前工作区，HEAD **39e735b**；Context V2、Web Research 与 Agent 相关文件有未提交改动。源码事实不代表这些更改已经部署。

## 1. 当前 Command System 事实

命令由 **app/commands/service.py** 的 **CommandService._register_commands()** 注册，经过独立 **CommandRegistry** 解析、检查 policy、timeout 并调用 handler；BotService 在进入 LLM 前识别 slash command（**app/commands/registry.py:19-64**、**app/bot_service.py:83-86**）。本设计只扩展现有 Registry。

| 命令组 | 当前格式 | 权限/能力 |
|---|---|---|
| 通用 | **/help [command ...]**、**/<command> --help** | 根据注册 policy 显示 |
| Bot 状态 | **/status**、**/usage** | status 公开；usage 按用户授权范围或管理员本群聚合 |
| 模型 | **/model show**、**/model list**、**/model use <model>** | 查看公开；切换管理员 |
| 上下文 | **/context show [--summary]**、**/context stats**、**/context compact**、**/context reset**、**/context purge** | show/stats 公开；管理操作管理员；reset/purge 经 /confirm |
| 记忆 | **/memory list/add/delete/search/clear** | 管理员；memory clear 二次确认 |
| 定时任务 | **/task list**、**/task add <description> ...**、**/task cancel <id>** | 管理员；只操作 scheduled_tasks |
| Minecraft 数据 | **/pack show/list/use**、**/wiki <query>**、**/recipe <item-id>**、**/machine <machine-id>** | pack use 管理员；查询走现有服务 |
| SFM | **/sfm help/search/validate** | 受版本与 Validator 状态限制 |
| 仓库 | **/repo search/read** | 只读；仓库白名单、固定 commit 与服务策略 |
| 外部工具 | **/weather <location>**、**/news [topic]**、**/web [search|read] ...** | 直接调用 Tool Registry |
| 群配置 | **/config show**、**/config set <key> <value> ...** | show 公开安全字段；set 管理员 |
| 确认 | **/confirm <token>** | 管理员；令牌绑定用户/群/操作/目标及 TTL |

注册位置见 **app/commands/service.py:101-220**。未知 slash command 不进入 Agent。**/status** 显示 Bot、模型、知识服务与 Tavily configured/last health，不代表 Agent 运行态，也不发联网探测请求（**app/commands/service.py:227-264**）。

### 1.1 与进度相关的当前语义

- **/task list/add/cancel** 管理 scheduled task，不是 Agent 任务查询或取消（**app/commands/service.py:141-148,457-510**）。
- **/context stats** 显示 SQLite 历史、索引、摘要、Compaction 与窗口估算，不显示当前模型/工具活动（**app/commands/service.py:334-354**）。
- **/context reset** 与 **/context purge** 经确认 token 执行 epoch reset/历史清理（**app/commands/service.py:360-369,768-829**）。后续需加入共享 delivery barrier/fence，而不能仅在事件入队时验证 epoch。
- **/config set** 当前只允许 **model_provider**、**default_pack**（**app/commands/service.py:51,750-766**）；群配置保存在已有 **group_settings** 表（**app/memory.py:316-323,1911-1930**）。当前没有 progress_mode。
- **/web**、**/news** 直接走 Tool Registry。**CommandService._dispatch_tool()** 目前在 Registry 之前发通用“正在查询”提示（**app/commands/service.py:836-867**），应改成 AP-02/AP-03 的真实 Registry 生命周期事件。
- 当前没有 **/agent** 或 **/progress**。数据库有 Agent task/checkpoint，但没有实时任务查询命令；SQLite running 行不证明当前进程仍有 coroutine。

## 2. 推荐 V1 命令

配置复用 **/config**；只新增 **/agent status** 作为只读运行态查询。不新增 **/progress**、Agent cancel 或 **/task status**。配置走原 whitelist/admin policy，Agent task 与 scheduled task 分开。

命令状态只展示 Agent live execution 的安全投影。WorkingState 持久化与 QQ 投递需作为独立诊断维度呈现，不得从 Agent 完成状态推断“已保存”或“已送达”。

### 2.1 /config show

**谁可用**：所有群成员。
**数据范围**：当前群安全配置。
**新增输出**：显示 progress_mode；未设置时显示 **balanced（默认）**。不显示事件 payload、目标、query 或用户 ID。

~~~text
本群安全配置：
model_provider：使用默认模型
default_pack：未选择
progress_mode：balanced（默认）
~~~

当前 _config_show() 只输出 model_provider/default_pack（**app/commands/service.py:744-748**）；V1 扩展同一 handler。

### 2.2 /config set progress_mode <quiet|balanced|verbose>

**谁可用**：管理员，复用现有 /config set admin policy。
**作用范围**：仅更新当前群 **group_settings** 的 progress_mode。
**确认**：不需二次确认；非破坏性操作。
**校验**：仅接受 quiet、balanced、verbose；不接受任意配置名、URL、脚本或环境变量；model_provider/default_pack 原行为不变。

示例：

~~~text
/config set progress_mode balanced
~~~

成功提示：本群进度模式已设为 balanced；正在运行的任务保持原模式，新任务生效。

失败提示：

- 值无效：progress_mode 仅支持 quiet、balanced、verbose。
- 普通成员：复用 Registry 的 PERMISSION_DENIED。
- 未授权配置键：沿用现有 whitelist 错误，不写入存储。

任务开始时读取并固定进度模式；管理员更改作用于新任务。已有 group_settings 表无需 schema migration。

### 2.3 /agent status

**谁可用**：所有群成员。
**数据范围**：只读当前进程 Coordinator 的 live projection；不读 prompt、不重建事件历史、不发 LLM 请求。
**普通成员**：只见本人发起的活动用户任务。若无权查看，统一回答“当前没有你可查看的活动 Agent 任务”，不透露其他成员是否在提问。
**管理员**：可看本群安全聚合；不显示发起者 QQ ID、目标全文、工具参数、URL、路径或 Evidence 正文。
**Scheduled task**：继续使用 **/task list** 查询；默认不并入 /agent status。

响应示例：

~~~text
你的任务正在读取相关网页。已结束 2/3 项：1 项成功、1 项部分结果；仍有 1 项运行中。
~~~

无活动任务：当前没有你可查看的活动 Agent 任务。

管理员聚合：本群有 1 项 Agent 工作正在进行：正在检索资料（已结束 2 项，1 项无结果）。

只展示固定分类、枚举状态、安全计数和 elapsed bucket。统计来自 Coordinator 对真实 AgentEvent 的归约；不从自由文本或 SQLite 状态推断。命令由现有 Registry 执行，不转入 Agent Loop。

## 3. V1 不新增的命令

### 不新增 /progress

进度偏好由 **/config set progress_mode ...** 管理；**/agent status** 是运行状态查询。这样避免两组命令重复维护，也不与 **/task** 混淆。

### 不新增 /agent cancel

当前 **BotService._active_groups** 是群 busy marker，**MessageTaskRuntime** 管理 worker task，但没有按用户请求暴露的 cancel handle；SQLite **agent_tasks** 是 WorkingState 记录（**app/bot_service.py:185-203**、**app/task_runtime.py:31-35,145-155**、**app/memory.py:359-384,1290-1372**）。单独修改 DB status 不能取消 asyncio coroutine，因此 V1 不提供无法兑现的取消命令。

未来取消任务需要真实 task_id→asyncio.Task 活跃句柄、用户/管理员授权、取消和 WorkingState terminal CAS、跨群校验、工具清理、final suppression 与确认语义。普通成员只能取消本人任务；管理员是否可取消他人任务需单独决定。重启后 interrupted task 不可当作活跃或自动续跑。

### 不增加 /task status 或 /context status

- **/task list** 已展示 scheduled task 状态。
- **/context stats** 继续显示历史、索引、摘要和 Compaction 统计。
- **/status** 继续显示 Bot/模型/知识服务配置与可用状态。
- **/usage** 继续显示授权范围内的模型 usage。
- **/agent status** 只显示当前进程正在运行的 Agent 状态。

这些查询域互不替代；UNKNOWN 统计不从配置存在推断为正常。

## 4. 权限与隐私矩阵

| 命令/数据 | 普通成员 | 管理员 | 额外约束 |
|---|---|---|---|
| **/config show** progress_mode | 可查看本群值 | 同左 | 只显示安全模式字段 |
| **/config set progress_mode ...** | 拒绝 | 可设置本群 | 固定 enum，不可编辑任意配置 |
| **/agent status** | 只看本人任务 | 本群安全聚合 | 不公开目标/query/QQ ID |
| **/task list/add/cancel** | 拒绝 | 保持现有权限 | 只操作 scheduled_tasks |
| **/context stats** | 现有安全群统计 | 按当前实现 | 不加入另一用户任务正文 |
| **/context reset/purge** | 拒绝 | 保持确认机制 | 需同 Dispatcher barrier fence |
| **/status** | 公开 | 同左 | configured 不等于真实连通 |
| **/usage** | 当前用户范围 | 本群范围 | 不与进度状态混为一谈 |

Command Registry 和 Coordinator 都检查 group/user/task scope；不能从昵称或消息文本推断管理员。命令参数不能指定别群、其他用户或任意任务目标。

## 5. 帮助与兼容影响

- **/help config** 由 CommandSpec metadata 展示新增进度模式和管理员限制；**/help agent** 展示 /agent status 的 scope 及“只报告当前进程运行状态”的边界。
- 未知 **/agent cancel** 或旧 progress 命令返回标准未知命令提示，不落入普通 AI 对话。
- 不增加 /progress alias；不同时暴露 /agent status 与 /progress status。
- 不改变 **/task**、**/context stats**、**/status**、**/usage** 既有领域语义。
- slash 工具命令继续复用 Tool Registry；AP-02/AP-03 替换 dispatch 前 callback 后，命令参数、结果和权限保持原有行为。
- 进度消息不是 slash command，不写入普通历史，不作为新的 Agent user message。
