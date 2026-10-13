# Minecraft Agent 数据与能力验证报告

**核查日期：** 2026-10-13

**范围：** P7-03 能力状态核对。此报告描述本地代码和测试证据；没有连接生产 NapCat、模型服务、真实搜索 API、GTNH 数据源或 SFM Parser。

## 能力状态

| 能力 | 当前实现与验证等级 | 用户可见状态 / 解释 | 真实能力状态 |
|---|---|---|---|
| QQ / OneBot 接入 | 单元及 FastAPI ASGI 测试；没有真实 NapCat 集成 | 空回调密钥时拒绝回调；生产 NapCat header 与 rollout 未验证 | 生产配置未验证 |
| 自研 Agent Loop、原生 Function Calling、Tool Registry | Fake LLM 与本地 Registry 集成测试 | 每个工具结果与 `tool_call_id` 对应；工具结果状态独立保留 | 没有真实模型 Provider 测试 |
| 通用文档检索 | 人工 Markdown；临时 SQLite / FTS5 本地集成；混合检索路径另以 Fake Vector 覆盖 | `DATA_UNAVAILABLE` 表示未配置索引；`NOT_INDEXED` 表示来源尚无可检索片段；版本不匹配返回 `VERSION_MISMATCH` | 未对真实 GTNH 文档或真实 Chroma corpus 验证 |
| GTNH Wiki / 任务书 RAG | 没有项目数据集；仅有标为人工 Fixture 的通用摄取 / 检索测试 | 不应声称能查询 GTNH 具体资料；缺少数据时返回不可用 / 未索引状态 | **未实现真实语料能力；P6-01 外部阻塞** |
| 结构化 Recipe 服务 | 人工 JSON Fixture 经 Recipe Importer 写入临时 SQLite，再由 Agent 工具查询 | 未导入的 pack/version 返回 `DATA_UNAVAILABLE`；只存在其他版本时返回 `VERSION_MISMATCH`；已激活版本无精确命中返回 `NO_RESULTS`；不会回退到其他版本 | **没有真实 GTNH 配方或已验证 exporter；P6-02 / P6-03 外部阻塞** |
| 本地 Git 检索 | 临时 Git 仓库 Fixture；本地 repo、固定 commit、路径限制和读取边界测试 | 仅对配置的白名单仓库开放只读查询；无仓库配置时明确不可用 | 真实目标仓库未配置或验收 |
| GitHub / Web Search | 工具契约、Fake / Mock 和受控 HTTP 安全测试；没有访问真实远端 API | 可报告工具错误 / 无结果；本轮没有实测远端服务可用性或搜索质量 | 外部服务集成未验证 |
| SFM 文档 / 示例检索 | 人工 Markdown Fixture + SQLite FTS 本地集成；按 SFM 与 Minecraft 版本精确过滤 | 缺来源时为 `NOT_INDEXED`；仅有其他版本时为 `VERSION_MISMATCH`；无配置的文档存储时为 `DATA_UNAVAILABLE` | 没有官方 SFM 文档或真实示例数据 |
| SFM 生成与验证 | Fake LLM / Fake Validator 工作流；合成 transcript 和有界修复测试 | 默认没有 Parser 时，Agent 工具外层状态为 `DATA_UNAVAILABLE`，结果内层工作流状态为 `unavailable`，且不检索或调用模型；Fake 结果明确为 `simulated_valid` / `simulated_invalid`，并标记 `is_real_validation=false` | **没有真实 Parser / Compiler；P6-04 外部阻塞** |
| SQLite、FTS5、向量层 | SQLite 临时数据库与 FTS5 有本地组件测试；向量行为使用 Fake / Mock 覆盖 | 空库正常返回明确状态 | 未对真实 Minecraft corpus 执行 Chroma / embedding 集成验收 |

P6-01 缺少有许可的 GTNH Wiki 与任务书；P6-02 / P6-03 缺真实 recipe 样本、导出器、目标版本和可验证字段；P6-04 缺目标 SFM 版本、可独立运行的 Parser / Compiler 与真实样例。`gtnh-toolkit` 没有被本次核查认定为已安装或可用。上述事项不阻止 P0–P5 的 Mock、Fixture、本地组件测试，但相关真实领域能力在 P6 验收前不得对外标为可用。

## 测试等级定义

- **Mock：** 替身实现隔离网络、模型、向量后端或解析器边界。它验证本地调用合同，不验证真实 Provider 或外部依赖行为。
- **人工 Fixture：** 仓库内人工编写的样本，用于验证解析、存储、过滤、版本隔离和来源定位。它不证明样本代表任何真实整合包、GTNH 或 SFM 版本。
- **本地组件集成：** 真实本地代码组件通过临时 SQLite / FTS5、临时 Git 仓库、Tool Registry 和 Agent Loop 协同运行。仅覆盖被明确列出的组件。
- **真实数据集验证：** 对来源、许可、版本、内容 hash 和覆盖范围均可追踪的真实领域数据执行导入和查询。目前没有此等级的 GTNH 或 SFM 结果。
- **外部依赖未验证：** 数据源、Provider、Parser 或 exporter 尚未取得 / 执行 / 兼容性验证；不得把 Mock / Fixture 结果升级为该依赖已可用。

## 测试证据

P7-02 的跨层集成使用 Fake LLM、Fake Validator、合成 SFM Markdown、合成 Recipe JSON 和临时 SQLite。它逐个检查 `tool_call_id` 对应的 SFM 版本错配、Recipe 缺失版本、Fixture v1 / v2 的配方和 EU/t、空 SFM / Recipe 库状态、引用以及工具响应闭环。并发用例使用 `MessageTaskRuntime` 与临时数据库检查两个 worker 的并发上限。

P7-02 跨域集成与 P7-03 状态映射定向回归（包括新增的无 Parser Agent Tool 外层 / 内层状态断言）：

```text
.venv/bin/python -m unittest tests.test_agent_integration tests.test_sfm_knowledge tests.test_hybrid_retrieval tests.test_recipe_query tests.test_recipe_importer tests.test_recipe_repository tests.test_sfm_workflow tests.test_task_runtime tests.test_tool_registry tests.test_tool_compat -q
Ran 103 tests — OK
```

P7 全量本地回归（含 P7-03 状态映射）：

```text
.venv/bin/python -m unittest discover -s tests -q
Ran 283 tests — OK
```

全量测试包括两项受控 loopback TCP/TLS connector 测试；在受限 sandbox 中不能绑定 loopback，使用获准的本地测试权限重跑同一命令通过。它们是本机受控连接器验证，不是公网目标或生产代理集成。输出中的异常日志来自测试注入的错误路径；全量结果亦有 FastAPI / HTML parser deprecation warnings 和 SQLite ResourceWarning，这些警告不改变测试通过状态。

相关状态与 Fake 边界的代码证据：

- `tests/test_agent_integration.py`：跨 Tool Registry / Agent Loop、SQLite FTS / Recipe 服务、逐 `tool_call_id` 版本与空数据断言。
- `tests/test_hybrid_retrieval.py`：人工文档、FTS / Fake Vector 混合检索、版本过滤、引用和空索引状态。
- `tests/test_recipe_query.py`、`tests/test_recipe_importer.py`：合成 Recipe Schema、导入、精确 / 条件查询、来源和版本状态。
- `tests/test_sfm_knowledge.py`：SFM 版本精确过滤和 `NOT_INDEXED` / `VERSION_MISMATCH` / `DATA_UNAVAILABLE`。
- `tests/test_sfm_validator.py`、`tests/test_sfm_workflow.py`：默认 Parser 不可用、Agent Tool 外层 `DATA_UNAVAILABLE` / 内层 `unavailable`、无模型调用 / 检索、Fake 状态仅为 simulated、诊断和有界修复。
- `tests/test_local_git.py`：临时 Git 仓库的本地只读检索测试。
- `tests/test_web_security.py`：MockTransport、Fake Resolver 和受控 loopback TCP/TLS 安全边界；没有真实网页抓取。

## Fixture 来源指纹

以下 SHA-256 只固定仓库中人工 Fixture 的当前字节内容，便于复现测试；它们不证明任何外部来源、许可或领域真实性。

| Fixture | 声明 | SHA-256 |
|---|---|---|
| `tests/fixtures/documents/markdown_fixture.md` | 人工文档 Fixture，不是 GTNH 文档 | `54ddc761256f117e828e03c72ccb1dcdc5c8e83bf474d091ce44e8121862d67d` |
| `tests/fixtures/documents/second_pack_fixture.md` | 人工文档 Fixture，不是 GTNH 文档 | `e0d8f53978b8e0d0c4005bc17d64e777a9556cbce5e2befdacb893daac057ae4` |
| `tests/fixtures/recipes/synthetic_recipe_set.json` | 手工合成 Recipe contract 数据，不是 GTNH / exporter 数据 | `11dc23db6293637427995a0c80ec0e1afbed7fb9c066cba45e74576151f668af` |
| `tests/fixtures/sfm/synthetic_sfm_retrieval.md` | 人工占位文档，不是官方 SFM 文档 | `ea33c5d1a129afde214c1186f639df7bb6957e04269ab83d29a81f0f8158052b` |
| `tests/fixtures/sfm/synthetic_program.sfm` | 合成工作流样本，不是 SFM 程序 | `954a8e01bcd0957e72b349a9c5e15078bdba6058d8ae7cb0f7a09af77b7fbdd0` |
| `tests/fixtures/sfm/synthetic_invalid_program.sfm` | 合成工作流样本，不是 SFM 程序 | `46645283831c6fcc2cdb8e4f5833ef4f230fb6ce8cf0da01f922201939ee0f70` |

临时 Git 仓库、其余内联 Markdown / Recipe 样本与 Fake Validator 响应由测试运行时创建或声明，没有独立的真实数据样本清单。

## 发布门与后续验证

目前可准确发布的状态仅为：Bot / Agent 的代码与 Mock 回归通过；通用文档、Recipe 和 SFM contract 使用人工 Fixture / 本地组件得到测试；本地 Git 边界使用临时仓库测试。生产 NapCat、真实模型 Provider、外部 Web 搜索服务、目标 GitHub 内容、真实 Chroma 语料、GTNH 文档 / 配方和真实 SFM Parser 均未由这些测试验收。

只有取得许可并锁定来源与版本后，才执行 P6 的真实数据检查。Recipe 报告须记录 exporter 版本、数据集 / 游戏版本、字段覆盖、导入错误、原始记录 hash 和查询核对；SFM 报告须记录 Parser 来源 commit / 依赖 hash、目标版本矩阵、运行边界、真实示例和结构化诊断覆盖。验证失败或数据缺失时，保留 `DATA_UNAVAILABLE` / `NOT_INDEXED` / `VERSION_MISMATCH` / `UNAVAILABLE` 等状态，不能依据 Fixture 推断真实语料可用。
