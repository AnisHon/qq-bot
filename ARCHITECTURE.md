# Architecture

## Runtime Flow

1. NapCat pushes a OneBot group event to `POST /onebot/event`.
2. `NatcapQQAdapter` parses text, attachments, all `at` segments and the `reply` id. A quoted message is hydrated with `get_msg`.
3. `EventDeduplicator` rejects replayed message ids. The bot resolves its current group card/name and decides whether the event wakes it.
4. `BotService` records the message, applies slash commands, chooses focused recent context and builds the LLM request.
5. DeepSeek either answers directly or enters the bounded native function-calling loop in `tools/agent.py`.
6. `QQMessageDispatcher` serializes sends per group, suppresses repeated status text, splits long output and attaches a native QQ reply only to the first part.

## Context Model

- The current request and explicitly quoted message have highest relevance.
- Recent messages from the active user and previous bot answers are preferred.
- At most two ambient messages from other members are retained, and they are marked as background rather than instructions.
- Older history is summarized into `conversation_summaries` instead of being appended indefinitely.
- Admin-managed durable memories are separate from short history and summaries.

## Agent And Evidence

`knowledge_search` is the common entry point for local SFM FTS chunks and Minecraft domain sources. External tools cover web/news search, URL reading, weather batches and public GitHub repositories. Calls run concurrently where useful and are bounded by rounds, calls per round, total calls and wall time.

Equivalent tool arguments are normalized and deduplicated. Research answers receive a final synthesis and evidence-consistency pass. Sources are appended as plain QQ text. Exact recipes and identifiers must be supported by retrieved evidence; weak retrieval does not prevent a clearly marked best-effort answer.

## Model Providers

NVIDIA NIM is the default OpenAI-compatible provider using `integrate.api.nvidia.com/v1`; DeepSeek remains available only when selected explicitly and is never an automatic fallback. `/model use <provider-or-model-id>` is admin-only and persists the selection per group in `group_settings`; scheduled tasks and automatic summaries use that same group selection. Multiple NVIDIA model IDs are registered independently, while `nvidia` aliases the `NVIDIA_MODEL` default.

Scheduled occurrences are claimed atomically. A successful recurring task advances from its previous schedule anchor to the next future occurrence. Any execution, model, tool, delivery, or process-interruption failure disables the task and records the error; there is no implicit paid retry.

Network fallback order:

1. Bing China for general search and Google News RSS for current news.
2. Direct HTTP for selected pages and APIs.
3. Jina Reader for public HTML returning 403/429 or unreadable body text.
4. GitHub API, then cached shallow clone for public repository inspection.

## Operational Boundaries

- QQ users cannot run host shell commands. Agent autonomy is limited to registered tools.
- Only configured admins can change memory, context and scheduled tasks.
- `.env`, SQLite files, logs and cloned repository caches are ignored by Git.
- Status updates are throttled independently from final answers. A generic wait message is sent only after `REPLY_NOTICE_DELAY` when no tool progress was reported.

## Handoff Checks

```bash
UV_CACHE_DIR=/tmp/uv-cache uv sync --group dev
UV_CACHE_DIR=/tmp/uv-cache uv run python -m unittest discover -s tests -v
python3 -m compileall app tests scripts
systemctl --user restart qq-deepseek-bot.service
curl http://127.0.0.1:8080/health
journalctl --user -u qq-deepseek-bot.service -n 100 --no-pager
```

When changing OneBot parsing, add fixtures to `tests/test_adapter.py`. Sending behavior belongs in `tests/test_dispatcher.py`; context and command behavior belongs in `tests/test_bot_service.py`; tool-loop evidence behavior belongs in `tests/test_agent.py`.
