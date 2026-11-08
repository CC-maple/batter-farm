from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from src.core.camera_control import (
    CameraReplayControl,
    CameraReplayInterrupted,
    CameraSettings,
    execute_camera_turn,
    load_camera_recording,
    replay_camera_recording,
)
from src.core.capture import Screenshot, save_screenshot
from src.core.detector import FarmIconDetector, create_farm_icon_detector
from src.core.game_window import capture_game_client, timestamped_path
from src.core.hotkey import RunControl
from src.core.input import ClientCoordinateMapper, InputController, is_admin
from src.core.ocr import OcrEngine, crop_image, find_text_contains, normalize_ocr_region
from src.core.overlay import create_overlay_controller
from src.core.state_machine import StateResult, StateStatus, deadline_expired, monotonic_deadline
from src.core.targets import (
    LoadedTemplate,
    OcrTargetSpec,
    TargetMatch,
    TemplateSpec,
    filter_matches,
    load_templates,
    match_ocr_targets,
    match_targets,
    parse_ocr_target_specs,
    parse_template_specs,
    select_best_match,
)
from src.utils.window import get_client_bounds


RegionTuple = tuple[int, int, int, int]


class FarmVehicleInterrupted(Exception):
    """Raised when an explicit runtime command interrupts only the current farm action."""


class FarmVehicleActionFailed(Exception):
    """Raised when a nested replay check has a verified, user-visible failure reason."""


class FarmVehicleTask:
    def __init__(
        self,
        config: dict[str, Any],
        root: str | Path,
        input_controller: InputController,
        control: RunControl,
        event_sink: Any | None = None,
    ) -> None:
        self.config = config
        self.root = Path(root)
        self.input = input_controller
        self.control = control
        self.event_sink = event_sink
        self.window_config = config.get("window", {})
        self.capture_config = config.get("capture", {})
        self.runtime_config = config.get("runtime", {})
        self.vehicle_config = config.get("farm_vehicle", {})
        self.templates = load_templates(parse_template_specs(config, self.root))
        self.ocr_targets = parse_ocr_target_specs(config)
        self.last_capture: Screenshot | None = None
        self.ocr_engine: OcrEngine | None = None
        self.detector: FarmIconDetector | None = None
        self.processor_state = {"move_completed": False, "collect_completed": False}
        self.last_unselected_marker: TargetMatch | None = None
        self.overlay = create_overlay_controller(
            config,
            owner_hwnd=getattr(input_controller, "target_window_hwnd", None),
        )
        overlay_config = config.get("overlay", {})
        self._overlay_follow_interval_seconds = max(
            0.02,
            float(overlay_config.get("follow_interval_ms", overlay_config.get("pump_interval_ms", 50))) / 1000.0,
        )
        self._overlay_last_follow_at = 0.0
        self._overlay_follow_error_reported = False

    def close(self) -> None:
        self.overlay.close()

    def _emit_event(self, event_type: str, **payload: Any) -> None:
        sink = getattr(self, "event_sink", None)
        if sink is None:
            return
        try:
            sink.emit(event_type, **payload)
        except Exception:
            logging.exception("runtime event sink failed type=%s", event_type)

    def run(self) -> int:
        dry_run = bool(self.runtime_config.get("_dry_run", False))
        if not dry_run and bool(self.runtime_config.get("require_admin", True)) and not is_admin():
            logging.error("administrator privileges are required for game input")
            return 2

        self._emit_event("task_started", mode="automatic", task_id=None, dry_run=dry_run)
        self.overlay.update_status(run_state="running", current_step="marker_sequence", log="start marker_sequence")
        return self._state_result_to_exit_code(self._run_marker_sequence())

    def run_realtime_monitor(self, *, continuous: bool = True) -> int:
        """Recognize configured markers without dispatching game input."""
        marker_config = self.vehicle_config.get("marker_sequence", {})
        steps = marker_config.get("steps", []) if isinstance(marker_config, dict) else []
        if not isinstance(steps, list) or not steps:
            return self._state_result_to_exit_code(StateResult(StateStatus.FAILED, message="marker_sequence.steps is empty"))

        marker_names: list[str] = []
        for raw_step in steps:
            if (
                isinstance(raw_step, dict)
                and bool(raw_step.get("enabled", True))
                and not bool(raw_step.get("independent_only", False))
            ):
                marker_names.extend(self._marker_match_names(raw_step))
        marker_names = sorted(set(marker_names))
        recognition_source = str(marker_config.get("recognition_source", "template")).strip().lower()
        detector_names = self._marker_detector_names(marker_names, recognition_source)
        ocr_names = self._marker_ocr_names(marker_names, recognition_source)
        template_names = self._marker_template_names(marker_names, recognition_source, detector_names, ocr_names)
        templates = self._templates_by_name(template_names)
        if not templates and not detector_names and not ocr_names:
            return self._state_result_to_exit_code(StateResult(StateStatus.FAILED, message="realtime monitor has no recognition sources"))

        interval = max(0.05, float(marker_config.get("realtime_interval_seconds", 0.15)))
        confirm_frames = max(1, int(marker_config.get("realtime_confirm_frames", 2 if continuous else 1)))
        stable_name: str | None = None
        stable_count = 0
        scan_index = 1
        self._emit_event("task_started", mode="realtime", task_id=None, dry_run=True)
        self._emit_event("overlay_visibility", visible=self.overlay.show_requested)
        self.overlay.update_status(run_state="monitoring", current_step="实时触发", log="实时识别已启动，不执行输入")
        while not self._stopped():
            self._handle_overlay_toggle()
            self._wait_if_paused()
            matches = self._capture_and_match(
                f"realtime_scan_{scan_index:05d}",
                templates=templates,
                ignore_regions=self._marker_exclude_regions(marker_config),
                detector_names=detector_names,
                ocr_names=ocr_names,
                display_overlay=False,
                activate=False,
            )
            best = select_best_match(matches)
            current_name = best.name if best is not None else None
            if current_name == stable_name:
                stable_count += 1
            else:
                stable_name = current_name
                stable_count = 1
            if best is not None and stable_count >= confirm_frames:
                self.overlay.update_status(
                    run_state="monitoring",
                    current_step="实时触发",
                    last_marker=best.name,
                    last_score=best.score,
                    log=f"识别到 {best.name}，仅显示不执行",
                )
                if self.last_capture is not None:
                    self.overlay.show_matches(self.last_capture, matches, selected=best)
                self._emit_event(
                    "realtime_detection",
                    marker=best.name,
                    confidence=best.score,
                    stable_frames=stable_count,
                )
            elif best is None:
                self._emit_event("realtime_clear", scan=scan_index)
            if not continuous:
                break
            scan_index += 1
            self._sleep_checked(interval, allow_interrupt=False)
        status = StateStatus.STOPPED if self._stopped() else StateStatus.SUCCEEDED
        return self._state_result_to_exit_code(StateResult(status, message="realtime monitor stopped"))

    def run_independent(self, task_id: str) -> int:
        """Run one configured non-click task directly, without task-type recognition."""
        marker_config = self.vehicle_config.get("marker_sequence", {})
        marker_steps = marker_config.get("steps", []) if isinstance(marker_config, dict) else []
        independent_steps = self.vehicle_config.get("independent_tasks", [])
        raw_steps = (
            list(marker_steps if isinstance(marker_steps, list) else [])
            + list(independent_steps if isinstance(independent_steps, list) else [])
        )
        step = next(
            (
                raw
                for raw in raw_steps
                if isinstance(raw, dict) and str(raw.get("name", "")) == task_id and bool(raw.get("enabled", True))
            ),
            None,
        )
        if step is None:
            return self._state_result_to_exit_code(StateResult(StateStatus.FAILED, message=f"unknown independent task: {task_id}"))
        action = str(step.get("action", "replay")).strip().lower()
        if action in {"observe", "observe_only", "log_only", "click_match", "click_until_gone"}:
            return self._state_result_to_exit_code(
                StateResult(StateStatus.FAILED, message=f"task {task_id} requires a live match and cannot run independently")
            )
        dry_run = bool(self.runtime_config.get("_dry_run", False))
        if not dry_run and bool(self.runtime_config.get("require_admin", True)) and not is_admin():
            return self._state_result_to_exit_code(StateResult(StateStatus.FAILED, message="administrator privileges are required"))

        marker_names = self._marker_match_names(step)
        marker_name = marker_names[0] if marker_names else task_id
        synthetic_match = TargetMatch(
            spec=TemplateSpec(name=marker_name, path=self.root / "<independent>", required=False),
            score=1.0,
            top_left=(0, 0),
            size=(0, 0),
        )
        self._emit_event("task_started", mode="independent", task_id=task_id, dry_run=dry_run)
        self.overlay.update_status(run_state="running", current_step=task_id, log=f"start independent task {task_id}")
        if dry_run:
            result = StateResult(StateStatus.SUCCEEDED, message=f"independent task preview: {task_id}")
        else:
            self.control.set_busy(True)
            try:
                result = self._execute_marker_step(step, synthetic_match, marker_config)
            finally:
                self.control.set_busy(False)
        return self._state_result_to_exit_code(result)

    def run_recording_plan(self, steps: list[dict[str, Any]]) -> int:
        if bool(self.runtime_config.get("require_admin", True)) and not is_admin():
            logging.error("administrator privileges are required for recording replay")
            return 2
        config = {
            "reset_before_each": True,
            "reset_after_each": True,
            "preflight_capture": True,
            "capture_after_reset": True,
            "after_actions_wait_seconds": 1.0,
            "steps": steps,
        }
        self.overlay.update_status(run_state="running", current_step="recording_plan", log="start recording plan")
        return self._state_result_to_exit_code(self._run_recording_plan(config))

    def _state_result_to_exit_code(self, result: StateResult) -> int:
        display_messages = {
            StateStatus.SUCCEEDED: "任务已完成",
            StateStatus.STOPPED: "任务已停止",
            StateStatus.TIMEOUT: "任务已超时",
            StateStatus.FAILED: "任务失败，详细原因请查看日志",
        }
        self.overlay.update_status(
            run_state=result.status.value,
            log=display_messages.get(result.status, "任务状态已更新"),
        )
        self._emit_event("task_result", status=result.status.value, message=result.message)
        if result.status == StateStatus.SUCCEEDED:
            return 0
        if result.status == StateStatus.STOPPED:
            return 130
        if result.status == StateStatus.TIMEOUT:
            return 3
        return 2

    def _run_recording_plan(self, config: dict[str, Any]) -> StateResult:
        steps = config.get("steps", [])
        if not isinstance(steps, list) or not steps:
            return StateResult(StateStatus.FAILED, message="recording plan steps are empty")

        for index, raw_step in enumerate(steps, start=1):
            if self._stopped():
                return StateResult(StateStatus.STOPPED, message="recording plan stopped")
            if not isinstance(raw_step, dict):
                return StateResult(StateStatus.FAILED, message=f"recording plan step #{index} is not a mapping")
            if not bool(raw_step.get("enabled", True)):
                continue

            name = self._safe_label(str(raw_step.get("name", f"step_{index}")))
            logging.info("recording plan step start index=%s name=%s", index, name)
            if bool(raw_step.get("preflight_capture", config.get("preflight_capture", True))):
                self._capture_and_match(f"{name}_preflight")

            reset_before = raw_step.get("reset_before_start", config.get("reset_before_start", {}))
            self._run_reset(reset_before, default_enabled=bool(config.get("reset_before_each", True)))

            if bool(raw_step.get("capture_after_reset", config.get("capture_after_reset", True))):
                self._capture_and_match(f"{name}_after_reset")

            replay_config = raw_step.get("recording_replay", raw_step.get("replay", {}))
            if not isinstance(replay_config, dict) or not bool(replay_config.get("enabled", True)):
                return StateResult(StateStatus.FAILED, message=f"recording plan step {name} has no enabled replay")

            try:
                self._run_recording_replay(replay_config, label_prefix=name)
            except FarmVehicleActionFailed as exc:
                return StateResult(StateStatus.FAILED, message=str(exc))
            self._sleep_checked(float(raw_step.get("after_actions_wait_seconds", config.get("after_actions_wait_seconds", 1.0))))
            self._capture_and_match(f"{name}_after_actions")
            self._save_last(f"{name}_after_actions")

            reset_after = raw_step.get("reset_after_finish", config.get("reset_after_finish", {}))
            self._run_reset(reset_after, default_enabled=bool(config.get("reset_after_each", True)))
            logging.info("recording plan step completed index=%s name=%s", index, name)

        return StateResult(StateStatus.SUCCEEDED, message="recording plan completed")

    def _run_marker_sequence(self) -> StateResult:
        config = self.vehicle_config.get("marker_sequence", {})
        steps = config.get("steps", [])
        if not isinstance(steps, list) or not steps:
            return StateResult(StateStatus.FAILED, message="marker_sequence.steps is empty")

        startup_result = self._wait_for_initial_marker_start(config)
        if startup_result is not None:
            return startup_result

        raw_max_steps = config.get("max_steps")
        max_steps = None if raw_max_steps in (None, "", "null", 0) else max(1, int(raw_max_steps))
        completed: set[str] = set()
        run_counts: dict[str, int] = {}
        ran_steps = 0
        scan_index = 1
        while max_steps is None or ran_steps < max_steps:
            if self._stopped():
                return StateResult(StateStatus.STOPPED, message="marker sequence stopped")
            wait_seconds = float(config.get("scan_wait_seconds", 0.0))
            if wait_seconds > 0:
                self._sleep_checked(wait_seconds)

            selected_steps = self._select_marker_steps(
                steps,
                completed,
                run_counts,
                marker_config=config,
                label=f"marker_sequence_scan_{scan_index:02d}",
            )
            if not selected_steps:
                self._save_last("marker_sequence_no_match")
                unselected = getattr(self, "last_unselected_marker", None)
                if unselected is not None:
                    self.overlay.update_status(
                        last_marker=unselected.name,
                        last_score=unselected.score,
                        log=f"recognized {unselected.name}, but excluded; F9 rescan / F10 stop",
                    )
                else:
                    self.overlay.update_status(
                        last_marker="none",
                        last_score="-",
                        log="no marker; F9 rescan / F10 stop",
                    )
                if bool(config.get("exit_on_no_match", False)):
                    return StateResult(StateStatus.SUCCEEDED, message="no executable marker found")
                no_match_rescan_seconds = config.get("no_match_rescan_seconds")
                if no_match_rescan_seconds not in (None, "", False):
                    delay = max(0.1, float(no_match_rescan_seconds))
                    logging.info("marker sequence no match; automatic rescan scheduled after %.3fs", delay)
                    self.overlay.update_status(
                        last_action=f"{delay:.1f} 秒后自动重扫",
                        log=f"no marker; automatic rescan in {delay:.1f}s / F10 stop",
                    )
                    self._sleep_checked(delay)
                    scan_index += 1
                    continue
                idle_result = self._wait_for_marker_rescan()
                if idle_result == "rescan":
                    scan_index += 1
                    continue
                if idle_result == "exit":
                    return StateResult(StateStatus.SUCCEEDED, message="marker sequence exited by user")
                return StateResult(StateStatus.STOPPED, message="marker sequence stopped")

            if bool(getattr(self, "runtime_config", {}).get("_dry_run", False)):
                step, match = selected_steps[0]
                step_name = str(step.get("name", "step"))
                message = f"预览：将执行 {step_name}，未发送任何输入"
                logging.info(
                    "dry-run preview selected step=%s marker=%s score=%.4f",
                    step_name,
                    match.name,
                    match.score,
                )
                self.overlay.update_status(
                    run_state="preview",
                    current_step=step_name,
                    last_marker=match.name,
                    last_score=match.score,
                    active_keys="-",
                    mouse="-",
                    last_action="仅预览，不执行输入",
                    log=message,
                )
                self._save_last(f"{step_name}_dry_run_preview")
                if bool(config.get("exit_after_preview", False)):
                    return StateResult(StateStatus.SUCCEEDED, message=message)
                command = self._wait_for_marker_rescan(phase="dry_run_preview")
                if command == "rescan":
                    scan_index += 1
                    continue
                if command == "exit":
                    return StateResult(StateStatus.SUCCEEDED, message="dry-run preview exited by user")
                return StateResult(StateStatus.STOPPED, message="dry-run preview stopped")

            completed_this_attempt = False
            interrupted_this_attempt = False
            skipped_failure_this_attempt = False
            retry_after_error_this_attempt = False
            last_result: StateResult | None = None
            for step, match in selected_steps:
                step_name = str(step.get("name", "step"))
                logging.info("marker sequence selected step=%s marker=%s score=%.4f", step_name, match.name, match.score)
                self._emit_event(
                    "task_selected",
                    task_id=step_name,
                    marker=match.name,
                    confidence=match.score,
                )
                self.overlay.update_status(current_step=step_name, last_marker=match.name, last_score=match.score)

                self.control.set_busy(True)
                try:
                    result = self._execute_marker_step(step, match, config)
                finally:
                    self.control.set_busy(False)
                if result.status == StateStatus.SUCCEEDED and self.control.consume_interrupt_request():
                    result = StateResult(StateStatus.SUCCEEDED, message=f"marker step interrupted: {step_name}")
                last_result = result
                if result.status == StateStatus.SUCCEEDED:
                    completed.add(step_name)
                    run_counts[step_name] = run_counts.get(step_name, 0) + 1
                    if bool(step.get("count_toward_max_steps", True)):
                        ran_steps += 1
                    completed_this_attempt = True
                    if result.message.startswith("marker step interrupted"):
                        interrupted_this_attempt = True
                    break
                if bool(step.get("wait_for_rescan_on_failure", False)):
                    reason = result.message or f"{step_name} failed"
                    logging.error("marker step paused after failure name=%s reason=%s", step_name, reason)
                    self.overlay.update_status(
                        run_state="error",
                        current_step=step_name,
                        last_action="等待 F9 重试",
                        log=reason,
                    )
                    command = self._wait_for_marker_rescan(phase=f"{step_name}_error")
                    if command == "rescan":
                        self.overlay.update_status(
                            run_state="running",
                            last_action="F9 重新扫描",
                            log="F9 scan requested",
                        )
                        retry_after_error_this_attempt = True
                        break
                    if command == "exit":
                        return StateResult(StateStatus.SUCCEEDED, message="marker sequence exited by user")
                    return StateResult(StateStatus.STOPPED, message="marker sequence stopped")
                if self._can_skip_marker_failure(step, result):
                    skipped_failure_this_attempt = True
                    if bool(step.get("mark_completed_on_failure", False)):
                        completed.add(step_name)
                        logging.info("marker step disabled for this run after failure name=%s", step_name)
                    logging.info(
                        "marker step skipped after failure name=%s message=%s",
                        step_name,
                        result.message,
                    )
                    self.overlay.update_status(log=f"{step_name}: skipped")
                    continue
                return result

            if retry_after_error_this_attempt:
                scan_index += 1
                continue

            if not completed_this_attempt:
                if skipped_failure_this_attempt:
                    scan_index += 1
                    continue
                if ran_steps:
                    return StateResult(StateStatus.SUCCEEDED, message="marker sequence completed")
                message = last_result.message if last_result is not None else "marker_sequence found no executable marker"
                return StateResult(StateStatus.FAILED, message=message)

            if interrupted_this_attempt:
                self.overlay.update_status(log="current action interrupted; F9 rescan / F10 stop")
                idle_result = self._wait_for_marker_rescan()
                if idle_result == "rescan":
                    scan_index += 1
                    continue
                if idle_result == "exit":
                    return StateResult(StateStatus.SUCCEEDED, message="marker sequence exited by user")
                return StateResult(StateStatus.STOPPED, message="marker sequence stopped")

            scan_index += 1

        return StateResult(StateStatus.SUCCEEDED, message="marker sequence completed")

    def _wait_for_initial_marker_start(self, config: dict[str, Any]) -> StateResult | None:
        if not bool(config.get("wait_for_initial_f9", False)):
            return None

        self.overlay.update_status(
            run_state="waiting",
            current_step="等待 F9 开始",
            last_marker="-",
            last_score="-",
            active_keys="-",
            mouse="-",
            last_action="等待用户指令",
            log="ready; F9 scan / F10 stop",
        )
        self._capture(activate=True)
        self.overlay.update_status(log="game focused; F9 scan / F10 stop")
        logging.info("marker sequence startup ready: game focused, waiting for F9 scan or F10/ESC stop")
        command = self._wait_for_marker_rescan(phase="startup")
        if command == "rescan":
            self.overlay.update_status(
                run_state="running",
                current_step="marker_sequence_scan_01",
                last_action="F9 开始扫描",
                log="F9 scan requested",
            )
            return None
        if command == "exit":
            return StateResult(StateStatus.SUCCEEDED, message="marker sequence exited before first scan")
        return StateResult(StateStatus.STOPPED, message="marker sequence stopped before first scan")

    def _wait_for_marker_rescan(self, phase: str = "idle") -> str:
        logging.info("marker sequence %s: waiting for F9 rescan or F10/ESC stop", phase)
        while not self._stopped():
            self._handle_overlay_toggle()
            self._pump_overlays()
            self._wait_if_paused()
            if self.control.consume_rescan_request():
                logging.info("marker sequence %s: F9 rescan consumed", phase)
                return "rescan"
            if self.control.consume_exit_request():
                logging.info("marker sequence %s: explicit exit request consumed", phase)
                return "exit"
            time.sleep(0.1)
        return "stopped"

    def _select_marker_steps(
        self,
        steps: list[Any],
        completed: set[str],
        run_counts: dict[str, int],
        marker_config: dict[str, Any],
        label: str,
    ) -> list[tuple[dict[str, Any], TargetMatch]]:
        self.overlay.update_status(current_step=label)
        self.last_unselected_marker = None
        candidates: list[dict[str, Any]] = []
        marker_names: list[str] = []
        for index, raw_step in enumerate(steps, start=1):
            if not isinstance(raw_step, dict) or not bool(raw_step.get("enabled", True)):
                continue
            if bool(raw_step.get("independent_only", False)):
                continue
            name = str(raw_step.get("name", f"step_{index}"))
            if name in completed and not bool(raw_step.get("repeatable", False)):
                continue
            names = self._marker_match_names(raw_step)
            if not names:
                continue
            candidates.append(raw_step)
            marker_names.extend(names)

        marker_name_set = sorted(set(marker_names))
        recognition_source = str(marker_config.get("recognition_source", "template")).strip().lower()
        if recognition_source not in {"template", "detector", "hybrid", "ocr"}:
            raise ValueError(f"Unsupported marker_sequence.recognition_source: {recognition_source!r}")

        detector_names = self._marker_detector_names(marker_name_set, recognition_source)
        ocr_names = self._marker_ocr_names(marker_name_set, recognition_source)
        template_names = self._marker_template_names(marker_name_set, recognition_source, detector_names, ocr_names)
        templates = self._templates_by_name(template_names)
        if not templates and not detector_names and not ocr_names:
            logging.warning("marker sequence has no loaded recognition sources names=%s", marker_name_set)
            return []

        matches = self._capture_and_match(
            label,
            templates=templates,
            ignore_regions=self._marker_exclude_regions(marker_config),
            detector_names=detector_names,
            ocr_names=ocr_names,
        )
        ranked: list[tuple[int, int, float, str, dict[str, Any], TargetMatch]] = []
        for step in candidates:
            names = self._marker_match_names(step)
            best = select_best_match(self._filter_marker_step_matches(matches, names, step))
            if best is None:
                continue
            priority = int(step.get("priority", 0))
            step_name = str(step.get("name", "step"))
            action = str(step.get("action", "replay")).strip().lower()
            if action in {"observe", "observe_only", "log_only"}:
                message = str(
                    step.get(
                        "message",
                        f"{step_name}: {best.name} {best.score:.3f}",
                    )
                )
                self.overlay.update_status(last_marker=best.name, last_score=best.score, log=message)
                logging.info(
                    "marker step observe-only name=%s marker=%s score=%.4f message=%s",
                    step_name,
                    best.name,
                    best.score,
                    message,
                )
                continue
            run_count = run_counts.get(step_name, 0)
            rank_run_count = -1 if bool(step.get("priority_over_run_count", False)) else run_count
            ranked.append((rank_run_count, priority, best.score, step_name, step, best))
            logging.info(
                "marker step candidate name=%s marker=%s score=%.4f priority=%s run_count=%s",
                step_name,
                best.name,
                best.score,
                priority,
                run_count,
            )

        if not ranked:
            self.last_unselected_marker = select_best_match(matches)
            if self.last_unselected_marker is not None:
                logging.info(
                    "marker sequence recognized but no executable candidate marker=%s score=%.4f",
                    self.last_unselected_marker.name,
                    self.last_unselected_marker.score,
                )
            logging.info("marker sequence no candidate matched")
            return []

        ranked.sort(key=lambda item: (item[0], -item[1], -item[2], item[3]))
        return [(step, match) for _, _, _, _, step, match in ranked]

    def _marker_detector_names(self, marker_names: list[str], recognition_source: str) -> list[str]:
        if recognition_source in {"template", "ocr"}:
            return []
        detector_names = self._configured_detector_names()
        if recognition_source == "detector":
            return marker_names
        return [name for name in marker_names if name in detector_names]

    def _marker_template_names(
        self,
        marker_names: list[str],
        recognition_source: str,
        detector_names: list[str],
        ocr_names: list[str] | None = None,
    ) -> list[str]:
        if recognition_source in {"detector", "ocr"}:
            return []
        detector_name_set = set(detector_names)
        ocr_name_set = set(ocr_names or [])
        if recognition_source == "hybrid":
            return [name for name in marker_names if name not in detector_name_set and name not in ocr_name_set]
        return marker_names

    def _marker_ocr_names(self, marker_names: list[str], recognition_source: str) -> list[str]:
        if recognition_source == "ocr":
            return marker_names
        if recognition_source != "hybrid":
            return []
        configured_names = {spec.name for spec in self.ocr_targets}
        return [name for name in marker_names if name in configured_names]

    def _resolve_named_targets(
        self,
        names: list[str],
        recognition_source: str,
    ) -> tuple[list[LoadedTemplate], list[str], list[str]]:
        source = str(recognition_source).strip().lower()
        if source not in {"template", "detector", "hybrid", "ocr"}:
            raise ValueError(f"Unsupported recognition_source: {source!r}")
        detector_names = self._marker_detector_names(names, source)
        ocr_names = self._marker_ocr_names(names, source)
        template_names = self._marker_template_names(names, source, detector_names, ocr_names)
        templates = self._templates_by_name(template_names)
        selected_ocr_targets = self._ocr_targets_by_name(ocr_names)
        selected_ocr_names = {spec.name for spec in selected_ocr_targets}
        if source == "ocr" and set(ocr_names) != selected_ocr_names:
            missing = sorted(set(ocr_names) - selected_ocr_names)
            raise ValueError(f"OCR targets are not configured: {', '.join(missing)}")
        if not templates and not detector_names and not ocr_names:
            raise ValueError(f"no recognition targets are configured for names={names}")
        return templates, detector_names, ocr_names

    def _configured_detector_names(self) -> set[str]:
        detection_config = self.config.get("detection", {})
        if not isinstance(detection_config, dict):
            return set()

        names: set[str] = set()
        raw_classes = detection_config.get("classes", {})
        if isinstance(raw_classes, dict):
            names.update(str(name) for name in raw_classes if str(name).strip())

        raw_labels = detection_config.get("labels_path", "models/farm-icons/labels.txt")
        labels_path = Path(str(raw_labels))
        if not labels_path.is_absolute():
            labels_path = self.root / labels_path
        if labels_path.exists():
            names.update(line.strip() for line in labels_path.read_text(encoding="utf-8").splitlines() if line.strip())
        return names

    def _filter_marker_step_matches(
        self,
        matches: list[TargetMatch],
        names: list[str],
        step: dict[str, Any],
    ) -> list[TargetMatch]:
        filtered = filter_matches(matches, names=names)
        if not filtered:
            return []

        kept: list[TargetMatch] = []
        for match in filtered:
            allowed_regions = self._marker_step_allowed_regions(step, match.name)
            if allowed_regions and not self._match_center_in_regions(match, allowed_regions):
                logging.info(
                    "marker step ignored match outside allowed regions step=%s marker=%s center=%s regions=%s",
                    step.get("name", "step"),
                    match.name,
                    match.center,
                    allowed_regions,
                )
                continue

            excluded_regions = self._marker_step_excluded_regions(step, match.name)
            if excluded_regions and self._match_center_in_regions(match, excluded_regions):
                logging.info(
                    "marker step ignored match inside excluded regions step=%s marker=%s center=%s regions=%s",
                    step.get("name", "step"),
                    match.name,
                    match.center,
                    excluded_regions,
                )
                continue

            kept.append(match)
        return kept

    def _marker_step_allowed_regions(self, step: dict[str, Any], marker_name: str) -> tuple[RegionTuple, ...]:
        raw_regions = step.get("match_regions", step.get("match_roi"))
        return self._marker_step_regions(raw_regions, marker_name)

    def _marker_step_excluded_regions(self, step: dict[str, Any], marker_name: str) -> tuple[RegionTuple, ...]:
        return self._marker_step_regions(step.get("match_exclude_regions"), marker_name)

    def _marker_step_regions(self, raw_regions: Any, marker_name: str) -> tuple[RegionTuple, ...]:
        if raw_regions in (None, "", "null"):
            return ()

        if isinstance(raw_regions, dict):
            raw_regions = raw_regions.get(marker_name)
            if raw_regions in (None, "", "null"):
                return ()

        regions = _normalize_regions(raw_regions)
        if not regions or self.last_capture is None:
            return regions
        return self._scale_regions_for_capture(regions, self.last_capture)

    def _match_center_in_regions(self, match: TargetMatch, regions: tuple[RegionTuple, ...]) -> bool:
        x, y = match.center
        for left, top, width, height in regions:
            if left <= x <= left + width and top <= y <= top + height:
                return True
        return False

    def _can_skip_marker_failure(self, step: dict[str, Any], result: StateResult) -> bool:
        if bool(step.get("skip_on_failure", False)):
            return True
        action = str(step.get("action", "replay")).strip().lower()
        if action != "restaurant_service":
            return False
        if result.message != "restaurant state not detected":
            return False
        return bool(step.get("skip_on_precheck_failure", True))

    def _execute_marker_step(
        self,
        step: dict[str, Any],
        match: TargetMatch,
        marker_config: dict[str, Any],
    ) -> StateResult:
        name = self._safe_label(str(step.get("name", "marker_step")))
        action = str(step.get("action", "replay")).strip().lower()
        self.overlay.update_status(current_step=name, last_marker=match.name, last_score=match.score)
        logging.info("marker step start name=%s action=%s marker=%s", name, action, match.name)
        self._emit_event("route_started", task_id=name, action=action, marker=match.name)

        exit_status = StateStatus.FAILED
        interrupted = False
        try:
            reset_before = step.get("reset_before_start", marker_config.get("reset_before_start", {}))
            self._run_reset(reset_before, default_enabled=False)

            if action == "replay":
                replay_config = self._marker_replay_config(step)
                self._run_recording_replay(replay_config, label_prefix=name)
            elif action == "click_match":
                self._click_match(match)
                wait_seconds = float(step.get("after_click_wait_seconds", 0.0))
                if wait_seconds > 0:
                    self._sleep_checked(wait_seconds)
            elif action == "click_until_gone":
                result = self._click_match_until_gone(step, match, name)
                if result.status != StateStatus.SUCCEEDED:
                    return result
            elif action == "key_script":
                self._run_key_script(step.get("actions", []), label_prefix=name)
            elif action == "restaurant_service":
                result = self._run_restaurant_service(step, name)
                if result.status != StateStatus.SUCCEEDED:
                    return result
            elif action == "processor":
                result = self._run_processor_task(step, name)
                if result.status != StateStatus.SUCCEEDED:
                    return result
            else:
                return StateResult(StateStatus.FAILED, message=f"unsupported marker action: {action!r}")

            if bool(step.get("capture_after_action", marker_config.get("capture_after_action", True))):
                self._capture_and_match(f"{name}_after_action")
                self._save_last(f"{name}_after_action")

            post_result = self._run_post_action_match_click(step.get("post_action_match_click"), name)
            if post_result is not None and post_result.status != StateStatus.SUCCEEDED:
                return post_result

            exit_status = StateStatus.SUCCEEDED
            self._emit_event("route_completed", task_id=name, action=action, success=True)
            return StateResult(StateStatus.SUCCEEDED, message=f"marker step completed: {name}")
        except FarmVehicleActionFailed as exc:
            logging.error("marker step action failed name=%s reason=%s", name, exc)
            self._emit_event("route_completed", task_id=name, action=action, success=False, message=str(exc))
            return StateResult(StateStatus.FAILED, message=str(exc))
        except FarmVehicleInterrupted:
            interrupted = True
            logging.info("marker step interrupted by runtime command name=%s", name)
            self.overlay.update_status(log=f"{name}: interrupted")
            return StateResult(StateStatus.SUCCEEDED, message=f"marker step interrupted: {name}")
        finally:
            if exit_status == StateStatus.SUCCEEDED or interrupted:
                reset_after = step.get("reset_after_finish", marker_config.get("reset_after_finish", {}))
                self._run_reset(reset_after, default_enabled=False)
            else:
                reset_after = step.get("reset_after_failure", marker_config.get("reset_after_failure", {}))
                self._run_reset(reset_after, default_enabled=False)

    def _marker_match_names(self, step: dict[str, Any]) -> list[str]:
        raw_names = step.get("match_any", step.get("templates", []))
        if isinstance(raw_names, str):
            return [raw_names]
        return [str(name) for name in raw_names if str(name).strip()]

    def _click_match_until_gone(
        self,
        step: dict[str, Any],
        initial_match: TargetMatch,
        label: str,
    ) -> StateResult:
        names = self._marker_match_names(step)
        recognition_source = str(step.get("recognition_source", "template")).strip().lower()
        templates, detector_names, ocr_names = self._resolve_named_targets(names, recognition_source)

        max_clicks = max(1, int(step.get("max_clicks", 5)))
        wait_seconds = max(0.0, float(step.get("after_click_wait_seconds", 0.5)))
        current_match = initial_match
        for click_index in range(1, max_clicks + 1):
            logging.info(
                "click-until-gone attempt label=%s marker=%s click=%s/%s score=%.4f",
                label,
                current_match.name,
                click_index,
                max_clicks,
                current_match.score,
            )
            self.overlay.update_status(
                current_step=label,
                last_action=f"点击鱼卡 {click_index}/{max_clicks}",
                log=f"{label}: click {click_index}/{max_clicks}",
            )
            self._click_match(current_match)
            if wait_seconds > 0:
                self._sleep_checked(wait_seconds)

            scan_label = f"{label}_verify_{click_index:02d}"
            matches = self._capture_and_match(
                scan_label,
                templates=templates,
                detector_names=detector_names,
                ocr_names=ocr_names,
            )
            current_match = select_best_match(filter_matches(matches, names=names))
            if current_match is None:
                logging.info("click-until-gone completed label=%s clicks=%s", label, click_index)
                self.overlay.update_status(
                    run_state="running",
                    last_action=f"鱼卡已消失（{click_index} 次）",
                    log=f"{label}: cleared after {click_index} click(s)",
                )
                return StateResult(StateStatus.SUCCEEDED, message=f"{label} cleared after {click_index} click(s)")

        reason = f"鱼卡点击失败：连续点击 {max_clicks} 次后“恭喜获得鱼卡”文字仍存在"
        logging.error("%s", reason)
        self._save_last(f"{label}_still_present_after_{max_clicks}_clicks")
        self.overlay.update_status(
            run_state="error",
            current_step=label,
            last_action="鱼卡文字未消失",
            log=reason,
        )
        return StateResult(StateStatus.FAILED, message=reason)

    def _marker_replay_config(self, step: dict[str, Any]) -> dict[str, Any]:
        raw_replay = step.get("recording_replay", step.get("replay", {}))
        if not isinstance(raw_replay, dict) or not bool(raw_replay.get("enabled", True)):
            raise ValueError(f"marker step {step.get('name')!r} has no enabled replay")

        replay_config = dict(raw_replay)
        source_recordings = self.config.get("route", {}).get("source_recordings", {})
        source = str(replay_config.get("source", step.get("source", ""))).strip()
        path = replay_config.get("path")
        if path is None and source:
            path = source_recordings.get(source)
        if not path:
            raise ValueError(f"marker step {step.get('name')!r} must define recording_replay.path or source")

        replay_config["path"] = path
        replay_config["include_mouse"] = bool(replay_config.get("include_mouse", step.get("include_mouse", False)))
        replay_config["ignore_keys"] = list(replay_config.get("ignore_keys", step.get("ignore_keys", ["r"])))
        replay_config["speed"] = float(replay_config.get("speed", step.get("speed", 1.0)))
        replay_config["max_initial_wait_seconds"] = replay_config.get(
            "max_initial_wait_seconds",
            step.get("max_initial_wait_seconds", 1.5),
        )
        return replay_config

    def _run_post_action_match_click(self, raw_config: Any, label_prefix: str) -> StateResult | None:
        if not isinstance(raw_config, dict) or not bool(raw_config.get("enabled", False)):
            return None

        names = [str(name) for name in raw_config.get("match_any", []) if str(name).strip()]
        if not names:
            logging.warning("post-action match click has no match_any names label=%s", label_prefix)
            return None

        recognition_source = str(raw_config.get("recognition_source", "template")).strip().lower()
        templates, detector_names, ocr_names = self._resolve_named_targets(names, recognition_source)

        label = self._safe_label(str(raw_config.get("label", "post_action_match")))
        repeat_until_no_match = bool(raw_config.get("repeat_until_no_match", False))
        interval_seconds = float(raw_config.get("interval_seconds", raw_config.get("after_click_wait_seconds", 0.0)))
        max_checks = max(1, int(raw_config.get("max_checks", 1)))
        clicked_count = 0

        for attempt in range(1, max_checks + 1):
            scan_label = f"{label_prefix}_{label}_scan_{attempt:02d}"
            matches = self._capture_and_match(
                scan_label,
                templates=templates,
                detector_names=detector_names,
                ocr_names=ocr_names,
            )
            best = select_best_match(filter_matches(matches, names=names))
            if best is None:
                logging.info(
                    "post-action match click completed label=%s clicked=%s reason=no_match attempt=%s",
                    label_prefix,
                    clicked_count,
                    attempt,
                )
                self.overlay.update_status(log=f"{scan_label}: no match")
                return None

            logging.info(
                "post-action match click matched label=%s marker=%s score=%.4f attempt=%s",
                label_prefix,
                best.name,
                best.score,
                attempt,
            )
            self._click_match(best)
            clicked_count += 1

            if not repeat_until_no_match:
                break
            if interval_seconds > 0:
                self._sleep_checked(interval_seconds)

        if repeat_until_no_match and clicked_count >= max_checks:
            logging.info(
                "post-action match click stopped label=%s clicked=%s reason=max_checks",
                label_prefix,
                clicked_count,
            )

        if bool(raw_config.get("verify_after_click", True)) and clicked_count:
            verify_label = f"{label_prefix}_{label}_verify"
            verify_matches = self._capture_and_match(
                verify_label,
                templates=templates,
                detector_names=detector_names,
                ocr_names=ocr_names,
            )
            verify_best = select_best_match(filter_matches(verify_matches, names=names))
            logging.info(
                "post-action match click verify label=%s marker_present=%s",
                label_prefix,
                verify_best is not None,
            )
            if bool(raw_config.get("save_after_click", False)):
                self._save_last(verify_label)
        elif bool(raw_config.get("save_after_click", False)) and clicked_count:
            self._capture_and_match(
                f"{label_prefix}_{label}_after_click",
                templates=templates,
                detector_names=detector_names,
                ocr_names=ocr_names,
            )
            self._save_last(f"{label_prefix}_{label}_after_click")

        return StateResult(StateStatus.SUCCEEDED, message=f"post-action match click completed: {label_prefix}")

    def _run_processor_task(self, step: dict[str, Any], label: str) -> StateResult:
        config = self.vehicle_config.get("processor", {})
        if not isinstance(config, dict):
            config = {}
        step_config = step.get("processor", {})
        if isinstance(step_config, dict):
            config = _deep_merge_dicts(config, step_config)
        if not bool(config.get("enabled", False)):
            return StateResult(StateStatus.FAILED, message="processor task is disabled")

        self.processor_state = {"move_completed": False, "collect_completed": False}
        completion_config = config.get("completion_verification", {})
        completion_enabled = isinstance(completion_config, dict) and bool(completion_config.get("enabled", False))
        max_task_attempts = max(1, int(config.get("task_max_attempts", 2))) if completion_enabled else 1

        for task_attempt in range(1, max_task_attempts + 1):
            self.processor_state = {"move_completed": False, "collect_completed": False}
            logging.info(
                "processor full attempt=%s/%s start state=%s",
                task_attempt,
                max_task_attempts,
                self.processor_state,
            )
            label_prefix = (
                f"{label}_attempt_{task_attempt:02d}"
                if completion_enabled
                else label
            )
            result, residual_marker = self._run_processor_attempt(
                config,
                label_prefix=label_prefix,
                completion_config=completion_config if completion_enabled else None,
            )
            if result.status == StateStatus.SUCCEEDED:
                return result
            if not residual_marker:
                return result
            if task_attempt < max_task_attempts:
                message = f"加工台仍有待收获标识，正在完整补跑 {task_attempt + 1}/{max_task_attempts}"
                logging.warning(message)
                self.overlay.update_status(current_step=label, log=message)
                retry_wait = max(0.0, float(config.get("full_retry_wait_seconds", 0.5)))
                if retry_wait > 0:
                    self._sleep_checked(retry_wait)
                continue

            self.processor_state = {"move_completed": False, "collect_completed": False}
            message = "加工台收获标识在两次完整流程后仍存在，请人工检查"
            logging.error(message)
            self.overlay.update_status(run_state="error", current_step=label, log=message)
            return StateResult(StateStatus.FAILED, message=message)

        return StateResult(StateStatus.FAILED, message="processor task failed")

    def _run_processor_attempt(
        self,
        config: dict[str, Any],
        *,
        label_prefix: str,
        completion_config: dict[str, Any] | None,
    ) -> tuple[StateResult, bool]:
        """Run one complete processor move/collect flow.

        The boolean result is true only when the collect route has returned to
        the farm but the processor marker is still present.  That state is safe
        for the caller to retry from the beginning; other failures may leave the
        game in an unknown position and must fail closed.
        """

        move_config = config.get("move_recording", {})
        move_replay = self._processor_replay_config(move_config, "processor_move")
        try:
            self._run_recording_replay(move_replay, label_prefix=f"{label_prefix}_move")
        finally:
            self.input.release_all()

        wait_seconds = max(0.0, float(config.get("after_move_wait_seconds", 0.5)))
        if wait_seconds > 0:
            self._sleep_checked(wait_seconds)
        move_ok = self._wait_for_processor_state(
            config.get("move_verification", {}),
            "after_processor_move",
        )
        self._save_last("after_processor_move")
        if not move_ok:
            logging.warning("processor move verification failed state=%s", self.processor_state)
            return StateResult(StateStatus.FAILED, message="processor move verification failed"), False

        self.processor_state["move_completed"] = True
        logging.info("processor move completed state=%s", self.processor_state)

        collect_config = config.get("collect_recording", {})
        collect_replay = self._processor_replay_config(collect_config, "processor_collect")
        collect_resets_position = self._recording_contains_key_down(collect_replay, "r")
        max_collect_attempts = max(1, int(config.get("collect_max_attempts", 2)))
        for collect_attempt in range(1, max_collect_attempts + 1):
            if not self.processor_state["move_completed"]:
                logging.warning("processor collect blocked because move_completed is false")
                break
            logging.info(
                "processor collect attempt=%s/%s state=%s",
                collect_attempt,
                max_collect_attempts,
                self.processor_state,
            )
            try:
                self._run_recording_replay(
                    collect_replay,
                    label_prefix=f"{label_prefix}_collect_{collect_attempt:02d}",
                )
            finally:
                self.input.release_all()

            collect_wait = max(0.0, float(config.get("after_collect_wait_seconds", 0.5)))
            if collect_wait > 0:
                self._sleep_checked(collect_wait)
            collect_ok = self._wait_for_processor_state(
                config.get("collect_verification", {}),
                "after_processor_collect",
            )
            self._save_last("after_processor_collect")
            if collect_ok:
                if completion_config is not None:
                    completion_ok = self._wait_for_processor_state(
                        completion_config,
                        "after_processor_completion",
                    )
                    self._save_last("after_processor_completion")
                    if not completion_ok:
                        self.processor_state = {"move_completed": False, "collect_completed": False}
                        logging.warning(
                            "processor residual marker detected after confirmed farm return state=%s",
                            self.processor_state,
                        )
                        return (
                            StateResult(
                                StateStatus.FAILED,
                                message="processor harvest marker remains after collect",
                            ),
                            True,
                        )

                self.processor_state["collect_completed"] = True
                if collect_resets_position:
                    self.processor_state["move_completed"] = False
                logging.info("processor collect completed state=%s", self.processor_state)
                return StateResult(StateStatus.SUCCEEDED, message="processor move and collect completed"), False

            logging.warning(
                "processor collect verification failed attempt=%s state=%s",
                collect_attempt,
                self.processor_state,
            )
            if collect_resets_position:
                self.processor_state["move_completed"] = False
                logging.warning(
                    "processor collect recording contains R reset; move_completed cleared and collect retry stopped"
                )
                break

        return StateResult(StateStatus.FAILED, message="processor collect verification failed"), False

    def _wait_for_processor_state(self, raw_config: Any, label: str) -> bool:
        config = raw_config if isinstance(raw_config, dict) else {}
        max_wait_seconds = max(0.0, float(config.get("max_wait_seconds", 0.0)))
        interval_seconds = max(0.1, float(config.get("interval_seconds", 0.5)))
        required_frames = max(1, int(config.get("consecutive_frames", 1)))
        deadline = monotonic_deadline(max_wait_seconds)
        attempt = 1
        confirmed_frames = 0

        while True:
            capture = self._capture(activate=False)
            if self._verify_processor_state(config, capture, label):
                confirmed_frames += 1
                if confirmed_frames >= required_frames:
                    logging.info(
                        "processor verification completed label=%s attempt=%s consecutive_frames=%s/%s max_wait_seconds=%.3f",
                        label,
                        attempt,
                        confirmed_frames,
                        required_frames,
                        max_wait_seconds,
                    )
                    return True
            else:
                confirmed_frames = 0
            if deadline_expired(deadline):
                logging.warning(
                    "processor verification timed out label=%s attempts=%s consecutive_frames=%s/%s max_wait_seconds=%.3f",
                    label,
                    attempt,
                    confirmed_frames,
                    required_frames,
                    max_wait_seconds,
                )
                return False

            logging.info(
                "processor verification retry label=%s attempt=%s consecutive_frames=%s/%s interval_seconds=%.3f",
                label,
                attempt,
                confirmed_frames,
                required_frames,
                interval_seconds,
            )
            self._sleep_checked(interval_seconds)
            attempt += 1

    def _processor_replay_config(self, raw_config: Any, label: str) -> dict[str, Any]:
        if not isinstance(raw_config, dict) or not bool(raw_config.get("enabled", True)):
            raise ValueError(f"{label} recording is not enabled")
        replay_config = dict(raw_config)
        path = replay_config.get("path")
        source = str(replay_config.get("source", "")).strip()
        if path is None and source:
            path = self.config.get("route", {}).get("source_recordings", {}).get(source)
        if not path:
            raise ValueError(f"{label} recording path is required")
        replay_config["path"] = path
        replay_config["include_mouse"] = bool(replay_config.get("include_mouse", True))
        replay_config["ignore_keys"] = list(replay_config.get("ignore_keys", []))
        replay_config["speed"] = float(replay_config.get("speed", 1.0))
        replay_config["max_initial_wait_seconds"] = replay_config.get("max_initial_wait_seconds", 1.5)
        return replay_config

    def _verify_processor_state(self, raw_config: Any, capture: Screenshot, label: str) -> bool:
        if not isinstance(raw_config, dict) or not bool(raw_config.get("enabled", False)):
            logging.error("processor verification is disabled or pending label=%s config=%s", label, raw_config)
            return False

        recognition_source = str(raw_config.get("recognition_source", "template")).strip().lower()
        if recognition_source not in {"template", "detector", "hybrid", "ocr"}:
            raise ValueError(f"Unsupported processor verification recognition_source: {recognition_source!r}")
        validation_status = str(raw_config.get("validation_status", "configured"))
        expected = str(raw_config.get("expected", "present")).strip().lower()
        if expected not in {"present", "absent"}:
            raise ValueError(f"processor verification expected must be present or absent, got {expected!r}")

        if recognition_source == "ocr":
            matched = self._verify_scene_text(raw_config, capture, label)
            ok = matched if expected == "present" else not matched
            logging.info(
                "processor verification label=%s source=ocr validation_status=%s expected=%s matched=%s result=%s",
                label,
                validation_status,
                expected,
                matched,
                ok,
            )
            return ok

        names = self._marker_match_names(raw_config)
        detector_names = self._marker_detector_names(names, recognition_source)
        template_names = self._marker_template_names(names, recognition_source, detector_names)
        templates = self._templates_by_name(template_names)
        if template_names and not templates:
            logging.error(
                "processor verification has no loaded templates label=%s names=%s",
                label,
                template_names,
            )
            return False
        if detector_names and self._get_detector() is None:
            logging.error(
                "processor verification detector is unavailable label=%s names=%s",
                label,
                detector_names,
            )
            return False
        if not templates and not detector_names:
            logging.error("processor verification has no recognition targets label=%s names=%s", label, names)
            return False

        matches: list[TargetMatch] = []
        if templates:
            matches.extend(match_targets(capture.image, templates))
        if detector_names:
            matches.extend(self._detect_targets(capture, detector_names, ()))
        best = select_best_match(self._filter_marker_step_matches(matches, names, raw_config))
        ok = (best is not None) if expected == "present" else (best is None)
        logging.info(
            "processor verification label=%s source=%s validation_status=%s expected=%s matched=%s best=%s score=%s result=%s",
            label,
            recognition_source,
            validation_status,
            expected,
            best is not None,
            best.name if best is not None else None,
            best.score if best is not None else None,
            ok,
        )
        return ok

    def _recording_contains_key_down(self, replay_config: dict[str, Any], key: str) -> bool:
        path = Path(str(replay_config.get("path", "")))
        if not path.is_absolute():
            path = self.root / path
        data = json.loads(path.read_text(encoding="utf-8"))
        events = data.get("events", [])
        normalized_key = str(key).strip().lower()
        return any(
            str(event.get("type", "")) == "key_down"
            and str(event.get("key", "")).strip().lower() == normalized_key
            for event in events
            if isinstance(event, dict)
        )

    def _run_restaurant_service(self, step: dict[str, Any], label: str) -> StateResult:
        config = self.vehicle_config.get("restaurant_service", {})
        if not isinstance(config, dict):
            config = {}
        step_config = step.get("restaurant_service", {})
        if isinstance(step_config, dict):
            config = _deep_merge_dicts(config, step_config)

        attempt_state = {"camera_replay_executed": False}
        farm_capture = self._capture(activate=bool(self.capture_config.get("activate_window", True)))
        if not self._verify_scene_text(config.get("farm_scene_check", {}), farm_capture, f"{label}_farm_scene"):
            self._save_last(f"{label}_farm_scene_failed")
            logging.warning("restaurant camera replay blocked: current scene is not confirmed as farm")
            return StateResult(StateStatus.FAILED, message="restaurant farm scene not detected")

        camera_config = config.get("farm_camera_replay", {})
        if isinstance(camera_config, dict) and bool(camera_config.get("enabled", False)):
            try:
                self._run_restaurant_camera_replay_once(camera_config, label, attempt_state)
            finally:
                self.input.release_all()

        precheck_config = config.get("precheck", {})
        if isinstance(precheck_config, dict) and bool(precheck_config.get("enabled", False)):
            replay_config = self._restaurant_replay_config(precheck_config)
            self._run_recording_replay(replay_config, label_prefix=f"{label}_precheck_route")
            wait_seconds = float(precheck_config.get("after_wait_seconds", 0.2))
            if wait_seconds > 0:
                self._sleep_checked(wait_seconds)

            precheck_capture = self._capture(activate=False)
            if not self._verify_restaurant_marker(
                precheck_config.get("waiting_marker", {}),
                precheck_capture,
                f"{label}_precheck_marker",
            ):
                self._save_last(f"{label}_precheck_marker_absent")
                self._run_key_script(
                    precheck_config.get("after_no_marker_actions", []),
                    label_prefix=f"{label}_precheck_no_marker",
                )
                return StateResult(StateStatus.SUCCEEDED, message="restaurant precheck marker not detected")
            self._save_last(f"{label}_precheck_marker_detected")

        enter_config = config.get("enter_restaurant", {})
        if isinstance(enter_config, dict) and bool(enter_config.get("enabled", False)):
            self._run_key_script(enter_config.get("actions", []), label_prefix=f"{label}_enter_restaurant")
            wait_seconds = float(enter_config.get("after_wait_seconds", 1.0))
            if wait_seconds > 0:
                self._sleep_checked(wait_seconds)

        initial_capture = self._capture(activate=bool(self.capture_config.get("activate_window", True)))
        if not self._verify_restaurant_state(config, initial_capture, f"{label}_precheck"):
            self._save_last(f"{label}_precheck_failed")
            return StateResult(StateStatus.FAILED, message="restaurant state not detected")

        walk_config = config.get("walk_to_menu", {})
        if isinstance(walk_config, dict) and bool(walk_config.get("enabled", False)):
            replay_config = self._restaurant_replay_config(walk_config)
            self._run_recording_replay(replay_config, label_prefix=f"{label}_walk_to_menu")
            wait_seconds = float(walk_config.get("after_wait_seconds", 0.2))
            if wait_seconds > 0:
                self._sleep_checked(wait_seconds)

        menu_config = config.get("menu", {})
        if isinstance(menu_config, dict) and bool(menu_config.get("right_click", {}).get("enabled", True)):
            right_click = menu_config.get("right_click", {})
            point = _normalize_point(right_click.get("point", [799, 619]))
            self._click_client_point(point, button=str(right_click.get("button", "right")), label=f"{label}_menu")
            self._sleep_checked(float(right_click.get("after_wait_seconds", menu_config.get("after_open_wait_seconds", 0.8))))

        menu_capture = self._capture(activate=False)
        menu_state = self._restaurant_menu_state(config, menu_capture, f"{label}_menu")
        if not menu_state:
            self._save_last(f"{label}_menu_state_failed")
            return StateResult(StateStatus.FAILED, message="restaurant menu state not detected")

        clicked = self._click_restaurant_menu_button(config, menu_capture, label)
        if not clicked:
            self._save_last(f"{label}_button_not_found")
            return StateResult(StateStatus.FAILED, message="restaurant menu button not detected")

        after_click_wait = float(config.get("after_button_click_wait_seconds", 0.8))
        if after_click_wait > 0:
            self._sleep_checked(after_click_wait)

        back_config = config.get("return_to_farm", {})
        if isinstance(back_config, dict) and bool(back_config.get("enabled", True)):
            self._run_key_script(back_config.get("actions", []), label_prefix=f"{label}_return_to_farm")
            wait_seconds = float(back_config.get("after_wait_seconds", 1.0))
            if wait_seconds > 0:
                self._sleep_checked(wait_seconds)
            capture = self._capture(activate=False)
            if not self._verify_scene_text(back_config.get("verify_scene", {}), capture, f"{label}_farm_verify"):
                self._save_last(f"{label}_farm_verify_failed")
                return StateResult(StateStatus.FAILED, message="farm scene text not detected after restaurant")

        self._save_last(f"{label}_completed")
        return StateResult(StateStatus.SUCCEEDED, message="restaurant service completed")

    def _run_restaurant_camera_replay_once(
        self,
        raw_config: dict[str, Any],
        label: str,
        attempt_state: dict[str, bool],
    ) -> None:
        if not bool(raw_config.get("run_once_per_attempt", True)):
            raise ValueError("restaurant farm_camera_replay.run_once_per_attempt must remain true")
        if attempt_state.get("camera_replay_executed", False):
            logging.info("restaurant camera replay skipped: already executed in this recognition attempt")
            return
        attempt_state["camera_replay_executed"] = True

        path_value = raw_config.get("path")
        if not path_value:
            raise ValueError("restaurant farm_camera_replay.path is required")
        path = Path(str(path_value))
        if not path.is_absolute():
            path = self.root / path
        require_mode = str(raw_config.get("require_mode", "visible")).strip().lower()
        recording = load_camera_recording(path, require_mode=require_mode)
        if self.last_capture is None:
            self._capture(activate=False)
        if self.last_capture is None:
            raise RuntimeError("restaurant camera replay has no game-client capture origin")

        def stop_requested() -> bool:
            if self._stopped():
                return True
            return self.control.consume_interrupt_request()

        try:
            replay_camera_recording(
                recording=recording,
                controller=self.input,
                origin=self.last_capture.origin,
                control=CameraReplayControl(
                    stop_requested=stop_requested,
                    paused=lambda: self.control.paused,
                ),
                require_mode=require_mode,
            )
        except CameraReplayInterrupted as exc:
            self.input.release_all()
            if self._stopped():
                raise KeyboardInterrupt("restaurant camera replay stopped") from exc
            raise FarmVehicleInterrupted() from exc

        settle_seconds = max(0.0, float(raw_config.get("settle_seconds", 0.5)))
        if settle_seconds > 0:
            self._sleep_checked(settle_seconds)
        self._capture(activate=False)
        self._save_last(f"{label}_after_restaurant_camera_replay")

    def _verify_restaurant_state(self, config: dict[str, Any], capture: Screenshot, label: str) -> bool:
        checks = config.get("entry_checks", {})
        if not isinstance(checks, dict):
            checks = {}

        scene_ok = self._verify_scene_text(checks.get("scene", {}), capture, f"{label}_scene")
        status_ok = self._verify_scene_text(checks.get("status", {}), capture, f"{label}_status")
        marker_ok = True
        marker_config = checks.get("waiting_marker", {})
        if isinstance(marker_config, dict) and bool(marker_config.get("enabled", True)):
            marker_ok = self._verify_restaurant_marker(marker_config, capture, f"{label}_waiting_marker")

        return scene_ok and status_ok and marker_ok

    def _verify_restaurant_marker(self, config: Any, capture: Screenshot, label: str) -> bool:
        if not isinstance(config, dict) or not bool(config.get("enabled", True)):
            return True
        names = config.get("templates", ["restaurant_reception"])
        if isinstance(names, str):
            names = [names]
        templates = self._templates_by_name([str(name) for name in names])
        if not templates:
            logging.warning("restaurant marker check has no templates label=%s names=%s", label, names)
            return False
        matches = match_targets(capture.image, templates)
        best = select_best_match(matches)
        ok = best is not None
        if best is not None:
            logging.info(
                "restaurant marker matched label=%s name=%s score=%.4f center=%s",
                label,
                best.name,
                best.score,
                best.center,
            )
            self.overlay.update_status(last_marker=best.name, last_score=best.score)
        else:
            logging.info("restaurant marker not found label=%s names=%s", label, names)
        return ok

    def _restaurant_menu_state(self, config: dict[str, Any], capture: Screenshot, label: str) -> bool:
        menu_config = config.get("menu", {})
        if not isinstance(menu_config, dict):
            return True
        state_config = menu_config.get("state_text", {})
        return self._verify_scene_text(state_config, capture, f"{label}_state")

    def _click_restaurant_menu_button(self, config: dict[str, Any], capture: Screenshot, label: str) -> bool:
        buttons = config.get("buttons", [])
        if not isinstance(buttons, list):
            return False

        for raw_button in buttons:
            if not isinstance(raw_button, dict) or not bool(raw_button.get("enabled", True)):
                continue
            if self._verify_scene_text(raw_button.get("text", {}), capture, f"{label}_{raw_button.get('name', 'button')}"):
                point = _normalize_point(raw_button.get("click_point"))
                self._click_client_point(point, button=str(raw_button.get("button", "left")), label=f"{label}_{raw_button.get('name', 'button')}")
                return True
        return False

    def _verify_scene_text(self, config: Any, capture: Screenshot, label: str) -> bool:
        if not isinstance(config, dict) or not bool(config.get("enabled", True)):
            return True

        roi = normalize_ocr_region(config.get("roi"))
        scaled_roi = self._scale_regions_for_capture([roi], capture)[0]
        roi_image = crop_image(capture.image, scaled_roi)
        if bool(config.get("save_roi", False)):
            directory = self.root / "logs" / "farm_vehicle"
            save_screenshot(roi_image, timestamped_path(directory, f"{label}_ocr_roi"))

        lines = self._get_ocr_engine().recognize(roi_image)
        for line in lines:
            logging.info("scene ocr label=%s text=%r score=%s box=%s", label, line.text, line.score, line.box)

        targets = config.get("contains", config.get("target_contains", []))
        if isinstance(targets, str):
            targets = [targets]
        targets = [str(target) for target in targets if str(target).strip()]
        if not targets:
            return True

        for target in targets:
            if find_text_contains(lines, target) is not None:
                logging.info("restaurant text matched label=%s target=%r", label, target)
                return True

        logging.warning("scene text not found label=%s targets=%s roi=%s", label, targets, scaled_roi)
        return False

    def _restaurant_replay_config(self, replay_source: dict[str, Any]) -> dict[str, Any]:
        replay_config = dict(replay_source.get("recording_replay", replay_source))
        source_recordings = self.config.get("route", {}).get("source_recordings", {})
        source = str(replay_config.get("source", "")).strip()
        path = replay_config.get("path")
        if path is None and source:
            path = source_recordings.get(source)
        if not path:
            raise ValueError("restaurant replay must define recording_replay.path or source")
        replay_config["path"] = path
        replay_config["include_mouse"] = bool(replay_config.get("include_mouse", False))
        replay_config["ignore_keys"] = list(replay_config.get("ignore_keys", ["r"]))
        replay_config["speed"] = float(replay_config.get("speed", 1.0))
        replay_config["max_initial_wait_seconds"] = replay_config.get("max_initial_wait_seconds", 1.5)
        return replay_config

    def _click_client_point(self, point: tuple[int, int], button: str, label: str) -> None:
        if self.last_capture is None:
            raise RuntimeError("cannot click a client point before capturing the game client")
        client_x, client_y = self._scale_point_for_capture(point, self.last_capture)
        origin_x, origin_y = self.last_capture.origin
        screen_x = origin_x + client_x
        screen_y = origin_y + client_y
        normalized_button = str(button).strip().lower()
        self.overlay.update_status(mouse=f"{normalized_button}@{client_x},{client_y}", last_action=f"click {label}")
        if normalized_button == "left":
            self.input.safe_click(screen_x, screen_y)
        else:
            self.input.move_to(screen_x, screen_y)
            self.input.click_button(normalized_button)
        self.overlay.update_status(mouse="-", last_action=f"clicked {label}")

    def _scale_point_for_capture(self, point: tuple[int, int], capture: Screenshot) -> tuple[int, int]:
        overlay_config = self.config.get("overlay", {})
        if not bool(overlay_config.get("scale_with_client", True)):
            return point
        image_height, image_width = capture.image.shape[:2]
        base_size = overlay_config.get("base_client_size", [1600, 900])
        base_width, base_height = _normalize_size(base_size)
        return (
            int(round(point[0] * image_width / max(base_width, 1))),
            int(round(point[1] * image_height / max(base_height, 1))),
        )

    def _get_ocr_engine(self) -> OcrEngine:
        if self.ocr_engine is None:
            recognition_config = self.config.get("recognition", {})
            self.ocr_engine = OcrEngine(str(recognition_config.get("ocr_engine", "rapidocr")))
        return self.ocr_engine

    def _marker_exclude_regions(self, marker_config: dict[str, Any]) -> list[RegionTuple]:
        regions = [_normalize_region(region) for region in marker_config.get("exclude_regions", [])]
        overlay_config = self.config.get("overlay", {})
        if (
            bool(marker_config.get("exclude_overlay_region", True))
            and bool(overlay_config.get("enabled", False))
            and overlay_config.get("region") is not None
        ):
            regions.append(_normalize_region(overlay_config.get("region")))
        return regions

    def _scale_regions_for_capture(self, regions: list[RegionTuple] | tuple[RegionTuple, ...], capture: Screenshot) -> tuple[RegionTuple, ...]:
        if not regions:
            return ()

        image_height, image_width = capture.image.shape[:2]
        overlay_config = self.config.get("overlay", {})
        base_size = overlay_config.get("base_client_size", [1600, 900])
        base_width, base_height = _normalize_size(base_size)
        if not bool(overlay_config.get("scale_with_client", True)):
            return tuple(regions)

        scale_x = image_width / max(base_width, 1)
        scale_y = image_height / max(base_height, 1)
        scaled: list[RegionTuple] = []
        for left, top, width, height in regions:
            scaled.append(
                (
                    max(0, int(round(left * scale_x))),
                    max(0, int(round(top * scale_y))),
                    max(1, int(round(width * scale_x))),
                    max(1, int(round(height * scale_y))),
                )
            )
        return tuple(scaled)

    def _run_reset(self, reset_config: Any, default_enabled: bool = False) -> None:
        if self._stopped():
            return
        if not isinstance(reset_config, dict):
            reset_config = {}
        if not bool(reset_config.get("enabled", default_enabled)):
            return
        key = str(reset_config.get("key", "r"))
        self.overlay.update_status(active_keys=key, last_action=f"press {key}", log=f"reset {key}")
        self.input.press(
            key,
            hold_seconds=float(reset_config.get("hold_seconds", 0.08)),
        )
        self.overlay.update_status(active_keys="-", last_action=f"release {key}")
        wait_seconds = float(reset_config.get("wait_seconds", 0.5))
        if wait_seconds > 0:
            self._sleep_checked(wait_seconds, allow_interrupt=False)

    def _capture(self, activate: bool = False) -> Screenshot:
        if self._stopped():
            raise KeyboardInterrupt("farm vehicle task stopped before capture")
        self._handle_overlay_toggle()
        restore_overlay = self.overlay.prepare_capture()
        try:
            if self._stopped():
                raise KeyboardInterrupt("farm vehicle task stopped before window activation")
            capture, _, _, _, _ = capture_game_client(
                self.window_config,
                activate=activate,
                use_client_region=bool(self.capture_config.get("use_client_region", True)),
            )
        except Exception:
            self.overlay.finish_capture(None, restore_overlay)
            raise
        self.last_capture = capture
        self.overlay.finish_capture(capture, restore_overlay)
        return capture

    def _capture_and_match(
        self,
        label: str,
        templates: list[LoadedTemplate] | None = None,
        ignore_regions: list[RegionTuple] | tuple[RegionTuple, ...] | None = None,
        detector_names: list[str] | tuple[str, ...] | None = None,
        ocr_names: list[str] | tuple[str, ...] | None = None,
        display_overlay: bool = True,
        activate: bool | None = None,
    ) -> list[TargetMatch]:
        if self._stopped():
            return []
        self._wait_if_paused()
        should_activate = (
            bool(self.capture_config.get("activate_window", True))
            if activate is None
            else bool(activate)
        )
        capture = self._capture(activate=should_activate)
        logging.info("farm vehicle capture label=%s origin=%s region=%s", label, capture.origin, capture.region)
        selected_templates = self.templates if templates is None else templates
        effective_ignore_regions = self._scale_regions_for_capture(ignore_regions or [], capture)
        if effective_ignore_regions:
            logging.info("farm vehicle capture ignore_regions label=%s regions=%s", label, effective_ignore_regions)
        matches: list[TargetMatch] = []
        if selected_templates:
            matches.extend(match_targets(capture.image, selected_templates, ignore_regions=effective_ignore_regions))
        if detector_names:
            matches.extend(self._detect_targets(capture, detector_names, effective_ignore_regions))
        if ocr_names:
            matches.extend(
                match_ocr_targets(
                    capture.image,
                    self._ocr_targets_by_name(list(ocr_names)),
                    self._get_ocr_engine(),
                )
            )
        best = select_best_match(matches)
        if display_overlay:
            if best is not None:
                self.overlay.update_status(
                    last_marker=best.name,
                    last_score=best.score,
                    log=f"{label}: {best.name} {best.score:.3f}",
                )
                self.overlay.show_matches(capture, matches, selected=best)
            else:
                self.overlay.update_status(log=f"{label}: no match")
                self.overlay.clear_matches()
        self._emit_event(
            "detection",
            label=label,
            matches=[
                {
                    "name": item.name,
                    "score": item.score,
                    "top_left": list(item.top_left),
                    "size": list(item.size),
                }
                for item in matches
            ],
            selected=None if best is None else best.name,
        )
        return matches

    def _detect_targets(
        self,
        capture: Screenshot,
        names: list[str] | tuple[str, ...],
        ignore_regions: tuple[RegionTuple, ...],
    ) -> list[TargetMatch]:
        detector = self._get_detector()
        if detector is None:
            logging.warning("detector recognition requested but detection.enabled is false names=%s", names)
            return []

        matches: list[TargetMatch] = []
        for detected in detector.detect(capture.image, names=names, ignore_regions=ignore_regions):
            spec = TemplateSpec(
                name=detected.name,
                path=detector.model_path,
                category="farm",
                role="observe",
                threshold=detector.class_thresholds.get(detected.name, detector.score_threshold),
                priority=0,
                click=False,
                required=False,
            )
            matches.append(
                TargetMatch(
                    spec=spec,
                    score=detected.score,
                    top_left=detected.top_left,
                    size=detected.size,
                )
            )
        return matches

    def _get_detector(self) -> FarmIconDetector | None:
        if self.detector is None:
            self.detector = create_farm_icon_detector(self.config, self.root)
        return self.detector

    def _click_match(self, match: TargetMatch) -> None:
        if self.last_capture is None:
            raise RuntimeError("cannot click a match before capturing the game client")
        client_x, client_y = match.click_point
        origin_x, origin_y = self.last_capture.origin
        self.overlay.update_status(
            mouse=f"left@{client_x},{client_y}",
            last_action=f"click {match.name}",
            log=f"click {match.name} {match.score:.3f}",
        )
        self.input.safe_click(origin_x + client_x, origin_y + client_y)
        self.overlay.update_status(mouse="-", last_action=f"clicked {match.name}")

    def _run_key_script(self, actions: Any, label_prefix: str) -> None:
        if not isinstance(actions, list):
            raise ValueError(f"{label_prefix} actions must be a list.")

        for index, raw_action in enumerate(actions, start=1):
            if self._stopped():
                raise KeyboardInterrupt(f"{label_prefix} stopped")
            self._wait_if_paused()
            if not isinstance(raw_action, dict):
                raise ValueError(f"{label_prefix} action #{index} must be a mapping.")

            action = str(raw_action.get("action", "press")).strip().lower()
            label = f"{label_prefix}_{index:02d}_{action}"
            logging.info("%s action=%s config=%s", label_prefix, action, raw_action)

            if action == "wait":
                self._sleep_checked(float(raw_action.get("seconds", 0.0)))
            elif action == "press":
                self.input.press(
                    str(raw_action["key"]),
                    hold_seconds=float(raw_action.get("hold_seconds", 0.05)),
                )
            elif action == "hold":
                key = str(raw_action["key"])
                self.input.key_down(key)
                try:
                    self._sleep_checked(float(raw_action.get("hold_seconds", raw_action.get("seconds", 0.0))))
                finally:
                    self.input.key_up(key)
            elif action == "down":
                self.input.key_down(str(raw_action["key"]))
            elif action == "up":
                self.input.key_up(str(raw_action["key"]))
            elif action == "screenshot":
                self._capture_and_match(label)
                self._save_last(label)
            else:
                raise ValueError(f"Unsupported {label_prefix} action #{index}: {action!r}")

            after_wait = float(raw_action.get("after_wait_seconds", 0.0))
            if after_wait > 0:
                self._sleep_checked(after_wait)

    def _run_recording_replay(self, replay_config: dict[str, Any], label_prefix: str) -> None:
        raw_path = replay_config.get("path")
        if not raw_path:
            raise ValueError(f"{label_prefix} recording_replay.path is required when enabled.")
        path = Path(str(raw_path))
        if not path.is_absolute():
            path = self.root / path

        data = json.loads(path.read_text(encoding="utf-8"))
        raw_camera_control = data.get("camera_control", {})
        camera_control_config = raw_camera_control if isinstance(raw_camera_control, dict) else {}
        recording_camera_mode = str(
            camera_control_config.get("mode", data.get("mode", "visible"))
        ).strip().lower()
        ignore_keys = {str(key).strip().lower() for key in replay_config.get("ignore_keys", [])}
        speed = max(0.01, float(replay_config.get("speed", 1.0)))
        max_initial_wait = replay_config.get("max_initial_wait_seconds")
        max_initial_wait_seconds = None if max_initial_wait in (None, "") else float(max_initial_wait)
        include_mouse = bool(replay_config.get("include_mouse", False))
        include_unheld_mouse_moves = bool(replay_config.get("include_unheld_mouse_moves", False))
        start_ms_config = replay_config.get("start_ms")
        end_ms_config = replay_config.get("end_ms")
        start_ms = None if start_ms_config in (None, "") else int(start_ms_config)
        end_ms = None if end_ms_config in (None, "") else int(end_ms_config)

        held: set[str] = set()
        mouse_held: set[str] = set()
        edges: list[dict[str, Any]] = []
        baseline_ms: int | None = None
        for event in data.get("events", []):
            event_type = event.get("type")
            t_ms = int(event.get("t_ms", 0))
            if start_ms is not None and t_ms < start_ms:
                continue
            if end_ms is not None and t_ms > end_ms:
                continue

            if event_type in {"key_down", "key_up"}:
                key = str(event.get("key", "")).strip().lower()
                if not key:
                    continue
                if key in ignore_keys:
                    if not edges:
                        baseline_ms = t_ms
                    if key in held and event_type == "key_up":
                        held.discard(key)
                    continue

                if event_type == "key_down":
                    if key in held:
                        continue
                    held.add(key)
                    edges.append({"t_ms": t_ms, "kind": "key_down", "key": key})
                elif event_type == "key_up" and key in held:
                    held.remove(key)
                    edges.append({"t_ms": t_ms, "kind": "key_up", "key": key})
                continue

            if include_mouse and event_type == "mouse_move" and (mouse_held or include_unheld_mouse_moves):
                edges.append(
                    {
                        "t_ms": t_ms,
                        "kind": "mouse_move",
                        "client": event.get("client"),
                        "screen": event.get("screen"),
                        "inside_client": bool(event.get("inside_client", False)),
                    }
                )
                continue

            if include_mouse and event_type == "mouse_scroll":
                delta = event.get("delta")
                if not isinstance(delta, list) or len(delta) != 2:
                    continue
                edges.append(
                    {
                        "t_ms": t_ms,
                        "kind": "mouse_scroll",
                        "delta": [int(delta[0]), int(delta[1])],
                        "client": event.get("client"),
                        "screen": event.get("screen"),
                        "inside_client": bool(event.get("inside_client", False)),
                    }
                )
                continue

            if event_type == "camera_turn":
                mode = str(event.get("mode", recording_camera_mode)).strip().lower()
                direction = str(event.get("direction", "")).strip().lower()
                duration_ms = int(event.get("duration_ms", 0))
                if mode not in {"hidden", "visible"}:
                    raise ValueError(f"{label_prefix} camera turn mode is invalid: {mode!r}")
                if direction not in {"left", "right"} or duration_ms <= 0:
                    raise ValueError(f"{label_prefix} camera turn event is invalid: {event}")
                if end_ms is not None:
                    duration_ms = min(duration_ms, max(0, end_ms - t_ms))
                if duration_ms <= 0:
                    continue
                edges.append(
                    {
                        "t_ms": t_ms,
                        "kind": "camera_turn",
                        "mode": mode,
                        "direction": direction,
                        "duration_ms": duration_ms,
                    }
                )
                continue

            if include_mouse and event_type in {"mouse_down", "mouse_up"}:
                button = str(event.get("button", "left")).strip().lower()
                if event_type == "mouse_down":
                    if button in mouse_held:
                        continue
                    mouse_held.add(button)
                    edges.append(
                        {
                            "t_ms": t_ms,
                            "kind": "mouse_down",
                            "button": button,
                            "client": event.get("client"),
                            "screen": event.get("screen"),
                            "inside_client": bool(event.get("inside_client", False)),
                        }
                    )
                    continue
                if button in mouse_held:
                    mouse_held.remove(button)
                edges.append(
                    {
                        "t_ms": t_ms,
                        "kind": "mouse_up",
                        "button": button,
                        "client": event.get("client"),
                        "screen": event.get("screen"),
                        "inside_client": bool(event.get("inside_client", False)),
                    }
                )

        edges.extend(self._recording_match_wait_edges(replay_config, start_ms=start_ms, end_ms=end_ms))
        edges.sort(key=lambda item: int(item["t_ms"]))

        if not edges:
            raise ValueError(f"recording replay has no keyboard, mouse, click, scroll, or camera edges: {path}")

        camera_edges = [edge for edge in edges if edge.get("kind") == "camera_turn"]
        raw_camera_settings = camera_control_config.get("settings", data.get("settings", {}))
        if camera_edges and not isinstance(raw_camera_settings, dict):
            raise ValueError(f"{label_prefix} camera settings must be an object")
        camera_settings = CameraSettings.from_dict(raw_camera_settings) if camera_edges else None
        previous_camera_end_ms = -1
        for edge in camera_edges:
            camera_start_ms = int(edge["t_ms"])
            if camera_start_ms < previous_camera_end_ms:
                raise ValueError(f"{label_prefix} recording contains overlapping F5/F6 camera turns")
            previous_camera_end_ms = camera_start_ms + int(edge["duration_ms"])

        coordinate_mapper: ClientCoordinateMapper | None = None
        coordinate_edges = [
            edge for edge in edges if str(edge.get("kind", "")) in {"mouse_move", "mouse_down", "mouse_up", "mouse_scroll"}
        ]
        if coordinate_edges:
            replay_capture = self._capture(activate=True)
            image_height, image_width = replay_capture.image.shape[:2]
            coordinate_mapper = ClientCoordinateMapper.from_recording(
                data,
                origin=replay_capture.origin,
                current_size=(image_width, image_height),
            )
            for edge in coordinate_edges:
                if edge.get("kind") == "mouse_up" and edge.get("client") is None:
                    continue
                coordinate_mapper.screen_point(
                    edge.get("client"),
                    bool(edge.get("inside_client", False)),
                )
        camera_origin = self._recording_client_origin() if camera_edges else None

        if baseline_ms is None or baseline_ms > int(edges[0]["t_ms"]):
            baseline_ms = start_ms if start_ms is not None else int(edges[0]["t_ms"])

        first_event_elapsed_seconds = max(0.0, (int(edges[0]["t_ms"]) - baseline_ms) / 1000.0 / speed)
        initial_trim_seconds = 0.0
        if max_initial_wait_seconds is not None:
            initial_trim_seconds = max(
                0.0,
                first_event_elapsed_seconds - max(0.0, max_initial_wait_seconds),
            )

        replay_started = time.monotonic()
        timeline_shift_seconds = 0.0
        max_lateness_ms = 0.0
        active_keys: set[str] = set()
        active_mouse: set[str] = set()
        camera_stop_event = threading.Event()
        camera_workers: list[threading.Thread] = []
        camera_errors: list[BaseException] = []
        camera_error_lock = threading.Lock()

        def raise_camera_error() -> None:
            with camera_error_lock:
                error = camera_errors[0] if camera_errors else None
            if error is not None:
                raise RuntimeError(f"{label_prefix} F5/F6 camera turn failed") from error

        def start_camera_turn(edge: dict[str, Any]) -> None:
            if camera_settings is None or camera_origin is None:
                raise ValueError(f"{label_prefix} camera settings or client origin are missing")

            def run_camera_turn() -> None:
                try:
                    execute_camera_turn(
                        controller=self.input,
                        origin=camera_origin,
                        mode=str(edge["mode"]),
                        direction=str(edge["direction"]),
                        settings=camera_settings,
                        duration_ms=max(1, int(round(int(edge["duration_ms"]) / speed))),
                        control=CameraReplayControl(
                            stop_requested=lambda: camera_stop_event.is_set() or self._stopped(),
                            paused=lambda: self.control.paused,
                        ),
                    )
                except CameraReplayInterrupted as exc:
                    if not camera_stop_event.is_set() and not self._stopped():
                        with camera_error_lock:
                            camera_errors.append(exc)
                        camera_stop_event.set()
                except BaseException as exc:
                    with camera_error_lock:
                        camera_errors.append(exc)
                    camera_stop_event.set()

            worker = threading.Thread(
                target=run_camera_turn,
                name=f"{label_prefix}-camera-turn",
                daemon=True,
            )
            camera_workers.append(worker)
            worker.start()

        try:
            for edge in edges:
                raise_camera_error()
                t_ms = int(edge["t_ms"])
                target_elapsed_seconds = max(
                    0.0,
                    (t_ms - baseline_ms) / 1000.0 / speed - initial_trim_seconds,
                )
                target_deadline = replay_started + target_elapsed_seconds + timeline_shift_seconds
                if camera_edges:
                    paused_seconds = self._wait_until_checked(
                        target_deadline,
                        poll_callback=raise_camera_error,
                    )
                else:
                    paused_seconds = self._wait_until_checked(target_deadline)
                raise_camera_error()
                timeline_shift_seconds += paused_seconds
                effective_deadline = target_deadline + paused_seconds
                max_lateness_ms = max(
                    max_lateness_ms,
                    max(0.0, time.monotonic() - effective_deadline) * 1000.0,
                )

                kind = str(edge["kind"])
                logging.info("%s recording replay kind=%s edge=%s", label_prefix, kind, edge)
                if kind == "key_down":
                    key = str(edge["key"])
                    active_keys.add(key)
                    self.overlay.update_status(
                        active_keys=self._format_active_keys(active_keys),
                        last_action=f"down {key}",
                        log=f"{label_prefix}: {key} down",
                    )
                    self.input.key_down(key)
                elif kind == "key_up":
                    key = str(edge["key"])
                    active_keys.discard(key)
                    self.overlay.update_status(
                        active_keys=self._format_active_keys(active_keys),
                        last_action=f"up {key}",
                        log=f"{label_prefix}: {key} up",
                    )
                    self.input.key_up(key)
                elif kind == "mouse_down":
                    x, y = self._recording_mouse_screen_point(edge, label_prefix, coordinate_mapper)
                    button = str(edge.get("button", "left"))
                    active_mouse.add(button)
                    self.overlay.update_status(
                        mouse=f"{self._format_mouse(active_mouse)}@{x},{y}",
                        last_action=f"mouse {button} down",
                        log=f"{label_prefix}: mouse {button} down",
                    )
                    self.input.move_to(x, y, duration_seconds=0.0)
                    self.input.mouse_down(button)
                elif kind == "mouse_move":
                    x, y = self._recording_mouse_screen_point(edge, label_prefix, coordinate_mapper)
                    self.overlay.update_status(
                        mouse=f"{self._format_mouse(active_mouse)}@{x},{y}",
                        last_action="mouse move",
                        log=f"{label_prefix}: mouse move",
                    )
                    self.input.move_to(x, y, duration_seconds=0.0)
                elif kind == "mouse_up":
                    button = str(edge.get("button", "left"))
                    if edge.get("client") is not None or edge.get("screen") is not None:
                        x, y = self._recording_mouse_screen_point(edge, label_prefix, coordinate_mapper)
                        self.input.move_to(x, y, duration_seconds=0.0)
                    active_mouse.discard(button)
                    self.overlay.update_status(
                        mouse=self._format_mouse(active_mouse),
                        last_action=f"mouse {button} up",
                        log=f"{label_prefix}: mouse {button} up",
                    )
                    self.input.mouse_up(button, after_delay_seconds=0.0)
                elif kind == "mouse_scroll":
                    x, y = self._recording_mouse_screen_point(edge, label_prefix, coordinate_mapper)
                    self.input.move_to(x, y, duration_seconds=0.0)
                    delta = edge.get("delta", [0, 0])
                    self.input.scroll(dx=int(delta[0]), dy=int(delta[1]))
                elif kind == "camera_turn":
                    self.overlay.update_status(
                        last_action=f"camera {edge['direction']}",
                        log=f"{label_prefix}: camera {edge['direction']} {edge['duration_ms']} ms",
                    )
                    start_camera_turn(edge)
                elif kind == "match_wait":
                    self._run_recording_match_wait(edge, label_prefix)
                    resume_t_ms = int(edge.get("resume_t_ms", t_ms))
                    resume_elapsed_seconds = max(
                        0.0,
                        (resume_t_ms - baseline_ms) / 1000.0 / speed - initial_trim_seconds,
                    )
                    timeline_shift_seconds = time.monotonic() - replay_started - resume_elapsed_seconds
                    continue
            for worker in camera_workers:
                while worker.is_alive():
                    raise_camera_error()
                    if self._stopped():
                        raise KeyboardInterrupt("farm vehicle task stopped during camera turn")
                    self._raise_if_interrupted()
                    self._handle_overlay_toggle()
                    self._pump_overlays()
                    worker.join(timeout=0.01)
            raise_camera_error()
        finally:
            camera_stop_event.set()
            for worker in camera_workers:
                worker.join(timeout=2.0)
            for key in list(active_keys | held):
                self.input.key_up(key)
            for button in list(active_mouse | mouse_held):
                self.input.mouse_up(button, after_delay_seconds=0.0)
            self.input.release_all()
            self.overlay.update_status(active_keys="-", mouse="-")
        logging.info(
            "%s recording replay timing edges=%s max_lateness_ms=%.3f",
            label_prefix,
            len(edges),
            max_lateness_ms,
        )

    def _recording_match_wait_edges(
        self,
        replay_config: dict[str, Any],
        start_ms: int | None,
        end_ms: int | None,
    ) -> list[dict[str, Any]]:
        raw_waits = replay_config.get("match_waits", replay_config.get("marker_waits", []))
        if not isinstance(raw_waits, list):
            return []

        edges: list[dict[str, Any]] = []
        for index, raw_wait in enumerate(raw_waits, start=1):
            if not isinstance(raw_wait, dict) or not bool(raw_wait.get("enabled", True)):
                continue
            raw_t_ms = raw_wait.get("t_ms", raw_wait.get("start_t_ms", raw_wait.get("after_t_ms")))
            if raw_t_ms in (None, ""):
                raise ValueError(f"recording match_wait #{index} must define t_ms.")
            t_ms = int(raw_t_ms)
            if start_ms is not None and t_ms < start_ms:
                continue
            if end_ms is not None and t_ms > end_ms:
                continue

            edge = dict(raw_wait)
            edge["kind"] = "match_wait"
            edge["t_ms"] = t_ms
            if edge.get("resume_t_ms") in (None, ""):
                raw_resume = edge.get("end_t_ms", edge.get("until_t_ms"))
                if raw_resume not in (None, ""):
                    edge["resume_t_ms"] = int(raw_resume)
            edges.append(edge)
        return edges

    def _run_recording_match_wait(self, edge: dict[str, Any], label_prefix: str) -> None:
        names = edge.get("match_any", edge.get("templates", []))
        if isinstance(names, str):
            names = [names]
        names = [str(name) for name in names if str(name).strip()]
        if not names:
            raise ValueError(f"{label_prefix} match_wait has no match_any names")

        recognition_source = str(edge.get("recognition_source", "template")).strip().lower()
        templates, detector_names, ocr_names = self._resolve_named_targets(names, recognition_source)

        wait_seconds = max(0.0, float(edge.get("duration_seconds", edge.get("wait_seconds", 0.0))))
        interval_seconds = max(0.1, float(edge.get("interval_seconds", 1.0)))
        click_on_match = bool(edge.get("click_on_match", True))
        stop_on_match = bool(edge.get("stop_on_match", True))
        label = self._safe_label(str(edge.get("label", "match_wait")))
        max_clicks = max(1, int(edge.get("max_clicks", 1)))
        click_until_gone = bool(edge.get("click_until_gone", False))
        clicked_count = 0
        attempt = 1
        deadline = monotonic_deadline(wait_seconds)

        logging.info(
            "%s recording match_wait start label=%s names=%s duration=%.3f interval=%.3f",
            label_prefix,
            label,
            names,
            wait_seconds,
            interval_seconds,
        )
        self.overlay.update_status(current_step=f"{label_prefix}_{label}", log=f"{label}: wait")

        while not deadline_expired(deadline):
            scan_label = f"{label_prefix}_{label}_scan_{attempt:02d}"
            matches = self._capture_and_match(
                scan_label,
                templates=templates,
                detector_names=detector_names,
                ocr_names=ocr_names,
            )
            best = select_best_match(filter_matches(matches, names=names))
            if best is not None:
                logging.info(
                    "%s recording match_wait matched label=%s marker=%s score=%.4f attempt=%s",
                    label_prefix,
                    label,
                    best.name,
                    best.score,
                    attempt,
                )
                if click_on_match and clicked_count < max_clicks:
                    if click_until_gone:
                        result = self._click_match_until_gone(
                            {
                                "match_any": names,
                                "recognition_source": recognition_source,
                                "max_clicks": max_clicks,
                                "after_click_wait_seconds": edge.get("after_click_wait_seconds", 0.5),
                            },
                            best,
                            f"{label_prefix}_{label}",
                        )
                        if result.status != StateStatus.SUCCEEDED:
                            raise FarmVehicleActionFailed(result.message)
                        clicked_count = max_clicks
                    else:
                        self._click_match(best)
                        clicked_count += 1
                if stop_on_match:
                    break

            attempt += 1
            remaining_seconds = max(0.0, deadline - time.monotonic())
            if remaining_seconds <= 0:
                break
            self._sleep_checked(min(interval_seconds, remaining_seconds))

        logging.info(
            "%s recording match_wait finished label=%s clicked=%s attempts=%s",
            label_prefix,
            label,
            clicked_count,
            attempt,
        )

    def _format_active_keys(self, keys: set[str]) -> str:
        if not keys:
            return "-"
        preferred = ["w", "a", "s", "d", "q", "e", "r"]
        ordered = [key for key in preferred if key in keys]
        ordered.extend(sorted(keys - set(ordered)))
        return "+".join(ordered)

    def _format_mouse(self, buttons: set[str]) -> str:
        if not buttons:
            return "-"
        preferred = ["left", "right", "middle"]
        ordered = [button for button in preferred if button in buttons]
        ordered.extend(sorted(buttons - set(ordered)))
        return "+".join(ordered)

    def _recording_mouse_screen_point(
        self,
        edge: dict[str, Any],
        label_prefix: str,
        mapper: ClientCoordinateMapper | None,
    ) -> tuple[int, int]:
        if mapper is None:
            raise ValueError(f"{label_prefix} mouse replay has no validated client coordinate mapper")
        return mapper.screen_point(edge.get("client"), bool(edge.get("inside_client", False)))

    def _recording_client_origin(self) -> tuple[int, int]:
        if self.last_capture is None:
            self._capture(activate=True)
        if self.last_capture is None:
            raise RuntimeError("recording replay has no game-client capture origin")
        return int(self.last_capture.origin[0]), int(self.last_capture.origin[1])

    def _safe_label(self, value: str) -> str:
        return "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in value).strip("_") or "step"

    def _templates_by_name(self, names: list[str]) -> list[LoadedTemplate]:
        name_set = set(names)
        return [item for item in self.templates if item.spec.name in name_set]

    def _ocr_targets_by_name(self, names: list[str]) -> list[OcrTargetSpec]:
        name_set = set(names)
        return [item for item in self.ocr_targets if item.name in name_set]

    def _save_last(self, label: str) -> None:
        if self.last_capture is None:
            return
        directory = self.root / "logs" / "farm_vehicle"
        path = timestamped_path(directory, label)
        save_screenshot(self.last_capture.image, path)
        logging.info("farm vehicle screenshot saved label=%s path=%s", label, path)

    def _sleep_checked(self, seconds: float, allow_interrupt: bool = True) -> None:
        self._wait_until_checked(time.monotonic() + max(0.0, float(seconds)), allow_interrupt=allow_interrupt)

    def _wait_until_checked(
        self,
        deadline: float,
        allow_interrupt: bool = True,
        poll_callback: Any | None = None,
    ) -> float:
        adjusted_deadline = float(deadline)
        paused_seconds = 0.0
        while True:
            if poll_callback is not None:
                poll_callback()
            if self._stopped():
                raise KeyboardInterrupt("farm vehicle task stopped")
            if allow_interrupt:
                self._raise_if_interrupted()
            self._handle_overlay_toggle()
            self._pump_overlays()

            if self.control.paused:
                pause_started = time.monotonic()
                while self.control.paused and not self._stopped():
                    if poll_callback is not None:
                        poll_callback()
                    if allow_interrupt:
                        self._raise_if_interrupted()
                    self._handle_overlay_toggle()
                    self._pump_overlays()
                    time.sleep(0.05)
                pause_duration = time.monotonic() - pause_started
                adjusted_deadline += pause_duration
                paused_seconds += pause_duration
                continue

            remaining_seconds = adjusted_deadline - time.monotonic()
            if remaining_seconds <= 0:
                return paused_seconds
            time.sleep(min(0.005, remaining_seconds))

    def _stopped(self) -> bool:
        return self.control.stopped

    def _raise_if_interrupted(self) -> None:
        if self.control.consume_interrupt_request():
            self.input.release_all()
            self.overlay.update_status(active_keys="-", mouse="-", log="current action interrupted")
            raise FarmVehicleInterrupted()

    def _handle_overlay_toggle(self) -> None:
        if self.control.consume_overlay_toggle():
            visible = self.overlay.toggle()
            self._emit_event("overlay_visibility", visible=visible)
        consume_visibility = getattr(self.control, "consume_overlay_visibility_request", None)
        if callable(consume_visibility):
            requested = consume_visibility()
            if requested is not None:
                visible = self.overlay.set_visible(bool(requested))
                self._emit_event("overlay_visibility", visible=visible)

    def _pump_overlays(self) -> None:
        self._sync_overlay_geometry()
        pump = getattr(self.overlay, "pump", None)
        if callable(pump):
            pump()

    def _sync_overlay_geometry(self, force: bool = False) -> None:
        """Keep persistent overlay windows attached to the live game client."""
        bind_geometry = getattr(self.overlay, "bind_client_geometry", None)
        if not callable(bind_geometry) or not bool(getattr(self.overlay, "enabled", True)):
            return

        raw_hwnd = getattr(self.input, "target_window_hwnd", None)
        if raw_hwnd in (None, ""):
            raw_hwnd = self.window_config.get("hwnd")
        if raw_hwnd in (None, ""):
            return

        now = time.monotonic()
        interval = float(getattr(self, "_overlay_follow_interval_seconds", 0.05))
        last_follow_at = float(getattr(self, "_overlay_last_follow_at", 0.0))
        if not force and now - last_follow_at < interval:
            return
        self._overlay_last_follow_at = now

        try:
            bounds = get_client_bounds(int(raw_hwnd), log_result=False)
            if bounds.width <= 0 or bounds.height <= 0:
                return
            bind_geometry(
                (int(bounds.left), int(bounds.top)),
                (int(bounds.width), int(bounds.height)),
            )
            self._overlay_follow_error_reported = False
        except Exception:
            if not bool(getattr(self, "_overlay_follow_error_reported", False)):
                logging.warning("failed to update overlay position from target client hwnd=%s", raw_hwnd, exc_info=True)
                self._overlay_follow_error_reported = True

    def _wait_if_paused(self) -> None:
        while self.control.paused and not self._stopped():
            self._handle_overlay_toggle()
            self._pump_overlays()
            time.sleep(0.05)


def _normalize_region(value: Any) -> RegionTuple:
    values = tuple(int(item) for item in value)
    if len(values) != 4:
        raise ValueError("region must be [left, top, width, height].")
    left, top, width, height = values
    if left < 0 or top < 0 or width <= 0 or height <= 0:
        raise ValueError("region must use non-negative left/top and positive width/height.")
    return left, top, width, height


def _normalize_regions(value: Any) -> tuple[RegionTuple, ...]:
    if value in (None, "", "null"):
        return ()
    if isinstance(value, dict):
        raise ValueError("regions must be a region or a list of regions, not a mapping.")

    values = list(value)
    if len(values) == 4 and all(not isinstance(item, (list, tuple)) for item in values):
        return (_normalize_region(values),)
    return tuple(_normalize_region(item) for item in values)


def _normalize_point(value: Any) -> tuple[int, int]:
    values = tuple(int(item) for item in value)
    if len(values) != 2:
        raise ValueError("point must be [x, y].")
    return values[0], values[1]


def _normalize_size(value: Any) -> tuple[int, int]:
    values = tuple(int(item) for item in value)
    if len(values) != 2:
        raise ValueError("size must be [width, height].")
    return max(1, values[0]), max(1, values[1])


def _deep_merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge_dicts(existing, value)
        else:
            merged[key] = value
    return merged
