from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol


class CameraReplayError(ValueError):
    """Raised when a semantic camera recording is missing or invalid."""


class CameraReplayInterrupted(Exception):
    """Raised when the caller requests an immediate camera replay stop."""


class CameraInputController(Protocol):
    def move_to(self, x: int, y: int, duration_seconds: float | None = None) -> None: ...

    def move_relative(self, dx: int, dy: int, duration_seconds: float | None = None) -> None: ...

    def mouse_down(self, button: str = "left", x: int | None = None, y: int | None = None) -> None: ...

    def mouse_up(self, button: str = "left", after_delay_seconds: float | None = None) -> None: ...

    def release_all(self) -> None: ...


@dataclass(frozen=True)
class CameraSettings:
    step_dx: int = 4
    interval_ms: int = 10
    anchor_client: tuple[int, int] = (800, 450)
    segment_ms: int = 250
    segment_gap_ms: int = 30
    invert: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_dx": int(self.step_dx),
            "interval_ms": int(self.interval_ms),
            "anchor_client": [int(self.anchor_client[0]), int(self.anchor_client[1])],
            "segment_ms": int(self.segment_ms),
            "segment_gap_ms": int(self.segment_gap_ms),
            "invert": bool(self.invert),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CameraSettings:
        raw_anchor = value.get("anchor_client", [800, 450])
        if not isinstance(raw_anchor, (list, tuple)):
            raise CameraReplayError("camera settings anchor_client must be a two-item list")
        anchor = tuple(int(item) for item in raw_anchor)
        if len(anchor) != 2:
            raise CameraReplayError("camera settings anchor_client must contain two integers")
        return cls(
            step_dx=max(1, abs(int(value.get("step_dx", 4)))),
            interval_ms=max(1, int(value.get("interval_ms", 10))),
            anchor_client=(anchor[0], anchor[1]),
            segment_ms=max(1, int(value.get("segment_ms", 250))),
            segment_gap_ms=max(0, int(value.get("segment_gap_ms", 30))),
            invert=bool(value.get("invert", False)),
        )


@dataclass(frozen=True)
class CameraTurnEvent:
    t_ms: int
    mode: str
    direction: str
    duration_ms: int


@dataclass(frozen=True)
class CameraRecording:
    path: Path
    mode: str
    duration_ms: int
    settings: CameraSettings
    events: tuple[CameraTurnEvent, ...]
    payload: dict[str, Any]


@dataclass(frozen=True)
class CameraReplayControl:
    stop_requested: Callable[[], bool] = lambda: False
    paused: Callable[[], bool] = lambda: False


class _ActiveTimer:
    def __init__(
        self,
        control: CameraReplayControl,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        self.control = control
        self.clock = clock
        self.sleep = sleep
        self.paused_seconds = 0.0

    def active_now(self) -> float:
        return self.clock() - self.paused_seconds

    def check(self) -> None:
        if self.control.stop_requested():
            raise CameraReplayInterrupted("camera replay interrupted")
        if not self.control.paused():
            return

        pause_started = self.clock()
        while self.control.paused():
            if self.control.stop_requested():
                raise CameraReplayInterrupted("camera replay interrupted while paused")
            self.sleep(0.005)
        self.paused_seconds += max(0.0, self.clock() - pause_started)

    def wait_until(self, active_deadline: float) -> None:
        while True:
            self.check()
            remaining = float(active_deadline) - self.active_now()
            if remaining <= 0:
                return
            self.sleep(min(0.005, remaining))

    def wait(self, seconds: float) -> None:
        self.wait_until(self.active_now() + max(0.0, float(seconds)))


def load_camera_recording(path: str | Path, require_mode: str | None = None) -> CameraRecording:
    recording_path = Path(path)
    if not recording_path.exists():
        raise CameraReplayError(f"camera recording does not exist: {recording_path}")
    if not recording_path.is_file():
        raise CameraReplayError(f"camera recording is not a file: {recording_path}")

    try:
        payload = json.loads(recording_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CameraReplayError(f"camera recording could not be read: {recording_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CameraReplayError("camera recording root must be an object")
    if str(payload.get("recording_type", "")) != "camera_turn":
        raise CameraReplayError("camera recording_type must be 'camera_turn'")

    mode = str(payload.get("mode", "")).strip().lower()
    if mode not in {"hidden", "visible"}:
        raise CameraReplayError("camera recording mode must be hidden or visible")
    normalized_required = str(require_mode).strip().lower() if require_mode is not None else None
    if normalized_required and mode != normalized_required:
        raise CameraReplayError(f"camera recording mode must be {normalized_required!r}, got {mode!r}")

    try:
        duration_ms = int(payload.get("duration_ms"))
    except (TypeError, ValueError) as exc:
        raise CameraReplayError("camera recording duration_ms must be an integer") from exc
    if duration_ms <= 0:
        raise CameraReplayError("camera recording duration_ms must be positive")

    raw_settings = payload.get("settings", {})
    if not isinstance(raw_settings, dict):
        raise CameraReplayError("camera recording settings must be an object")
    settings = CameraSettings.from_dict(raw_settings)

    raw_events = payload.get("events")
    if not isinstance(raw_events, list) or not raw_events:
        raise CameraReplayError("camera recording events must be a non-empty list")

    events: list[CameraTurnEvent] = []
    previous_t_ms = -1
    for index, raw_event in enumerate(raw_events, start=1):
        if not isinstance(raw_event, dict):
            raise CameraReplayError(f"camera event #{index} must be an object")
        if str(raw_event.get("type", "")) != "camera_turn":
            raise CameraReplayError(f"camera event #{index} type must be 'camera_turn'")
        try:
            t_ms = int(raw_event.get("t_ms"))
            event_duration_ms = int(raw_event.get("duration_ms"))
        except (TypeError, ValueError) as exc:
            raise CameraReplayError(f"camera event #{index} timing values must be integers") from exc
        event_mode = str(raw_event.get("mode", mode)).strip().lower()
        direction = str(raw_event.get("direction", "")).strip().lower()
        if t_ms < 0 or t_ms < previous_t_ms:
            raise CameraReplayError("camera event t_ms values must be non-negative and monotonic")
        if event_duration_ms <= 0:
            raise CameraReplayError(f"camera event #{index} duration_ms must be positive")
        if event_mode != mode:
            raise CameraReplayError(
                f"camera event #{index} mode {event_mode!r} differs from recording mode {mode!r}"
            )
        if direction not in {"left", "right"}:
            raise CameraReplayError(f"camera event #{index} direction must be left or right")
        if t_ms + event_duration_ms > duration_ms:
            raise CameraReplayError(f"camera event #{index} extends past recording duration_ms")
        events.append(
            CameraTurnEvent(
                t_ms=t_ms,
                mode=event_mode,
                direction=direction,
                duration_ms=event_duration_ms,
            )
        )
        previous_t_ms = t_ms

    return CameraRecording(
        path=recording_path,
        mode=mode,
        duration_ms=duration_ms,
        settings=settings,
        events=tuple(events),
        payload=payload,
    )


def execute_camera_turn(
    controller: CameraInputController,
    origin: tuple[int, int],
    mode: str,
    direction: str,
    settings: CameraSettings,
    duration_ms: int | None,
    control: CameraReplayControl | None = None,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    _timer: _ActiveTimer | None = None,
) -> None:
    normalized_mode = str(mode).strip().lower()
    normalized_direction = str(direction).strip().lower()
    if normalized_mode not in {"hidden", "visible"}:
        raise CameraReplayError(f"unsupported camera mode: {mode!r}")
    if normalized_direction not in {"left", "right"}:
        raise CameraReplayError(f"unsupported camera direction: {direction!r}")
    if duration_ms is not None and int(duration_ms) <= 0:
        raise CameraReplayError("camera turn duration_ms must be positive")

    timer = _timer or _ActiveTimer(control or CameraReplayControl(), clock, sleep)
    direction_sign = -1 if normalized_direction == "left" else 1
    if settings.invert:
        direction_sign *= -1
    step_dx = direction_sign * max(1, abs(int(settings.step_dx)))
    interval_seconds = max(0.001, settings.interval_ms / 1000.0)
    turn_deadline = float("inf") if duration_ms is None else timer.active_now() + int(duration_ms) / 1000.0

    if normalized_mode == "hidden":
        next_tick = timer.active_now()
        while timer.active_now() < turn_deadline:
            timer.check()
            controller.move_relative(step_dx, 0, duration_seconds=0.0)
            next_tick = min(turn_deadline, next_tick + interval_seconds)
            timer.wait_until(next_tick)
        return

    anchor_x = int(origin[0]) + int(settings.anchor_client[0])
    anchor_y = int(origin[1]) + int(settings.anchor_client[1])
    segment_seconds = max(0.001, settings.segment_ms / 1000.0)
    gap_seconds = max(0.0, settings.segment_gap_ms / 1000.0)

    while timer.active_now() < turn_deadline:
        timer.check()
        controller.move_to(anchor_x, anchor_y, duration_seconds=0.0)
        controller.mouse_down("left")
        try:
            segment_deadline = min(turn_deadline, timer.active_now() + segment_seconds)
            next_tick = timer.active_now()
            while timer.active_now() < segment_deadline:
                timer.check()
                controller.move_relative(step_dx, 0, duration_seconds=0.0)
                next_tick = min(segment_deadline, next_tick + interval_seconds)
                timer.wait_until(next_tick)
        finally:
            controller.mouse_up("left", after_delay_seconds=0.0)

        remaining_seconds = max(0.0, turn_deadline - timer.active_now())
        if gap_seconds > 0 and remaining_seconds > 0:
            timer.wait(min(gap_seconds, remaining_seconds))


def replay_camera_recording(
    recording: CameraRecording,
    controller: CameraInputController,
    origin: tuple[int, int],
    control: CameraReplayControl | None = None,
    require_mode: str | None = None,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    normalized_required = str(require_mode).strip().lower() if require_mode is not None else None
    if normalized_required and recording.mode != normalized_required:
        raise CameraReplayError(
            f"camera recording mode must be {normalized_required!r}, got {recording.mode!r}"
        )

    timer = _ActiveTimer(control or CameraReplayControl(), clock, sleep)
    replay_started = timer.active_now()
    logging.info(
        "restaurant camera replay start path=%s mode=%s events=%s duration_ms=%s",
        recording.path,
        recording.mode,
        len(recording.events),
        recording.duration_ms,
    )
    try:
        for index, event in enumerate(recording.events, start=1):
            timer.wait_until(replay_started + event.t_ms / 1000.0)
            logging.info(
                "restaurant camera replay event index=%s direction=%s duration_ms=%s t_ms=%s",
                index,
                event.direction,
                event.duration_ms,
                event.t_ms,
            )
            execute_camera_turn(
                controller=controller,
                origin=origin,
                mode=recording.mode,
                direction=event.direction,
                settings=recording.settings,
                duration_ms=event.duration_ms,
                control=control,
                clock=clock,
                sleep=sleep,
                _timer=timer,
            )
        timer.wait_until(replay_started + recording.duration_ms / 1000.0)
        logging.info("restaurant camera replay completed path=%s", recording.path)
    except CameraReplayInterrupted:
        logging.info("restaurant camera replay interrupted path=%s", recording.path)
        raise
    except Exception:
        logging.exception("restaurant camera replay failed path=%s", recording.path)
        raise
    finally:
        try:
            controller.mouse_up("left", after_delay_seconds=0.0)
        except Exception:
            logging.exception("restaurant camera replay failed to release left mouse button")
        controller.release_all()
