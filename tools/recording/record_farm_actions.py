from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pyautogui
from pynput import keyboard, mouse

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from main import load_config, setup_logging
from src.core.capture import save_screenshot
from src.core.game_window import capture_game_client, screen_to_client
from src.core.input import is_admin
from src.utils.window import WindowNotFoundError


def recording_dir(root: Path) -> Path:
    path = root / "logs" / "recordings"
    path.mkdir(parents=True, exist_ok=True)
    return path


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


def non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be zero or greater")
    return parsed


class FarmActionRecorder:
    def __init__(
        self,
        config: dict[str, Any],
        output_dir: Path,
        move_sample_ms: int,
        screenshot_mode: str,
        stop_key: str,
        mark_key: str,
        screenshot_interval_ms: int = 250,
    ) -> None:
        self.config = config
        self.output_dir = output_dir
        self.move_sample_seconds = max(0.0, float(move_sample_ms) / 1000.0)
        self.screenshot_mode = screenshot_mode
        self.screenshot_interval_ms = int(screenshot_interval_ms)
        if self.screenshot_interval_ms < 0:
            raise ValueError("screenshot_interval_ms must be zero or greater")
        self.screenshot_interval_seconds = float(self.screenshot_interval_ms) / 1000.0
        self.stop_key = stop_key.lower()
        self.mark_key = mark_key.lower()
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.capture_lock = threading.Lock()
        self.events: list[dict[str, Any]] = []
        self.started_monotonic = time.monotonic()
        self.started_at = datetime.now().isoformat(timespec="seconds")
        self.last_move_monotonic = 0.0
        self.active_buttons: set[str] = set()
        self.capture_origin: tuple[int, int] | None = None
        self.capture_region: tuple[int, int, int, int] | None = None
        self.hwnd: int | None = None
        self.title = ""
        self.session_id = timestamp()
        self.screenshot_dir = self.output_dir / f"recording_{self.session_id}_screens"
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)

    def refresh_window(self) -> None:
        use_client_region = bool(self.config.get("capture", {}).get("use_client_region", True))
        shot, _, title, hwnd, bounds = capture_game_client(
            self.config.get("window", {}),
            activate=True,
            use_client_region=use_client_region,
        )
        if hwnd is None or bounds is None or shot.region is None:
            raise WindowNotFoundError(
                "Farm recording aborted: the configured game window could not be resolved and activated."
            )
        self.capture_origin = shot.origin
        self.capture_region = shot.region
        self.hwnd = hwnd
        self.title = title
        self._save_screenshot("session_start", shot.image)

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

    def append_event(self, event: dict[str, Any], screenshot_label: str | None = None) -> None:
        event.setdefault("t_ms", self.elapsed_ms())
        if "screen" in event:
            client = self.screen_to_record_client(int(event["screen"][0]), int(event["screen"][1]))
            event["client"] = list(client) if client is not None else None
            event["inside_client"] = self._inside_client(client)

        if screenshot_label and self._should_capture_for(event["type"]):
            path = self.capture_screenshot(screenshot_label)
            if path is not None:
                event["screenshot"] = str(path.relative_to(ROOT))

        with self.lock:
            self.events.append(event)

    def _inside_client(self, client: tuple[int, int] | None) -> bool | None:
        if client is None or self.capture_region is None:
            return None
        _, _, width, height = self.capture_region
        x, y = client
        return 0 <= x < width and 0 <= y < height

    def _should_capture_for(self, event_type: str) -> bool:
        if self.screenshot_mode == "none":
            return False
        if self.screenshot_mode == "all":
            return True
        if self.screenshot_mode == "marks":
            return event_type in {"session_start", "session_stop", "mark"}
        return event_type in {
            "session_start",
            "session_stop",
            "mark",
            "mouse_down",
            "mouse_up",
            "key_down",
        }

    def capture_screenshot(self, label: str) -> Path | None:
        try:
            with self.capture_lock:
                shot, _, title, hwnd, _ = capture_game_client(
                    self.config.get("window", {}),
                    activate=False,
                    use_client_region=bool(self.config.get("capture", {}).get("use_client_region", True)),
                )
                self.capture_origin = shot.origin
                self.capture_region = shot.region
                self.hwnd = hwnd
                self.title = title
                return self._save_screenshot(label, shot.image)
        except Exception:
            logging.exception("failed to capture recording screenshot label=%s", label)
            return None

    def _auto_capture_loop(self) -> None:
        if self.screenshot_interval_seconds <= 0:
            self.stop_event.wait()
            return

        next_capture = time.monotonic() + self.screenshot_interval_seconds
        sequence = 1
        while True:
            wait_seconds = max(0.0, next_capture - time.monotonic())
            if self.stop_event.wait(wait_seconds):
                return

            self.capture_screenshot(f"auto_{sequence:06d}")
            sequence += 1
            next_capture += self.screenshot_interval_seconds

            now = time.monotonic()
            if next_capture <= now:
                missed_intervals = math.floor(
                    (now - next_capture) / self.screenshot_interval_seconds
                ) + 1
                next_capture += missed_intervals * self.screenshot_interval_seconds

    def _save_screenshot(self, label: str, image) -> Path:
        path = self.screenshot_dir / f"{self.elapsed_ms():07d}_{label}.png"
        save_screenshot(image, path)
        return path

    def _request_stop(self, key: str) -> None:
        self.stop_event.set()
        event = {"type": "session_stop", "key": key, "t_ms": self.elapsed_ms()}
        with self.lock:
            if any(item.get("type") == "session_stop" for item in self.events):
                return
            self.events.append(event)

    def _attach_stop_screenshot(self) -> None:
        if not self._should_capture_for("session_stop"):
            return
        path = self.capture_screenshot("session_stop")
        if path is None:
            return
        relative_path = str(path.relative_to(ROOT))
        with self.lock:
            for event in reversed(self.events):
                if event.get("type") == "session_stop":
                    event["screenshot"] = relative_path
                    return

    def on_move(self, x: int, y: int) -> None:
        now = time.monotonic()
        if self.move_sample_seconds > 0 and now - self.last_move_monotonic < self.move_sample_seconds:
            return
        self.last_move_monotonic = now
        self.append_event(
            {
                "type": "mouse_move",
                "screen": [int(x), int(y)],
                "buttons": sorted(self.active_buttons),
            }
        )

    def on_click(self, x: int, y: int, button: mouse.Button, pressed: bool) -> None:
        name = mouse_button_name(button)
        if pressed:
            self.active_buttons.add(name)
            event_type = "mouse_down"
        else:
            self.active_buttons.discard(name)
            event_type = "mouse_up"
        self.append_event(
            {
                "type": event_type,
                "button": name,
                "screen": [int(x), int(y)],
                "buttons": sorted(self.active_buttons),
            },
            screenshot_label=f"{event_type}_{name}",
        )

    def on_scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        self.append_event(
            {
                "type": "mouse_scroll",
                "screen": [int(x), int(y)],
                "delta": [int(dx), int(dy)],
            }
        )

    def on_key_press(self, key: keyboard.Key | keyboard.KeyCode) -> bool | None:
        name = key_name(key)
        if name == self.mark_key:
            self.append_event({"type": "mark", "key": name}, screenshot_label="mark")
            print(f"mark t_ms={self.elapsed_ms()}")
            return None
        if name == self.stop_key:
            self._request_stop(name)
            return False
        self.append_event({"type": "key_down", "key": name}, screenshot_label=f"key_down_{name}")
        return None

    def on_key_release(self, key: keyboard.Key | keyboard.KeyCode) -> None:
        name = key_name(key)
        if name not in {self.stop_key, self.mark_key}:
            self.append_event({"type": "key_up", "key": name})

    def write_output(self) -> Path:
        output = self.output_dir / f"recording_{self.session_id}.json"
        payload = {
            "version": 1,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "duration_ms": self.elapsed_ms(),
            "window": {
                "title": self.title,
                "hwnd": self.hwnd,
                "capture_origin": list(self.capture_origin),
                "capture_region": list(self.capture_region) if self.capture_region else None,
            },
            "screen_size": list(pyautogui.size()),
            "stop_key": self.stop_key,
            "mark_key": self.mark_key,
            "move_sample_ms": int(round(self.move_sample_seconds * 1000)),
            "screenshot_mode": self.screenshot_mode,
            "screenshot_interval_ms": self.screenshot_interval_ms,
            "events": self.events,
        }
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return output

    def run(self) -> Path:
        self.refresh_window()
        self.append_event({"type": "session_start"}, screenshot_label="session_start")
        print(f"recording started: stop={self.stop_key.upper()} mark={self.mark_key.upper()}")
        if self.screenshot_interval_ms > 0:
            print(f"automatic screenshots every {self.screenshot_interval_ms} ms")
        else:
            print("automatic screenshots disabled")
        print("operate in the game window now...")

        mouse_listener = mouse.Listener(
            on_move=self.on_move,
            on_click=self.on_click,
            on_scroll=self.on_scroll,
        )
        keyboard_listener = keyboard.Listener(
            on_press=self.on_key_press,
            on_release=self.on_key_release,
        )
        mouse_listener.start()
        keyboard_listener.start()
        try:
            self._auto_capture_loop()
        except KeyboardInterrupt:
            logging.info("farm action recording stopped by Ctrl+C")
            self._request_stop("keyboard_interrupt")
        finally:
            self.stop_event.set()
            mouse_listener.stop()
            keyboard_listener.stop()
            mouse_listener.join(timeout=2)
            keyboard_listener.join(timeout=2)
        with self.lock:
            has_stop_event = any(event.get("type") == "session_stop" for event in self.events)
        if not has_stop_event:
            self.append_event({"type": "session_stop", "key": "listener_stop"})
        self._attach_stop_screenshot()
        return self.write_output()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record farm mouse/keyboard actions with client coordinates.")
    parser.add_argument("--config", default="configs/config.yaml")
    parser.add_argument("--output-dir", default="logs/recordings")
    parser.add_argument("--countdown", type=float, default=3.0, help="Seconds to wait before recording starts.")
    parser.add_argument(
        "--move-sample-ms",
        type=int,
        default=10,
        help="Mouse move sampling interval in milliseconds (default: 10, about 100 Hz). Use 0 to record all moves.",
    )
    parser.add_argument(
        "--screenshot-mode",
        choices=("important", "marks", "all", "none"),
        default="important",
        help="When to save event-triggered client screenshots.",
    )
    parser.add_argument(
        "--screenshot-interval-ms",
        type=non_negative_int,
        default=250,
        help="Automatic client screenshot interval in milliseconds (default: 250). Use 0 to disable.",
    )
    parser.add_argument("--stop-key", default="f10")
    parser.add_argument("--mark-key", default="f9")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    setup_logging(config)
    if bool(config.get("runtime", {}).get("require_admin", True)) and not is_admin():
        print("administrator privileges are required for reliable recording hotkeys")
        return 2
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    countdown = max(0.0, float(args.countdown))
    if countdown:
        print(f"recording will start in {countdown:.1f}s; focus the game window")
        time.sleep(countdown)

    recorder = FarmActionRecorder(
        config=config,
        output_dir=output_dir,
        move_sample_ms=args.move_sample_ms,
        screenshot_mode=args.screenshot_mode,
        stop_key=args.stop_key,
        mark_key=args.mark_key,
        screenshot_interval_ms=args.screenshot_interval_ms,
    )
    output = recorder.run()
    print(f"recording saved={output}")
    print(f"events={len(recorder.events)} screenshots={recorder.screenshot_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
