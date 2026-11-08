from __future__ import annotations

import heapq
import threading
import time
from collections import deque
from dataclasses import replace
from typing import Any, Callable, Mapping, Protocol
from uuid import uuid4

from src.runtime.scheduler_models import (
    EventKind,
    JobKind,
    RuntimeCommand,
    ScheduledJob,
    SchedulerEvent,
    SchedulerState,
)


class Clock(Protocol):
    def monotonic(self) -> float: ...


class MonotonicClock:
    def monotonic(self) -> float:
        return time.monotonic()


class SchedulerCore:
    """Pure scheduling state owned by one scheduler thread.

    It performs no capture, inference, input, GUI work, or sleeping. Heap entries
    are immutable; replaced/coalesced entries remain as harmless stale records
    until popped.
    """

    def __init__(self, *, clock: Clock | None = None, max_pending_jobs: int = 256) -> None:
        if max_pending_jobs <= 0:
            raise ValueError("max_pending_jobs must be positive")
        self.clock = clock or MonotonicClock()
        self.max_pending_jobs = int(max_pending_jobs)
        self.run_id = ""
        self.generation = 0
        self.state = SchedulerState.IDLE
        self._state_before_pause = SchedulerState.IDLE
        self._paused_at: float | None = None
        self._sequence = 0
        self._heap: list[ScheduledJob] = []
        self._active: dict[str, ScheduledJob] = {}
        self._coalesced: dict[str, str] = {}

    @property
    def pending_count(self) -> int:
        return len(self._active)

    def begin_run(self, run_id: str | None = None) -> str:
        self.generation += 1
        self.run_id = run_id or uuid4().hex
        self.state = SchedulerState.STARTING
        self._state_before_pause = SchedulerState.STARTING
        self._paused_at = None
        self._clear_jobs()
        return self.run_id

    def set_state(self, state: SchedulerState) -> None:
        if self.state is SchedulerState.PAUSED and state is not SchedulerState.STOPPING:
            self._state_before_pause = state
            return
        self.state = state

    def schedule(
        self,
        kind: JobKind,
        *,
        delay: float = 0.0,
        priority: int,
        payload: Mapping[str, Any] | None = None,
        coalesce_key: str | None = None,
        pull_forward: bool = False,
    ) -> ScheduledJob | None:
        if self.state in {SchedulerState.IDLE, SchedulerState.STOPPING, SchedulerState.COMPLETED, SchedulerState.ERROR}:
            return None
        # The scheduler's logical clock is frozen while paused.  Jobs created
        # during a pause therefore start their delay at the pause boundary;
        # resume() will shift that boundary to the actual resume time exactly
        # once, just like it does for jobs that already existed.
        logical_now = (
            self._paused_at
            if self.state is SchedulerState.PAUSED and self._paused_at is not None
            else self.clock.monotonic()
        )
        due_at = logical_now + max(0.0, float(delay))
        if coalesce_key and coalesce_key in self._coalesced:
            existing_id = self._coalesced[coalesce_key]
            existing = self._active.get(existing_id)
            if existing is not None:
                if not pull_forward or due_at >= existing.due_at:
                    return existing
                replacement = replace(
                    existing,
                    due_at=due_at,
                    priority=min(existing.priority, int(priority)),
                    payload=payload or existing.payload,
                )
                self._active[existing_id] = replacement
                heapq.heappush(self._heap, replacement)
                return replacement

        if len(self._active) >= self.max_pending_jobs:
            worst = max(self._active.values(), key=lambda item: (item.priority, item.due_at, item.sequence))
            if int(priority) >= worst.priority:
                return None
            self.cancel(worst.job_id)

        self._sequence += 1
        job = ScheduledJob(
            due_at=due_at,
            priority=int(priority),
            sequence=self._sequence,
            job_id=uuid4().hex,
            run_id=self.run_id,
            generation=self.generation,
            kind=kind,
            coalesce_key=coalesce_key,
            payload=payload or {},
        )
        self._active[job.job_id] = job
        if coalesce_key:
            self._coalesced[coalesce_key] = job.job_id
        heapq.heappush(self._heap, job)
        return job

    def request_rescan(self) -> ScheduledJob | None:
        return self.schedule(
            JobKind.SCAN,
            delay=0.0,
            priority=60,
            payload={"reason": "rescan"},
            coalesce_key="business-scan",
            pull_forward=True,
        )

    def cancel(self, job_id: str) -> bool:
        job = self._active.pop(job_id, None)
        if job is None:
            return False
        if job.coalesce_key and self._coalesced.get(job.coalesce_key) == job_id:
            self._coalesced.pop(job.coalesce_key, None)
        return True

    def pause(self) -> None:
        if self.state in {SchedulerState.IDLE, SchedulerState.PAUSED, SchedulerState.STOPPING}:
            return
        self._state_before_pause = self.state
        self.state = SchedulerState.PAUSED
        self._paused_at = self.clock.monotonic()

    def resume(self) -> None:
        if self.state is not SchedulerState.PAUSED or self._paused_at is None:
            return
        delta = max(0.0, self.clock.monotonic() - self._paused_at)
        shifted: dict[str, ScheduledJob] = {}
        self._heap.clear()
        for job_id, job in self._active.items():
            updated = replace(job, due_at=job.due_at + delta)
            shifted[job_id] = updated
            heapq.heappush(self._heap, updated)
        self._active = shifted
        self._paused_at = None
        self.state = self._state_before_pause

    def stop(self) -> None:
        self.generation += 1
        self.state = SchedulerState.STOPPING
        self._paused_at = None
        self._clear_jobs()

    def complete(self, *, error: bool = False) -> None:
        self._clear_jobs()
        self.state = SchedulerState.ERROR if error else SchedulerState.COMPLETED

    def reset_idle(self) -> None:
        self._clear_jobs()
        self.state = SchedulerState.IDLE

    def accepts(self, event: SchedulerEvent) -> bool:
        if self.state in {SchedulerState.IDLE, SchedulerState.STOPPING, SchedulerState.COMPLETED, SchedulerState.ERROR}:
            return False
        return event.run_id == self.run_id and event.generation == self.generation

    def pop_due_events(self, now: float | None = None) -> list[SchedulerEvent]:
        if self.state is SchedulerState.PAUSED:
            return []
        current = self.clock.monotonic() if now is None else float(now)
        events: list[SchedulerEvent] = []
        while self._heap and self._heap[0].due_at <= current:
            job = heapq.heappop(self._heap)
            if self._active.get(job.job_id) is not job:
                continue
            self.cancel(job.job_id)
            if job.run_id != self.run_id or job.generation != self.generation:
                continue
            events.append(
                SchedulerEvent.create(
                    job.kind.event_kind,
                    run_id=job.run_id,
                    generation=job.generation,
                    created_at=current,
                    payload={**dict(job.payload), "job_id": job.job_id, "due_at": job.due_at},
                )
            )
        return events

    def seconds_until_next_job(self, default: float | None = None) -> float | None:
        self._discard_stale_heap_head()
        if not self._heap or self.state is SchedulerState.PAUSED:
            return default
        return max(0.0, self._heap[0].due_at - self.clock.monotonic())

    def pending_jobs(self) -> list[ScheduledJob]:
        return sorted(self._active.values())

    def _discard_stale_heap_head(self) -> None:
        while self._heap and self._active.get(self._heap[0].job_id) is not self._heap[0]:
            heapq.heappop(self._heap)

    def _clear_jobs(self) -> None:
        self._heap.clear()
        self._active.clear()
        self._coalesced.clear()


class _CommandEnvelope:
    def __init__(self, command: RuntimeCommand) -> None:
        self.command = command
        self.done = threading.Event()
        self.result: Any = None
        self.error: BaseException | None = None


class SchedulerThread:
    """Long-lived command/event owner used by the worker process."""

    def __init__(
        self,
        handler: Callable[[RuntimeCommand], Any],
        *,
        name: str = "batterfarm-scheduler",
        max_pending_commands: int = 256,
    ) -> None:
        if max_pending_commands <= 0:
            raise ValueError("max_pending_commands must be positive")
        self._handler = handler
        self._queue: deque[_CommandEnvelope] = deque()
        self._max_pending_commands = int(max_pending_commands)
        self._condition = threading.Condition()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._closed = threading.Event()
        self._thread.start()

    @property
    def thread_id(self) -> int | None:
        return self._thread.ident

    def submit(self, command: RuntimeCommand, *, wait: bool = True) -> Any:
        envelope = _CommandEnvelope(command)
        with self._condition:
            while len(self._queue) >= self._max_pending_commands and not self._closed.is_set():
                self._condition.wait()
            if self._closed.is_set():
                raise RuntimeError("scheduler is closed")
            self._queue.append(envelope)
            self._condition.notify()
        if not wait:
            return None
        envelope.done.wait()
        if envelope.error is not None:
            raise envelope.error
        return envelope.result

    def close(self, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            self._closed.set()
            self._condition.notify_all()
        if self._thread is threading.current_thread():
            return
        self._thread.join(timeout=max(0.0, deadline - time.monotonic()))

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._queue:
                    if self._closed.is_set():
                        return
                    self._condition.wait()
                envelope = self._queue.popleft()
                self._condition.notify_all()
            try:
                envelope.result = self._handler(envelope.command)
            except BaseException as exc:
                envelope.error = exc
            finally:
                envelope.done.set()
