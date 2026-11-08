from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping
from uuid import uuid4


class SchedulerState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    SCANNING = "scanning"
    SELECTING = "selecting"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    POST_ACTION_WATCH = "post_action_watch"
    WAITING_USER = "waiting_user"
    PAUSED = "paused"
    STOPPING = "stopping"
    COMPLETED = "completed"
    ERROR = "error"


class EventKind(str, Enum):
    START_REQUESTED = "start_requested"
    PAUSE_REQUESTED = "pause_requested"
    RESUME_REQUESTED = "resume_requested"
    STOP_REQUESTED = "stop_requested"
    EMERGENCY_STOP_REQUESTED = "emergency_stop_requested"
    RESCAN_REQUESTED = "rescan_requested"
    OVERLAY_PREFERENCE_CHANGED = "overlay_preference_changed"
    SHUTDOWN_REQUESTED = "shutdown_requested"
    SCAN_DUE = "scan_due"
    WATCHER_DUE = "watcher_due"
    RETRY_DUE = "retry_due"
    TIMEOUT_DUE = "timeout_due"
    COOLDOWN_EXPIRED = "cooldown_expired"
    REFRESH_DUE = "refresh_due"
    CAPTURE_STARTED = "capture_started"
    CAPTURE_COMPLETED = "capture_completed"
    CAPTURE_FAILED = "capture_failed"
    CAPTURE_SKIPPED = "capture_skipped"
    RECOGNITION_COMPLETED = "recognition_completed"
    RECOGNITION_FAILED = "recognition_failed"
    ACTION_STARTED = "action_started"
    ACTION_CHECKPOINT = "action_checkpoint"
    ACTION_COMPLETED = "action_completed"
    ACTION_FAILED = "action_failed"
    ACTION_INTERRUPTED = "action_interrupted"
    INPUT_RELEASED = "input_released"
    WATCHER_ARMED = "watcher_armed"
    WATCHER_MATCHED = "watcher_matched"
    WATCHER_MISSED = "watcher_missed"
    WATCHER_CLEARED = "watcher_cleared"
    WATCHER_TIMED_OUT = "watcher_timed_out"
    WATCHER_CANCELLED = "watcher_cancelled"


class JobKind(str, Enum):
    SCAN = "scan"
    WATCHER = "watcher"
    RETRY = "retry"
    TIMEOUT = "timeout"
    COOLDOWN = "cooldown"
    REFRESH = "refresh"

    @property
    def event_kind(self) -> EventKind:
        return {
            JobKind.SCAN: EventKind.SCAN_DUE,
            JobKind.WATCHER: EventKind.WATCHER_DUE,
            JobKind.RETRY: EventKind.RETRY_DUE,
            JobKind.TIMEOUT: EventKind.TIMEOUT_DUE,
            JobKind.COOLDOWN: EventKind.COOLDOWN_EXPIRED,
            JobKind.REFRESH: EventKind.REFRESH_DUE,
        }[self]


@dataclass(frozen=True)
class RuntimeCommand:
    kind: EventKind
    payload: Mapping[str, Any] = field(default_factory=dict)
    request_id: str | None = None
    command_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True)
class SchedulerEvent:
    kind: EventKind
    run_id: str
    generation: int
    event_id: str
    created_at: float
    payload: Mapping[str, Any] = field(default_factory=dict)
    request_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    @classmethod
    def create(
        cls,
        kind: EventKind,
        *,
        run_id: str,
        generation: int,
        created_at: float,
        payload: Mapping[str, Any] | None = None,
        request_id: str | None = None,
    ) -> "SchedulerEvent":
        return cls(
            kind=kind,
            run_id=run_id,
            generation=generation,
            event_id=uuid4().hex,
            created_at=created_at,
            payload=payload or {},
            request_id=request_id,
        )


@dataclass(order=True, frozen=True)
class ScheduledJob:
    due_at: float
    priority: int
    sequence: int
    job_id: str = field(compare=False)
    run_id: str = field(compare=False)
    generation: int = field(compare=False)
    kind: JobKind = field(compare=False)
    coalesce_key: str | None = field(compare=False, default=None)
    payload: Mapping[str, Any] = field(compare=False, default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True)
class RecognitionPlan:
    templates: tuple[str, ...] = ()
    detector_classes: tuple[str, ...] = ()
    ocr_regions: tuple[str, ...] = ()


@dataclass(frozen=True)
class CaptureRequest:
    request_id: str
    run_id: str
    generation: int
    reason: str
    activate_policy: str
    recognition_plan: RecognitionPlan
    deadline: float | None = None
    subscribers: tuple[str, ...] = ()

    @property
    def equivalence_key(self) -> tuple[Any, ...]:
        return (self.run_id, self.generation, self.activate_policy, self.recognition_plan)


@dataclass(frozen=True)
class CaptureResult:
    request_id: str
    run_id: str
    generation: int
    capture_id: str
    captured_at: float
    hwnd: int | None
    client_origin: tuple[int, int]
    client_size: tuple[int, int]
    payload: Any = None
    error: str | None = None


@dataclass(frozen=True)
class ActionRequest:
    action_id: str
    run_id: str
    generation: int
    name: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True)
class ActionResult:
    action_id: str
    run_id: str
    generation: int
    name: str
    value: Any = None
    error: str | None = None
