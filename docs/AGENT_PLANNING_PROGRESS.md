# Agent Planning V1 实施进度

| Task | 状态 | 记录 |
|---|---|---|
| APL-01 Complexity Assessment 与 Planner | COMPLETED | 低成本启发式分类；复杂任务经现有 LLM Client 进行有 deadline 的 JSON 计划生成，失败回退既有 Agent Loop。 |
| APL-02 TaskPlan、PlanStep 与 WorkingState | COMPLETED | 复用 `agent_tasks.state_json` / revision checkpoint；不新增表；损坏的已存在计划不会被静默丢弃。 |
| APL-03 Plan-Aware Execution Controller | COMPLETED | 通过隐藏 Registry 工具提交步骤评估；实际 ToolResult 和 Evidence 进入 Controller；原生 Agent 工具调度未重写。 |
| APL-04 Observation、Replanning 与失败恢复 | COMPLETED | NO_RESULTS 等观察不会自动完成步骤；最多两次重规划；目标、已完成步骤和未解决验收条件受程序约束。 |
| APL-05 Completion Verification 与 QQ PublicPlan | COMPLETED | TaskPlan 出现在 Compaction 工作状态；最终前重试并检查 Evidence/依赖；未完成结果会明确标为部分完成；PublicPlan 来自计划投影。 |
| APL-06 集成、A/B 评估与完整回归 | COMPLETED | 20 个规划测试、128 个 Agent/Context/Registry/Progress 相关回归及完整 509 项 unittest 全部通过；Fake A/B 开销数据记录于实施报告。 |

## 已核对基线

- HEAD：`394065d`；`main` 跟踪 `origin/main`；开始时工作区干净。
- Agent 的工具执行、并发和 tool_call/result 配对仍由 `app/tools/agent.py::run_agent_loop()` 管理。
- `WorkingState` 已按 task/group/user/epoch 持久化在 SQLite JSON checkpoint 中，带 revision CAS；进程重启会将 running 标为 interrupted。状态目前只记录通用工具名和结果状态，没有结构化计划。
- LLM Client 的 `chat()` 与 `chat_with_tools()` 共用 Context Window 和 Usage Budget 请求入口，可在同一 deadline 下调用；复杂请求规划调用必须经过该 Client。
- Public Commentary 已由内部 Function Calling 和 ProgressCoordinator 提供；本次计划公开文本将从校验后的 TaskPlan 投影生成，执行事实仍来自工具结果。
- 没有读取 `.env`、调用真实模型、QQ 或外部付费服务。

## 执行记录

| Task | 修改范围 | 测试命令 / 结果 | 问题与恢复点 |
|---|---|---|---|
| APL-01 | `app/planning/planner.py`、`contracts.py` | `timeout 120 .venv/bin/python -m unittest tests.test_agent_planning -q` — 最终 20 passed | Planner JSON/Schema 失败时回到现有 loop；未使用真实模型。 |
| APL-02 | `app/context/contracts.py`、`app/context/working.py` | 同上；计划 checkpoint round-trip、旧 JSON 无 `task_plan` 字段兼容及损坏计划拒绝均通过 | 发现并修复 Agent 直接调用未传 epoch 时 EvidenceStore 写入与计划校验 scope 不一致。 |
| APL-03 | `app/planning/controller.py`、`app/tools/agent.py`、`app/progress/events.py` | `timeout 180 .venv/bin/python -m unittest tests.test_agent tests.test_agent_compaction tests.test_agent_integration tests.test_agent_progress_integration tests.test_tool_registry tests.test_agent_planning -q` — 相关集合通过（总计 128 项命令也覆盖 Context/Progress） | 工具成功只更新观察；自然语言完成条件仍由模型判断，状态明确记为 `model_assessed`。 |
| APL-04 | `app/planning/controller.py`、`app/planning/contracts.py` | 同上；NO_RESULTS→replan→部分完成 Fixture 通过 | 重规划禁止删除未解决 step 或降低原完成条件/Evidence 要求；上限为 2 次。 |
| APL-05 | `app/tools/agent.py`、`app/handlers/llm_handler.py`、`app/progress/events.py` | 同上；TaskPlan 持久化、进入 Compaction prompt、PublicPlan 和未完成最终回复测试通过 | 未引入额外 QQ 发送链或第二个 Agent；复杂请求仅增加一次初始 Planner 调用。 |
| APL-06 | `tests/test_agent_planning.py`、`docs/AGENT_PLANNING_IMPLEMENTATION.md` | 定向测试 20 passed；`timeout 300 .venv/bin/python -m unittest discover -s tests -q` — **509 passed, 0 failed** | Fake A/B 比较测固定 Fixture 的计划覆盖与开销，不代表真实模型质量。全量输出存在既有 warning 和模拟故障日志，没有测试失败。 |

## Fake A/B 记录

- 固定成功场景：旧 Agent Loop 4 次模型请求、3 次业务工具；Planning V1 6 次模型请求（含 1 次 Planner）、3 次业务工具、5 次内部步骤评估，5/5 计划步骤已标记完成，0 次 replan。
- 一次运行累计输入消息为 5,573 / 30,918 UTF-8 字节；本地 Fake 运行时间约 8.08 / 14.26 ms。字节数不是 Token，时间不是生产延迟，脚本化输出也不能验证真实模型质量。
- 新闻 NO_RESULTS Fixture：有限 replan 后仍保留未完成状态，并输出部分完成提示；没有真实来源时不伪造新闻。

当前状态：APL-01～APL-06 全部 COMPLETED。已生成实施报告；没有部署、真实模型请求、生产数据库修改或 Git push。真实模型、GTNH 数据与真实 SFM Parser 尚未验证。
