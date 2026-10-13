from __future__ import annotations

import asyncio
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from app.llm import ChatWithToolsResponse, ToolCallRequest
from app.sfm.contracts import (
    SFMTargetVersion,
    ValidationResult,
    ValidationStatus,
    ValidatorDiagnostic,
)
from app.sfm.fake_validator import FakeSFMValidator
from app.sfm.validator import SFMValidationService
from app.sfm.workflow import (
    MAX_COMPLETION_TOKEN_CEILING,
    SFMGenerationWorkflow,
    SFMWorkflowStatus,
)
from app.tools.agent import TOOL_REGISTRY, _append_references, run_agent_loop
from app.tools.contracts import Evidence, PolicySnapshot, ToolContext, ToolResult, ToolStatus
from app.usage_budget import BudgetExceeded


TARGET = SFMTargetVersion("sfm-fixture-v1", "mc-fixture-v1")
INITIAL_SOURCE = "synthetic program source v1"
REPAIRED_SOURCE = "synthetic program source v2"
INVALID_DIAGNOSTIC = ValidatorDiagnostic(
    code="fixture_error",
    message="人工固定测试诊断，不代表 SFM grammar。",
    line=3,
    column=4,
)
SFM_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "sfm"


class SFMGenerationWorkflowTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.evidence = Evidence(
            kind="document",
            source_id="synthetic-sfm-doc",
            title="Synthetic SFM fixture",
            locator="fixture://sfm/v1/example.md#L4",
            revision="synthetic-r1",
        )
        self.knowledge = _FakeKnowledge()

    def _workflow(self, model, validator, *, max_repairs=2):
        return SFMGenerationWorkflow(
            model,
            self.knowledge,
            SFMValidationService(validator) if validator is not None else SFMValidationService(),
            max_repair_attempts=max_repairs,
        )

    async def test_fake_success_is_clearly_simulated_and_keeps_evidence(self) -> None:
        model = _ScriptedModel([f"```sfm\n{INITIAL_SOURCE}\n```"])
        validator = FakeSFMValidator(valid_samples={INITIAL_SOURCE})
        result = await self._workflow(model, validator).generate_validate(
            "make a synthetic program", target=TARGET, deadline_monotonic=asyncio.get_running_loop().time() + 3
        )

        self.assertEqual(result.status, SFMWorkflowStatus.SIMULATED_VALID)
        self.assertEqual(result.source, INITIAL_SOURCE)
        self.assertEqual(result.validation.status, ValidationStatus.SIMULATED_VALID)
        self.assertEqual(result.validation.validator_id, "fake")
        self.assertFalse(result.validation.is_real_validation)
        self.assertEqual(result.model_calls, 1)
        self.assertEqual(result.repair_attempts, 0)
        self.assertEqual(result.evidence, (self.evidence,))
        self.assertEqual(model.calls[0]["max_tokens"], 768)
        self.assertEqual(MAX_COMPLETION_TOKEN_CEILING, 9216)
        self.assertEqual(self.knowledge.queries[0]["sfm_version"], TARGET.sfm_version)
        self.assertEqual(self.knowledge.queries[0]["minecraft_version"], TARGET.minecraft_version)
        payload = result.to_dict()
        self.assertEqual(payload["source"], INITIAL_SOURCE)
        self.assertEqual(payload["model_call_attempts"], 1)
        self.assertFalse(payload["validation"]["is_real_validation"])
        self.assertEqual(payload["evidence"][0]["locator"], self.evidence.locator)

    async def test_all_bounded_retrieval_sources_are_serialized(self) -> None:
        evidence = tuple(
            Evidence(
                kind="document",
                source_id=f"synthetic-sfm-{index}",
                title=f"Synthetic SFM source {index}",
                locator=f"fixture://sfm/v1/source-{index}.md#L{index + 1}",
                revision="synthetic-r1",
            )
            for index in range(6)
        )
        self.knowledge.result = ToolResult(
            status=ToolStatus.OK,
            content={
                "results": [
                    {
                        "source_id": item.source_id,
                        "title": item.title,
                        "content": "Synthetic reference text only.",
                        "locator": item.locator,
                    }
                    for item in evidence
                ]
            },
            evidence=evidence,
        )
        model = _ScriptedModel([INITIAL_SOURCE])
        result = await self._workflow(
            model, FakeSFMValidator(valid_samples={INITIAL_SOURCE})
        ).generate_validate(
            "make a synthetic program",
            target=TARGET,
            deadline_monotonic=asyncio.get_running_loop().time() + 3,
        )

        payload = result.to_dict()
        self.assertEqual(
            [item["locator"] for item in payload["evidence"]],
            [item.locator for item in evidence],
        )
        self.assertEqual(payload["evidence_omitted"], 0)

    def test_sfm_references_survive_existing_references_and_normal_cap(self) -> None:
        previous = (0, "Earlier source", "https://example.invalid/earlier")
        sfm_sources = [
            (-1, f"Synthetic SFM source {index}", f"fixture://sfm/source-{index}#L1")
            for index in range(6)
        ]

        answer = _append_references("synthetic answer", [previous, *sfm_sources])

        for _, _, locator in sfm_sources:
            self.assertIn(locator, answer)
        self.assertNotIn(previous[2], answer)

    async def test_synthetic_program_fixture_transcript_stays_simulated(self) -> None:
        valid_fixture = (SFM_FIXTURE_DIR / "synthetic_program.sfm").read_text(encoding="utf-8").strip()
        invalid_fixture = (SFM_FIXTURE_DIR / "synthetic_invalid_program.sfm").read_text(encoding="utf-8").strip()
        validator = FakeSFMValidator(
            valid_samples={valid_fixture},
            invalid_samples={invalid_fixture: (INVALID_DIAGNOSTIC,)},
        )
        model = _ScriptedModel([invalid_fixture, valid_fixture])

        result = await self._workflow(model, validator).generate_validate(
            "repair this synthetic fixture",
            target=TARGET,
            deadline_monotonic=asyncio.get_running_loop().time() + 3,
        )

        transcript = json.dumps(result.to_dict(), ensure_ascii=False)
        self.assertEqual(result.status, SFMWorkflowStatus.SIMULATED_VALID)
        self.assertEqual(result.source, valid_fixture)
        self.assertEqual(result.repair_attempts, 1)
        self.assertEqual(result.validation.status, ValidationStatus.SIMULATED_VALID)
        self.assertFalse(result.validation.is_real_validation)
        self.assertIn('"status": "simulated_valid"', transcript)
        self.assertIn('"is_real_validation": false', transcript)
        self.assertNotIn('"status": "valid"', transcript)

    async def test_invalid_fixture_reports_fake_diagnostic_not_parser_output(self) -> None:
        invalid_fixture = (SFM_FIXTURE_DIR / "synthetic_invalid_program.sfm").read_text(encoding="utf-8").strip()
        model = _ScriptedModel([invalid_fixture])
        result = await self._workflow(
            model,
            FakeSFMValidator(invalid_samples={invalid_fixture: (INVALID_DIAGNOSTIC,)}),
            max_repairs=0,
        ).generate_validate(
            "validate a synthetic fixture",
            target=TARGET,
            deadline_monotonic=asyncio.get_running_loop().time() + 3,
        )

        payload = result.to_dict()
        self.assertEqual(result.status, SFMWorkflowStatus.SIMULATED_INVALID)
        self.assertEqual(payload["validation"]["status"], "simulated_invalid")
        self.assertTrue(payload["validation"]["is_simulated"])
        self.assertFalse(payload["validation"]["is_real_validation"])
        self.assertEqual(payload["validation"]["diagnostics"][0]["code"], "fixture_error")
        self.assertEqual(result.model_calls, 1)

    async def test_invalid_fake_diagnostic_causes_one_bounded_repair(self) -> None:
        model = _ScriptedModel([INITIAL_SOURCE, REPAIRED_SOURCE])
        validator = FakeSFMValidator(
            valid_samples={REPAIRED_SOURCE},
            invalid_samples={INITIAL_SOURCE: (INVALID_DIAGNOSTIC,)},
        )
        result = await self._workflow(model, validator).generate_validate(
            "make a synthetic program", target=TARGET, deadline_monotonic=asyncio.get_running_loop().time() + 3
        )

        self.assertEqual(result.status, SFMWorkflowStatus.SIMULATED_VALID)
        self.assertEqual(result.source, REPAIRED_SOURCE)
        self.assertEqual(result.repair_attempts, 1)
        self.assertEqual(result.model_calls, 2)
        repair_prompt = model.calls[1]["messages"][-1]["content"]
        self.assertIn('"line":3', repair_prompt)
        self.assertIn('"column":4', repair_prompt)
        self.assertIn(TARGET.sfm_version, repair_prompt)

    async def test_two_repairs_are_hard_maximum_and_fake_never_becomes_real(self) -> None:
        model = _ScriptedModel([INITIAL_SOURCE, INITIAL_SOURCE, INITIAL_SOURCE, "unexpected fourth"])
        validator = FakeSFMValidator(invalid_samples={INITIAL_SOURCE: (INVALID_DIAGNOSTIC,)})
        result = await self._workflow(model, validator).generate_validate(
            "make an invalid synthetic program", target=TARGET, deadline_monotonic=asyncio.get_running_loop().time() + 3
        )

        self.assertEqual(result.status, SFMWorkflowStatus.SIMULATED_INVALID)
        self.assertEqual(result.repair_attempts, 2)
        self.assertEqual(result.model_calls, 3)
        self.assertEqual(len(model.calls), 3)
        self.assertFalse(result.validation.is_real_validation)

    async def test_no_parser_stops_before_retrieval_or_model_request(self) -> None:
        model = _ScriptedModel([INITIAL_SOURCE])
        workflow = self._workflow(model, validator=None)
        result = await workflow.generate_validate(
            "make a program", target=TARGET, deadline_monotonic=asyncio.get_running_loop().time() + 3
        )

        self.assertEqual(result.status, SFMWorkflowStatus.UNAVAILABLE)
        self.assertEqual(result.validation.status, ValidationStatus.UNAVAILABLE)
        self.assertIsNone(result.source)
        self.assertEqual(result.model_calls, 0)
        self.assertEqual(model.calls, [])
        self.assertEqual(self.knowledge.queries, [])

    async def test_empty_or_wrong_version_knowledge_stops_generation(self) -> None:
        model = _ScriptedModel([INITIAL_SOURCE])
        validator = FakeSFMValidator(valid_samples={INITIAL_SOURCE})
        self.knowledge.result = ToolResult(status=ToolStatus.NOT_INDEXED, content={"results": []})
        missing = await self._workflow(model, validator).generate_validate(
            "make a program", target=TARGET, deadline_monotonic=asyncio.get_running_loop().time() + 3
        )
        self.assertEqual(missing.status, SFMWorkflowStatus.NOT_INDEXED)
        self.assertEqual(model.calls, [])

        self.knowledge.result = ToolResult(status=ToolStatus.VERSION_MISMATCH, content={"results": []})
        mismatch = await self._workflow(model, validator).generate_validate(
            "make a program", target=TARGET, deadline_monotonic=asyncio.get_running_loop().time() + 3
        )
        self.assertEqual(mismatch.status, SFMWorkflowStatus.VERSION_MISMATCH)
        self.assertEqual(model.calls, [])

        self.knowledge.result = ToolResult(status=ToolStatus.NO_RESULTS, content={"results": []})
        no_results = await self._workflow(model, validator).generate_validate(
            "unmatched query", target=TARGET, deadline_monotonic=asyncio.get_running_loop().time() + 3
        )
        self.assertEqual(no_results.status, SFMWorkflowStatus.NO_RESULTS)
        self.assertNotEqual(no_results.status, SFMWorkflowStatus.NOT_INDEXED)
        self.assertEqual(model.calls, [])

    async def test_validator_unavailable_or_unsupported_stops_without_repairs(self) -> None:
        model = _ScriptedModel([INITIAL_SOURCE, REPAIRED_SOURCE])
        configured_unavailable = _FixedValidator(ValidationStatus.UNAVAILABLE)
        trusted_unavailable = SFMValidationService(
            configured_unavailable,
            trusted_validator_ids={configured_unavailable.validator_id},
        )
        workflow = SFMGenerationWorkflow(model, self.knowledge, trusted_unavailable)
        unavailable = await workflow.generate_validate(
            "make a program", target=TARGET, deadline_monotonic=asyncio.get_running_loop().time() + 3
        )
        self.assertEqual(unavailable.status, SFMWorkflowStatus.UNAVAILABLE)
        self.assertEqual(unavailable.model_calls, 1)
        self.assertEqual(unavailable.repair_attempts, 0)

        unsupported_target = SFMTargetVersion("unsupported-sfm", "unsupported-mc")
        validator = FakeSFMValidator(unsupported_targets={unsupported_target})
        unsupported_model = _ScriptedModel([INITIAL_SOURCE, REPAIRED_SOURCE])
        unsupported = await self._workflow(unsupported_model, validator).generate_validate(
            "make a program", target=unsupported_target, deadline_monotonic=asyncio.get_running_loop().time() + 3
        )
        self.assertEqual(unsupported.status, SFMWorkflowStatus.VERSION_UNSUPPORTED)
        self.assertEqual(unsupported.repair_attempts, 0)
        self.assertEqual(unsupported_model.calls.__len__(), 1)

    async def test_model_timeout_budget_exhaustion_and_deadline(self) -> None:
        slow = _SlowModel()
        timed = await self._workflow(slow, FakeSFMValidator(valid_samples={INITIAL_SOURCE})).generate_validate(
            "make a program", target=TARGET, deadline_monotonic=asyncio.get_running_loop().time() + 0.01
        )
        self.assertEqual(timed.status, SFMWorkflowStatus.TIMED_OUT)
        self.assertTrue(slow.cancelled.is_set())

        budget = await self._workflow(
            _ScriptedModel([BudgetExceeded("tokens_per_day", "group")]),
            FakeSFMValidator(valid_samples={INITIAL_SOURCE}),
        ).generate_validate(
            "make a program", target=TARGET, deadline_monotonic=asyncio.get_running_loop().time() + 3
        )
        self.assertEqual(budget.status, SFMWorkflowStatus.BUDGET_EXCEEDED)
        self.assertEqual(budget.model_calls, 1)

        expired = await self._workflow(
            _ScriptedModel([INITIAL_SOURCE]), FakeSFMValidator(valid_samples={INITIAL_SOURCE})
        ).generate_validate(
            "make a program", target=TARGET, deadline_monotonic=asyncio.get_running_loop().time() - 1
        )
        self.assertEqual(expired.status, SFMWorkflowStatus.TIMED_OUT)
        self.assertEqual(expired.model_calls, 0)

    async def test_caller_cancellation_propagates(self) -> None:
        model = _SlowModel()
        task = asyncio.create_task(
            self._workflow(model, FakeSFMValidator()).generate_validate(
                "make a program", target=TARGET, deadline_monotonic=asyncio.get_running_loop().time() + 10
            )
        )
        await asyncio.wait_for(model.entered.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(model.cancelled.is_set())

    async def test_generated_source_must_fit_return_cap_before_validation(self) -> None:
        source = "x" * 1500
        model = _ScriptedModel([source])
        validator = FakeSFMValidator(valid_samples={source})
        result = await self._workflow(model, validator).generate_validate(
            "make a program",
            target=TARGET,
            deadline_monotonic=asyncio.get_running_loop().time() + 3,
            max_result_chars=2000,
        )
        self.assertEqual(result.status, SFMWorkflowStatus.ERROR)
        self.assertIsNone(result.validation)
        self.assertIn("未验证", result.message)

    async def test_tool_registration_exposes_structured_fake_boundary(self) -> None:
        model = _ScriptedModel([INITIAL_SOURCE])
        workflow = self._workflow(model, FakeSFMValidator(valid_samples={INITIAL_SOURCE}))
        context = ToolContext(
            request_id="sfm-workflow-fixture",
            policy_snapshot=PolicySnapshot(roles=frozenset({"group_member"})),
            deadline_monotonic=asyncio.get_running_loop().time() + 3,
            result_max_chars=6000,
            extensions={"sfm_generation_workflow": workflow},
        )
        tool = await TOOL_REGISTRY.dispatch(
            "sfm_generate_validate",
            {
                "description": "make a synthetic program",
                "sfm_version": TARGET.sfm_version,
                "minecraft_version": TARGET.minecraft_version,
            },
            context,
        )
        self.assertEqual(tool.status, ToolStatus.PARTIAL)
        self.assertEqual(TOOL_REGISTRY.get("sfm_generate_validate").max_concurrency, 2)
        self.assertEqual(tool.content["source"], INITIAL_SOURCE)
        self.assertEqual(tool.content["validation"]["status"], "simulated_valid")
        self.assertFalse(tool.content["validation"]["is_real_validation"])
        self.assertEqual(tool.evidence, (self.evidence,))

    async def test_parser_diagnostics_are_sanitized_for_agent_tool_and_repair_prompt(self) -> None:
        private_diagnostic = ValidatorDiagnostic(
            code=r"C:\\private\\parser\\diagnostic",
            message=(
                r"failed \\server\share\parser.py /tmp "
                "github_pat_1234567890abcdef hf_1234567890abcdef "
                "api_key=fixture-secret"
            ),
            line=4,
        )
        invalid = FakeSFMValidator(
            valid_samples={REPAIRED_SOURCE},
            invalid_samples={INITIAL_SOURCE: (private_diagnostic,)},
        )

        exposed_result = await self._workflow(
            _ScriptedModel([INITIAL_SOURCE]), invalid, max_repairs=0
        ).generate_validate(
            "validate a fixture",
            target=TARGET,
            deadline_monotonic=asyncio.get_running_loop().time() + 3,
        )
        context = ToolContext(
            request_id="sfm-redaction-fixture",
            policy_snapshot=PolicySnapshot(roles=frozenset({"group_member"})),
            deadline_monotonic=asyncio.get_running_loop().time() + 3,
            result_max_chars=6000,
            extensions={
                "sfm_generation_workflow": self._workflow(
                    _ScriptedModel([INITIAL_SOURCE]), invalid, max_repairs=0
                )
            },
        )
        tool = await TOOL_REGISTRY.dispatch(
            "sfm_generate_validate",
            {
                "description": "validate a fixture",
                "sfm_version": TARGET.sfm_version,
                "minecraft_version": TARGET.minecraft_version,
            },
            context,
        )
        serialized_tool = json.dumps(tool.content, ensure_ascii=False)
        serialized_result = json.dumps(exposed_result.to_dict(), ensure_ascii=False)
        for secret in (
            r"\\server\share\parser.py",
            "/tmp",
            "github_pat_1234567890abcdef",
            "hf_1234567890abcdef",
            "fixture-secret",
        ):
            self.assertNotIn(secret, serialized_tool)
            self.assertNotIn(secret, serialized_result)
        self.assertEqual(tool.content["validation"]["diagnostics"][0]["line"], 4)

        repair_model = _ScriptedModel([INITIAL_SOURCE, REPAIRED_SOURCE])
        repaired = await self._workflow(repair_model, invalid, max_repairs=1).generate_validate(
            "repair a fixture",
            target=TARGET,
            deadline_monotonic=asyncio.get_running_loop().time() + 3,
        )
        self.assertEqual(repaired.status, SFMWorkflowStatus.SIMULATED_VALID)
        repair_prompt = repair_model.calls[1]["messages"][-1]["content"]
        for secret in (
            r"\\server\share\parser.py",
            "/tmp",
            "github_pat_1234567890abcdef",
            "hf_1234567890abcdef",
            "fixture-secret",
        ):
            self.assertNotIn(secret, repair_prompt)

    async def test_tool_reports_outer_unavailable_and_inner_unavailable_without_parser(self) -> None:
        model = _ScriptedModel([INITIAL_SOURCE])
        workflow = self._workflow(model, validator=None)
        context = ToolContext(
            request_id="sfm-unavailable-status-fixture",
            policy_snapshot=PolicySnapshot(roles=frozenset({"group_member"})),
            deadline_monotonic=asyncio.get_running_loop().time() + 3,
            result_max_chars=6000,
            extensions={"sfm_generation_workflow": workflow},
        )
        tool = await TOOL_REGISTRY.dispatch(
            "sfm_generate_validate",
            {
                "description": "make a synthetic program",
                "sfm_version": TARGET.sfm_version,
                "minecraft_version": TARGET.minecraft_version,
            },
            context,
        )

        self.assertEqual(tool.status, ToolStatus.DATA_UNAVAILABLE)
        self.assertEqual(tool.content["status"], "unavailable")
        self.assertEqual(tool.content["validation"]["status"], "unavailable")
        self.assertFalse(tool.content["validation"]["is_real_validation"])
        self.assertEqual(tool.error.code, "sfm_workflow_unavailable")
        self.assertEqual(model.calls, [])
        self.assertEqual(self.knowledge.queries, [])

    async def test_tool_never_returns_partially_clipped_program_source(self) -> None:
        source = "\\" * 800
        model = _ScriptedModel([source])
        workflow = self._workflow(model, FakeSFMValidator(valid_samples={source}))
        context = ToolContext(
            request_id="sfm-output-limit-fixture",
            policy_snapshot=PolicySnapshot(roles=frozenset({"group_member"})),
            deadline_monotonic=asyncio.get_running_loop().time() + 3,
            result_max_chars=2000,
            extensions={"sfm_generation_workflow": workflow},
        )
        tool = await TOOL_REGISTRY.dispatch(
            "sfm_generate_validate",
            {
                "description": "make a synthetic program",
                "sfm_version": TARGET.sfm_version,
                "minecraft_version": TARGET.minecraft_version,
            },
            context,
        )
        self.assertEqual(tool.status, ToolStatus.PARTIAL)
        self.assertIsNone(tool.content["source"])
        self.assertTrue(tool.content["source_omitted"])
        self.assertEqual(tool.content["status"], "source_omitted_output_limit")
        self.assertEqual(tool.error.code, "sfm_source_output_limit")

    async def test_agent_runs_generation_workflow_alone_and_preserves_tool_call_results(self) -> None:
        model = _ScriptedModel([INITIAL_SOURCE])
        workflow = self._workflow(model, FakeSFMValidator(valid_samples={INITIAL_SOURCE}))
        planner = _PlannerWithParallelCalls()
        with patch("app.tools.legacy_tools.get_weather", new=AsyncMock(return_value=[])) as weather:
            reply = await run_agent_loop(
                planner,
                [{"role": "user", "content": "generate an SFM program"}],
                max_rounds=3,
                max_calls_per_round=4,
                max_total_calls=4,
                total_timeout=3,
                tool_timeout=2,
                results_max_chars=6000,
                tool_extensions={
                    "sfm_generation_workflow": workflow,
                    "use_tool_cache": False,
                },
            )
        weather.assert_awaited_once()
        self.assertTrue(reply.startswith("fixture complete"))
        self.assertIn("fixture://sfm/v1/example.md#L4", reply)
        self.assertEqual(len(model.calls), 1)
        self.assertIn("simulated_valid", planner.tool_messages[0])
        self.assertIn("SFM 生成与验证工作流单独运行", planner.tool_messages[1])


class _FakeKnowledge:
    def __init__(self) -> None:
        self.result = ToolResult(
            status=ToolStatus.OK,
            content={
                "results": [
                    {
                        "source_id": "synthetic-sfm-doc",
                        "title": "Synthetic source",
                        "content": "Synthetic reference text only; not real SFM syntax.",
                        "locator": "fixture://sfm/v1/example.md#L4",
                    }
                ]
            },
            evidence=(
                Evidence(
                    kind="document",
                    source_id="synthetic-sfm-doc",
                    title="Synthetic SFM fixture",
                    locator="fixture://sfm/v1/example.md#L4",
                    revision="synthetic-r1",
                ),
            ),
        )
        self.queries: list[dict[str, str]] = []

    async def search_examples(self, query, *, sfm_version, minecraft_version, limit):
        self.queries.append(
            {
                "query": query,
                "sfm_version": sfm_version,
                "minecraft_version": minecraft_version,
                "limit": str(limit),
            }
        )
        return self.result


class _ScriptedModel:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    async def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class _FixedValidator:
    validator_id = "fixture-validator"

    def __init__(self, status) -> None:
        self.status = status

    async def validate(self, source, target, *, timeout_seconds):
        del source, timeout_seconds
        return ValidationResult(
            status=self.status,
            target=target,
            validator_id=self.validator_id,
        )


class _SlowModel:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def chat(self, messages, **kwargs):
        del messages, kwargs
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class _PlannerWithParallelCalls:
    def __init__(self) -> None:
        self.round = 0
        self.tool_messages: list[str] = []

    async def chat_with_tools(self, messages, *, tools, **kwargs):
        del kwargs
        if self.round == 0:
            self.round += 1
            names = {tool["function"]["name"] for tool in tools}
            self.asserted_workflow_tool = "sfm_generate_validate" in names
            return ChatWithToolsResponse(
                text=None,
                tool_calls=[
                    ToolCallRequest(
                        "sfm-call",
                        "sfm_generate_validate",
                        '{"description":"make a synthetic program","sfm_version":"sfm-fixture-v1","minecraft_version":"mc-fixture-v1"}',
                    ),
                    ToolCallRequest("weather-call", "weather", '{"location":"Tokyo"}'),
                ],
            )
        if self.round == 1:
            self.round += 1
            self.tool_messages = [
                str(message.get("content"))
                for message in messages
                if message.get("role") == "tool"
            ]
            return ChatWithToolsResponse(
                text=None,
                tool_calls=[ToolCallRequest("weather-retry", "weather", '{"location":"Tokyo"}')],
            )
        self.tool_messages = [
            str(message.get("content"))
            for message in messages
            if message.get("role") == "tool"
        ]
        return ChatWithToolsResponse(text="fixture complete")


if __name__ == "__main__":
    unittest.main()
