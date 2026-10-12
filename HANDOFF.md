# QQ Bot 项目交接说明

更新时间：2026-08-17

## 1. 项目定位

这是一个基于 NapCat / OneBot 11 的 QQ 群聊 Agent，运行在 Python + FastAPI 上。

主要能力：

- QQ 群消息接收、@ 唤醒、昵称唤醒、引用消息和附件信息解析
- 普通中文对话和有限范围的 Agent 工具调用
- 网页、新闻、天气、GitHub、本地 SFM 知识库检索
- SQLite 聊天历史、自动摘要、长期记忆和定时任务
- NVIDIA NIM 多模型切换
- 阿里百炼兼容 OpenAI 接口多模型切换
- New API / OpenAI 兼容中转多模型切换
- CCTQ New API 中转多模型切换
- QQ 普通文本分段、状态提示、防抖和引用回复

不要把这个项目当成可执行任意 Shell 的通用 Agent。当前模型只能调用 `app/tools/agent.py` 注册的工具，QQ 用户不能直接执行主机命令。

## 2. 运行环境

- 工作目录：`/home/anishan/project/bot`
- Python 环境：项目 `.venv`，由 `uv` 管理
- Bot HTTP：`0.0.0.0:8080`
- NapCat HTTP API：`127.0.0.1:3000`
- NapCat 回调：`POST /onebot/event`
- 健康检查：`http://127.0.0.1:8080/health`
- 服务：用户级 `qq-deepseek-bot.service`
- 数据库：`data/bot.db`
- SFM 原始文档：`sfm.pdf`
- SFM 向量数据：`data/chromadb/`

## 3. 启动和部署

当前推荐使用用户级 systemd：

```bash
cd /home/anishan/project/bot
systemctl --user daemon-reload
systemctl --user enable --now qq-deepseek-bot.service
systemctl --user restart qq-deepseek-bot.service
systemctl --user status qq-deepseek-bot.service --no-pager
curl http://127.0.0.1:8080/health
```

服务启动脚本是 `scripts/run.sh`，会加载根目录 `.env`，然后执行：

```text
uv run uvicorn app.main:app --host 0.0.0.0 --port 8080
```

日志：

```bash
journalctl --user -u qq-deepseek-bot.service -n 100 --no-pager
tail -f logs/bot.log
```

NapCat 需要先运行，并确认 HTTP server 在 `127.0.0.1:3000`，HTTP client 回调指向：

```text
http://127.0.0.1:8080/onebot/event
```

NapCat 配置按实例单独维护。真实配置和 token 不要提交到 Git。

## 4. 密钥和配置

真实配置只在 `.env`，该文件已被 `.gitignore` 忽略。不要在交接文档、日志、聊天消息或提交中复制任何 API key。

配置模板是 `.env.example`。关键配置：

- `BOT_QQ_ID`：机器人 QQ 号
- `BOT_ADMIN_QQ_LIST`：管理员 QQ 号列表，逗号分隔
- `NATCAPQQ_API_TOKEN`：NapCat HTTP API token
- `NATCAPQQ_CALLBACK_TOKEN`：NapCat 回调 token
- `NVIDIA_API_KEY`：NVIDIA NIM key
- `QWEN_API_KEY`：阿里百炼兼容接口 key
- `QWEN_BASE_URL`：当前配置的阿里兼容接口地址
- `QWEN_MODEL`：`qwen` 别名对应的默认千问模型
- `NEWAPI_API_KEY`：New API 中转 key
- `NEWAPI_BASE_URL`：New API 中转的 OpenAI 兼容 `/v1` 地址
- `NEWAPI_MODEL`：`newapi` 别名对应的默认模型
- `CCTQ_API_KEY`：CCTQ New API 中转 key
- `CCTQ_BASE_URL`：CCTQ OpenAI 兼容 `/v1` 地址
- `CCTQ_MODEL`：`cctq` 别名对应的默认模型
- `NVIDIA_MODEL`：`nvidia` 别名对应的默认 NVIDIA 模型
- `DEFAULT_MODEL_PROVIDER=nvidia`：新群默认使用 NVIDIA
- `SQLITE_PATH=data/bot.db`
- `NETWORK_PROXY_URL=http://127.0.0.1:7890`：代理在线时使用；代理不可用会自动直连

DeepSeek 仍可显式选择，但不再作为 NVIDIA 失败时的自动兜底，避免余额不足时二次扣费。

## 5. 模型

模型由 `app/main.py` 注册。当前 NVIDIA 注册表包括：

- `openai/gpt-oss-120b`，由 `NVIDIA_MODEL` 配置
- `meta/llama-3.1-70b-instruct`
- `mistralai/mistral-large-2-instruct`
- `nvidia/nemotron-4-340b-instruct`
- `meta/llama-3.3-70b-instruct`
- `moonshotai/kimi-k2.6`
- `minimaxai/minimax-m3`
- `stepfun-ai/step-3.7-flash`
- `z-ai/glm-5.2`

阿里 provider `qwen` 注册的模型：

- `qwen3.7-flash`
- `deepseek-v4-flash`
- `qwen-flash`
- `qwen3.5-flash`

切换示例：`/model use qwen`、`/model use qwen3.7-flash`。

New API provider `newapi` 当前探测到的普通 GPT 模型：

- `gpt-5.4-mini`
- `gpt-5.5`
- `gpt-5.6-luna`
- `gpt-5.6-sol`
- `gpt-5.6-terra`
- `gpt-6-astra`

`codex-auto-review` 也出现在远端 `/v1/models`，但没有注册为群聊模型，因为它不是普通聊天模型。

CCTQ provider `cctq` 当前配置为 `gpt-6-luna`、`gpt-5.6-sol`、`gpt-5.6-terra` 和 `gpt-6-astra`。这些模型 ID 与另一个 New API provider 重叠，请使用 `/model use cctq/<模型ID>` 指定渠道。图像模型及 `codex-auto-review` 未加入普通聊天列表。

管理员命令：

```text
/model list
/model show
/model use nvidia
/model use moonshotai/kimi-k2.6
/model use z-ai/glm-5.2
/model use deepseek
```

列表中的模型不代表 NVIDIA 当前账户一定部署了它。项目按要求直接注册，实际不可用时会返回友好错误，不会自动调用 DeepSeek。

## 6. 代码结构

- `app/main.py`：FastAPI、OneBot 回调、启动关闭、定时任务循环、全局服务组装
- `app/natcapqq_adapter.py`：OneBot 事件、消息段、引用、附件和 NapCat API
- `app/qq_dispatcher.py`：QQ 发送、分段、引用首段、防抖和状态消息节流
- `app/qq_context.py`：引用、@、当前活跃用户和旁支群聊上下文选择
- `app/bot_service.py`：命令解析、唤醒、上下文组装、模型选择和业务编排
- `app/llm.py`：OpenAI 兼容 Chat Completions 客户端和工具调用解析
- `app/tools/agent.py`：有边界的 Agent loop、工具定义、去重、证据格式化
- `app/tools/external.py`：天气、搜索、新闻、网页、GitHub 网络工具
- `app/memory.py`：SQLite 表、聊天历史、摘要、长期记忆、定时任务和群设置
- `app/rag.py`：SQLite FTS SFM RAG
- `app/vector_rag.py`：Chroma 向量检索相关实现
- `app/handlers/`：较新的命令、LLM、任务处理拆分；修改前确认当前调用链是否仍由 `bot_service.py` 直接负责
- `tests/`：适配器、上下文、分发器、Agent、记忆和命令测试

## 7. Agent 和联网

工具定义集中在 `app/tools/agent.py` 的 `TOOL_DEFINITIONS`。当前工具包括：

- `knowledge_search`
- `web_search`
- `news_search`
- `multi_search`
- `fetch_url`
- `weather` / `weather_batch`
- GitHub 仓库搜索、仓库内容检索和文件读取

所有工具调用都有轮数、总调用数、单工具超时和总耗时限制。不要删除这些上限，否则一个模型可能循环调用并大量消耗 API 余额。

网络客户端在 `external.py` 中会检测 `NETWORK_PROXY_URL` 的端口：

- Clash 可用：走代理
- Clash 不可用：自动直连
- 天气主源：Open-Meteo
- 天气备用源：wttr.in
- 网页正文失败：Jina Reader 降级

如果联网失败，先检查 `ss -ltnp` 和 `NETWORK_PROXY_URL`，不要先改模型或增加 Agent 重试次数。

## 8. 定时任务安全规则

定时任务表在 SQLite 的 `scheduled_tasks`。调度实现位于 `app/main.py` 和 `app/memory.py`。

当前重要行为：

- 到期任务通过 `claim_due_scheduled_tasks()` 原子领取
- 同一任务的一次到期事件只执行一次
- 任务执行、模型调用、工具调用或发送失败后立即停用
- 失败不会自动重试，不会继续消耗模型余额
- 周期任务成功后从原计划时间推进到下一个未来周期，不按完成时间漂移
- Bot 进程在任务执行中重启时，该任务会被标记失败停用

管理员命令：

```text
/task add in 10m <提示词>
/task add at 2026-08-17 18:30 <提示词>
/task add every 1h <提示词>
/task add daily 09:30 <提示词>
/task list
/task delete <编号>
/task clear
```

如果用户要求“失败后自动重试”，必须先设计有上限的退避和每日预算，不能恢复无限重试。

## 9. SFM RAG

PDF 更新后重新索引：

```bash
cd /home/anishan/project/bot
UV_CACHE_DIR=/tmp/uv-cache uv run python scripts/index_sfm_pdf.py sfm.pdf
systemctl --user restart qq-deepseek-bot.service
```

RAG 不应把整份 PDF 直接塞进上下文。应通过 `knowledge_search` 召回少量相关片段。

## 10. 验证命令

这些检查不调用付费 LLM：

```bash
cd /home/anishan/project/bot
python3 -m compileall -q app tests scripts
UV_CACHE_DIR=/tmp/uv-cache uv run python -m unittest discover -s tests -v
git diff --check
systemctl --user restart qq-deepseek-bot.service
curl http://127.0.0.1:8080/health
```

## 11. 当前工作区注意事项

当前工作区存在较多未提交改动，这是项目迭代的结果，不要用 `git reset --hard` 或 `git checkout --` 清理。重点未跟踪文件包括 `app/handlers/`、`app/vector_rag.py`、`app/tools/cache.py`、`data/` 和测试文件。

备份压缩包、数据库、Chroma 数据、日志、`.env` 和本机知识库 PDF 属于本机文件，不要提交或上传。

接手后的第一步建议：读取本文件、`README.md`、`ARCHITECTURE.md`，检查 `git status`，再看 `systemctl --user status qq-deepseek-bot.service` 和最近 100 行日志。
