from __future__ import annotations

import argparse
import json
import logging
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any

from pynput import keyboard

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
from src.core.input import ClientCoordinateMapper, InputController, SafetyError, is_admin
from src.utils.window import activate_window, get_client_bounds, resolve_window
from tools.recording.input_tool_common import start_stdin_stop_listener


class ReplayInterrupted(RuntimeError):
    pass


def load_recording(path: Path) -> dict[str, Any]:
    if not path.is_absolute():
        path = ROOT / path
    return json.loads(path.read_text(encoding="utf-8"))


def event_time_ms(event: dict[str, Any]) -> int:
    return int(event.get("t_ms", 0))


def build_edges(
    events: list[dict[str, Any]],
    include_unheld_mouse_moves: bool = True,
) -> list[dict[str, Any]]:
    held_keys: set[str] = set()
    held_buttons: set[str] = set()
    edges: list[dict[str, Any]] = []

    for event in events:
        event_type = str(event.get("type", ""))
        t_ms = event_time_ms(event)

        if event_type in {"key_down", "key_up"}:
            key = str(event.get("key", "")).strip().lower()
            if not key:
                continue
            if event_type == "key_down":
                if key in held_keys:
                    continue
                held_keys.add(key)
                edges.append({"t_ms": t_ms, "kind": "key_down", "key": key})
            elif key in held_keys:
                held_keys.remove(key)
                edges.append({"t_ms": t_ms, "kind": "key_up", "key": key})
            continue

        if event_type == "mouse_move" and (held_buttons or include_unheld_mouse_moves):
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

        if event_type in {"mouse_down", "mouse_up"}:
            button = str(event.get("button", "left")).strip().lower()
            if event_type == "mouse_down":
                if button in held_buttons:
                    continue
                held_buttons.add(button)
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
            else:
                held_buttons.discard(button)
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
            continue

        if event_type == "mouse_scroll":
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
            mode = str(event.get("mode", "")).strip().lower()
            direction = str(event.get("direction", "")).strip().lower()
            try:
                duration_ms = int(event.get("duration_ms", 0))
            except (TypeError, ValueError):
                continue
            if mode not in {"hidden", "visible"} or direction not in {"left", "right"} or duration_ms <= 0:
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

    return sorted(edges, key=event_time_ms)


def mouse_screen_point(edge: dict[str, Any], mapper: ClientCoordinateMapper) -> tuple[int, int]:
    return mapper.screen_point(edge.get("client"), bool(edge.get("inside_client", False)))


def camera_settings_from_recording(recording: dict[str, Any]) -> CameraSettings:
    camera_control = recording.get("camera_control", {})
    if isinstance(camera_control, dict):
        raw_settings = camera_control.get("settings", {})
        if isinstance(raw_settings, dict) and raw_settings:
            return CameraSettings.from_dict(raw_settings)
    legacy_settings = recording.get("settings", {})
    if isinstance(legacy_settings, dict):
        return CameraSettings.from_dict(legacy_settings)
    raise ValueError("recording camera settings must be an object")


def requires_coordinate_mapper(edge: dict[str, Any]) -> bool:
    kind = str(edge.get("kind", ""))
    if kind in {"mouse_move", "mouse_down", "mouse_scroll"}:
        return True
    return kind == "mouse_up" and (edge.get("client") is not None or edge.get("screen") is not None)


def sleep_until(deadline: float, stop_event: threading.Event) -> None:
    while True:
        if stop_event.is_set():
            raise ReplayInterrupted("recording replay stopped")
        remaining_seconds = float(deadline) - time.monotonic()
        if remaining_seconds <= 0:
            return
        time.sleep(min(0.005, remaining_seconds))


def replay_recording(path: Path) -> int:
    config = load_config(ROOT / "configs" / "config.yaml")
    setup_logging(config)
    if bool(config.get("runtime", {}).get("require_admin", True)) and not is_admin():
        raise SafetyError("Administrator privileges are required for recording replay.")
    recording = load_recording(path)
    events = recording.get("events", [])
    if not isinstance(events, list):
        raise ValueError("recording events must be a list")

    edges = build_edges(events, include_unheld_mouse_moves=True)
    if not edges:
        raise ValueError(f"recording has no replayable keyboard, mouse, click, scroll, or camera input: {path}")

    selector, _, _ = resolve_target_selector(config.get("window", {}))
    resolved = resolve_window(selector)
    if resolved is None:
        raise SafetyError("Target game window could not be resolved; recording replay aborted.")
    activate_window(selector=selector)
    client_bounds = get_client_bounds(resolved.hwnd)
    coordinate_edges = [edge for edge in edges if requires_coordinate_mapper(edge)]
    mapper: ClientCoordinateMapper | None = None
    if coordinate_edges:
        mapper = ClientCoordinateMapper.from_recording(
            recording,
            origin=(client_bounds.left, client_bounds.top),
            current_size=(client_bounds.width, client_bounds.height),
        )
        for edge in coordinate_edges:
            mouse_screen_point(edge, mapper)

    camera_edges = [edge for edge in edges if edge.get("kind") == "camera_turn"]
    camera_settings = camera_settings_from_recording(recording) if camera_edges else None
    previous_camera_end_ms = -1
    for edge in camera_edges:
        start_ms = int(edge["t_ms"])
        if start_ms < previous_camera_end_ms:
            raise ValueError("recording contains overlapping F5/F6 camera turns")
        previous_camera_end_ms = start_ms + int(edge["duration_ms"])

    input_controller = InputController(
        click_delay_seconds=float(config.get("input", {}).get("click_delay_seconds", 0.15)),
        click_hold_seconds=float(config.get("input", {}).get("click_hold_seconds", 0.08)),
        move_duration_seconds=float(config.get("input", {}).get("move_duration_seconds", 0.05)),
        click_backend=str(config.get("input", {}).get("click_backend", "sendinput")),
        fail_safe=bool(config.get("input", {}).get("fail_safe", True)),
        target_window_title=resolved.title,
        target_window_hwnd=resolved.hwnd,
    )

    print(f"replay file={path}")
    print(f"events={len(events)} replay_edges={len(edges)}")
    print("starting replay... F10/ESC or BatterUI safe stop ends the replay")

    max_lateness_ms = 0.0
    active_keys: set[str] = set()
    active_buttons: set[str] = set()
    stop_event = threading.Event()
    camera_workers: list[threading.Thread] = []
    camera_errors: queue.SimpleQueue[BaseException] = queue.SimpleQueue()

    def raise_camera_error() -> None:
        try:
            error = camera_errors.get_nowait()
        except queue.Empty:
            return
        raise RuntimeError("F5/F6 camera turn failed during recording replay") from error

    def start_camera_turn(edge: dict[str, Any]) -> None:
        if camera_settings is None:
            raise ValueError("recording camera settings are missing")

        def run_camera_turn() -> None:
            try:
                execute_camera_turn(
                    controller=input_controller,
                    origin=(client_bounds.left, client_bounds.top),
                    mode=str(edge["mode"]),
                    direction=str(edge["direction"]),
                    settings=camera_settings,
                    duration_ms=int(edge["duration_ms"]),
                    control=CameraReplayControl(stop_requested=stop_event.is_set),
                )
            except CameraReplayInterrupted as exc:
                if not stop_event.is_set():
                    camera_errors.put(exc)
                    stop_event.set()
            except BaseException as exc:
                camera_errors.put(exc)
                stop_event.set()

        worker = threading.Thread(target=run_camera_turn, name="recording-camera-turn", daemon=True)
        camera_workers.append(worker)
        worker.start()

    def on_stop_key(key: keyboard.Key | keyboard.KeyCode) -> bool | None:
        if key in {keyboard.Key.f10, keyboard.Key.esc}:
            stop_event.set()
            return False
        return None

    stop_listener = keyboard.Listener(on_press=on_stop_key)
    stop_listener.start()
    stop_listener.wait()
    start_stdin_stop_listener(stop_event)
    replay_started = time.monotonic()
    result = 0
    try:
        for edge in edges:
            raise_camera_error()
            if stop_event.is_set():
                raise ReplayInterrupted("recording replay stopped")
            t_ms = int(edge["t_ms"])
            target_deadline = replay_started + max(0, t_ms) / 1000.0
            sleep_until(target_deadline, stop_event)
            max_lateness_ms = max(
                max_lateness_ms,
                max(0.0, time.monotonic() - target_deadline) * 1000.0,
            )

            kind = str(edge["kind"])
            if kind == "key_down":
                key = str(edge["key"])
                active_keys.add(key)
                input_controller.key_down(key)
            elif kind == "key_up":
                key = str(edge["key"])
                active_keys.discard(key)
                input_controller.key_up(key)
            elif kind == "mouse_down":
                if mapper is None:
                    raise SafetyError("recording mouse coordinates are unavailable")
                button = str(edge.get("button", "left"))
                x, y = mouse_screen_point(edge, mapper)
                active_buttons.add(button)
                input_controller.move_to(x, y, duration_seconds=0.0)
                input_controller.mouse_down(button)
            elif kind == "mouse_move":
                if mapper is None:
                    raise SafetyError("recording mouse coordinates are unavailable")
                x, y = mouse_screen_point(edge, mapper)
                input_controller.move_to(x, y, duration_seconds=0.0)
            elif kind == "mouse_up":
                button = str(edge.get("button", "left"))
                if edge.get("client") is not None or edge.get("screen") is not None:
                    if mapper is None:
                        raise SafetyError("recording mouse coordinates are unavailable")
                    x, y = mouse_screen_point(edge, mapper)
                    input_controller.move_to(x, y, duration_seconds=0.0)
                active_buttons.discard(button)
                input_controller.mouse_up(button, after_delay_seconds=0.0)
            elif kind == "mouse_scroll":
                if mapper is None:
                    raise SafetyError("recording mouse coordinates are unavailable")
                x, y = mouse_screen_point(edge, mapper)
                input_controller.move_to(x, y, duration_seconds=0.0)
                delta = edge.get("delta", [0, 0])
                input_controller.scroll(dx=int(delta[0]), dy=int(delta[1]))
            elif kind == "camera_turn":
                start_camera_turn(edge)

        for worker in camera_workers:
            while worker.is_alive():
                raise_camera_error()
                if stop_event.is_set():
                    raise ReplayInterrupted("recording replay stopped")
                worker.join(timeout=0.01)
        raise_camera_error()
    except ReplayInterrupted:
        raise_camera_error()
        print("replay stopped safely")
        result = 130
    finally:
        stop_event.set()
        for key in list(active_keys):
            try:
                input_controller.key_up(key)
            except Exception:
                logging.exception("failed to release active key: %s", key)
        for button in list(active_buttons):
            try:
                input_controller.mouse_up(button, after_delay_seconds=0.0)
            except Exception:
                logging.exception("failed to release active mouse button: %s", button)
        for worker in camera_workers:
            worker.join(timeout=2.0)
        input_controller.release_all()
        stop_listener.stop()
        stop_listener.join(timeout=2.0)

    logging.info(
        "standalone recording replay timing edges=%s max_lateness_ms=%.3f",
        len(edges),
        max_lateness_ms,
    )
    if result == 0:
        print("replay finished")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay one BatterFarm recording JSON.")
    parser.add_argument("recording")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        return replay_recording(Path(args.recording))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, SafetyError) as exc:
        print(f"recording replay failed: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
