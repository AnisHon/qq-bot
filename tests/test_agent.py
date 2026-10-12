from __future__ import annotations

from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from app.llm import ChatWithToolsResponse, ToolCallRequest
from app.models import SearchResult
from app.tools.agent import _parse_dsml_tool_calls, _tool_call_message, _tool_signature, _validate_research_answer, run_agent_loop


class _FakeLLM:
    def __init__(self) -> None:
        self.round = 0

    async def chat_with_tools(self, messages, tools, temperature=0.25):
        self.round += 1
        if self.round == 1:
            return ChatWithToolsResponse(
                text=None,
                tool_calls=[
                    ToolCallRequest(f"call-{idx}", "weather", f'{{"location":"城市{idx}"}}')
                    for idx in range(3)
                ],
            )
        return ChatWithToolsResponse(text="最终回答")

    async def chat(self, messages, temperature=0.7, max_tokens=None):
        return "降级回答"


class AgentLoopTest(IsolatedAsyncioTestCase):
    def test_reversed_ftb_task_relation_is_rejected(self) -> None:
        messages = [
            {
                "role": "tool",
                "content": "关系检测：目标作为任务要求出现 1 次；这不是配方输出。 allthetweaks:atm_star",
            }
        ]
        answer = "完成FTB任务后会奖励 allthetweaks:atm_star。"
        corrected = _validate_research_answer(answer, messages)
        self.assertNotIn("会奖励", corrected)
        self.assertIn("JEI/EMI", corrected)

    def test_denial_of_evidence_confirmed_item_is_rejected(self) -> None:
        messages = [{"role": "tool", "content": "任务目标 item: allthetweaks:atm_star"}]
        corrected = _validate_research_answer("ATM10里没有叫ATM之星的物品。", messages)
        self.assertNotIn("没有叫", corrected)
        self.assertIn("allthetweaks:atm_star", corrected)

    def test_dsml_content_is_recovered_as_tool_call(self) -> None:
        text = '''<｜｜DSML｜｜tool_calls>
<｜｜DSML｜｜invoke name="github_repo_inspect">
<｜｜DSML｜｜parameter name="repository" string="true">AllTheMods/ATM-10</｜｜DSML｜｜parameter>
<｜｜DSML｜｜parameter name="query" string="true">ATM Star recipe</｜｜DSML｜｜parameter>
</｜｜DSML｜｜invoke>
</｜｜DSML｜｜tool_calls>'''
        calls = _parse_dsml_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "github_repo_inspect")
        self.assertIn("AllTheMods/ATM-10", calls[0].arguments)

    def test_equivalent_tool_calls_have_same_signature(self) -> None:
        first = ToolCallRequest("1", "web_search", '{"query":" ATM9   Recipe "}')
        second = ToolCallRequest("2", "web_search", '{"query": "atm9 recipe"}')
        self.assertEqual(_tool_signature(first), _tool_signature(second))

    def test_tool_call_extension_is_preserved(self) -> None:
        call = ToolCallRequest(
            "1",
            "weather",
            '{"location":"北京"}',
            {"google": {"thought_signature": "opaque"}},
        )
        self.assertEqual(_tool_call_message(call)["extra_content"]["google"]["thought_signature"], "opaque")

    async def test_every_tool_call_gets_a_response_when_round_is_capped(self) -> None:
        messages = [{"role": "user", "content": "查询多个城市天气"}]
        result = [SearchResult("天气", "晴", "", "天气")]
        with patch("app.tools.agent._execute_native_tool_call", new=AsyncMock(return_value=result)):
            answer = await run_agent_loop(
                _FakeLLM(),
                messages,
                max_rounds=2,
                max_calls_per_round=2,
                max_total_calls=2,
                total_timeout=10,
                tool_timeout=2,
                results_max_chars=2000,
            )

        self.assertEqual(answer, "最终回答")
        tool_ids = [message["tool_call_id"] for message in messages if message["role"] == "tool"]
        self.assertEqual(tool_ids, ["call-0", "call-1", "call-2"])
