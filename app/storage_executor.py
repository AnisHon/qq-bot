from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import logging
from typing import Any, Callable, TypeVar


LOGGER = logging.getLogger(__name__)
T = TypeVar("T")
MAX_SQLITE_WORKERS = 4
# Chroma / sentence-transformers shared-client thread safety is not established
# by this project, so vector access remains serial until measured otherwise.
MAX_VECTOR_WORKERS = 1
MAX_QUEUE_CAPACITY = 128


class StorageExecutor:
    """Bounded offload lanes for synchronous SQLite and vector storage work."""

    def __init__(
        self,
        *,
        sqlite_workers: int = 1,
        vector_workers: int = 1,
        queue_capacity: int = 16,
    ) -> None:
        if sqlite_workers < 1 or vector_workers < 1 or queue_capacity < 1:
            raise ValueError("storage worker and queue limits must be positive")
        if (
            sqlite_workers > MAX_SQLITE_WORKERS
            or vector_workers > MAX_VECTOR_WORKERS
            or queue_capacity > MAX_QUEUE_CAPACITY
        ):
            raise ValueError("storage worker or queue limit exceeds the hard cap")
        self._pools = {
            "sqlite": ThreadPoolExecutor(max_workers=sqlite_workers, thread_name_prefix="storage-sqlite"),
            "vector": ThreadPoolExecutor(max_workers=vector_workers, thread_name_prefix="storage-vector"),
        }
        self._slots = {
            lane: asyncio.Semaphore(workers + queue_capacity)
            for lane, workers in (("sqlite", sqlite_workers), ("vector", vector_workers))
        }
        self._pending: set[asyncio.Future[Any]] = set()
        self._closed = False

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def closed(self) -> bool:
        return self._closed

    async def run_sqlite(self, function: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        return await self._run("sqlite", function, *args, **kwargs)

    async def run_vector(self, function: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        return await self._run("vector", function, *args, **kwargs)

    async def _run(self, lane: str, function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        if self._closed:
            raise RuntimeError("storage executor is closed")
        slots = self._slots[lane]
        await slots.acquire()
        if self._closed:
            slots.release()
            raise RuntimeError("storage executor is closed")

        loop = asyncio.get_running_loop()
        try:
            future = loop.run_in_executor(self._pools[lane], partial(function, *args, **kwargs))
        except BaseException:
            slots.release()
            raise

        self._pending.add(future)
        future.add_done_callback(lambda completed: self._completed(slots, completed))
        # asyncio.wait does not cancel its input futures when this caller is
        # cancelled. The synchronous operation remains tracked until its done
        # callback releases the slot.
        # A short timeout also bounds the handoff latency if the loop misses a
        # worker-thread wakeup; the completion callback remains the fast path.
        while not future.done():
            await asyncio.wait(
                (future,),
                timeout=0.05,
                return_when=asyncio.FIRST_COMPLETED,
            )
        return future.result()

    def _completed(
        self,
        slots: asyncio.Semaphore,
        future: asyncio.Future[Any],
    ) -> None:
        self._pending.discard(future)
        try:
            future.exception()
        except asyncio.CancelledError:
            pass
        slots.release()

    async def aclose(self, timeout: float = 10.0) -> None:
        if self._closed:
            return
        self._closed = True
        pending = tuple(self._pending)
        if pending:
            _done, still_pending = await asyncio.wait(pending, timeout=max(0.0, timeout))
        else:
            still_pending = set()

        for pool in self._pools.values():
            pool.shutdown(wait=not still_pending, cancel_futures=True)
        if still_pending:
            LOGGER.error(
                "storage work remains after shutdown grace; running sync calls cannot be cancelled: %s",
                len(still_pending),
            )
