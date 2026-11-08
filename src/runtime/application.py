from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.core.hotkey import RunControl
from src.core.input import InputController
from src.runtime.protocol import RunMode, RunProfile
from src.tasks.farm_vehicle import FarmVehicleTask
from src.utils.window import (
    WindowBounds,
    WindowNotFoundError,
    WindowSelector,
    activate_window,
    parse_window_selector,
    resolve_window,
)


def resolve_target_selector(window_config: dict[str, Any]) -> tuple[WindowSelector, str, int | None]:
    selector = parse_window_selector(window_config)
    resolved_window = resolve_window(selector)
    if resolved_window is None:
        raise WindowNotFoundError(f"Cannot find window matching selector: {selector}")
    return selector, resolved_window.title, resolved_window.hwnd


def apply_run_profile(config: dict[str, Any], profile: RunProfile) -> dict[str, Any]:
    profile.validate()
    result = copy.deepcopy(config)
    result.setdefault("runtime", {})["_dry_run"] = bool(profile.dry_run)
    if profile.overlay is not None:
        overlay_config = result.setdefault("overlay", {})
        overlay_config["enabled"] = True
        overlay_config["start_visible"] = bool(profile.overlay)

    detection = result.setdefault("detection", {})
    overrides = {
        "enabled": profile.detector_enabled,
        "model_path": profile.detector_model,
        "score_threshold": profile.detector_confidence,
        "nms_threshold": profile.detector_iou,
        "input_size": profile.detector_imgsz,
        "device": profile.detector_device,
    }
    for key, value in overrides.items():
        if value is not None:
            detection[key] = value

    marker = result.setdefault("farm_vehicle", {}).setdefault("marker_sequence", {})
    marker["wait_for_initial_f9"] = False
    if profile.mode is RunMode.AUTOMATIC:
        marker["max_steps"] = None if profile.continuous else profile.max_tasks
        marker["exit_on_no_match"] = not profile.continuous
        marker["exit_after_preview"] = not profile.continuous
        if profile.continuous:
            scheduler_config = result.get("scheduler", {})
            marker["no_match_rescan_seconds"] = max(
                0.1,
                float(scheduler_config.get("idle_scan_interval_seconds", 1.0)),
            )
    return result


@dataclass
class TaskSession:
    config: dict[str, Any]
    control: RunControl
    input_controller: InputController
    task: FarmVehicleTask
    target_selector: WindowSelector
    hotkeys_started: bool = False
    _closed: bool = False

    def activate_target_window(self, settle_seconds: float = 0.2) -> WindowBounds:
        bounds = activate_window(selector=self.target_selector, settle_seconds=settle_seconds)
        if bounds is None:
            raise WindowNotFoundError(f"Cannot activate target window: {self.target_selector}")
        return bounds

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.task.close()
        finally:
            try:
                self.input_controller.release_all()
            finally:
                self.control.stop()


def create_task_session(
    config: dict[str, Any],
    root: str | Path,
    *,
    event_sink: Any | None = None,
    start_hotkeys: bool = True,
    control: RunControl | None = None,
) -> TaskSession:
    control = control or RunControl()
    target_selector, target_title, target_hwnd = resolve_target_selector(config.get("window", {}))
    input_config = config.get("input", {})
    input_controller = InputController(
        click_delay_seconds=float(input_config.get("click_delay_seconds", 0.15)),
        click_hold_seconds=float(input_config.get("click_hold_seconds", 0.08)),
        move_duration_seconds=float(input_config.get("move_duration_seconds", 0.05)),
        click_backend=str(input_config.get("click_backend", "sendinput")),
        fail_safe=bool(input_config.get("fail_safe", True)),
        target_window_title=target_title,
        target_window_hwnd=target_hwnd,
        enabled=not bool(config.get("runtime", {}).get("_dry_run", False)),
    )
    task: FarmVehicleTask | None = None
    try:
        if start_hotkeys and not control.stopped:
            control.start()
        task = FarmVehicleTask(
            config=config,
            root=root,
            input_controller=input_controller,
            control=control,
            event_sink=event_sink,
        )
        return TaskSession(
            config,
            control,
            input_controller,
            task,
            target_selector,
            hotkeys_started=start_hotkeys,
        )
    except Exception:
        if task is not None:
            task.close()
        input_controller.release_all()
        control.stop()
        raise


def run_profile(session: TaskSession, profile: RunProfile) -> int:
    # UI profiles skip the legacy "wait for F9" startup path, so foregrounding
    # must be an explicit shared runtime step rather than an F9 side effect.
    control = getattr(session, "control", None)
    if control is not None and control.stopped:
        raise KeyboardInterrupt("worker action stopped before window activation")
    session.activate_target_window()
    if control is not None and control.stopped:
        raise KeyboardInterrupt("worker action stopped during window activation")
    if profile.mode is RunMode.REALTIME:
        return session.task.run_realtime_monitor(continuous=profile.continuous)
    if profile.mode is RunMode.INDEPENDENT:
        return session.task.run_independent(str(profile.task_id))
    return session.task.run()
