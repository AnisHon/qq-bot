from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from app.models import GroupMessageEvent


LOGGER = logging.getLogger(__name__)
MessageHandler = Callable[[GroupMessageEvent], Awaitable[None]]


class MessageTaskRuntime:
    """Bounded in-process admission and lifecycle for OneBot message work."""

    def __init__(
        self,
        handler: MessageHandler,
        *,
        worker_count: int = 4,
        queue_capacity: int = 64,
    ) -> None:
        if worker_count < 1 or queue_capacity < 1:
            raise ValueError("worker_count and queue_capacity must be positive")
        self._handler = handler
        self._worker_count = worker_count
        self._background_capacity = worker_count
        self._queue: asyncio.Queue[GroupMessageEvent] = asyncio.Queue(maxsize=queue_capacity)
        self._workers: list[asyncio.Task[None]] = []
        self._background: set[asyncio.Task[Any]] = set()
        self._accepting = False
        self._closed = False

    @property
    def accepting(self) -> bool:
        return self._accepting

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    @property
    def queue_capacity(self) -> int:
        return self._queue.maxsize

    @property
    def worker_count(self) -> int:
        return self._worker_count

    @property
    def background_count(self) -> int:
        return len(self._background)

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("message runtime is already closed")
        if self._workers:
            return
        self._accepting = True
        self._workers = []
        for index in range(self._worker_count):
            task = asyncio.create_task(self._worker(index), name=f"onebot-message-worker-{index}")
            task.add_done_callback(self._worker_done)
            self._workers.append(task)

    def try_submit(self, event: GroupMessageEvent) -> bool:
        if not self._accepting:
            return False
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            return False
        return True

    def spawn_background(self, coroutine: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any] | None:
        if self._closed:
            coroutine.close()
            return None
        if len(self._background) >= self._background_capacity:
            coroutine.close()
            LOGGER.warning("background task capacity reached; skipped task %s", name)
            return None
        task = asyncio.create_task(coroutine, name=name)
        self._background.add(task)
        task.add_done_callback(self._background_done)
        return task

    async def aclose(self, drain_timeout: float = 15.0) -> None:
        if self._closed:
            return
        self._accepting = False
        deadline = time.monotonic() + max(0.0, drain_timeout)

        drained = await self._wait_until(self._queue.join(), deadline)
        if drained:
            drained = await self._wait_background(deadline)

        if drained:
            await self._cancel_and_wait(tuple(self._workers), reason="idle worker shutdown")
        else:
            abandoned = self._queue.qsize()
            await self._cancel_and_wait(
                (*self._workers, *self._background),
                reason="message runtime drain timeout",
            )
            while True:
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                else:
                    self._queue.task_done()
            LOGGER.warning(
                "message runtime drain timed out; cancelled workers and abandoned queued jobs=%s",
                abandoned,
            )

        self._workers.clear()
        self._background.clear()
        self._closed = True

    async def _cancel_and_wait(self, tasks: tuple[asyncio.Task[Any], ...], *, reason: str) -> None:
        pending_tasks = tuple(task for task in tasks if not task.done())
        for task in pending_tasks:
            task.cancel()
        if not pending_tasks:
            return
        done, pending = await asyncio.wait(pending_tasks, timeout=1.0)
        if pending:
            LOGGER.error(
                "%s: tasks did not stop after cancellation: %s",
                reason,
                ", ".join(task.get_name() for task in pending),
            )
        # Retrieve failures from completed tasks even if their callbacks have not run yet.
        for task in done:
            try:
                task.exception()
            except asyncio.CancelledError:
                pass

    async def _worker(self, index: int) -> None:
        while True:
            event = await self._queue.get()
            try:
                await self._handler(event)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                LOGGER.exception("message worker %s failed", index)
            finally:
                self._queue.task_done()

    async def _wait_until(self, awaitable: Awaitable[Any], deadline: float) -> bool:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            await asyncio.wait_for(awaitable, timeout=remaining)
            return True
        except TimeoutError:
            return False

    async def _wait_background(self, deadline: float) -> bool:
        pending = tuple(self._background)
        if not pending:
            return True
        remaining = max(0.0, deadline - time.monotonic())
        _done, still_pending = await asyncio.wait(pending, timeout=remaining)
        return not still_pending

    def _background_done(self, task: asyncio.Task[Any]) -> None:
        self._background.discard(task)
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            LOGGER.error(
                "background task %s failed",
                task.get_name(),
                exc_info=(type(error), error, error.__traceback__),
            )

    def _worker_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            LOGGER.error(
                "message worker %s terminated unexpectedly",
                task.get_name(),
                exc_info=(type(error), error, error.__traceback__),
            )
