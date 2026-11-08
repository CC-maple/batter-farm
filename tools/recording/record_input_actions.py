from __future__ import annotations

import argparse
import copy
import json
import logging
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pyautogui
from pynput import keyboard, mouse

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from main import load_config, resolve_target_selector, setup_logging
from src.core.camera_control import (
    CameraReplayControl,
    CameraReplayInterrupted,
    CameraSettings,
    execute_camera_turn,
)
from src.core.game_window import screen_to_client
from src.core.input import InputController, is_admin
from src.core.overlay import create_overlay_controller
from src.utils.window import WindowBounds, WindowNotFoundError, activate_window, get_client_bounds
from tools.recording.input_tool_common import start_stdin_stop_listener


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def key_name(key: keyboard.Key | keyboard.KeyCode) -> str:
    if isinstance(key, keyboard.KeyCode):
        if key.char is not None:
            return str(key.char).lower()
        if key.vk is not None:
            return f"vk_{key.vk}"
        return "keycode"
    return str(key).replace("Key.", "").lower()


def mouse_button_name(button: mouse.Button) -> str:
    return str(button).replace("Button.", "").lower()


class EmptyRecordingError(ValueError):
    """Raised when a recording contains no replayable input."""


class RecordingCancelled(RuntimeError):
    """Raised when recording is stopped before the listeners are ready."""


@dataclass
class CameraTurnSession:
    hotkey: str
    direction: str
    t_ms: int
    started_monotonic: float
    stop_event: threading.Event = field(default_factory=threading.Event)
    released_monotonic: float | None = None
    thread: threading.Thread | None = None


def create_recording_overlay(config: dict[str, Any], hwnd: int) -> Any:
    overlay_runtime_config = copy.deepcopy(config)
    overlay_config = overlay_runtime_config.setdefault("overlay", {})
    overlay_config["enabled"] = True
    overlay_config["start_visible"] = True
    overlay_config["hide_before_capture"] = False
    overlay_config["title"] = "BATTERFARM · 键鼠录制"
    match_config = overlay_config.setdefault("match_boxes", {})
    match_config["enabled"] = False
    return create_overlay_controller(overlay_runtime_config, owner_hwnd=hwnd)


class InputActionRecorder:
    def __init__(
        self,
        config: dict[str, Any],
        output_dir: Path,
        move_sample_ms: int,
        stop_key: str,
        mark_key: str,
        camera_mode: str = "visible",
        camera_settings: CameraSettings | None = None,
        camera_controller: InputController | None = None,
        overlay: Any | None = None,
        overlay_enabled: bool = False,
    ) -> None:
        self.config = config
        self.output_dir = output_dir
        self.move_sample_seconds = max(0.0, float(move_sample_ms) / 1000.0)
        self.stop_key = stop_key.lower()
        self.mark_key = mark_key.lower()
        self.camera_mode = str(camera_mode).strip().lower()
        if self.camera_mode not in {"hidden", "visible"}:
            raise ValueError("camera_mode must be hidden or visible")
        self.camera_settings = camera_settings or CameraSettings()
        self.camera_controller = camera_controller
        self.camera_origin: tuple[int, int] | None = None
        self.camera_session: CameraTurnSession | None = None
        self.camera_workers: list[threading.Thread] = []
        self.camera_input_active = threading.Event()
        self.camera_failure: BaseException | None = None
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.events: list[dict[str, Any]] = []
        self.started_monotonic = time.monotonic()
        self.started_at = datetime.now().isoformat(timespec="seconds")
        self.last_move_monotonic = 0.0
        self.active_buttons: set[str] = set()
        self.active_keys: set[str] = set()
        self.capture_origin: tuple[int, int] | None = None
        self.capture_region: tuple[int, int, int, int] | None = None
        self.hwnd: int | None = None
        self.title = ""
        self.session_id = timestamp()
        self.coordinate_source = "none"
        self.overlay = overlay
        self.overlay_enabled = bool(overlay_enabled)
        self.overlay_updates: queue.SimpleQueue[dict[str, Any]] = queue.SimpleQueue()
        self._last_geometry_refresh = 0.0
        self.accepting_events = True

    def refresh_window(self) -> None:
        window_config = self.config.get("window", {})
        capture_config = self.config.get("capture", {})
        selector, title, hwnd = resolve_target_selector(window_config)
        bounds: WindowBounds | None = activate_window(selector=selector)

        use_client_region = bool(capture_config.get("use_client_region", True))
        if hwnd is None or bounds is None:
            raise WindowNotFoundError(
                "Input recording aborted: the configured game window could not be resolved and activated."
            )

        client_bounds = get_client_bounds(hwnd)
        self.camera_origin = (client_bounds.left, client_bounds.top)
        if use_client_region:
            self.capture_region = client_bounds.as_region()
            self.capture_origin = (client_bounds.left, client_bounds.top)
            self.coordinate_source = "screen_to_client"
        else:
            self.capture_region = bounds.as_region()
            self.capture_origin = (bounds.left, bounds.top)
            self.coordinate_source = "window_bounds"

        self.hwnd = hwnd
        self.title = title
        if self.camera_controller is None:
            input_config = self.config.get("input", {})
            self.camera_controller = InputController(
                click_delay_seconds=float(input_config.get("click_delay_seconds", 0.15)),
                click_hold_seconds=float(input_config.get("click_hold_seconds", 0.08)),
                move_duration_seconds=float(input_config.get("move_duration_seconds", 0.05)),
                click_backend=str(input_config.get("click_backend", "sendinput")),
                fail_safe=bool(input_config.get("fail_safe", True)),
                target_window_title=self.title,
                target_window_hwnd=self.hwnd,
            )
        if self.overlay_enabled and self.overlay is None:
            self.overlay = create_recording_overlay(self.config, self.hwnd)
        logging.info(
            "input-only recorder window title=%r hwnd=%s region=%s source=%s",
            self.title,
            self.hwnd,
            self.capture_region,
            self.coordinate_source,
        )

    def elapsed_ms(self) -> int:
        return int(round((time.monotonic() - self.started_monotonic) * 1000))

    def screen_to_record_client(self, x: int, y: int) -> tuple[int, int] | None:
        if self.hwnd:
            try:
                return screen_to_client(self.hwnd, x, y)
            except Exception:
                logging.exception("failed to convert screen to client coordinates")

        if self.capture_region is None or self.capture_origin is None:
            return None
        return int(x) - int(self.capture_origin[0]), int(y) - int(self.capture_origin[1])

    def append_event(self, event: dict[str, Any]) -> None:
        event.setdefault("t_ms", self.elapsed_ms())
        if "screen" in event:
            client = self.screen_to_record_client(int(event["screen"][0]), int(event["screen"][1]))
            event["client"] = list(client) if client is not None else None
            event["inside_client"] = self._inside_client(client)

        with self.lock:
            self.events.append(event)

    def _inside_client(self, client: tuple[int, int] | None) -> bool | None:
        if client is None or self.capture_region is None:
            return None
        _, _, width, height = self.capture_region
        x, y = client
        return 0 <= x < width and 0 <= y < height

    def _queue_overlay(self, **items: Any) -> None:
        if self.overlay is not None:
            self.overlay_updates.put(dict(items))

    def _pump_overlay(self, *, force: bool = False) -> None:
        overlay = self.overlay
        if overlay is None or self.hwnd is None:
            return
        now = time.monotonic()
        if force or now - self._last_geometry_refresh >= 0.2:
            bounds = get_client_bounds(self.hwnd)
            overlay.bind_client_geometry(
                (bounds.left, bounds.top),
                (bounds.width, bounds.height),
            )
            self._last_geometry_refresh = now
        while True:
            try:
                items = self.overlay_updates.get_nowait()
            except queue.Empty:
                break
            overlay.update_status(**items)
        overlay.pump(force=force)

    def _format_active_keys(self) -> str:
        with self.lock:
            keys = sorted(self.active_keys)
        return "+".join(key.upper() for key in keys) if keys else "-"

    def _start_camera_turn(self, hotkey: str, direction: str) -> None:
        with self.lock:
            if self.camera_session is not None or self.stop_event.is_set():
                return
            if self.camera_controller is None or self.camera_origin is None:
                raise RuntimeError("camera input controller is not initialized")
            session = CameraTurnSession(
                hotkey=hotkey,
                direction=direction,
                t_ms=self.elapsed_ms(),
                started_monotonic=time.monotonic(),
            )
            worker = threading.Thread(
                target=self._run_camera_turn,
                args=(session,),
                name=f"camera-turn-{direction}",
                daemon=True,
            )
            session.thread = worker
            self.camera_session = session
            self.camera_workers.append(worker)
            self.camera_input_active.set()
            worker.start()
        direction_name = "左转" if direction == "left" else "右转"
        self._queue_overlay(
            active_keys=hotkey.upper(),
            last_action=f"{hotkey.upper()} 按下 · {direction_name}",
            log=f"{hotkey.upper()}：开始{direction_name}",
        )
        print(f"camera turn start mode={self.camera_mode} direction={direction}")

    def _stop_camera_turn(self, hotkey: str | None = None) -> None:
        with self.lock:
            session = self.camera_session
            if session is None or (hotkey is not None and session.hotkey != hotkey):
                return
            if session.released_monotonic is None:
                session.released_monotonic = time.monotonic()
            session.stop_event.set()

    def _run_camera_turn(self, session: CameraTurnSession) -> None:
        succeeded = False
        try:
            if self.camera_controller is None or self.camera_origin is None:
                raise RuntimeError("camera input controller is not initialized")
            execute_camera_turn(
                controller=self.camera_controller,
                origin=self.camera_origin,
                mode=self.camera_mode,
                direction=session.direction,
                settings=self.camera_settings,
                duration_ms=None,
                control=CameraReplayControl(
                    stop_requested=lambda: session.stop_event.is_set() or self.stop_event.is_set(),
                ),
            )
            succeeded = True
        except CameraReplayInterrupted:
            succeeded = True
        except Exception as exc:
            logging.exception("camera turn failed mode=%s direction=%s", self.camera_mode, session.direction)
            with self.lock:
                self.camera_failure = exc
            self.stop_event.set()
        finally:
            released_at = session.released_monotonic or time.monotonic()
            duration_ms = max(1, int(round((released_at - session.started_monotonic) * 1000.0)))
            if succeeded:
                self.append_event(
                    {
                        "t_ms": session.t_ms,
                        "type": "camera_turn",
                        "mode": self.camera_mode,
                        "direction": session.direction,
                        "duration_ms": duration_ms,
                    }
                )
                direction_name = "左转" if session.direction == "left" else "右转"
                self._queue_overlay(
                    active_keys=self._format_active_keys(),
                    last_action=f"已记录 {direction_name} {duration_ms} ms",
                    log=f"{session.hotkey.upper()}：{direction_name} {duration_ms} ms",
                )
                print(
                    f"camera turn saved mode={self.camera_mode} "
                    f"direction={session.direction} duration_ms={duration_ms}"
                )
            with self.lock:
                if self.camera_session is session:
                    self.camera_session = None
                self.camera_input_active.clear()

    def on_move(self, x: int, y: int) -> None:
        if not self.accepting_events or self.camera_input_active.is_set():
            return
        now = time.monotonic()
        if self.move_sample_seconds > 0 and now - self.last_move_monotonic < self.move_sample_seconds:
            return
        self.last_move_monotonic = now
        with self.lock:
            buttons = sorted(self.active_buttons)
        self.append_event(
            {
                "type": "mouse_move",
                "screen": [int(x), int(y)],
                "buttons": buttons,
            }
        )

    def on_click(self, x: int, y: int, button: mouse.Button, pressed: bool) -> None:
        if not self.accepting_events or self.camera_input_active.is_set():
            return
        name = mouse_button_name(button)
        with self.lock:
            if pressed:
                self.active_buttons.add(name)
                event_type = "mouse_down"
            else:
                self.active_buttons.discard(name)
                event_type = "mouse_up"
            buttons = sorted(self.active_buttons)

        self.append_event(
            {
                "type": event_type,
                "button": name,
                "screen": [int(x), int(y)],
                "buttons": buttons,
            }
        )

    def on_scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        if not self.accepting_events or self.camera_input_active.is_set():
            return
        self.append_event(
            {
                "type": "mouse_scroll",
                "screen": [int(x), int(y)],
                "delta": [int(dx), int(dy)],
            }
        )

    def on_key_press(self, key: keyboard.Key | keyboard.KeyCode) -> bool | None:
        if not self.accepting_events:
            return None
        name = key_name(key)
        if name == self.mark_key:
            self.append_event({"type": "mark", "key": name})
            print(f"mark t_ms={self.elapsed_ms()}")
            return None
        if name in {self.stop_key, "esc"}:
            self._stop_camera_turn()
            self.append_event({"type": "session_stop", "key": name})
            self.stop_event.set()
            return False
        if name == "f5":
            self._start_camera_turn(name, "left")
            return None
        if name == "f6":
            self._start_camera_turn(name, "right")
            return None

        with self.lock:
            if name in self.active_keys:
                return None
            self.active_keys.add(name)
            keys = sorted(self.active_keys)

        self.append_event({"type": "key_down", "key": name, "keys": keys})
        self._queue_overlay(active_keys=self._format_active_keys(), last_action=f"{name.upper()} 按下")
        return None

    def on_key_release(self, key: keyboard.Key | keyboard.KeyCode) -> None:
        if not self.accepting_events:
            return
        name = key_name(key)
        if name in {self.stop_key, "esc", self.mark_key}:
            return
        if name in {"f5", "f6"}:
            self._stop_camera_turn(name)
            return

        with self.lock:
            if name not in self.active_keys:
                return
            self.active_keys.discard(name)
            keys = sorted(self.active_keys)

        self.append_event({"type": "key_up", "key": name, "keys": keys})
        self._queue_overlay(active_keys=self._format_active_keys(), last_action=f"{name.upper()} 松开")

    def write_output(self) -> Path:
        if self.capture_region is None or self.capture_origin is None or self.hwnd is None:
            raise WindowNotFoundError("Refusing to save a recording without resolved client coordinates.")
        output = self.output_dir / f"recording_{self.session_id}.json"
        with self.lock:
            events = sorted(
                (dict(event) for event in self.events),
                key=lambda event: int(event.get("t_ms", 0)),
            )
        replayable_types = {
            "key_down",
            "key_up",
            "mouse_move",
            "mouse_down",
            "mouse_up",
            "mouse_scroll",
            "camera_turn",
        }
        if not any(str(event.get("type", "")) in replayable_types for event in events):
            raise EmptyRecordingError("No keyboard, mouse, click, scroll, or F5/F6 camera input was recorded.")
        payload = {
            "version": 1,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "duration_ms": self.elapsed_ms(),
            "recording_mode": "input_only",
            "window": {
                "title": self.title,
                "hwnd": self.hwnd,
                "capture_origin": list(self.capture_origin),
                "capture_region": list(self.capture_region) if self.capture_region else None,
                "coordinate_source": self.coordinate_source,
            },
            "screen_size": list(pyautogui.size()),
            "stop_key": self.stop_key,
            "mark_key": self.mark_key,
            "move_sample_ms": int(round(self.move_sample_seconds * 1000)),
            "camera_control": {
                "left_key": "f5",
                "right_key": "f6",
                "mode": self.camera_mode,
                "settings": self.camera_settings.to_dict(),
            },
            "screenshot_mode": "none",
            "events": events,
        }
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return output

    def run(self) -> Path:
        self.refresh_window()
        mouse_listener = mouse.Listener(
            on_move=self.on_move,
            on_click=self.on_click,
            on_scroll=self.on_scroll,
        )
        keyboard_listener = keyboard.Listener(
            on_press=self.on_key_press,
            on_release=self.on_key_release,
        )
        self.accepting_events = False
        try:
            mouse_listener.start()
            keyboard_listener.start()
            start_stdin_stop_listener(self.stop_event)
            mouse_listener.wait()
            keyboard_listener.wait()
            if self.stop_event.is_set():
                raise RecordingCancelled("Input recording was cancelled before the listeners became ready.")

            self.started_monotonic = time.monotonic()
            self.started_at = datetime.now().isoformat(timespec="seconds")
            self.session_id = timestamp()
            self.accepting_events = True
            self.append_event({"type": "session_start"})
            print(
                f"input recording ready: stop={self.stop_key.upper()}/ESC "
                f"mark={self.mark_key.upper()} camera=F5/F6"
            )
            print("record WASD, keyboard, mouse and clicks; hold F5/F6 to turn the camera")
            print("unbind F5/F6 in the game before recording; pynput hotkeys are not suppressed")
            self._queue_overlay(
                run_state="running",
                current_step="录制键鼠 · F5 左转 · F6 右转 · F10/ESC 保存",
                active_keys="-",
                mouse="-",
                last_action="等待输入",
                last_marker="-",
                last_score="-",
                log="键鼠录制已就绪",
            )
            self._pump_overlay(force=True)
            while not self.stop_event.wait(0.05):
                self._pump_overlay()
        except KeyboardInterrupt:
            self.append_event({"type": "session_stop", "key": "keyboard_interrupt"})
            self.stop_event.set()
        finally:
            self.accepting_events = False
            self.stop_event.set()
            self._stop_camera_turn()
            for worker in list(self.camera_workers):
                worker.join(timeout=3.0)
                if worker.is_alive() and self.camera_failure is None:
                    self.camera_failure = RuntimeError("camera turn worker did not stop")
            with self.lock:
                has_stop_event = any(event.get("type") == "session_stop" for event in self.events)
            if self.events and not has_stop_event:
                self.append_event({"type": "session_stop", "key": "ui_stop"})
            mouse_listener.stop()
            keyboard_listener.stop()
            mouse_listener.join(timeout=2)
            keyboard_listener.join(timeout=2)
            if self.camera_controller is not None:
                self.camera_controller.release_all()
            if self.overlay is not None:
                self._queue_overlay(
                    run_state="stopped",
                    active_keys="-",
                    mouse="-",
                    last_action="正在保存录制",
                    log="录制结束",
                )
                self._pump_overlay(force=True)
                self.overlay.close()
        if self.camera_failure is not None:
            raise RuntimeError("F5/F6 camera turn failed during input recording") from self.camera_failure
        return self.write_output()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record farm mouse/keyboard actions without screenshots.")
    parser.add_argument("--config", default="configs/config.yaml")
    parser.add_argument("--output-dir", default="logs/recordings")
    parser.add_argument("--countdown", type=float, default=3.0, help="Seconds to wait before recording starts.")
    parser.add_argument(
        "--move-sample-ms",
        type=int,
        default=1,
        help="Mouse move sampling interval in milliseconds (default: 1, up to about 1000 Hz). Use 0 to record all moves.",
    )
    parser.add_argument("--stop-key", default="f10")
    parser.add_argument("--mark-key", default="f9")
    parser.add_argument("--camera-mode", choices=("hidden", "visible"), default="visible")
    parser.add_argument("--step-dx", type=int, default=4)
    parser.add_argument("--interval-ms", type=int, default=10)
    parser.add_argument("--anchor-x", type=int, default=800)
    parser.add_argument("--anchor-y", type=int, default=450)
    parser.add_argument("--segment-ms", type=int, default=250)
    parser.add_argument("--segment-gap-ms", type=int, default=30)
    parser.add_argument("--invert", action="store_true")
    parser.add_argument("--overlay", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    setup_logging(config)
    if bool(config.get("runtime", {}).get("require_admin", True)) and not is_admin():
        print("administrator privileges are required for F5/F6 camera input")
        return 2
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    countdown = max(0.0, float(args.countdown))
    if countdown:
        print(f"input recording will start in {countdown:.1f}s; focus the game window")
        time.sleep(countdown)

    recorder = InputActionRecorder(
        config=config,
        output_dir=output_dir,
        move_sample_ms=args.move_sample_ms,
        stop_key=args.stop_key,
        mark_key=args.mark_key,
        camera_mode=args.camera_mode,
        camera_settings=CameraSettings(
            step_dx=max(1, abs(int(args.step_dx))),
            interval_ms=max(1, int(args.interval_ms)),
            anchor_client=(int(args.anchor_x), int(args.anchor_y)),
            segment_ms=max(1, int(args.segment_ms)),
            segment_gap_ms=max(0, int(args.segment_gap_ms)),
            invert=bool(args.invert),
        ),
        overlay_enabled=bool(args.overlay),
    )
    try:
        output = recorder.run()
    except RecordingCancelled as exc:
        print(str(exc))
        return 130
    except EmptyRecordingError as exc:
        print(f"input recording not saved: {exc}")
        return 3
    print(f"input recording saved={output}")
    print(f"events={len(recorder.events)} screenshots=none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
