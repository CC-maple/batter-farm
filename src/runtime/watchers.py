from __future__ import annotations

from collections import deque
from enum import Enum


class WatcherState(str, Enum):
    DISARMED = "disarmed"
    ARMED_WAITING = "armed_waiting"
    MATCHED = "matched"
    CLEARING = "clearing"
    VERIFYING_ABSENT = "verifying_absent"
    GRACE_PERIOD = "grace_period"
    CLEARED = "cleared"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    FAILED = "failed"
    WAITING_USER = "waiting_user"


class WatcherDecision(str, Enum):
    NONE = "none"
    MATCHED = "matched"
    CLEARED = "cleared"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    WAIT_FOR_USER = "wait_for_user"


class StableMatchWatcher:
    """Pure bounded watcher state for template-score debounce and clearing.

    Capture and clicks deliberately live outside this class. The scheduler feeds
    scores in and turns returned decisions into capture/action commands.
    """

    def __init__(
        self,
        watcher_id: str,
        *,
        formal_threshold: float = 0.92,
        candidate_threshold: float | None = None,
        candidate_window: int = 3,
        candidate_hits: int = 2,
        absent_confirm_frames: int = 2,
        max_actions: int = 5,
    ) -> None:
        if not 0 <= formal_threshold <= 1:
            raise ValueError("formal_threshold must be between 0 and 1")
        candidate_threshold = formal_threshold if candidate_threshold is None else candidate_threshold
        if not 0 <= candidate_threshold <= formal_threshold:
            raise ValueError("candidate_threshold must be between 0 and formal_threshold")
        if candidate_window <= 0 or not 0 < candidate_hits <= candidate_window:
            raise ValueError("candidate frame settings are invalid")
        if absent_confirm_frames <= 0 or max_actions <= 0:
            raise ValueError("absent_confirm_frames and max_actions must be positive")
        self.watcher_id = watcher_id
        self.formal_threshold = float(formal_threshold)
        self.candidate_threshold = float(candidate_threshold)
        self.candidate_hits = int(candidate_hits)
        self.absent_confirm_frames = int(absent_confirm_frames)
        self.max_actions = int(max_actions)
        self.state = WatcherState.DISARMED
        self.action_count = 0
        self._candidate_hits: deque[bool] = deque(maxlen=int(candidate_window))
        self._absent_count = 0

    def arm(self) -> None:
        self.state = WatcherState.ARMED_WAITING
        self.action_count = 0
        self._candidate_hits.clear()
        self._absent_count = 0

    def observe(self, *, score: float | None) -> WatcherDecision:
        if self.state is WatcherState.CANCELLED:
            return WatcherDecision.CANCELLED
        if self.state in {WatcherState.DISARMED, WatcherState.TIMED_OUT, WatcherState.FAILED, WatcherState.WAITING_USER}:
            return WatcherDecision.NONE

        present = score is not None and float(score) >= self.candidate_threshold
        formal = score is not None and float(score) >= self.formal_threshold

        if self.state is WatcherState.VERIFYING_ABSENT:
            if present:
                self._absent_count = 0
                if self.action_count >= self.max_actions:
                    self.state = WatcherState.WAITING_USER
                    return WatcherDecision.WAIT_FOR_USER
                self.state = WatcherState.MATCHED
                return WatcherDecision.MATCHED
            self._absent_count += 1
            if self._absent_count >= self.absent_confirm_frames:
                self.state = WatcherState.CLEARED
                return WatcherDecision.CLEARED
            return WatcherDecision.NONE

        if self.state in {WatcherState.ARMED_WAITING, WatcherState.GRACE_PERIOD, WatcherState.CLEARED, WatcherState.MATCHED}:
            self._candidate_hits.append(present)
            if formal or (
                len(self._candidate_hits) == self._candidate_hits.maxlen
                and sum(self._candidate_hits) >= self.candidate_hits
            ):
                self.state = WatcherState.MATCHED
                self._absent_count = 0
                return WatcherDecision.MATCHED
        return WatcherDecision.NONE

    def record_action(self) -> WatcherDecision:
        if self.state is not WatcherState.MATCHED:
            raise RuntimeError("watcher action requires a matched state")
        self.action_count += 1
        self.state = WatcherState.CLEARING
        return WatcherDecision.NONE

    def begin_verification(self) -> None:
        if self.state not in {WatcherState.MATCHED, WatcherState.CLEARING}:
            raise RuntimeError("verification requires a matched or clearing state")
        self.state = WatcherState.VERIFYING_ABSENT
        self._absent_count = 0
        self._candidate_hits.clear()

    def begin_grace(self) -> None:
        if self.state is not WatcherState.CLEARED:
            raise RuntimeError("grace period requires a cleared state")
        self.state = WatcherState.GRACE_PERIOD
        self._candidate_hits.clear()

    def timeout(self) -> WatcherDecision:
        if self.state in {WatcherState.DISARMED, WatcherState.CANCELLED, WatcherState.CLEARED}:
            return WatcherDecision.NONE
        self.state = WatcherState.TIMED_OUT
        return WatcherDecision.TIMED_OUT

    def cancel(self) -> None:
        self.state = WatcherState.CANCELLED
        self._candidate_hits.clear()
        self._absent_count = 0
