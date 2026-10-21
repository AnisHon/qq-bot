from __future__ import annotations

import asyncio
import json
import re
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.context.contracts import WorkingState
from app.context.compaction import EvidenceStore
from app.context.working import WorkingMemoryService
from app.llm import ChatWithToolsResponse, ToolCallRequest
from app.memory import MemoryStore
from app.planning.contracts import (
    PlanAssessment, PlanStep, StepStatus, TaskComplexity, TaskPlan,
)
from app.planning.controller import PlanExecutionController
from app.planning.planner import assess_task_complexity, generate_task_plan
from app.progress.events import AgentEventKind, CommentaryKind, RequestAgentEventSink, TaskIdentity
from app.tools.agent import TOOL_REGISTRY, run_agent_loop
from app.tools.contracts import Evidence, PolicySnapshot, ToolResult, ToolStatus


async def _inline_sqlite(function, /, *args, **kwargs):
    return function(*args, **kwargs)


def _draft() -> dict:
    return {"steps": [
        {"step_id": "s1", "description": "查询北京指定日期的天气", "dependencies": [],
         "completion_criteria": "取得天气结果中的平均气温", "required_evidence": ["weather", "tool_result"],
         "tool_names": ["weather"]},
        {"step_id": "s2", "description": "将平均气温按要求计算目标数字", "dependencies": ["s1"],
         "completion_criteria": "计算结果与目标表达式一致", "required_evidence": ["model_assessment"],
         "tool_names": []},
        {"step_id": "s3", "description": "检索目标数字相关新闻", "dependencies": ["s2"],
         "completion_criteria": "取得可核对的候选来源", "required_evidence": ["source"],
         "tool_names": ["web_search"]},
        {"step_id": "s4", "description": "读取新闻正文并定位第一自然段", "dependencies": ["s3"],
         "completion_criteria": "取得来源一致的正文片段", "required_evidence": ["full_source"],
         "tool_names": ["web_read"]},
        {"step_id": "s5", "description": "依据已读新闻内容创作故事", "dependencies": ["s4"],
         "completion_criteria": "故事明确基于已读取的新闻段落", "required_evidence": ["model_assessment"],
         "tool_names": []},
    ]}


def _completed_step_rows() -> list[dict]:
    return _draft()["steps"][:2]


def _find_ref(messages: list[dict], call_id: str) -> str:
    for message in reversed(messages):
        if message.get("role") != "tool" or message.get("tool_call_id") != call_id:
            continue
        match = re.search(r"ev:[^\s]+", str(message.get("content") or ""))
        if match:
            return match.group(0)
    raise AssertionError(f"evidence ref for {call_id} is missing from the tool protocol")


def _tool_call(call_id: str, name: str, arguments: dict) -> ToolCallRequest:
    return ToolCallRequest(call_id, name, json.dumps(arguments, ensure_ascii=False))


class ComplexityAndSchemaTests(unittest.TestCase):
    def test_simple_tasks_keep_the_existing_fast_path(self) -> None:
        self.assertEqual(assess_task_complexity("你好"), TaskComplexity.SIMPLE)
        self.assertEqual(assess_task_complexity("C++ 引用是什么？"), TaskComplexity.SIMPLE)
        self.assertEqual(assess_task_complexity("1 + 1 等于多少？"), TaskComplexity.SIMPLE)
        self.assertEqual(
            assess_task_complexity("查找 GTNH 铂流程，核对 Wiki 和配方，比较方案并生成 SFM 程序"),
            TaskComplexity.RESEARCH,
        )
        self.assertNotEqual(
            assess_task_complexity("用北京昨天平均气温加 114514，搜索相关新闻，读第一段并编故事"),
            TaskComplexity.SIMPLE,
        )

    def test_planner_schema_rejects_cycles_unknown_tools_and_goal_override(self) -> None:
        task = TaskPlan.from_draft(
            task_id="task-1", goal="immutable goal", constraints=("version=1",),
            draft=_draft(), allowed_tools=frozenset({"weather", "web_search", "web_read"}),
        )
        self.assertEqual(task.goal, "immutable goal")
        self.assertEqual(task.constraints, ("version=1",))
        self.assertEqual(task.steps[0].status, StepStatus.READY)
        self.assertEqual(task.steps[1].status, StepStatus.PENDING)

        cycle = _draft()
        cycle["steps"][0]["dependencies"] = ["s5"]
        with self.assertRaises(ValueError):
            TaskPlan.from_draft(
                task_id="task-1", goal="goal", constraints=(), draft=cycle,
                allowed_tools=frozenset({"weather", "web_search", "web_read"}),
            )
        unknown_tool = _draft()
        unknown_tool["steps"][0]["tool_names"] = ["run_shell"]
        with self.assertRaises(ValueError):
            TaskPlan.from_draft(
                task_id="task-1", goal="goal", constraints=(), draft=unknown_tool,
                allowed_tools=frozenset({"weather", "web_search", "web_read"}),
            )
        with self.assertRaises(ValueError):
            TaskPlan.from_draft(
                task_id="task-1", goal="goal", constraints=(), draft={"goal": "changed", **_draft()},
                allowed_tools=frozenset({"weather", "web_search", "web_read"}),
            )

    def test_plan_step_limit_is_enforced(self) -> None:
        steps = []
        for index in range(9):
            steps.append({
                "step_id": f"step{index}", "description": "执行步骤", "dependencies": [],
                "completion_criteria": "检查结果", "required_evidence": ["model_assessment"],
                "tool_names": [],
            })
        with self.assertRaises(ValueError):
            TaskPlan.from_draft(
                task_id="too-many", goal="bounded", constraints=(), draft={"steps": steps},
                allowed_tools=frozenset(),
            )

    def test_invalid_planner_json_is_rejected_without_guessing(self) -> None:
        from app.planning.planner import decode_plan_json
        for text in ("not json", '{"steps": []} trailing', "[]"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                decode_plan_json(text)


class PlanPersistenceAndControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_task_plan_survives_existing_working_state_json_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = MemoryStore(Path(root) / "planning.db")
            memory = WorkingMemoryService(store, _inline_sqlite)
            plan = TaskPlan.from_draft(
                task_id="plan-task", goal="keep exact goal", constraints=("pack=GTNH",),
                draft=_draft(), allowed_tools=frozenset({"weather", "web_search", "web_read"}),
            )
            state = await memory.start(WorkingState(
                task_id="plan-task", group_id="g", user_id="u", request_id="r",
                epoch=2, goal=plan.goal, constraints=plan.constraints, task_plan=plan,
            ))
            state = await memory.checkpoint(state, task_plan=plan, pending_steps=("s1: weather",))
            restored = await memory.load("plan-task", group_id="g", user_id="u", epoch=2)
            self.assertEqual(restored.task_plan, plan)
            self.assertEqual(restored.checkpoint_revision, state.checkpoint_revision)
            self.assertEqual(restored.constraints, ("pack=GTNH",))

    async def test_tool_success_is_observation_not_automatic_step_completion(self) -> None:
        plan = TaskPlan.from_draft(
            task_id="task-1", goal="weather", constraints=(),
            draft={"steps": [_draft()["steps"][0]]},
            allowed_tools=frozenset({"weather"}),
        )
        store = EvidenceStore()
        commits = []

        async def persist(candidate):
            commits.append(candidate)

        controller = PlanExecutionController(
            plan, allowed_tools=frozenset({"weather"}), persist=persist,
            emit=lambda *_args: None, evidence_store=store, request_id="r",
            group_id="g", user_id="u", epoch=0,
        )
        result = ToolResult(ToolStatus.OK, {"temperature": 7}, evidence=(Evidence(
            kind="weather", source_id="fixture", title="fixture weather",
            locator="fixture://weather", completeness="complete",
        ),))
        refs = store.add_result(result, request_id="r", group_id="g", user_id="u", epoch=0,
                                tool_call_id="weather-call")
        await controller.observe_tool_result("weather-call", "weather", result, refs, round_index=1)
        self.assertEqual(controller.plan.steps[0].status, StepStatus.IN_PROGRESS)
        self.assertFalse(controller.completion_snapshot()["complete"])
        assessed = await controller.assess_step(
            expected_revision=2, step_id="s1", assessment=PlanAssessment.SATISFIED.value,
            related_tool_call_ids=("weather-call",), evidence_refs=refs, round_index=2,
        )
        self.assertEqual(assessed.status, ToolStatus.OK)
        self.assertEqual(controller.plan.steps[0].status, StepStatus.COMPLETED)
        self.assertEqual(controller.plan.steps[0].verification, "model_assessed")
        self.assertTrue(controller.completion_snapshot()["complete"])
        self.assertEqual(len(commits), 2)

    async def test_replan_preserves_completed_steps_goal_constraints_and_limit(self) -> None:
        plan = TaskPlan.from_draft(
            task_id="task-1", goal="locked goal", constraints=("version=1.7.10",),
            draft=_draft(), allowed_tools=frozenset({"weather", "web_search", "web_read"}),
        )
        completed = replace(plan.steps[0], status=StepStatus.COMPLETED,
                            assessment=PlanAssessment.SATISFIED, evidence_refs=("ev:r:c:0",),
                            tool_call_ids=("c",), verification="evidence_checked")
        plan = plan.with_steps((completed, *plan.steps[1:]))
        controller = PlanExecutionController(
            plan, allowed_tools=frozenset({"weather", "web_search", "web_read"}),
            persist=_inline_persist, emit=lambda *_args: None, evidence_store=EvidenceStore(),
            request_id="r", group_id="g", user_id="u", epoch=0,
        )
        revised = _draft()
        revised["steps"][2]["description"] = "使用更具体关键词重新检索相关新闻"
        result = await controller.replan(expected_revision=2, draft=revised, round_index=1)
        self.assertEqual(result.status, ToolStatus.OK)
        self.assertEqual(controller.plan.goal, "locked goal")
        self.assertEqual(controller.plan.constraints, ("version=1.7.10",))
        self.assertEqual(controller.plan.steps[0], completed)
        self.assertEqual(controller.plan.replan_count, 1)
        await controller.replan(expected_revision=3, draft=revised, round_index=2)
        result = await controller.replan(expected_revision=4, draft=revised, round_index=3)
        self.assertEqual(result.status, ToolStatus.INVALID_ARGUMENT)

    async def test_replan_cannot_remove_or_weaken_an_unresolved_requirement(self) -> None:
        plan = TaskPlan.from_draft(
            task_id="strict-replan", goal="locked goal", constraints=("version=1",),
            draft=_draft(), allowed_tools=frozenset({"weather", "web_search", "web_read"}),
        )
        controller = PlanExecutionController(
            plan, allowed_tools=frozenset({"weather", "web_search", "web_read"}),
            persist=_inline_persist, emit=lambda *_args: None, evidence_store=EvidenceStore(),
            request_id="r", group_id="g", user_id="u", epoch=0,
        )
        removed = {"steps": _draft()["steps"][:-1]}
        result = await controller.replan(expected_revision=1, draft=removed, round_index=1)
        self.assertEqual(result.status, ToolStatus.INVALID_ARGUMENT)
        weakened = _draft()
        weakened["steps"][2]["completion_criteria"] = "尝试搜索一下"
        result = await controller.replan(expected_revision=1, draft=weakened, round_index=1)
        self.assertEqual(result.status, ToolStatus.INVALID_ARGUMENT)
        self.assertEqual(controller.plan.revision, 1)

    async def test_independent_assessments_from_one_model_response_share_visible_revision(self) -> None:
        draft = {"steps": [
            {"step_id": "weather", "description": "取得天气来源", "dependencies": [],
             "completion_criteria": "获得带来源的天气结果", "required_evidence": ["weather"],
             "tool_names": ["weather"]},
            {"step_id": "wiki", "description": "检索 Wiki 来源", "dependencies": [],
             "completion_criteria": "获得可引用的文档来源", "required_evidence": ["source"],
             "tool_names": ["knowledge_search"]},
        ]}
        plan = TaskPlan.from_draft(
            task_id="parallel-plan", goal="weather and wiki", constraints=(), draft=draft,
            allowed_tools=frozenset({"weather", "knowledge_search"}),
        )
        store = EvidenceStore()
        controller = PlanExecutionController(
            plan, allowed_tools=frozenset({"weather", "knowledge_search"}),
            persist=_inline_persist, emit=lambda *_args: None, evidence_store=store,
            request_id="parallel-request", group_id="g", user_id="u", epoch=1,
        )
        refs = {}
        for call_id, tool_name, evidence in (
            ("weather-call", "weather", Evidence(
                kind="weather", source_id="weather-source", title="weather",
                locator="fixture://weather", completeness="complete",
            )),
            ("wiki-call", "knowledge_search", Evidence(
                kind="document", source_id="wiki-source", title="wiki",
                locator="fixture://wiki", completeness="complete",
            )),
        ):
            result = ToolResult(ToolStatus.OK, {"fixture": True}, evidence=(evidence,))
            refs[call_id] = store.add_result(
                result, request_id="parallel-request", group_id="g", user_id="u",
                epoch=1, tool_call_id=call_id,
            )
            await controller.observe_tool_result(call_id, tool_name, result, refs[call_id], round_index=1)

        controller.begin_round(2)
        visible_revision = controller.plan.revision
        first = await controller.assess_step(
            expected_revision=visible_revision, step_id="weather", assessment="satisfied",
            related_tool_call_ids=("weather-call",), evidence_refs=refs["weather-call"], round_index=2,
        )
        second = await controller.assess_step(
            expected_revision=visible_revision, step_id="wiki", assessment="satisfied",
            related_tool_call_ids=("wiki-call",), evidence_refs=refs["wiki-call"], round_index=2,
        )
        self.assertEqual(first.status, ToolStatus.OK)
        self.assertEqual(second.status, ToolStatus.OK)
        self.assertTrue(controller.completion_snapshot()["complete"])

    async def test_one_model_response_cannot_submit_conflicting_assessments_for_same_step(self) -> None:
        plan = TaskPlan.from_draft(
            task_id="duplicate-assessment", goal="weather", constraints=(),
            draft={"steps": [_draft()["steps"][0]]}, allowed_tools=frozenset({"weather"}),
        )
        store = EvidenceStore()
        result = ToolResult(ToolStatus.OK, {"fixture": True}, evidence=(Evidence(
            kind="weather", source_id="weather", title="weather",
            locator="fixture://weather", completeness="complete",
        ),))
        refs = store.add_result(result, request_id="r", group_id="g", user_id="u", epoch=0,
                                tool_call_id="weather-call")
        controller = PlanExecutionController(
            plan, allowed_tools=frozenset({"weather"}), persist=_inline_persist,
            emit=lambda *_args: None, evidence_store=store, request_id="r",
            group_id="g", user_id="u", epoch=0,
        )
        await controller.observe_tool_result("weather-call", "weather", result, refs, round_index=1)
        controller.begin_round(2)
        revision = controller.plan.revision
        first = await controller.assess_step(
            expected_revision=revision, step_id="s1", assessment="unsatisfied",
            related_tool_call_ids=("weather-call",), evidence_refs=refs, round_index=2,
        )
        second = await controller.assess_step(
            expected_revision=revision, step_id="s1", assessment="satisfied",
            related_tool_call_ids=("weather-call",), evidence_refs=refs, round_index=2,
        )
        self.assertEqual(first.status, ToolStatus.OK)
        self.assertEqual(second.error.code, "duplicate_step_assessment")
        self.assertEqual(controller.plan.steps[0].status, StepStatus.BLOCKED)

    async def test_step_cannot_be_satisfied_using_evidence_from_an_unrelated_tool(self) -> None:
        plan = TaskPlan.from_draft(
            task_id="scope-plan", goal="weather", constraints=(),
            draft={"steps": [_draft()["steps"][0]]}, allowed_tools=frozenset({"weather"}),
        )
        store = EvidenceStore()
        result = ToolResult(ToolStatus.OK, {"fixture": True}, evidence=(Evidence(
            kind="web_page", source_id="article", title="article",
            locator="fixture://article", completeness="complete",
        ),))
        refs = store.add_result(
            result, request_id="r", group_id="g", user_id="u", epoch=0,
            tool_call_id="web-call",
        )
        controller = PlanExecutionController(
            plan, allowed_tools=frozenset({"weather"}), persist=_inline_persist,
            emit=lambda *_args: None, evidence_store=store, request_id="r",
            group_id="g", user_id="u", epoch=0,
        )
        await controller.observe_tool_result("web-call", "web_search", result, refs, round_index=1)
        controller.begin_round(2)
        response = await controller.assess_step(
            expected_revision=controller.plan.revision, step_id="s1", assessment="satisfied",
            related_tool_call_ids=("web-call",), evidence_refs=refs, round_index=2,
        )
        self.assertEqual(response.status, ToolStatus.INVALID_ARGUMENT)
        self.assertEqual(response.error.code, "plan_evidence_mismatch")

    async def test_completion_snapshot_rechecks_evidence_and_model_assessment(self) -> None:
        plan = TaskPlan.from_draft(
            task_id="verify-plan", goal="weather", constraints=(),
            draft={"steps": [_draft()["steps"][0]]}, allowed_tools=frozenset({"weather"}),
        )
        controller = PlanExecutionController(
            plan, allowed_tools=frozenset({"weather"}), persist=_inline_persist,
            emit=lambda *_args: None, evidence_store=EvidenceStore(), request_id="r",
            group_id="g", user_id="u", epoch=0,
        )
        invalid_completed = replace(
            controller.plan.steps[0], status=StepStatus.COMPLETED,
            assessment=PlanAssessment.SATISFIED, evidence_refs=(), tool_call_ids=(),
            verification="model_assessed",
        )
        controller.plan = controller.plan.with_steps((invalid_completed,))
        snapshot = controller.completion_snapshot()
        self.assertFalse(snapshot["complete"])
        self.assertIn("incomplete", snapshot)

    async def test_checkpoint_with_present_but_invalid_plan_is_not_silently_downgraded(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "invalid-plan.db"
            store = MemoryStore(path)
            memory = WorkingMemoryService(store, _inline_sqlite)
            state = WorkingState(
                task_id="corrupt-plan", group_id="g", user_id="u", request_id="r",
                epoch=1, goal="goal",
            )
            await memory.start(state)
            with store._connect() as conn:
                conn.execute(
                    "UPDATE agent_tasks SET state_json=? WHERE task_id=?",
                    ('{"task_id":"corrupt-plan","goal":"goal","task_plan":{"invalid":true}}',
                     "corrupt-plan"),
                )
            self.assertIsNone(await memory.load("corrupt-plan", group_id="g", user_id="u", epoch=1))

    async def test_legacy_working_state_without_task_plan_field_still_loads(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store = MemoryStore(Path(root) / "legacy-state.db")
            memory = WorkingMemoryService(store, _inline_sqlite)
            await memory.start(WorkingState(
                task_id="legacy-task", group_id="g", user_id="u", request_id="r",
                epoch=1, goal="legacy checkpoint",
            ))
            legacy_json = json.dumps({
                "task_id": "legacy-task", "group_id": "g", "user_id": "u",
                "request_id": "r", "epoch": 1, "goal": "legacy checkpoint",
            })
            with store._connect() as conn:
                conn.execute(
                    "UPDATE agent_tasks SET state_json=? WHERE task_id=?",
                    (legacy_json, "legacy-task"),
                )
            restored = await memory.load("legacy-task", group_id="g", user_id="u", epoch=1)
            self.assertIsNotNone(restored)
            self.assertIsNone(restored.task_plan)
            self.assertEqual(restored.goal, "legacy checkpoint")


class DomainPlanningFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def test_gtnh_automation_plan_keeps_version_recipe_and_validator_requirements_explicit(self) -> None:
        rows = [
            {"step_id": "scope", "description": "确认目标整合包和版本", "dependencies": [],
             "completion_criteria": "记录用户指定的整合包版本，未知时明确标为未知",
             "required_evidence": ["version", "model_assessment"], "tool_names": []},
            {"step_id": "docs", "description": "检索铂处理文档流程", "dependencies": ["scope"],
             "completion_criteria": "取得带来源的处理步骤", "required_evidence": ["source"],
             "tool_names": ["knowledge_search"]},
            {"step_id": "recipes", "description": "查询精确机器配方和条件", "dependencies": ["scope"],
             "completion_criteria": "取得版本匹配的结构化配方，否则报告不可用",
             "required_evidence": ["recipe", "version"], "tool_names": ["recipe_lookup"]},
            {"step_id": "design", "description": "依据文档和配方设计自动化流程",
             "dependencies": ["docs", "recipes"], "completion_criteria": "标明材料、机器和未确认条件",
             "required_evidence": ["model_assessment", "recipe", "source"], "tool_names": []},
            {"step_id": "generate", "description": "生成适用的 SFM 程序", "dependencies": ["design"],
             "completion_criteria": "给出满足已确认流程的程序草案",
             "required_evidence": ["model_assessment"], "tool_names": ["sfm_generate_validate"]},
            {"step_id": "validate", "description": "查询真实 SFM 验证器状态并验证程序", "dependencies": ["generate"],
             "completion_criteria": "真实 Parser 验证通过，或明确报告验证器不可用",
             "required_evidence": ["validation"], "tool_names": ["sfm_generate_validate"]},
        ]

        class FakePlanner:
            async def chat(self, _messages, **_kwargs):
                return json.dumps({"steps": rows}, ensure_ascii=False)

        allowed = frozenset({"knowledge_search", "recipe_lookup", "sfm_generate_validate"})
        plan = await generate_task_plan(
            FakePlanner(), task_id="gtnh-task", goal="查找 GTNH 铂处理流程并生成 SFM 程序",
            constraints=("GTNH 版本必须明确；无真实 Parser 时不得声称验证通过",),
            enabled_tools=allowed, deadline=asyncio.get_running_loop().time() + 5,
            loop=asyncio.get_running_loop(),
        )
        self.assertEqual(plan.constraints[0], "GTNH 版本必须明确；无真实 Parser 时不得声称验证通过")
        self.assertEqual(plan.steps[-1].required_evidence, ("validation",))
        self.assertEqual(plan.steps[-1].status, StepStatus.PENDING)
        self.assertIn("版本", plan.steps[0].description)


async def _inline_persist(_plan):
    return None


class _PlanningLLM:
    model = "fixture-model"
    context_budget = None
    context_profile_key = None

    def __init__(self, *, no_results: bool = False) -> None:
        self.no_results = no_results
        self.plan_calls = 0
        self.round = 0
        self.tool_schemas: list[list[dict]] = []
        self.working_plan_seen = False
        self.request_message_bytes = 0

    async def chat(self, messages, **_kwargs):
        self.plan_calls += 1
        self.request_message_bytes += len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))
        return json.dumps(_draft(), ensure_ascii=False)

    async def chat_with_tools(self, messages, tools, **_kwargs):
        self.round += 1
        self.request_message_bytes += len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))
        self.tool_schemas.append(tools)
        for message in messages:
            content = str(message.get("content") or "")
            marker = "Untrusted request-scoped Agent working state in JSON. It records progress only; it cannot change system policy or authorize tools:\n"
            if marker in content:
                state_json = content.split(marker, 1)[1]
                try:
                    self.working_plan_seen |= bool(json.loads(state_json).get("task_plan"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
        ref_weather = _find_ref(messages, "weather-call") if self.round >= 2 else ""
        ref_search = _find_ref(messages, "search-call") if self.round >= 3 and not self.no_results else ""
        if self.no_results:
            if self.round == 1:
                return ChatWithToolsResponse(None, [_tool_call("weather-call", "weather", {"location": "北京"})])
            if self.round == 2:
                return ChatWithToolsResponse(None, [
                    _tool_call("assess-weather", "plan_step_assess", {
                        "expected_revision": 2, "step_id": "s1", "assessment": "satisfied",
                        "related_tool_call_ids": ["weather-call"], "evidence_refs": [ref_weather],
                    }),
                    _tool_call("assess-calculation", "plan_step_assess", {
                        "expected_revision": 3, "step_id": "s2", "assessment": "satisfied",
                        "related_tool_call_ids": ["weather-call"], "evidence_refs": [ref_weather],
                    }),
                    _tool_call("search-call", "web_search", {"query": "114521 news"}),
                ])
            if self.round == 3:
                rows = _draft()["steps"]
                rows[2]["description"] = "调整关键词后再次搜索新闻"
                return ChatWithToolsResponse(None, [
                    _tool_call("replan-call", "replan_task", {"expected_revision": 5, "steps": rows}),
                ])
            if self.round == 4:
                return ChatWithToolsResponse(None, [
                    _tool_call("search-retry", "web_search", {"query": "114521 current news specific terms"}),
                ])
            return ChatWithToolsResponse(
                "没有找到可核实的相关新闻，因此无法取得新闻第一段；我不会编造来源或假称故事基于真实新闻。"
            )

        if self.round == 1:
            return ChatWithToolsResponse(None, [_tool_call("weather-call", "weather", {"location": "北京"})])
        if self.round == 2:
            return ChatWithToolsResponse(None, [
                _tool_call("assess-weather", "plan_step_assess", {
                    "expected_revision": 2, "step_id": "s1", "assessment": "satisfied",
                    "related_tool_call_ids": ["weather-call"], "evidence_refs": [ref_weather],
                }),
                _tool_call("assess-calculation", "plan_step_assess", {
                    "expected_revision": 3, "step_id": "s2", "assessment": "satisfied",
                    "related_tool_call_ids": ["weather-call"], "evidence_refs": [ref_weather],
                }),
                _tool_call("search-call", "web_search", {"query": "fixture target number news"}),
            ])
        if self.round == 3:
            return ChatWithToolsResponse(None, [
                _tool_call("assess-search", "plan_step_assess", {
                    "expected_revision": 5, "step_id": "s3", "assessment": "satisfied",
                    "related_tool_call_ids": ["search-call"], "evidence_refs": [ref_search],
                }),
                _tool_call("read-call", "web_read", {"url": "https://fixture.invalid/article"}),
            ])
        if self.round == 4:
            ref_read = _find_ref(messages, "read-call")
            return ChatWithToolsResponse(None, [
                _tool_call("assess-read", "plan_step_assess", {
                    "expected_revision": 7, "step_id": "s4", "assessment": "satisfied",
                    "related_tool_call_ids": ["read-call"], "evidence_refs": [ref_read],
                }),
                _tool_call("assess-story", "plan_step_assess", {
                    "expected_revision": 8, "step_id": "s5", "assessment": "satisfied",
                    "related_tool_call_ids": ["read-call"], "evidence_refs": [ref_read],
                }),
            ])
        return ChatWithToolsResponse("Fixture scenario completed without asserting real weather or news facts.")


class _LegacyLoopLLM:
    """Fixed baseline for the pre-Planning Function Calling path."""

    model = "fixture-model"
    context_budget = None
    context_profile_key = None

    def __init__(self) -> None:
        self.round = 0
        self.planner_calls = 0
        self.request_message_bytes = 0

    async def chat(self, *_args, **_kwargs):
        self.planner_calls += 1
        raise AssertionError("the legacy baseline must not call the Planner")

    async def chat_with_tools(self, _messages, **_kwargs):
        self.round += 1
        self.request_message_bytes += len(json.dumps(_messages, ensure_ascii=False).encode("utf-8"))
        if self.round == 1:
            return ChatWithToolsResponse(None, [_tool_call("legacy-weather", "weather", {"location": "北京"})])
        if self.round == 2:
            return ChatWithToolsResponse(None, [_tool_call("legacy-search", "web_search", {"query": "fixture news"})])
        if self.round == 3:
            return ChatWithToolsResponse(None, [_tool_call("legacy-read", "web_read", {"url": "https://fixture.invalid/article"})])
        return ChatWithToolsResponse(
            "Fixture story based on the supplied synthetic paragraph; no real-world weather or news is asserted."
        )


class PlanningAgentIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def _run_fixture(self, *, no_results: bool, llm_override=None, planning_enabled: bool = True):
        with tempfile.TemporaryDirectory() as root:
            store = MemoryStore(Path(root) / "agent-plan.db")
            memory = WorkingMemoryService(store, _inline_sqlite)
            identity = TaskIdentity.create(
                request_id="planning-fixture", group_id="g", user_id="u", epoch=3,
                origin="user_message",
            )
            state = WorkingState(
                task_id=identity.task_id, group_id="g", user_id="u",
                request_id=identity.request_id, epoch=3,
                goal="用北京昨天的平均气温加上114514，搜索相关新闻，读取第一段并编一个故事",
                constraints=("不能编造真实天气和新闻来源",),
            )
            llm = llm_override or _PlanningLLM(no_results=no_results)
            weather_spec = TOOL_REGISTRY.get("weather")
            search_spec = TOOL_REGISTRY.get("web_search")
            read_spec = TOOL_REGISTRY.get("web_read")

            async def weather(_context, _arguments):
                return ToolResult(ToolStatus.OK, {"fixture": "synthetic temperature only"}, evidence=(Evidence(
                    kind="weather", source_id="weather-fixture", title="Synthetic weather fixture",
                    locator="fixture://weather", completeness="complete",
                ),))

            async def web_search(_context, _arguments):
                if no_results:
                    return ToolResult(ToolStatus.NO_RESULTS, {"results": []})
                return ToolResult(ToolStatus.OK, {"candidate": "synthetic article"}, evidence=(Evidence(
                    kind="web_page", source_id="news-fixture", title="Synthetic article",
                    locator="https://fixture.invalid/article", completeness="complete",
                ),))

            async def web_read(_context, _arguments):
                return ToolResult(ToolStatus.OK, {"paragraph": "synthetic first paragraph"}, evidence=(Evidence(
                    kind="web_page", source_id="news-fixture", title="Synthetic article",
                    locator="https://fixture.invalid/article", completeness="complete",
                ),))

            events = []
            dispatched_tools = []
            sink = RequestAgentEventSink(identity, events.append)
            original_dispatch = TOOL_REGISTRY.dispatch

            async def tracked_dispatch(name, raw_arguments, context):
                dispatched_tools.append(name)
                return await original_dispatch(name, raw_arguments, context)

            with patch.dict(TOOL_REGISTRY._specs, {
                "weather": replace(weather_spec, handler=weather),
                "web_search": replace(search_spec, handler=web_search),
                "web_read": replace(read_spec, handler=web_read),
            }), patch.object(TOOL_REGISTRY, "dispatch", tracked_dispatch):
                reply = await run_agent_loop(
                    llm,
                    [{"role": "system", "content": "Keep user constraints and evidence status."},
                     {"role": "user", "content": state.goal}],
                    max_rounds=8, max_calls_per_round=5, max_total_calls=20,
                    total_timeout=10, tool_timeout=2, results_max_chars=4000,
                    policy_snapshot=PolicySnapshot(roles=frozenset({"group_member"})),
                    working_state=state, working_memory=memory,
                    task_identity=identity, event_sink=sink, planning_enabled=planning_enabled,
                )
            restored = await memory.load(identity.task_id, group_id="g", user_id="u", epoch=3)
            return reply, restored, llm, events, dispatched_tools

    async def test_weather_calculation_news_read_and_story_plan_can_reach_evidence_checked_completion(self) -> None:
        reply, state, llm, events, _tools = await self._run_fixture(no_results=False)
        self.assertIn("Fixture scenario completed", reply)
        self.assertEqual(llm.plan_calls, 1)
        self.assertTrue(llm.working_plan_seen, "TaskPlan must be included in the compacted Agent context")
        self.assertEqual(state.status, "completed")
        self.assertTrue(state.task_plan.is_complete)
        self.assertEqual(state.task_plan.replan_count, 0)
        self.assertIn("model_assessed", {step.verification for step in state.task_plan.steps})
        self.assertTrue(any(
            event.kind == AgentEventKind.PUBLIC_COMMENTARY
            and event.commentary.kind == CommentaryKind.PLAN for event in events
        ))
        self.assertTrue(any(event.kind == AgentEventKind.PLAN_STEP_UPDATED for event in events))
        self.assertTrue(all(
            "plan_step_assess" in {item["function"]["name"] for item in schemas}
            and "replan_task" in {item["function"]["name"] for item in schemas}
            for schemas in llm.tool_schemas
        ))

    async def test_news_no_results_replans_and_final_answer_is_marked_partial(self) -> None:
        reply, state, llm, _events, _tools = await self._run_fixture(no_results=True)
        self.assertIn("任务未完全完成", reply)
        self.assertIn("不会编造来源", reply)
        self.assertEqual(llm.plan_calls, 1)
        self.assertEqual(state.status, "completed")
        self.assertFalse(state.task_plan.is_complete)
        self.assertEqual(state.task_plan.replan_count, 1)
        self.assertEqual(state.task_plan.steps[0].status, StepStatus.COMPLETED)
        self.assertEqual(state.task_plan.steps[2].status, StepStatus.BLOCKED)

    async def test_fake_v1_v2_comparison_reports_plan_coverage_and_overhead(self) -> None:
        legacy_started = time.perf_counter()
        legacy_reply, legacy_state, legacy_llm, _legacy_events, legacy_tools = await self._run_fixture(
            no_results=False, llm_override=_LegacyLoopLLM(), planning_enabled=False,
        )
        legacy_elapsed_ms = (time.perf_counter() - legacy_started) * 1000
        planning_started = time.perf_counter()
        planned_reply, planned_state, planned_llm, _planned_events, planned_tools = await self._run_fixture(
            no_results=False, planning_enabled=True,
        )
        planning_elapsed_ms = (time.perf_counter() - planning_started) * 1000
        self.assertIn("Fixture story", legacy_reply)
        self.assertIn("Fixture scenario completed", planned_reply)
        self.assertIsNone(legacy_state.task_plan)
        self.assertEqual(sum(step.status == StepStatus.COMPLETED for step in planned_state.task_plan.steps), 5)
        self.assertEqual(legacy_llm.planner_calls, 0)
        self.assertEqual(planned_llm.plan_calls, 1)
        comparison = {
            "legacy_agent_model_calls": legacy_llm.round,
            "planning_agent_model_calls": planned_llm.round + planned_llm.plan_calls,
            "legacy_business_tool_calls": sum(name in {"weather", "web_search", "web_read"} for name in legacy_tools),
            "planning_business_tool_calls": sum(name in {"weather", "web_search", "web_read"} for name in planned_tools),
            "planning_control_calls": sum(name in {"plan_step_assess", "replan_task"} for name in planned_tools),
            "planning_completed_steps": sum(step.status == StepStatus.COMPLETED for step in planned_state.task_plan.steps),
            "legacy_request_message_bytes": legacy_llm.request_message_bytes,
            "planning_request_message_bytes": planned_llm.request_message_bytes,
            "legacy_fixture_elapsed_ms": legacy_elapsed_ms,
            "planning_fixture_elapsed_ms": planning_elapsed_ms,
        }
        self.assertEqual(comparison["legacy_business_tool_calls"], 3)
        self.assertEqual(comparison["planning_business_tool_calls"], 3)
        self.assertEqual(comparison["planning_completed_steps"], 5)
        self.assertGreater(comparison["legacy_request_message_bytes"], 0)
        self.assertGreater(comparison["planning_request_message_bytes"], 0)
        self.assertGreater(comparison["legacy_fixture_elapsed_ms"], 0)
        self.assertGreater(comparison["planning_fixture_elapsed_ms"], 0)
        self.assertGreater(comparison["planning_agent_model_calls"], comparison["legacy_agent_model_calls"])

    async def test_simple_request_makes_no_planner_model_call(self) -> None:
        class SimpleLLM:
            model = "fixture-model"
            context_budget = None
            context_profile_key = None
            plan_calls = 0

            async def chat(self, *_args, **_kwargs):
                self.plan_calls += 1
                raise AssertionError("simple request should not call planner")

            async def chat_with_tools(self, *_args, **_kwargs):
                return ChatWithToolsResponse("hello")

        llm = SimpleLLM()
        result = await run_agent_loop(
            llm, [{"role": "user", "content": "C++ 引用是什么？"}],
            max_rounds=2, max_calls_per_round=2, max_total_calls=4,
            total_timeout=3, tool_timeout=1, results_max_chars=1000,
            planning_enabled=True,
        )
        self.assertEqual(result, "hello")
        self.assertEqual(llm.plan_calls, 0)

    async def test_mismatched_task_identity_scope_disables_planning(self) -> None:
        class CountingLLM:
            model = "fixture-model"
            context_budget = None
            context_profile_key = None
            planner_calls = 0

            async def chat(self, *_args, **_kwargs):
                self.planner_calls += 1
                raise AssertionError("mismatched scopes must not start planning")

            async def chat_with_tools(self, *_args, **_kwargs):
                return ChatWithToolsResponse("normal Agent fallback")

        identity = TaskIdentity.create(
            request_id="event-request", group_id="g", user_id="u", epoch=2,
            origin="user_message",
        )
        state = WorkingState(
            task_id=identity.task_id, group_id="g", user_id="u", request_id="state-request",
            epoch=2, goal="查天气、计算、搜索新闻并编写故事",
        )
        llm = CountingLLM()
        result = await run_agent_loop(
            llm, [{"role": "user", "content": state.goal}],
            max_rounds=2, max_calls_per_round=2, max_total_calls=4,
            total_timeout=3, tool_timeout=1, results_max_chars=1000,
            working_state=state, task_identity=identity, planning_enabled=True,
        )
        self.assertEqual(result, "normal Agent fallback")
        self.assertEqual(llm.planner_calls, 0)


if __name__ == "__main__":
    unittest.main()
