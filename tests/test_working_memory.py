from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from app.context.contracts import WorkingState
from app.context.working import WorkingMemoryService, WorkingStateConflict
from app.llm import ChatWithToolsResponse, ToolCallRequest
from app.memory import MemoryStore
from app.tools.agent import run_agent_loop
from app.tools.contracts import Evidence, ToolResult, ToolStatus
from unittest.mock import patch


async def _inline_sqlite(function, /, *args, **kwargs):
    return function(*args, **kwargs)


class WorkingMemoryTest(unittest.IsolatedAsyncioTestCase):
    async def test_agent_loop_persists_only_observed_tool_progress_and_terminal_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "context.db")
            service = WorkingMemoryService(store, _inline_sqlite)

            class FakeLLM:
                model = "fake"
                context_budget = None
                context_profile_key = None

                def __init__(self):
                    self.round = 0

                async def chat_with_tools(self, *_args, **_kwargs):
                    self.round += 1
                    if self.round == 1:
                        return ChatWithToolsResponse(text=None, tool_calls=[
                            ToolCallRequest("call1", "weather", '{"location":"Oslo"}')
                        ])
                    return ChatWithToolsResponse(text="verified final")

            async def dispatch(_name, _args, _context):
                return ToolResult(
                    status=ToolStatus.OK, content={"temperature": "3 C"},
                    evidence=(Evidence(
                        kind="weather", source_id="weather:fixture", title="Oslo",
                        locator="fixture://weather/oslo", completeness="complete",
                    ),),
                )

            initial = WorkingState(
                task_id="request", group_id="g", user_id="u", request_id="request",
                epoch=0, goal="Check Oslo weather", constraints=("Use current tool result",),
            )
            with patch("app.tools.agent.TOOL_REGISTRY.dispatch", side_effect=dispatch):
                reply = await run_agent_loop(
                    FakeLLM(), [{"role": "system", "content": "policy"}, {"role": "user", "content": "weather?"}],
                    max_rounds=3, max_calls_per_round=1, max_total_calls=1,
                    total_timeout=5, tool_timeout=1, results_max_chars=2000,
                    context_epoch=0, working_state=initial, working_memory=service,
                )
            self.assertIn("verified final", reply)
            loaded = await service.load("request", group_id="g", user_id="u", epoch=0)
            self.assertEqual(loaded.status, "completed")
            self.assertEqual(loaded.completed_steps, ("weather: ok",))
            self.assertTrue(loaded.evidence_refs)

    async def test_checkpoints_are_scoped_revisioned_and_not_auto_resumed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "context.db"
            store = MemoryStore(path)
            service = WorkingMemoryService(store, _inline_sqlite)
            state = await service.start(WorkingState(
                task_id="request-1", group_id="g1", user_id="u1", request_id="request-1",
                epoch=3, goal="Find a fact", constraints=("version=1.20",),
            ))
            state = await service.checkpoint(
                state, completed_steps=("repository_search: ok",), evidence_refs=("ev:request-1:c1:0",)
            )
            self.assertEqual(state.checkpoint_revision, 1)
            self.assertIsNone(await service.load("request-1", group_id="g1", user_id="u2", epoch=3))
            self.assertIsNone(await service.load("request-1", group_id="g2", user_id="u1", epoch=3))
            loaded = await service.load("request-1", group_id="g1", user_id="u1", epoch=3)
            self.assertEqual(loaded.completed_steps, ("repository_search: ok",))
            self.assertEqual(loaded.evidence_refs, ("ev:request-1:c1:0",))

            # New store instance simulates process restart. It marks running work interrupted.
            restarted = MemoryStore(path)
            restart_service = WorkingMemoryService(
                restarted, _inline_sqlite
            )
            recovered = await restart_service.load(
                "request-1", group_id="g1", user_id="u1", epoch=3
            )
            self.assertEqual(recovered.status, "interrupted")
            self.assertEqual(recovered.checkpoint_revision, 1)
            self.assertEqual(recovered.completed_steps, ("repository_search: ok",))

    async def test_checkpoint_uses_compare_and_swap_and_reset_removes_task_state_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "context.db")
            store.add_message("g", "u", "member", "user", "history remains")
            service = WorkingMemoryService(store, _inline_sqlite)
            initial = await service.start(WorkingState(
                task_id="r", group_id="g", user_id="u", request_id="r", epoch=0, goal="goal",
            ))
            first = await service.checkpoint(initial, pending_steps=("verify",))
            with self.assertRaises(WorkingStateConflict):
                await service.checkpoint(initial, pending_steps=("stale write",))
            self.assertEqual(first.checkpoint_revision, 1)

            store.reset_group_context("g")
            self.assertIsNone(store.get_agent_task("r", group_id="g", user_id="u"))
            self.assertEqual(store.count_group_history("g"), 1)

    async def test_completed_checkpoint_is_terminal_and_scope_cannot_be_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = MemoryStore(Path(temp_dir) / "context.db")
            service = WorkingMemoryService(store, _inline_sqlite)
            state = await service.start(WorkingState(
                task_id="r", group_id="g", user_id="u", request_id="r", epoch=0, goal="goal",
            ))
            completed = await service.checkpoint(state, status="completed")
            self.assertEqual(completed.status, "completed")
            with self.assertRaises(WorkingStateConflict):
                await service.start(WorkingState(
                    task_id="r", group_id="g", user_id="u", request_id="r", epoch=0, goal="goal",
                ))


if __name__ == "__main__":
    unittest.main()
