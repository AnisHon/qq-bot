from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from contextlib import suppress
from enum import StrEnum
import logging
from typing import Awaitable, Callable

from app.progress.coordinator import ProgressCandidate, ProgressCoordinator
from app.progress.presentation import render_progress
from app.qq_dispatcher import DeliveryResult, QQMessageDispatcher


LOGGER = logging.getLogger(__name__)
REPORTER_WORKERS = 2
GROUP_PROGRESS_INTERVAL_SECONDS = 8.0
GLOBAL_PROGRESS_WINDOW_SECONDS = 60.0
MAX_GLOBAL_PROGRESS_ATTEMPTS = 30
MAX_TRACKED_GROUPS = 512


class FinalDeliveryStatus(StrEnum):
    ACCEPTED_BY_ADAPTER = "accepted_by_adapter"
    SUPPRESSED = "suppressed"
    STALE_EPOCH = "stale_epoch"
    FAILED = "failed"
    CANCELLED = "cancelled"


EpochReader = Callable[[str], Awaitable[int]]


class QQProgressReporter:
    """Bounded status delivery and final-message fencing over the existing dispatcher."""

    def __init__(
        self,
        coordinator: ProgressCoordinator,
        dispatcher: QQMessageDispatcher,
        *,
        read_epoch: EpochReader,
        workers: int = REPORTER_WORKERS,
    ) -> None:
        self.coordinator = coordinator
        self.dispatcher = dispatcher
        self.read_epoch = read_epoch
        self.worker_count = max(1, min(4, int(workers)))
        self._workers: list[asyncio.Task[None]] = []
        self._closing = False
        self._inflight = 0
        self._last_group_attempt: OrderedDict[str, float] = OrderedDict()
        self._global_attempts: deque[float] = deque()

    def start(self) -> None:
        if self._closing:
            raise RuntimeError("progress reporter is closed")
        if self._workers:
            return
        self._workers = [
            asyncio.create_task(self._run_worker(index), name=f"qq-progress-reporter-{index}")
            for index in range(self.worker_count)
        ]

    async def _run_worker(self, worker_index: int) -> None:
        while not self._closing:
            candidate = await self.coordinator.mailbox.get()
            if candidate is None:
                return
            self._inflight += 1
            try:
                await self._send_candidate(candidate)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - progress delivery never owns Agent success
                self.coordinator.metrics["progress_send_failed"] += 1
                LOGGER.warning("progress delivery failed (%s)", type(exc).__name__)
            finally:
                self._inflight -= 1

    async def _send_candidate(self, candidate: ProgressCandidate) -> None:
        if candidate.expires_monotonic <= self.coordinator.clock.monotonic():
            self.coordinator.metrics["progress_expired_before_lock"] += 1
            return
        async with self.dispatcher.delivery_barrier(candidate.identity.group_id):
            # Recheck both task state and epoch at the actual adapter boundary.
            if candidate.expires_monotonic <= self.coordinator.clock.monotonic():
                self.coordinator.metrics["progress_expired_before_adapter"] += 1
                return
            if not self.coordinator.delivery_is_current(candidate.identity, candidate.sequence):
                self.coordinator.metrics["progress_stale_or_terminal_dropped"] += 1
                return
            try:
                current_epoch = await self.read_epoch(candidate.identity.group_id)
            except Exception as exc:  # noqa: BLE001 - fail closed for epoch fencing
                self.coordinator.metrics["progress_epoch_check_failed"] += 1
                LOGGER.warning("progress epoch check failed (%s)", type(exc).__name__)
                return
            if current_epoch != candidate.identity.epoch:
                self.coordinator.invalidate_group_epoch(candidate.identity.group_id, current_epoch)
                self.coordinator.metrics["progress_stale_epoch_dropped"] += 1
                return
            now = self.coordinator.clock.monotonic()
            group_id = candidate.identity.group_id
            last_group = self._last_group_attempt.get(group_id)
            if last_group is not None and now - last_group < GROUP_PROGRESS_INTERVAL_SECONDS:
                self.coordinator.metrics["progress_group_rate_limited"] += 1
                return
            while self._global_attempts and now - self._global_attempts[0] >= GLOBAL_PROGRESS_WINDOW_SECONDS:
                self._global_attempts.popleft()
            if len(self._global_attempts) >= MAX_GLOBAL_PROGRESS_ATTEMPTS:
                self.coordinator.metrics["progress_global_rate_limited"] += 1
                return
            self._global_attempts.append(now)
            self._last_group_attempt[group_id] = now
            self._last_group_attempt.move_to_end(group_id)
            while len(self._last_group_attempt) > MAX_TRACKED_GROUPS:
                self._last_group_attempt.popitem(last=False)
            result = await self.dispatcher.send_locked(
                candidate.identity.group_id,
                render_progress(candidate),
                deduplicate=True,
                status=True,
            )
            if result == DeliveryResult.SUPPRESSED:
                self.coordinator.metrics["progress_suppressed"] += 1
            else:
                self.coordinator.metrics["progress_adapter_accepted"] += 1

    async def send_final(
        self,
        group_id: str,
        text: str,
        *,
        reply_to: str = "",
        task_id: str | None = None,
        epoch: int | None = None,
    ) -> FinalDeliveryStatus:
        # Closing before waiting for the group lock invalidates pending candidates.
        if task_id:
            self.coordinator.close_task(task_id)
        group_key = str(group_id)
        try:
            async with self.dispatcher.delivery_barrier(group_key):
                if epoch is not None:
                    try:
                        current_epoch = await self.read_epoch(group_key)
                    except Exception as exc:  # noqa: BLE001
                        self.coordinator.metrics["final_epoch_check_failed"] += 1
                        LOGGER.warning("final epoch check failed (%s)", type(exc).__name__)
                        return self._finish_final(task_id, FinalDeliveryStatus.FAILED)
                    if current_epoch != epoch:
                        self.coordinator.metrics["final_stale_epoch_dropped"] += 1
                        return self._finish_final(task_id, FinalDeliveryStatus.STALE_EPOCH)
                try:
                    result = await self.dispatcher.send_locked(
                        group_key, text, reply_to=reply_to, deduplicate=False
                    )
                except Exception as exc:  # noqa: BLE001 - execution outcome is already final
                    self.coordinator.metrics["final_send_failed"] += 1
                    LOGGER.warning("final QQ delivery failed (%s)", type(exc).__name__)
                    return self._finish_final(task_id, FinalDeliveryStatus.FAILED)
                if result == DeliveryResult.SUPPRESSED:
                    self.coordinator.metrics["final_suppressed"] += 1
                    return self._finish_final(task_id, FinalDeliveryStatus.SUPPRESSED)
                self.coordinator.metrics["final_adapter_accepted"] += 1
                return self._finish_final(task_id, FinalDeliveryStatus.ACCEPTED_BY_ADAPTER)
        except asyncio.CancelledError:
            self._finish_final(task_id, FinalDeliveryStatus.CANCELLED)
            raise

    def _finish_final(self, task_id: str | None, status: FinalDeliveryStatus) -> FinalDeliveryStatus:
        self.coordinator.record_final_delivery(task_id, status.value)
        return status

    async def aclose(self, timeout: float = 2.0) -> None:
        self._closing = True
        self.coordinator.metrics["progress_shutdown_inflight_unknown"] += self._inflight
        workers, self._workers = self._workers, []
        for worker in workers:
            worker.cancel()
        if workers:
            with suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(asyncio.gather(*workers, return_exceptions=True), timeout=max(0.0, timeout))
