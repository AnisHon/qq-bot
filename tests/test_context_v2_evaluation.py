from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from app.config import Settings
from app.context.budget import ContextWindowBudget
from app.context.contracts import ContextMessagePlan
from app.context.manager import ContextManager
from app.memory import MemoryStore
from app.models import GroupMessageEvent
from app.qq_context import select_recent_context


async def _inline_sqlite(function, /, *args, **kwargs):
    return function(*args, **kwargs)


class ContextV2OfflineEvaluationTest(unittest.IsolatedAsyncioTestCase):
    async def test_v1_v2_fixed_history_fixture_reports_recall_constraints_and_context_estimates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "evaluation.db"
            store = MemoryStore(path)
            fixture_contents = []
            rare_terms = (
                "qzphasealpha", "qzphasebravo", "qzphasecharlie", "qzphasedelta", "qzphaseecho",
                "qzphasefoxtrot", "qzphasegolf", "qzphasehotel", "qzphaseindia", "qzphasejuliet",
            )
            target_positions = {17 + sample * 91: term for sample, term in enumerate(rare_terms)}
            target_ids: dict[str, int] = {}
            for index in range(1000):
                content = f"群聊噪声消息 {index} 今天讨论天气与工厂闲聊"
                if index in target_positions:
                    term = target_positions[index]
                    content = (
                        f"Fixture fact {term}：GTNH人工测试记录将钨酸盐输入指定为 sodium_tungstate；"
                        "约束：只回答版本 2.7.3，不可用其他版本替代。"
                    )
                fixture_contents.append(content)
                row_id = store.add_message(
                    "fixture-group", f"member-{index % 11}", "Member", "user", content,
                    request_id=f"fixture-{index}",
                )
                if index in target_positions:
                    target_ids[target_positions[index]] = row_id
            fixture_hash = hashlib.sha256("\n".join(fixture_contents).encode("utf-8")).hexdigest()
            settings = Settings(sqlite_path=path, agent_tools_enabled=False)
            manager = ContextManager(settings, store, _inline_sqlite)
            estimator = ContextWindowBudget((), mode="off")
            v1_hits = 0
            v2_hits = 0
            constraints_preserved = True
            v1_token_samples: list[int] = []
            v2_token_samples: list[int] = []
            for sample, term in enumerate(rare_terms):
                query = f"请回忆之前讨论过的 {term} 钨酸盐配方，并只按 2.7.3 回答。"
                event = GroupMessageEvent(
                    message_id=f"evaluation-current-{sample}", group_id="fixture-group", user_id="current-user",
                    nickname="Evaluator", text=query, is_at_bot=True, timestamp=sample + 1, raw_event={},
                )
                snapshot = store.record_message_snapshot(
                    group_id=event.group_id, user_id=event.user_id, nickname=event.nickname,
                    content=query, request_id=event.message_id, onebot_message_id=event.message_id,
                )
                recent_v1 = store.get_recent_messages("fixture-group", 80)
                v1_selected = select_recent_context(recent_v1, "current-user", 8)
                v1_ids = {int(row["id"]) for row in v1_selected}
                v1_hits += int(target_ids[term] in v1_ids)
                v1_messages = [
                    {"role": "system", "content": "Trusted fixed policy: preserve the requested version exactly."},
                    *({"role": row.get("role", "user"), "content": str(row.get("content") or "")}
                      for row in v1_selected),
                    {"role": "user", "content": query},
                ]
                v1_tokens, _ = estimator.estimate_input_tokens(
                    {"model": "fixture-model", "messages": v1_messages}
                )
                v1_token_samples.append(v1_tokens)

                prepared = await manager.build(
                    event, snapshot, query=query,
                    system_prompt="Trusted fixed policy: preserve the requested version exactly.",
                    time_prompt="Fixture time.", model="fixture-model", tool_schemas=[],
                    context_budget=None, profile_key=None,
                )
                v2_hits += int(any(item.key == f"history:{target_ids[term]}" for item in prepared.items))
                v2_text = "\n".join(str(message.get("content") or "") for message in prepared.messages)
                constraints_preserved = constraints_preserved and "2.7.3" in v2_text and query in v2_text
                v2_tokens, _ = estimator.estimate_input_tokens(
                    {"model": "fixture-model", "messages": list(prepared.messages)}
                )
                v2_token_samples.append(v2_tokens)

            self.assertEqual(fixture_hash, hashlib.sha256("\n".join(fixture_contents).encode("utf-8")).hexdigest())
            self.assertEqual(v1_hits, 0)
            self.assertEqual(v2_hits, len(rare_terms))
            self.assertTrue(constraints_preserved)
            self.assertTrue(all(item > 0 for item in (*v1_token_samples, *v2_token_samples)))
            # Machine-readable deterministic fixture metrics, not model quality or tokenizer results.
            metrics = {
                "fixture_version": 1,
                "fixture_sha256": fixture_hash,
                "rows": 1000,
                "samples": 10,
                "v1_rare_fact_recall_at_8": v1_hits / len(rare_terms),
                "v2_rare_fact_recall_at_12": v2_hits / len(rare_terms),
                "v2_current_constraint_present": constraints_preserved,
                "v1_average_estimated_input_tokens": sum(v1_token_samples) / len(v1_token_samples),
                "v2_average_estimated_input_tokens": sum(v2_token_samples) / len(v2_token_samples),
                "compaction_calls": 0,
                "extra_model_calls": 0,
                "model_task_result_accuracy": "not measured; no model is invoked",
            }
            self.assertEqual(json.loads(json.dumps(metrics))["extra_model_calls"], 0)
            self.__class__.last_metrics = metrics

    def test_compaction_plan_keeps_mandatory_current_message_and_drops_only_optional_context(self) -> None:
        from app.context.budget import ContextWindowProfile
        from app.context.compaction import AgentContextCompactor

        budget = ContextWindowBudget(
            [ContextWindowProfile("fixture", "bounded", 2048, max_output_tokens=256,
                                  preferred_output_tokens=256)],
            mode="enforce", default_output_tokens=256, minimum_output_tokens=128,
            safety_ratio=0.05, safety_floor_tokens=128,
        )
        base = [
            {"role": "system", "content": "Trusted system policy"},
            {"role": "user", "content": "Optional historical background " + "x" * 8000},
            {"role": "user", "content": "Current constraint: use exact version 2.7.3"},
        ]
        fit = AgentContextCompactor().fit(
            base, (ContextMessagePlan(1, "history:old", 1, False),), (),
            model="bounded", tool_schemas=[], budget=budget, profile_key="fixture:bounded",
            max_tokens=256,
        )
        rendered = "\n".join(message["content"] for message in fit.messages)
        self.assertEqual(fit.removed_context_keys, ("history:old",))
        self.assertIn("Current constraint: use exact version 2.7.3", rendered)
        self.assertIn("Trusted system policy", rendered)
        self.assertNotIn("Optional historical background", rendered)


if __name__ == "__main__":
    unittest.main()
