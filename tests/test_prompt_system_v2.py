from __future__ import annotations

import json
from pathlib import Path
import unittest

from app.handlers.llm_handler import SYSTEM_PROMPT
from app.planning.planner import PLANNER_SYSTEM, REPLAN_SYSTEM
from app.prompts import (
    AGENT_PLAN_EXECUTION_RULES,
    CURRENT_REQUEST_GUIDANCE,
    GROUP_SUMMARY_SYSTEM_PROMPT,
    MAIN_SYSTEM_PROMPT,
    RESEARCH_FINAL_RULES,
)
from app.tools.agent import _annotate_incomplete_plan, _build_final_answer_messages


class PromptSystemV2Tests(unittest.TestCase):
    def test_main_prompt_has_contextual_style_without_weakening_accuracy(self) -> None:
        self.assertIs(SYSTEM_PROMPT, MAIN_SYSTEM_PROMPT)
        for phrase in (
            "参与 QQ 群聊的 AI 助手",
            "闲聊轻松直接",
            "技术讨论准确、有深度但不照本宣科",
            "顺着已有对话往下说",
            "幽默随语境出现",
            "不必每次使用标题、列表、总结、固定开场",
            "不能编造精确配方、物品 ID、版本、引文、日期或来源",
            "FTB 任务是要求",
            "不读取或透露 reasoning_content",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, MAIN_SYSTEM_PROMPT)
        self.assertNotIn("controlled QQ group assistant", MAIN_SYSTEM_PROMPT)
        self.assertNotIn("Keep replies short.", MAIN_SYSTEM_PROMPT)

    def test_request_guidance_is_routing_context_not_a_second_persona(self) -> None:
        self.assertIn("Match the depth to the task", CURRENT_REQUEST_GUIDANCE)
        self.assertIn("continue from established context", CURRENT_REQUEST_GUIDANCE)
        self.assertIn("avoid equivalent searches", CURRENT_REQUEST_GUIDANCE)
        self.assertNotIn("directly and concisely", CURRENT_REQUEST_GUIDANCE)

    def test_planner_keeps_strict_schema_but_uses_request_language_for_descriptions(self) -> None:
        for prompt in (PLANNER_SYSTEM, REPLAN_SYSTEM):
            self.assertIn("JSON", prompt)
            self.assertIn("step_id", prompt)
            self.assertIn("用户当前语言", prompt)
            self.assertIn("不要输出思维过程", prompt)
        self.assertIn("WorkingState", AGENT_PLAN_EXECUTION_RULES)
        self.assertIn("Follow the main conversation style", AGENT_PLAN_EXECUTION_RULES)

    def test_final_research_prompt_keeps_evidence_constraints_and_user_facing_style(self) -> None:
        messages = [
            {"role": "system", "content": MAIN_SYSTEM_PROMPT},
            {"role": "system", "content": "date fixture"},
            {"role": "user", "content": "question"},
            {"role": "system", "content": "plan fixture"},
            {"role": "tool", "content": "tool evidence"},
        ]
        final = _build_final_answer_messages(messages, 1000)
        self.assertIn("Research is finished", final[0]["content"])
        self.assertIn("similarly named item", RESEARCH_FINAL_RULES)
        self.assertIn("Original request", final[1]["content"])
        self.assertIn("tool evidence", final[1]["content"])
        self.assertIn("Return exactly one JSON object", GROUP_SUMMARY_SYSTEM_PROMPT)

    def test_incomplete_plan_notice_does_not_expose_internal_counters(self) -> None:
        notice = _annotate_incomplete_plan("目前没有找到相关新闻。", {
            "complete": False, "completed_count": 2, "total_count": 5,
        })
        self.assertIn("还有要求没能核实完", notice)
        self.assertIn("目前没有找到相关新闻", notice)
        self.assertNotIn("2/5", notice)
        self.assertNotIn("计划状态", notice)
        self.assertNotIn("step_id", notice)

    def test_offline_style_fixture_has_at_least_twenty_diverse_cases(self) -> None:
        path = Path(__file__).parent / "fixtures" / "prompt_style_cases.json"
        fixture = json.loads(path.read_text(encoding="utf-8"))
        cases = fixture["cases"]
        self.assertGreaterEqual(len(cases), 20)
        self.assertEqual(len({row["id"] for row in cases}), len(cases))
        categories = {row["category"] for row in cases}
        self.assertTrue({"daily_chat", "technical", "minecraft", "agent_research", "security"} <= categories)
        self.assertTrue(all(row["user"] and row["checks"] for row in cases))


if __name__ == "__main__":
    unittest.main()
