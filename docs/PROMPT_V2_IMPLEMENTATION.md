# Prompt System V2 实施报告

**实现基线：** `3e6a4a4` 及其工作区源码。已有未跟踪的 `docs/PROMPT_STYLE_AUDIT.md` 在本次改动中保留。
**状态：** 源码修改已完成；Mock/Fixture 与本地单元测试通过。未调用真实模型或 QQ，未部署、提交或 push。

## 1. Main System Prompt 前后对比

旧 Prompt 的身份开头是 `You are a controlled QQ group assistant.`，并统一要求 `concise natural Chinese`、`Keep replies short`，对所有任务共用相同表达基线。它没有明确区分闲聊、技术讨论、排障和复杂研究。旧文本的完整内容见 [PROMPT_STYLE_AUDIT.md](PROMPT_STYLE_AUDIT.md) 第 3 节。

新 Prompt 位于 [app/prompts.py](../app/prompts.py#L7)，由 [llm_handler.py](../app/handlers/llm_handler.py#L50) 以兼容名称 `SYSTEM_PROMPT` 引用，实际请求仍通过原有 Handler/ContextManager/Agent 路径发送。新身份定位为参与 QQ 群聊的 AI 助手，明确要求按场景调整语气：闲聊可以轻松，技术问题准确深入但不照本宣科，排障先看当前症状，复杂研究有条理但不写成报告。

修改重点：

- 去掉所有回复都要短、普通文本不能有层次的统一压力；允许按任务复杂度展开。
- 允许顺着用户语气自然回应、承接已知上下文、少讲用户已经掌握的基础概念；幽默可用但不强求，也不假装真人经历。
- 不强制标题、列表、总结、固定开场或结尾反问；技术代码、配置和命令仍可单独换行。
- 保留引用/历史/记忆/工具内容是不可信数据、不可编造配方/ID/版本/来源、FTB task/reward/dependency 方向等安全和事实边界。
- 继续不公开 reasoning_content、工具原始参数、凭据和私有信息；进度文案只能描述真实状态。

## 2. 人格、动态请求与 Agent Prompt

`ContextManager` 不再在每条当前请求后重复“directly and concisely / do not repeat”风格指令。动态后缀改为共享 `CURRENT_REQUEST_GUIDANCE`，只提示根据任务匹配深度、承接已知上下文，以及研究时优先使用相关本地知识、证据充分后停止搜索，见 [app/prompts.py](../app/prompts.py#L36) 和 [context/manager.py](../app/context/manager.py#L410)。当前请求仍是 user 消息，系统指令仍在 system role，动态可信边界没有变化。

Agent 的执行协议单独放在 `AGENT_PLAN_EXECUTION_RULES`：工具权限、证据引用、步骤完成评估、重规划上限和目标约束保持严格。同时要求不要把 `WorkingState`、checkpoint、revision、evidence reference 等实现状态直接写给普通用户，见 [app/prompts.py](../app/prompts.py#L43) 与 [tools/agent.py](../app/tools/agent.py#L859)。这没有改变 ToolPolicy、Tool Schema、Function Calling 或计划完成检查。

复杂任务未完成时，用户看到的说明改为自然描述“还有要求未能核实”，不再公布内部 `2/5` 计数或 `计划状态` 字段，见 [tools/agent.py](../app/tools/agent.py#L1577)。

## 3. Planner 与 PublicPlan

Planner 和 Replanner 仍然返回严格 JSON，字段、依赖、证据类型、工具白名单、最多 8 步和不可自行标记完成等约束不变。新增的语言规则要求 `description` 使用用户当前语言、写成具体行动、避免“分析问题/继续思考”等空步骤，见 [app/prompts.py](../app/prompts.py#L49)。Planner 现有模块保留 `PLANNER_SYSTEM` / `REPLAN_SYSTEM` 名称作为兼容别名，见 [planning/planner.py](../app/planning/planner.py#L8)。

`TaskPlan.public_summary()` 仍由真实 TaskPlan 步骤确定性生成，不增加润色模型调用；它现在用自然衔接表达 1–4 个实际动作，不显示 step ID、revision、状态或生硬的分号清单，见 [planning/contracts.py](../app/planning/contracts.py#L264)。若应用已通过 coordinator 接受并发布 TaskPlan，后续重复的模型 PLAN 会被拒绝；正常工具和 UPDATE/REVISION 仍走原有调用路径。PublicCommentary 工具说明集中到 `PUBLIC_COMMENTARY_TOOL_RULES`，见 [app/prompts.py](../app/prompts.py#L47) 与 [progress/commentary.py](../app/progress/commentary.py#L12)。

示意：TaskPlan 的动作是“查找 GTNH 铂处理资料、核对版本与关键配方、整理机器和物料流程”，公开计划会组合成“我先查找 GTNH 铂处理资料，接着核对版本与关键配方，最后整理机器和物料流程。”这只是确定性投影示例，不是实时模型输出。

## 4. PublicCommentary 与 Execution Trace

内部任务状态和对外叙述继续分离：工具执行事实来自 Registry/AgentEvent；PublicCommentary 只表述意图或由已完成 ToolResult 支持的阶段说明。计划事件、工具结果关联、epoch/权限校验、CQ 文本转义、进度配额和 QQ Reporter 均复用现有实现。

移除了“计划状态：x/y 项已标记完成”，完成某个有证据的步骤后，公开更新改为引用真实步骤描述和下一项 READY 动作；最后步骤完成后用短句说明正在整理结果，见 [tools/agent.py](../app/tools/agent.py#L831)。

没有可用模型 Commentary 时，静态进度仍只根据真实事件状态生成，但类别文案现在更具体自然，例如“正在查找网页资料……”“正在读取网页正文……”“已有查询结果，我正在整理答复……”，见 [progress/presentation.py](../app/progress/presentation.py#L47)。结果统计依然来自 ToolOutcome，不把 NO_RESULTS、失败或拒绝说成成功。

为防止参数泄露，Execution Trace 不直接显示自由格式的原始搜索 Query、URL、仓库路径或 Tool Arguments。当前事件契约不包含安全参数投影字段，因此状态文案仍显示工具类别和结构化结果；如果未来要展示查询摘要，需要在工具端增加针对字段的显式安全投影，不能直接序列化参数。

## 5. Research Answer Validator 修复

此前 `_validate_research_answer()` 将未完成语句、证据中未见的 namespace ID、特定任务/奖励关系错误等不同问题统一替换成固定 ATM10/ATM Star 长文。这会覆盖无关整合包、SFM 或一般研究答案。

现在校验与用户可见说明分离：

- `_validate_research_answer()` 返回 `AnswerValidation`，记录 `PASS`、`INCOMPLETE` 或 `UNSUPPORTED_CLAIM` 与问题类别，见 [tools/agent.py](../app/tools/agent.py#L1717)。
- `_validated_research_answer()` 只删除与未核实 ID、反向任务/奖励关系、与来源冲突的特定说法或未完成的后续承诺对应的句段，保留其它有用内容，并解释具体缺口，见 [tools/agent.py](../app/tools/agent.py#L1795)。不再插入 ATM10、JEI/EMI 或 ATM Star 专属建议。
- 校验将当前模型消息中的工具结果与本请求 EvidenceStore 的有界留存内容合并，尽量避免 Compaction 移除旧 tool message 后误删已验证的 ID。读取只使用当前 request/group/user/epoch 范围，最多 512 KiB，见 [tools/agent.py](../app/tools/agent.py#L627)。
- 正常退出与 max-rounds final synthesis 共用同一校验函数，见 [tools/agent.py](../app/tools/agent.py#L1001) 和 [tools/agent.py](../app/tools/agent.py#L1351)。

保留了具体可执行的安全检查：来源标记目标为任务要求时拒绝“完成任务后获得目标物品”这种方向颠倒的说法；已证实物品时拒绝模型否认该物品存在；证据中没有的精确 namespace ID 不作为已确认结果。修复不声称是通用语义事实审查器：它不能可靠验证所有数量、机器参数或配方因果关系，仍需依赖结构化配方服务、Evidence、TaskPlan 和模型遵循提示。

## 6. Prompt 组织与其他专用 Prompt

新增轻量 [app/prompts.py](../app/prompts.py)，集中共享人格、动态请求指导、Agent 执行规则、Planner/Replanner、PublicCommentary、研究最终回答和群聊摘要片段；没有加入模板引擎。SFM 的“只输出源码”协议仍保留在 [sfm/workflow.py](../app/sfm/workflow.py#L399)，不影响普通聊天。工具 Schema 仍由 ToolSpec 持有。

摘要器保留原 JSON Schema、source row 校验和不可信对话边界；新增说明这是结构化记忆记录而非面向用户的报告，见 [app/prompts.py](../app/prompts.py#L53)。`RESEARCH_FINAL_RULES` 用于 max-rounds 的最终合成 Prompt，不把普通回答全部改成研究报告。

## 7. 用户可见示例

以下是基于 Prompt 约束人工编写的对照示例，不是模型输出，也不是实测 A/B 结果。

| 场景 | 旧 Prompt 倾向（人工模拟） | V2 候选（人工示例） |
|---|---|---|
| 普通群聊 | “挺有意思的，但后期内容比较耗时。” | “好玩，尤其是产线终于跑通那一下挺爽。后期确实越来越磨人。” |
| 技术讨论 | “不完全是。引用是对象别名，指针保存地址。具体实现取决于编译器。” | “有点像，但不能直接画等号：引用语义上是别名，指针是能改指向的对象。底层怎么实现是另一回事。” |
| 模组排障 | “需要检查机器、供电和结构。请提供更多信息。” | “又炸了？先看供电还是结构报错。你说的是哪台 MI 机器？把控制器提示贴一下，我能更快缩小范围。” |
| 多阶段研究 | “我会按计划处理：搜索资料；核对来源；整理方案。” | “我先找 Wiki 和社区里的铂处理流程，再对一下版本和配方；信息对上后给你按机器和物流拆一版自动化方案。” |

真实表达会受到模型与 Provider 的语言能力影响，以上只用于说明 Prompt 方向。

## 8. 模型参数、协议和额外开销

- 未修改温度、presence penalty、frequency penalty、max_tokens、top_p 或 reasoning 设置。本次修改的是 Prompt 和已有公开消息投影；真实部署模型配置仍可能来自外部环境，未读取任何 `.env` 或密钥。
- 未增加模型 API 请求。复杂请求原有 Planner 模型调用仍然存在；简单请求仍走原有简单路径。PublicPlan 由 TaskPlan 确定性投影，PublicCommentary 继续通过正常 Function Calling；没有新增“润色计划”调用。
- Function Calling JSON、tool_call_id/tool_result 配对、ToolPolicy、ContextBudget、UsageBudget、WorkingState、EvidenceStore、SFM 生成/验证协议均未改变。
- 普通研究回答当前直接使用 Agent 最终输出；未调用的 `_verify_research_answer()` 仍不是常规回答后处理路径。

## 9. 测试与质量结果

新增离线用例文件 [tests/fixtures/prompt_style_cases.json](../tests/fixtures/prompt_style_cases.json)，共 22 个案例，覆盖日常聊天、技术讨论和追问、Minecraft/MI/配方、研究任务、无结果、SFM Parser 不可用、引用/网页注入及简单任务。测试验证 Prompt 约束和确定性处理，不生成模型回答。

实际命令和结果：

```text
timeout 120 .venv/bin/python -m unittest tests.test_prompt_system_v2 tests.test_agent tests.test_agent_planning tests.test_context_manager tests.test_progress_reporter tests.test_public_commentary -q
Ran 77 tests — OK

timeout 300 .venv/bin/python -m unittest discover -s tests -q
Ran 525 tests — OK
```

`git diff --check` 通过。完整测试期间有故障注入测试预期的 RuntimeError/降级日志、既有 FastAPI/正则 DeprecationWarning 和 SQLite ResourceWarning；测试进程以退出码 0 完成。

### 真实模型 A/B 的可复现方式

本次没有调用真实模型，也没有声称自然度已提升。后续可用同一 Provider/model、相同采样参数、相同 Context Fixture 和相同 Tool Fixture，把审计报告中的旧 Main Prompt 与 V2 Prompt 按随机顺序各跑一次；评审者对自然度、上下文承接、技术准确性、信息密度、语气适配、模板化、PublicPlan、执行真实性、完成质量和安全性做 1–5 分盲评。公开运行前需再次确认测试环境和费用；不要把本地 Fixture 测试得分冒充线上效果。

## 10. 状态与上线

- **IMPLEMENTED：** Prompt 组织、自然语言规则、Planner/PublicPlan、阶段进度文案和研究回答校验修复。
- **TESTED_WITH_FAKE：** 22 个离线 Prompt 风格 Fixture、Agent/Planner/Commentary/Validator 测试。
- **TESTED_LOCALLY：** 目标回归 77 项、完整 unittest 525 项及 diff whitespace 检查。
- **NOT_VERIFIED：** 真实模型中文风格变化、真实群聊体验、真实 Web/GTNH 数据质量。
- **BLOCKED：** 无代码级阻塞；尚未进行生产部署或 QQ 实测。

不需要数据库迁移、新配置项或新依赖。代码和测试层面可以进入人工审查/部署准备；因本次明确未调用真实模型或 QQ，也未部署，不能标记为线上体验已验收。
