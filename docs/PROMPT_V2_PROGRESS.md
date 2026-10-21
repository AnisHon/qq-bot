# Prompt System V2 进度

**执行基线：** `3e6a4a4`；开始时仅有用户此前生成的未跟踪审计报告 `docs/PROMPT_STYLE_AUDIT.md`，已保留。实现以当前工作区源码为准。
**限制：** 不调用真实模型、付费 API 或 QQ；不部署、不提交、不读取密钥。

## 任务状态

| Task | 状态 | 实际修改 | 测试/下一步 |
|---|---|---|---|
| PSV2-01 Main System Prompt 与人格 | COMPLETED | 新建 `app/prompts.py`；将主提示词改为自然中文、场景化表达，保留安全/事实约束；`llm_handler.py` 保留 `SYSTEM_PROMPT` 兼容别名 | 75 项目标测试通过 |
| PSV2-02 动态请求与 Agent Prompt | COMPLETED | 共享请求指导缩短为任务深度/承接上下文/研究路由；Agent 执行协议与对用户表达要求分开 | ContextManager 组装测试通过 |
| PSV2-03 Planner / PublicPlan | COMPLETED | Planner/Replanner 明确使用用户当前语言；TaskPlan 公共投影自然衔接，无内部 ID/状态；已公开 TaskPlan 时拒绝重复模型计划 | Planning 与 Commentary 测试通过 |
| PSV2-04 PublicCommentary / Progress | COMPLETED | 移除“计划状态 x/y 已标记完成”；按真实完成步骤生成阶段说明；进度类别文案更具体自然，仍只用安全结构化状态 | Progress / Commentary 测试通过 |
| PSV2-05 Research Answer Validator | COMPLETED | 将检测与呈现分离为 `AnswerValidation`；只移除未核实/不完整片段并说明具体限制，删除 ATM10 固定长答复覆盖 | 合法 ID、GTNH 未知 ID、FTB 关系、ATM 矛盾、无结果与未完成路径目标测试通过 |
| PSV2-06 Prompt 组织与去重 | COMPLETED | 新增轻量 `app/prompts.py` 集中共享人格、请求、Agent、Planner、PublicCommentary、摘要、研究合成片段；SFM 专用协议保留在原模块 | Prompt 组装目标测试通过；采样参数未变 |
| PSV2-07 评估、测试和回归 | COMPLETED | 建立 22 个离线自然语言案例及确定性测试；检查研究校验使用压缩前保留的有界 EvidenceStore 内容 | 目标测试：`timeout 120 .venv/bin/python -m unittest tests.test_prompt_system_v2 tests.test_agent tests.test_agent_planning tests.test_context_manager tests.test_progress_reporter tests.test_public_commentary -q`，77 项通过；全量 `timeout 300 .venv/bin/python -m unittest discover -s tests -q`，525 项通过 |

## 已知限制

- Prompt 和对照样例只能证明源码协议与确定性行为，不能证明真实模型回复质量提升。
- Model sampling 参数保持原配置；本次不调整 temperature、presence/frequency penalty 或 Provider 设置。
- 公共进度仍只投影静态工具类别和结构化结果；不添加自由格式原始参数，避免把私有查询或凭据直接发到群里。

## 恢复位置

所有 PSV2-01～PSV2-07 已完成。目标回归 77/77 通过；全量 unittest 525/525 通过；`git diff --check` 通过。全量测试包含预期的故障注入日志和既有 DeprecationWarning/ResourceWarning，命令退出码为 0。已生成实施报告；无数据库迁移、配置或依赖变更。未调用真实模型/QQ，未部署、提交或 push。
