from __future__ import annotations

import asyncio
import unittest

from app.models import GroupMessageEvent
from app.qq_dispatcher import EventDeduplicator
from app.task_runtime import MessageTaskRuntime


def _event(message_id: str) -> GroupMessageEvent:
    return GroupMessageEvent(message_id, "group", "user", "name", message_id, False, 0, {})


class MessageTaskRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_admission_is_bounded_and_shutdown_drains_accepted_jobs(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        handled: list[str] = []

        async def handler(event: GroupMessageEvent) -> None:
            if event.message_id == "1":
                entered.set()
                await release.wait()
            handled.append(event.message_id)

        runtime = MessageTaskRuntime(handler, worker_count=1, queue_capacity=1)
        runtime.start()
        self.assertTrue(runtime.try_submit(_event("1")))
        await entered.wait()
        self.assertTrue(runtime.try_submit(_event("2")))
        self.assertFalse(runtime.try_submit(_event("3")))
        self.assertEqual(runtime.queue_size, 1)

        closing = asyncio.create_task(runtime.aclose(drain_timeout=1))
        await asyncio.sleep(0)
        self.assertFalse(runtime.accepting)
        self.assertFalse(runtime.try_submit(_event("4")))
        release.set()
        await closing
        self.assertCountEqual(handled, ["1", "2"])

    async def test_worker_limit_and_worker_survives_handler_error(self) -> None:
        active = 0
        peak = 0
        started = 0
        both_started = asyncio.Event()
        release = asyncio.Event()
        handled: list[str] = []

        async def handler(event: GroupMessageEvent) -> None:
            nonlocal active, peak, started
            if event.message_id == "error":
                raise RuntimeError("fixture failure")
            active += 1
            peak = max(peak, active)
            started += 1
            if started == 2:
                both_started.set()
            await release.wait()
            active -= 1
            handled.append(event.message_id)

        runtime = MessageTaskRuntime(handler, worker_count=2, queue_capacity=4)
        with self.assertLogs("app.task_runtime", level="ERROR"):
            runtime.start()
            self.assertTrue(runtime.try_submit(_event("error")))
            self.assertTrue(runtime.try_submit(_event("1")))
            self.assertTrue(runtime.try_submit(_event("2")))
            self.assertTrue(runtime.try_submit(_event("3")))
            await both_started.wait()
            self.assertLessEqual(peak, 2)
            release.set()
            await runtime.aclose(drain_timeout=1)
        self.assertCountEqual(handled, ["1", "2", "3"])

    async def test_shutdown_timeout_cancels_active_and_background_tasks(self) -> None:
        entered = asyncio.Event()
        worker_cancelled = asyncio.Event()
        background_cancelled = asyncio.Event()

        async def handler(_event: GroupMessageEvent) -> None:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                worker_cancelled.set()
                raise

        async def background() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                background_cancelled.set()
                raise

        runtime = MessageTaskRuntime(handler, worker_count=1, queue_capacity=1)
        runtime.start()
        runtime.spawn_background(background(), name="fixture-background")
        with self.assertLogs("app.task_runtime", level="WARNING"):
            self.assertIsNone(
                runtime.spawn_background(background(), name="over-capacity-background")
            )
        self.assertEqual(runtime.background_count, 1)
        self.assertTrue(runtime.try_submit(_event("1")))
        await entered.wait()
        with self.assertLogs("app.task_runtime", level="WARNING"):
            await runtime.aclose(drain_timeout=0.01)
        self.assertTrue(worker_cancelled.is_set())
        self.assertTrue(background_cancelled.is_set())
        self.assertFalse(runtime.accepting)

    async def test_background_failure_is_consumed_and_does_not_break_close(self) -> None:
        async def handler(_event: GroupMessageEvent) -> None:
            return None

        async def fail() -> None:
            raise RuntimeError("background fixture failure")

        runtime = MessageTaskRuntime(handler, worker_count=1, queue_capacity=1)
        runtime.start()
        task = runtime.spawn_background(fail(), name="fixture-failure")
        assert task is not None
        with self.assertLogs("app.task_runtime", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "background fixture failure"):
                await task
            await asyncio.sleep(0)
        await runtime.aclose(drain_timeout=1)


class EventDeduplicatorTest(unittest.TestCase):
    def test_accept_and_explicit_admission_mark_keep_existing_semantics(self) -> None:
        dedupe = EventDeduplicator()
        self.assertFalse(dedupe.contains("m1"))
        dedupe.mark("m1")
        self.assertTrue(dedupe.contains("m1"))
        self.assertFalse(dedupe.accept("m1"))
        self.assertTrue(dedupe.accept("m2"))
        self.assertTrue(dedupe.accept(""))


if __name__ == "__main__":
    unittest.main()
