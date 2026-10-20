# Agent Progress V2 Implementation

## Status

- **IMPLEMENTED**: Public Commentary contract, Registry control tool, Agent Loop integration, and Coordinator/Reporter rendering path.
- **TESTED_WITH_FAKE**: multi-step weather → calculation intent → news search → evidence-conscious final answer flow, using Fake LLM, fake weather/search handlers, and a Fake QQ adapter.
- **TESTED_LOCALLY**: targeted progress/agent tests and the complete unittest suite.
- **NOT_VERIFIED**: whether each production model will choose to call the commentary function, and delivery behavior in a live QQ/NapCat environment.
- **BLOCKED**: none for local implementation.

No database migration, new dependency, production API call, QQ message, deployment, commit, or push was made.

## Commentary protocol

The implementation uses an internal native Function Calling tool named `publish_public_commentary`. It is registered in the existing `TOOL_REGISTRY` and dispatched through the existing validation, timeout, semaphore, and result path. Its schema is internal by default and is exposed to `run_agent_loop()` only when the request has a live Progress event consumer and the request mode is not `quiet`.

The function accepts `kind`, bounded `text`, and optional `related_tool_call_ids`. Supported kinds are `plan`, `update`, `revision`, and `blocked`. A typed `PublicCommentary` is carried by `AgentEventKind.PUBLIC_COMMENTARY`; task ID and round are checked against the enclosing event identity.

This uses the model's normal `chat_with_tools()` response. It does not require provider-specific JSON mode, and it does not issue a separate model request to write an opening sentence. A commentary function call still consumes the existing Agent tool-call/round budget. The prompt tells the model to pair commentary with substantive work and avoid commentary-only calls.

If a request has no progress consumer or is in `quiet` mode, the schema is omitted and the existing deterministic V1 progress behavior remains the available path. Models that ignore the function likewise continue with V1 progress.

## Agent and Registry changes

- `app/progress/events.py` adds `CommentaryKind`, `PublicCommentary`, and the `PUBLIC_COMMENTARY` event kind. Commentary is bounded to 240 characters and six tool references, normalized, checked for common credential/URL/private-ID patterns, and has CQ-looking markup escaped.
- `app/progress/commentary.py` defines the control tool and its validation. Plans are limited to the first Agent round and cannot cite results. Updates require references to completed tool calls from this request whose actual outcome is compatible with the commentary kind.
- `app/tools/agent.py` builds request-specific schemas and allowed names. In a response containing both commentary and domain tools, it dispatches the commentary control call first, then dispatches domain tools concurrently. Tool results are restored to the original model `tool_calls` order, preserving one result per `tool_call_id`.
- Verified outcomes are taken from actual `ToolResult` values from earlier calls in the same Agent task. Commentary calls are excluded from this map and from WorkingState completed-step/open-question updates.
- Commentary remains an internal tool protocol block for the current Agent request. It is not sent as a new user message, does not enter ordinary chat history, and does not create Evidence or grant tool permissions.
- `app/tools/contracts.py` and `app/tools/registry.py` let internal tools remain hidden from normal registry listings/schemas while allowing the Agent's explicit request-scoped schema to include them.
- `app/handlers/llm_handler.py` includes the conditional schema in Context Manager prompt fitting and clarifies that commentary is public narration, not reasoning or proof of execution.

The Registry lifecycle events for the commentary tool use `ToolCategory.COMMENTARY`; `ProgressCoordinator` ignores those lifecycle events as user progress. The separate typed public-commentary event is accepted only while the task is active and epoch-current. `app/progress/coordinator.py` applies the existing task-wide notice delay, interval, and three-message cap; commentary shares that quota rather than receiving a second quota. The existing Reporter and Dispatcher send it through the same QQ delivery barrier and epoch check. `app/progress/presentation.py` renders the validated sentence as plain text.

## Status and trust rules

- `PLAN` is permitted once, in round 1, before any prior domain result is available. It describes intent only.
- `UPDATE` requires at least one referenced completed tool outcome of `ok` or `partial`.
- `REVISION` requires a referenced completed result with a relevant partial/no-result/unavailable/failure outcome.
- `BLOCKED` requires a referenced unavailable, denied, timed-out, or failed result.
- Coordinator validation repeats the task-local tool-call and outcome checks. Repeated commentary and more than three accepted commentary events per task are rejected.
- The model never receives access to `reasoning_content` through this feature. Arbitrary assistant `content` is not forwarded as progress.
- The system prompt forbids quoting instructions embedded in Web/Tool results. Commentary has no ability to alter ToolPolicy, WorkingState, ContextBudget, or Evidence.

The outcome/reference checks establish that a statement is associated with a real completed call, but they cannot prove that every free-form sentence is semantically accurate or free of all private information. Prompt guidance and conservative pattern checks reduce risk; they are not a perfect privacy or truth classifier. The final answer remains responsible for substantiated conclusions.

## Example user-visible behavior

For a genuinely multi-step request, if the model emits a valid plan and the task remains active past the existing progress delay, a message can look like:

> 我会先查询北京昨日天气，再计算目标数字，检索并核实相关新闻，最后依据真实新闻开头创作故事。

Subsequent progress can be model-authored only when tied to actual completed tool results. Otherwise the existing static V1 status is used. Fast tasks still close before the progress threshold and produce only the final answer. Reporter send failures remain isolated from Agent execution.

## Tests

Focused tests:

```bash
.venv/bin/python -m unittest tests.test_public_commentary tests.test_progress_events tests.test_progress_coordinator tests.test_progress_reporter tests.test_agent_progress_integration tests.test_agent tests.test_tool_registry tests.test_tool_compat -q
```

Result: **74 tests passed**.

Full suite:

```bash
timeout 300 .venv/bin/python -m unittest discover -s tests -q
```

Result: **489 tests passed, 0 failed**. The suite emitted existing warning/log output from mocked failure-path, FastAPI deprecation, and SQLite resource-warning tests; no test failed or timed out.

New coverage in `tests/test_public_commentary.py` includes:

- Schema is hidden without a live progress consumer and in quiet mode.
- Text normalization, length limit, URL/credential rejection, and CQ escaping.
- A virtual-time fast-task case that suppresses commentary before three seconds.
- Multi-step Fake LLM flow with one plan in the first normal tool-calling response, a verified update attached to a weather result, and a no-results search outcome.
- Fake Reporter/Dispatcher delivery and final-answer ordering.
- Tool-call/result ID pairing across the Agent protocol.
- Rejection of an `UPDATE` claiming success when its referenced search returned no results, while a result-linked `REVISION` is accepted.
- No separate commentary-only model call in the fixture: two tool-bearing rounds and the final response use three total Fake LLM calls.

The weather fixture deliberately does not assert or invent Beijing's real temperature. The Fake Web Search returns no results, and the final Fake response explicitly avoids fabricating a news source or story grounded in a nonexistent article.

## Files changed

- `app/handlers/llm_handler.py`
- `app/progress/__init__.py`
- `app/progress/coordinator.py`
- `app/progress/events.py`
- `app/progress/presentation.py`
- `app/progress/commentary.py` (new)
- `app/tools/agent.py`
- `app/tools/contracts.py`
- `app/tools/registry.py`
- `tests/test_public_commentary.py` (new)

## Remaining verification

- **NOT_VERIFIED**: real production models may ignore or phrase Public Commentary differently; no paid model request was made.
- **NOT_VERIFIED**: live QQ ordering/visibility remains subject to NapCat and QQ delivery behavior; tests exercised only the local Fake adapter.
- **NOT_VERIFIED**: pattern checks cannot guarantee detection of every secret or private fact. Commentary is bounded and tool-reference-gated, but its natural-language semantics are still model-generated.
- No deployment has been performed.
