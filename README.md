# QQ DeepSeek Bot

这是一个基于 NapCat / OneBot 11 和 DeepSeek function calling 的 QQ 群聊 agent。

## 方案

- 接入层：NapCat / OneBot 11
- 业务层：Python + FastAPI
- 存储：SQLite
- LLM：DeepSeek OpenAI 兼容接口
- 可选 LLM：NVIDIA NIM OpenAI 兼容接口；默认 `openai/gpt-oss-120b`
- 可选 LLM：阿里百炼兼容 OpenAI 接口；provider 名称为 `qwen`
- 可选 LLM：New API OpenAI 兼容中转；provider 名称为 `newapi`
- 可选 LLM：CCTQ New API OpenAI 兼容中转；provider 名称为 `cctq`
- Agent 工具：本地知识、网页/新闻、网页正文、天气、GitHub 仓库检索
- SFM RAG：`sfm.pdf` 预处理为 SQLite FTS 索引，由 agent 按需调用

当前本机探查结果：

- `/opt/natcapqq` 使用 NapCat 目录结构
- `/opt/natcapqq` 使用 NapCat / OneBot 11
- 当前采用 OneBot HTTP client 回调 + HTTP server API
- NapCat 监听 `127.0.0.1:3000`，bot 监听 `0.0.0.0:8080`

因此这里采用：

1. NapCat HTTP client 推送事件到本 bot 的 `/onebot/event`
2. 本 bot 通过 NapCat HTTP server 调用 `send_group_msg`

## 目录

```text
app/
  main.py                 # HTTP 入口、任务调度、进度提示
  natcapqq_adapter.py     # OneBot 事件/消息段和 API
  qq_context.py           # 引用、@、多人群聊上下文选择
  qq_dispatcher.py        # 分段、串行发送、去重和状态节流
  bot_service.py          # 命令、上下文组装、业务编排
  memory.py / rag.py      # SQLite 对话、摘要、长期记忆和 FTS
  llm.py                  # DeepSeek 客户端和 function calling
  tools/
    agent.py              # 有界 agent loop、工具协议、证据复核
    external.py           # 网络、天气、GitHub 数据源
scripts/run.sh
deploy/qq-deepseek-bot.service
```

更完整的事件流、模块边界和排障说明见 `ARCHITECTURE.md`。

## 配置

复制 `.env.example` 为 `.env`，至少补齐：

- `DEEPSEEK_API_KEY`
- `BOT_QQ_ID`
- `BOT_ADMIN_QQ_LIST`
- `NATCAPQQ_API_TOKEN`
- `NATCAPQQ_CALLBACK_TOKEN`

## NapCat 配置建议

请在 NapCat 的配置目录中编辑对应实例的配置文件（例如 `/opt/natcapqq/napcat/config/onebot11_<实例ID>.json`），或按下面示例设置：

```json
{
  "network": {
    "httpServers": [
      {
        "name": "qq-deepseek-bot-api",
        "enable": true,
        "port": 3000,
        "host": "127.0.0.1",
        "enableCors": true,
        "enableWebsocket": false,
        "messagePostFormat": "array",
        "token": "CHANGE_ME",
        "debug": false
      }
    ],
    "httpSseServers": [],
    "httpClients": [
      {
        "name": "qq-deepseek-bot-callback",
        "enable": true,
        "url": "http://127.0.0.1:8080/onebot/event",
        "messagePostFormat": "array",
        "reportSelfMessage": false,
        "token": "CHANGE_ME",
        "debug": false
      }
    ],
    "websocketServers": [],
    "websocketClients": [],
    "plugins": []
  },
  "musicSignUrl": "",
  "enableLocalFile2Url": false,
  "parseMultMsg": false,
  "imageDownloadProxy": "",
  "timeout": {
    "baseTimeout": 10000,
    "uploadSpeedKBps": 256,
    "downloadSpeedKBps": 256,
    "maxTimeout": 1800000
  }
}
```

## 启动

```bash
./scripts/run.sh
```

或用户级 systemd（当前已采用这种方式，避免 root 密码）：

```bash
mkdir -p ~/.config/systemd/user
cp deploy/qq-deepseek-bot.user.service ~/.config/systemd/user/qq-deepseek-bot.service
systemctl --user daemon-reload
systemctl --user enable --now qq-deepseek-bot.service
systemctl --user status qq-deepseek-bot.service
journalctl --user -u qq-deepseek-bot.service -f
systemctl --user restart qq-deepseek-bot.service
systemctl --user stop qq-deepseek-bot.service
```

系统级 systemd 也可用，但需要 root：

```bash
sudo cp deploy/qq-deepseek-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now qq-deepseek-bot
sudo systemctl status qq-deepseek-bot
sudo journalctl -u qq-deepseek-bot -f
```

## 行为

- 群聊默认只在 `@bot` 时回复；当前配置为 `BOT_TRIGGER_MODE=both`，也支持 `/bot` 前缀。
- bot 会尝试读取自己在当前群里的群名片/昵称。用户发送 `@AI名字`（文本形式，任意位置）或在开头发送 `AI名字` 时也会唤醒；拿不到群名时默认按 `胖虎` 识别。
- 回复按 QQ 普通文本输出，不使用 Markdown。
- 回复不会带 `胖虎:`、`AI:` 这类说话人前缀；如果模型偶发输出，会在发送前清理。
- 同一个群同一时间只处理一个被触发的问题；处理上一条时，新的触发问题会收到“先不切换话题”的提示。
- 超过 `REPLY_NOTICE_DELAY` 仍未生成回复时，会先发送等待提示；当前默认 3 秒。长回复会自动分段，只有首段引用原消息。
- agent 真正调用知识库、搜索、天气或 GitHub 工具时会立即报告当前阶段；状态消息按群节流，避免连续刷屏。
- 支持原生 QQ `reply` 和 `at` 消息段。回复 bot 的消息无需再次 @；引用内容优先于旁支群聊上下文。
- 带附件的消息会把附件类型和文件名传给模型。图片会被标记为图片附件，但当前不会读取图片内容或 OCR；模型会说明暂时看不了图片内容。
- Minecraft 只作为资料查询方向，不扮演 Minecraft 角色；除非用户明确要求角色扮演。
- 模型策略偏保守：不确定、检索不到或资料不足时应明确说明，而不是编造。
- SFM、Minecraft、网页、新闻、天气和 GitHub 都通过同一个 agent 工具循环调度；不会把 PDF 全文硬塞进每轮上下文。
- 网页直读遇到 403/429 或正文无法提取时会尝试 Jina Reader；GitHub API 受限时可降级为公共仓库浅克隆和本地检索。
- 管理员命令统一使用英文 slash 形式；旧的中文“记住/忘记/清空上下文”不再作为管理命令。
- `/task` 定时任务只允许管理员设置、查看、删除。到点后 bot 会自动在设置命令所在群里执行提示词。
- 短期聊天历史会自动摘要成 `conversation_summaries`，回答时使用“自动摘要 + 最近少量消息”，不再固定塞最近 20 条。
- 非管理员不能修改长期记忆、自动摘要或上下文。

## 管理命令

```text
/help
/memory add <内容>
/memory add
第一条记忆
第二条记忆
/memory delete <编号或关键词> <编号或关键词>
/memory list
/memory clear
/context clear
/summary show
/summary clear
/weather <地点>
/news <关键词>
/web <关键词或URL>
/model show
/model list
/model use deepseek
/model use nvidia
/model use qwen
/model use qwen3.7-flash
/model use deepseek-v4-flash
/model use newapi
/model use gpt-5.4-mini
/model use cctq
/model use cctq/gpt-6-luna
/task add in 10m <提示词>
/task add at 2026-07-12 18:30 <提示词>
/task add every 1h <提示词>
/task add daily 09:30 <提示词>
/task list
/task delete <编号>
/task clear
```

说明：

- `/memory add` 后换行时，每个非空行会保存为一条长期记忆。
- `/memory delete` 支持一次删除多个编号，例如 `/memory delete 3 4 5`；关键词包含空格时可用英文引号包起来。
- `/context clear` 会清空当前群短期聊天历史和自动摘要，不删除长期记忆。
- `/memory clear` 会清空当前群长期记忆。
- `/weather`、`/news`、`/web` 普通群成员也可以使用。
- `/model` 仅管理员可用，模型选择按群保存在 SQLite；未设置时使用 `DEFAULT_MODEL_PROVIDER`，当前默认是 NVIDIA。
- NVIDIA 使用 `integrate.api.nvidia.com/v1` 的 OpenAI 兼容端点。当前通用对话模型为 `openai/gpt-oss-120b`，可通过 `NVIDIA_MODEL` 覆盖。
- `/task add in 10m ...` 是一次性延迟任务；`/task add at ...` 是预约任务；`/task add every 1h ...` 是循环任务，循环间隔最短 60 秒。
- `/task add daily 09:30 ...` 按服务器本地时区每天执行。

## SFM RAG

当前知识源是项目根目录的 `sfm.pdf`。首次部署或 PDF 更新后执行：

```bash
uv run python scripts/index_sfm_pdf.py sfm.pdf
systemctl --user restart qq-deepseek-bot.service
```

配置项：

- `RAG_ENABLED=true`
- `RAG_DOCUMENT_NAME=sfm`
- `RAG_QUERY_LIMIT=4`

上下文和发送相关配置：

- `MESSAGE_HISTORY_LIMIT=80`：SQLite 中保留的原始短期消息上限。
- `CONTEXT_RECENT_MESSAGE_LIMIT=8`：每次回答直接提供给模型的最近消息数。
- `SUMMARY_TRIGGER_MESSAGE_COUNT=18`：超过该数量后触发自动摘要。
- `SUMMARY_MAX_CHARS=1200`：自动摘要最大长度。
- `QQ_MESSAGE_MAX_CHARS=850`：单条 QQ 回复分段长度。
- `REPLY_NOTICE_DELAY=3.0`：普通回答超过多少秒未完成时发送等待提示。
- `BOT_DEFAULT_NAME=胖虎`：拿不到群名片/昵称时的文本唤醒默认名字。
- `BOT_NAME_CACHE_SECONDS=3600`：bot 群名片/昵称缓存时间。
- `NETWORK_TOOL_TIMEOUT=10`：天气、新闻、网页工具单次请求超时。
- `GITHUB_TOKEN`：可选，只读 token；提高 GitHub API 限额。不要提交到仓库。
- `NVIDIA_API_KEY`：可选 NVIDIA API key；留空时 NVIDIA 不会出现在 `/model list`。
- `NVIDIA_MODEL=openai/gpt-oss-120b`：NVIDIA 使用的通用对话模型。该模型支持现有的原生工具调用。
- `DEFAULT_MODEL_PROVIDER=nvidia`：新群或未选择群的默认 provider。
- NVIDIA 模型可用完整 ID 切换，例如 `/model use moonshotai/kimi-k2.6`；`/model list` 查看全部配置项。模型是否实际可调用由 NVIDIA 端点决定。
- 额外配置的模型包括 `stepfun-ai/step-3.7-flash` 和 `z-ai/glm-5.2`。
- 千问 provider 注册了 `qwen3.7-flash`、`deepseek-v4-flash`、`qwen-flash`、`qwen3.5-flash`；可用 `/model list` 查看，实际可用性由阿里接口决定。
- New API provider 注册了 `gpt-5.4-mini`、`gpt-5.5`、`gpt-5.6-luna`、`gpt-5.6-sol`、`gpt-5.6-terra`、`gpt-6-astra`；模型列表来自 2026-09-22 对 `/v1/models` 的探测。
- CCTQ 中转 provider `cctq` 注册了 `gpt-6-luna`、`gpt-5.6-sol`、`gpt-5.6-terra`、`gpt-6-astra`。由于模型 ID 与另一中转重叠，使用 `/model use cctq/<模型ID>` 指定渠道。`gpt-image-*` 和 `codex-auto-review` 不属于普通聊天模型，因此未加入聊天列表。
- 定时任务失败后会立即停用，不会自动重试。周期任务成功后按原计划时间推进，执行耗时不会导致计划逐次漂移。
- `AGENT_TOOL_MAX_ROUNDS` / `AGENT_TOOL_MAX_TOTAL_CALLS` / `AGENT_TOTAL_TIMEOUT`：agent 的轮次、调用数和总时长硬上限。
- `SCHEDULER_POLL_INTERVAL=10`：定时任务轮询间隔。

RAG 数据和聊天记忆共用 `SQLITE_PATH` 指向的 SQLite 文件。PDF 抽取结果会被切成小块写入 `rag_chunks` 和 `rag_chunks_fts`，回答时按问题召回相关块。

## 日志

- 控制台日志：标准输出
- 文件日志：`logs/bot.log`

## 已知限制

- `.env` 包含本机运行所需配置，仓库提交或共享前不要泄露。
- 网页搜索依赖 Bing China，新闻优先使用 Google News RSS，正文读取有 Jina Reader 降级；第三方站点结构或网络策略变化时仍可能失败。
- 短期上下文、自动摘要和长期记忆都在 SQLite 中。`/context clear` 只删除短期聊天历史和自动摘要，不删除长期记忆。
