from __future__ import annotations

import asyncio
import threading
import tempfile
from pathlib import Path
import unittest

from app.memory import MemoryStore
from app.storage_executor import StorageExecutor


async def _wait_for_thread_event(event: threading.Event, timeout: float = 1.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while not event.is_set() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.005)
    return event.is_set()


class StorageExecutorTest(unittest.IsolatedAsyncioTestCase):
    def test_constructor_rejects_limits_above_hard_caps(self) -> None:
        with self.assertRaisesRegex(ValueError, "hard cap"):
            StorageExecutor(sqlite_workers=5)
        with self.assertRaisesRegex(ValueError, "hard cap"):
            StorageExecutor(vector_workers=2)
        with self.assertRaisesRegex(ValueError, "hard cap"):
            StorageExecutor(queue_capacity=129)

    async def test_blocking_storage_call_does_not_block_event_loop_heartbeat(self) -> None:
        executor = StorageExecutor(sqlite_workers=1, vector_workers=1, queue_capacity=1)
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def blocking_read() -> str:
            entered.set()
            release.wait()
            finished.set()
            return "read complete"

        task = asyncio.create_task(executor.run_sqlite(blocking_read))
        try:
            self.assertTrue(await _wait_for_thread_event(entered))
            ticks = 0
            for _ in range(5):
                await asyncio.sleep(0.005)
                ticks += 1
            self.assertEqual(ticks, 5)
        finally:
            release.set()
        self.assertTrue(await _wait_for_thread_event(finished))
        self.assertEqual(await task, "read complete")
        await executor.aclose()
        self.assertTrue(executor.closed)

    async def test_vector_lane_offloads_embedding_work(self) -> None:
        executor = StorageExecutor(sqlite_workers=1, vector_workers=1, queue_capacity=1)
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def blocking_vector_search() -> list[str]:
            entered.set()
            release.wait()
            finished.set()
            return ["fixture result"]

        task = asyncio.create_task(executor.run_vector(blocking_vector_search))
        try:
            self.assertTrue(await _wait_for_thread_event(entered))
            for _ in range(5):
                await asyncio.sleep(0.005)
            self.assertFalse(task.done())
        finally:
            release.set()
        self.assertTrue(await _wait_for_thread_event(finished))
        self.assertEqual(await task, ["fixture result"])
        await executor.aclose()

    async def test_each_lane_bounds_submitted_work_and_propagates_failure(self) -> None:
        executor = StorageExecutor(sqlite_workers=1, vector_workers=1, queue_capacity=1)
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def first() -> str:
            entered.set()
            release.wait()
            finished.set()
            return "first"

        first_task = asyncio.create_task(executor.run_sqlite(first))
        self.assertTrue(await _wait_for_thread_event(entered))
        second_task = asyncio.create_task(executor.run_sqlite(lambda: "second"))
        await asyncio.sleep(0.01)
        self.assertEqual(executor.pending_count, 2)

        third_task = asyncio.create_task(executor.run_sqlite(lambda: "third"))
        await asyncio.sleep(0.01)
        self.assertEqual(executor.pending_count, 2)
        release.set()
        self.assertTrue(await _wait_for_thread_event(finished))
        self.assertEqual(await first_task, "first")
        self.assertEqual(await second_task, "second")
        self.assertEqual(await third_task, "third")

        with self.assertRaisesRegex(ValueError, "fixture storage failure"):
            await executor.run_vector(
                lambda: (_ for _ in ()).throw(ValueError("fixture storage failure"))
            )
        await executor.aclose()

    async def test_cancellation_does_not_release_slot_until_sync_call_finishes(self) -> None:
        executor = StorageExecutor(sqlite_workers=1, vector_workers=1, queue_capacity=1)
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def blocking_write() -> None:
            entered.set()
            release.wait()
            finished.set()

        first_task = asyncio.create_task(executor.run_sqlite(blocking_write))
        self.assertTrue(await _wait_for_thread_event(entered))
        first_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first_task

        second_task = asyncio.create_task(executor.run_sqlite(lambda: "second"))
        await asyncio.sleep(0.01)
        self.assertEqual(executor.pending_count, 2)
        self.assertFalse(second_task.done())
        release.set()
        self.assertTrue(await _wait_for_thread_event(finished))
        self.assertEqual(await second_task, "second")
        await executor.aclose()

    async def test_shutdown_drains_inflight_sync_work_and_rejects_new_work(self) -> None:
        executor = StorageExecutor(sqlite_workers=1, vector_workers=1, queue_capacity=1)
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def blocking_operation() -> None:
            entered.set()
            release.wait()
            finished.set()

        task = asyncio.create_task(executor.run_sqlite(blocking_operation))
        self.assertTrue(await _wait_for_thread_event(entered))
        close_task = asyncio.create_task(executor.aclose(timeout=1))
        await asyncio.sleep(0.01)
        self.assertFalse(close_task.done())
        release.set()
        self.assertTrue(await _wait_for_thread_event(finished))
        await close_task
        await task
        self.assertTrue(executor.closed)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await executor.run_sqlite(lambda: None)

    async def test_concurrent_sqlite_writes_complete_without_lost_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MemoryStore(Path(tmpdir) / "bot.db", history_limit=40)
            executor = StorageExecutor(sqlite_workers=2, vector_workers=1, queue_capacity=8)
            await asyncio.gather(
                *(
                    executor.run_sqlite(
                        store.add_message,
                        "fixture-group",
                        f"user-{index}",
                        "fixture",
                        "user",
                        f"message-{index}",
                    )
                    for index in range(20)
                )
            )
            rows = await executor.run_sqlite(store.get_recent_messages, "fixture-group", 40)
            self.assertEqual(len(rows), 20)
            self.assertEqual({row["content"] for row in rows}, {f"message-{i}" for i in range(20)})
            await executor.aclose()


if __name__ == "__main__":
    unittest.main()
