from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from src.runtime.action_executor import ActionExecutor
from src.runtime.capture_coordinator import CaptureCoordinator
from src.runtime.scheduler import SchedulerCore, SchedulerThread
from src.runtime.scheduler_models import (
    ActionRequest,
    CaptureRequest,
    CaptureResult,
    EventKind,
    JobKind,
    RecognitionPlan,
    RuntimeCommand,
    SchedulerEvent,
    SchedulerState,
)
from src.runtime.watchers import StableMatchWatcher, WatcherDecision, WatcherState


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class SchedulerCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.scheduler = SchedulerCore(clock=self.clock, max_pending_jobs=8)
        self.scheduler.begin_run("run-1")

    def test_due_jobs_use_priority_then_insertion_order(self) -> None:
        self.scheduler.schedule(JobKind.SCAN, delay=1.0, priority=70, payload={"name": "normal"})
        self.scheduler.schedule(JobKind.TIMEOUT, delay=1.0, priority=40, payload={"name": "verify"})
        self.scheduler.schedule(JobKind.WATCHER, delay=1.0, priority=40, payload={"name": "guard"})

        self.clock.advance(1.0)
        events = self.scheduler.pop_due_events()

        self.assertEqual([event.payload["name"] for event in events], ["verify", "guard", "normal"])

    def test_same_coalesce_key_keeps_one_job_and_can_be_pulled_forward(self) -> None:
        first = self.scheduler.schedule(
            JobKind.SCAN,
            delay=10.0,
            priority=70,
            coalesce_key="business-scan",
        )
        second = self.scheduler.schedule(
            JobKind.SCAN,
            delay=20.0,
            priority=70,
            coalesce_key="business-scan",
        )
        forced = self.scheduler.schedule(
            JobKind.SCAN,
            delay=0.0,
            priority=60,
            coalesce_key="business-scan",
            pull_forward=True,
        )

        self.assertEqual(first.job_id, second.job_id)
        self.assertEqual(first.job_id, forced.job_id)
        self.assertEqual(self.scheduler.pending_count, 1)
        events = self.scheduler.pop_due_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, EventKind.SCAN_DUE)

    def test_periodic_jobs_do_not_backfill_missed_ticks(self) -> None:
        self.scheduler.schedule(JobKind.SCAN, delay=1.0, priority=70, coalesce_key="scan")
        self.clock.advance(20.0)

        self.assertEqual(len(self.scheduler.pop_due_events()), 1)
        self.assertEqual(len(self.scheduler.pop_due_events()), 0)

        self.scheduler.schedule(JobKind.SCAN, delay=1.0, priority=70, coalesce_key="scan")
        self.assertEqual(len(self.scheduler.pop_due_events()), 0)

    def test_pause_freezes_and_resume_shifts_all_deadlines_once(self) -> None:
        self.scheduler.schedule(JobKind.TIMEOUT, delay=10.0, priority=40)
        self.clock.advance(2.0)
        self.scheduler.pause()
        self.clock.advance(30.0)
        self.assertEqual(self.scheduler.pop_due_events(), [])

        self.scheduler.resume()
        self.clock.advance(7.9)
        self.assertEqual(self.scheduler.pop_due_events(), [])
        self.clock.advance(0.1)
        self.assertEqual(len(self.scheduler.pop_due_events()), 1)

    def test_job_created_during_pause_starts_delay_from_resume(self) -> None:
        self.scheduler.pause()
        self.clock.advance(10.0)
        self.scheduler.schedule(JobKind.SCAN, delay=1.0, priority=70)
        self.clock.advance(10.0)

        self.scheduler.resume()
        self.clock.advance(0.9)
        self.assertEqual(self.scheduler.pop_due_events(), [])
        self.clock.advance(0.1)
        self.assertEqual(len(self.scheduler.pop_due_events()), 1)

    def test_stop_invalidates_jobs_and_old_results(self) -> None:
        old_generation = self.scheduler.generation
        self.scheduler.schedule(JobKind.SCAN, delay=0.0, priority=70)
        self.scheduler.stop()

        self.assertGreater(self.scheduler.generation, old_generation)
        self.assertEqual(self.scheduler.state, SchedulerState.STOPPING)
        self.assertEqual(self.scheduler.pop_due_events(), [])
        stale = SchedulerEvent.create(
            EventKind.CAPTURE_COMPLETED,
            run_id="run-1",
            generation=old_generation,
            created_at=self.clock.monotonic(),
        )
        self.assertFalse(self.scheduler.accepts(stale))

    def test_rescan_requests_coalesce(self) -> None:
        self.scheduler.request_rescan()
        self.scheduler.request_rescan()
        self.scheduler.request_rescan()

        self.assertEqual(self.scheduler.pending_count, 1)
        self.assertEqual(len(self.scheduler.pop_due_events()), 1)

    def test_queue_limit_drops_low_priority_job_but_keeps_stop_out_of_heap(self) -> None:
        scheduler = SchedulerCore(clock=self.clock, max_pending_jobs=2)
        scheduler.begin_run("run-2")
        scheduler.schedule(JobKind.REFRESH, delay=5, priority=90, coalesce_key="refresh-1")
        scheduler.schedule(JobKind.SCAN, delay=5, priority=70, coalesce_key="scan-1")
        accepted = scheduler.schedule(JobKind.WATCHER, delay=5, priority=20, coalesce_key="watcher-1")

        self.assertIsNotNone(accepted)
        self.assertEqual(scheduler.pending_count, 2)
        self.assertEqual({job.kind for job in scheduler.pending_jobs()}, {JobKind.SCAN, JobKind.WATCHER})


class SchedulerThreadTests(unittest.TestCase):
    @staticmethod
    def _command(name: str) -> RuntimeCommand:
        return RuntimeCommand(EventKind.REFRESH_DUE, payload={"name": name})

    def test_close_timeout_is_not_blocked_by_a_full_command_queue(self) -> None:
        handler_started = threading.Event()
        release_handler = threading.Event()
        processed: list[str] = []

        def handler(command: RuntimeCommand) -> None:
            name = str(command.payload["name"])
            if name == "first":
                handler_started.set()
                release_handler.wait(2)
            processed.append(name)

        scheduler = SchedulerThread(handler, max_pending_commands=1)
        try:
            scheduler.submit(self._command("first"), wait=False)
            self.assertTrue(handler_started.wait(1))
            scheduler.submit(self._command("second"), wait=False)

            started_at = time.monotonic()
            scheduler.close(timeout=0.05)
            elapsed = time.monotonic() - started_at

            self.assertLess(elapsed, 0.2)
            self.assertTrue(scheduler._thread.is_alive())
            release_handler.set()
            scheduler.close(timeout=1)
            self.assertFalse(scheduler._thread.is_alive())
            self.assertEqual(processed, ["first", "second"])
        finally:
            release_handler.set()
            scheduler.close(timeout=1)

    def test_submitter_waiting_for_capacity_is_rejected_when_close_wins(self) -> None:
        handler_started = threading.Event()
        release_handler = threading.Event()
        submit_waiting = threading.Event()
        submit_finished = threading.Event()
        submit_error: list[BaseException] = []
        processed: list[str] = []

        def handler(command: RuntimeCommand) -> None:
            name = str(command.payload["name"])
            if name == "first":
                handler_started.set()
                release_handler.wait(2)
            processed.append(name)

        scheduler = SchedulerThread(handler, max_pending_commands=1)
        submitter: threading.Thread | None = None
        original_wait = scheduler._condition.wait

        def observed_wait(timeout: float | None = None) -> bool:
            if threading.current_thread() is submitter:
                submit_waiting.set()
            return original_wait(timeout)

        scheduler._condition.wait = observed_wait

        def submit_third() -> None:
            try:
                scheduler.submit(self._command("third"))
            except BaseException as exc:
                submit_error.append(exc)
            finally:
                submit_finished.set()

        try:
            scheduler.submit(self._command("first"), wait=False)
            self.assertTrue(handler_started.wait(1))
            scheduler.submit(self._command("second"), wait=False)
            submitter = threading.Thread(target=submit_third, daemon=True)
            submitter.start()
            self.assertTrue(submit_waiting.wait(1))

            scheduler.close(timeout=0.05)

            self.assertTrue(submit_finished.wait(0.2))
            self.assertEqual(len(submit_error), 1)
            self.assertIsInstance(submit_error[0], RuntimeError)
            release_handler.set()
            scheduler.close(timeout=1)
            self.assertFalse(scheduler._thread.is_alive())
            self.assertEqual(processed, ["first", "second"])
        finally:
            release_handler.set()
            scheduler.close(timeout=1)
            if submitter is not None:
                submitter.join(timeout=1)


class StableMatchWatcherTests(unittest.TestCase):
    def test_high_score_matches_immediately(self) -> None:
        watcher = StableMatchWatcher(
            "fish_card_result",
            formal_threshold=0.92,
            candidate_threshold=0.86,
            candidate_window=3,
            candidate_hits=2,
            absent_confirm_frames=2,
            max_actions=5,
        )
        watcher.arm()

        decision = watcher.observe(score=0.94)

        self.assertEqual(decision, WatcherDecision.MATCHED)
        self.assertEqual(watcher.state, WatcherState.MATCHED)

    def test_candidate_scores_require_two_of_three_frames(self) -> None:
        watcher = StableMatchWatcher(
            "fish_card_result",
            formal_threshold=0.92,
            candidate_threshold=0.86,
            candidate_window=3,
            candidate_hits=2,
        )
        watcher.arm()

        self.assertEqual(watcher.observe(score=0.88), WatcherDecision.NONE)
        self.assertEqual(watcher.observe(score=0.40), WatcherDecision.NONE)
        self.assertEqual(watcher.observe(score=0.89), WatcherDecision.MATCHED)

    def test_absent_requires_consecutive_frames_and_rearms_during_grace(self) -> None:
        watcher = StableMatchWatcher("fish_card_result", absent_confirm_frames=2)
        watcher.arm()
        watcher.observe(score=1.0)
        watcher.begin_verification()

        self.assertEqual(watcher.observe(score=None), WatcherDecision.NONE)
        self.assertEqual(watcher.observe(score=None), WatcherDecision.CLEARED)
        watcher.begin_grace()
        self.assertEqual(watcher.observe(score=1.0), WatcherDecision.MATCHED)

    def test_action_limit_waits_for_user(self) -> None:
        watcher = StableMatchWatcher("fish_card_result", max_actions=2)
        watcher.arm()
        watcher.observe(score=1.0)
        self.assertEqual(watcher.record_action(), WatcherDecision.NONE)
        watcher.begin_verification()
        watcher.observe(score=1.0)
        self.assertEqual(watcher.record_action(), WatcherDecision.NONE)
        watcher.begin_verification()
        self.assertEqual(watcher.observe(score=1.0), WatcherDecision.WAIT_FOR_USER)
        self.assertEqual(watcher.state, WatcherState.WAITING_USER)

    def test_last_allowed_action_is_verified_before_waiting_for_user(self) -> None:
        watcher = StableMatchWatcher("fish_card_result", max_actions=1, absent_confirm_frames=2)
        watcher.arm()
        watcher.observe(score=1.0)

        self.assertEqual(watcher.record_action(), WatcherDecision.NONE)
        watcher.begin_verification()
        self.assertEqual(watcher.observe(score=None), WatcherDecision.NONE)
        self.assertEqual(watcher.observe(score=None), WatcherDecision.CLEARED)
        self.assertEqual(watcher.state, WatcherState.CLEARED)

    def test_cancel_rejects_late_observation(self) -> None:
        watcher = StableMatchWatcher("fish_card_result")
        watcher.arm()
        watcher.cancel()
        self.assertEqual(watcher.observe(score=1.0), WatcherDecision.CANCELLED)


class ResourceCoordinatorTests(unittest.TestCase):
    def test_action_executor_rejects_second_action_while_lease_is_held(self) -> None:
        executor = ActionExecutor()
        started = threading.Event()
        release = threading.Event()
        first = ActionRequest("a1", "run", 1, "first")
        second = ActionRequest("a2", "run", 1, "second")
        try:
            executor.submit(first, lambda: started.set() or release.wait(1))
            self.assertTrue(started.wait(1))
            with self.assertRaisesRegex(RuntimeError, "already running"):
                executor.submit(second, lambda: None)
            release.set()
            self.assertTrue(executor.wait_idle(1))
        finally:
            release.set()
            executor.close()

    def test_action_executor_survives_callback_failure(self) -> None:
        executor = ActionExecutor()
        callback_called = threading.Event()
        second_ran = threading.Event()

        def failing_callback(_result) -> None:
            callback_called.set()
            raise RuntimeError("callback failed")

        try:
            with self.assertLogs(level="ERROR"):
                executor.submit(
                    ActionRequest("a1", "run", 1, "first"),
                    lambda: None,
                    callback=failing_callback,
                )
                self.assertTrue(callback_called.wait(1))
                self.assertTrue(executor.wait_idle(1))

            executor.submit(ActionRequest("a2", "run", 1, "second"), second_ran.set)
            self.assertTrue(second_ran.wait(1))
            self.assertTrue(executor.wait_idle(1))
            self.assertTrue(executor.thread.is_alive())
        finally:
            executor.close()

    def test_capture_coordinator_reuses_equivalent_in_flight_request(self) -> None:
        coordinator = CaptureCoordinator()
        started = threading.Event()
        release = threading.Event()
        callbacks: list[str] = []
        request = CaptureRequest(
            "capture-1",
            "run",
            1,
            "fish-card",
            "require_foreground",
            RecognitionPlan(templates=("fish_card_result",)),
        )

        def operation(current: CaptureRequest) -> CaptureResult:
            started.set()
            release.wait(1)
            return CaptureResult(current.request_id, current.run_id, current.generation, "frame", 1.0, 42, (0, 0), (1600, 900))

        try:
            first_id = coordinator.submit(request, operation, callback=lambda result: callbacks.append("first"))
            self.assertTrue(started.wait(1))
            second_id = coordinator.submit(
                CaptureRequest(
                    "capture-2",
                    "run",
                    1,
                    "fish-card-repeat",
                    "require_foreground",
                    RecognitionPlan(templates=("fish_card_result",)),
                ),
                operation,
                callback=lambda result: callbacks.append("second"),
            )
            self.assertEqual(first_id, second_id)
            release.set()
            for _ in range(100):
                if len(callbacks) == 2:
                    break
                threading.Event().wait(0.001)
            self.assertEqual(callbacks, ["first", "second"])
        finally:
            release.set()
            coordinator.close()

    def test_capture_coordinator_isolates_callback_failure(self) -> None:
        coordinator = CaptureCoordinator()
        started = threading.Event()
        release = threading.Event()
        second_callback = threading.Event()
        next_capture = threading.Event()
        request = CaptureRequest(
            "capture-1",
            "run",
            1,
            "fish-card",
            "require_foreground",
            RecognitionPlan(templates=("fish_card_result",)),
        )

        def operation(current: CaptureRequest) -> CaptureResult:
            started.set()
            release.wait(1)
            return CaptureResult(current.request_id, current.run_id, current.generation, "frame", 1.0, 42, (0, 0), (1600, 900))

        def failing_callback(_result) -> None:
            raise RuntimeError("callback failed")

        try:
            with self.assertLogs(level="ERROR"):
                coordinator.submit(request, operation, callback=failing_callback)
                self.assertTrue(started.wait(1))
                coordinator.submit(
                    CaptureRequest(
                        "capture-2",
                        "run",
                        1,
                        "fish-card-repeat",
                        "require_foreground",
                        RecognitionPlan(templates=("fish_card_result",)),
                    ),
                    operation,
                    callback=lambda _result: second_callback.set(),
                )
                release.set()
                self.assertTrue(second_callback.wait(1))

            coordinator.submit(
                CaptureRequest(
                    "capture-3",
                    "run",
                    1,
                    "next",
                    "passive",
                    RecognitionPlan(templates=("other",)),
                ),
                lambda current: (
                    next_capture.set()
                    or CaptureResult(current.request_id, current.run_id, current.generation, "next", 2.0, 42, (0, 0), (1600, 900))
                ),
                callback=lambda _result: None,
            )
            self.assertTrue(next_capture.wait(1))
            self.assertTrue(coordinator._thread.is_alive())
        finally:
            release.set()
            coordinator.close()


if __name__ == "__main__":
    unittest.main()
