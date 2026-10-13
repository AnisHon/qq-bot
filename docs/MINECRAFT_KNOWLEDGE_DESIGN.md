# Minecraft 知识与工具服务设计

状态：设计稿。除当前 SFM PDF 检索及通用 SQLite FTS5 / Chroma 基础设施外，本文描述的 Minecraft / GTNH 能力均为未来能力。

## 1. 当前基础、缺少的数据和能力边界

| 项目 | 状态 | 代码 / 证据 |
|---|---|---|
| SFM PDF 文本切分与索引 | 已有基础能力；当前源文件或部署数据不在本次核对范围内 | scripts/index_sfm_pdf.py；app/rag.py；app/vector_rag.py |
| SQLite FTS5 | 已用于 RAG 关键词查询；RagChunk 目前含 chunk_id、source、page、title、content、score | app/rag.py |
| Chroma + sentence-transformers | 已用于语义检索；collection 按 document_name 命名 | app/vector_rag.py |
| GTNH Wiki / 本地 Markdown Wiki | 未准备、未索引 | 当前项目没有 GTNH 文档导入器或数据集 |
| GTNH 任务书文本 | 未准备、未导出、未索引 | 当前无任务书数据管线 |
| Minecraft / GTNH 配方数据 | 未准备、未导出、未校验 Schema | 当前无 Recipe 模型、导入器、查询服务 |
| gtnh-toolkit | 未核实是否安装或可用 | 不作为前置假设 |
| 本地 Bot 工作区 Git 检索 | 未实现 | 当前 GitHub 工具检索公开远端仓库；app/tools/external.py |
| SFM 编译器 / 验证器 | 未实现 | 当前只有 SFM PDF 检索和普通 Agent 回答；无本地 Parser / Validator |

因此先建通用接口并用人工 Fixture 验证，再单独做 P6 真实数据接入。任何用户可见文案、测试报告和能力清单都必须标明数据集版本及验证等级。

## 2. 通用文档摄取与检索

### 2.1 建议职责

- DocumentIngestor：发现来源、计算 hash、解析为规范段落，不持有数据库生命周期。
- DocumentParser：按格式解析成 ParsedDocument，首批实现 Markdown；后续接 PDF、HTML、Wiki / 任务书导出。
- Chunker：按章节与自然段切分，保留字符 / 行 / 页位置，不跨章节拼接；chunk 长度、overlap 可配置。
- DocumentIndex：调用现有 SQLite FTS5 和 Chroma 的薄适配层，实施 generation 更新、删除和一致性状态。
- DocumentKnowledgeService：暴露 search / status / rebuild / delete 接口，组装混合排名和引用。

不要为每个输入格式定义独立索引库。Parser 输出同一个含 provenance 的中间表示，Recipe 数据不进入此管线。

~~~python
DocumentSource(
    source_id: str, source_uri: str, source_type: str,
    corpus_id: str, pack_id: str | None, pack_version: str | None,
    game_version: str | None, source_revision: str | None,
    license_id: str | None, content_hash: str,
)
DocumentChunk(
    chunk_id: str, document_id: str, generation: str,
    title: str, section_path: tuple[str, ...], content: str,
    locator: PageLocator | LineLocator | AnchorLocator,
    metadata: dict[str, str],
)
~~~

chunk_id 由 source_id、content hash / generation 和段落定位稳定生成；同一原文相同 generation 可幂等 upsert。引用定位不使用索引行号作为永久身份，采用 source revision + 原始行 / 页 / anchor。

### 2.2 元数据与检索

文档元数据至少记录 corpus_id、source_id、source_type、pack_id、pack_version、game_version、source_revision、content_hash、license / usage note、parser_version、chunker_version、index_generation、indexed_at 和可用状态。来源过滤在 SQLite 和 Chroma 两路查询中保持一致。

混合检索流程：

1. 输入清洗与查询语言 tokenization。
2. SQLite FTS5 BM25 / 精确关键词检索与 Chroma vector 检索并行运行，二者各取候选 top-k；每一路设置超时 / 候选上限。
3. 同一 active generation、pack_id / version / corpus 过滤后按 chunk_id 去重。
4. 用 RRF 统一排序：score(d) = Σ 1 / (k + rank_i(d))，不直接混合 FTS BM25 与 Chroma cosine 的量纲。
5. 返回证据对象，保持来源路径、标题层级、页码 / 行范围、revision 和排序分数来源。引用 renderer 生成可读引用，不能只显示模糊“本地知识库”。

SQLite FTS5 可作为已有关键词检索基础，Chroma 继续承担近义语义召回；参考 [SQLite FTS5](https://www.sqlite.org/fts5.html) 与 [Chroma Python API](https://docs.trychroma.com/reference/python)。当前 app/handlers/llm_handler.py 的 _maybe_search_sfm 将 FTS 和向量命中按 chunk_id 拼接，没有 RRF；这是需改进的检索质量问题，而不是缺少一套新框架的证据。

查询接口区分：

- NotIndexed(corpus_id)：数据集存在但索引为空 / 尚未完成。
- DataUnavailable(corpus_id)：指定数据源或 corpus 根本不存在或未配置。
- VersionMismatch(requested, available)：有数据但版本键不匹配；只报告存在何种版本，不自行拿来回答。
- NoResults：目标 corpus / version 的有效索引中查询无命中。
- Partial：一路检索失败但另一条有结果，标明降级来源。
- ToolExecutionError：DB、embedding、索引或解析器执行失败。

### 2.3 增量更新、删除与恢复

P0-07 与此处职责不同。P0-07 只修复当前 SFM PDF 的已有 Chroma collection：保留 document-name collection 与旧 API，新增按 document 清除全部 vector IDs 的替换 / 删除语义，并为当前 SQLite RagStore 提供单文档 FTS / chunks / metadata 删除；写新 chunks 前计算 embedding，再清除该文档全部旧 vectors 并写入新 IDs，正常成功完成后不再命中消失的尾部 chunk。空 chunks 表示替换为空；显式删除清理 SQLite 与 Chroma。两端不共享事务，只有两端都成功才报告删除完成；失败明确报告 partial / error，重试删除幂等。它不实现 generation、pending cleanup 或 SQLite / Chroma 原子提交；向量写失败可能导致旧向量已删而新向量缺失，此时依赖 FTS 降级或重跑索引恢复，不得声称具备完整回滚。

P2-03 才对通用多来源索引实施以下 generation 生命周期：

1. 发现变更时先解析到新 generation，验证 chunk 数、引用 locator、大小与 hash。
2. SQLite 事务写入新文档元数据、chunk、FTS 行，但标记 generation 为 building；旧 active revision 仍可查询。
3. Chroma 写入含 document_id / generation / pack filters 的新 chunk。超时或异常记为可重试，不把一半结果标成可用。
4. 检查 SQLite / FTS / Chroma 的 chunk IDs 集合；通过后将 active generation 原子切换为新版本。
5. 清理旧 generation，包括已消失的尾部 chunk；清理失败进入 pending cleanup。查询端只读 active revision，不返回旧片段。
6. 删除 document 时先 tombstone，再清理两侧索引；若重启恢复未完成操作，继续幂等清理。

SQLite 和 Chroma 本身不能共享事务，所以“先全删再写”可能令系统停机或留下旧向量。采用 generation / active pointer 是避免混查的应用层一致性策略。支持空 collection 和 Chroma 不可用时退化为 FTS5。

### 2.4 Fixture 范围

人工 Markdown Fixture 可覆盖：标题层级、行范围、front matter 元数据、两个模拟版本过滤、英文 / 中文关键词、FTS 查询、Fake Embedder 的向量召回、RRF、引用定位、重复导入幂等、内容更新导致旧 chunk 删除、删除文档、空 corpus、缺少版本、Chroma 写入失败和重建恢复。

这些测试仅证明通用 ingestion / retrieval 契约，不证明 GTNH 文档已被获取、来源许可已核实、术语分词质量已校准或回答覆盖率可用。

## 3. 结构化配方服务（Contract-first）

### 3.1 模型边界

精确配方检索使用 SQLite 规范化结构和查询服务。向量检索可用于解释术语或查文档，不作为 Recipe 的事实数据源。所有 recipe 行必须属于不可变 dataset revision，且 dataset key 精确含 pack_id / pack_version / game_version；来源记录包含 exporter、exporter_version、source_revision、license / attribution、imported_at、schema_version 和内容 hash。

建议核心模型：

~~~python
PackVersionKey(
    pack_id: str, pack_version: str, game_version: str,
)
RecipeDataset(
    dataset_id: str, key: PackVersionKey, source_id: str,
    source_revision: str, importer_version: str,
    schema_version: int, content_hash: str, status: str,
)
RawRecipeRecord(
    source_record_id: str | None,
    source_locator: str | None,       # source path + line / JSON pointer / record key
    raw_payload_ref: str,             # exact bytes or immutable source blob reference
    raw_hash: str,
    parsed_fields: dict,
    unknown_fields: dict,
)
Ingredient(
    kind: str,                   # item_exact | fluid_exact | tag | ore_dictionary | predicate | source_extension
    value: str | None,           # exact ID / tag / ore name; predicate may have no scalar value
    amount: Decimal | None,
    unit: str | None,
    role: str,                   # material | tool | catalyst | condition
    consumed: bool | None,       # unknown is not silently treated as true
    constraints: dict | None,    # understood NBT / components / predicate constraints
    interpretation: str,         # exact | conditional | unsupported
    source_locator: str | None,
)
RecipeOutput(
    kind: str, value: str | None, amount: Decimal | None,
    probability: Decimal = Decimal("1"),
    constraints: dict | None = None,
    interpretation: str = "exact",
)
Recipe(
    recipe_id: str, key: PackVersionKey, machine_ids: tuple[str, ...],
    inputs: tuple[Ingredient, ...], outputs: tuple[RecipeOutput, ...],
    energy_eut: int | None, duration_ticks: int | None,
    conditions: dict, source_record_id: str, source_locator: str | None,
    raw_source_hash: str, raw_payload_ref: str,
    unknown_fields: dict, status: str,  # accepted | conditional | quarantined
)
~~~

Recipe 保留 RawRecipeRecord 的引用，不把原始正文复制到每个规范化行。raw_payload_ref 只能指向当前受控导入数据集中的不可变记录；来源定位和 hash 要足以复现字段映射，并支持取得真实样本后审阅 / 演进 Schema。

Schema 要求：

- 一项输入 / 输出是一行实体，不能将多输入压成一个字符串。核心 Schema 只规范化已理解的共同字段：精确 item / fluid ID、tag、旧版 Ore Dictionary、Ingredient Predicate 必须区分；不能把非精确条件降格成 item ID。
- NBT / item component 作为约束保留；只有 importer 能证明并实现条件语义时才可匹配，否则 interpretation=unsupported。工具 / 催化剂通过 role 和 consumed=false 表达；若来源无法判断是否消耗，则 consumed=null，并标记 conditional / unsupported。
- 每条记录保留来源原始 payload（原文件记录或受控 blob 引用）、raw hash、source-native record ID 和行 / JSON pointer / 文件定位。无法解析的扩展字段进入 unknown_fields；未知关键字段、条件不完整或数量单位无法解释时 quarantine，不能作为可查询精确配方。非关键未知字段也必须在导入报告保留。
- RecipeOutput 同样保留 tag / predicate / 条件、概率和解释状态；未知输出条件不可伪装成确定 item / fluid 输出。
- 查询结果附 match_quality：exact（精确 ID 直接匹配）、conditional（仅在已知且版本化 resolver 解析 tag / predicate 后匹配）、unsupported（条件无法解释，仅供诊断 / 导入审计）。默认 production_recipes 只按 exact ID；没有 resolver 时，不将 tag 或 predicate 当成 item_id 命中。
- probability 是 [0,1] 范围 Decimal；多结果各自带独立概率和数量。
- 机器标识可支持一个 recipe 多 machine；如果真实导出源是多机复用，由 importer 规范成关联表，不能假设源数据只有一个机器。
- energy_eut / duration_ticks 缺失保留 NULL，并在结果声明未知；不填模型猜测值。
- recipe_id 优先保留 source-native id；若来源无稳定 id，用 canonical source row hash，并保留重复条目来源位置。不得只按 outputs 去重，避免丢失机器 / 条件不同的配方。

### 3.2 Importer / Repository / Query API

~~~python
class RecipeSource:
    def manifest(self) -> RecipeSourceManifest: ...
    def iter_records(self) -> Iterable[RawRecipeRecord]: ...

class RecipeImporter:
    def validate(self, manifest, records) -> ImportReport: ...
    def import_dataset(self, source, target_key) -> ImportReport: ...

class RecipeRepository:
    def dataset_status(self, key: PackVersionKey) -> DatasetStatus: ...
    def find_producing(self, key, item_id) -> list[Recipe]: ...
    def find_consuming(self, key, kind, identifier) -> list[Recipe]: ...
    def find_by_machine(self, key, machine_id) -> list[Recipe]: ...
    def get_recipe(self, dataset_id, recipe_id) -> Recipe | None: ...

class RecipeQueryService:
    def production_recipes(self, key, item_id, match_mode="exact_only") -> QueryResult: ...
    def machine_recipes(self, key, machine_id) -> QueryResult: ...
~~~

QueryResult 中每个候选以 RecipeMatch(recipe, match_quality, matched_conditions) 包装；match_quality 仅为 exact 或 conditional。unsupported 记录留在 ImportReport / 诊断接口，不作为用户查询的正向结果。

Importer 先做 dry validation：manifest / version 必填；ID 和数量可解析；概率范围有效；machine / recipe 引用完整；units 明确；外键和唯一键无冲突；将未知字段、无效记录、接受 / 拒绝计数和 sample errors 输出报告。错误记录按 source locator 隔离，不能静默跳过。导入先写 staging dataset，验证通过后事务切换 active dataset；失败保留上一版可查询数据。

RecipeQueryService 对请求里的 pack/version 必须明确。如果用户没有说版本且当前群未配置唯一默认值，先询问或返回 NEED_VERSION。dataset 不存在为 DataUnavailable；有其他版本数据而目标版本无数据为 VersionMismatch；dataset 存在但无该物品命中为 NoResults。严禁自动选择“最接近版本”。精确 ID 查询与条件查询显式区分；tag / Ore Dictionary / predicate resolver 缺失时不能提升为精确命中；quarantined 记录不进入生产查询。

### 3.3 Agent Tool Schema

- recipe_lookup：required pack_id、pack_version、game_version、item_id；optional direction（production / consumption / either）、match_mode（exact_only 默认；需显式允许 conditional）、machine_id、limit。结果逐项标明 exact / conditional；不能将 unsupported 条件返回成肯定答案。
- recipe_machine_lookup：required 精确 dataset key 和 machine_id；optional limit。
- ToolResult 包含 status、Recipes 结构化数据、dataset/source Evidence、截断标志。
- 回复模板从结构化结果生成多输入 / 多输出、物品 / 流体、概率、消耗 / 非消耗、能耗与时长。不完整字段写“数据源未提供”。

### 3.4 必须覆盖的人工 Fixture

Fixture 仓库包括：

1. 一个物品有两种生产 recipe。
2. 一个 recipe 有多个物品输入。
3. 一个 recipe 同时含物品与流体输入。
4. 一个输出有非 100% 概率。
5. 一个输入是非消耗工具 / 催化剂。
6. 同一输出物品在两个 pack version 中 recipe 不同，结果必须隔离。
7. 查询不存在物品返回 NoResults。
8. 查询未导入的 pack/version 返回 DataUnavailable 或 VersionMismatch，绝不返回近似版本。

测试需另覆盖 malformed record、缺来源、未知单位、重复 ID、多机关联、概率边界、导入中断、tag / Ore Dictionary / predicate / NBT 或 component 约束、未知关键字段 quarantine、非关键未知字段 raw payload 保留、exact 与 conditional 查询分离。Fixture 完全人工构造，只验证 Schema 和查询逻辑，不代表 GTNH 真配方，也不证明 gtnh-toolkit 或任一 exporter 可用。

## 4. 本地 Git 仓库检索

### 4.1 服务接口

RepositorySearchService 只允许读取部署配置中列出的仓库 root。每个 root 有稳定 repo_id，查询创建一个 immutable SnapshotRef（HEAD commit SHA 或管理员配置 commit）。一次回答的搜索和行读取都固定使用同一 SHA，引用包含 repo_id、commit、相对路径和行范围。

最小接口：

- search_paths(repo_id, query, snapshot, limit)：路径 / 文件名搜索。
- search_text(repo_id, query, snapshot, filters, limit)：受范围限制的纯文本搜索。
- read_file(repo_id, relative_path, start_line, end_line, snapshot)：按行读取并截断。
- search_symbols(...)：可选后续能力；先用简单语言级符号扫描，只有项目规模或使用场景需要时再增 AST parser。

### 4.2 文件安全

- model 只传 repo_id 和相对路径，不能提交任意绝对路径。
- 解析真实路径并验证仍在 root 下；拒绝 ..、绝对路径、符号链接逃逸、submodule 出界和竞态变化。打开文件时再验证最终 inode / realpath。
- 默认排除 .git、.env*、密钥和凭证文件名、私钥扩展名、数据库、日志、二进制、大文件，以及管理员额外 denylist。文件输出和搜索摘要应用总字节限制。
- 读取 commit 对应 Git object，不在用户可写工作区上执行任意 checkout；本地工作树变更应明确选择只读工作树 snapshot 或 HEAD SHA。
- 不执行仓库脚本、不自动安装依赖，不跟随仓库内 URL。

先完成固定目录与读取边界，再开放 ToolRegistry 中的 repository_search / repository_read。现有 public GitHub 工具继续保留。

### 4.3 临时仓库测试

用 unittest 的 TemporaryDirectory 创建临时 Git repo 与本地 commit（无网络）。覆盖 path/text 命中、固定 commit、行段读取、空文件、UTF-8 / 二进制、文件大小上限、.env / key 排除、目录白名单、../ 越界、符号链接逃逸、已删除文件和超长结果引用。

## 5. Web Search 防护与证据

保留 app/tools/external.py 中已有搜索功能，通过 WebSearchService 包装并逐步迁移到 Registry。当前 fetch_web_page 使用自动跟随 redirect 的普通 HTTPX client，Jina fallback 将原 URL 传给远端 Reader；两者均不能作为安全抓取出口。安全抓取器使用独立 HTTPX client（trust_env=False、follow_redirects=False），每跳规范化 URL、解析所有 A / AAAA、拒绝非公网 / 保留地址后，把本次批准 IP 固定为实际 TCP 连接目标，同时原样保留 hostname 用于 HTTPS SNI、Host 和证书校验。受限 custom transport / connector 是应用策略边界；如果固定 IP 只能依赖不稳定的 httpcore 内部 API，需锁版本并以真实受控连接集成测试证明，无法证明时 fail closed 或改用在代理端实现同等 DNS-to-socket pinning 的可信出站代理。HTTP 代理、环境代理和第三方 Reader 不得绕开检查；未经验证 URL 不交给 Jina。限制 URL scheme/port、重定向数、总超时、原始 body 与解压 body 大小。

统一输出 SearchEvidence：查询、来源 title、原始 URL、最终 URL、fetched_at、内容 hash（可选）和摘要。工具返回引用不应把无法核验的文字冒充来源。缓存仅缓存合规的公开结果，按 query / URL / TTL 和策略版本分区；权限拒绝、失败、私有 URL 不缓存为成功结果。

安全案例至少覆盖：localhost literal、IPv4 / IPv6 / IPv4-mapped IPv6 私网、link-local、metadata 地址、混合公私 DNS、IDNA、URL 编码及十进制 / 八进制 / 十六进制等特殊 IP、DNS rebinding、每跳 301/302 到私网、重定向循环、非 HTTP scheme、超大原始 body / 解压 body、超时和证书失败。MockTransport 测策略分支，Fake Resolver 测名称解析 / 地址归一化，受控 connector 测实际 peer IP 固定、Host / TLS SNI / 证书主机名；Fake Resolver 或 MockTransport 单独通过不证明生产网络目标安全。部署 egress 限制仍需单独验证。

## 6. SFM 编程 Agent

### 6.1 上游已观察证据与未确认项

对 TeamDman/SuperFactoryManager 公共仓库的只读网页检查（2026-10-12）得到：

- 仓库介绍 SFM 4 为 Minecraft 工厂物流 DSL，公开仓库页面标注 MPL-2.0。它包含多个 Minecraft 版本分支 / tag，应绑定目标 Minecraft/SFM 版本，而非仅写“最新版”。[上游仓库与许可](https://github.com/TeamDman/SuperFactoryManager)
- 1.19.2 分支存在 ANTLR grammar platform/minecraft/src/main/antlr/sfml/SFML.g4，也有 VS Code 语法高亮 grammar 副本；语法高亮副本不能替代编译器验证。[ANTLR grammar](https://github.com/TeamDman/SuperFactoryManager/blob/1.19.2/platform/minecraft/src/main/antlr/sfml/SFML.g4)
- 1.19.2 的 ProgramBuilder.build() 创建 SFMLLexer / SFMLParser、装配 ListErrorListener、解析 program、调用 ASTBuilder、执行 resource type 检查，并返回 ProgramBuildResult / ProgramMetadata errors。错误类型包括 Minecraft TranslatableContents；资源类型检查会访问 SFM registry，部分检查在未加载游戏时跳过。[ProgramBuilder](https://github.com/TeamDman/SuperFactoryManager/blob/1.19.2/platform/minecraft/src/main/java/ca/teamdman/sfml/program_builder/ProgramBuilder.java)
- 同分支 ASTBuilder 继承生成的 ANTLR visitor，并引用 SFMConfig、Mojang DataFixer Pair 等；SFM 主项目位于平台 / Minecraft 构建内。[ASTBuilder](https://github.com/TeamDman/SuperFactoryManager/blob/1.19.2/platform/minecraft/src/main/java/ca/teamdman/sfml/ast/ASTBuilder.java)
- 官方 examples 目录确实有 SFM 示例源码。[上游 examples](https://github.com/TeamDman/SuperFactoryManager/tree/1.19.2/examples)

这些网页证据没有验证 Parser 可独立作为 CLI / Java library 启动，也没有运行构建确认是否需要 Minecraft runtime、如何输出稳定机器可读诊断、GTNH 实际版本对应哪个分支，或许可证是否允许将二进制随 Bot 分发。因此这些都保持“待验证”。本项目当前没有 Parser，也没有可用的 SFM 验证器。

### 6.2 Level 1：文档与示例检索

将 SFM 文档、指定版本源码和人工审核示例作为 DocumentSource，metadata 包含 sfm_version、minecraft_version、来源 commit、许可 / attribution、文件路径和行号。先接通通用 DocumentKnowledgeService。搜索语法 / 命令 / 示例时按精确版本过滤，并引用原始文件行范围。不能把代码高亮 grammar 说成验证结果。

### 6.3 Level 2：生成与验证

定义稳定接口：

~~~python
SFMValidator.validate(
    source: str,
    target: SFMTargetVersion,
    timeout_seconds: float,
) -> ValidationResult

ValidationResult(
    status: VALID | INVALID | UNAVAILABLE | VERSION_UNSUPPORTED | TIMEOUT | ERROR,
    validator_id: str,
    validator_version: str | None,
    target_version: SFMTargetVersion,
    diagnostics: tuple[Diagnostic, ...],
)
Diagnostic(
    severity: str, code: str | None, message: str,
    line: int | None, column: int | None, end_line: int | None,
)
~~~

真实实现需要锁定 SFM release / commit、版本矩阵和依赖 hash；在隔离子进程中以 stdin 提交有限长度源码，输出 JSON；网络禁用、工作目录临时、只读依赖、限制资源。优先评估官方 ProgramBuilder / Parser 对接或基于官方 grammar 的轻量解析器；只有语法层级的 ANTLR parse 不代表 SFM AST / resource type 语义验证完成。

没有真实兼容 Validator 时只注入 FakeValidator，输出 validator_id=fake，所有状态限定为工作流模拟；界面明确显示“未执行真实 SFM Parser”。

### 6.4 Level 3：有界修复流程

1. 根据用户描述与检索证据生成初版程序。
2. 调用 SFMValidator；若 UNAVAILABLE / VERSION_UNSUPPORTED，停止修复并说明缺少哪项依赖。
3. VALID 时附目标版本、验证器 ID / 版本和诊断为空的结构化结果。
4. INVALID 时只把带行列号的诊断和当前源码交模型做修复；最多 2 轮，重用相同 deadline、token / 费用预算和结果字节预算。
5. 每轮源码先通过长度和字符限制，再次验证。错误或超时停止，不无限自修。
6. 最终交付源码、版本、Validator 状态、诊断和引用。禁止执行、上传或部署程序。

Fake Validator 的测试覆盖修复成功、持续失败、超时、取消、目标版本不支持、诊断行列映射、预算耗尽和伪造状态不可冒充；真实语法能力要等 P6 用指定版本的真实 SFM 样例验证。

## 7. 能力交付标签

推荐用户可见状态：

- 基础设施可用 / 数据未导入：SQLite / 服务可启动，但没有目标数据。
- Fixture 验证：仅人工样本通过。
- 真实数据已验证：明确列出数据集、pack/version、来源 commit / hash 和验证日期。
- 验证器未接入：能检索文档或生成草稿，不能说已通过语法检查。
- 部分结果：检索/索引某一路降级，引用和缺失范围明确。

## 8. 测试分层

每次实施任务先报告对应单元测试；阶段结束再报告阶段回归。结果使用五种标签分别汇总：Mock、人工 Fixture、本地组件集成、真实数据集验证、外部依赖未验证。Fake / Fixture 通过不能提升为真实 GTNH 配方、GTNH 文档或真实 SFM Parser 能力。

| 测试类型 | 可证明 | 不可证明 |
|---|---|---|
| 文档 Fixture | Parser、切片、元数据、过滤、FTS / Fake Vector、RRF、locator、更新删除逻辑 | GTNH 语料覆盖、许可、真实术语效果 |
| Recipe Fixture | Schema、ID、输入输出、概率、非消耗工具、版本隔离、状态码与查询 | GTNH 配方正确或数据完整 |
| 临时 Git repo | 白名单、安全读取、固定 commit、引用、大小 / 敏感文件策略 | 外部仓库 API 或真实项目索引质量 |
| Mock Web / Fake Resolver | SSRF 与网络限制逻辑、错误分类、缓存、证据序列化 | 生产 DNS、egress / proxy 实际隔离 |
| Fake Validator | 生成—验证—修复协议、限次、预算、诊断序列化 | SFM grammar 或语义正确 |
| 真实数据集 / Parser | 指定版本和来源下的集成能力 | 其他版本 / 来源自动兼容 |

## 9. 参考

- 现有项目：app/rag.py、app/vector_rag.py、app/handlers/llm_handler.py、app/tools/agent.py、app/tools/external.py、scripts/index_sfm_pdf.py。
- [SFM 上游仓库](https://github.com/TeamDman/SuperFactoryManager)（只读网页检查；独立运行与目标版本适配仍待验证）。
- [SQLite FTS5](https://www.sqlite.org/fts5.html)、[Chroma Python API](https://docs.trychroma.com/reference/python)。
